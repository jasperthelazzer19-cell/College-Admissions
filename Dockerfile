# Candor web + in-container carousel renderer.
# Replaces the Nixpacks build so the container ships a real headless Chromium —
# this lets the render worker (RENDER_WORKER=1) generate carousels server-side,
# so the phone "Make" buttons work even when the creator's Mac is asleep/off.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHROME=/usr/bin/chromium \
    CHROME_EXTRA_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"

# chromium = the renderer; fonts so slides render like they do on macOS
# (Newsreader is embedded/web-loaded; these cover body + emoji fallbacks).
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      fonts-liberation \
      fonts-dejavu-core \
      fonts-noto-color-emoji \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .

# Railway injects $PORT; app.py binds 0.0.0.0:$PORT. The web-volume mounts at
# /app/data at runtime (DB_PATH=/app/data/college.db) — do not write the DB here.
CMD ["python", "app.py"]
