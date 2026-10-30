# ApolloCare Intelligence: AI Knowledge & Task Management

Hospital-operations app for the DigiPlus assessment ("AI-Powered Knowledge & Tasks Management Platform"),
built on the Apollo Hospitals DMBI case study. **All data is synthetic; no Apollo Hospitals system is accessed.**

Stack: FastAPI + SQLAlchemy 2 (SQLite locally, PostgreSQL in Docker/prod), JWT auth, plain-JS web UI served by the API
(one process, one URL), OpenAI for AI features with deterministic fallbacks.

## Run locally (Windows PowerShell)
    cd backend
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    Copy-Item .env.example .env
    python -m app.seed
    python -m pytest -q
    python -m uvicorn app.main:app --reload
Open http://localhost:8000 (API docs at /docs). macOS/Linux: `source .venv/bin/activate` and `cp`.
Demo logins (password `Demo@1234`): `billing1`, `billing2`, `claims1`, `coord1`, `manager1`, `analyst1`, `admin`.
Docker: `docker compose up --build`. Deployment: see `DEPLOY.md`.

## What is implemented (maps to the assessment outcomes)
| Requirement | Where |
|---|---|
| Create/persist meetings and work transfers (agenda, participants, notes, decisions, action items) | Meetings & Decisions tab, Work Handover Hub |
| Lifecycle + track every action item (owner, due, status) until closed | Tasks: Open > Assigned > In Progress > Blocked > Pending Approval > Completed/Cancelled; history is append-only |
| Carry open items forward, keeping history | `POST /meetings/{id}/carry-forward`, `POST /tasks/{id}/carry-forward`: same task ID, origin ref + original due date preserved |
| AI: raw notes to summary, decisions, action items, owners | `POST /meetings/{id}/extract` returns proposals; a human edits and approves; nothing is saved before |
| AI catch-up brief for someone who missed a meeting / is taking over | `GET /meetings/{id}/catch-up`, `POST /handovers/{id}/brief` |
| Record the outcome of each item | Completing a task requires resolution summary, actions performed and final result |
| Validation and error handling | Pydantic schemas, state-machine checks, 4xx JSON errors carrying a `request_id` |
| Knowledge base linking | Not built yet (see limitations) |

Work transfer: select open tasks, submit, manager review, receiver accepts. Tasks are locked while a handover is pending and
change owner only on acceptance. The task row never changes ID; a `TRANSFERRED` history entry records previous and new owner.

## AI configuration
Set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`, default `gpt-4o-mini`, `AI_TIMEOUT_SECONDS`) in `.env`.
- Prompts live in `app/ai.py`. Input is JSON built from database records only; prompts tell the model that record text is untrusted data.
- Output is requested as JSON and **validated** (`app/textrules.py`): owners must be real participants, dates must literally appear in the notes,
  and decisions/actions whose wording is not found in the notes are dropped and reported as unresolved. This is the guard against invented decisions.
- **Failure handling:** timeout, bad JSON, quota errors or a missing key all fall back to rule-based output (`generated_by: "rules"`).
  AI is never in the write path: tasks, assignment, handover, status and history work with the LLM completely down (tested).
- The AI never changes ownership, closes tasks or deletes anything; humans approve every AI-derived record.

## Data source
Synthetic demo users and five demo tasks from `app/seed.py`; everything else is entered through the app. Meeting input is typed or pasted text
(transcripts); audio transcription is not implemented.

## Logging
`app/logging_setup.py`: JSON lines to console and `logs/app.log` (rotating, 5 MB x 3). Every request gets a `request_id`
(accepts an incoming `X-Request-ID`, returns it in the response header and in error bodies) and the `user_id`.
Key events: `task_created`, `task_status_changed`, `task_reassigned`, `task_completed`, `task_carried_forward`, `handover_created`,
`handover_accepted`, `meeting_created`, `meeting_extraction_approved`, `login_failed`. AI calls log `source`, `outcome` and `latency_ms`
(`ai_meeting_extract`, `ai_handover_brief`, `ai_catch_up_brief`). Unhandled errors log a stack trace. Passwords, tokens and API keys are never logged.

## Special feature: Handover Continuity Score
`GET /handovers/{id}/continuity` (`app/continuity.py`, shown in the handover screen). The assessment's core problem is lost context.
Instead of only summarising a handover after the fact, the score tells the *outgoing* employee, before submitting, whether the receiver could
continue without phoning them: decisions recorded, notes, pending issues, documents, expected outcomes, an explanation on every blocked task,
progress notes, and reasons for overdue tasks. It returns 0-100, a context-loss risk level and the exact gaps to fix. It is deterministic and
explainable (no LLM), so it works offline and a reviewer can see why a score was given.

## Approach and assumptions
- Action items **are** tasks (one table), linked to meetings through `meeting_action_items`; carrying forward adds a link instead of copying.
- Task and handover rules live in `app/services.py` and `app/meetings.py`, with no AI imports, so core flows cannot be broken by AI failures.
- Authorization is enforced in the API (`app/security.py` role-permission map), not only in the UI.
- Assumes one organisation, one time zone; datetimes stored as naive UTC.

## Known limitations
- No knowledge-base / document upload or RAG yet; "link items to documents" is not implemented.
- No notifications or background jobs; no audio transcription; no Alembic migrations (tables are created at startup).
- Patient, billing and claims modules, discharge-summary AI and ML predictions from the larger ApolloCare spec are not built yet.
- UI is plain HTML/JS rather than React; basic styling, no pagination controls in the task table (API supports them).
- The tests could not be executed in the build environment (no network), so run `python -m pytest -q` first.
