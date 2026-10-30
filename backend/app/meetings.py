from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services as svc
from .logging_setup import log_event
from .models import (OPEN_STATUSES, Meeting, MeetingActionItem, MeetingParticipant, Task, User, utcnow)
from .security import has_perm
from .services import DomainError


def meeting_ref(m: Meeting) -> str:
    return f"MTG-{m.id:04d}"


def participants(db: Session, m: Meeting) -> list[dict]:
    rows = db.execute(select(MeetingParticipant, User).join(User, User.id == MeetingParticipant.user_id)
                      .where(MeetingParticipant.meeting_id == m.id).order_by(User.full_name)).all()
    return [{"id": u.id, "name": u.full_name, "attended": p.attended} for p, u in rows]


def can_access(db: Session, user: User, m: Meeting) -> bool:
    return (user.id == m.organizer_id or has_perm(user.role, "task:view_all")
            or any(p["id"] == user.id for p in participants(db, m)))


def can_edit(user: User, m: Meeting) -> bool:
    return user.id == m.organizer_id or has_perm(user.role, "task:assign")


def get_meeting(db: Session, mid: int, user: User) -> Meeting:
    m = db.get(Meeting, mid)
    if not m or not can_access(db, user, m):
        raise DomainError("Meeting not found", 404)
    return m


def create_meeting(db: Session, actor: User, data: dict) -> Meeting:
    if not has_perm(actor.role, "task:create"):
        raise DomainError("Your role cannot create meetings", 403)
    ids = set(data.pop("participant_ids", [])) | {actor.id}
    for uid in ids:
        if not db.get(User, uid):
            raise DomainError(f"Participant {uid} not found", 404)
    prev = data.get("previous_meeting_id")
    if prev and not db.get(Meeting, prev):
        raise DomainError("Previous meeting not found", 404)
    m = Meeting(**data, organizer_id=actor.id, decisions=[])
    db.add(m)
    db.flush()
    for uid in ids:
        db.add(MeetingParticipant(meeting_id=m.id, user_id=uid))
    db.commit()
    log_event("meeting_created", meeting=meeting_ref(m), participants=len(ids))
    return m


def update_meeting(db: Session, actor: User, m: Meeting, fields: dict) -> Meeting:
    if not can_edit(actor, m):
        raise DomainError("Only the organizer or a manager can edit this meeting", 403)
    for k in ("agenda", "notes", "status"):
        if fields.get(k) is not None:
            setattr(m, k, fields[k])
    db.commit()
    return m


def add_decision(db: Session, actor: User, m: Meeting, text: str) -> Meeting:
    if not can_edit(actor, m):
        raise DomainError("Only the organizer or a manager can record decisions", 403)
    m.decisions = [*(m.decisions or []), {"text": text.strip(), "by": actor.full_name, "at": utcnow().isoformat()}]
    db.commit()
    log_event("meeting_decision_recorded", meeting=meeting_ref(m))
    return m


def set_attendance(db: Session, actor: User, m: Meeting, attended_ids: list[int]) -> None:
    if not can_edit(actor, m):
        raise DomainError("Only the organizer or a manager can record attendance", 403)
    for p in db.scalars(select(MeetingParticipant).where(MeetingParticipant.meeting_id == m.id)):
        p.attended = p.user_id in attended_ids
    db.commit()


def link_task(db: Session, m: Meeting, task: Task, origin: str = "created") -> None:
    if not db.scalar(select(MeetingActionItem).where(MeetingActionItem.meeting_id == m.id,
                                                      MeetingActionItem.task_id == task.id)):
        db.add(MeetingActionItem(meeting_id=m.id, task_id=task.id, origin=origin))
        db.commit()


def add_action_item(db: Session, actor: User, m: Meeting, data: dict) -> Task:
    if not can_access(db, actor, m):
        raise DomainError("Meeting not found", 404)
    task = svc.create_task(db, actor, {
        "title": data["title"], "description": f"Action item from {meeting_ref(m)}: {m.title}",
        "department": m.department, "category": "General operations", "priority": data.get("priority", "medium"),
        "owner_id": data.get("owner_id"), "due_date": data.get("due_date")})
    link_task(db, m, task, "created")
    return task


def action_items(db: Session, m: Meeting) -> list[tuple[Task, str]]:
    rows = db.execute(select(Task, MeetingActionItem.origin).join(MeetingActionItem, MeetingActionItem.task_id == Task.id)
                      .where(MeetingActionItem.meeting_id == m.id).order_by(Task.id)).all()
    return [(t, o) for t, o in rows]


def approve_extraction(db: Session, actor: User, m: Meeting, summary: dict, decisions: list[str],
                       items: list[dict]) -> list[Task]:
    """Nothing from the AI is saved until a human approves it."""
    if not can_edit(actor, m) and actor.id not in [p["id"] for p in participants(db, m)]:
        raise DomainError("Only meeting participants can approve extracted items", 403)
    known = {d["text"] for d in m.decisions or []}
    new = [{"text": d.strip(), "by": actor.full_name, "at": utcnow().isoformat(), "source": "ai-approved"}
           for d in decisions if d.strip() and d.strip() not in known]
    m.decisions = [*(m.decisions or []), *new]
    m.ai_summary = {**summary, "approved_by": actor.full_name, "approved_at": utcnow().isoformat()}
    db.commit()
    created = [add_action_item(db, actor, m, i) for i in items]
    log_event("meeting_extraction_approved", meeting=meeting_ref(m), decisions=len(new), actions=len(created))
    return created


def carry_forward_items(db: Session, actor: User, src: Meeting, dst: Meeting, reason: str,
                        review_date: datetime) -> dict:
    """Open items keep their task ID and history; they just gain a link to the next meeting."""
    if not can_edit(actor, src):
        raise DomainError("Only the organizer or a manager can carry items forward", 403)
    if src.id == dst.id:
        raise DomainError("Choose a different meeting to carry items into")
    carried, skipped = [], []
    for t, _ in action_items(db, src):
        if t.status not in OPEN_STATUSES:
            continue
        try:
            svc.carry_forward(db, actor, t, meeting_ref(src), reason, review_date, authorized=True)
            link_task(db, dst, t, "carried")
            carried.append(svc.task_ref(t))
        except DomainError as e:
            skipped.append({"task": svc.task_ref(t), "reason": e.message})
    if dst.previous_meeting_id is None:
        dst.previous_meeting_id = src.id
        db.commit()
    return {"carried": carried, "skipped": skipped}


def catch_up_context(db: Session, m: Meeting, user: User) -> dict:
    now = utcnow()
    parts = participants(db, m)
    me = next((p for p in parts if p["id"] == user.id), None)
    items = []
    for t, origin in action_items(db, m):
        items.append({"ref": svc.task_ref(t), "title": t.title, "status": t.status, "owner": svc._name(db, t.owner_id),
                      "owner_id": t.owner_id, "due": t.due_date.isoformat() if t.due_date else None,
                      "overdue": bool(t.due_date and t.due_date < now and t.status in OPEN_STATUSES),
                      "carried": origin == "carried", "from": t.carried_from_ref})
    return {"meeting": {"ref": meeting_ref(m), "title": m.title, "date": m.meeting_date.isoformat(),
                        "agenda": m.agenda, "notes": m.notes[:3000]},
            "summary": (m.ai_summary or {}).get("summary", ""), "decisions": [d["text"] for d in m.decisions or []],
            "items": items, "you_attended": me["attended"] if me else None,
            "you_are_participant": me is not None, "user_id": user.id}


def rule_catch_up(ctx: dict) -> dict:
    open_items = [i for i in ctx["items"] if i["status"] in OPEN_STATUSES]
    mine = [i for i in open_items if i["owner_id"] == ctx["user_id"]]
    attention = [f"{i['ref']} is overdue ({i['title']})" for i in open_items if i["overdue"]] + \
                [f"{i['ref']} has no owner ({i['title']})" for i in open_items if i["owner_id"] is None]
    recap = ctx["summary"] or (ctx["meeting"]["notes"][:400] + ("…" if len(ctx["meeting"]["notes"]) > 400 else ""))
    return {"what_happened": recap or "No notes or summary were recorded for this meeting.",
            "decisions": ctx["decisions"] or [],
            "changed": [f"{i['ref']} carried forward from {i['from']}" for i in ctx["items"] if i["carried"]],
            "pending_items": [f"{i['ref']} {i['title']} [{i['status']}, owner {i['owner']}, due {i['due'] or 'not set'}]"
                              for i in open_items],
            "your_items": [f"{i['ref']} {i['title']} (due {i['due'] or 'not set'})" for i in mine],
            "needs_immediate_attention": attention,
            "documents_to_review": [],
            "you_attended": ctx["you_attended"]}
