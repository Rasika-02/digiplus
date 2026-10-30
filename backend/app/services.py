"""Core business rules for tasks and handovers. No LLM calls in here, by design:
everything in this file must keep working when the AI provider is down."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .logging_setup import log_event
from .models import (CLOSED_STATUSES, OPEN_STATUSES, TASK_STATUSES, Handover, HandoverHistory, HandoverTask,
                     Task, TaskComment, TaskHistory, TaskOutcome, User, utcnow)
from .security import has_perm


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def task_ref(task: Task) -> str:
    return f"TSK-{task.id:05d}"


def handover_ref(h: Handover) -> str:
    return f"HND-{h.id:05d}"


def _user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if not u or not u.is_active:
        raise DomainError(f"User {user_id} not found or inactive", 404)
    return u


def _name(db: Session, user_id: int | None) -> str:
    u = db.get(User, user_id) if user_id else None
    return u.full_name if u else "Unassigned"


def _hist(db, task, event, actor, frm="", to="", comment="", handover_id=None):
    db.add(TaskHistory(task_id=task.id, event=event, from_value=frm, to_value=to, actor_id=actor.id,
                       comment=comment, handover_id=handover_id))


def can_view_task(actor: User, task: Task) -> bool:
    return has_perm(actor.role, "task:view_all") or actor.id in (task.owner_id, task.created_by_id)


def can_modify_task(actor: User, task: Task) -> bool:
    return has_perm(actor.role, "task:assign") or actor.id == task.owner_id


# ---------------------------------------------------------------- tasks
TRANSITIONS = {
    "OPEN": {"ASSIGNED", "IN_PROGRESS", "CANCELLED"},
    "ASSIGNED": {"OPEN", "IN_PROGRESS", "BLOCKED", "CANCELLED"},
    "IN_PROGRESS": {"BLOCKED", "PENDING_APPROVAL", "CANCELLED"},
    "BLOCKED": {"IN_PROGRESS", "CANCELLED"},
    "PENDING_APPROVAL": {"IN_PROGRESS", "CANCELLED"},
}  # COMPLETED is reachable only through complete_task(), which demands an outcome


def create_task(db: Session, actor: User, data: dict) -> Task:
    if not has_perm(actor.role, "task:create"):
        raise DomainError("Your role cannot create tasks", 403)
    owner_id = data.get("owner_id")
    if owner_id:
        _user(db, owner_id)
    task = Task(**{**data, "status": "ASSIGNED" if owner_id else "OPEN", "created_by_id": actor.id})
    db.add(task)
    db.flush()
    _hist(db, task, "CREATED", actor, to=task.status)
    if owner_id:
        _hist(db, task, "ASSIGNED", actor, to=_name(db, owner_id))
    db.commit()
    log_event("task_created", task=task_ref(task), department=task.department, priority=task.priority)
    return task


def change_status(db: Session, actor: User, task: Task, new: str, comment: str = "") -> Task:
    if not can_modify_task(actor, task):
        raise DomainError("Only the assignee or a coordinator/manager can change this task", 403)
    if new not in TASK_STATUSES:
        raise DomainError(f"Unknown status {new}")
    if new == "COMPLETED":
        raise DomainError("Use the complete endpoint: an outcome record is required")
    if new not in TRANSITIONS.get(task.status, set()):
        raise DomainError(f"Cannot move task from {task.status} to {new}", 409)
    if new in ("BLOCKED", "CANCELLED") and not comment.strip():
        raise DomainError(f"A reason is required when setting a task to {new}")
    old, task.status = task.status, new
    _hist(db, task, "STATUS_CHANGE", actor, old, new, comment)
    if new == "BLOCKED":
        db.add(TaskComment(task_id=task.id, author_id=actor.id, kind="blocker", body=comment))
    db.commit()
    log_event("task_status_changed", task=task_ref(task), old=old, new=new)
    return task


def complete_task(db: Session, actor: User, task: Task, outcome: dict) -> Task:
    if not can_modify_task(actor, task):
        raise DomainError("Only the assignee or a coordinator/manager can complete this task", 403)
    if task.status not in ("IN_PROGRESS", "PENDING_APPROVAL"):
        raise DomainError(f"Only In Progress or Pending Approval tasks can be completed (now {task.status})", 409)
    for f in ("resolution_summary", "actions_performed", "final_result"):
        if not str(outcome.get(f, "")).strip():
            raise DomainError(f"Outcome field '{f}' is required")
    db.add(TaskOutcome(task_id=task.id, completed_by_id=actor.id, **outcome))
    old, task.status = task.status, "COMPLETED"
    _hist(db, task, "STATUS_CHANGE", actor, old, "COMPLETED", outcome["resolution_summary"])
    db.commit()
    log_event("task_completed", task=task_ref(task))
    return task


def reassign_task(db: Session, actor: User, task: Task, new_owner_id: int, reason: str) -> Task:
    if not has_perm(actor.role, "task:assign"):
        raise DomainError("Your role cannot reassign tasks", 403)
    if task.status in CLOSED_STATUSES:
        raise DomainError("Closed tasks cannot be reassigned", 409)
    if task.pending_handover_id:
        raise DomainError("Task is part of a pending handover; finish or cancel the handover first", 409)
    new_owner = _user(db, new_owner_id)
    old_name = _name(db, task.owner_id)
    task.owner_id = new_owner.id
    if task.status == "OPEN":
        task.status = "ASSIGNED"
    _hist(db, task, "REASSIGNED", actor, old_name, new_owner.full_name, reason)
    db.commit()
    log_event("task_reassigned", task=task_ref(task), to_user=new_owner.id)
    return task


def add_comment(db: Session, actor: User, task: Task, body: str, kind: str = "comment") -> TaskComment:
    if not can_view_task(actor, task):
        raise DomainError("You cannot access this task", 403)
    c = TaskComment(task_id=task.id, author_id=actor.id, body=body, kind=kind)
    db.add(c)
    db.commit()
    return c


def carry_forward(db: Session, actor: User, task: Task, reference: str, reason: str,
                  review_date: datetime, new_due_date: datetime | None = None, authorized: bool = False) -> Task:
    """Same task row, same ID. Origin and old due date are preserved, never duplicated."""
    if not authorized and not can_modify_task(actor, task):
        raise DomainError("Only the assignee or a coordinator/manager can carry this task forward", 403)
    if task.status in CLOSED_STATUSES:
        raise DomainError("Only open items can be carried forward", 409)
    if task.original_due_date is None:
        task.original_due_date = task.due_date
    old_due = task.due_date.isoformat() if task.due_date else ""
    task.carried_from_ref, task.carry_reason, task.review_date = reference, reason, review_date
    if new_due_date:
        task.due_date = new_due_date
    _hist(db, task, "CARRIED_FORWARD", actor, old_due, reference, reason)
    db.commit()
    log_event("task_carried_forward", task=task_ref(task), reference=reference)
    return task


# ------------------------------------------------------------- handovers
def create_handover(db: Session, actor: User, data: dict) -> Handover:
    if not has_perm(actor.role, "handover:create"):
        raise DomainError("Your role cannot create handovers", 403)
    source_id = data.pop("source_id", None) or actor.id
    if source_id != actor.id and not has_perm(actor.role, "handover:review"):
        raise DomainError("Only managers can start a handover on behalf of someone else", 403)
    if data["receiver_id"] == source_id:
        raise DomainError("Receiving employee must be different from the source employee")
    _user(db, source_id), _user(db, data["receiver_id"])
    h = Handover(**data, source_id=source_id, created_by_id=actor.id, status="DRAFT")
    db.add(h)
    db.flush()
    db.add(HandoverHistory(handover_id=h.id, event="CREATED", actor_id=actor.id, detail=h.reason))
    db.commit()
    log_event("handover_created", handover=handover_ref(h))
    return h


def eligible_tasks(db: Session, h: Handover) -> list[Task]:
    return list(db.scalars(select(Task).where(
        Task.owner_id == h.source_id, Task.status.in_(OPEN_STATUSES), Task.pending_handover_id.is_(None))))


def _check_handover_actor(actor: User, h: Handover):
    if actor.id not in (h.source_id, h.created_by_id) and not has_perm(actor.role, "handover:review"):
        raise DomainError("You are not allowed to modify this handover", 403)


def add_handover_tasks(db: Session, actor: User, h: Handover, task_ids: list[int]) -> Handover:
    _check_handover_actor(actor, h)
    if h.status != "DRAFT":
        raise DomainError("Tasks can only be selected while the handover is a Draft", 409)
    ok = {t.id: t for t in eligible_tasks(db, h)}
    bad = [i for i in task_ids if i not in ok]
    if bad:
        raise DomainError(f"Tasks not eligible for transfer (not owned by source, closed, or already in a handover): {bad}", 409)
    for tid in task_ids:
        t = ok[tid]
        db.add(HandoverTask(handover_id=h.id, task_id=t.id, previous_owner_id=t.owner_id))
        t.pending_handover_id = h.id
        _hist(db, t, "HANDOVER_PLANNED", actor, comment=handover_ref(h), handover_id=h.id)
    db.add(HandoverHistory(handover_id=h.id, event="TASKS_SELECTED", actor_id=actor.id, detail=str(task_ids)))
    db.commit()
    return h


def _hh(db, h, event, actor, detail=""):
    db.add(HandoverHistory(handover_id=h.id, event=event, actor_id=actor.id, detail=detail))


def _links(db, h):
    return list(db.scalars(select(HandoverTask).where(HandoverTask.handover_id == h.id)))


def submit_handover(db: Session, actor: User, h: Handover) -> Handover:
    _check_handover_actor(actor, h)
    if h.status != "DRAFT":
        raise DomainError(f"Only Draft handovers can be submitted (now {h.status})", 409)
    if not _links(db, h):
        raise DomainError("Select at least one task before submitting")
    h.status = "PENDING_REVIEW"
    _hh(db, h, "SUBMITTED", actor)
    db.commit()
    return h


def review_handover(db: Session, actor: User, h: Handover) -> Handover:
    if not has_perm(actor.role, "handover:review"):
        raise DomainError("Only managers can review handovers", 403)
    if h.status != "PENDING_REVIEW":
        raise DomainError(f"Handover is {h.status}, not Pending Review", 409)
    h.status = "AWAITING_ACCEPTANCE"
    _hh(db, h, "REVIEWED", actor)
    db.commit()
    return h


def accept_handover(db: Session, actor: User, h: Handover, comment: str = "") -> Handover:
    if actor.id != h.receiver_id:
        raise DomainError("Only the receiving employee can accept this handover", 403)
    if h.status != "AWAITING_ACCEPTANCE":
        raise DomainError(f"Handover is {h.status}, not Awaiting Acceptance", 409)
    now, moved, skipped = utcnow(), 0, 0
    for ht in _links(db, h):
        t = db.get(Task, ht.task_id)
        t.pending_handover_id = None
        if t.status in OPEN_STATUSES and t.owner_id == ht.previous_owner_id:
            t.owner_id = h.receiver_id  # same row, same ID, history untouched
            if t.status == "OPEN":
                t.status = "ASSIGNED"
            ht.new_owner_id, ht.transferred_at, ht.result = h.receiver_id, now, "transferred"
            _hist(db, t, "TRANSFERRED", actor, _name(db, ht.previous_owner_id), _name(db, h.receiver_id),
                  comment, handover_id=h.id)
            moved += 1
        else:
            ht.result = "skipped"
            skipped += 1
    h.status, h.accepted_at, h.ack_comment = "ACCEPTED", now, comment
    _hh(db, h, "ACCEPTED", actor, f"transferred={moved} skipped={skipped}")
    db.commit()
    log_event("handover_accepted", handover=handover_ref(h), transferred=moved, skipped=skipped)
    return h


def cancel_handover(db: Session, actor: User, h: Handover, reason: str = "") -> Handover:
    _check_handover_actor(actor, h)
    if h.status not in ("DRAFT", "PENDING_REVIEW", "AWAITING_ACCEPTANCE"):
        raise DomainError("Accepted handovers cannot be cancelled", 409)
    for ht in _links(db, h):
        t = db.get(Task, ht.task_id)
        t.pending_handover_id = None
        ht.result = "skipped"
        _hist(db, t, "HANDOVER_CANCELLED", actor, comment=reason, handover_id=h.id)
    h.status = "CANCELLED"
    _hh(db, h, "CANCELLED", actor, reason)
    db.commit()
    return h


def advance_handover(db: Session, actor: User, h: Handover, target: str) -> Handover:
    if actor.id != h.receiver_id and not has_perm(actor.role, "handover:review"):
        raise DomainError("Only the receiver or a manager can advance this handover", 403)
    if target == "IN_PROGRESS" and h.status == "ACCEPTED":
        h.status = "IN_PROGRESS"
    elif target == "COMPLETED" and h.status in ("ACCEPTED", "IN_PROGRESS"):
        still_open = [ht.task_id for ht in _links(db, h) if ht.result == "transferred"
                      and db.get(Task, ht.task_id).status in OPEN_STATUSES]
        if still_open:
            raise DomainError(f"Transferred tasks still open: {still_open}", 409)
        h.status = "COMPLETED"
    else:
        raise DomainError(f"Cannot move handover from {h.status} to {target}", 409)
    _hh(db, h, target, actor)
    db.commit()
    return h
