from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from loguru import logger


class BaseScraper(ABC):
    """Clase base abstracta para los scrapers de cada terminal portuaria.

    Subclases concretas deben implementar `login` y `check_availability`.
    El nombre del terminal (`terminal_name`) se usa como prefijo en los logs
    y como identificador en Redis.
    """

    terminal_name: str = "BASE"

    def __init__(self, playwright_page: Any, config: dict) -> None:
        self.page = playwright_page
        self.config = config

    @abstractmethod
    async def login(self) -> bool:
        """Autentica en el portal. Retorna True si tuvo éxito, False si falló."""
        ...

    @abstractmethod
    async def check_availability(self, identifier: str) -> list[dict]:
        """Consulta disponibilidad de turnos para un booking/contenedor.

        Retorna una lista de dicts con shape:
            {"fecha": str, "franja": str, "disponible": bool}
        Si falla, retorna lista vacía (la excepción se loguea internamente).
        """
        ...

    async def _screenshot(self, name: str) -> str | None:
        """Guarda una captura del estado actual de la página para debugging.

        Los archivos van a `screenshots/` con formato:
            {terminal}_{name}_{YYYYMMDD-HHMMSS}.png
        Retorna la ruta absoluta del archivo creado o None si falla.
        """
        try:
            os.makedirs("screenshots", exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            filename = f"screenshots/{self.terminal_name}_{name}_{timestamp}.png"
            await self.page.screenshot(path=filename, full_page=True)
            logger.debug(f"[{self.terminal_name}][SCREENSHOT] guardado en {filename}")
            return filename
        except Exception as exc:
            logger.error(
                f"[{self.terminal_name}][SCREENSHOT] no se pudo guardar '{name}': {exc}"
            )
            return None

    async def safe_login(self) -> bool:
        """Wrapper de `login` que captura excepciones y guarda screenshot ante error."""
        try:
            logger.info(f"[{self.terminal_name}][LOGIN] iniciando")
            ok = await self.login()
            if ok:
                logger.info(f"[{self.terminal_name}][LOGIN] OK")
            else:
                logger.warning(f"[{self.terminal_name}][LOGIN] retornó False")
                await self._screenshot("login_failed")
            return ok
        except Exception as exc:
            logger.exception(
                f"[{self.terminal_name}][LOGIN] excepción capturada: {exc}"
            )
            await self._screenshot("login_exception")
            return False

    async def safe_check_availability(self, identifier: str) -> list[dict]:
        """Wrapper de `check_availability` que captura excepciones y retorna []."""
        try:
            logger.info(
                f"[{self.terminal_name}][CHECK] consultando disponibilidad para {identifier}"
            )
            slots = await self.check_availability(identifier)
            logger.info(
                f"[{self.terminal_name}][CHECK] {len(slots)} slot(s) obtenidos para {identifier}"
            )
            return slots
        except Exception as exc:
            logger.exception(
                f"[{self.terminal_name}][CHECK] excepción para {identifier}: {exc}"
            )
            await self._screenshot(f"check_exception_{identifier}")
            return []
