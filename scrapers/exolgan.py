from __future__ import annotations

from typing import Any

from loguru import logger

from scrapers.base import BaseScraper


class ExolganScraper(BaseScraper):
    """Scraper para el portal Exolgan (ITL)."""

    terminal_name: str = "EXOLGAN"

    def __init__(self, playwright_page: Any, config: dict) -> None:
        super().__init__(playwright_page, config)
        logger.debug(f"[{self.terminal_name}][INIT] scraper inicializado")

    async def login(self) -> bool:
        raise NotImplementedError("Pendiente de implementación")

    async def check_availability(self, identifier: str) -> list[dict]:
        raise NotImplementedError("Pendiente de implementación")
