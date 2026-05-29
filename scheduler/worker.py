from __future__ import annotations

import json
import os
from datetime import datetime

from loguru import logger
from playwright.async_api import async_playwright
from telegram.ext import Application

from bot.telegram_bot import send_alert
from scrapers.base import BaseScraper
from scrapers.t4 import T4Scraper
from storage.redis_client import RedisClient


# Key del hash en Redis con los items monitoreados (la escribe el bot).
ITEMS_KEY = "items:active"
WORKER_LAST_RUN_KEY = "worker:last_run"
WORKER_LAST_ERROR_KEY = "worker:last_error"

# Terminales con scraper implementado. A medida que se agreguen TRP/Exolgan
# se registran acá.
SCRAPERS: dict[str, type[BaseScraper]] = {
    "T4": T4Scraper,
}


def _load_items(redis_client: RedisClient) -> list[dict]:
    """Lee los items activos del hash ITEMS_KEY.

    Cada item: {"id", "terminal", "booking", "desde_fecha"}.
    """
    try:
        raw = redis_client.client.hgetall(ITEMS_KEY)
    except Exception as exc:
        logger.error(f"[WORKER] error leyendo {ITEMS_KEY} de Redis: {exc}")
        return []

    items: list[dict] = []
    for item_id, payload in raw.items():
        try:
            data = json.loads(payload)
            items.append(
                {
                    "id": item_id,
                    "terminal": data.get("terminal", ""),
                    "booking": data.get("booking", ""),
                    "desde_fecha": data.get("desde_fecha"),
                }
            )
        except json.JSONDecodeError as exc:
            logger.warning(f"[WORKER] item corrupto {item_id}: {exc}")
    return items


def _config_for(terminal: str) -> dict:
    """Arma el config del scraper desde el entorno: {TERMINAL}_URL/_USER/_PASS."""
    t = terminal.upper()
    return {
        "url": os.environ.get(f"{t}_URL", ""),
        "user": os.environ.get(f"{t}_USER", ""),
        "pass": os.environ.get(f"{t}_PASS", ""),
    }


def _record_last_error(
    redis_client: RedisClient, terminal: str, booking: str, exc: object
) -> None:
    """Guarda en Redis el último error del worker para que /status lo muestre."""
    stamp = datetime.utcnow().isoformat() + "Z"
    msg = f"{stamp} — {terminal}/{booking}: {exc}"
    try:
        redis_client.client.set(WORKER_LAST_ERROR_KEY, msg)
    except Exception as set_exc:
        logger.error(f"[WORKER] no pude guardar worker:last_error: {set_exc}")


async def run_check_cycle(
    app: Application, redis_client: RedisClient
) -> None:
    """Ejecuta un ciclo completo de chequeo de disponibilidad.

    Lee los items activos de Redis, corre el scraper de cada terminal en un
    browser headless, compara con el snapshot anterior y dispara alertas por
    Telegram ante slots nuevos. Nunca propaga excepciones.
    """
    start = datetime.utcnow()
    logger.info(f"[WORKER][CYCLE] inicio {start.isoformat()}Z")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    bot = app.bot
    checked = 0

    try:
        items = _load_items(redis_client)
        if not items:
            logger.info("[WORKER] no hay items activos — nada que chequear")
        else:
            # Agrupar por terminal para reusar el mismo browser/sesión.
            by_terminal: dict[str, list[dict]] = {}
            for item in items:
                by_terminal.setdefault(item["terminal"], []).append(item)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        viewport={"width": 1920, "height": 1080}
                    )
                    page = await context.new_page()

                    for terminal, terminal_items in by_terminal.items():
                        scraper_cls = SCRAPERS.get(terminal)
                        if scraper_cls is None:
                            logger.warning(
                                f"[WORKER] terminal {terminal} sin scraper "
                                f"implementado — salteando {len(terminal_items)} item(s)"
                            )
                            continue

                        scraper = scraper_cls(page, _config_for(terminal))
                        logged_in = False

                        for item in terminal_items:
                            booking = item["booking"]
                            desde = item["desde_fecha"]
                            item_id = item["id"]
                            logger.info(
                                f"[WORKER] chequeando {terminal} — {booking} "
                                f"({item_id})"
                            )
                            try:
                                # Login una sola vez por terminal; reusar la
                                # sesión si ya está activa.
                                if not logged_in:
                                    if not await scraper.login():
                                        logger.error(
                                            f"[WORKER] login falló para {terminal} "
                                            f"— salteando sus items"
                                        )
                                        _record_last_error(
                                            redis_client,
                                            terminal,
                                            booking,
                                            "login falló",
                                        )
                                        break
                                    logged_in = True

                                slots = await scraper.check_availability(
                                    booking, desde_fecha=desde
                                )

                                # Diff contra el snapshot anterior: un slot es
                                # nuevo si su fecha no estaba guardada.
                                prev = (
                                    redis_client.get_state(terminal, booking) or []
                                )
                                prev_fechas = {s.get("fecha") for s in prev}
                                nuevos = [
                                    s
                                    for s in slots
                                    if s.get("fecha") not in prev_fechas
                                ]

                                if nuevos:
                                    await send_alert(
                                        bot, chat_id, terminal, booking, nuevos
                                    )
                                    logger.info(
                                        f"[WORKER] alerta enviada para {booking}: "
                                        f"{nuevos}"
                                    )
                                else:
                                    logger.info(
                                        f"[WORKER] sin cambios para {booking}"
                                    )

                                # Persistir el nuevo snapshot.
                                redis_client.set_state(terminal, booking, slots)
                                checked += 1
                            except Exception as exc:
                                # El error de un item no detiene a los demás.
                                logger.exception(
                                    f"[WORKER] error chequeando {terminal}/"
                                    f"{booking}: {exc}"
                                )
                                _record_last_error(
                                    redis_client, terminal, booking, exc
                                )
                finally:
                    await browser.close()
    except Exception as exc:
        # Nunca propagar fuera del ciclo: el scheduler debe seguir corriendo.
        logger.exception(f"[WORKER] excepción inesperada en el ciclo: {exc}")
        _record_last_error(redis_client, "?", "?", exc)

    # Marcar la última corrida (siempre, aun si no hubo items).
    run_ts = datetime.utcnow().isoformat() + "Z"
    try:
        redis_client.client.set(WORKER_LAST_RUN_KEY, run_ts)
    except Exception as exc:
        logger.error(f"[WORKER] no pude guardar worker:last_run: {exc}")

    duration = (datetime.utcnow() - start).total_seconds()
    logger.info(
        f"[WORKER] ciclo completo — {checked} items chequeados ({duration:.2f}s)"
    )
