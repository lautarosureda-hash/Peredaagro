from __future__ import annotations

import os
import re
from typing import Any

from loguru import logger
from playwright.async_api import Frame, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scrapers.base import BaseScraper


COORDINATION_URL = (
    "https://apps.apmterminals.com.ar/puertodigital/"
    "CoordinationManagement/coordinationManagement"
)
DEFAULT_TIMEOUT_MS = 15000

# Patrón ISO 6346 para IDs de contenedor: 4 letras + 7 dígitos (ej. TCKU1234567).
CONTAINER_PATTERN = re.compile(r"\b[A-Z]{4}\d{7}\b")
# Fechas comunes en portales argentinos: DD/MM/YYYY o YYYY-MM-DD.
DATE_PATTERN = re.compile(r"\b(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\b")
# Franja horaria: "08:00-12:00", "08:00 - 12:00", "08:00 a 12:00".
TIME_RANGE_PATTERN = re.compile(r"\b(\d{2}:\d{2})\s*(?:-|a|A)\s*(\d{2}:\d{2})\b")


class T4Scraper(BaseScraper):
    """Scraper para el portal T4 / APM Terminals (https://apps.apmterminals.com.ar).

    NOTA sobre selectores: el portal no está disponible para inspección desde
    este entorno, así que los selectores son inferencias basadas en patrones
    comunes de portales Angular/React en español. Cada selector está comentado
    con el elemento al que apunta y casi todos tienen fallback en cascada.
    La primera corrida real con `tests/test_t4_manual.py` va a revelar qué
    necesita ajustarse — buscar los logs "[T4][SCRAPE] no aparece ..." para
    saber qué selector falló.
    """

    terminal_name: str = "T4"

    def __init__(self, playwright_page: Any, config: dict) -> None:
        super().__init__(playwright_page, config)
        self.base_url: str = config.get("url") or os.environ.get("T4_URL", "")
        self.username: str = config.get("user") or os.environ.get("T4_USER", "")
        self.password: str = config.get("pass") or os.environ.get("T4_PASS", "")
        logger.debug(f"[{self.terminal_name}][INIT] base_url={self.base_url}")

    # ---- Helpers internos -------------------------------------------------

    async def _wait_networkidle(self) -> None:
        """Espera a que la red quede idle. Si no llega en 15s, sigue igual con un warning."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.warning(
                f"[{self.terminal_name}] networkidle no alcanzado en 15s — sigo igual"
            )

    async def _active_context(self) -> Page | Frame:
        """Devuelve el contexto donde vive la UI del portal (page o iframe).

        Heurística: si hay un iframe cuya URL contiene 'puertodigital' o
        'CoordinationManagement', usamos ese frame. Si no, la page principal.
        """
        main = self.page.main_frame
        for f in self.page.frames:
            if f is main:
                continue
            url = f.url or ""
            if "puertodigital" in url or "CoordinationManagement" in url:
                logger.info(f"[{self.terminal_name}] usando iframe interno {url}")
                return f
        return self.page

    async def _is_login_page(self) -> bool:
        """True si en la página actual hay un input type=password visible."""
        try:
            return await self.page.locator("input[type='password']").count() > 0
        except Exception:
            return False

    async def _safe_back_to_base(self) -> None:
        """Navega siempre al base_url. Defensa contra quedar en pantalla de confirmación."""
        if not self.base_url:
            return
        try:
            await self.page.goto(
                self.base_url, timeout=DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] vuelto a {self.base_url} "
                f"(no se confirmó ningún turno)"
            )
        except Exception as exc:
            logger.error(f"[{self.terminal_name}][SCRAPE] error volviendo al base_url: {exc}")

    # ---- Login -----------------------------------------------------------

    async def login(self) -> bool:
        """Autentica en el portal. Nunca propaga excepciones — siempre retorna bool."""
        if not self.base_url:
            logger.error(f"[{self.terminal_name}][LOGIN] T4_URL no configurado")
            return False
        if not self.username or not self.password:
            logger.error(f"[{self.terminal_name}][LOGIN] faltan credenciales (T4_USER/T4_PASS)")
            return False

        try:
            logger.info(f"[{self.terminal_name}][LOGIN] navegando a {self.base_url}")
            await self.page.goto(
                self.base_url, timeout=DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
            await self._wait_networkidle()

            # Input de usuario. Atributos típicos: name=username, id=user, etc.
            # Fallback a get_by_label si los attrs cambian con minificación Angular.
            user_input = self.page.locator(
                "input[name='username'], input[name='user'], input[id*='user' i]"
            ).first
            if await user_input.count() == 0:
                user_input = self.page.get_by_label(re.compile("usuario", re.I)).first
            await user_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await user_input.fill(self.username)
            logger.debug(f"[{self.terminal_name}][LOGIN] usuario completado")

            # Input password — único type=password en pantallas de login.
            pass_input = self.page.locator("input[type='password']").first
            await pass_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await pass_input.fill(self.password)
            logger.debug(f"[{self.terminal_name}][LOGIN] contraseña completada")

            # Botón submit. Texto típico en español: "Ingresar", "Iniciar sesión", "Login".
            submit = self.page.get_by_role(
                "button", name=re.compile(r"ingresar|iniciar|login|entrar", re.I)
            ).first
            if await submit.count() == 0:
                submit = self.page.locator("button[type='submit'], input[type='submit']").first
            await submit.click()
            logger.info(f"[{self.terminal_name}][LOGIN] formulario enviado")

            await self._wait_networkidle()

            # Confirmación de login: si seguimos viendo input password, falló.
            if await self._is_login_page():
                logger.error(f"[{self.terminal_name}][LOGIN] sigue en login tras submit")
                await self._screenshot("login_still_on_login_page")
                return False

            logger.info(f"[{self.terminal_name}][LOGIN] OK — dashboard cargado")
            return True

        except PlaywrightTimeoutError as exc:
            logger.error(f"[{self.terminal_name}][LOGIN] timeout esperando elemento: {exc}")
            await self._screenshot("login_timeout")
            return False
        except Exception as exc:
            logger.exception(f"[{self.terminal_name}][LOGIN] excepción inesperada: {exc}")
            await self._screenshot("login_exception")
            return False

    # ---- Navegación a coordinationManagement -----------------------------

    async def _go_to_coordination(self, allow_relogin: bool = True) -> bool:
        """Navega a coordinationManagement. Si redirige al login, re-loguea UNA vez."""
        logger.info(f"[{self.terminal_name}][SCRAPE] navegando a coordinationManagement")
        try:
            await self.page.goto(
                COORDINATION_URL, timeout=DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
            await self._wait_networkidle()
        except PlaywrightTimeoutError as exc:
            logger.error(f"[{self.terminal_name}][SCRAPE] timeout navegando a coordination: {exc}")
            await self._screenshot("coordination_goto_timeout")
            return False

        if await self._is_login_page():
            if not allow_relogin:
                logger.error(
                    f"[{self.terminal_name}][SCRAPE] redirigió a login otra vez tras re-login"
                )
                return False
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] redirigido a login — re-intentando autenticación"
            )
            if not await self.login():
                return False
            return await self._go_to_coordination(allow_relogin=False)

        return True

    # ---- check_availability ----------------------------------------------

    async def check_availability(self, booking: str) -> list[dict]:
        """Consulta los turnos disponibles para un booking de exportación.

        Retorna list[dict] con shape:
            {"contenedor": str, "fecha": str, "franja": str}
        Si no hay slots o algo falla, retorna [].
        Nunca propaga excepciones — siempre vuelve a base_url antes de salir.
        """
        if not booking:
            logger.error(f"[{self.terminal_name}][SCRAPE] booking vacío")
            return []

        results: list[dict] = []
        try:
            if not await self._go_to_coordination():
                return []

            ctx = await self._active_context()

            # Paso 3: seleccionar "Exportación". Puede ser tab, botón o item de menú.
            if not await self._click_exportacion(ctx):
                return []

            # Paso 4: ingresar booking y disparar búsqueda.
            if not await self._submit_booking(ctx, booking):
                return []

            # Paso 5: detectar contenedores de los resultados.
            containers = await self._find_containers(ctx, booking)
            if not containers:
                logger.warning(
                    f"[{self.terminal_name}][SCRAPE] sin contenedores para booking {booking}"
                )
                return []

            logger.info(
                f"[{self.terminal_name}][SCRAPE] {len(containers)} contenedor(es): {containers}"
            )

            # Paso 6+7: para cada contenedor, abrir pantalla de turnos y extraer.
            for idx, container in enumerate(containers):
                slots = await self._scrape_container_slots(ctx, container)
                results.extend(slots)

                # Si quedan contenedores por procesar, volver a la grilla de resultados.
                if idx < len(containers) - 1:
                    if not await self._return_to_results(ctx, booking):
                        logger.warning(
                            f"[{self.terminal_name}][SCRAPE] no pude volver a resultados; "
                            f"detengo iteración"
                        )
                        break
                    # Refrescar el contexto por si el frame se rehidrató.
                    ctx = await self._active_context()

            return results

        except Exception as exc:
            logger.exception(
                f"[{self.terminal_name}][SCRAPE] excepción inesperada para booking {booking}: {exc}"
            )
            await self._screenshot(f"scrape_exception_{booking}")
            return results
        finally:
            # SEGURIDAD: siempre volver al base_url. Aunque el flujo no llegue
            # a una pantalla de confirmación, este goto garantiza que el operador
            # no quede en una ruta de reserva.
            await self._safe_back_to_base()

    # ---- Sub-pasos -------------------------------------------------------

    async def _click_exportacion(self, ctx: Page | Frame) -> bool:
        """Selecciona la opción 'Exportación' en la pantalla de coordination."""
        # Tab "Exportación" — primera prioridad si la UI usa tabs.
        loc = ctx.get_by_role("tab", name=re.compile("exportaci", re.I))
        if await loc.count() == 0:
            # Botón "Exportación"
            loc = ctx.get_by_role("button", name=re.compile("exportaci", re.I))
        if await loc.count() == 0:
            # Item de menú o cualquier elemento clickeable con ese texto
            loc = ctx.get_by_text(re.compile(r"exportaci", re.I)).first

        try:
            await loc.first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await loc.first.click()
            logger.info(f"[{self.terminal_name}][SCRAPE] 'Exportación' seleccionado")
            await self._wait_networkidle()
            return True
        except PlaywrightTimeoutError:
            logger.error(f"[{self.terminal_name}][SCRAPE] no aparece selector 'Exportación'")
            await self._screenshot("exportacion_not_found")
            return False

    async def _submit_booking(self, ctx: Page | Frame, booking: str) -> bool:
        """Completa el campo de booking y dispara la búsqueda."""
        # Input booking — preferir label visible. Fallback a name/id/placeholder.
        loc = ctx.get_by_label(re.compile("booking", re.I)).first
        if await loc.count() == 0:
            loc = ctx.locator(
                "input[name*='booking' i], input[id*='booking' i], "
                "input[placeholder*='booking' i]"
            ).first

        try:
            await loc.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await loc.fill(booking)
            logger.debug(f"[{self.terminal_name}][SCRAPE] booking '{booking}' tipeado")
        except PlaywrightTimeoutError:
            logger.error(f"[{self.terminal_name}][SCRAPE] no aparece input de booking")
            await self._screenshot("booking_input_not_found")
            return False

        # Disparar búsqueda: primero intentamos botón explícito, sino Enter.
        search_btn = ctx.get_by_role("button", name=re.compile(r"buscar|consultar", re.I))
        if await search_btn.count() > 0:
            await search_btn.first.click()
            logger.debug(f"[{self.terminal_name}][SCRAPE] botón Buscar clickeado")
        else:
            await loc.press("Enter")
            logger.debug(f"[{self.terminal_name}][SCRAPE] Enter en input de booking")

        await self._wait_networkidle()
        return True

    async def _find_containers(self, ctx: Page | Frame, booking: str) -> list[str]:
        """Extrae IDs de contenedor del DOM de resultados.

        Espera a que aparezca alguna grilla y luego matchea el patrón ISO 6346
        contra el texto visible. Si no hay grilla en 15s, retorna [].
        """
        try:
            await ctx.wait_for_selector(
                "table tbody tr, [role='row'], .container-row, [class*='resultado' i]",
                timeout=DEFAULT_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            logger.error(f"[{self.terminal_name}][SCRAPE] no aparece grilla de resultados")
            await self._screenshot(f"no_results_grid_{booking}")
            return []

        try:
            body_text = await ctx.locator("body").inner_text()
        except Exception as exc:
            logger.error(f"[{self.terminal_name}][SCRAPE] no pude leer texto del body: {exc}")
            return []

        return sorted(set(CONTAINER_PATTERN.findall(body_text)))

    async def _scrape_container_slots(self, ctx: Page | Frame, container: str) -> list[dict]:
        """Hace click en la fila del contenedor y extrae sus turnos disponibles."""
        try:
            # Click en cualquier elemento cuyo texto contenga el ID del contenedor.
            row = ctx.get_by_text(container, exact=False).first
            await row.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await row.click()
            logger.info(f"[{self.terminal_name}][SCRAPE] click en contenedor {container}")
            await self._wait_networkidle()

            # Esperar pantalla de turnos. Heurística amplia: tabla/grid/clases
            # con 'slot' o 'turno' o 'franja' en el nombre.
            await ctx.wait_for_selector(
                "table tbody tr, [role='grid'] [role='row'], "
                "[class*='slot' i], [class*='turno' i], [class*='franja' i]",
                timeout=DEFAULT_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as exc:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] timeout en pantalla de turnos "
                f"para {container}: {exc}"
            )
            await self._screenshot(f"slots_timeout_{container}")
            return []
        except Exception as exc:
            logger.exception(
                f"[{self.terminal_name}][SCRAPE] error abriendo turnos de {container}: {exc}"
            )
            await self._screenshot(f"slots_error_{container}")
            return []

        return await self._extract_slot_rows(ctx, container)

    async def _extract_slot_rows(self, ctx: Page | Frame, container: str) -> list[dict]:
        """Itera las filas/cards visibles y extrae (fecha, franja) si están disponibles."""
        slots: list[dict] = []
        try:
            rows = ctx.locator(
                "table tbody tr, [role='row'], [class*='slot' i], [class*='turno' i]"
            )
            count = await rows.count()
            logger.debug(
                f"[{self.terminal_name}][SCRAPE] {count} fila(s) candidatas para {container}"
            )

            for i in range(count):
                try:
                    text = (await rows.nth(i).inner_text()).strip()
                except Exception:
                    continue
                if not text:
                    continue

                date_match = DATE_PATTERN.search(text)
                time_match = TIME_RANGE_PATTERN.search(text)
                if not (date_match and time_match):
                    continue

                # Filtrar filas que muestran indicadores de NO disponibilidad.
                lower = text.lower()
                if any(k in lower for k in ("ocupado", "completo", "no disponible", "reservado")):
                    continue

                slots.append(
                    {
                        "contenedor": container,
                        "fecha": date_match.group(1),
                        "franja": f"{time_match.group(1)}-{time_match.group(2)}",
                    }
                )
        except Exception as exc:
            logger.exception(
                f"[{self.terminal_name}][SCRAPE] error extrayendo slots de {container}: {exc}"
            )

        logger.info(f"[{self.terminal_name}][SCRAPE] {len(slots)} slot(s) extraído(s) para {container}")
        return slots

    async def _return_to_results(self, ctx: Page | Frame, booking: str) -> bool:
        """Vuelve a la grilla de resultados para procesar el próximo contenedor.

        Estrategia: re-navegar a coordinationManagement y rehacer la búsqueda.
        Es más robusto que `page.go_back()` en SPAs donde el history puede no
        reflejar los pasos internos de la UI.
        """
        if not await self._go_to_coordination():
            return False
        ctx2 = await self._active_context()
        if not await self._click_exportacion(ctx2):
            return False
        if not await self._submit_booking(ctx2, booking):
            return False
        return True
