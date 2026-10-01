# Root Dockerfile: a build-context shim for Render.
#
# Render's default Root Directory is the repo root, so a Blueprint run without
# "Root Directory: agent" set looks for ./Dockerfile and fails with
# "failed to read dockerfile: open Dockerfile: no such file or directory".
# This file makes the default case work so the build succeeds either way.
#
# The real build lives in agent/Dockerfile. Keep the two in step when the
# dependencies or start command change.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY agent/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY agent/server.py ./server.py
COPY agent/road_trip.py ./road_trip.py

# /data is a mount point so a disk can be attached later. On the free tier
# there is no persistent disk and chat history resets on each deploy.
ENV PORT=8000 \
    CHAT_DB_PATH=/data/chat.db \
    ASSISTANT_ID=road_trip
RUN mkdir -p /data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
