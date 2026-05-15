from __future__ import annotations

from typing import Any

from loguru import logger

from scrapers.base import BaseScraper


class TRPScraper(BaseScraper):
    """Scraper para el portal TRP (Terminales Río de la Plata)."""

    terminal_name: str = "TRP"

    def __init__(self, playwright_page: Any, config: dict) -> None:
        super().__init__(playwright_page, config)
        logger.debug(f"[{self.terminal_name}][INIT] scraper inicializado")

    async def login(self) -> bool:
        raise NotImplementedError("Pendiente de implementación")

    async def check_availability(self, identifier: str) -> list[dict]:
        raise NotImplementedError("Pendiente de implementación")
