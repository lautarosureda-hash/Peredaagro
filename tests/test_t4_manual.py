"""Test manual del scraper T4.

Uso (desde la raíz del proyecto `terminal-monitor/`):

    python tests/test_t4_manual.py

Lanza Chromium en modo visible (headless=False) para que se pueda observar el
flujo en vivo. Requiere variables en .env: T4_URL, T4_USER, T4_PASS y un
T4_TEST_BOOKING válido. Guarda screenshots/test_t4_final.png al terminar.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Permitir importar el package `scrapers` cuando se ejecuta el archivo
# directamente con `python tests/test_t4_manual.py`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from scrapers.t4 import T4Scraper  # noqa: E402


async def main() -> None:
    load_dotenv()

    booking = os.environ.get("T4_TEST_BOOKING", "").strip()
    if not booking:
        logger.error("[TEST][T4] falta T4_TEST_BOOKING en .env — abortando")
        return

    logger.info(f"[TEST][T4] booking de prueba: {booking}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        scraper = T4Scraper(page, config={})

        logger.info("[TEST][T4] === paso 1: login ===")
        login_ok = await scraper.login()
        print(f"login OK: {login_ok}")

        if not login_ok:
            logger.error("[TEST][T4] login falló — no continúo con check_availability")
        else:
            logger.info(f"[TEST][T4] === paso 2: check_availability({booking}) ===")
            slots = await scraper.check_availability(booking)
            print(f"slots ({len(slots)}):")
            for s in slots:
                print(f"  - {s}")

        os.makedirs("screenshots", exist_ok=True)
        try:
            await page.screenshot(path="screenshots/test_t4_final.png", full_page=True)
            print("screenshot final → screenshots/test_t4_final.png")
        except Exception as exc:
            logger.error(f"[TEST][T4] no pude guardar screenshot final: {exc}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
