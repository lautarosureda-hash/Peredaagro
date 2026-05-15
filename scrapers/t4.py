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
        # T4_USER guarda el CUIT/CUIL del operador (no un username clásico).
        self.cuit: str = config.get("user") or os.environ.get("T4_USER", "")
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
        if not self.cuit or not self.password:
            logger.error(
                f"[{self.terminal_name}][LOGIN] faltan credenciales "
                f"(T4_USER=CUIT/CUIL y T4_PASS)"
            )
            return False

        try:
            logger.info(f"[{self.terminal_name}][LOGIN] navegando a {self.base_url}")
            await self.page.goto(
                self.base_url, timeout=DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
            await self._wait_networkidle()

            # Campo CUIT/CUIL — el portal lo identifica por su placeholder "CUIT/CUIL".
            # Fallback a atributos comunes si el placeholder estuviese minificado.
            cuit_input = self.page.get_by_placeholder(re.compile(r"CUIT", re.I)).first
            if await cuit_input.count() == 0:
                cuit_input = self.page.locator(
                    "input[placeholder*='CUIT' i], "
                    "input[name*='cuit' i], "
                    "input[id*='cuit' i]"
                ).first
            await cuit_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await cuit_input.fill(self.cuit)
            logger.debug(f"[{self.terminal_name}][LOGIN] CUIT/CUIL completado")

            # Campo CONTRASEÑA — preferimos placeholder, fallback al único type=password.
            pass_input = self.page.get_by_placeholder(re.compile(r"contrase", re.I)).first
            if await pass_input.count() == 0:
                pass_input = self.page.locator("input[type='password']").first
            await pass_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await pass_input.fill(self.password)
            logger.debug(f"[{self.terminal_name}][LOGIN] contraseña completada")

            # Botón "Ingresar". Texto exacto según el portal.
            submit = self.page.get_by_role("button", name="Ingresar", exact=True).first
            if await submit.count() == 0:
                submit = self.page.get_by_text("Ingresar", exact=True).first
            if await submit.count() == 0:
                submit = self.page.locator(
                    "button[type='submit'], input[type='submit']"
                ).first
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

            # Paso 3: seleccionar EXPORTACIÓN en el dropdown Categoría.
            if not await self._select_exportacion(ctx):
                return []

            # Paso 4 + 5: ingresar booking y clickear BUSCAR.
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

    async def _select_exportacion(self, ctx: Page | Frame) -> bool:
        """Selecciona 'EXPORTACIÓN' en el <select id=cbSearchCategory>.

        El dropdown ES un <select> HTML nativo con id/name 'cbSearchCategory'
        (aunque visualmente parezca custom). Tras la selección, el form
        renderiza dinámicamente el div #divSearchKeyParameter con el input
        de Booking — ese div es lo que consume `_submit_booking`.
        """
        try:
            await ctx.wait_for_selector("#cbSearchCategory", timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] #cbSearchCategory no aparece en el DOM"
            )
            await self._screenshot("cbSearchCategory_not_found")
            return False

        try:
            await ctx.select_option("#cbSearchCategory", label="EXPORTACIÓN")
            logger.info(
                f"[{self.terminal_name}][SCRAPE] EXPORTACIÓN seleccionada en #cbSearchCategory"
            )
            await self._wait_networkidle()
            return True
        except Exception as exc:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] no se pudo seleccionar EXPORTACIÓN "
                f"en #cbSearchCategory: {exc}"
            )
            await self._screenshot("cbSearchCategory_select_failed")
            return False

    async def _submit_booking(self, ctx: Page | Frame, booking: str) -> bool:
        """Completa el campo 'Booking (*)' y clickea el botón BUSCAR."""
        # Campo Booking — vive dentro de #divSearchKeyParameter, un div que el
        # form renderiza dinámicamente recién al elegir EXPORTACIÓN en Categoría.
        # Fallback al label "Booking" por si el id cambiase.
        booking_input = ctx.locator("#divSearchKeyParameter input").first
        if await booking_input.count() == 0:
            booking_input = ctx.get_by_label(re.compile(r"booking", re.I)).first

        try:
            await booking_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            logger.info(
                f"[{self.terminal_name}][SCRAPE] campo Booking (*) visible — ingresando {booking}"
            )
            await booking_input.fill(booking)
        except PlaywrightTimeoutError:
            logger.error(f"[{self.terminal_name}][SCRAPE] no aparece input de Booking")
            await self._screenshot("booking_input_not_found")
            return False

        # Botón BUSCAR (texto exacto en mayúsculas, color naranja en el portal).
        search_btn = ctx.get_by_role(
            "button", name=re.compile(r"^\s*BUSCAR\s*$", re.I)
        ).first
        if await search_btn.count() == 0:
            search_btn = ctx.get_by_text(re.compile(r"^\s*BUSCAR\s*$", re.I)).first
        if await search_btn.count() == 0:
            search_btn = ctx.locator("button[type='submit'], input[type='submit']").first

        try:
            await search_btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            logger.info(f"[{self.terminal_name}][SCRAPE] clicking BUSCAR")
            await search_btn.click()
        except PlaywrightTimeoutError:
            logger.error(f"[{self.terminal_name}][SCRAPE] no aparece botón BUSCAR")
            await self._screenshot("buscar_button_not_found")
            return False

        await self._wait_networkidle()
        return True

    async def _find_containers(self, ctx: Page | Frame, booking: str) -> list[str]:
        """Extrae IDs de contenedor de la tabla de resultados.

        - Espera a que la tabla renderice (incluso si está vacía, va a haber tbody).
        - Detecta el mensaje 'No hay registros a mostrar' → retorna [].
        - En caso normal, busca IDs ISO 6346 en el texto visible.
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

        # Empty state explícito del portal.
        if re.search(r"no hay registros", body_text, re.I):
            logger.info(
                f"[{self.terminal_name}][SCRAPE] tabla vacía: 'No hay registros a mostrar' "
                f"para booking {booking}"
            )
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
        if not await self._select_exportacion(ctx2):
            return False
        if not await self._submit_booking(ctx2, booking):
            return False
        return True
