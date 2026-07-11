"""
End-to-end test for Eden: loads the first Angel from assets/guardian-angel,
creates Kanban entries, exercises EVERY FastAPI entry point (verified
automatically against app.routes), and checks that nothing is left behind
in the space (no cards, no transactions, no subscriptions).

Runs against a real uvicorn server (background thread) so the SSE stream is
tested for real - starlette's TestClient cannot stream an infinite response.

    python tests/test_angel.py
"""
import httpx
import json
import os
import sys
import threading
import time

# Make the project root importable when running from the tests folder
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("MODE", "TESTING")

import uvicorn  # noqa: E402
from main import app, API_KEY, garden  # noqa: E402
from angel import Angel  # noqa: E402

HOST, PORT = "127.0.0.1", 8901
HEADERS = {"API-KEY": API_KEY}

passed = 0
failed = 0
exercised = set()  # (METHOD, path template) of every endpoint we called


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def start_server():
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start within 10s")
        time.sleep(0.05)
    return server, thread


def run_tests():
    server, thread = start_server()
    client = httpx.Client(base_url=f"http://{HOST}:{PORT}", headers=HEADERS,
                          timeout=httpx.Timeout(10.0))
    try:
        def call(method, template, url=None, **kwargs):
            exercised.add((method.upper(), template))
            return client.request(method, url or template, **kwargs)

        print("\n[angel from assets]")
        angel = Angel.from_skill_dir(os.path.join(PROJECT_ROOT, "assets", "guardian-angel"))
        check("angel loaded from SKILL.md", angel.name == "guardian-angel", angel.name)
        check("persona from frontmatter", angel.persona == "researcher", angel.persona)
        check("description from frontmatter", "guardian Angel" in angel.description, angel.description)
        check("skill carries full SKILL.md", angel.skill.skill_md.startswith("---"), angel.skill.skill_md[:20])
        check("instructions strip frontmatter", angel.instructions.startswith("# Guardian Angel"),
              angel.instructions[:30])
        check("version from frontmatter", angel.version == "1.0.0", angel.version)
        check("tags from frontmatter", angel.tags == ["eden", "kanban", "guardian"], angel.tags)

        print("\n[skill bundle robustness]")
        bundle = angel.to_bundle()
        wire = bundle.dump()
        check("bundle uses skill-bundle/v1 schema", wire["schema"] == "skill-bundle/v1", wire.get("schema"))
        check("bundle entrypoint is SKILL.md", wire["entrypoint"] == "SKILL.md", wire["entrypoint"])
        check("bundle files carry sha256", all(len(f["sha256"]) == 64 for f in wire["files"]),
              [f["path"] for f in wire["files"]])

        angel2 = Angel.from_bundle(bundle.dump_json())
        check("bundle round-trip preserves the angel", angel2 == angel,
              f"{angel2.model_dump()} != {angel.model_dump()}")

        def rejected(mutate, name):
            broken = json.loads(json.dumps(wire))
            mutate(broken)
            try:
                Angel.from_bundle(broken)
                check(name, False, "bundle was accepted")
            except (ValueError, Exception):
                check(name, True)

        rejected(lambda b: b["files"][0].__setitem__("content", b["files"][0]["content"] + "tampered"),
                 "tampered content rejected (sha256 mismatch)")
        rejected(lambda b: b.__setitem__("schema", "skill-bundle/v2"),
                 "unknown schema rejected")
        rejected(lambda b: b.__setitem__("entrypoint", "MAIN.md"),
                 "missing entrypoint rejected")
        rejected(lambda b: b["files"].append({"path": "../evil.sh", "sha256": "0" * 64, "content": "rm -rf /"}),
                 "path traversal rejected")
        rejected(lambda b: b["files"].append(json.loads(json.dumps(b["files"][0]))),
                 "duplicate paths rejected")

        print("\n[root]")
        r = call("GET", "/api/")
        check("GET /api/", r.status_code == 200, r.text)

        print("\n[angel enters the garden]")
        # The Angel watches for task cards of its persona (notify entry point)
        r = call("POST", "/api/notify/", json={
            "template": {"kind": "task", "fields": {"persona": angel.persona}}
        })
        check("angel registers notify subscription", r.status_code == 200, r.text)
        sub_id = r.json()["sub_id"]

        # God writes the Angel itself onto the board as a card
        r = call("POST", "/api/write/", json={
            "kind": "angel", "fields": angel.to_card_fields(), "agent": "god"
        })
        check("angel card written", r.status_code == 200, r.text)
        angel_card_id = r.json()["card"]["id"]

        # Create a Kanban entry: a task card for the Angel's persona
        r = call("POST", "/api/write/", json={
            "kind": "task",
            "fields": {"persona": angel.persona, "action": "summarize",
                       "url": "https://arxiv.org/abs/2511.00402"},
            "agent": "god"
        })
        check("task card (Kanban entry) written", r.status_code == 200, r.text)
        task_card_id = r.json()["card"]["id"]

        print("\n[reading the board]")
        r = call("POST", "/api/read/", json={
            "template": {"kind": "angel", "fields": {"persona": angel.persona}}
        })
        check("read angel card by template", r.status_code == 200
              and r.json()["card"]["fields"]["angel"] == angel.name, r.text)
        angel_back = Angel.from_card_fields(r.json()["card"]["fields"])
        check("angel rebuilt from card fields (bundle verified)", angel_back == angel,
              angel_back.model_dump_json())

        r = call("POST", "/api/read_all/", json={"template": {}})
        check("read_all sees both cards", r.status_code == 200 and r.json()["count"] == 2, r.text)

        r = call("GET", "/api/card/{card_id}", url=f"/api/card/{task_card_id}")
        check("get card by id", r.status_code == 200, r.text)

        r = call("GET", "/api/board/")
        check("board shows 2 open cards", r.status_code == 200
              and len(r.json()["lanes"]["open"]) == 2, r.text)

        print("\n[notify stream]")
        # The task write above matched the subscription; read it from the SSE stream
        exercised.add(("GET", "/api/notify/{sub_id}"))
        notification = None
        with client.stream("GET", f"/api/notify/{sub_id}") as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    notification = json.loads(line[5:].strip())
                    break
        check("SSE delivers the task write", notification is not None
              and notification["type"] == "write"
              and notification["card"]["id"] == task_card_id, str(notification))

        print("\n[angel claims the task]")
        r = call("POST", "/api/take/", json={
            "template": {"kind": "task", "fields": {"persona": angel.persona}},
            "mode": "claim", "agent": angel.name
        })
        check("take mode=claim", r.status_code == 200
              and r.json()["card"]["claimed_by"] == angel.name, r.text)

        r = call("POST", "/api/lease/renew/", json={"card_id": task_card_id, "lease_seconds": 600})
        check("angel renews lease while working", r.status_code == 200, r.text)

        print("\n[transaction: report result and release the task atomically]")
        r = call("POST", "/api/txn/begin/", json={})
        check("txn begin", r.status_code == 200, r.text)
        txn_id = r.json()["txn_id"]

        r = call("POST", "/api/write/", json={
            "kind": "result",
            "fields": {"persona": angel.persona, "task_id": task_card_id, "outcome": "summarized"},
            "agent": angel.name, "txn_id": txn_id
        })
        check("result card written under txn", r.status_code == 200, r.text)
        result_card_id = r.json()["card"]["id"]

        r = call("POST", "/api/txn/commit/", json={"txn_id": txn_id})
        check("txn commit", r.status_code == 200 and r.json()["writes"] == 1, r.text)

        r = call("POST", "/api/read/", json={"template": {"kind": "result"}})
        check("result card visible after commit", r.status_code == 200, r.text)

        # A second transaction is aborted: its work leaves no trace
        r = call("POST", "/api/txn/begin/", json={"timeout_seconds": 30})
        txn_id = r.json()["txn_id"]
        call("POST", "/api/take/", json={
            "template": {"kind": "result"}, "mode": "remove", "txn_id": txn_id
        })
        r = call("POST", "/api/txn/abort/", json={"txn_id": txn_id})
        check("txn abort restores the taken card", r.status_code == 200
              and r.json()["takes_restored"] == 1, r.text)

        print("\n[leave nothing behind]")
        # The user takes the result off the board (classic JavaSpaces take)
        r = call("POST", "/api/take/", json={
            "template": {"kind": "result"}, "mode": "remove", "agent": angel.persona
        })
        check("result card taken (removed)", r.status_code == 200
              and r.json()["card"]["id"] == result_card_id, r.text)

        # The claimed task card and the angel card are cleaned via their leases
        r = call("DELETE", "/api/lease/cancel/", url=f"/api/lease/cancel/?card_id={task_card_id}")
        check("claimed task card cancelled", r.status_code == 200, r.text)
        r = call("DELETE", "/api/lease/cancel/", url=f"/api/lease/cancel/?card_id={angel_card_id}")
        check("angel card cancelled", r.status_code == 200, r.text)

        # The Angel stops watching
        r = call("DELETE", "/api/notify/{sub_id}", url=f"/api/notify/{sub_id}")
        check("subscription removed", r.status_code == 200, r.text)

        print("\n[final state]")
        r = call("GET", "/api/board/")
        check("board is empty", r.json()["count"] == 0, r.text)
        check("no cards left in the space", len(garden.cards) == 0, str(garden.cards))
        check("no transactions left", len(garden.transactions) == 0, str(garden.transactions))
        check("no subscriptions left", len(garden.subscriptions) == 0, str(garden.subscriptions))

        print("\n[entry point coverage]")
        api_routes = {
            (method, route.path)
            for route in app.routes if route.path.startswith("/api")
            for method in route.methods if method != "HEAD"
        }
        missing = api_routes - exercised
        check(f"all {len(api_routes)} FastAPI entry points exercised", not missing,
              f"missing: {sorted(missing)}")

    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=10)

    print(f"\n{'=' * 40}\nPassed: {passed}  Failed: {failed}")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    raise SystemExit(0 if ok else 1)
