from __future__ import annotations

import json
import uuid

import redis
from loguru import logger


class RedisClient:
    """Wrapper sobre redis-py para el estado del monitor.

    Estructura de keys:
        - slots:{terminal}:{identifier}  -> JSON (list[dict]) último snapshot de slots
        - items:active                   -> Hash {item_id: JSON({terminal, identifier})}
    """

    STATE_KEY = "slots:{terminal}:{identifier}"
    ITEMS_KEY = "items:active"

    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        # Timeouts de socket: sin esto, una conexión colgada (red caída hacia
        # el add-on de Railway) bloquea para siempre la llamada síncrona y, como
        # estas se ejecutan sobre el event loop, congelaría bot + scheduler.
        self.client: redis.Redis = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        logger.info(f"[REDIS][INIT] conectado a {redis_url}")

    def get_state(self, terminal: str, identifier: str) -> list[dict] | None:
        """Retorna el último snapshot de slots para (terminal, identifier), o None si no existe."""
        key = self.STATE_KEY.format(terminal=terminal, identifier=identifier)
        try:
            raw = self.client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.error(f"[REDIS][GET_STATE] error leyendo {key}: {exc}")
            return None

    def set_state(self, terminal: str, identifier: str, slots: list[dict]) -> None:
        """Guarda el snapshot actual de slots para (terminal, identifier)."""
        key = self.STATE_KEY.format(terminal=terminal, identifier=identifier)
        try:
            self.client.set(key, json.dumps(slots))
            logger.debug(f"[REDIS][SET_STATE] {key} = {len(slots)} slot(s)")
        except Exception as exc:
            logger.error(f"[REDIS][SET_STATE] error escribiendo {key}: {exc}")

    def get_monitored_items(self) -> list[dict]:
        """Retorna la lista de items monitoreados activos.

        Cada item tiene shape: {"id": str, "terminal": str, "identifier": str}
        """
        try:
            raw_items = self.client.hgetall(self.ITEMS_KEY)
            items: list[dict] = []
            for item_id, payload in raw_items.items():
                try:
                    data = json.loads(payload)
                    items.append(
                        {
                            "id": item_id,
                            "terminal": data["terminal"],
                            "identifier": data["identifier"],
                        }
                    )
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning(
                        f"[REDIS][GET_ITEMS] item corrupto {item_id}: {exc}"
                    )
            return items
        except Exception as exc:
            logger.error(f"[REDIS][GET_ITEMS] error: {exc}")
            return []

    def add_item(self, terminal: str, identifier: str) -> None:
        """Agrega un item a la lista de monitoreo. Genera un UUID como id interno."""
        item_id = uuid.uuid4().hex[:12]
        payload = json.dumps({"terminal": terminal, "identifier": identifier})
        try:
            self.client.hset(self.ITEMS_KEY, item_id, payload)
            logger.info(
                f"[REDIS][ADD_ITEM] {item_id} -> {terminal}:{identifier}"
            )
        except Exception as exc:
            logger.error(f"[REDIS][ADD_ITEM] error agregando {terminal}:{identifier}: {exc}")

    def remove_item(self, item_id: str) -> None:
        """Elimina un item monitoreado por su id interno."""
        try:
            removed = self.client.hdel(self.ITEMS_KEY, item_id)
            if removed:
                logger.info(f"[REDIS][REMOVE_ITEM] {item_id} eliminado")
            else:
                logger.warning(f"[REDIS][REMOVE_ITEM] {item_id} no existía")
        except Exception as exc:
            logger.error(f"[REDIS][REMOVE_ITEM] error eliminando {item_id}: {exc}")
