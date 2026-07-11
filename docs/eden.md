# Eden — Documentation

Eden represents the Garden of Eden where God places its Angels. The Angels are user persona's guardian Angels that operate on top of Hermes-Agent under the Linda coordination principle.

This document explains the concept, the architecture, the data models, the API, and the agent layer in detail. For a quick start see the [README](../README.md).

---

## 1. Concept

### 1.1 The Linda principle

[Linda](https://en.wikipedia.org/wiki/Linda_(coordination_language)) is a coordination model from the 1980s (David Gelernter): independent processes never communicate directly — they coordinate through a shared **tuple space** into which they *write* tuples and from which they *read* or *take* tuples matched by templates. The processes are decoupled in **space** (they don't know each other), in **time** (they don't need to run simultaneously), and in **naming** (a tuple carries data, not an addressee).

**JavaSpaces** (Sun, part of Jini) is the best-known industrial implementation of Linda, adding entries (typed tuples), leases, notifications, and transactions.

### 1.2 The Eden mapping

Eden recreates the JavaSpaces programming model on top of **Hermes-Agent Kanban cards**. The space is a Kanban board; entries are cards; taking a card is claiming it.

| JavaSpaces | Hermes-Agent Kanban | Eden |
|---|---|---|
| Space | Shared Kanban board | a `space` name (default `"eden"`) |
| Entry | Kanban card | `Card` (kind + free-form fields) |
| `write()` | Create a card | `POST /api/write/` |
| `read(template)` | Query without removing | `POST /api/read/`, `/api/read_all/` |
| `take(template)` | Atomically claim or remove | `POST /api/take/` |
| `notify()` | Subscribe to card events | `POST /api/notify/` + SSE |
| Lease | Card TTL | `lease_seconds`, `/api/lease/*` |
| Transaction | Atomic board operations | `/api/txn/*` |

Deliberately **not** the full JavaSpaces specification: matching is exact-equality on a subset of fields, storage is in-memory in a single process, and transactions only cover write/take. The goal is the programming model, not the guarantees of a distributed Jini deployment.

### 1.3 The cast

- **God** — the operator (a human or an orchestrator) who places Angels in the Garden and writes the entry cards of a workflow.
- **Angel** — a guardian agent serving one *user persona*, running on Hermes-Agent. Its behaviour is an agentskills.io skill (a `SKILL.md`).
- **The Host (`Angels`)** — a multi-agent system: a set of Angels wired together *only* through the board.
- **The Garden (`TupleSpace`)** — the shared space holding all cards.

---

## 2. Architecture

```
                 ┌───────────────────────────────────────────┐
                 │              Eden API (FastAPI)           │
   Angel ──HTTP──►  main.py: 15 entry points, API-KEY auth   │
   Angel ──SSE───►  space.py: TupleSpace ("the Garden")      │
   God  ───HTTP──►    cards / transactions / subscriptions   │
                 │    asyncio.Condition = atomicity + wakeup │
                 │    reaper task = lease & txn expiry       │
                 └───────────────────────────────────────────┘

   angel.py   Angel, AngelSkill, SkillBundle (skill-bundle/v1)
   angels.py  Skill (identity), SkillDependency, Angels (the Host)
   config.py  TESTING/PROD mode, rotating file logging
```

### 2.1 Modules

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app: request models, the 15 endpoints, API-key check, background reaper |
| `space.py` | The tuple-space engine: `Card`, `Template`, `TupleSpace`, `Transaction`, `Subscription` |
| `angel.py` | `Angel` + `AngelSkill` (agentskills.io) and the `skill-bundle/v1` transport (`SkillBundle`) |
| `angels.py` | `Skill` identity, `SkillDependency` (card + workflow assignments), `Angels` (the Host) |
| `config.py` | Run-mode resolution and logging (same pattern as the `events` project) |
| `assets/` | On-disk skills; `assets/guardian-angel/SKILL.md` is the first simple Agent |
| `tests/` | Three self-contained suites (see §7) |

### 2.2 Concurrency model

All mutations of the space happen while holding a single `asyncio.Condition`:

- **Atomicity of take()** — matching and claiming/removing happen under the lock, so two Angels can never take the same card, even when both were blocked waiting for it.
- **Blocking read/take** — a request with `timeout_seconds > 0` waits on the condition; every state change that can create a match (write, commit, abort, expiry) calls `notify_all()` to wake the waiters, which re-check their template.
- **Single process** — the lock is per-process, so Eden must run as **one uvicorn worker** (no `--workers N`). This mirrors the in-memory design of the `events` service.

A background **reaper** task runs every `REAPER_INTERVAL_SECONDS` (default 5 s): it removes cards whose lease expired (emitting `expire` notifications) and auto-aborts transactions whose lease expired (restoring their held cards).

---

## 3. The space in detail

### 3.1 Card (the Entry)

```json
{
  "id": 7,
  "space": "eden",
  "kind": "task",
  "fields": {"persona": "researcher", "action": "summarize"},
  "lane": "open",
  "created_by": "god",
  "claimed_by": null,
  "created_at": "2026-07-11T12:00:00Z",
  "lease_expires_at": "2026-07-12T12:00:00Z",
  "txn_id": null
}
```

- `kind` plays the role of the JavaSpaces Entry *class*; `fields` are the tuple's values (any JSON).
- `lane` is the Kanban lane: `open` (matchable) or `claimed` (owned by an agent, visible on the board but no longer matchable). While held by an uncommitted transaction a card is in the internal `held` lane and invisible.
- `lease_expires_at = null` means the card never expires.

### 3.2 Template matching

A template follows JavaSpaces semantics — *omitted values are wildcards, provided values must match exactly*:

```json
{"space": "eden", "kind": "task", "fields": {"persona": "researcher"}}
```

matches any **open** card on board `eden` of kind `task` whose `persona` field equals `"researcher"`, regardless of its other fields. `kind` omitted = any kind; `fields` empty = any fields. Matching is exact equality per field (no ranges or regex — see §8).

### 3.3 Visibility rules

A card is matchable by read/take when **all** of these hold:

1. it is in the `open` lane;
2. it is not held or pending under a transaction — unless the request carries that same `txn_id` (a transaction sees its own uncommitted writes).

The board view (`GET /api/board/`) hides cards involved in uncommitted transactions.

### 3.4 Leases

Every card carries a time-to-live:

- `lease_seconds` at write time (default `DEFAULT_LEASE_SECONDS` = 24 h; `0` = forever);
- `POST /api/lease/renew/` — sets a new TTL counted from *now* (`0` = forever);
- `DELETE /api/lease/cancel/?card_id=` — removes the card immediately;
- expiry — the reaper purges the card and notifies subscribers with an `expire` event. Cards held by a live transaction expire with the transaction, not on their own.

Leases are the garbage collector of the space: an Angel that crashes mid-work loses its claim when the lease runs out, and abandoned cards do not pile up.

### 3.5 Transactions

Lightweight, single-space transactions covering write and take:

- `POST /api/txn/begin/` → `txn_id`, with its own lease (default 60 s; auto-abort on expiry).
- **write under txn** — the card exists but is invisible to everyone outside the transaction until commit.
- **take under txn** — the card is *held*: invisible to everyone (including further takes in the same transaction) until commit finalizes the take or abort restores the card.
- `commit` — atomically: pending writes become visible (with `write` notifications), held takes are finalized (removed or moved to `claimed`).
- `abort` — pending writes are discarded; held cards return to their previous lane.

The canonical use is the **atomic hand-off**: take the input card *and* write the output card in one transaction, so a crash between the two steps cannot lose work (see the newsroom workflow in `tests/test_angels.py`).

### 3.6 Notifications

`notify()` is split in two:

1. `POST /api/notify/` with a template → returns `sub_id`. From then on every matching card event is queued for this subscription.
2. `GET /api/notify/{sub_id}` — a Server-Sent Events stream delivering the queue:

```
data: {"type": "write", "card": {...}}
```

Event types: `write` (card created, or became visible on commit), `take`, `expire`, `cancel`. Events are queued even while no client is connected, so an Angel that reconnects does not miss what happened in between. `DELETE /api/notify/{sub_id}` cancels the registration.

---

## 4. Security & configuration

Same model as the `events` project:

- **API key** — every endpoint requires the `API-KEY` header to equal the `API_KEY` from `.env` (403 otherwise). `.env` is git-ignored; recreate it on the VPS.
- **Run mode** — resolved automatically: Windows → `TESTING` (DEBUG logging to `C:\temp\python_eden.log`), Linux → `PROD` (INFO logging to `/home/angel/logs/python_eden.log`). Override with `MODE=TESTING|PROD`. Logs rotate at 1 MiB, 7 backups.
- **HTTPS** — terminate TLS in a reverse proxy in front of uvicorn on the VPS.

Server tunables (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | — | Shared secret for the `API-KEY` header |
| `DEFAULT_LEASE_SECONDS` | `86400` | Card TTL when the write does not specify one |
| `DEFAULT_TXN_LEASE_SECONDS` | `60` | Transaction lease (auto-abort) |
| `REAPER_INTERVAL_SECONDS` | `5` | Period of the expiry reaper |
| `MODE` | inferred from OS | `TESTING` or `PROD` |

Blocking `read`/`take` timeouts are capped at 60 s per request so calls cannot hang forever.

---

## 5. API reference

All endpoints require the `API-KEY` header. Interactive docs: `/docs` (Swagger UI) and `/redoc`.

| # | Method & path | JavaSpaces | Purpose |
|---|---|---|---|
| 1 | `GET /api/` | — | Health/info |
| 2 | `POST /api/write/` | `write()` | Create a card (`space`, `kind`, `fields`, `lease_seconds`, `agent`, `txn_id`) |
| 3 | `POST /api/read/` | `read()` / `readIfExists()` | One matching card, non-destructive; `timeout_seconds: 0` = if-exists, `>0` = block |
| 4 | `POST /api/read_all/` | scan | All matching cards (optional `?limit=`) |
| 5 | `POST /api/take/` | `take()` / `takeIfExists()` | Atomically claim (`mode:"claim"`) or remove (`mode:"remove"`) one match |
| 6 | `GET /api/board/?space=` | — | Kanban view: cards grouped by lane |
| 7 | `GET /api/card/{card_id}` | — | Direct lookup by ID |
| 8 | `POST /api/lease/renew/` | `Lease.renew()` | New TTL from now (`0` = forever) |
| 9 | `DELETE /api/lease/cancel/?card_id=` | `Lease.cancel()` | Remove the card immediately |
| 10 | `POST /api/txn/begin/` | `Transaction` | Start a transaction (`timeout_seconds` = its lease) |
| 11 | `POST /api/txn/commit/` | commit | Apply the transaction's writes and takes atomically |
| 12 | `POST /api/txn/abort/` | abort | Discard writes, restore taken cards |
| 13 | `POST /api/notify/` | `notify()` | Register a template subscription → `sub_id` |
| 14 | `GET /api/notify/{sub_id}` | event delivery | SSE stream of matching events |
| 15 | `DELETE /api/notify/{sub_id}` | cancel registration | Remove the subscription |

Error shape everywhere: `403 {"detail": "Invalid API Key"}`, `404 {"detail": "..."}` for no match / unknown card, transaction or subscription, `422` for invalid request bodies (pydantic).

### Typical exchange

```bash
# God writes a task card with a 1 h lease
curl -X POST "$EDEN/api/write/" -H "API-KEY: $KEY" -H "Content-Type: application/json" \
  -d '{"kind": "task", "fields": {"persona": "researcher"}, "lease_seconds": 3600, "agent": "god"}'

# An Angel blocks up to 30 s to claim the next task
curl -X POST "$EDEN/api/take/" -H "API-KEY: $KEY" -H "Content-Type: application/json" \
  -d '{"template": {"kind": "task"}, "mode": "claim", "agent": "gabriel", "timeout_seconds": 30}'
```

---

## 6. The agent layer

### 6.1 Angel and AngelSkill (`angel.py`)

An `Angel` is a guardian agent for one user persona. Its behaviour is an **agentskills.io skill exactly as Hermes-Agent implements it**: a directory with a required `SKILL.md` (YAML frontmatter + instructions) plus optional `scripts/`, `references/`, `assets/` directories.

```python
angel = Angel.from_skill_dir("assets/guardian-angel")
angel.name           # from frontmatter: "guardian-angel"
angel.persona        # "researcher" - the persona it guards
angel.version        # "1.0.0"; angel.tags -> ["eden", "kanban", "guardian"]
angel.instructions   # SKILL.md body without frontmatter (what Hermes-Agent executes)
```

### 6.2 skill-bundle/v1 (integrity-checked serialization)

For transport between agents a skill is packed into a self-describing envelope:

```json
{
  "schema": "skill-bundle/v1",
  "metadata": {"name": "deploy-k8s", "version": "1.2.0", "description": "...", "tags": ["kubernetes"]},
  "entrypoint": "SKILL.md",
  "files": [{"path": "SKILL.md", "sha256": "...", "content": "..."}]
}
```

Validation is strict **at parse time** — a bad bundle can never become an Angel:

- the `schema` id must be exactly `skill-bundle/v1`;
- the `entrypoint` must be among the files;
- paths must be unique and safe (no absolute paths, drive letters, or `..` traversal);
- every file's SHA-256 is recomputed and must match (tamper/corruption detection).

`Angel.to_bundle()` / `Angel.from_bundle()` round-trip an Angel; `Angel.to_card_fields()` embeds the bundle in a Kanban card (`kind="angel"`) and `Angel.from_card_fields()` verifies it on the way back — skills shipped through the space are integrity-checked end to end.

### 6.3 Skill identity and dependencies (`angels.py`)

A useful pattern is to separate the **identity** of a skill from its **serialization**:

```
Skill (identity)                        SkillBundle (serialization)
 ├── metadata                            skill-bundle/v1 JSON envelope
 ├── entrypoint (SKILL.md)               with SHA-256 per file,
 ├── resources                           produced by Skill.to_bundle()
 ├── helper scripts                      and verified again by
 └── dependencies                        Skill.from_bundle()
      ├── Kanban card assignments
      └── workflow assignments
```

A `SkillDependency` declares what the skill needs from the board:

```python
SkillDependency(kind="task",   action="consume",  # take matching cards
                fields={"persona": "researcher"},
                workflow="newsroom", step=1)
SkillDependency(kind="result", action="produce",  # write cards of this kind
                fields={"persona": "researcher"},
                workflow="newsroom", step=1)
```

Conversions: `Skill.from_angel()` / `skill.to_angel()` (identity ↔ execution) and `skill.to_bundle()` / `Skill.from_bundle()` (identity ↔ serialization; dependencies travel as `dependencies.json` inside the bundle).

### 6.4 Angels — the Host

`Angels` assembles Skill identities into a multi-agent system. Members hold **no references to each other**; one member's `produce` is another member's `consume`, and named workflows order the exchanges.

```python
host = Angels(name="newsroom-host")
host.enlist(guardian_skill)          # unique names enforced
host.enlist(scribe_skill)

host.consumers_of("result")          # ["scribe-angel"]
host.producers_of("result")          # ["guardian-angel"]
host.workflow("newsroom")            # ordered (step, angel, dependency)
host.validate_system()               # [] = sound wiring
host.deployment_cards()              # POST /api/write/ payloads (kind='angel' cards)
```

`validate_system()` flags: workflow steps not consecutive from 1; a step consuming a kind no earlier step produces; produced kinds nobody consumes (a workflow's *final* deliverable is exempt — the user picks it up).

### 6.5 The Angel coordination loop

From `assets/guardian-angel/SKILL.md` — the canonical loop every Angel follows:

1. **Watch** — register `notify()` for its consume templates and listen on the SSE stream.
2. **Claim** — `take(mode=claim)` a matching card; the space guarantees exclusivity.
3. **Work** — execute the task with Hermes-Agent tools.
4. **Report** — `write()` the produce card (use a transaction for an atomic take-and-write hand-off).
5. **Release** — cancel the claimed card's lease; renew leases on anything still in progress.

Rule of the Garden: **leave nothing behind** — every claimed card must end as a result card, a renewed lease, or a cancelled lease.

### 6.6 A worked workflow (from `tests/test_angels.py`)

```
newsroom workflow
  God ── write ──► [task] ── take+write (one txn) ──► [result] ── take ──► [digest] ── take ──► user
                    guardian-angel (step 1)            scribe-angel (step 2)
```

The guardian takes the task and writes the result in **one transaction** (atomic hand-off); the scribe is woken by SSE, claims the result, writes the digest; the user takes the digest off the board. If the scribe fails, aborting its transaction restores the result card untouched.

---

## 7. Testing

Three self-contained suites (plain scripts, no pytest needed):

| Suite | Checks | Covers |
|---|---|---|
| `tests/test_api.py` | 32 | Security (API key), write/read/take (both modes), board, leases incl. expiry, transactions incl. isolation, notify queues — via FastAPI `TestClient` |
| `tests/test_angel.py` | 42 | Angel from `assets/`, skill-bundle robustness (tamper/schema/entrypoint/traversal/duplicates rejected), full single-Angel lifecycle |
| `tests/test_angels.py` | 41 | Skill identity/serialization round-trips, Host wiring + `validate_system`, two-Angel newsroom workflow |

`test_angel.py` and `test_angels.py` run against a **real uvicorn server** in a background thread, because starlette's `TestClient` buffers responses and cannot stream an infinite SSE response. Both suites end with two structural checks:

- **nothing left behind** — the space holds zero cards, transactions, and subscriptions;
- **entry-point coverage** — the set of exercised `(method, path)` pairs is compared against `app.routes`, so adding an endpoint without testing it fails the suite.

```bash
python tests/test_api.py && python tests/test_angel.py && python tests/test_angels.py
```

---

## 8. Deployment on the VPS

```bash
git clone https://github.com/kiodia/eden.git && cd eden
python -m venv env && source env/bin/activate
pip install -r requirements.txt
echo 'API_KEY = "<a strong secret>"' > .env
uvicorn main:app --host 127.0.0.1 --port 8000     # PROD mode auto-detected on Linux
```

- Put a reverse proxy (nginx/caddy) with HTTPS in front; for SSE make sure proxy buffering is off for `/api/notify/` (`proxy_buffering off;` in nginx).
- **One worker only** — state is in-memory and the atomicity lock is per-process.
- Logs go to `/home/angel/logs/python_eden.log` (rotating). Create the user/dir or override via `MODE=TESTING` for ad-hoc runs.
- A restart empties the Garden (cards, transactions, subscriptions). Angels should treat the space as volatile and re-deploy/re-subscribe on reconnect.

## 9. Limitations & roadmap

Known limitations (by design, for now):

- in-memory, single-process, single-node — a restart clears the space;
- exact-equality template matching only (no ranges, regex, or type hierarchies);
- transactions cover write/take only (no nested or distributed transactions);
- notifications have no lease of their own and no delivery acknowledgement.

Planned enhancements: persistent storage backend, richer matching, notify leases, multiple named boards with per-board settings, and direct Hermes-Agent Kanban backend integration.
