"""
Test suite for the Eden API (JavaSpaces operations on Kanban cards).
Uses the FastAPI TestClient, so no server needs to be running.

    python test_api.py
"""
from fastapi.testclient import TestClient
import os
import time

os.environ.setdefault("MODE", "TESTING")

from main import app, API_KEY  # noqa: E402

HEADERS = {"API-KEY": API_KEY}
BAD_HEADERS = {"API-KEY": "wrong_key"}

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def run_tests():
    with TestClient(app) as client:

        print("\n[security]")
        r = client.get("/api/", headers=BAD_HEADERS)
        check("wrong API key rejected (403)", r.status_code == 403, r.text)
        r = client.get("/api/")
        check("missing API key rejected (403)", r.status_code == 403, r.text)
        r = client.get("/api/", headers=HEADERS)
        check("valid API key accepted", r.status_code == 200, r.text)

        print("\n[write / read]")
        r = client.post("/api/write/", headers=HEADERS, json={
            "kind": "task",
            "fields": {"persona": "researcher", "action": "summarize"},
            "agent": "gabriel"
        })
        check("write card", r.status_code == 200, r.text)
        card_id = r.json()["card"]["id"]

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "task", "fields": {"persona": "researcher"}}
        })
        check("read matching template", r.status_code == 200 and r.json()["card"]["id"] == card_id, r.text)

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "task", "fields": {"persona": "nobody"}}
        })
        check("read non-matching template -> 404", r.status_code == 404, r.text)

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "task"}
        })
        check("read is non-destructive", r.status_code == 200, r.text)

        r = client.get(f"/api/card/{card_id}", headers=HEADERS)
        check("get card by id", r.status_code == 200, r.text)

        print("\n[take]")
        r = client.post("/api/take/", headers=HEADERS, json={
            "template": {"kind": "task"},
            "mode": "claim",
            "agent": "michael"
        })
        check("take mode=claim", r.status_code == 200 and r.json()["card"]["claimed_by"] == "michael", r.text)

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "task"}
        })
        check("claimed card no longer matchable", r.status_code == 404, r.text)

        r = client.get("/api/board/", headers=HEADERS)
        lanes = r.json()["lanes"]
        check("board shows card in claimed lane",
              any(c["id"] == card_id for c in lanes["claimed"]), r.text)

        r = client.post("/api/write/", headers=HEADERS, json={
            "kind": "task", "fields": {"n": 1}
        })
        take_id = r.json()["card"]["id"]
        r = client.post("/api/take/", headers=HEADERS, json={
            "template": {"kind": "task", "fields": {"n": 1}},
            "mode": "remove"
        })
        check("take mode=remove", r.status_code == 200, r.text)
        r = client.get(f"/api/card/{take_id}", headers=HEADERS)
        check("removed card is gone", r.status_code == 404, r.text)

        print("\n[leases]")
        r = client.post("/api/write/", headers=HEADERS, json={
            "kind": "ephemeral", "fields": {}, "lease_seconds": 3600
        })
        lease_id = r.json()["card"]["id"]
        r = client.post("/api/lease/renew/", headers=HEADERS,
                        json={"card_id": lease_id, "lease_seconds": 7200})
        check("renew lease", r.status_code == 200, r.text)
        r = client.delete(f"/api/lease/cancel/?card_id={lease_id}", headers=HEADERS)
        check("cancel lease removes card", r.status_code == 200, r.text)
        r = client.get(f"/api/card/{lease_id}", headers=HEADERS)
        check("cancelled card is gone", r.status_code == 404, r.text)

        print("\n[transactions]")
        r = client.post("/api/txn/begin/", headers=HEADERS, json={})
        txn_id = r.json()["txn_id"]
        check("txn begin", r.status_code == 200, r.text)

        r = client.post("/api/write/", headers=HEADERS, json={
            "kind": "secret", "fields": {"x": 1}, "txn_id": txn_id
        })
        check("write under txn", r.status_code == 200, r.text)

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "secret"}
        })
        check("txn write invisible outside txn", r.status_code == 404, r.text)

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "secret"}, "txn_id": txn_id
        })
        check("txn write visible inside txn", r.status_code == 200, r.text)

        r = client.post("/api/txn/commit/", headers=HEADERS, json={"txn_id": txn_id})
        check("txn commit", r.status_code == 200, r.text)

        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "secret"}
        })
        check("committed write visible to all", r.status_code == 200, r.text)

        # abort: a take under txn is restored
        r = client.post("/api/txn/begin/", headers=HEADERS, json={})
        txn_id = r.json()["txn_id"]
        r = client.post("/api/take/", headers=HEADERS, json={
            "template": {"kind": "secret"}, "mode": "remove", "txn_id": txn_id
        })
        check("take under txn", r.status_code == 200, r.text)
        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "secret"}
        })
        check("held card invisible during txn", r.status_code == 404, r.text)
        r = client.post("/api/txn/abort/", headers=HEADERS, json={"txn_id": txn_id})
        check("txn abort", r.status_code == 200 and r.json()["takes_restored"] == 1, r.text)
        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "secret"}
        })
        check("aborted take restored the card", r.status_code == 200, r.text)

        print("\n[lease expiry]")
        r = client.post("/api/write/", headers=HEADERS, json={
            "kind": "flash", "fields": {}, "lease_seconds": 1
        })
        check("write short-lived card", r.status_code == 200, r.text)
        time.sleep(1.2)
        # the reaper runs every few seconds; readIfExists after manual purge window
        import asyncio
        from main import garden
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(garden.purge_expired())
        r = client.post("/api/read/", headers=HEADERS, json={
            "template": {"kind": "flash"}
        })
        check("expired card purged", r.status_code == 404, r.text)

        print("\n[notify]")
        r = client.post("/api/notify/", headers=HEADERS, json={
            "template": {"kind": "signal"}
        })
        sub_id = r.json()["sub_id"]
        check("register subscription", r.status_code == 200, r.text)

        client.post("/api/write/", headers=HEADERS, json={
            "kind": "signal", "fields": {"msg": "hello"}
        })
        client.post("/api/write/", headers=HEADERS, json={
            "kind": "other", "fields": {"msg": "noise"}
        })
        from main import garden as g
        sub = g.subscriptions[sub_id]
        check("matching write queued for subscriber", sub.queue.qsize() == 1,
              f"qsize={sub.queue.qsize()}")

        r = client.delete(f"/api/notify/{sub_id}", headers=HEADERS)
        check("remove subscription", r.status_code == 200, r.text)

        print("\n[read_all]")
        r = client.post("/api/read_all/", headers=HEADERS, json={"template": {}})
        check("read_all returns cards", r.status_code == 200 and r.json()["count"] >= 1, r.text)

    print(f"\n{'=' * 40}\nPassed: {passed}  Failed: {failed}")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    raise SystemExit(0 if ok else 1)
