'''
Eden tuple space - a JavaSpaces-inspired shared space built on Kanban cards.

Conceptual mapping (JavaSpaces -> Hermes-Agent Kanban):
    Space          -> shared Kanban board (a named collection of cards)
    Entry          -> Kanban card (kind + free-form fields)
    write()        -> create a card
    read(template) -> query matching cards without removing them
    take(template) -> atomically claim (move lane) or remove a matching card
    notify()       -> subscribe to events on matching cards
    Lease          -> card time-to-live (TTL), renewable and cancellable
    Transactions   -> atomic groups of write/take, commit or abort

This is intentionally NOT the full JavaSpaces specification: matching is
exact-equality on a subset of fields (null/omitted = wildcard), there is a
single in-process space, and transactions only cover write/take.

@author: vankomme
'''

from pydantic import BaseModel, Field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import asyncio
import logging
import uuid

log = logging.getLogger(__name__)

# Kanban lanes for a card
LANE_OPEN = "open"          # available for read/take (JavaSpaces: entry in space)
LANE_CLAIMED = "claimed"    # taken with mode=claim: stays on the board, owned by an agent

# take() modes
TAKE_REMOVE = "remove"      # classic JavaSpaces take: entry leaves the space
TAKE_CLAIM = "claim"        # Kanban take: card moves to the 'claimed' lane


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.isoformat().replace('+00:00', 'Z')


class Card(BaseModel):
    """A Kanban card, playing the role of a JavaSpaces Entry."""
    id: int = Field(..., description="Unique card ID")
    space: str = Field(..., description="Board name this card lives on")
    kind: str = Field(..., description="Entry type (JavaSpaces: the Entry class)")
    fields: Dict[str, Any] = Field(default_factory=dict, description="Free-form tuple fields")
    lane: str = Field(default=LANE_OPEN, description="Kanban lane: open | claimed")
    created_by: Optional[str] = Field(None, description="Agent that wrote the card")
    claimed_by: Optional[str] = Field(None, description="Agent that claimed the card (take mode=claim)")
    created_at: Optional[str] = Field(None, description="ISO timestamp of creation")
    lease_expires_at: Optional[str] = Field(None, description="ISO timestamp when the lease expires (None = forever)")
    txn_id: Optional[str] = Field(None, description="Transaction currently holding this card, if any")


class Template(BaseModel):
    """A JavaSpaces template: omitted/None values are wildcards,
    provided values must match the card exactly."""
    space: str = Field(default="eden", description="Board to match on")
    kind: Optional[str] = Field(None, description="Entry type to match (None = any)")
    fields: Dict[str, Any] = Field(default_factory=dict, description="Fields that must match exactly (subset)")

    def matches(self, card: Card) -> bool:
        if card.space != self.space:
            return False
        if self.kind is not None and card.kind != self.kind:
            return False
        for key, value in self.fields.items():
            if key not in card.fields or card.fields[key] != value:
                return False
        return True


class Subscription:
    """A notify() registration: a template plus a queue of matching events."""
    def __init__(self, sub_id: str, template: Template):
        self.id = sub_id
        self.template = template
        self.queue: asyncio.Queue = asyncio.Queue()
        self.created_at = utc_now()


class Transaction:
    """A lightweight transaction: groups writes and takes, applied atomically
    on commit, rolled back on abort, auto-aborted when its lease expires."""
    def __init__(self, txn_id: str, expires_at: datetime):
        self.id = txn_id
        self.expires_at = expires_at
        self.written: List[int] = []                 # card ids written under this txn
        self.taken: Dict[int, dict] = {}             # card id -> {mode, agent, prev_lane}


class TupleSpace:
    """The Garden: a shared Kanban board with JavaSpaces semantics."""

    def __init__(self, default_lease_seconds: int, txn_lease_seconds: int):
        self.default_lease_seconds = default_lease_seconds
        self.txn_lease_seconds = txn_lease_seconds
        self.cards: Dict[int, Card] = {}
        self.card_id_counter: int = 0
        self.transactions: Dict[str, Transaction] = {}
        self.subscriptions: Dict[str, Subscription] = {}
        # Condition protects all mutations and wakes blocked read()/take()
        self.condition = asyncio.Condition()

    # ------------------------------------------------------------------ #
    # visibility & matching
    # ------------------------------------------------------------------ #

    def _visible(self, card: Card, txn_id: Optional[str]) -> bool:
        """A card is matchable when it sits in the open lane and is not held
        by another transaction. Cards written under a txn are only visible
        inside that txn until commit."""
        if card.lane != LANE_OPEN:
            return False
        if card.txn_id is not None and card.txn_id != txn_id:
            return False
        return True

    def _find_matches(self, template: Template, txn_id: Optional[str]) -> List[Card]:
        return [
            c for c in self.cards.values()
            if self._visible(c, txn_id) and template.matches(c)
        ]

    # ------------------------------------------------------------------ #
    # notifications
    # ------------------------------------------------------------------ #

    def _notify(self, event_type: str, card: Card):
        """Push an event to every subscription whose template matches."""
        for sub in self.subscriptions.values():
            if sub.template.matches(card):
                sub.queue.put_nowait({
                    "type": event_type,
                    "card": card.model_dump()
                })

    # ------------------------------------------------------------------ #
    # write / read / take
    # ------------------------------------------------------------------ #

    async def write(self, space: str, kind: str, fields: Dict[str, Any],
                    lease_seconds: Optional[int], agent: Optional[str],
                    txn_id: Optional[str]) -> Card:
        async with self.condition:
            if txn_id is not None:
                self._get_txn(txn_id)  # validates the txn exists
            self.card_id_counter += 1
            lease = lease_seconds if lease_seconds is not None else self.default_lease_seconds
            expires = utc_now() + timedelta(seconds=lease) if lease > 0 else None
            card = Card(
                id=self.card_id_counter,
                space=space,
                kind=kind,
                fields=fields,
                lane=LANE_OPEN,
                created_by=agent,
                created_at=iso(utc_now()),
                lease_expires_at=iso(expires),
                txn_id=txn_id,
            )
            self.cards[card.id] = card
            if txn_id is not None:
                self.transactions[txn_id].written.append(card.id)
                log.info(f"Card written under txn {txn_id}: ID={card.id}, kind={kind}")
            else:
                log.info(f"Card written: ID={card.id}, space={space}, kind={kind}")
                self._notify("write", card)
                self.condition.notify_all()
            return card

    async def read(self, template: Template, txn_id: Optional[str],
                   timeout_seconds: float) -> Optional[Card]:
        """Non-destructive read. timeout 0 = readIfExists; otherwise block
        until a matching card appears or the timeout elapses."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        async with self.condition:
            while True:
                matches = self._find_matches(template, txn_id)
                if matches:
                    return matches[0]
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None

    async def read_all(self, template: Template, txn_id: Optional[str],
                       limit: Optional[int]) -> List[Card]:
        """Bulk non-destructive read of every matching card (scan)."""
        async with self.condition:
            matches = self._find_matches(template, txn_id)
            matches.sort(key=lambda c: c.id)
            if limit:
                matches = matches[:limit]
            return matches

    async def take(self, template: Template, mode: str, agent: Optional[str],
                   txn_id: Optional[str], timeout_seconds: float) -> Optional[Card]:
        """Atomically claim or remove one matching card. timeout 0 =
        takeIfExists; otherwise block until a match appears."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        async with self.condition:
            while True:
                matches = self._find_matches(template, txn_id)
                if matches:
                    card = matches[0]
                    if txn_id is not None:
                        txn = self._get_txn(txn_id)
                        # Hold the card invisibly until commit/abort
                        txn.taken[card.id] = {
                            "mode": mode, "agent": agent, "prev_lane": card.lane
                        }
                        card.lane = "held"
                        card.txn_id = txn_id
                        log.info(f"Card taken under txn {txn_id}: ID={card.id}, mode={mode}")
                    else:
                        self._finalize_take(card, mode, agent)
                    return card
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None

    def _finalize_take(self, card: Card, mode: str, agent: Optional[str]):
        if mode == TAKE_CLAIM:
            card.lane = LANE_CLAIMED
            card.claimed_by = agent
            card.txn_id = None
            log.info(f"Card claimed: ID={card.id} by agent={agent}")
        else:
            del self.cards[card.id]
            log.info(f"Card taken (removed): ID={card.id} by agent={agent}")
        self._notify("take", card)

    # ------------------------------------------------------------------ #
    # leases
    # ------------------------------------------------------------------ #

    async def renew_lease(self, card_id: int, lease_seconds: int) -> Card:
        async with self.condition:
            card = self.cards.get(card_id)
            if card is None:
                raise KeyError(card_id)
            expires = utc_now() + timedelta(seconds=lease_seconds) if lease_seconds > 0 else None
            card.lease_expires_at = iso(expires)
            log.info(f"Lease renewed: ID={card_id}, +{lease_seconds}s")
            return card

    async def cancel_lease(self, card_id: int) -> Card:
        """Cancelling a lease removes the card from the space immediately."""
        async with self.condition:
            card = self.cards.get(card_id)
            if card is None:
                raise KeyError(card_id)
            del self.cards[card_id]
            self._notify("cancel", card)
            log.info(f"Lease cancelled, card removed: ID={card_id}")
            return card

    async def purge_expired(self):
        """Remove cards whose lease expired and abort expired transactions.
        Called periodically by the reaper background task."""
        now = utc_now()
        now_str = iso(now)
        async with self.condition:
            for txn_id in [t.id for t in self.transactions.values() if t.expires_at <= now]:
                self._rollback(self.transactions.pop(txn_id))
                log.info(f"Transaction auto-aborted (lease expired): {txn_id}")
            expired = [
                c for c in self.cards.values()
                if c.lease_expires_at is not None and c.lease_expires_at <= now_str
                and c.txn_id is None  # cards held by a live txn expire with the txn
            ]
            for card in expired:
                del self.cards[card.id]
                self._notify("expire", card)
                log.info(f"Card expired: ID={card.id}, kind={card.kind}")
            if expired:
                self.condition.notify_all()

    # ------------------------------------------------------------------ #
    # transactions
    # ------------------------------------------------------------------ #

    def _get_txn(self, txn_id: str) -> Transaction:
        txn = self.transactions.get(txn_id)
        if txn is None:
            raise KeyError(txn_id)
        return txn

    async def txn_begin(self, timeout_seconds: Optional[int]) -> Transaction:
        async with self.condition:
            txn_id = uuid.uuid4().hex[:12]
            lease = timeout_seconds if timeout_seconds is not None else self.txn_lease_seconds
            txn = Transaction(txn_id, utc_now() + timedelta(seconds=lease))
            self.transactions[txn_id] = txn
            log.info(f"Transaction started: {txn_id} (lease {lease}s)")
            return txn

    async def txn_commit(self, txn_id: str) -> dict:
        async with self.condition:
            txn = self._get_txn(txn_id)
            del self.transactions[txn_id]
            # Writes become visible to everyone
            for card_id in txn.written:
                card = self.cards.get(card_id)
                if card is not None:
                    card.txn_id = None
                    self._notify("write", card)
            # Takes are finalized (removed or moved to the claimed lane)
            for card_id, info in txn.taken.items():
                card = self.cards.get(card_id)
                if card is not None:
                    card.lane = LANE_OPEN
                    self._finalize_take(card, info["mode"], info["agent"])
            self.condition.notify_all()
            log.info(f"Transaction committed: {txn_id} "
                     f"({len(txn.written)} writes, {len(txn.taken)} takes)")
            return {"writes": len(txn.written), "takes": len(txn.taken)}

    async def txn_abort(self, txn_id: str) -> dict:
        async with self.condition:
            txn = self._get_txn(txn_id)
            del self.transactions[txn_id]
            result = self._rollback(txn)
            self.condition.notify_all()
            log.info(f"Transaction aborted: {txn_id}")
            return result

    def _rollback(self, txn: Transaction) -> dict:
        # Writes under the txn never happened
        for card_id in txn.written:
            self.cards.pop(card_id, None)
        # Taken cards go back to their previous lane, visible again
        for card_id, info in txn.taken.items():
            card = self.cards.get(card_id)
            if card is not None:
                card.lane = info["prev_lane"]
                card.txn_id = None
        return {"writes_discarded": len(txn.written), "takes_restored": len(txn.taken)}

    # ------------------------------------------------------------------ #
    # notify subscriptions
    # ------------------------------------------------------------------ #

    def subscribe(self, template: Template) -> Subscription:
        sub = Subscription(uuid.uuid4().hex[:12], template)
        self.subscriptions[sub.id] = sub
        log.info(f"Subscription registered: {sub.id} (kind={template.kind}, space={template.space})")
        return sub

    def unsubscribe(self, sub_id: str) -> bool:
        if sub_id in self.subscriptions:
            del self.subscriptions[sub_id]
            log.info(f"Subscription removed: {sub_id}")
            return True
        return False
