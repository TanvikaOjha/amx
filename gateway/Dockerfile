FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY agent-card.json .

# SQLite channel state (payment ledger + HCS outbox) lives in /data.
# Create it and give it to the non-root user BEFORE declaring the VOLUME: Docker discards any change made
# to a volume path after the VOLUME instruction, and a new named volume copies the ownership of the image's
# directory on first use.  (With a host bind mount instead, chown the host directory to uid 10001.)
RUN useradd --system --uid 10001 amx \
    && mkdir -p /data \
    && chown amx:amx /data
ENV DB_PATH=/data/amx_gateway.sqlite3
VOLUME ["/data"]

USER amx

EXPOSE 8000
# ONE worker: the v1 ledger, HCS outbox and rate limiter assume a single process.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]