from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from loguru import logger

from bot.telegram_bot import setup_bot
from scheduler.worker import run_check_cycle
from storage.redis_client import RedisClient


def _configure_logger() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level="INFO",
    )


async def _main() -> None:
    load_dotenv()
    _configure_logger()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("[MAIN][BOOT] falta TELEGRAM_BOT_TOKEN en el entorno")
        raise SystemExit(1)

    interval_minutes = int(os.environ.get("MONITOR_INTERVAL_MINUTES", "10"))
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    logger.info("[MAIN][BOOT] arrancando terminal-monitor")
    logger.info(f"[MAIN][BOOT] intervalo de monitoreo: {interval_minutes} min")

    redis_client = RedisClient(redis_url)
    application = setup_bot(redis_client)

    scheduler = AsyncIOScheduler()
    # AsyncIOScheduler corre la corutina directamente sobre el event loop
    # de asyncio — no hay que envolverla en un lambda + create_task (eso
    # rompía con "RuntimeError: no running event loop").
    # run_check_cycle lanza el ciclo en un subprocess aparte (ver worker.py),
    # así que solo necesita el redis_client del padre para registrar estado;
    # el subprocess reconstruye su propio Bot de Telegram desde el entorno.
    scheduler.add_job(
        run_check_cycle,
        "interval",
        minutes=interval_minutes,
        args=[redis_client],
        id="check_cycle",
        next_run_time=datetime.now(),  # corre inmediatamente al arrancar
        max_instances=1,  # evita solapamiento de ciclos
        coalesce=True,  # si se acumulan disparos perdidos, corre uno solo
        misfire_grace_time=120,  # tolera hasta 2 min de atraso antes de descartar
    )
    scheduler.start()
    logger.info("[MAIN][BOOT] scheduler iniciado")

    async with application:
        await application.initialize()
        await application.start()
        if application.updater is not None:
            await application.updater.start_polling()
        logger.info("[MAIN][BOOT] bot de Telegram en polling")

        try:
            await asyncio.Event().wait()
        finally:
            logger.info("[MAIN][SHUTDOWN] deteniendo servicios")
            scheduler.shutdown(wait=False)
            if application.updater is not None:
                await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("[MAIN][SHUTDOWN] proceso terminado")
