"""Pure-Python helpers (no framework imports) for meeting extraction rules and AI-output checks."""
import re
from datetime import datetime

DECISION_RE = re.compile(r"^\s*(?:decision|decided|agreed)\s*[:\-]\s*(.+?)\s*$", re.I)
ACTION_RE = re.compile(r"^\s*(?:action(?:\s+item)?|todo|follow[- ]?up)\s*[:\-]\s*(.+?)\s*$", re.I)
DATE_RE = re.compile(r"\s*\b(?:by|due(?:\s+on)?)\s+(\d{4}-\d{2}-\d{2})\b", re.I)


def parse_iso_date(value) -> str | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        return None


def match_owner(text: str, participants: list[dict]):
    """Owner only if a participant is named at the start ('Ravi Patil to ...', 'Ravi will ...')."""
    for p in participants:
        for name in (p["name"], p["name"].split()[0]):
            m = re.match(rf"^{re.escape(name)}\s+(?:to|will)\s+(.+)$", text, re.I)
            if m:
                return p, m.group(1).strip()
    return None, text


def rule_extract(notes: str, participants: list[dict], title: str = "Meeting") -> dict:
    decisions, items, points = [], [], []
    for line in (notes or "").splitlines():
        if not line.strip():
            continue
        if (m := DECISION_RE.match(line)):
            decisions.append(m.group(1))
        elif (m := ACTION_RE.match(line)):
            text = m.group(1)
            due = None
            if (d := DATE_RE.search(text)):
                due = parse_iso_date(d.group(1))
                text = DATE_RE.sub("", text).strip()
            owner, task_title = match_owner(text, participants)
            items.append({"title": task_title, "owner_id": owner["id"] if owner else None,
                          "owner_name": owner["name"] if owner else None, "due_date": due,
                          "needs_confirmation": owner is None or due is None})
        else:
            points.append(line.strip())
    unresolved = [f"'{i['title']}' needs " + " and ".join(
        x for x, missing in (("an owner", i["owner_id"] is None), ("a due date", i["due_date"] is None)) if missing)
        for i in items if i["needs_confirmation"]]
    return {"summary": f"{title}: {len(decisions)} decision(s) and {len(items)} action item(s) found in the notes."
                       if (decisions or items) else f"{title}: no explicit decisions or action items found in the notes.",
            "key_points": points[:5], "decisions": decisions, "action_items": items, "unresolved": unresolved}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3]


def grounded(text: str, source: str, threshold: float = 0.6) -> bool:
    """Cheap anti-hallucination check: most significant words must appear in the source text."""
    words, pool = _words(text), set(_words(source))
    return bool(words) and sum(w in pool for w in words) / len(words) >= threshold


def clean_llm_extraction(data: dict, participants: list[dict], notes: str) -> dict:
    """Validate LLM output. Unknown owners -> None, bad dates -> None, ungrounded items dropped."""
    by_name = {p["name"].lower(): p for p in participants}
    dropped = 0
    decisions = []
    for d in data.get("decisions") or []:
        if isinstance(d, str) and grounded(d, notes):
            decisions.append(d)
        else:
            dropped += 1
    items = []
    for a in data.get("action_items") or []:
        if not isinstance(a, dict) or not isinstance(a.get("title"), str) or not grounded(a["title"], notes):
            dropped += 1
            continue
        owner = by_name.get(str(a.get("owner_name") or "").lower())
        due = parse_iso_date(a.get("due_date")) if a.get("due_date") else None
        if due and due not in notes:
            due = None  # only accept dates literally present in the notes
        items.append({"title": a["title"], "owner_id": owner["id"] if owner else None,
                      "owner_name": owner["name"] if owner else None, "due_date": due,
                      "needs_confirmation": owner is None or due is None})
    unresolved = [u for u in (data.get("unresolved") or []) if isinstance(u, str)]
    if dropped:
        unresolved.append(f"{dropped} AI-suggested item(s) were dropped because they were not found in the notes.")
    return {"summary": str(data.get("summary") or ""), "key_points": [k for k in data.get("key_points") or [] if isinstance(k, str)],
            "decisions": decisions, "action_items": items, "unresolved": unresolved}
