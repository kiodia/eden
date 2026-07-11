# FastAPI for Eden - a JavaSpaces-style tuple space on top of Hermes-Agent Kanban cards
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from config import init_config, flags
from dotenv import load_dotenv
from space import (
    TupleSpace, Template, LANE_OPEN, LANE_CLAIMED, TAKE_REMOVE, TAKE_CLAIM
)
import asyncio
import json
import os
import logging
log = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()
init_config()
app = FastAPI(title="Eden API", version="0.0.1", debug=flags.get("debug", False))
log.info(f"Eden API starting in {flags.get('mode')} mode")

# Read API key from environment variable
API_KEY = os.getenv("API_KEY")
log.debug(f"API_KEY loaded: {'yes' if API_KEY else 'no'}")

# Space configuration
DEFAULT_LEASE_SECONDS = int(os.getenv("DEFAULT_LEASE_SECONDS", 24 * 3600))   # cards live 24h by default
DEFAULT_TXN_LEASE_SECONDS = int(os.getenv("DEFAULT_TXN_LEASE_SECONDS", 60))  # txns auto-abort after 60s
REAPER_INTERVAL_SECONDS = int(os.getenv("REAPER_INTERVAL_SECONDS", 5))
MAX_BLOCKING_TIMEOUT_SECONDS = 60  # cap for blocking read/take so requests cannot hang forever

# The Garden: the shared tuple space holding all Kanban cards
garden = TupleSpace(DEFAULT_LEASE_SECONDS, DEFAULT_TXN_LEASE_SECONDS)


def check_api_key(api_key: Optional[str]):
    """Same security model as the events API: every endpoint requires the
    API-KEY header to match the key from .env."""
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")


# ---------------------------------------------------------------------- #
# Request models
# ---------------------------------------------------------------------- #

class WriteRequest(BaseModel):
    space: str = Field(default="eden", description="Board to write the card on")
    kind: str = Field(..., description="Entry type (JavaSpaces: the Entry class)")
    fields: Dict[str, Any] = Field(default_factory=dict, description="Free-form tuple fields")
    lease_seconds: Optional[int] = Field(None, ge=0, description="Card TTL in seconds (0 = forever, default from server)")
    agent: Optional[str] = Field(None, description="Name of the writing agent")
    txn_id: Optional[str] = Field(None, description="Transaction to write under (invisible until commit)")

    class Config:
        json_schema_extra = {
            "example": {
                "space": "eden",
                "kind": "task",
                "fields": {"persona": "researcher", "action": "summarize", "url": "https://arxiv.org/abs/2511.00402"},
                "lease_seconds": 3600,
                "agent": "gabriel"
            }
        }


class ReadRequest(BaseModel):
    template: Template = Field(default_factory=Template, description="Template: omitted values are wildcards")
    txn_id: Optional[str] = Field(None, description="Read inside this transaction (sees its pending writes)")
    timeout_seconds: float = Field(0, ge=0, le=MAX_BLOCKING_TIMEOUT_SECONDS,
                                   description="0 = readIfExists, >0 = block until match or timeout")

    class Config:
        json_schema_extra = {
            "example": {
                "template": {"space": "eden", "kind": "task", "fields": {"persona": "researcher"}},
                "timeout_seconds": 0
            }
        }


class TakeRequest(BaseModel):
    template: Template = Field(default_factory=Template, description="Template: omitted values are wildcards")
    mode: str = Field(default=TAKE_CLAIM, pattern=f"^({TAKE_CLAIM}|{TAKE_REMOVE})$",
                      description="'claim' moves the card to the claimed lane, 'remove' deletes it (classic take)")
    agent: Optional[str] = Field(None, description="Name of the taking agent")
    txn_id: Optional[str] = Field(None, description="Take inside this transaction (held until commit/abort)")
    timeout_seconds: float = Field(0, ge=0, le=MAX_BLOCKING_TIMEOUT_SECONDS,
                                   description="0 = takeIfExists, >0 = block until match or timeout")

    class Config:
        json_schema_extra = {
            "example": {
                "template": {"space": "eden", "kind": "task"},
                "mode": "claim",
                "agent": "gabriel",
                "timeout_seconds": 10
            }
        }


class LeaseRenewRequest(BaseModel):
    card_id: int = Field(..., description="Card whose lease to renew")
    lease_seconds: int = Field(..., ge=0, description="New TTL in seconds from now (0 = forever)")


class TxnBeginRequest(BaseModel):
    timeout_seconds: Optional[int] = Field(None, ge=1, description="Txn lease; auto-abort after this many seconds")


class TxnRequest(BaseModel):
    txn_id: str = Field(..., description="Transaction ID from /api/txn/begin/")


class NotifyRequest(BaseModel):
    template: Template = Field(default_factory=Template, description="Events are delivered for cards matching this template")


# ---------------------------------------------------------------------- #
# Root
# ---------------------------------------------------------------------- #

@app.get("/api/")
async def api_root(api_key: str = Header(None, alias="API-KEY")):
    check_api_key(api_key)
    return {"message": app.title, "version": app.version}


# ---------------------------------------------------------------------- #
# write / read / take
# ---------------------------------------------------------------------- #

@app.post("/api/write/")
async def write_card(request: WriteRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    JavaSpaces write(): create a Kanban card in the space.

    - **space**: board name (default "eden")
    - **kind**: entry type
    - **fields**: free-form tuple fields
    - **lease_seconds**: TTL; the card is purged when the lease expires
    - **txn_id**: optional - card stays invisible to others until the txn commits
    """
    check_api_key(api_key)
    try:
        card = await garden.write(request.space, request.kind, request.fields,
                                  request.lease_seconds, request.agent, request.txn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Transaction '{request.txn_id}' not found")
    return {
        "status": "success",
        "message": "Card written successfully",
        "card": card.model_dump(),
        "total_cards": len(garden.cards)
    }


@app.post("/api/read/")
async def read_card(request: ReadRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    JavaSpaces read(): return one matching card WITHOUT removing it.

    - **template**: kind/fields to match; omitted values are wildcards
    - **timeout_seconds**: 0 = readIfExists, >0 = block until a match appears
    """
    check_api_key(api_key)
    card = await garden.read(request.template, request.txn_id, request.timeout_seconds)
    if card is None:
        raise HTTPException(status_code=404, detail="No card matches the template")
    return {
        "status": "success",
        "card": card.model_dump()
    }


@app.post("/api/read_all/")
async def read_all_cards(request: ReadRequest, api_key: str = Header(None, alias="API-KEY"),
                         limit: Optional[int] = Query(None, ge=1, description="Limit number of cards returned")):
    """
    Bulk read: return ALL cards matching the template (non-destructive scan).
    """
    check_api_key(api_key)
    cards = await garden.read_all(request.template, request.txn_id, limit)
    return {
        "status": "success",
        "count": len(cards),
        "cards": [c.model_dump() for c in cards]
    }


@app.post("/api/take/")
async def take_card(request: TakeRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    JavaSpaces take(): atomically claim or remove one matching card.
    Two agents can never take the same card.

    - **mode**: 'claim' moves the card to the claimed lane (Kanban style),
      'remove' deletes it from the space (classic JavaSpaces)
    - **timeout_seconds**: 0 = takeIfExists, >0 = block until a match appears
    - **txn_id**: optional - the card is held until the txn commits or aborts
    """
    check_api_key(api_key)
    try:
        card = await garden.take(request.template, request.mode, request.agent,
                                 request.txn_id, request.timeout_seconds)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Transaction '{request.txn_id}' not found")
    if card is None:
        raise HTTPException(status_code=404, detail="No card matches the template")
    return {
        "status": "success",
        "message": f"Card taken ({request.mode})",
        "card": card.model_dump(),
        "total_cards": len(garden.cards)
    }


# ---------------------------------------------------------------------- #
# board inspection
# ---------------------------------------------------------------------- #

@app.get("/api/board/")
async def get_board(api_key: str = Header(None, alias="API-KEY"),
                    space: str = Query("eden", description="Board name")):
    """
    Kanban view of a space: all cards grouped by lane (open / claimed).
    Cards held by uncommitted transactions are not shown.
    """
    check_api_key(api_key)
    lanes = {LANE_OPEN: [], LANE_CLAIMED: []}
    for card in sorted(garden.cards.values(), key=lambda c: c.id):
        if card.space == space and card.lane in lanes and card.txn_id is None:
            lanes[card.lane].append(card.model_dump())
    return {
        "status": "success",
        "space": space,
        "lanes": lanes,
        "count": sum(len(v) for v in lanes.values())
    }


@app.get("/api/card/{card_id}")
async def get_card(card_id: int, api_key: str = Header(None, alias="API-KEY")):
    """
    Read a specific card by its ID (direct lookup, no template).
    """
    check_api_key(api_key)
    card = garden.cards.get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Card with ID {card_id} not found")
    return {
        "status": "success",
        "card": card.model_dump()
    }


# ---------------------------------------------------------------------- #
# leases
# ---------------------------------------------------------------------- #

@app.post("/api/lease/renew/")
async def renew_lease(request: LeaseRenewRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    JavaSpaces Lease.renew(): extend a card's TTL from now.

    - **lease_seconds**: new TTL in seconds (0 = the card never expires)
    """
    check_api_key(api_key)
    try:
        card = await garden.renew_lease(request.card_id, request.lease_seconds)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Card with ID {request.card_id} not found")
    return {
        "status": "success",
        "message": "Lease renewed",
        "card": card.model_dump()
    }


@app.delete("/api/lease/cancel/")
async def cancel_lease(api_key: str = Header(None, alias="API-KEY"),
                       card_id: int = Query(..., description="Card whose lease to cancel")):
    """
    JavaSpaces Lease.cancel(): the card is removed from the space immediately.
    """
    check_api_key(api_key)
    try:
        card = await garden.cancel_lease(card_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Card with ID {card_id} not found")
    return {
        "status": "success",
        "message": "Lease cancelled, card removed",
        "card": card.model_dump(),
        "total_cards": len(garden.cards)
    }


# ---------------------------------------------------------------------- #
# transactions
# ---------------------------------------------------------------------- #

@app.post("/api/txn/begin/")
async def txn_begin(request: TxnBeginRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    Start a transaction. Writes under it stay invisible and takes hold their
    card until commit; the txn auto-aborts when its lease expires.
    """
    check_api_key(api_key)
    txn = await garden.txn_begin(request.timeout_seconds)
    return {
        "status": "success",
        "txn_id": txn.id,
        "expires_at": txn.expires_at.isoformat().replace('+00:00', 'Z')
    }


@app.post("/api/txn/commit/")
async def txn_commit(request: TxnRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    Commit: pending writes become visible, taken cards are finalized
    (removed or moved to the claimed lane) - all atomically.
    """
    check_api_key(api_key)
    try:
        result = await garden.txn_commit(request.txn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Transaction '{request.txn_id}' not found")
    return {
        "status": "success",
        "message": f"Transaction '{request.txn_id}' committed",
        **result
    }


@app.post("/api/txn/abort/")
async def txn_abort(request: TxnRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    Abort: pending writes are discarded and taken cards are restored.
    """
    check_api_key(api_key)
    try:
        result = await garden.txn_abort(request.txn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Transaction '{request.txn_id}' not found")
    return {
        "status": "success",
        "message": f"Transaction '{request.txn_id}' aborted",
        **result
    }


# ---------------------------------------------------------------------- #
# notify (subscriptions + SSE)
# ---------------------------------------------------------------------- #

@app.post("/api/notify/")
async def register_notify(request: NotifyRequest, api_key: str = Header(None, alias="API-KEY")):
    """
    JavaSpaces notify(): register interest in cards matching a template.
    Returns a subscription ID; stream the events from GET /api/notify/{sub_id}.
    Delivered event types: write, take, expire, cancel.
    """
    check_api_key(api_key)
    sub = garden.subscribe(request.template)
    return {
        "status": "success",
        "sub_id": sub.id,
        "stream_url": f"/api/notify/{sub.id}"
    }


@app.get("/api/notify/{sub_id}")
async def notify_stream(sub_id: str, api_key: str = Header(None, alias="API-KEY")):
    """
    Server-Sent Events stream for a registered subscription.
    """
    check_api_key(api_key)
    sub = garden.subscriptions.get(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"Subscription '{sub_id}' not found")

    async def event_generator():
        log.info(f"SSE client connected on subscription {sub_id}")
        try:
            while True:
                notification = await sub.queue.get()
                yield f"data: {json.dumps(notification)}\n\n"
                log.info(f"SSE notification sent: sub={sub_id}, type={notification['type']}, "
                         f"card_id={notification['card']['id']}")
        except asyncio.CancelledError:
            log.info(f"SSE client disconnected from subscription {sub_id}")
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.delete("/api/notify/{sub_id}")
async def remove_notify(sub_id: str, api_key: str = Header(None, alias="API-KEY")):
    """
    Cancel a notify() registration.
    """
    check_api_key(api_key)
    if not garden.unsubscribe(sub_id):
        raise HTTPException(status_code=404, detail=f"Subscription '{sub_id}' not found")
    return {
        "status": "success",
        "message": f"Subscription '{sub_id}' removed"
    }


# ---------------------------------------------------------------------- #
# background reaper: expired leases & transactions
# ---------------------------------------------------------------------- #

async def reaper_loop():
    while True:
        try:
            await garden.purge_expired()
        except Exception:
            log.exception("Reaper iteration failed")
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(reaper_loop())
    log.info(f"Reaper started (interval {REAPER_INTERVAL_SECONDS}s, "
             f"default card lease {DEFAULT_LEASE_SECONDS}s, txn lease {DEFAULT_TXN_LEASE_SECONDS}s)")
