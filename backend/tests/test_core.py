from app import ai
from app.config import settings

TASK = {"title": "Chase pre-auth for bed 12", "department": "Claims", "category": "Pre-authorization",
        "priority": "high", "due_date": "2020-01-01T00:00:00"}


def mk_task(client, h, owner):
    r = client.post("/tasks", json={**TASK, "owner_id": owner}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_login_and_rbac(client, login):
    assert client.post("/auth/login", json={"username": "billing1", "password": "bad"}).status_code == 401
    assert client.get("/tasks").status_code == 401
    ha, _ = login("analyst1")
    assert client.post("/tasks", json=TASK, headers=ha).status_code == 403  # analysts are read-only
    hb, bid = login("billing1")
    assert client.post(f"/tasks/1/assign", json={"owner_id": bid, "reason": "x y z"}, headers=hb).status_code in (403, 404)


def test_lifecycle_requires_outcome_and_keeps_history(client, login):
    hb, bid = login("billing1")
    tid = mk_task(client, hb, bid)
    assert client.post(f"/tasks/{tid}/status", json={"status": "COMPLETED"}, headers=hb).status_code == 400
    assert client.post(f"/tasks/{tid}/status", json={"status": "BLOCKED"}, headers=hb).status_code == 400  # reason needed
    assert client.post(f"/tasks/{tid}/status", json={"status": "IN_PROGRESS"}, headers=hb).status_code == 200
    bad = {"resolution_summary": "", "actions_performed": "a", "final_result": "b"}
    assert client.post(f"/tasks/{tid}/complete", json=bad, headers=hb).status_code == 400
    good = {**bad, "resolution_summary": "Pre-auth approved"}
    assert client.post(f"/tasks/{tid}/complete", json=good, headers=hb).status_code == 200
    d = client.get(f"/tasks/{tid}", headers=hb).json()
    assert d["status"] == "COMPLETED" and d["outcome"]["resolution_summary"] == "Pre-auth approved"
    assert [h["event"] for h in d["history"]][:2] == ["CREATED", "ASSIGNED"]


def test_handover_transfer_preserves_identity_and_history(client, login):
    ha, aid = login("billing1")
    hr, rid = login("billing2")
    hm, _ = login("manager1")
    t1, t2 = mk_task(client, ha, aid), mk_task(client, ha, aid)
    client.post(f"/tasks/{t1}/comments", json={"body": "Insurer wants discharge summary", "kind": "blocker"}, headers=ha)
    h = client.post("/handovers", json={"title": "Leave cover", "receiver_id": rid, "department": "Billing",
                                        "reason": "Employee leave", "decisions": "Escalate after 45 min"},
                    headers=ha).json()
    hid = h["id"]
    assert client.post(f"/handovers/{hid}/tasks", json={"task_ids": [t1]}, headers=ha).status_code == 200
    # locked while pending: cannot be reassigned or double-booked
    assert client.post(f"/tasks/{t1}/assign", json={"owner_id": rid, "reason": "sneaky"}, headers=hm).status_code == 409
    h2 = client.post("/handovers", json={"title": "Other", "receiver_id": rid, "department": "Billing",
                                         "reason": "Shift change"}, headers=ha).json()
    assert client.post(f"/handovers/{h2['id']}/tasks", json={"task_ids": [t1]}, headers=ha).status_code == 409
    assert client.post(f"/handovers/{hid}/submit", headers=ha).status_code == 200
    # not accepted yet: owner unchanged and clearly marked
    assert client.get(f"/tasks/{t1}", headers=ha).json()["owner_id"] == aid
    assert client.get(f"/handovers/{hid}", headers=ha).json()["accepted"] is False
    assert client.post(f"/handovers/{hid}/accept", json={}, headers=hr).status_code == 409  # not reviewed yet
    assert client.post(f"/handovers/{hid}/review", headers=ha).status_code == 403
    assert client.post(f"/handovers/{hid}/review", headers=hm).status_code == 200
    assert client.post(f"/handovers/{hid}/accept", json={}, headers=ha).status_code == 403  # only receiver
    assert client.post(f"/handovers/{hid}/accept", json={"comment": "Got it"}, headers=hr).status_code == 200
    d = client.get(f"/tasks/{t1}", headers=hr).json()
    assert d["id"] == t1 and d["owner_id"] == rid
    events = [x["event"] for x in d["history"]]
    assert events[:2] == ["CREATED", "ASSIGNED"] and "TRANSFERRED" in events
    assert any(c["kind"] == "blocker" for c in d["comments"])  # old comments survive
    assert client.get(f"/tasks/{t2}", headers=hr).status_code == 404 or \
        client.get(f"/tasks/{t2}", headers=hr).json()["owner_id"] == aid  # unselected task stays put


def test_handover_works_when_llm_fails(client, login, monkeypatch):
    ha, aid = login("billing1")
    hr, rid = login("billing2")
    tid = mk_task(client, ha, aid)
    hid = client.post("/handovers", json={"title": "Cover", "receiver_id": rid, "department": "Billing",
                                          "reason": "Temporary absence"}, headers=ha).json()["id"]
    client.post(f"/handovers/{hid}/tasks", json={"task_ids": [tid]}, headers=ha)
    monkeypatch.setattr(settings, "openai_api_key", "sk-fake")
    monkeypatch.setattr(ai, "_call_llm", lambda ctx: (_ for _ in ()).throw(TimeoutError("down")))
    r = client.post(f"/handovers/{hid}/brief", headers=ha)
    assert r.status_code == 200 and r.json()["generated_by"] == "rules"
    assert any("overdue" in s for s in r.json()["needs_immediate_attention"])
    assert client.post(f"/handovers/{hid}/submit", headers=ha).status_code == 200  # workflow unaffected


def test_carry_forward_keeps_same_task_and_origin(client, login):
    hb, bid = login("billing1")
    tid = mk_task(client, hb, bid)
    body = {"reference": "MTG-0007", "reason": "Not discussed in time", "review_date": "2026-11-10T09:00:00",
            "new_due_date": "2026-11-12T09:00:00"}
    r = client.post(f"/tasks/{tid}/carry-forward", json=body, headers=hb)
    assert r.status_code == 200
    d = client.get(f"/tasks/{tid}", headers=hb).json()
    assert d["id"] == tid and d["carried_from_ref"] == "MTG-0007"
    assert d["original_due_date"].startswith("2020-01-01") and d["due_date"].startswith("2026-11-12")
    assert d["history"][-1]["event"] == "CARRIED_FORWARD"
    assert client.get("/tasks?mine=true", headers=hb).json()["total"] == 1  # no duplicate created
