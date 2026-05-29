from __future__ import annotations

import json
import os
import time
from datetime import date, datetime

from loguru import logger
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from storage.redis_client import RedisClient


# Terminales soportadas y su URL de portal (para los links de las alertas).
PORTAL_URLS: dict[str, str] = {
    "TRP": os.environ.get("TRP_URL", ""),
    "T4": os.environ.get(
        "T4_URL",
        "https://apps.apmterminals.com.ar/puertodigital/"
        "CoordinationManagement/coordinationManagement",
    ),
    "EXOLGAN": os.environ.get("EXOLGAN_URL", ""),
}

# Key del hash en Redis con los items monitoreados activos.
ITEMS_KEY = "items:active"

# Keys donde el worker publica su estado (consumidas por /status). Si todavía
# no fueron escritas, /status degrada con valores por defecto.
WORKER_LAST_RUN_KEY = "worker:last_run"
WORKER_LAST_ERROR_KEY = "worker:last_error"

# Días de la semana en español (datetime.weekday(): Lunes=0 .. Domingo=6).
DAYS_ES: dict[int, str] = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}


# ---- Helpers ------------------------------------------------------------


def _get_redis(context: ContextTypes.DEFAULT_TYPE) -> RedisClient | None:
    """Devuelve el `RedisClient` guardado en `bot_data`, o None si no está."""
    client = context.application.bot_data.get("redis_client")
    return client if isinstance(client, RedisClient) else None


def _redis_alive(redis_client: RedisClient | None) -> bool:
    """Chequea que Redis responda. Nunca propaga la excepción."""
    if redis_client is None:
        return False
    try:
        return bool(redis_client.client.ping())
    except Exception as exc:
        logger.error(f"[BOT][REDIS] Redis no responde: {exc}")
        return False


def _authorized(update: Update) -> bool:
    """Solo el chat configurado en TELEGRAM_CHAT_ID puede operar el bot.

    Si la variable no está seteada (entorno de desarrollo), se permite todo.
    """
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return True
    if update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == str(chat_id)


def _make_item_id(terminal: str, booking: str) -> str:
    """Genera un id corto: TERMINAL-BOOKING-<4 dígitos del timestamp>."""
    stamp = str(int(time.time()))[-4:]
    return f"{terminal}-{booking}-{stamp}"


def _load_items(redis_client: RedisClient) -> list[dict]:
    """Lee los items monitoreados desde el hash ITEMS_KEY.

    Cada item: {"id", "terminal", "booking", "desde_fecha"}.
    """
    try:
        raw = redis_client.client.hgetall(ITEMS_KEY)
    except Exception as exc:
        logger.error(f"[BOT][REDIS] error leyendo {ITEMS_KEY}: {exc}")
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
            logger.warning(f"[BOT][REDIS] item corrupto {item_id}: {exc}")
    return items


def _format_fecha(fecha: str) -> str:
    """Convierte 'YYYY-MM-DD' a 'Día DD/MM' en español. Si falla, devuelve crudo.

    Se parsea como `date` puro (no `datetime`) para evitar cualquier conversión
    de zona horaria: con datetime+UTC, "2026-05-31" en Argentina (UTC-3) podía
    interpretarse como el día anterior y arrojar el nombre de día equivocado.
    """
    try:
        fecha_date = date.fromisoformat(fecha)
        dia_nombre = DAYS_ES[fecha_date.weekday()]
        return f"{dia_nombre} {fecha_date.day:02d}/{fecha_date.month:02d}"
    except Exception:
        return fecha


def _format_item_line(idx: int, item: dict) -> str:
    """Línea para /lista: '1. [id] T4 — BOOKING (desde: ...)'."""
    desde = item.get("desde_fecha")
    suffix = f" (desde: {desde})" if desde else ""
    return (
        f"{idx}. [{item['id']}] {item['terminal']} — "
        f"{item['booking']}{suffix}"
    )


# ---- Comandos -----------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/start] recibido")
    if update.message is None or not _authorized(update):
        return
    await update.message.reply_text(
        "👋 Bot de monitoreo de turnos portuarios — Pereda Agro\n"
        "\n"
        "Comandos disponibles:\n"
        "/agregar T4 <booking> [desde:YYYY-MM-DD] — monitorear un booking\n"
        "/lista — ver bookings monitoreados\n"
        "/stop <id> — detener el monitoreo de un item\n"
        "/status — estado del worker\n"
        "\n"
        "Ejemplo:\n"
        "/agregar T4 BUA0367004 desde:2026-05-31"
    )


async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"[BOT][/agregar] recibido args={context.args}")
    if update.message is None or not _authorized(update):
        return

    redis_client = _get_redis(context)
    if not _redis_alive(redis_client):
        await update.message.reply_text(
            "⚠️ No puedo acceder al almacenamiento (Redis). Intentá de nuevo en unos minutos."
        )
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /agregar T4 <booking> [desde:YYYY-MM-DD]\n"
            "Ejemplo: /agregar T4 BUA0367004 desde:2026-05-31"
        )
        return

    terminal = args[0].upper()
    if terminal not in PORTAL_URLS:
        await update.message.reply_text(
            f"Terminal desconocida: {terminal}. "
            f"Opciones: {', '.join(PORTAL_URLS.keys())}"
        )
        return

    booking = args[1]
    desde_fecha: str | None = None
    for extra in args[2:]:
        if extra.lower().startswith("desde:"):
            value = extra.split(":", 1)[1].strip()
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                await update.message.reply_text(
                    f"Fecha inválida en '{extra}'. Formato esperado: desde:YYYY-MM-DD"
                )
                return
            desde_fecha = value

    item_id = _make_item_id(terminal, booking)
    payload = json.dumps(
        {
            "id": item_id,
            "terminal": terminal,
            "booking": booking,
            "desde_fecha": desde_fecha,
        }
    )
    try:
        redis_client.client.hset(ITEMS_KEY, item_id, payload)
    except Exception as exc:
        logger.error(f"[BOT][/agregar] error guardando {item_id}: {exc}")
        await update.message.reply_text("⚠️ No pude guardar el item. Reintentá más tarde.")
        return

    logger.info(
        f"[BOT][/agregar] monitoreando {item_id} ({terminal}:{booking}, "
        f"desde={desde_fecha})"
    )
    desde_txt = f" (desde: {desde_fecha})" if desde_fecha else ""
    await update.message.reply_text(
        f"✅ Monitoreando {terminal} — {booking}{desde_txt}"
    )


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/lista] recibido")
    if update.message is None or not _authorized(update):
        return

    redis_client = _get_redis(context)
    if not _redis_alive(redis_client):
        await update.message.reply_text(
            "⚠️ No puedo acceder al almacenamiento (Redis). Intentá de nuevo en unos minutos."
        )
        return

    items = _load_items(redis_client)
    if not items:
        await update.message.reply_text("No hay bookings monitoreados.")
        return

    lines = [_format_item_line(i, item) for i, item in enumerate(items, start=1)]
    await update.message.reply_text("📋 Monitoreando:\n" + "\n".join(lines))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"[BOT][/stop] recibido args={context.args}")
    if update.message is None or not _authorized(update):
        return

    redis_client = _get_redis(context)
    if not _redis_alive(redis_client):
        await update.message.reply_text(
            "⚠️ No puedo acceder al almacenamiento (Redis). Intentá de nuevo en unos minutos."
        )
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Uso: /stop <id>\nVé los ids con /lista.")
        return

    item_id = args[0]
    # Buscar el item para poder confirmar terminal/booking en la respuesta.
    raw = None
    try:
        raw = redis_client.client.hget(ITEMS_KEY, item_id)
    except Exception as exc:
        logger.error(f"[BOT][/stop] error leyendo {item_id}: {exc}")

    if raw is None:
        await update.message.reply_text(f"No encontré ningún item con id {item_id}.")
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"terminal": "?", "booking": item_id}

    try:
        redis_client.client.hdel(ITEMS_KEY, item_id)
    except Exception as exc:
        logger.error(f"[BOT][/stop] error eliminando {item_id}: {exc}")
        await update.message.reply_text("⚠️ No pude eliminar el item. Reintentá más tarde.")
        return

    logger.info(f"[BOT][/stop] eliminado {item_id}")
    await update.message.reply_text(
        f"🛑 Detenido: {data.get('terminal', '?')} — {data.get('booking', item_id)}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[BOT][/status] recibido")
    if update.message is None or not _authorized(update):
        return

    redis_client = _get_redis(context)
    if not _redis_alive(redis_client):
        await update.message.reply_text(
            "⚠️ No puedo acceder al almacenamiento (Redis). Intentá de nuevo en unos minutos."
        )
        return

    items = _load_items(redis_client)

    last_run = "nunca"
    last_error = ""
    try:
        last_run = redis_client.client.get(WORKER_LAST_RUN_KEY) or "nunca"
        last_error = redis_client.client.get(WORKER_LAST_ERROR_KEY) or ""
    except Exception as exc:
        logger.error(f"[BOT][/status] error leyendo estado del worker: {exc}")

    lines = [
        "📊 Status",
        f"• Items activos: {len(items)}",
        f"• Última corrida del worker: {last_run}",
    ]
    lines.append(
        f"• Errores recientes: {last_error}" if last_error else "• Errores recientes: ninguno"
    )
    await update.message.reply_text("\n".join(lines))


# ---- Setup --------------------------------------------------------------


def setup_bot(redis_client: RedisClient) -> Application:
    """Construye la `Application` de python-telegram-bot lista para correr.

    Lee el token de TELEGRAM_BOT_TOKEN, registra todos los handlers y deja el
    `redis_client` accesible para los comandos vía `bot_data`.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("[BOT][INIT] falta TELEGRAM_BOT_TOKEN en el entorno")

    app = Application.builder().token(token).build()
    app.bot_data["redis_client"] = redis_client

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("agregar", cmd_agregar))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info(
        "[BOT][INIT] handlers registrados: /start /agregar /lista /stop /status"
    )
    return app


async def send_alert(
    bot: Bot,
    chat_id: str,
    terminal: str,
    booking: str,
    slots: list[dict],
) -> None:
    """Envía una alerta con todos los turnos disponibles de un booking.

    `slots` es una lista de dicts con shape:
        {"contenedor": str, "fecha": "YYYY-MM-DD", "cantidad": int, "franja": str}
    """
    if not slots:
        logger.warning(
            f"[BOT][ALERT] send_alert llamado sin slots para {terminal}:{booking}"
        )
        return

    portal_url = PORTAL_URLS.get(terminal, "")
    separator = "━━━━━━━━━━━━━━━"

    slot_lines = [
        f"📅 {_format_fecha(slot.get('fecha', ''))} — "
        f"{slot.get('cantidad', 0)} turnos"
        for slot in slots
    ]

    message = "\n".join(
        [
            f"🟢 TURNOS DISPONIBLES — {terminal}",
            f"📦 Booking: {booking}",
            separator,
            *slot_lines,
            separator,
            f"Coordiná ahora → {portal_url}",
        ]
    )

    try:
        await bot.send_message(chat_id=chat_id, text=message)
        logger.info(
            f"[BOT][ALERT] enviada para {terminal}:{booking} "
            f"({len(slots)} slot(s))"
        )
    except Exception as exc:
        logger.error(
            f"[BOT][ALERT] error enviando alerta {terminal}:{booking}: {exc}"
        )
