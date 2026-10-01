# Single free-tier container: no Redis, no Postgres, no LangSmith license key.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# server.py resolves road_trip.py from beside itself when present, otherwise
# from ../notebooks/module-1 (the layout inside the course repo).
COPY server.py ./
COPY road_trip.py ./road_trip.py

# Chat history lives on a mounted volume so it survives restarts. On a host
# without a persistent disk this resets to empty, which is harmless.
ENV PORT=8000 \
    CHAT_DB_PATH=/data/chat.db \
    ASSISTANT_ID=road_trip
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]