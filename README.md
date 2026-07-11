# Eden

Eden represents the Garden of Eden where God places its Angels. The Angels are user persona's guardian Angels that operate on top of Hermes-Agent under the Linda coordination principle.

## Overview

Eden is a FastAPI service that recreates the **JavaSpaces programming model** — a shared tuple space supporting write, read, take, notify, leases, and transactions — on top of **Hermes-Agent Kanban cards**. The Angels coordinate through the space instead of talking to each other directly (the Linda principle).

The objective is deliberately **not** the complete JavaSpaces specification with all of its semantics and guarantees; matching is exact-equality on a subset of fields, storage is in-memory, and transactions only cover write/take.

### Conceptual Mapping

| JavaSpaces | Hermes-Agent Kanban | Eden endpoint |
|---|---|---|
| Space | Shared Kanban board | `space` field (default `"eden"`) |
| Entry | Kanban card (kind + fields) | `POST /api/write/` body |
| write() | Create a card | `POST /api/write/` |
| read(template) | Query matching cards without removing them | `POST /api/read/`, `POST /api/read_all/` |
| take(template) | Atomically claim or remove a matching card | `POST /api/take/` |
| notify() | Subscribe to events on matching cards | `POST /api/notify/` + SSE stream |
| Lease | Card expiration / time-to-live (TTL) | `lease_seconds`, `/api/lease/renew/`, `/api/lease/cancel/` |
| Transactions | Atomic board operations | `/api/txn/begin/`, `/api/txn/commit/`, `/api/txn/abort/` |

## Security

Same model as the `events` project:

- Every endpoint requires the `API-KEY` header, matching the key in `.env`
- The `.env` file is never committed (see `.gitignore`)
- Run mode is resolved automatically: Windows → `TESTING` (debug logging to `C:\temp\python_eden.log`), Linux → `PROD` (the VPS, logging to `/home/angel/logs/python_eden.log`). Override with `MODE=TESTING|PROD`.
- Use HTTPS (reverse proxy) in production on the VPS

Create a `.env` file:
```
API_KEY = "your_secure_api_key_here"
```

## Card Model

A card is a JavaSpaces Entry with Kanban flavor:

```json
{
  "id": 1,
  "space": "eden",
  "kind": "task",
  "fields": {"persona": "researcher", "action": "summarize"},
  "lane": "open",
  "created_by": "gabriel",
  "claimed_by": null,
  "created_at": "2026-07-11T12:00:00Z",
  "lease_expires_at": "2026-07-12T12:00:00Z",
  "txn_id": null
}
```

Lanes: `open` (matchable by read/take) and `claimed` (taken with `mode=claim`, owned by an agent, still visible on the board).

**Templates** follow JavaSpaces matching: omitted values are wildcards, provided values must match exactly. `{"kind": "task", "fields": {"persona": "researcher"}}` matches any open `task` card whose `persona` field equals `researcher`, whatever its other fields are.

## API Endpoints

All requests need the header `API-KEY: <your key>`.

### write — `POST /api/write/`
```json
{
  "space": "eden",
  "kind": "task",
  "fields": {"persona": "researcher", "action": "summarize", "url": "https://arxiv.org/abs/2511.00402"},
  "lease_seconds": 3600,
  "agent": "gabriel",
  "txn_id": null
}
```
`lease_seconds`: TTL of the card (0 = forever, server default 24 h). With a `txn_id` the card stays invisible until the transaction commits.

### read — `POST /api/read/`
Returns **one** matching card without removing it (404 if none).
```json
{
  "template": {"kind": "task", "fields": {"persona": "researcher"}},
  "timeout_seconds": 0
}
```
`timeout_seconds`: `0` = readIfExists; `> 0` = block until a matching card is written or the timeout elapses (max 60 s).

### read_all — `POST /api/read_all/`
Bulk non-destructive scan of every matching card. Optional `?limit=N`.

### take — `POST /api/take/`
Atomically claim or remove **one** matching card — two Angels can never take the same card.
```json
{
  "template": {"kind": "task"},
  "mode": "claim",
  "agent": "michael",
  "timeout_seconds": 10
}
```
- `mode: "claim"` — Kanban style: the card moves to the `claimed` lane with `claimed_by` set; it is no longer matchable but stays visible on the board.
- `mode: "remove"` — classic JavaSpaces take: the card leaves the space.
- With a `txn_id` the card is held invisibly until commit (finalized) or abort (restored).

### board — `GET /api/board/?space=eden`
Kanban view: all cards of a space grouped by lane (`open` / `claimed`). Cards held by uncommitted transactions are hidden.

### card by id — `GET /api/card/{card_id}`

### Leases
- `POST /api/lease/renew/` — `{"card_id": 1, "lease_seconds": 7200}` extends the TTL from now (0 = forever).
- `DELETE /api/lease/cancel/?card_id=1` — the card is removed immediately.
- A background reaper purges expired cards every few seconds and emits `expire` notifications.

### Transactions
- `POST /api/txn/begin/` — `{"timeout_seconds": 60}` → `{"txn_id": "..."}`. The txn auto-aborts when its lease expires.
- `POST /api/txn/commit/` — `{"txn_id": "..."}`: pending writes become visible, held takes are finalized, atomically.
- `POST /api/txn/abort/` — `{"txn_id": "..."}`: pending writes are discarded, held cards are restored.

### notify — subscriptions + SSE
1. Register: `POST /api/notify/` with `{"template": {"kind": "task"}}` → `{"sub_id": "...", "stream_url": "/api/notify/..."}`
2. Stream: `GET /api/notify/{sub_id}` (Server-Sent Events). Each message:
   ```
   data: {"type": "write", "card": {...}}
   ```
   Event types: `write` (card created or txn committed), `take`, `expire`, `cancel`.
3. Cancel: `DELETE /api/notify/{sub_id}`

## Examples (curl)

```bash
# An Angel writes a task card
curl -X POST "http://localhost:8000/api/write/" \
  -H "API-KEY: your_api_key" -H "Content-Type: application/json" \
  -d '{"kind": "task", "fields": {"persona": "researcher"}, "agent": "gabriel"}'

# Another Angel blocks up to 30s waiting to claim a task
curl -X POST "http://localhost:8000/api/take/" \
  -H "API-KEY: your_api_key" -H "Content-Type: application/json" \
  -d '{"template": {"kind": "task"}, "mode": "claim", "agent": "michael", "timeout_seconds": 30}'

# Watch the board
curl -H "API-KEY: your_api_key" "http://localhost:8000/api/board/"

# Subscribe and stream notifications
curl -X POST "http://localhost:8000/api/notify/" \
  -H "API-KEY: your_api_key" -H "Content-Type: application/json" \
  -d '{"template": {"kind": "task"}}'
curl -N -H "API-KEY: your_api_key" "http://localhost:8000/api/notify/<sub_id>"
```

## Setup & Running

```bash
# Install dependencies (inside the virtualenv)
pip install -r requirements.txt

# Run the server
uvicorn main:app --reload
```

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

Server-side defaults (override via environment variables): `DEFAULT_LEASE_SECONDS` (86400), `DEFAULT_TXN_LEASE_SECONDS` (60), `REAPER_INTERVAL_SECONDS` (5).

### VPS deployment

Just like `events`, Eden detects Linux and switches to `PROD` mode automatically (INFO logging to `/home/angel/logs/python_eden.log`). Run it behind your reverse proxy with HTTPS, e.g.:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

## Testing

```bash
python tests/test_api.py
```

Covers security (API key), write/read/take (both modes), board view, leases (renew/cancel/expiry), transactions (commit, abort, isolation), and notify subscriptions — 32 checks.

## Error Responses

- `403` — `{"detail": "Invalid API Key"}`
- `404` — no card matches the template / unknown card, transaction, or subscription

## Future Enhancements

- [ ] Persistent storage (database backend)
- [ ] Rich template matching (ranges, regex)
- [ ] Notify leases (auto-expiring subscriptions)
- [ ] Multiple named boards with per-board settings
- [ ] Direct Hermes-Agent Kanban backend integration
