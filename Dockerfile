FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium + TODAS sus dependencias de sistema vía Playwright.
# Usamos `--with-deps` en lugar de una lista manual de libs porque Playwright
# mantiene la lista completa y curada para su build de chromium-headless-shell.
# Una lista manual incompleta hacía que chrome-headless-shell crasheara al
# lanzarse (SIGTRAP / TargetClosedError).
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "main.py"]
