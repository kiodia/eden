"""
Test for the Angels multi-agent system: two Angels wired through the Eden
board in a 'newsroom' workflow, exercising every FastAPI entry point.

- guardian-angel (loaded from assets/): consumes 'task' cards, produces
  'result' cards (workflow step 1)
- scribe-angel (built programmatically as a Skill identity): consumes
  'result' cards, produces 'digest' cards (workflow step 2)

Runs against a real uvicorn server (background thread) like test_angel.py.

    python tests/test_angels.py
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
from angel import Angel, BundleMetadata  # noqa: E402
from angels import Angels, Skill, SkillDependency  # noqa: E402

HOST, PORT = "127.0.0.1", 8902
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


SCRIBE_SKILL_MD = """---
name: scribe-angel
description: Turns result cards into digest cards for the persona
persona: researcher
version: 1.0.0
tags: eden, kanban, scribe
---

# Scribe Angel

Watch the Eden board for result cards of your persona, claim them, and
write a digest card summarizing the outcome. Leave nothing behind.
"""


def build_host():
    """Assemble the two-Angel system."""
    # Member 1: the guardian Angel from the assets folder, given its
    # board dependencies (Kanban card + workflow assignments)
    guardian = Angel.from_skill_dir(os.path.join(PROJECT_ROOT, "assets", "guardian-angel"))
    guardian_skill = Skill.from_angel(guardian, dependencies=[
        SkillDependency(kind="task", action="consume",
                        fields={"persona": "researcher"}, workflow="newsroom", step=1),
        SkillDependency(kind="result", action="produce",
                        fields={"persona": "researcher"}, workflow="newsroom", step=1),
    ])

    # Member 2: the scribe, built directly as a Skill identity
    scribe_skill = Skill(
        metadata=BundleMetadata(name="scribe-angel", version="1.0.0",
                                description="Turns result cards into digest cards",
                                tags=["eden", "kanban", "scribe"]),
        entrypoint=SCRIBE_SKILL_MD,
        resources={"references/style.md": "# Digest style\n\nOne paragraph, plain language.\n"},
        scripts={"digest.py": "def digest(result):\n    return f\"Digest of {result}\"\n"},
        dependencies=[
            SkillDependency(kind="result", action="consume",
                            fields={"persona": "researcher"}, workflow="newsroom", step=2),
            SkillDependency(kind="digest", action="produce",
                            fields={"persona": "researcher"}, workflow="newsroom", step=2),
        ],
    )

    host = Angels(name="newsroom-host")
    host.enlist(guardian_skill)
    host.enlist(scribe_skill)
    return host, guardian_skill, scribe_skill


def run_tests():
    print("\n[skill identity]")
    host, guardian_skill, scribe_skill = build_host()
    check("skill name from metadata", scribe_skill.name == "scribe-angel", scribe_skill.name)
    check("skill instructions from entrypoint",
          scribe_skill.instructions.startswith("# Scribe Angel"), scribe_skill.instructions[:30])
    check("consume/produce split", [d.kind for d in scribe_skill.consumes()] == ["result"]
          and [d.kind for d in scribe_skill.produces()] == ["digest"], scribe_skill.dependencies)

    print("\n[identity vs serialization]")
    bundle = scribe_skill.to_bundle()
    paths = [f.path for f in bundle.files]
    check("bundle carries scripts, resources and dependencies.json",
          set(paths) == {"SKILL.md", "scripts/digest.py", "references/style.md", "dependencies.json"},
          paths)
    skill_back = Skill.from_bundle(bundle.dump_json())
    check("bundle round-trip preserves the identity", skill_back == scribe_skill,
          skill_back.model_dump_json())
    angel = scribe_skill.to_angel()
    check("identity materializes as an Angel", angel.name == "scribe-angel"
          and angel.persona == "researcher"
          and angel.skill.references == {"style.md": "# Digest style\n\nOne paragraph, plain language.\n"},
          angel.model_dump_json())
    check("angel round-trip preserves the skill core",
          Skill.from_angel(angel, dependencies=scribe_skill.dependencies) == scribe_skill,
          "")

    print("\n[the host]")
    check("two angels enlisted", sorted(host.angels.keys()) == ["guardian-angel", "scribe-angel"],
          list(host.skills.keys()))
    try:
        host.enlist(scribe_skill)
        check("duplicate enlisting rejected", False, "was accepted")
    except ValueError:
        check("duplicate enlisting rejected", True)
    check("board wiring: result flows guardian -> scribe",
          host.producers_of("result") == ["guardian-angel"]
          and host.consumers_of("result") == ["scribe-angel"], "")
    steps = host.workflow("newsroom")
    check("newsroom workflow ordered by step",
          [(s, n, d.kind, d.action) for s, n, d in steps] == [
              (1, "guardian-angel", "task", "consume"),
              (1, "guardian-angel", "result", "produce"),
              (2, "scribe-angel", "result", "consume"),
              (2, "scribe-angel", "digest", "produce"),
          ], steps)
    check("system wiring is sound", host.validate_system() == [], host.validate_system())

    broken = Angels(name="broken-host")
    broken.enlist(Skill(
        metadata=BundleMetadata(name="lonely-angel"),
        entrypoint="---\nname: lonely-angel\n---\n\n# Lonely",
        dependencies=[SkillDependency(kind="orphan", action="consume", workflow="w", step=2)],
    ))
    check("broken wiring is reported", broken.validate_system() != [], "no issues found")

    print("\n[deployment on the eden api]")
    server, thread = start_server()
    client = httpx.Client(base_url=f"http://{HOST}:{PORT}", headers=HEADERS,
                          timeout=httpx.Timeout(10.0))
    try:
        def call(method, template, url=None, **kwargs):
            exercised.add((method.upper(), template))
            return client.request(method, url or template, **kwargs)

        r = call("GET", "/api/")
        check("GET /api/", r.status_code == 200, r.text)

        angel_card_ids = []
        for payload in host.deployment_cards():
            r = call("POST", "/api/write/", json=payload)
            check(f"angel deployed: {payload['fields']['angel']}", r.status_code == 200, r.text)
            angel_card_ids.append(r.json()["card"]["id"])

        r = call("POST", "/api/read_all/", json={"template": {"kind": "angel"}})
        check("both angels on the board", r.json()["count"] == 2, r.text)
        deployed = Angel.from_card_fields(r.json()["cards"][0]["fields"])
        check("deployed angel card rebuilds (bundle verified)",
              deployed.name in host.angels, deployed.name)
        check("deployed card carries assignments",
              len(r.json()["cards"][0]["fields"]["assignments"]) == 2, r.text)

        print("\n[newsroom workflow: task -> result -> digest]")
        # The scribe watches for the guardian's results (step 2 consume)
        result_template = {"kind": "result", "fields": {"persona": "researcher"}}
        r = call("POST", "/api/notify/", json={"template": result_template})
        sub_id = r.json()["sub_id"]
        check("scribe subscribes to result cards", r.status_code == 200, r.text)

        # God writes a task (the newsroom workflow's entry card)
        r = call("POST", "/api/write/", json={
            "kind": "task",
            "fields": {"persona": "researcher", "action": "summarize",
                       "url": "https://arxiv.org/abs/2511.00402"},
            "agent": "god"
        })
        check("task card written", r.status_code == 200, r.text)
        task_id = r.json()["card"]["id"]

        r = call("GET", "/api/card/{card_id}", url=f"/api/card/{task_id}")
        check("task card readable by id", r.status_code == 200, r.text)

        # Step 1 - the guardian takes the task and writes the result atomically
        r = call("POST", "/api/txn/begin/", json={})
        txn_id = r.json()["txn_id"]
        r = call("POST", "/api/take/", json={
            "template": {"kind": "task", "fields": {"persona": "researcher"}},
            "mode": "remove", "agent": "guardian-angel", "txn_id": txn_id
        })
        check("guardian takes the task under txn", r.status_code == 200, r.text)
        r = call("POST", "/api/write/", json={
            "kind": "result",
            "fields": {"persona": "researcher", "task_id": task_id, "outcome": "summarized"},
            "agent": "guardian-angel", "txn_id": txn_id
        })
        check("guardian writes the result under txn", r.status_code == 200, r.text)
        result_id = r.json()["card"]["id"]
        r = call("POST", "/api/txn/commit/", json={"txn_id": txn_id})
        check("step 1 committed atomically", r.status_code == 200
              and r.json()["writes"] == 1 and r.json()["takes"] == 1, r.text)

        # The scribe is notified of the new result via SSE
        exercised.add(("GET", "/api/notify/{sub_id}"))
        notification = None
        with client.stream("GET", f"/api/notify/{sub_id}") as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    notification = json.loads(line[5:].strip())
                    break
        check("SSE notifies the scribe of the result", notification is not None
              and notification["type"] == "write"
              and notification["card"]["id"] == result_id, str(notification))

        # Step 2 - the scribe first fails (txn aborted, result restored) ...
        r = call("POST", "/api/txn/begin/", json={"timeout_seconds": 30})
        txn_id = r.json()["txn_id"]
        call("POST", "/api/take/", json={
            "template": result_template, "mode": "remove",
            "agent": "scribe-angel", "txn_id": txn_id
        })
        r = call("POST", "/api/txn/abort/", json={"txn_id": txn_id})
        check("scribe's failed attempt aborted, result restored",
              r.status_code == 200 and r.json()["takes_restored"] == 1, r.text)

        # ... then retries: claims the result, renews the lease while working
        r = call("POST", "/api/take/", json={
            "template": result_template, "mode": "claim", "agent": "scribe-angel"
        })
        check("scribe claims the result", r.status_code == 200
              and r.json()["card"]["claimed_by"] == "scribe-angel", r.text)
        r = call("POST", "/api/lease/renew/", json={"card_id": result_id, "lease_seconds": 600})
        check("scribe renews the lease while working", r.status_code == 200, r.text)

        r = call("POST", "/api/write/", json={
            "kind": "digest",
            "fields": {"persona": "researcher", "result_id": result_id,
                       "text": "Digest of the summarized paper"},
            "agent": "scribe-angel"
        })
        check("scribe writes the digest", r.status_code == 200, r.text)
        digest_id = r.json()["card"]["id"]

        # The user picks up the workflow's deliverable
        r = call("POST", "/api/read/", json={
            "template": {"kind": "digest", "fields": {"persona": "researcher"}}
        })
        check("user reads the digest", r.status_code == 200
              and r.json()["card"]["id"] == digest_id, r.text)
        r = call("POST", "/api/take/", json={
            "template": {"kind": "digest"}, "mode": "remove", "agent": "researcher"
        })
        check("user takes the digest off the board", r.status_code == 200, r.text)

        print("\n[leave nothing behind]")
        r = call("DELETE", "/api/lease/cancel/", url=f"/api/lease/cancel/?card_id={result_id}")
        check("claimed result card cancelled", r.status_code == 200, r.text)
        for card_id in angel_card_ids:
            r = call("DELETE", "/api/lease/cancel/", url=f"/api/lease/cancel/?card_id={card_id}")
            check(f"angel card {card_id} cancelled", r.status_code == 200, r.text)
        r = call("DELETE", "/api/notify/{sub_id}", url=f"/api/notify/{sub_id}")
        check("scribe's subscription removed", r.status_code == 200, r.text)

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
