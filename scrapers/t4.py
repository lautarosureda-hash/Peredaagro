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

# Columnas de la grilla de resultados (confirmadas sobre el HTML real — 20
# celdas por fila; la celda 0 es un checkbox vacío):
#   0:checkbox   1:Contenedor 2:L.Deuda  3:Categoría 4:Tipo   5:Línea
#   6:Estado     7:EIR        8:Documento 9:POL      10:POD   11:Nave
#   12:Viaje     13:Cutoff    14:Turno   15:Precintos 16:Servicios
#   17:Coordinar (botón calendario)  18:Anular Turno  19:(extra)
COL_CONTENEDOR = 1
COL_DOCUMENTO = 8
COL_CUTOFF = 13
COL_TURNO = 14
MIN_CELLS_PER_ROW = 18  # filtra placeholder rows / spinners


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

    async def check_availability(
        self, booking: str, desde_fecha: str | None = None
    ) -> list[dict]:
        """Consulta los turnos disponibles para un booking de exportación.

        Retorna list[dict] con shape:
            {"contenedor": str, "fecha": str, "cantidad": int, "franja": str}
        Si no hay slots o algo falla, retorna [].
        Nunca propaga excepciones — siempre vuelve a base_url antes de salir.

        `desde_fecha` (formato "YYYY-MM-DD") filtra slots: si se pasa, solo se
        incluyen los que tengan fecha >= desde_fecha.
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

            # Paso 6+7: probar los contenedores en orden hasta encontrar uno
            # con turnos disponibles. Un contenedor que ya tiene turno asignado
            # abre el calendario vacío, así que no alcanza con el primero.
            for row in rows:
                logger.info(
                    f"[{self.terminal_name}][SCRAPE] chequeando contenedor "
                    f"{row['contenedor']} (row_index={row['row_index']})"
                )
                slots = await self._get_slots_for_container(
                    row["row_index"], desde_fecha=desde_fecha
                )
                if slots:
                    logger.info(
                        f"[{self.terminal_name}][SCRAPE] slots encontrados en "
                        f"{row['contenedor']} — usando estos resultados"
                    )
                    return slots
                logger.info(
                    f"[{self.terminal_name}][SCRAPE] {row['contenedor']} sin "
                    f"slots — probando siguiente"
                )

            return []

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
        #    El match de la opción es tolerante: normaliza acentos y mayúsculas
        #    y busca por substring 'EXPORT', porque el portal puede rotular la
        #    opción como 'EXPORTACIÓN', 'EXPORTACION' o 'Exportación'. Un match
        #    exacto fallaba en silencio y dejaba pasar el booking sin categoría.
        try:
            result = await ctx.evaluate(
                """() => {
                    const sel = document.querySelector('#cbSearchCategory');
                    if (!sel) return { ok: false, reason: 'select-no-encontrado' };
                    const norm = s => (s || '')
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .trim().toUpperCase();
                    const opt = Array.from(sel.options)
                        .find(o => norm(o.text).includes('EXPORT'));
                    if (!opt) {
                        return {
                            ok: false,
                            reason: 'opcion-export-no-encontrada',
                            opciones: Array.from(sel.options).map(o => o.text),
                        };
                    }
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return {
                        ok: sel.value === opt.value,
                        reason: sel.value === opt.value ? 'ok' : 'value-no-aplicado',
                        value: sel.value,
                        text: opt.text,
                    };
                }"""
            )
        except Exception as exc:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] error ejecutando JS dispatchEvent en "
                f"#cbSearchCategory: {exc}"
            )
            await self._screenshot("cbSearchCategory_dispatchEvent_failed")
            return False

        if not result or not result.get("ok"):
            reason = result.get("reason") if result else "evaluate-devolvio-null"
            opciones = result.get("opciones") if result else None
            logger.error(
                f"[{self.terminal_name}][SCRAPE] no se pudo seleccionar EXPORTACIÓN "
                f"en #cbSearchCategory (motivo={reason}, opciones={opciones}) — "
                f"abortando para no pegar el booking sin categoría"
            )
            await self._screenshot("cbSearchCategory_exportacion_no_seleccionada")
            return False

        logger.info(
            f"[{self.terminal_name}][SCRAPE] EXPORTACIÓN seleccionada via JS + change event "
            f"(value={result.get('value')}, text={result.get('text')})"
        )

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

        # Esperar a que la grilla renderice su primera fila tras BUSCAR, en
        # vez de un wait fijo.
        try:
            await ctx.wait_for_selector(
                "table tbody tr", timeout=DEFAULT_TIMEOUT_MS
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] tabla con filas detectada"
            )
        except PlaywrightTimeoutError:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] no aparecieron filas en la tabla "
                f"tras BUSCAR"
            )
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

        # DEBUG: HTML de la primera fila para ver la estructura real del DOM.
        try:
            first_row_html = await ctx.evaluate(
                "document.querySelector('table tbody tr')?.innerHTML"
            )
            logger.debug(
                f"[{self.terminal_name}][SCRAPE] HTML primera fila: "
                f"{(first_row_html or '')[:500]}"
            )
        except Exception:
            pass

        items: list[dict] = []
        for i in range(row_count):
            try:
                cells = rows_loc.nth(i).locator("td")
                cell_count = await cells.count()
                logger.debug(
                    f"[{self.terminal_name}][SCRAPE] fila {i}: {cell_count} celdas"
                )
                # Filas con pocas celdas son placeholders/spinners/empty-state.
                if cell_count < MIN_CELLS_PER_ROW:
                    logger.debug(
                        f"[{self.terminal_name}][SCRAPE] fila {i} descartada: "
                        f"{cell_count} < {MIN_CELLS_PER_ROW}"
                    )
                    continue
                contenedor = (await cells.nth(COL_CONTENEDOR).inner_text()).strip()
                logger.debug(
                    f"[{self.terminal_name}][SCRAPE] fila {i}: contenedor='{contenedor}'"
                )
                if not contenedor:
                    logger.debug(
                        f"[{self.terminal_name}][SCRAPE] fila {i} descartada: "
                        f"contenedor vacío"
                    )
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

    async def _get_slots_for_container(
        self, row_index: int, desde_fecha: str | None = None
    ) -> list[dict]:
        """Clickea el calendario azul de la columna 'Coordinar' en la fila
        row_index y scrapea los turnos disponibles que se abren.

        SOLO lee — nunca clickea botones de confirmar/reservar.

        `desde_fecha` (formato "YYYY-MM-DD") filtra slots: si se pasa, solo se
        incluyen los que tengan fecha >= desde_fecha.
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

        # Antes de clickear: scroll a la fila para que la columna 'Coordinar'
        # —que puede quedar fuera del viewport— quede visible y clickeable.
        try:
            await ctx.evaluate(
                f"""() => {{
                    const row = document.querySelector(
                        'table tbody tr:nth-child({row_index + 1})'
                    );
                    if (row) row.scrollIntoView({{behavior: 'instant', block: 'center'}});
                }}"""
            )
            await ctx.wait_for_timeout(300)
        except Exception as exc:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] no pude hacer scroll a la fila "
                f"{row_index}: {exc}"
            )

        # Esperar a que Knockout termine de renderizar los botones Coordinar
        # antes de clickear. Sin esta espera el click corre contra un DOM
        # todavía incompleto y el botón de la fila no existe aún.
        try:
            await ctx.wait_for_selector(
                "button.btn-primary.btn-sm.btn-icon",
                state="visible",
                timeout=DEFAULT_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] no aparecieron los botones Coordinar "
                f"en el DOM para fila {row_index}"
            )
            await self._screenshot(f"coordinar_buttons_not_found_row_{row_index}")
            return []
        logger.info(f"[{self.terminal_name}][SCRAPE] botones Coordinar visibles en DOM")

        # Hay exactamente 1 botón Coordinar por fila, en orden — el índice del
        # botón coincide con row_index.
        buttons = ctx.locator("button.btn-primary.btn-sm.btn-icon")
        count = await buttons.count()
        logger.info(
            f"[{self.terminal_name}][SCRAPE] {count} botones Coordinar encontrados"
        )
        if row_index >= count:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] row_index {row_index} fuera de rango "
                f"({count} botones Coordinar)"
            )
            await self._screenshot(f"coordinar_index_out_of_range_row_{row_index}")
            return []

        target = buttons.nth(row_index)
        # El binding Knockout 'enable: allowToCreateAppt' deshabilita el botón
        # cuando no hay turno disponible para ese contenedor.
        if await target.get_attribute("disabled") is not None:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] botón Coordinar deshabilitado para "
                f"fila {row_index} — sin turno disponible"
            )
            return []

        # Click via dispatchEvent MouseEvent: ni el .click() de Playwright ni
        # un btn.click() por JS disparaban el handler de Knockout; un
        # MouseEvent 'click' con bubbles sí lo gatilla.
        await ctx.evaluate(
            f"""() => {{
                const btn = document.querySelectorAll(
                    'button.btn-primary.btn-sm.btn-icon'
                )[{row_index}];
                if (btn) btn.dispatchEvent(
                    new MouseEvent('click', {{bubbles: true, cancelable: true}})
                );
            }}"""
        )
        logger.info(
            f"[{self.terminal_name}][SCRAPE] dispatchEvent MouseEvent en botón "
            f"índice {row_index} (contenedor {container})"
        )

        # El click en Coordinar abre el modal #viewUnitCalendarModal con el
        # calendario de turnos. Esperar a que esté visible (clase '.show').
        modal_selector = (
            "#viewUnitCalendarModal.show, [id*='viewUnitCalendarModal'].show"
        )
        try:
            await ctx.wait_for_selector(modal_selector, timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] no apareció el modal "
                f"viewUnitCalendarModal para fila {row_index} (contenedor {container})"
            )
            await self._screenshot(f"viewUnitCalendarModal_not_found_row_{row_index}")
            return []
        logger.info(f"[{self.terminal_name}][SCRAPE] modal Calendario abierto")

        # Knockout puede tardar en poblar el modal tras abrirlo. Esperar a que
        # el contenedor aparezca en #txtUnitId y a que el calendario tenga
        # celdas antes de scrapear; si no, se lee un modal vacío.
        try:
            await ctx.wait_for_function(
                "document.querySelector('#txtUnitId')?.value?.trim().length > 0",
                timeout=10000,
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] modal cargado — txtUnitId tiene valor"
            )
            await ctx.wait_for_selector("#viewUnitCalendarModal td", timeout=10000)
        except PlaywrightTimeoutError:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] el modal Calendario no terminó de "
                f"cargar (txtUnitId vacío o calendario sin días) para {container}"
            )
            await self._screenshot(f"modal_not_loaded_row_{row_index}")
            return []

        # Confirmar el contenedor cargado en el modal.
        try:
            unit_id = await ctx.evaluate(
                "document.querySelector('#txtUnitId')?.value"
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] contenedor en modal: {unit_id}"
            )
        except Exception:
            pass

        # DEBUG: clases + texto de los <td> del calendario para identificar
        # cuáles marcan los días disponibles.
        try:
            td_classes = await ctx.evaluate(
                """() => Array.from(
                    document.querySelectorAll('#viewUnitCalendarModal td')
                )
                    .map(td => td.className + '|' + td.innerText.trim())
                    .filter(x => x.includes('|') && x.split('|')[1])
                    .slice(0, 20)
                    .join(' // ')"""
            )
            logger.info(
                f"[{self.terminal_name}][SCRAPE] clases de tds del calendario: "
                f"{td_classes}"
            )
        except Exception:
            pass

        # El modal es un calendario mensual de FullCalendar. SOLO LEER.
        #
        # FullCalendar organiza el grid en filas (.fc-day-grid .fc-row). Cada
        # fila tiene dos sub-estructuras paralelas:
        #   1. .fc-bg con td[data-date]      → la fecha exacta de cada columna
        #   2. .fc-content-skeleton con td.fc-event-container → los eventos
        #      (turnos disponibles) de cada columna
        # La columna del evento coincide por índice con la columna del día en
        # la misma fila, así que dayTds[i] da la fecha del evento eventTds[i].
        try:
            slots_data = await ctx.evaluate(
                """() => {
                    const rows = document.querySelectorAll(
                        '#viewUnitCalendarModal .fc-day-grid .fc-row'
                    );
                    const results = [];
                    for (const row of rows) {
                        // Fechas de esta fila (una por columna)
                        // SOLO dentro de .fc-bg: FullCalendar duplica los
                        // td[data-date] en .fc-bg y en .fc-content-skeleton,
                        // así que sin filtrar dayTds traía el doble y el
                        // índice del evento no coincidía con la fecha.
                        const dayTds = row.querySelectorAll('.fc-bg td[data-date]');
                        // Eventos de esta fila (td.fc-event-container, uno por columna)
                        const eventTds = row.querySelectorAll('td.fc-event-container');

                        for (let i = 0; i < eventTds.length; i++) {
                            const txt = eventTds[i].innerText.trim();
                            if (!txt) continue; // columna sin evento
                            const cantidad = parseInt(txt, 10);
                            if (isNaN(cantidad) || cantidad <= 0) continue;

                            // La fecha del día en esa columna está en dayTds[i]
                            const fecha = dayTds[i]?.getAttribute('data-date') || '';
                            if (!fecha) continue;

                            results.push({ fecha, cantidad });
                        }
                    }
                    return results;
                }"""
            )
        except Exception as exc:
            logger.error(
                f"[{self.terminal_name}][SCRAPE] error extrayendo slots del "
                f"calendario: {exc}"
            )
            slots_data = []

        logger.info(f"[{self.terminal_name}][SCRAPE] slots encontrados: {slots_data}")

        slots: list[dict] = []
        for slot in slots_data:
            fecha = slot.get("fecha", "")
            cantidad = slot.get("cantidad", 0)
            if not fecha:
                continue
            # Filtro opcional por fecha mínima (comparación lexicográfica
            # válida para el formato YYYY-MM-DD).
            if desde_fecha and fecha < desde_fecha:
                continue
            logger.info(
                f"[{self.terminal_name}][SCRAPE] {fecha} — {cantidad} turnos "
                f"({container})"
            )
            slots.append(
                {
                    "contenedor": container,
                    "fecha": fecha,
                    "cantidad": cantidad,
                    "franja": "INGRESO",
                }
            )

        # Cerrar el modal antes de retornar para dejar el portal en estado limpio.
        try:
            await ctx.click(
                "#viewUnitCalendarModal button.close, "
                "#viewUnitCalendarModal .btn-secondary",
                timeout=DEFAULT_TIMEOUT_MS,
            )
            await ctx.wait_for_selector(
                "#viewUnitCalendarModal", state="hidden", timeout=5000
            )
            logger.info(f"[{self.terminal_name}][SCRAPE] modal Calendario cerrado")
        except Exception as exc:
            logger.warning(
                f"[{self.terminal_name}][SCRAPE] no pude cerrar el modal Calendario: {exc}"
            )

        logger.info(
            f"[{self.terminal_name}][SCRAPE] {len(slots)} slot(s) para {container}"
        )
        return slots
