from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    """Naive UTC everywhere so SQLite and Postgres behave the same."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


TASK_STATUSES = ["OPEN", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "PENDING_APPROVAL", "COMPLETED", "CANCELLED"]
OPEN_STATUSES = ["OPEN", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "PENDING_APPROVAL"]
CLOSED_STATUSES = ["COMPLETED", "CANCELLED"]
CATEGORIES = ["Billing", "Insurance claim", "Pre-authorization", "Discharge", "Patient documentation",
              "Diagnostics", "Administration", "General operations"]
HANDOVER_STATUSES = ["DRAFT", "PENDING_REVIEW", "AWAITING_ACCEPTANCE", "ACCEPTED", "IN_PROGRESS",
                     "COMPLETED", "CANCELLED"]


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(30))
    department: Mapped[str] = mapped_column(String(60), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)  # never changes, even across handovers
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(String(60), index=True)
    category: Mapped[str] = mapped_column(String(40))
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expected_outcome: Mapped[str] = mapped_column(Text, default="")
    related_patient_ref: Mapped[str] = mapped_column(String(60), default="")
    related_claim_ref: Mapped[str] = mapped_column(String(60), default="")
    # handover lock: set while the task is part of an unaccepted handover
    pending_handover_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # carry-forward trace
    carried_from_ref: Mapped[str] = mapped_column(String(80), default="")
    original_due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    carry_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TaskHistory(Base):
    """Append-only. Nothing in the app updates or deletes rows here."""
    __tablename__ = "task_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    event: Mapped[str] = mapped_column(String(30))
    from_value: Mapped[str] = mapped_column(String(200), default="")
    to_value: Mapped[str] = mapped_column(String(200), default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    comment: Mapped[str] = mapped_column(Text, default="")
    handover_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskComment(Base):
    __tablename__ = "task_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(15), default="comment")  # comment|progress|blocker|resolution
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskOutcome(Base):
    __tablename__ = "task_outcomes"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), unique=True)
    resolution_summary: Mapped[str] = mapped_column(Text)
    actions_performed: Mapped[str] = mapped_column(Text)
    final_result: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    completed_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Handover(Base):
    __tablename__ = "handovers"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    department: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str] = mapped_column(Text, default="")
    decisions: Mapped[str] = mapped_column(Text, default="")
    pending_issues: Mapped[str] = mapped_column(Text, default="")
    important_documents: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(25), default="DRAFT", index=True)
    ack_comment: Mapped[str] = mapped_column(Text, default="")
    brief: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    brief_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HandoverTask(Base):
    __tablename__ = "handover_tasks"
    __table_args__ = (UniqueConstraint("handover_id", "task_id"), Index("ix_ht_task", "task_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey("handovers.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))  # same task, never a copy
    previous_owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    new_owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    transferred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str] = mapped_column(String(15), default="pending")  # pending|transferred|skipped


class HandoverHistory(Base):
    __tablename__ = "handover_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey("handovers.id"), index=True)
    event: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    meeting_date: Mapped[datetime] = mapped_column(DateTime)
    department: Mapped[str] = mapped_column(String(60))
    organizer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    agenda: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")  # pasted transcript/text notes (no audio transcription)
    decisions: Mapped[list] = mapped_column(JSON, default=list)  # [{text, by, at}]
    previous_meeting_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="SCHEDULED")  # SCHEDULED|HELD|CLOSED
    ai_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    attended: Mapped[bool] = mapped_column(default=True)


class MeetingActionItem(Base):
    """Links the SAME task to every meeting it appears in: created there, or carried forward."""
    __tablename__ = "meeting_action_items"
    __table_args__ = (UniqueConstraint("meeting_id", "task_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    origin: Mapped[str] = mapped_column(String(10), default="created")  # created|carried
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
