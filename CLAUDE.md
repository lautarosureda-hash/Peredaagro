# terminal-monitor — Pereda Agro S.A.

Bot de monitoreo de turnos en terminales portuarias argentinas.
Detecta automáticamente la apertura de turnos para retiro/entrega de contenedores en T4/APM Terminals, TRP y Exolgan, y notifica al operador vía Telegram.

## Estado actual (29/05/2026)

- ✅ **Deploy funcionando en Railway** (Dockerfile-based, deployment `708770bb`)
- ✅ **Scraper T4 funcionando** — login, búsqueda por booking, extracción de turnos del calendario, alertas enviadas
- ✅ **Bot Telegram operativo** — `/agregar`, `/lista`, `/stop`, `/status` respondiendo
- ✅ **Alertas funcionando** — primer alerta real enviada: BUA0367004 / TLLU2006489, 46 turnos 01/06 y 71 turnos 02/06
- 🔲 Scraper TRP — skeleton, pendiente de implementar
- 🔲 Scraper Exolgan — skeleton, pendiente de implementar
- 🔲 Comando `/check` para forzar ciclo inmediato desde Telegram

## Stack

- Python 3.11 (imagen `python:3.11-slim` vía Dockerfile)
- Playwright 1.59.0 (scraping con browser real)
- APScheduler 3.11.2 (ciclo de monitoreo cada 10 min)
- python-telegram-bot 22.7
- Redis 7.4 (Railway add-on, `zephyr.proxy.rlwy.net:33689`)
- loguru 0.7.3
- Hosting: Railway (Dockerfile builder)

## Repo y deploy

- **GitHub:** https://github.com/lautarosureda-hash/Peredaagro
- **Railway project:** https://railway.com/project/a33fee1d-59e7-4084-9a1d-3293fcb74707
- **Deploy:** `railway up` desde local (NO hay auto-deploy activado)
- **IMPORTANTE:** antes de `railway up`, siempre hacer `git pull origin main`

## Estructura

```
terminal-monitor/
├── Dockerfile               # python:3.11-slim + libs sistema + playwright chromium
├── railway.json             # builder: DOCKERFILE
├── .env                     # credenciales reales (gitignored)
├── .env.example
├── requirements.txt
├── CLAUDE.md
├── main.py
├── Procfile
├── bot/
│   └── telegram_bot.py      # handlers Telegram + send_alert
├── scrapers/
│   ├── base.py
│   ├── t4.py                # ✅ COMPLETO Y FUNCIONANDO
│   ├── trp.py               # skeleton
│   └── exolgan.py           # skeleton
├── storage/
│   └── redis_client.py
├── scheduler/
│   └── worker.py            # run_check_cycle()
└── tests/
    ├── test_t4_manual.py
    └── logs/
```

## Reglas de desarrollo

1. **Sin credenciales hardcodeadas.** Todo secreto viene de `os.environ`. Nunca commitear `.env`.
2. **Logging con loguru.** Nada de `print()`. Formato `"[TERMINAL][PASO] mensaje"`.
3. **Type hints obligatorios** en todas las firmas.
4. **Excepciones no se silencian.** `login` falla → `False`. `check_availability` falla → `[]`.
5. **Screenshots para debug.** `_screenshot(name)` ante errores; archivos en `screenshots/`.
6. **Stateless en el ciclo, stateful en Redis.** Estado vive en Redis, no en memoria.

## Módulos

- `main.py` — entry point. Carga `.env`, arranca bot + scheduler.
- `bot/telegram_bot.py` — handlers Telegram. `cmd_status` construye el mensaje desde Redis (`worker:last_run`, `worker:last_error`). Error guardado truncado a 300 chars para no superar límite de Telegram.
- `scrapers/t4.py` — scraper T4/APM. Login con CUIT `30508697909`. Busca por booking, itera contenedores, abre modal de Calendario, extrae slots (fecha + cantidad de turnos). NUNCA confirma ningún turno.
- `scheduler/worker.py` — `run_check_cycle()` abre un browser por ciclo, itera items por terminal, compara con snapshot previo en Redis, dispara alertas en diff.
- `storage/redis_client.py` — `get_state/set_state` para snapshots, `hgetall/hset/hdel` para items activos en `items:active`.

## Redis keys

- `items:active` — hash con los items monitoreados (escrito por el bot)
- `worker:last_run` — timestamp del último ciclo exitoso
- `worker:last_error` — último error del worker (truncado a 300 chars)

## Variables de entorno (Railway)

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `T4_URL`, `T4_USER` (CUIT 30508697909), `T4_PASS`
- `REDIS_URL` (`zephyr.proxy.rlwy.net:33689`)
- `MONITOR_INTERVAL_MINUTES=10`

## Comandos de desarrollo

```powershell
# Local
cd C:\Users\lauta\repos\terminal-monitor
python main.py

# Deploy
git pull origin main
railway up

# Test manual T4
python tests/test_t4_manual.py > tests\logs\test_output.txt 2>&1
```

## Próximos pasos

1. Comando `/check` — forzar ciclo inmediato desde Telegram
2. Activar auto-deploy en Railway (Settings → Source → conectar GitHub)
3. Scraper TRP — login usuario/contraseña, identificador = N° contenedor
4. Scraper Exolgan — flujo 7 pasos, detener ANTES del click de reserva
5. Fix encoding screenshot (`\u2192` → charmap en Windows)
