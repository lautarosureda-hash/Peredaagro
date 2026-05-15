from __future__ import annotations

import asyncio
import os
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from loguru import logger

from bot.telegram_bot import build_application
from scheduler.worker import run_check_cycle


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

    logger.info("[MAIN][BOOT] arrancando terminal-monitor")
    logger.info(f"[MAIN][BOOT] intervalo de monitoreo: {interval_minutes} min")

    application = build_application(token)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_check_cycle,
        "interval",
        minutes=interval_minutes,
        id="check_cycle",
        next_run_time=None,
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
