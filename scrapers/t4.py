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

# Columnas de la grilla de resultados (confirmadas visualmente sobre el portal):
#   0:Contenedor 1:L.Deuda 2:Categoría 3:Tipo 4:Línea 5:Estado 6:EIR
#   7:Documento  8:POL     9:POD       10:Nave 11:Viaje 12:Cutoff 13:Turno
#   14:Precintos 15:Servicios 16:Coordinar (botón con ícono calendario)
COL_CONTENEDOR = 0
COL_DOCUMENTO = 7
COL_CUTOFF = 12
COL_TURNO = 13
MIN_CELLS_PER_ROW = COL_TURNO + 1  # 14 — filtra placeholder rows / spinners

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

            # Paso 5: leer filas de la tabla (contenedor + metadata).
            rows = await self._find_containers(ctx, booking)
            if not rows:
                logger.warning(
                    f"[{self.terminal_name}][SCRAPE] sin filas para booking {booking}"
                )
                return []

            logger.info(
                f"[{self.terminal_name}][SCRAPE] {len(rows)} fila(s): "
                f"{[r['contenedor'] for r in rows]}"
            )

            # Paso 6+7: por cada fila, clickear su botón Coordinar y scrapear turnos.
            for idx, entry in enumerate(rows):
                slots = await self._get_slots_for_container(entry["row_index"])
                results.extend(slots)

                # Antes del próximo row_index: volver a la grilla de resultados.
                # El click en Coordinar puede haber abierto un modal o navegado;
                # re-hacer la búsqueda garantiza un estado consistente.
                if idx < len(rows) - 1:
                    if not await self._return_to_results(ctx, booking):
                        logger.warning(
                            f"[{self.terminal_name}][SCRAPE] no pude volver a resultados; "
                            f"detengo iteración"
                        )
                        break
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

        El select usa Knockout.js (data-bind="options:..."): la API
        `select_option` de Playwright no dispara el evento 'change' que
        Knockout escucha para reconfigurar el form. Por eso seteamos el
        value vía JS y emitimos manualmente un evento 'change' con bubbles,
        lo que sí gatilla el handler de Knockout y hace que aparezca
        #divSearchKeyParameter con el input de Booking.
        """
        # 1. Esperar a que el <select> esté en el DOM.
        try:
            await ctx.wait_for_selector("#cbSearchCategory", timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] #cbSearchCategory no aparece en el DOM"
            )
            await self._screenshot("cbSearchCategory_not_found")
            return False

        # 2. Setear el value de EXPORTACIÓN y dispatch 'change' para Knockout.
        try:
            await ctx.evaluate(
                """() => {
                    const sel = document.querySelector('#cbSearchCategory');
                    const opt = Array.from(sel.options).find(o => o.text.trim() === 'EXPORTACIÓN');
                    if (opt) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }"""
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] EXPORTACIÓN seleccionada via JS + change event"
            )
        except Exception as exc:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] error ejecutando JS dispatchEvent en "
                f"#cbSearchCategory: {exc}"
            )
            await self._screenshot("cbSearchCategory_dispatchEvent_failed")
            return False

        # 3. Margen fijo para que Knockout procese el 'change' y re-renderice.
        #    Excepción al "sin sleeps fijos": chequear style.display no captura
        #    visibilidad gobernada por clases CSS, y el siguiente paso usa varios
        #    selectores con su propio wait — el sleep solo evita que el primer
        #    intento corra contra un DOM todavía sin actualizar.
        await ctx.wait_for_timeout(1000)

        # 4. Probar selectores candidatos del input Booking en orden de
        #    especificidad. El primero que aparezca dentro de 10s gana.
        candidate_selectors = [
            "#divSearchKeyParameter input",
            "input[placeholder*='booking' i]",
            "input[placeholder*='BK' i]",
            "input[name*='booking' i]",
        ]
        for selector in candidate_selectors:
            try:
                await ctx.wait_for_selector(selector, timeout=10000)
                logger.info(
                    f"[{self.terminal_name}][SCRAPE] campo Booking ubicado vía '{selector}'"
                )
                return True
            except PlaywrightTimeoutError:
                continue

        # 5. Ningún selector apareció. Diagnosticar leyendo el value del select.
        try:
            val = await ctx.evaluate(
                "document.querySelector('#cbSearchCategory')?.value"
            )
        except Exception as exc:
            val = f"<error leyendo value: {exc}>"
        logger.error(
            f"[{self.terminal_name}][SCRAPE] valor actual de #cbSearchCategory: {val}"
        )
        await self._screenshot("divSearchKeyParameter_all_selectors_failed")
        return False

    async def _submit_booking(self, ctx: Page | Frame, booking: str) -> bool:
        """Completa el campo Booking y clickea el botón BUSCAR.

        Asume que `_select_exportacion` ya hizo visible #divSearchKeyParameter.
        """
        try:
            await ctx.fill(
                "#divSearchKeyParameter input", booking, timeout=DEFAULT_TIMEOUT_MS
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] booking {booking} ingresado en "
                f"#divSearchKeyParameter input"
            )
        except PlaywrightTimeoutError:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] no se pudo completar #divSearchKeyParameter input"
            )
            await self._screenshot("booking_fill_failed")
            return False

        try:
            logger.info(f"[{self.terminal_name}][SCRAPE] clicking BUSCAR")
            await ctx.click("button:has-text('BUSCAR')", timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.error(f"[{self.terminal_name}][SCRAPE] no se pudo clickear BUSCAR")
            await self._screenshot("buscar_click_failed")
            return False

        await self._wait_networkidle()
        return True

    async def _find_containers(self, ctx: Page | Frame, booking: str) -> list[dict]:
        """Extrae info por fila de la tabla de resultados.

        Tras BUSCAR, Knockout re-renderiza la tabla async. Esperamos a que
        aparezca al menos una fila REAL (sin el placeholder 'No hay registros')
        antes de leer. Si timeout-ea, el booking no tiene resultados.

        Retorna list[dict] con:
            {"row_index": int, "contenedor": str, "documento": str,
             "cutoff": str, "turno": str}
        El `row_index` se consume después en `_get_slots_for_container`.
        """
        try:
            await ctx.wait_for_selector(
                "table tbody tr:not(:has-text('No hay registros'))",
                timeout=DEFAULT_TIMEOUT_MS,
            )
            logger.info(f"[{self.terminal_name}][SCRAPE] tabla cargó con resultados")
        except PlaywrightTimeoutError:
            logger.info(
                f"[{self.terminal_name}][SCRAPE] tabla vacía: sin filas reales para "
                f"booking {booking} (timeout esperando fila != 'No hay registros')"
            )
            return []

        rows_loc = ctx.locator("table tbody tr")
        try:
            row_count = await rows_loc.count()
        except Exception as exc:
            logger.error(f"[{self.terminal_name}][SCRAPE] no pude contar filas: {exc}")
            return []

        items: list[dict] = []
        for i in range(row_count):
            try:
                cells = rows_loc.nth(i).locator("td")
                cell_count = await cells.count()
                # Filas con pocas celdas son placeholders/spinners/empty-state.
                if cell_count < MIN_CELLS_PER_ROW:
                    continue
                contenedor = (await cells.nth(COL_CONTENEDOR).inner_text()).strip()
                if not contenedor:
                    continue
                items.append(
                    {
                        "row_index": i,
                        "contenedor": contenedor,
                        "documento": (await cells.nth(COL_DOCUMENTO).inner_text()).strip(),
                        "cutoff": (await cells.nth(COL_CUTOFF).inner_text()).strip(),
                        "turno": (await cells.nth(COL_TURNO).inner_text()).strip(),
                    }
                )
            except Exception as exc:
                logger.warning(
                    f"[{self.terminal_name}][SCRAPE] error leyendo fila {i}: {exc}"
                )
                continue

        logger.info(
            f"[{self.terminal_name}][SCRAPE] {len(items)} fila(s) extraída(s) de la tabla"
        )
        return items

    async def _get_slots_for_container(self, row_index: int) -> list[dict]:
        """Clickea el ícono Coordinar (última columna) de la fila row_index
        y scrapea los turnos disponibles que se abren.

        SOLO lee — nunca clickea botones de confirmar/reservar. El caller
        debe llamar `_return_to_results` antes del próximo row_index.
        """
        ctx = await self._active_context()

        # nth-child es 1-indexed en CSS; row_index es 0-indexed.
        row_selector = f"table tbody tr:nth-child({row_index + 1})"

        # Leer el contenedor antes del click (para incluirlo en cada slot).
        container = ""
        try:
            container = (
                await ctx.locator(f"{row_selector} td:nth-child({COL_CONTENEDOR + 1})")
                .first.inner_text()
            ).strip()
        except Exception:
            pass

        # Click en el botón/link de la columna Coordinar (última columna).
        coordinar_selector = f"{row_selector} td:last-child button, {row_selector} td:last-child a"
        try:
            logger.info(
                f"[{self.terminal_name}][SCRAPE] clickeando Coordinar de fila {row_index} "
                f"(contenedor {container})"
            )
            await ctx.click(coordinar_selector, timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] no se pudo clickear Coordinar de fila {row_index}"
            )
            await self._screenshot(f"coordinar_click_failed_row_{row_index}")
            return []

        await self._wait_networkidle()

        # Esperar la pantalla/modal de turnos.
        try:
            await ctx.wait_for_selector(
                "[role='dialog'], [class*='modal' i], [class*='slot' i], "
                "[class*='turno' i], [class*='franja' i], "
                "table tbody tr:not(:has-text('No hay'))",
                timeout=DEFAULT_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] no aparece pantalla de turnos para "
                f"fila {row_index} (contenedor {container})"
            )
            await self._screenshot(f"slots_screen_not_found_row_{row_index}")
            return []

        # SOLO LEER. _extract_slot_rows nunca clickea — solo extrae texto.
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
