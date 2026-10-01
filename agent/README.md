# road_trip agent - hosted backend

A single-container LangGraph-Platform-compatible server for the `road_trip`
agent, so the Vercel chat UI has a permanent backend instead of a tunnel to a
laptop.

## Why this exists

The official Agent Chat UI talks to a LangGraph Agent Server. Deploying the real
`langchain/langgraph-api` image requires all of:

```
REDIS_URI                    Redis, for resumable streaming
DATABASE_URI                 Postgres, for threads/runs/checkpoints
LANGSMITH_API_KEY            tracing
LANGGRAPH_CLOUD_LICENSE_KEY  a paid LangSmith plan
```

`langgraph dev` is exempt from the license check, which is why local dev needs
none of this. For one demo agent the SDK only touches six endpoints, so this
module implements them against SQLite: no Redis, no Postgres, no license key, no
billing.

## Endpoints

| Endpoint | SDK caller |
|---|---|
| `GET /info` | `checkGraphStatus` in `providers/Stream.tsx` |
| `POST /threads` | `client.threads.create()` |
| `GET /threads/{id}` | thread fetch |
| `POST /threads/search` | `ThreadProvider`, the history sidebar |
| `GET /threads/{id}/history` | `useThreadHistory`, called after every run |
| `POST /threads/{id}/runs/stream` | `client.runs.stream()` |

SSE events emitted: `metadata`, `messages`, `values`, `error`, `end`. Note the
server streams both `messages` and `values` regardless of what
`stream_mode` the client asked for; the SDK handles both and the UI gets token
streaming even though it requests `["values"]`.

## Files

- `server.py` - the whole backend
- `requirements.txt` - pinned to the versions verified in the course venv
- `Dockerfile` - `python:3.13-slim`, single process
- `.env.example` - the two keys, names only

`road_trip.py` is expected either next to `server.py` (standalone deploy) or at
`../notebooks/module-1/` (course repo). `server.py` probes both.

## Running it

Locally, using the separate venv that avoids touching the course one:

```powershell
cd C:\Users\kayou\OneDrive\Bureau\week3\lca-lc-foundations
.\.venv-deploy\Scripts\python.exe -m uvicorn deploy.server:app --port 2030
```

`road_trip.py` calls `load_dotenv()`, so run it from the repo root to pick up
the course `.env`. The API keys are read from the environment, never from here.

## Deploying

1. Push `server.py`, `requirements.txt`, `Dockerfile`, `.env.example` and a copy
   of `road_trip.py` to a public repo.
2. Create a Web Service from that repo, Dockerfile as the build method.
3. Set `GOOGLE_API_KEY` and `TAVILY_API_KEY` in the host's dashboard.
4. Note the public URL, then set `NEXT_PUBLIC_API_URL` in
   `roadtrip-chat/apps/web/.env.production` and redeploy to Vercel.

## Conversation state

There is no checkpointer. Each run reloads the stored message list, appends the
new input, and invokes the agent with the whole conversation, then saves the
returned state as one history entry.

Deliberately simple and restart-proof. The cost is that prior turns are re-sent
to the model on every message, so long conversations get more expensive. Fine
for a demo, wrong for production.

## Test coverage

`protocol_test.py` / `protocol_client.py` ran the real endpoints against a stub
graph and asserted all 24 protocol invariants (SSE event order, chunk shape,
history branch fields, thread search) with no API calls. Only the Gemini call
itself is unexercised in that harness.