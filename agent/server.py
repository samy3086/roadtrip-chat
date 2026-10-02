"""
Minimal LangGraph-Platform-compatible server for the road_trip agent.

Why this exists
---------------
The official Agent Chat UI talks to a LangGraph Agent Server. Deploying the real
thing (`langchain/langgraph-api`) requires `REDIS_URI`, `DATABASE_URI`,
`LANGSMITH_API_KEY` and `LANGGRAPH_CLOUD_LICENSE_KEY` (a paid LangSmith plan).

For a small demo agent we only need the handful of endpoints the SDK actually
calls. This module implements those against SQLite so it can run as a single
free container with no Redis, no Postgres and no license key.

Endpoints implemented (verified against @langchain/langgraph-sdk 0.0.57):

    GET  /info                        health check used by the UI
    POST /threads                     client.threads.create()
    GET  /threads/{thread_id}         thread fetch
    POST /threads/search              ThreadProvider history sidebar
    GET  /threads/{thread_id}/history useThreadHistory, called after every run
    POST /threads/{thread_id}/runs/stream   client.runs.stream() -> SSE

SSE events emitted: `metadata`, `messages`, `values`, `error`, `end`.

Conversation state
------------------
There is no checkpointer. Each run reloads the stored message list, appends the
new input, and invokes the agent with the full conversation. The returned state
is persisted as one history entry. This is deliberately simple and restart-proof
at the cost of re-sending prior turns to the model on every message, which
matters only if conversations get long.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# road_trip.py sits in different places depending on how this is run: inside
# the course repo it is ../notebooks/module-1, in the standalone deploy repo it
# sits next to this file. Probe both so one server.py works either way.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE,
    _HERE / "notebooks" / "module-1",
    _HERE.parent / "notebooks" / "module-1",
):
    if (_candidate / "road_trip.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break
else:  # pragma: no cover - only reached if the agent file is missing
    raise RuntimeError(f"road_trip.py not found near {_HERE}")

from langchain_core.messages import convert_to_messages  # noqa: E402

DB_PATH = os.environ.get("CHAT_DB_PATH", str(Path(__file__).resolve().parent / "chat.db"))
ASSISTANT_ID = os.environ.get("ASSISTANT_ID", "road_trip")

app = FastAPI(title="road_trip compat server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent = None


def get_agent():
    """Import the agent lazily so /info answers even if keys are missing."""
    global _agent
    if _agent is None:
        from road_trip import agent  # noqa: WPS433

        _agent = agent
    return _agent


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS threads (
            thread_id  TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata   TEXT NOT NULL DEFAULT '{}',
            status     TEXT NOT NULL DEFAULT 'idle'
        );
        CREATE TABLE IF NOT EXISTS states (
            seq                    INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id              TEXT NOT NULL,
            snapshot               TEXT NOT NULL,
            checkpoint_id          TEXT NOT NULL,
            parent_checkpoint_id   TEXT,
            metadata               TEXT NOT NULL DEFAULT '{}',
            created_at             TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS states_thread ON states (thread_id, seq);
        CREATE TABLE IF NOT EXISTS runs (
            run_id     TEXT PRIMARY KEY,
            thread_id  TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


init_db()


def row_to_thread(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "thread_id": row["thread_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "metadata": json.loads(row["metadata"]),
        "status": row["status"],
        "values": {},
        "interrupts": {},
    }


# --------------------------------------------------------------------------
# message serialization (must match what coerceMessageLikeToMessage accepts)
# --------------------------------------------------------------------------

def ser_content(content: Any) -> Any:
    """Content is a string or a list of already JSON-safe blocks."""
    return content


def ser_tool_calls(msg: Any) -> list[dict[str, Any]] | None:
    raw = getattr(msg, "tool_calls", None) or []
    calls = []
    for call in raw:
        if not isinstance(call, dict):
            call = {"name": call.get("name"), "args": {}, "id": call.get("id")}
        calls.append(
            {
                "name": call.get("name"),
                "args": call.get("args") or {},
                "id": call.get("id"),
                "type": "tool_call",
            }
        )
    return calls or None


def ser_message(msg: Any) -> dict[str, Any]:
    """A complete message, for the `values` and `history` payloads."""
    if isinstance(msg, dict):
        # Already serialized (e.g. carried in from stored history).
        t = msg.get("type") or ""
        # Never persist tool/thought traces
        if t in ("tool", "tool_call", "tool_calls", "thought", "thought_chunk", "reasoning", "system"):
            return {
                "type": "ai",
                "id": msg.get("id"),
                "content": msg.get("content") or "",
            }
        return msg
    t = getattr(msg, "type", "")
    if t in ("tool", "thought", "tool_call", "system"):
        return {
            "type": "ai",
            "id": getattr(msg, "id", None),
            "content": ser_content(getattr(msg, "content", "")),
        }
    out: dict[str, Any] = {
        "type": msg.type,
        "id": getattr(msg, "id", None),
        "content": ser_content(msg.content),
    }
    calls = ser_tool_calls(msg)
    if calls:
        out["tool_calls"] = calls
    if msg.type == "tool":
        out["tool_call_id"] = getattr(msg, "tool_call_id", None)
        out["name"] = getattr(msg, "name", None)
    extra = getattr(msg, "additional_kwargs", None)
    if extra:
        out["additional_kwargs"] = extra
    return out


def ser_chunk(chunk: Any, message_id: str) -> dict[str, Any]:
    """
    A streaming chunk. The SDK strips a trailing "MessageChunk" from the type
    and lowercases it, so "AIMessageChunk" becomes "ai" for them. Chunk-level
    tool_call_chunks are passed through, though the JS side only reads
    tool_calls, so tool progress is really carried by the `values` event.
    """
    out: dict[str, Any] = {
        "type": type(chunk).__name__,
        "id": message_id,
        "content": ser_content(getattr(chunk, "content", "")),
    }
    tool_chunks = getattr(chunk, "tool_call_chunks", None)
    if tool_chunks:
        out["tool_call_chunks"] = tool_chunks
    return out


def sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

@app.get("/info")
async def info() -> dict[str, Any]:
    return {
        "version": "compat-1",
        "flags": {
            "assistants": False,
            "crons": False,
            "langsmith": False,
        },
    }


@app.post("/threads")
async def create_thread(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    metadata = body.get("metadata") or {}
    thread_id = str(uuid.uuid4())
    stamp = now_iso()
    conn = db()
    conn.execute(
        "INSERT INTO threads (thread_id, created_at, updated_at, metadata, status)"
        " VALUES (?, ?, ?, ?, 'idle')",
        (thread_id, stamp, stamp, json.dumps(metadata)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    conn.close()
    return row_to_thread(row)


@app.get("/threads")
async def list_threads(request: Request) -> list[dict[str, Any]]:
    """
    Plain thread list. The SDK's ThreadsClient.list() is a GET here, but with
    no GET route on "/threads" FastAPI answered 405, which is the confusing
    half of "method not allowed" -- the path was right, the verb was not.
    """
    try:
        limit = int(request.query_params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    metadata_filter = request.query_params.get("metadata")

    conn = db()
    rows = conn.execute(
        "SELECT * FROM threads ORDER BY updated_at DESC LIMIT 1000"
    ).fetchall()
    conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        if metadata_filter:
            try:
                wanted = json.loads(metadata_filter)
            except Exception:
                wanted = {}
            meta = json.loads(row["metadata"])
            if not all(meta.get(k) == v for k, v in wanted.items()):
                continue
        out.append(row_to_thread(row))
    return out[: max(1, limit)]


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> JSONResponse:
    conn = db()
    row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    conn.close()
    if row is None:
        return JSONResponse({"detail": "Thread not found"}, status_code=404)
    return JSONResponse(row_to_thread(row))


@app.patch("/threads/{thread_id}")
async def update_thread(thread_id: str, request: Request) -> JSONResponse:
    """Merge a metadata patch. The sidebar sets graph_id this way."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    conn = db()
    row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Thread not found"}, status_code=404)

    metadata = json.loads(row["metadata"])
    patch = body.get("metadata")
    if isinstance(patch, dict):
        metadata.update(patch)
    conn.execute(
        "UPDATE threads SET metadata = ?, updated_at = ? WHERE thread_id = ?",
        (json.dumps(metadata), now_iso(), thread_id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    conn.close()
    return JSONResponse(row_to_thread(updated))


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str) -> JSONResponse:
    """Remove a thread and its states. Used by the sidebar's delete action."""
    conn = db()
    row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Thread not found"}, status_code=404)
    conn.execute("DELETE FROM states WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM runs WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"thread_id": thread_id})


@app.post("/threads/search")
async def search_threads(request: Request) -> list[dict[str, Any]]:
    """The history sidebar asks for threads whose metadata contains graph_id."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    wanted: dict[str, Any] = body.get("metadata") or {}
    limit = int(body.get("limit") or 100)

    conn = db()
    rows = conn.execute(
        "SELECT * FROM threads ORDER BY updated_at DESC LIMIT 500"
    ).fetchall()
    conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        meta = json.loads(row["metadata"])
        if all(meta.get(key) == value for key, value in wanted.items()):
            out.append(row_to_thread(row))
    return out[:limit]


async def _thread_history(thread_id: str, limit: int) -> list[dict[str, Any]]:
    """
    Returns states oldest-first; the SDK treats the final element as the head.
    Each state needs `values`, `checkpoint.checkpoint_id` and
    `parent_checkpoint.checkpoint_id` for its branch bookkeeping.
    """
    conn = db()
    rows = conn.execute(
        "SELECT * FROM states WHERE thread_id = ? ORDER BY seq DESC LIMIT ?",
        (thread_id, max(1, limit)),
    ).fetchall()
    conn.close()

    states = []
    for row in reversed(rows):
        states.append(
            {
                "values": json.loads(row["snapshot"]),
                "checkpoint": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": row["checkpoint_id"],
                },
                "parent_checkpoint": (
                    {"checkpoint_id": row["parent_checkpoint_id"]}
                    if row["parent_checkpoint_id"]
                    else None
                ),
                "metadata": json.loads(row["metadata"]),
                "tasks": [],
                "created_at": row["created_at"],
                "thread_id": thread_id,
            }
        )
    return states


# The SDK's ThreadsClient.getHistory POSTs to this path (client.js:
# `getHistory` -> { method: "POST", json: { limit } }). Only implementing GET
# made every history load 405, which surfaced in the chat UI as
# "Method Not Allowed". GET is kept because the local test harness and curl
# use it. Both verbs share the handler so the two paths cannot drift.
@app.get("/threads/{thread_id}/history")
async def thread_history_get(thread_id: str, limit: int = 10) -> list[dict[str, Any]]:
    return await _thread_history(thread_id, limit)


@app.post("/threads/{thread_id}/history")
async def thread_history_post(thread_id: str, request: Request) -> list[dict[str, Any]]:
    # Body mirrors the SDK: { limit, before, metadata, checkpoint }.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        limit = int(body.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    return await _thread_history(thread_id, limit)


def _persist(
    thread_id: str,
    values: dict[str, Any],
    parent_checkpoint_id: str | None,
    run_id: str,
    step: int,
    failed: bool,
) -> None:
    """
    Save one history state. Called on success and on failure alike.

    parent_checkpoint_id must be the previous state's checkpoint_id, not a
    message id: the SDK's getBranchSequence keys children by
    parent_checkpoint.checkpoint_id and looks them up by checkpoint.checkpoint_id,
    so a mismatched parent orphans this state and the turn never renders.
    """
    serialized = [ser_message(m) for m in values.get("messages", [])]
    conn = db()
    try:
        conn.execute(
            "INSERT INTO states"
            " (thread_id, snapshot, checkpoint_id, parent_checkpoint_id, metadata, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                json.dumps({"messages": serialized}),
                str(uuid.uuid4()),
                parent_checkpoint_id,
                json.dumps(
                    {"source": "loop", "step": step, "run_id": run_id, "error": failed}
                ),
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE threads SET updated_at = ?, status = ? WHERE thread_id = ?",
            (now_iso(), "error" if failed else "idle", thread_id),
        )
        conn.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            ("error" if failed else "success", run_id),
        )
        conn.commit()
    finally:
        conn.close()


@app.post("/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: Request) -> StreamingResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    raw_input = body.get("input") or {}
    assistant_id = body.get("assistant_id") or ASSISTANT_ID
    run_id = str(uuid.uuid4())
    stamp = now_iso()

    conn = db()
    row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO threads (thread_id, created_at, updated_at, metadata, status)"
            " VALUES (?, ?, ?, ?, 'idle')",
            (thread_id, stamp, stamp, json.dumps({"graph_id": assistant_id})),
        )
    else:
        # Stamp the graph_id so /threads/search can find this thread later.
        meta = json.loads(row["metadata"])
        meta.setdefault("graph_id", assistant_id)
        conn.execute(
            "UPDATE threads SET metadata = ?, updated_at = ? WHERE thread_id = ?",
            (json.dumps(meta), stamp, thread_id),
        )
    conn.execute(
        "INSERT INTO runs (run_id, thread_id, created_at) VALUES (?, ?, ?)",
        (run_id, thread_id, stamp),
    )
    conn.commit()

    prior_rows = conn.execute(
        "SELECT * FROM states WHERE thread_id = ? ORDER BY seq", (thread_id,)
    ).fetchall()
    conn.close()

    prior_messages: list[dict[str, Any]] = []
    prior_checkpoint_id: str | None = None
    if prior_rows:
        prior_messages = json.loads(prior_rows[-1]["snapshot"]).get("messages", [])
        prior_checkpoint_id = prior_rows[-1]["checkpoint_id"]
    prior_step = len(prior_rows)

    async def events() -> AsyncIterator[bytes]:
        yield sse("metadata", {"run_id": run_id, "thread_id": thread_id})

        try:
            agent = get_agent()
        except Exception as exc:  # missing keys etc.
            yield sse("error", {"message": f"agent unavailable: {exc}"})
            yield sse("end", {})
            return

        # The UI sends `{"messages": [ {...}, ... ]}`; convert_to_messages
        # accepts the same {type, content, id} dicts the SDK serializes. Stored
        # prior turns come back as plain dicts, so convert those too, otherwise
        # the payload mixes dicts with message objects and serialization breaks.
        incoming = convert_to_messages(raw_input.get("messages") or [])
        prior_msgs = convert_to_messages(prior_messages) if prior_messages else []
        # sanitize before invoking
        prior_msgs = convert_to_messages(_sanitize_messages([ser_message(m) for m in prior_msgs])) if prior_msgs else []
        payload = {"messages": prior_msgs + incoming}

        latest_values: dict[str, Any] = {"messages": payload["messages"]}
        # Chunk ids can be None; keep one stable id per streamed message.
        current_id: str | None = None

        try:
            async for mode, chunk in agent.astream(
                payload, stream_mode=["messages", "values"]
            ):
                if mode == "messages":
                    # LangGraph yields (message_chunk, metadata) tuples here,
                    # not a bare chunk. Older shapes yield the chunk directly.
                    message_chunk = chunk[0] if isinstance(chunk, tuple) else chunk
                    is_ai = type(message_chunk).__name__ in (
                        "AIMessageChunk",
                        "ChatMessageChunk",
                    )
                    has_content = bool(message_chunk.content)
                    tool_chunks = getattr(message_chunk, "tool_call_chunks", None) or []
                    starts_call = bool(tool_chunks) and tool_chunks[0].get("index") == 0

                    if not is_ai:
                        # Tool/system chunks render fine through `values`.
                        pass
                    elif not has_content:
                        # Keep ids stable across a message's chunks, including
                        # the tool-call preamble where content is still empty.
                        if starts_call or current_id is None:
                            current_id = getattr(message_chunk, "id", None) or str(
                                uuid.uuid4()
                            )
                    else:
                        current_id = getattr(message_chunk, "id", None) or current_id
                        if current_id is None:
                            current_id = str(uuid.uuid4())

                    if is_ai and has_content and current_id:
                        yield sse("messages", [ser_chunk(message_chunk, current_id)])

                elif mode == "values":
                    latest_values = chunk
                    msgs = chunk.get("messages", [])
                    sanitized = _sanitize_messages([ser_message(m) for m in msgs])
                    # Always keep at least the last human+ai visible pair if filtering removed everything
                    if not sanitized and msgs:
                        # fallback: keep only the very last ai/human
                        last = msgs[-1]
                        lm = ser_message(last)
                        t = lm.get("type")
                        if t in ("ai", "assistant", "human"):
                            sanitized = [lm]
                    latest_values["messages"] = sanitized
                    yield sse("values", {"messages": sanitized})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Persist what was already streamed before reporting the failure.
            # Otherwise the UI shows the partial reply, then re-reads an empty
            # history on refresh and the turn appears to vanish.
            _persist(
                thread_id, latest_values, prior_checkpoint_id, run_id, prior_step, failed=True
            )
            yield sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            yield sse("end", {})
            return

        _persist(
            thread_id, latest_values, prior_checkpoint_id, run_id, prior_step, failed=False
        )
        yield sse("end", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - containers must listen externally
        port=int(os.environ.get("PORT", "8000")),
    )