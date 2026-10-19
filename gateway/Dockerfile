FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY agent-card.json .

# SQLite channel state lives here; mount a volume so restarts keep nonces.
VOLUME ["/data"]
ENV DB_PATH=/data/amx_gateway.sqlite3

RUN useradd --system --uid 10001 amx && mkdir -p /data && chown amx /data
USER amx

EXPOSE 8000
# ONE worker: the v1 ledger and rate limiter assume a single process.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]