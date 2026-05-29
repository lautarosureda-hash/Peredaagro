# terminal-monitor

Bot de monitoreo de turnos en terminales portuarias argentinas para Pereda Agro S.A.
Detecta automáticamente la apertura de turnos para retiro/entrega de contenedores en TRP, T4 (APM Terminals) y Exolgan, y notifica al operador vía Telegram.

## Propósito

Las terminales portuarias publican turnos con muy poca antelación y se agotan en minutos. Este bot revisa periódicamente los portales de cada terminal con credenciales reales del operador, detecta nuevas franjas disponibles para los bookings/contenedores monitoreados y dispara alertas inmediatas a Telegram para que el equipo coordine el camión a tiempo.

## Stack

- Python 3.11+
- Playwright 1.59.0 (scraping con browser real, login en portales con JS)
- APScheduler 3.11.2 (ciclo de monitoreo periódico)
- python-telegram-bot 22.7 (handlers de comandos + alertas)
- Redis 7.4.0 (estado de items monitoreados y último snapshot de slots)
- loguru 0.7.3 (logging estructurado)
- python-dotenv 1.2.2 (carga de credenciales)
- Hosting: Railway

## Reglas de desarrollo

1. **Sin credenciales hardcodeadas.** Todo secreto viene de `os.environ` cargado vía `python-dotenv`. Nunca commitear `.env`.
2. **Logging con loguru.** Nada de `print()` ni `logging` stdlib. Formato `"[TERMINAL][PASO] mensaje"`.
3. **Type hints obligatorios** en todas las firmas de métodos y funciones.
4. **Excepciones no se silencian.** Se loguean con contexto. `login` falla → retorna `False`. `check_availability` falla → retorna `[]`. Nunca un `except: pass` mudo.
5. **Screenshots para debug.** Cada scraper debe poder llamar `_screenshot(name)` ante errores; los archivos van a `screenshots/` con timestamp.
6. **Stateless en el ciclo, stateful en Redis.** El worker no guarda estado en memoria entre ciclos. El último snapshot de slots y la lista de items activos viven en Redis.

## Módulos

- `main.py` — entry point. Carga `.env`, arranca el bot de Telegram y el scheduler de APScheduler.
- `bot/telegram_bot.py` — handlers de `/agregar`, `/lista`, `/stop`, `/status` y función `send_alert` para notificar turnos disponibles.
- `scrapers/base.py` — `BaseScraper` abstracto con `login`, `check_availability`, `_screenshot` y manejo de excepciones uniforme.
- `scrapers/trp.py`, `scrapers/t4.py`, `scrapers/exolgan.py` — implementaciones concretas (esqueletos por ahora).
- `storage/redis_client.py` — wrapper de Redis para items monitoreados y snapshots de slots.
- `scheduler/worker.py` — `run_check_cycle()` que itera items, corre scrapers y dispara alertas en diff.

## Cómo correr localmente

```
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # completá las credenciales
python main.py
```

Necesitás un Redis local corriendo (`docker run -p 6379:6379 redis:7`) o ajustar `REDIS_URL` a una instancia remota.

## Variables de entorno requeridas

- `TELEGRAM_BOT_TOKEN` — token del bot de Telegram (BotFather).
- `TELEGRAM_CHAT_ID` — chat ID al que llegan las alertas.
- `TRP_URL`, `TRP_USER`, `TRP_PASS` — portal y credenciales TRP.
- `T4_URL`, `T4_USER`, `T4_PASS` — portal y credenciales APM Terminals (T4).
- `EXOLGAN_URL`, `EXOLGAN_DNI`, `EXOLGAN_PASS`, `EXOLGAN_CUIT` — portal y credenciales Exolgan.
- `REDIS_URL` — URL de Redis (default `redis://localhost:6379/0`).
- `MONITOR_INTERVAL_MINUTES` — minutos entre ciclos del scheduler (default 10).
