from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from playwright.async_api import async_playwright
from telegram import Bot

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

# Timeout global del ciclo completo. El ciclo corre en un SUBPROCESS separado;
# si tarda más que esto se mata el árbol de procesos completo (incluido el
# driver node de Playwright), garantizando que el scheduler (max_instances=1)
# pueda arrancar el siguiente ciclo aunque Playwright se congele por completo.
# 480s = 8 min, holgado frente al intervalo default de 10 min entre ciclos.
CYCLE_TIMEOUT_SECONDS = 480

# Tiempo de gracia para que el SO recoja el proceso después de mandarle el kill.
KILL_REAP_TIMEOUT_SECONDS = 15

# El kill del árbol de procesos difiere por plataforma (Railway = Linux,
# desarrollo local = Windows).
IS_WINDOWS = os.name == "nt"

# Terminales con scraper implementado.
SCRAPERS: dict[str, type[BaseScraper]] = {
    "T4": T4Scraper,
}

# Lock en proceso para que un /check manual no se solape con el ciclo del
# scheduler (ni dos ciclos manuales entre sí). Vive en el event loop principal,
# compartido por el bot de Telegram y el scheduler. NO protege contra el
# subprocess en sí (ese ya tiene su propio aislamiento), sino contra disparar
# dos ciclos a la vez desde el proceso padre.
_cycle_lock = asyncio.Lock()


def is_cycle_running() -> bool:
    """True si hay un ciclo (del scheduler o manual) en curso ahora mismo."""
    return _cycle_lock.locked()


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


async def _run_check_cycle(bot: Bot, redis_client: RedisClient) -> None:
    """Cuerpo del ciclo de chequeo. Corre dentro del subprocess de ciclo; el
    timeout duro lo aplica el wrapper público `run_check_cycle` en el proceso
    padre matando este subprocess."""
    start = datetime.utcnow()
    logger.info(f"[WORKER][CYCLE] inicio {start.isoformat()}Z")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
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


def _safe_set_last_run(redis_client: RedisClient) -> None:
    """Marca en Redis que hubo una corrida (aunque haya sido abortada), para
    que /status no quede mostrando una corrida vieja."""
    try:
        redis_client.client.set(
            WORKER_LAST_RUN_KEY, datetime.utcnow().isoformat() + "Z"
        )
    except Exception as exc:
        logger.error(f"[WORKER] no pude guardar worker:last_run: {exc}")


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Mata el subprocess del ciclo y TODO su árbol de hijos (bloqueante).

    Es imprescindible matar el árbol completo (no solo el proceso python): el
    driver de Playwright lanza un proceso node + chromium como hijos; matar
    solo el python dejaría esos huérfanos colgados consumiendo memoria. Por eso
    en Linux mandamos SIGKILL a todo el grupo de procesos y en Windows usamos
    `taskkill /T` (recursivo).

    Corre dentro del thread de `_spawn_and_wait_cycle`, así que usa llamadas
    bloqueantes —no asyncio— a propósito.
    """
    if proc.poll() is not None:
        return

    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=KILL_REAP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error(f"[WORKER][CYCLE] taskkill falló: {exc}")
            try:
                proc.kill()
            except OSError:
                pass
    else:
        # SIGKILL al grupo de procesos completo (creado con start_new_session).
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.error(f"[WORKER][CYCLE] killpg falló: {exc}")
            try:
                proc.kill()
            except OSError:
                pass

    # Esperar a que el SO recoja el proceso para no dejar zombies.
    try:
        proc.wait(timeout=KILL_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error(
            f"[WORKER][CYCLE] el subprocess pid {proc.pid} no terminó tras el "
            f"kill ({KILL_REAP_TIMEOUT_SECONDS}s)"
        )


def _spawn_and_wait_cycle() -> tuple[int | None, str]:
    """Lanza el subprocess del ciclo y lo espera con timeout DURO bloqueante.

    Todo (spawn + wait + kill) es BLOQUEANTE y corre dentro de un hilo via
    `asyncio.to_thread`, de modo que el event loop principal (bot de Telegram +
    scheduler) nunca se bloquea. A diferencia de la versión anterior basada en
    `asyncio.create_subprocess_exec` + `asyncio.wait_for`:

    - `subprocess.Popen` + `proc.wait(timeout=...)` usan un timeout REAL del SO
      (waitpid con deadline), no la cancelación cooperativa de asyncio, que
      puede no propagarse y dejar la corutina colgada para siempre.
    - El spawn queda DENTRO del timeout/hilo: si crear el proceso se cuelga, el
      event loop sigue respondiendo (antes `create_subprocess_exec` no tenía
      timeout y podía wedgear el scheduler eternamente con max_instances=1).

    Devuelve `(returncode, status)` con status ∈ {"ok", "timeout",
    "spawn_error"}. En "spawn_error" returncode es None y el mensaje va en el
    segundo campo.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Aislar el subprocess en su propio grupo/sesión de procesos para poder
    # matar todo el árbol de una (ver _kill_process_tree).
    spawn_kwargs: dict = {}
    if IS_WINDOWS:
        spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        spawn_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "scheduler.worker"],
            cwd=repo_root,
            **spawn_kwargs,
        )
    except Exception as exc:
        logger.exception(f"[WORKER][CYCLE] no pude lanzar el subprocess: {exc}")
        return None, f"spawn_error: {exc}"

    logger.info(f"[WORKER][CYCLE] subprocess lanzado (pid {proc.pid})")

    try:
        rc = proc.wait(timeout=CYCLE_TIMEOUT_SECONDS)
        return rc, "ok"
    except subprocess.TimeoutExpired:
        logger.error(
            f"[WORKER][CYCLE] timeout global — matando subprocess pid "
            f"{proc.pid} (límite {CYCLE_TIMEOUT_SECONDS}s). El scheduler podrá "
            f"arrancar el próximo ciclo."
        )
        _kill_process_tree(proc)
        return None, "timeout"


async def run_check_cycle(redis_client: RedisClient) -> bool:
    """Ejecuta un ciclo completo de chequeo en un SUBPROCESS con timeout duro.

    El ciclo (login + scraping con Playwright) corre en un proceso aparte
    (`python -m scheduler.worker`). Si tarda más de `CYCLE_TIMEOUT_SECONDS` se
    mata el árbol de procesos completo —incluido el driver node de Playwright—,
    garantizando que el scheduler (max_instances=1) pueda lanzar el próximo
    ciclo aunque Playwright se cuelgue de forma irrecuperable.

    La gestión del subprocess (spawn + espera + kill) es bloqueante y se delega
    a un hilo con `asyncio.to_thread` para no bloquear el event loop. Ver
    `_spawn_and_wait_cycle` para por qué NO usamos la API asyncio de subprocess.

    El subprocess hereda stdout/stderr, así que sus logs aparecen integrados en
    los del proceso principal (Railway). Reconstruye sus propias conexiones a
    Redis y a Telegram desde el entorno, por lo que no necesita compartir
    objetos con el proceso padre.

    Un `_cycle_lock` evita solapar este ciclo con otro ya en curso (p. ej. un
    `/check` manual disparado mientras corre el del scheduler). Devuelve True si
    el ciclo efectivamente corrió, o False si se salteó por haber otro en curso.
    """
    if _cycle_lock.locked():
        logger.info(
            "[WORKER][CYCLE] ya hay un ciclo en curso — se saltea este disparo"
        )
        return False

    async with _cycle_lock:
        rc, status = await asyncio.to_thread(_spawn_and_wait_cycle)

        if status == "ok" and rc == 0:
            logger.info("[WORKER][CYCLE] subprocess finalizó ok")
        else:
            if status == "timeout":
                _record_last_error(
                    redis_client,
                    "?",
                    "?",
                    f"ciclo cancelado por timeout global ({CYCLE_TIMEOUT_SECONDS}s) — "
                    f"subprocess terminado",
                )
            elif status == "spawn_error":
                _record_last_error(
                    redis_client, "?", "?", f"no pude lanzar el ciclo: {rc}"
                )
            else:
                # status == "ok" pero rc != 0: el subprocess ya registró el error
                # puntual en Redis; esto cubre crashes que no alcanzaron a hacerlo
                # (segfault, OOM).
                logger.error(f"[WORKER][CYCLE] subprocess terminó con código {rc}")
                _record_last_error(
                    redis_client,
                    "?",
                    "?",
                    f"subprocess de ciclo terminó con código {rc}",
                )

            # El subprocess pudo no haber escrito worker:last_run; lo garantizamos
            # acá para que /status no muestre una corrida vieja.
            _safe_set_last_run(redis_client)

    return True


def _configure_subprocess_logger() -> None:
    """Mismo formato de log que main.py, para que los logs del subprocess se
    vean consistentes en Railway."""
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | {message}"
        ),
        level="INFO",
    )


async def _cycle_entrypoint() -> None:
    """Punto de entrada del subprocess de ciclo (`python -m scheduler.worker`).

    Construye sus propias conexiones (Redis + Bot de Telegram) desde el entorno
    y corre un único ciclo de chequeo. Se mantiene autónomo a propósito: no
    comparte ningún objeto con el proceso padre.
    """
    load_dotenv()
    _configure_subprocess_logger()

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("[WORKER][CYCLE] falta TELEGRAM_BOT_TOKEN — no puedo alertar")

    redis_client = RedisClient(redis_url)
    bot = Bot(token)
    # `async with bot` inicializa/cierra el Bot (necesario en PTB para usar
    # send_message fuera de una Application).
    async with bot:
        await _run_check_cycle(bot, redis_client)


if __name__ == "__main__":
    asyncio.run(_cycle_entrypoint())
