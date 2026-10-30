"""Demo users (synthetic). Run: python -m app.seed   Password for all: Demo@1234"""
from sqlalchemy import select

from .db import Base, SessionLocal, engine
from datetime import timedelta

from .models import Task, TaskHistory, User, utcnow
from .security import hash_password

DEMO = [("admin", "Admin User", "admin", "IT"), ("manager1", "Meera Iyer", "manager", "Claims"),
        ("coord1", "Karan Shah", "coordinator", "Billing"), ("billing1", "Asha Nair", "billing", "Billing"),
        ("billing2", "Ravi Patil", "billing", "Billing"), ("claims1", "Sneha Rao", "claims", "Claims"),
        ("analyst1", "Dev Menon", "analyst", "Analytics")]


def seed_users(db, password: str = "Demo@1234"):
    pw = hash_password(password)
    for username, name, role, dept in DEMO:
        if not db.scalar(select(User).where(User.username == username)):
            db.add(User(username=username, full_name=name, role=role, department=dept, password_hash=pw))
    db.commit()


def seed_tasks(db):
    """Synthetic demo tasks for billing1 (overdue, blocked, in progress, open)."""
    if db.scalar(select(Task)):
        return
    owner = db.scalar(select(User).where(User.username == "billing1"))
    now = utcnow()
    demo = [("Collect missing discharge summary for CLM-1024", "Insurance claim", "high", "BLOCKED", -2, "CLM-1024"),
            ("Submit final bill for bed 12 patient", "Billing", "urgent", "IN_PROGRESS", -1, "CLM-1031"),
            ("Chase pre-authorization for knee replacement", "Pre-authorization", "high", "ASSIGNED", 1, "CLM-1040"),
            ("Reconcile package mismatch on invoice INV-882", "Billing", "medium", "ASSIGNED", 3, ""),
            ("Update discharge checklist for ward 4", "Discharge", "low", "OPEN", 7, "")]
    for title, cat, pri, status, days, claim in demo:
        t = Task(title=title, department="Billing", category=cat, priority=pri, status=status,
                 owner_id=owner.id, created_by_id=owner.id, due_date=now + timedelta(days=days),
                 related_claim_ref=claim, description="Synthetic demo task")
        db.add(t)
        db.flush()
        db.add(TaskHistory(task_id=t.id, event="CREATED", to_value=status, actor_id=owner.id))
    db.commit()


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        seed_users(s)
        seed_tasks(s)
    print("Seeded synthetic demo users (password Demo@1234)")
