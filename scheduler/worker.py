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

# Máximo de caracteres para el mensaje de error guardado en Redis.
# Telegram tiene un límite de 4096 caracteres por mensaje; /status compone
# otras líneas también, así que acotamos el error a 300 chars.
MAX_ERROR_LEN = 300

# Terminales con scraper implementado.
SCRAPERS: dict[str, type[BaseScraper]] = {
    "T4": T4Scraper,
}


def _load_items(redis_client: RedisClient) -> list[dict]:
    """Lee los items activos del hash ITEMS_KEY."""
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
    """Arma el config del scraper desde el entorno."""
    t = terminal.upper()
    return {
        "url": os.environ.get(f"{t}_URL", ""),
        "user": os.environ.get(f"{t}_USER", ""),
        "pass": os.environ.get(f"{t}_PASS", ""),
    }


def _record_last_error(
    redis_client: RedisClient, terminal: str, booking: str, exc: object
) -> None:
    """Guarda en Redis el último error del worker (truncado) para /status."""
    stamp = datetime.utcnow().isoformat() + "Z"
    # Tomamos solo la primera línea del traceback para no superar el límite
    # de caracteres de Telegram en /status.
    exc_str = str(exc).split("\n")[0][:MAX_ERROR_LEN]
    msg = f"{stamp} — {terminal}/{booking}: {exc_str}"
    try:
        redis_client.client.set(WORKER_LAST_ERROR_KEY, msg)
    except Exception as set_exc:
        logger.error(f"[WORKER] no pude guardar worker:last_error: {set_exc}")


async def run_check_cycle(
    app: Application, redis_client: RedisClient
) -> None:
    """Ejecuta un ciclo completo de chequeo de disponibilidad."""
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

                                redis_client.set_state(terminal, booking, slots)
                                checked += 1
                            except Exception as exc:
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
        logger.exception(f"[WORKER] excepción inesperada en el ciclo: {exc}")
        _record_last_error(redis_client, "?", "?", exc)

    run_ts = datetime.utcnow().isoformat() + "Z"
    try:
        redis_client.client.set(WORKER_LAST_RUN_KEY, run_ts)
    except Exception as exc:
        logger.error(f"[WORKER] no pude guardar worker:last_run: {exc}")

    duration = (datetime.utcnow() - start).total_seconds()
    logger.info(
        f"[WORKER] ciclo completo — {checked} items chequeados ({duration:.2f}s)"
    )
