from __future__ import annotations

import os

from loguru import logger
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes


PORTAL_URLS: dict[str, str] = {
    "TRP": os.environ.get("TRP_URL", ""),
    "T4": os.environ.get("T4_URL", ""),
    "EXOLGAN": os.environ.get("EXOLGAN_URL", ""),
}


async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/agregar] recibido")
    if update.message is not None:
        await update.message.reply_text("🚧 En construcción")


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/lista] recibido")
    if update.message is not None:
        await update.message.reply_text("🚧 En construcción")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/stop] recibido")
    if update.message is not None:
        await update.message.reply_text("🚧 En construcción")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/status] recibido")
    if update.message is not None:
        await update.message.reply_text("🚧 En construcción")


def build_application(token: str) -> Application:
    """Construye la `Application` de python-telegram-bot con los handlers registrados."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("agregar", cmd_agregar))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    logger.info("[BOT][INIT] handlers registrados: /agregar /lista /stop /status")
    return app


async def send_alert(
    bot: Bot,
    chat_id: str,
    terminal: str,
    identifier: str,
    slot: dict,
) -> None:
    """Envía una alerta de turno disponible al chat configurado."""
    portal_url = PORTAL_URLS.get(terminal, "")
    message = (
        f"🟢 TURNO DISPONIBLE — {terminal}\n"
        f"📦 Booking/Contenedor: {identifier}\n"
        f"📅 Día: {slot['fecha']}\n"
        f"⏰ Franja: {slot['franja']}\n"
        f"Coordiná ahora → {portal_url}"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=message)
        logger.info(
            f"[BOT][ALERT] enviada para {terminal}:{identifier} "
            f"({slot['fecha']} {slot['franja']})"
        )
    except Exception as exc:
        logger.error(
            f"[BOT][ALERT] error enviando alerta {terminal}:{identifier}: {exc}"
        )
