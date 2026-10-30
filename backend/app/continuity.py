"""Special feature: Handover Continuity Score (0-100). Deterministic and explainable:
it measures whether the receiving employee could continue the work WITHOUT phoning the previous owner."""


def score_continuity(h: dict, tasks: list[dict]) -> dict:
    n = len(tasks)

    def share(pred, pool):
        return 1.0 if not pool else sum(1 for t in pool if pred(t)) / len(pool)

    blocked = [t for t in tasks if t["status"] == "BLOCKED"]
    overdue = [t for t in tasks if t["overdue"]]
    checks = [
        ("Decisions recorded", 20, 1.0 if h["decisions"].strip() else 0.0, "Write down decisions already made (or state that none were made)."),
        ("Handover notes written", 15, 1.0 if h["notes"].strip() else 0.0, "Add notes: context the receiver cannot see in the task list."),
        ("Pending issues listed", 10, 1.0 if h["pending_issues"].strip() else 0.0, "List pending issues, or write 'none'."),
        ("Important documents attached", 10, 1.0 if h["documents"] else 0.0, "Reference the SOPs/documents the receiver should read."),
        ("Tasks have expected outcomes", 10, share(lambda t: t["has_expected_outcome"], tasks),
         "Fill in the expected outcome on each task."),
        ("Every blocked task explains its blocker", 15, share(lambda t: t["blocker_count"] > 0, blocked),
         "Add a blocker comment (who/what is being waited on) to each blocked task."),
        ("Tasks have at least one progress note", 10, share(lambda t: t["comment_count"] > 0, tasks),
         "Add a progress comment to tasks that have none."),
        ("Overdue tasks are explained", 10, share(lambda t: t["comment_count"] > 0, overdue),
         "Add a comment explaining why each overdue task slipped."),
    ]
    out, total = [], 0
    for name, pts, frac, fix in checks:
        earned = round(pts * frac)
        total += earned
        out.append({"check": name, "points": pts, "earned": earned, "gap": None if earned == pts else fix})
    level = "Low" if total >= 80 else "Medium" if total >= 50 else "High"
    return {"score": total, "context_loss_risk": level, "tasks_checked": n, "checks": out,
            "gaps": [c["gap"] for c in out if c["gap"]]}
