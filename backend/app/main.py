import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import jwt
from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ai, meetings as mtg, services as svc
from .continuity import score_continuity
from .db import Base, engine, get_db
from .logging_setup import log_event, request_id_var, setup_logging, user_id_var
from .models import (CATEGORIES, CLOSED_STATUSES, OPEN_STATUSES, Handover, HandoverHistory, HandoverTask, Task,
                     TaskComment, TaskHistory, TaskOutcome, User, utcnow, Meeting)
from .security import create_token, decode_token, has_perm, verify_password

setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)  # dev convenience; Alembic migrations come in the DB-hardening phase
    yield


app = FastAPI(title="ApolloCare Intelligence API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    user_id_var.set("-")
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    log_event("http_request", method=request.method, path=request.url.path, status=response.status_code,
              latency_ms=round((time.perf_counter() - start) * 1000))
    return response


@app.exception_handler(svc.DomainError)
async def domain_error(_: Request, exc: svc.DomainError):
    return JSONResponse({"detail": exc.message, "request_id": request_id_var.get()}, status_code=exc.status)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    logging.getLogger("apollocare").exception("unhandled_error")
    return JSONResponse({"detail": "Internal error", "request_id": request_id_var.get()}, status_code=500)


def naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not creds:
        raise svc.DomainError("Not authenticated", 401)
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise svc.DomainError("Invalid or expired token", 401)
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise svc.DomainError("User inactive", 401)
    user_id_var.set(str(user.id))
    return user


def require(perm: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not has_perm(user.role, perm):
            raise svc.DomainError(f"Missing permission: {perm}", 403)
        return user
    return dep


# ------------------------------------------------------------ schemas
class LoginIn(BaseModel):
    username: str
    password: str


class TaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    department: str = Field(min_length=2)
    category: Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    owner_id: Optional[int] = None
    due_date: Optional[datetime] = None
    expected_outcome: str = ""
    related_patient_ref: str = ""
    related_claim_ref: str = ""


class StatusIn(BaseModel):
    status: str
    comment: str = ""


class AssignIn(BaseModel):
    owner_id: int
    reason: str = Field(min_length=3)


class CommentIn(BaseModel):
    body: str = Field(min_length=1)
    kind: Literal["comment", "progress", "blocker", "resolution"] = "comment"


class OutcomeIn(BaseModel):
    resolution_summary: str
    actions_performed: str
    final_result: str
    evidence_refs: list[str] = []


class CarryIn(BaseModel):
    reference: str = Field(min_length=1)
    reason: str = Field(min_length=3)
    review_date: datetime
    new_due_date: Optional[datetime] = None


class HandoverIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    receiver_id: int
    source_id: Optional[int] = None
    department: str
    reason: Literal["Employee leave", "Shift change", "Department transfer", "Employee resignation",
                    "Work reassignment", "Temporary absence", "Other operational reasons"]
    notes: str = ""
    decisions: str = ""
    pending_issues: str = ""
    important_documents: list[str] = []


class TaskIdsIn(BaseModel):
    task_ids: list[int] = Field(min_length=1)


class CommentOnlyIn(BaseModel):
    comment: str = ""


class AdvanceIn(BaseModel):
    target: Literal["IN_PROGRESS", "COMPLETED"]


# --------------------------------------------------------- serializers
def task_out(db: Session, t: Task) -> dict:
    now = utcnow()
    return {"id": t.id, "ref": svc.task_ref(t), "title": t.title, "description": t.description,
            "department": t.department, "category": t.category, "priority": t.priority, "status": t.status,
            "owner": svc._name(db, t.owner_id), "owner_id": t.owner_id, "created_by_id": t.created_by_id,
            "due_date": t.due_date, "expected_outcome": t.expected_outcome,
            "related_patient_ref": t.related_patient_ref, "related_claim_ref": t.related_claim_ref,
            "overdue": bool(t.due_date and t.due_date < now and t.status in OPEN_STATUSES),
            "pending_handover": f"HND-{t.pending_handover_id:05d}" if t.pending_handover_id else None,
            "carried_from_ref": t.carried_from_ref, "original_due_date": t.original_due_date,
            "review_date": t.review_date, "carry_reason": t.carry_reason, "created_at": t.created_at}


def task_detail(db: Session, t: Task) -> dict:
    out = task_out(db, t)
    out["history"] = [{"event": h.event, "from": h.from_value, "to": h.to_value, "by": svc._name(db, h.actor_id),
                       "comment": h.comment, "handover_id": h.handover_id, "at": h.created_at}
                      for h in db.scalars(select(TaskHistory).where(TaskHistory.task_id == t.id)
                                          .order_by(TaskHistory.id))]
    out["comments"] = [{"id": c.id, "kind": c.kind, "body": c.body, "by": svc._name(db, c.author_id),
                        "at": c.created_at}
                       for c in db.scalars(select(TaskComment).where(TaskComment.task_id == t.id)
                                           .order_by(TaskComment.id))]
    o = db.scalar(select(TaskOutcome).where(TaskOutcome.task_id == t.id))
    out["outcome"] = None if not o else {
        "resolution_summary": o.resolution_summary, "actions_performed": o.actions_performed,
        "final_result": o.final_result, "evidence_refs": o.evidence_refs,
        "completed_by": svc._name(db, o.completed_by_id), "completed_at": o.completed_at}
    return out


def handover_out(db: Session, h: Handover, detail: bool = False) -> dict:
    out = {"id": h.id, "ref": svc.handover_ref(h), "title": h.title, "status": h.status, "reason": h.reason,
           "department": h.department, "source": svc._name(db, h.source_id), "source_id": h.source_id,
           "receiver": svc._name(db, h.receiver_id), "receiver_id": h.receiver_id, "notes": h.notes,
           "decisions": h.decisions, "pending_issues": h.pending_issues,
           "important_documents": h.important_documents, "created_at": h.created_at,
           "accepted_at": h.accepted_at, "ack_comment": h.ack_comment,
           "accepted": h.status in ("ACCEPTED", "IN_PROGRESS", "COMPLETED")}
    if detail:
        out["tasks"] = [{"task_ref": f"TSK-{ht.task_id:05d}", "task_id": ht.task_id, "result": ht.result,
                         "previous_owner": svc._name(db, ht.previous_owner_id),
                         "new_owner": svc._name(db, ht.new_owner_id) if ht.new_owner_id else None,
                         "transferred_at": ht.transferred_at}
                        for ht in db.scalars(select(HandoverTask).where(HandoverTask.handover_id == h.id))]
        out["history"] = [{"event": x.event, "by": svc._name(db, x.actor_id), "detail": x.detail, "at": x.created_at}
                          for x in db.scalars(select(HandoverHistory).where(HandoverHistory.handover_id == h.id)
                                              .order_by(HandoverHistory.id))]
        out["brief"], out["brief_generated_at"] = h.brief, h.brief_generated_at
    return out


def get_task(db: Session, task_id: int, user: User) -> Task:
    t = db.get(Task, task_id)
    if not t or not svc.can_view_task(user, t):
        raise svc.DomainError("Task not found", 404)
    return t


def get_handover(db: Session, hid: int, user: User) -> Handover:
    h = db.get(Handover, hid)
    if not h or not (user.id in (h.source_id, h.receiver_id, h.created_by_id) or has_perm(user.role, "task:view_all")):
        raise svc.DomainError("Handover not found", 404)
    return h


# --------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        log_event("login_failed", level=logging.WARNING)
        raise svc.DomainError("Invalid credentials", 401)
    log_event("login_ok", user=user.id)
    return {"access_token": create_token(user.id, user.role), "token_type": "bearer",
            "user": {"id": user.id, "name": user.full_name, "role": user.role, "department": user.department}}


@app.get("/users")
def users(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": u.id, "name": u.full_name, "role": u.role, "department": u.department}
            for u in db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.full_name))]


@app.post("/tasks", status_code=201)
def create_task(body: TaskIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = body.model_dump()
    data["due_date"] = naive(data["due_date"])
    return task_out(db, svc.create_task(db, user, data))


@app.get("/tasks")
def list_tasks(db: Session = Depends(get_db), user: User = Depends(current_user),
               status: Optional[str] = None, department: Optional[str] = None, category: Optional[str] = None,
               owner_id: Optional[int] = None, overdue: Optional[bool] = None, mine: bool = False,
               q: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    stmt = select(Task)
    if mine or not has_perm(user.role, "task:view_all"):
        stmt = stmt.where((Task.owner_id == user.id) | (Task.created_by_id == user.id) if not mine
                          else Task.owner_id == user.id)
    for col, val in ((Task.status, status), (Task.department, department), (Task.category, category),
                     (Task.owner_id, owner_id)):
        if val is not None:
            stmt = stmt.where(col == val)
    if q:
        stmt = stmt.where(Task.title.ilike(f"%{q}%"))
    if overdue:
        stmt = stmt.where(Task.due_date < utcnow(), Task.status.in_(OPEN_STATUSES))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Task.due_date.is_(None), Task.due_date, Task.id)
                      .offset((page - 1) * page_size).limit(page_size))
    return {"total": total, "page": page, "items": [task_out(db, t) for t in rows]}


@app.get("/tasks/stats")
def task_stats(db: Session = Depends(get_db), _: User = Depends(require("task:view_all"))):
    by_status = dict(db.execute(select(Task.status, func.count()).group_by(Task.status)).all())
    by_dept = dict(db.execute(select(Task.department, func.count()).group_by(Task.department)).all())
    overdue = db.scalar(select(func.count()).where(Task.due_date < utcnow(), Task.status.in_(OPEN_STATUSES)))
    done = db.execute(select(Task.created_at, TaskOutcome.completed_at)
                      .join(TaskOutcome, TaskOutcome.task_id == Task.id)).all()
    avg_h = round(sum((c - s).total_seconds() for s, c in done) / len(done) / 3600, 1) if done else None
    return {"by_status": by_status, "by_department": by_dept, "overdue": overdue, "avg_completion_hours": avg_h}


@app.get("/tasks/{task_id}")
def task_get(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return task_detail(db, get_task(db, task_id, user))


@app.post("/tasks/{task_id}/status")
def task_status(task_id: int, body: StatusIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return task_out(db, svc.change_status(db, user, get_task(db, task_id, user), body.status, body.comment))


@app.post("/tasks/{task_id}/assign")
def task_assign(task_id: int, body: AssignIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return task_out(db, svc.reassign_task(db, user, get_task(db, task_id, user), body.owner_id, body.reason))


@app.post("/tasks/{task_id}/comments", status_code=201)
def task_comment(task_id: int, body: CommentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = svc.add_comment(db, user, get_task(db, task_id, user), body.body, body.kind)
    return {"id": c.id}


@app.post("/tasks/{task_id}/complete")
def task_complete(task_id: int, body: OutcomeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return task_detail(db, svc.complete_task(db, user, get_task(db, task_id, user), body.model_dump()))


@app.post("/tasks/{task_id}/carry-forward")
def task_carry(task_id: int, body: CarryIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = svc.carry_forward(db, user, get_task(db, task_id, user), body.reference, body.reason,
                          naive(body.review_date), naive(body.new_due_date))
    return task_out(db, t)


@app.post("/handovers", status_code=201)
def handover_create(body: HandoverIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.create_handover(db, user, body.model_dump()), True)


@app.get("/handovers")
def handover_list(db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(Handover).order_by(Handover.id.desc())
    if not has_perm(user.role, "task:view_all"):
        stmt = stmt.where((Handover.source_id == user.id) | (Handover.receiver_id == user.id))
    return [handover_out(db, h) for h in db.scalars(stmt)]


@app.get("/handovers/{hid}")
def handover_get(hid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, get_handover(db, hid, user), True)


@app.get("/handovers/{hid}/eligible-tasks")
def handover_eligible(hid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [task_out(db, t) for t in svc.eligible_tasks(db, get_handover(db, hid, user))]


@app.post("/handovers/{hid}/tasks")
def handover_tasks(hid: int, body: TaskIdsIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.add_handover_tasks(db, user, get_handover(db, hid, user), body.task_ids), True)


@app.post("/handovers/{hid}/submit")
def handover_submit(hid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.submit_handover(db, user, get_handover(db, hid, user)), True)


@app.post("/handovers/{hid}/review")
def handover_review(hid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.review_handover(db, user, get_handover(db, hid, user)), True)


@app.post("/handovers/{hid}/accept")
def handover_accept(hid: int, body: CommentOnlyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.accept_handover(db, user, get_handover(db, hid, user), body.comment), True)


@app.post("/handovers/{hid}/cancel")
def handover_cancel(hid: int, body: CommentOnlyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.cancel_handover(db, user, get_handover(db, hid, user), body.comment), True)


@app.post("/handovers/{hid}/advance")
def handover_advance(hid: int, body: AdvanceIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return handover_out(db, svc.advance_handover(db, user, get_handover(db, hid, user), body.target), True)


@app.post("/handovers/{hid}/brief")
def handover_brief(hid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Never fails because of the AI provider: falls back to a rule-based brief."""
    return ai.generate_handover_brief(db, get_handover(db, hid, user))



# ------------------------------------------------- continuity (special feature)
@app.get("/handovers/{hid}/continuity")
def handover_continuity(hid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    h = get_handover(db, hid, user)
    now, rows = utcnow(), []
    for ht in db.scalars(select(HandoverTask).where(HandoverTask.handover_id == h.id)):
        t = db.get(Task, ht.task_id)
        kinds = [c.kind for c in db.scalars(select(TaskComment).where(TaskComment.task_id == t.id))]
        rows.append({"status": t.status, "has_expected_outcome": bool(t.expected_outcome.strip()),
                     "blocker_count": kinds.count("blocker"), "comment_count": len(kinds),
                     "overdue": bool(t.due_date and t.due_date < now and t.status in OPEN_STATUSES)})
    return score_continuity({"decisions": h.decisions, "notes": h.notes, "pending_issues": h.pending_issues,
                             "documents": h.important_documents}, rows)


# ------------------------------------------------------------- meetings
class MeetingIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    meeting_date: datetime
    department: str = Field(min_length=2)
    participant_ids: list[int] = []
    agenda: str = ""
    notes: str = ""
    previous_meeting_id: Optional[int] = None


class MeetingUpdateIn(BaseModel):
    agenda: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[Literal["SCHEDULED", "HELD", "CLOSED"]] = None


class DecisionIn(BaseModel):
    text: str = Field(min_length=3)


class AttendanceIn(BaseModel):
    attended_ids: list[int]


class ActionItemIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    owner_id: Optional[int] = None
    due_date: Optional[datetime] = None
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class ApproveIn(BaseModel):
    summary: str = ""
    decisions: list[str] = []
    action_items: list[ActionItemIn] = []


class MeetingCarryIn(BaseModel):
    to_meeting_id: int
    reason: str = Field(min_length=3)
    review_date: datetime


def meeting_out(db: Session, m: Meeting, detail: bool = False) -> dict:
    out = {"id": m.id, "ref": mtg.meeting_ref(m), "title": m.title, "meeting_date": m.meeting_date,
           "department": m.department, "status": m.status, "organizer": svc._name(db, m.organizer_id),
           "organizer_id": m.organizer_id, "previous_meeting_id": m.previous_meeting_id}
    if detail:
        prev = db.get(Meeting, m.previous_meeting_id) if m.previous_meeting_id else None
        out.update({"agenda": m.agenda, "notes": m.notes, "decisions": m.decisions or [], "ai_summary": m.ai_summary,
                    "participants": mtg.participants(db, m), "previous_meeting": mtg.meeting_ref(prev) if prev else None,
                    "action_items": [{**task_out(db, t), "origin": o} for t, o in mtg.action_items(db, m)]})
    return out


@app.post("/meetings", status_code=201)
def meeting_create(body: MeetingIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = body.model_dump()
    data["meeting_date"] = naive(data["meeting_date"])
    return meeting_out(db, mtg.create_meeting(db, user, data), True)


@app.get("/meetings")
def meeting_list(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [meeting_out(db, m) for m in db.scalars(select(Meeting).order_by(Meeting.meeting_date.desc()))
            if mtg.can_access(db, user, m)]


@app.get("/meetings/{mid}")
def meeting_get(mid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = mtg.get_meeting(db, mid, user)
    out = meeting_out(db, m, True)
    out["can_edit"] = mtg.can_edit(user, m)
    return out


@app.put("/meetings/{mid}")
def meeting_update(mid: int, body: MeetingUpdateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return meeting_out(db, mtg.update_meeting(db, user, mtg.get_meeting(db, mid, user), body.model_dump()), True)


@app.post("/meetings/{mid}/decisions", status_code=201)
def meeting_decision(mid: int, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return meeting_out(db, mtg.add_decision(db, user, mtg.get_meeting(db, mid, user), body.text), True)


@app.post("/meetings/{mid}/attendance")
def meeting_attendance(mid: int, body: AttendanceIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = mtg.get_meeting(db, mid, user)
    mtg.set_attendance(db, user, m, body.attended_ids)
    return meeting_out(db, m, True)


@app.post("/meetings/{mid}/action-items", status_code=201)
def meeting_action_item(mid: int, body: ActionItemIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = body.model_dump()
    data["due_date"] = naive(data["due_date"])
    return task_out(db, mtg.add_action_item(db, user, mtg.get_meeting(db, mid, user), data))


@app.post("/meetings/{mid}/extract")
def meeting_extract(mid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """AI proposals only. Review, edit and POST to /approve to save."""
    m = mtg.get_meeting(db, mid, user)
    if not m.notes.strip():
        raise svc.DomainError("Add meeting notes or paste a transcript first")
    return ai.extract_meeting(db, m)


@app.post("/meetings/{mid}/approve")
def meeting_approve(mid: int, body: ApproveIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = mtg.get_meeting(db, mid, user)
    items = [{**i.model_dump(), "due_date": naive(i.due_date)} for i in body.action_items]
    mtg.approve_extraction(db, user, m, {"summary": body.summary}, body.decisions, items)
    return meeting_out(db, m, True)


@app.post("/meetings/{mid}/carry-forward")
def meeting_carry(mid: int, body: MeetingCarryIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    src, dst = mtg.get_meeting(db, mid, user), mtg.get_meeting(db, body.to_meeting_id, user)
    return mtg.carry_forward_items(db, user, src, dst, body.reason, naive(body.review_date))


@app.get("/meetings/{mid}/catch-up")
def meeting_catch_up(mid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return ai.catch_up_brief(db, mtg.get_meeting(db, mid, user), user)


# Serve the web UI from the same process (no CORS or npm needed). Must stay LAST so API routes win.
import os  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_root = os.path.dirname(os.path.dirname(__file__))
for _cand in (os.path.join(_root, "..", "frontend"), os.path.join(_root, "frontend"), os.path.join(_root, "static")):
    if os.path.isdir(_cand):
        app.mount("/", StaticFiles(directory=_cand, html=True), name="ui")
        break
