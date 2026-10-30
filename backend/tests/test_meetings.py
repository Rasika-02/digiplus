from app import ai
from app.config import settings

NOTES = ("Discussed pre-auth delays.\nDecision: Escalate pre-auth after 45 minutes\n"
         "Action: Ravi Patil to call insurer by 2026-11-05\nAction: Update discharge checklist")


def mk(client, h, rid, **kw):
    body = {"title": "Billing sync", "meeting_date": "2026-11-02T10:00:00", "department": "Billing",
            "participant_ids": [rid], "notes": NOTES, **kw}
    r = client.post("/meetings", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def approve_all(client, h, mid):
    p = client.post(f"/meetings/{mid}/extract", headers=h).json()
    body = {"summary": p["summary"], "decisions": p["decisions"],
            "action_items": [{"title": i["title"], "owner_id": i["owner_id"], "due_date": i["due_date"]}
                             for i in p["action_items"]]}
    r = client.post(f"/meetings/{mid}/approve", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_extraction_is_proposal_only_and_does_not_invent(client, login):
    ha, _ = login("billing1")
    _, rid = login("billing2")
    m = mk(client, ha, rid)
    p = client.post(f"/meetings/{m['id']}/extract", headers=ha).json()
    assert p["generated_by"] == "rules"
    assert p["decisions"] == ["Escalate pre-auth after 45 minutes"]
    assert p["action_items"][0]["owner_id"] == rid and p["action_items"][0]["due_date"] == "2026-11-05"
    assert p["action_items"][1]["owner_id"] is None and p["action_items"][1]["needs_confirmation"]
    d = client.get(f"/meetings/{m['id']}", headers=ha).json()
    assert d["decisions"] == [] and d["action_items"] == []  # nothing saved before approval


def test_llm_output_is_validated_and_failures_fall_back(client, login, monkeypatch):
    ha, _ = login("billing1")
    _, rid = login("billing2")
    m = mk(client, ha, rid)
    monkeypatch.setattr(settings, "openai_api_key", "sk-fake")
    monkeypatch.setattr(ai, "_llm_json", lambda s, c: {
        "summary": "Pre-auth delays discussed", "decisions": ["Escalate pre-auth after 45 minutes", "Buy a new MRI machine"],
        "action_items": [{"title": "Hire ten new nurses", "owner_name": "Ravi Patil", "due_date": "2026-12-01"}]})
    p = client.post(f"/meetings/{m['id']}/extract", headers=ha).json()
    assert p["generated_by"].startswith("llm") and p["decisions"] == ["Escalate pre-auth after 45 minutes"]
    assert p["action_items"] == [] and any("dropped" in u for u in p["unresolved"])
    monkeypatch.setattr(ai, "_llm_json", lambda s, c: (_ for _ in ()).throw(TimeoutError()))
    assert client.post(f"/meetings/{m['id']}/extract", headers=ha).json()["generated_by"] == "rules"


def test_approve_then_carry_forward_keeps_same_tasks(client, login):
    ha, _ = login("billing1")
    _, rid = login("billing2")
    m1 = mk(client, ha, rid)
    d1 = approve_all(client, ha, m1["id"])
    ids1 = sorted(i["id"] for i in d1["action_items"])
    assert len(ids1) == 2 and d1["decisions"][0]["text"].startswith("Escalate")
    m2 = mk(client, ha, rid, title="Billing sync 2", meeting_date="2026-11-09T10:00:00", notes="")
    r = client.post(f"/meetings/{m1['id']}/carry-forward", headers=ha,
                    json={"to_meeting_id": m2["id"], "reason": "Not finished", "review_date": "2026-11-16T10:00:00"})
    assert r.status_code == 200 and len(r.json()["carried"]) == 2
    d2 = client.get(f"/meetings/{m2['id']}", headers=ha).json()
    assert sorted(i["id"] for i in d2["action_items"]) == ids1  # same tasks, no duplicates
    assert all(i["origin"] == "carried" and i["carried_from_ref"] == m1["ref"] for i in d2["action_items"])
    t = client.get(f"/tasks/{ids1[0]}", headers=ha).json()
    assert t["history"][-1]["event"] == "CARRIED_FORWARD"


def test_catch_up_and_meeting_access(client, login):
    ha, _ = login("billing1")
    hr, rid = login("billing2")
    hc, _ = login("claims1")
    m = mk(client, ha, rid)
    approve_all(client, ha, m["id"])
    assert client.get(f"/meetings/{m['id']}", headers=hc).status_code == 404  # not a participant
    b = client.get(f"/meetings/{m['id']}/catch-up", headers=hr).json()
    assert any("call insurer" in i for i in b["your_items"])
    assert any("no owner" in i for i in b["needs_immediate_attention"])
    assert b["decisions"] == ["Escalate pre-auth after 45 minutes"]


def test_continuity_score_rewards_recorded_context(client, login):
    ha, aid = login("billing1")
    _, rid = login("billing2")
    base = {"title": "Cover", "receiver_id": rid, "department": "Billing", "reason": "Employee leave"}
    task = {"title": "Chase pre-auth", "department": "Claims", "category": "Pre-authorization",
            "owner_id": aid, "due_date": "2020-01-01T00:00:00", "expected_outcome": "Approval received"}
    scores = []
    for extra in ({}, {"notes": "n", "decisions": "d", "pending_issues": "none", "important_documents": ["SOP-12"]}):
        tid = client.post("/tasks", json=task, headers=ha).json()["id"]
        client.post(f"/tasks/{tid}/comments", json={"body": "Waiting on insurer", "kind": "progress"}, headers=ha) \
            if extra else None
        hid = client.post("/handovers", json={**base, **extra}, headers=ha).json()["id"]
        client.post(f"/handovers/{hid}/tasks", json={"task_ids": [tid]}, headers=ha)
        scores.append(client.get(f"/handovers/{hid}/continuity", headers=ha).json())
    assert scores[0]["context_loss_risk"] in ("High", "Medium") and scores[1]["score"] == 100
    assert scores[0]["gaps"] and not scores[1]["gaps"]
