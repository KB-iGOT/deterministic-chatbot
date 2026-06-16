# iGOT Deterministic Chatbot — Developer Guide

iGOT Karmayogi Bharat support chatbot. LangGraph engine, YAML-defined flows, deterministic-first (Mode A/B). Modes C/D (LLM-guided) are wired but Phase 2.

---

> **If you are adding a new support use case:** you only need to write a YAML file in `flows/`. You do not need to touch any Python. See [`flows/README.md`](flows/README.md) for the complete YAML reference.

---

## Table of Contents

1. [Quick start](#1-quick-start)
2. [Project layout](#2-project-layout)
3. [How the engine works](#3-how-the-engine-works)
4. [Adding a new flow](#4-adding-a-new-flow)
5. [Engine internals — when you need Python](#5-engine-internals--when-you-need-python)
6. [Logging](#6-logging)
7. [Testing](#7-testing)
8. [Code conventions](#8-code-conventions)
9. [Troubleshooting](#9-troubleshooting)
10. [Observability (Langfuse)](#10-observability-langfuse)

---

## 1. Quick start

### Option A — Docker (recommended)

```bash
cp .env.example .env          # fill in KARMAYOGI_API_KEY, ZOHO_* creds, GOOGLE_* creds
docker compose up
```

| URL | What |
|-----|------|
| <http://localhost:8000/dev-ui> | Chat widget for testing flows |
| <http://localhost:8000/docs>   | OpenAPI (Swagger) UI |
| <http://localhost:8000/health> | Liveness check |

**API endpoints:**

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | — | Liveness check |
| `POST` | `/ai-chatbot/v1/sessions` | JWT | Start a new session |
| `POST` | `/ai-chatbot/v1/sessions/{id}/turn` | JWT | Send user action, receive bot activities |
| `GET` | `/ai-chatbot/v1/sessions/mine` | JWT | Get caller's active session ID |
| `GET` | `/ai-chatbot/v1/sessions/{id}` | JWT | Restore session state |
| `GET` | `/docs` | — | OpenAPI / Swagger UI |

Hot-reload is active in dev mode — edit YAML or Python, changes apply immediately.

**Reset all data** (wipes DB volumes):
```bash
docker compose down -v
```

---

### Option B — Local Python

**Requirements:** Python 3.11+, Postgres.

```bash
pip install uv
uv sync --dev                  # creates .venv + installs all deps
source .venv/bin/activate
cp .env.example .env           # set POSTGRES_URL, ZOHO_* creds, KARMAYOGI_API_KEY
uvicorn app.main:app --reload --reload-include "*.yaml" --reload-exclude "logs" --port 8000
```

---

### Validate all flows (no server needed)

```bash
source .venv/bin/activate
python -m app.engine.compiler --validate flows/
```

Run this before every commit.

---

### Smoke test a conversation

The API requires an `x-authenticated-user-token` header on every request.

**Dev mode (`AUTH_DISABLED=true` in `.env`):** the header value is used directly as the `user_id`. Pass any UUID — no Keycloak required.

**Production (`AUTH_DISABLED=false`):** pass a real Keycloak JWT. The `sub` claim (`f:<x>:<uuid>`) is automatically extracted as `user_id`.

```bash
# Start a session (dev mode — UUID is used directly as user_id)
SESSION=$(curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"channel": "web", "language": "en"}' | jq -r .session_id)

# Pick "Certificate issue" — choice_id is the flow_id directly
curl -s -X POST "http://localhost:8000/ai-chatbot/v1/sessions/$SESSION/turn" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}' | jq .

# Pick sub-scenario
curl -s -X POST "http://localhost:8000/ai-chatbot/v1/sessions/$SESSION/turn" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"action": "select_choice", "choice_id": "course"}' | jq .
```

**Available flow IDs** (these are the valid `choice_id` values at topic selection):

| choice_id | Flow |
|---|---|
| `CERTIFICATE_DOWNLOAD` | Certificate not generated / wrong name |
| `ACCESS_REVOKED` | MDO access revoked |
| `COURSE_PROGRESS_ISSUE` | Course progress stuck |
| `RESOURCE_NOT_OPENING` | Resource / content not opening |
| `FEEDBACK_RATING_ISSUE` | Feedback / Rating issue |
| `PROFILE_VERIFICATION_DESIGNATION` | Designation / Group not verified |
| `LEADERBOARD_ISSUE` | Leaderboard not showing |
| `BULK_PROFILE_UPDATE` | MDO bulk profile upload |
| `UNENROLL_REQUEST` | Unenroll from course/program/event |
| `WEEKLY_CLAP_ISSUE` | Weekly clap not updated |
| `DOWNLOAD_REPORT_ISSUE` | Unable to download report |
| `KARMA_POINTS_ISSUE` | Karma points issue |

---

## 2. Project layout

```
det-chatbot/
├── flows/                         ← YOU WORK HERE for new use cases
│   ├── README.md                  ← YAML reference for flow authors (start here)
│   ├── mode_b_certificate_download.yaml
│   ├── mode_b_access_revoked.yaml
│   ├── mode_b_course_progress_issue.yaml
│   ├── mode_b_resource_not_opening.yaml
│   ├── mode_b_feedback_rating_issue.yaml
│   ├── mode_b_profile_verification_designation.yaml
│   ├── mode_b_leaderboard.yaml
│   ├── mode_b_bulk_profile_update.yaml
│   ├── mode_b_unenroll_request.yaml
│   ├── mode_b_weekly_clap_issue.yaml
│   ├── mode_b_download_report.yaml
│   ├── mode_b_karma_points_issue.yaml
│   ├── on_hold/                   ← parked flows; never auto-loaded
│   └── _shared/                   ← reusable fragments (imported by flows)
│       ├── _terminal.yaml             satisfied + ticket_raised_end nodes
│       ├── _zoho_ticket.yaml          Zoho Desk ticket block (parameterised)
│       ├── _karmayogi_user.yaml       fetch user profile API call
│       ├── _otp_flow.yaml             OTP send/verify sub-flow (email/mobile update)
│       └── system_messages.yaml       ← bot greeting, error text, persona (edit freely)
│
├── app/
│   ├── main.py                    FastAPI entry point; loads flows + system_messages at startup
│   ├── config.py                  Settings from .env (pydantic-settings)
│   ├── logging_setup.py           Centralised logging: colour console + rotating JSON file
│   │
│   ├── api/
│   │   ├── routes.py              POST /ai-chatbot/v1/sessions, POST .../turn — menu is auto-generated
│   │   ├── auth.py                Keycloak JWT validator; AUTH_DISABLED=true uses token as user_id
│   │   └── schemas.py             Request/response Pydantic models
│   │
│   ├── engine/
│   │   ├── compiler.py            YAML → LangGraph compiler + --validate CLI
│   │   ├── state.py               ConversationState (persisted per session)
│   │   ├── expression.py          Branch rule evaluator (simpleeval)
│   │   ├── template.py            Jinja2 renderer for messages + API requests
│   │   ├── activity.py            Channel-agnostic output types (8 activity types)
│   │   ├── runner.py              Turn runner + translation pipeline
│   │   └── nodes/
│   │       ├── api_call_node.py           type: api_call
│   │       ├── branch_node.py             type: branch
│   │       ├── collect_node.py            type: collect
│   │       ├── message_node.py            type: message
│   │       ├── resolution_node.py         type: resolution
│   │       ├── end_node.py                type: end
│   │       └── transfer_llm_node.py       type: transfer_llm (Mode B only)
│   │
│   ├── services/
│   │   ├── karmayogi.py           Karmayogi HTTP gateway (auth + base URL)
│   │   ├── tracing.py             Langfuse tracing integration (traces + generation spans)
│   │   └── registry.py            DI: karmayogi, zoho_desk_api, llm, translation
│   │
│   └── adapters/
│       ├── zoho.py                Zoho Desk — OAuth2 refresh token, class-level token cache
│       ├── translation.py         Gemini → Google Translate → Bhashini failover
│       └── presidio.py            PII redaction (used by transfer_llm only)
│
├── prompts/
│   └── transfer_llm_summary.jinja    Mode B ticket summary prompt template
│
├── dev_ui/
│   └── index.html                 Single-file chat widget (served at /dev-ui in dev mode)
│
├── scripts/
│   ├── test_local.py              In-process E2E runner (interactive + scripted + API probes)
│   └── llm_judge_runner.py        LLM-as-judge: walks every path, asks Claude to verdict
│
├── tests/
│   ├── engine/                    Unit tests for compiler, expression evaluator, node handlers
│   └── flows/                     Flow-level integration tests
│
├── test_reports/                  ← HTML reports from llm_judge_runner (gitignored)
├── logs/                          ← rotating NDJSON log file (gitignored)
├── Dockerfile / docker-compose.yml / .env.example
└── pyproject.toml
```

---

## 3. How the engine works

```
POST /ai-chatbot/v1/sessions
  → validate JWT (or use token as user_id if AUTH_DISABLED=true)
  → create session (uuid, user_id_hash, channel)
  → show greeting + topic picker (auto-generated from flow YAML metadata)
  → user picks a topic (choice_id == flow_id)
  → engine loads the matching compiled LangGraph
  → runs nodes until a node sets status = AWAITING_USER
  → returns pending_activities[] to client
  → state persisted via LangGraph checkpointer (Postgres)

POST /ai-chatbot/v1/sessions/{id}/turn
  → load checkpoint
  → translate user action → state update
      select_choice → collected._last_choice_id (+ save_to field if configured)
      send_message  → collected.<field> (whichever collect node is active)
      pick_item     → collected.<field> + extra_fields from picker
  → resume LangGraph from checkpoint
  → loop until next AWAITING_USER
  → return activities[]
```

### What each node type does

| Node type | User sees | Silent? |
|---|---|---|
| `message` | Text + optional quick-reply buttons | No |
| `collect` | Text prompt + input field or picker | No |
| `branch` | Nothing — instant routing | Yes |
| `api_call` | Nothing — HTTP call in background | Yes |
| `resolution` | Numbered steps + yes/no follow-up | No |
| `end` | Outcome banner (self_served / ticket_raised / ticket_failed) | No |
| `transfer_llm` | AI-written ticket summary or template fallback | No |

### Services available in YAML (`integration:` key)

| Key | Talks to | Auth |
|---|---|---|
| `karmayogi` | `https://igot.gov.in` | Bearer API key (`KARMAYOGI_API_KEY`) |
| `zoho_desk_api` | Zoho Desk REST API | OAuth2 refresh token (auto-refreshed + cached) |

---

## 4. Adding a new flow

**Step 1 — Write the YAML** in `flows/mode_b_<name>.yaml`:

```yaml
flow_id: MY_NEW_FLOW
flow_type: deterministic_with_llm_fallback
version: 1

metadata:
  menu_label:       "🆕 My new topic"
  menu_group:       "Learning"
  menu_order:       11          # position in topic picker
  category:         course
  classification:   Service Request
  default_priority: P3
  default_severity: Sev 3

enabled_channels: [web]

entry_node: start_node
imports:
  - _terminal
  - fragment: _zoho_ticket
    with:
      cf_category:     course
      cf_sub_category: my_sub_category
      cf_flow_id:      MY_NEW_FLOW

nodes:
  - id: start_node
    type: message
    # ...
```

**Step 2 — Validate:**
```bash
python -m app.engine.compiler --validate flows/
```

**Step 3 — Restart the server.** The menu auto-updates from YAML metadata — no Python changes needed.

> **That's it.** The topic picker, routing, Zoho ticket fragment, and LLM escalation all work automatically.

**Mode selection:**

| When | Use |
|---|---|
| SOP is a pure decision tree, no AI at any step | `deterministic` |
| Same, but want AI-written ticket summary at escalation | `deterministic_with_llm_fallback` |
| User describes issue in free text → AI classifies | Mode C — Phase 2 |
| Fully open-ended (e.g. course recommendation) | Mode D — Phase 2 |

---

## 5. Engine internals — when you need Python

**Most of the time you don't.** Only touch Python for these:

### Adding a new response transform

If a new Karmayogi API returns data in a format not covered by the existing transforms, add to `_TRANSFORMS` in `app/engine/nodes/api_call_node.py`:

```python
def _my_transform(value: Any) -> Any:
    # convert API response value to what branch rules expect
    ...

_TRANSFORMS["my_transform"] = _my_transform
```

Then use in YAML:
```yaml
response_mapping:
  - { from: "$.someField", to: collected.my_field, transform: my_transform }
```

**Existing transforms** (cover all current flows — you probably don't need a new one):

| Name | Input → Output | Use when |
|---|---|---|
| `unix_ms_to_iso` | Unix ms int → ISO-8601 string | `completedOn`, `enrolledDate` |
| `enrollment_status_to_int` | status string → `0/1/2` | branch rules needing integers |
| `extract_incomplete_ids` | `langContentStatus` dict → list of IDs | finding incomplete resources |
| `duration_to_minutes` | seconds (str/int) → float minutes | display duration |
| `detect_scorm` | list of mimeType strings → `true/false` | SCORM resource detection |

### Adding a new integration/service

1. Create `app/services/<name>.py` with `async execute_request(method, url, params, body, headers)`.
2. Register in `app/services/registry.py`.
3. Use as `integration: <name>` in YAML.

The adapter handles only auth + base URL. All endpoint details stay in YAML.

### Adding a new node type (rare)

Sub-class `NodeHandler` in `app/engine/nodes/`, register in `app/engine/nodes/__init__.py`. See `base.py` for the interface contract.

---

## 6. Logging

### Console (always active)

Colour-coded, human-readable. Format:
```
2026-06-03 10:30:15.123  INFO     zoho              Token refreshed. expires_in=3600s
2026-06-03 10:30:15.456  ERROR    api_call          HTTP 422 for POST /tickets
```

### File (optional)

NDJSON, one JSON object per line — easy to ship to Loki / CloudWatch / ELK.

Set `LOG_FILE` in `.env` to enable:
```dotenv
LOG_FILE=logs/igot-chatbot.log    # relative to project root
# or absolute:
LOG_FILE=/var/log/igot-chatbot/igot-chatbot.log
```

Files rotate at 10 MB, 5 backups kept. Override with:
```dotenv
LOG_FILE_MAX_BYTES=10485760
LOG_FILE_BACKUP_COUNT=5
```

### Log level

```dotenv
LOG_LEVEL=INFO     # DEBUG | INFO | WARNING | ERROR
```

| Level | When to use |
|---|---|
| `DEBUG` | Local dev — shows token reuse, Jinja render output, node transitions |
| `INFO` | Normal (default) — token refresh, tickets raised, flows loaded, HTTP 200 |
| `WARNING` | Unexpected but recoverable — 401 force-refresh, stub mode |
| `ERROR` | Needs attention — HTTP errors, ticket failures, render errors |

### Noisy loggers suppressed

`httpx`, `httpcore`, `langchain`, `langgraph`, `openai`, `google`, `vertexai`, `anthropic`, `watchfiles`, `grpc`, `urllib3` — all set to WARNING. Only `uvicorn.access` and `uvicorn.error` stay at INFO.

### Session-scoped logging

Use `SessionLogger` in Python code to prefix every line with session + flow context:

```python
from app.logging_setup import SessionLogger

slog = SessionLogger(session_id="abc123", flow_id="CERTIFICATE_DOWNLOAD")
slog.info("User selected option", node="ask_cert_type")
# → INFO  session  session=abc123  flow=CERTIFICATE_DOWNLOAD  User selected option  node=ask_cert_type
```

### Activity / audit log

Every significant user action emits a structured `[activity]` event at `INFO` level in the JSON log file. These events are tagged with `"event_type": "activity"` and are distinct from operational logs.

**Event types:**

| event | Emitted when |
|---|---|
| `session_start` | A new session is created (`POST /ai-chatbot/v1/sessions`) |
| `topic_selected` | User picks a flow from the topic menu |
| `user_turn` | User sends any action to a running flow |
| `flow_ended` | A flow reaches an `end` node (outcome captured) |

**Example log lines (pretty-printed for readability):**

```json
{"timestamp": "2026-06-03T10:31:00.123Z", "level": "INFO", "logger": "activity",
 "event_type": "activity", "event": "session_start",
 "session_id": "e3b7c1a2-...", "user_id_hash": "a1b2c3...", "channel": "web", "language": "en"}

{"timestamp": "2026-06-03T10:31:05.456Z", "level": "INFO", "logger": "activity",
 "event_type": "activity", "event": "topic_selected",
 "session_id": "e3b7c1a2-...", "flow_id": "CERTIFICATE_DOWNLOAD"}

{"timestamp": "2026-06-03T10:31:12.789Z", "level": "INFO", "logger": "activity",
 "event_type": "activity", "event": "user_turn",
 "session_id": "e3b7c1a2-...", "flow_id": "CERTIFICATE_DOWNLOAD",
 "action": "select_choice", "choice_id": "course"}

{"timestamp": "2026-06-03T10:31:45.001Z", "level": "INFO", "logger": "activity",
 "event_type": "activity", "event": "flow_ended",
 "session_id": "e3b7c1a2-...", "flow_id": "CERTIFICATE_DOWNLOAD", "outcome": "ticket_raised"}
```

**Querying activity events** (from `logs/igot-chatbot.log`):

```bash
# All session starts today
grep '"event":"session_start"' logs/igot-chatbot.log | jq .

# All flows that raised a ticket
grep '"event":"flow_ended"' logs/igot-chatbot.log | jq 'select(.outcome=="ticket_raised")'

# Activity funnel for a specific session
grep '"session_id":"e3b7c1a2-..."' logs/igot-chatbot.log | grep '"event_type":"activity"' | jq .
```

---

## 7. Testing

### Unit + integration tests (pytest)

```bash
pytest                             # all tests
pytest tests/engine/               # engine unit tests only
pytest tests/flows/                # flow-level integration tests
pytest -k "cert"                   # filter by name
```

**Writing a flow test:**

```python
@pytest.mark.asyncio
async def test_certificate_course_path(mocked_services):
    compiler = FlowCompiler(services=mocked_services)
    flow = compiler.load_flow(Path("flows/mode_b_certificate_download.yaml"))
    graph = compiler.compile_flow(flow)
    state = initial_state(session_id=uuid4(), user_id_hash="test")
    state_dict = state.model_dump(mode="json")
    state_dict["collected"]["cert_type"] = "course"
    result = await graph.ainvoke(state_dict, {"configurable": {"thread_id": "test"}})
    assert result["current_node"] == "ask_course_picker"
```

---

### `test_local.py` — in-process E2E runner (no server needed)

Runs conversations through the live LangGraph engine in-process, with real or stubbed services.

```bash
python scripts/test_local.py                   # interactive: step through a flow manually
python scripts/test_local.py --auto            # scripted: Certificate → C2 → resolved path
python scripts/test_local.py --karmayogi       # probe real Karmayogi enrollment + user APIs
python scripts/test_local.py --zoho            # create a real Zoho Desk test ticket
python scripts/test_local.py --translate       # test Gemini → Google Translate fallback
python scripts/test_local.py --all             # run all of the above in sequence
```

> `--zoho` creates a real ticket on every run — only use when explicitly testing Zoho.

---

### `llm_judge_runner.py` — LLM-as-judge quality evaluation

Exhaustively walks **every user-choice path** through a YAML flow, simulates each conversation against the live engine, then asks Claude to act as a QA judge and evaluate correctness against the SOP document. Produces a standalone HTML report.

```bash
# Test one flow
python scripts/llm_judge_runner.py --flow CERTIFICATE_DOWNLOAD

# Test all active flows (can take several minutes)
python scripts/llm_judge_runner.py --all

# Custom report output path
python scripts/llm_judge_runner.py --flow CERTIFICATE_DOWNLOAD --output reports/cert.html

# Dry run — list all paths without running conversations
python scripts/llm_judge_runner.py --flow LEADERBOARD_ISSUE --dry-run
```

**Requires** `ANTHROPIC_API_KEY` in `.env` (used to call Claude as the judge).
**Requires** `IGOT_TEST_USER_ID` in `.env` (a real Karmayogi user ID for API calls).

**What the judge evaluates:**
- Correctness against the SOP document (ground truth — not the YAML)
- Quality of bot messages (clarity, completeness, tone)
- Correct routing for each user choice
- Appropriate escalation to Zoho ticket

**Verdict levels:** `PASS` · `WARN` · `FAIL` · `ERROR`

**HTML report** is saved to `test_reports/` (gitignored). Each report contains:
- Summary table with verdict counts
- Per-path conversation transcript
- Claude's detailed verdict and fix suggestions for each path

**SOP files** live at `../reference/SOPs_md/` (one level above the project root). The judge uses these as the source of truth — if a flow says something that contradicts the SOP, that is a FAIL. If a flow is missing a SOP, the judge falls back to general quality principles.

**Run after writing a new flow** to catch routing gaps and message quality issues before release:

```bash
python scripts/llm_judge_runner.py --flow MY_NEW_FLOW
open test_reports/MY_NEW_FLOW_*.html
```

---

## 8. Code conventions

- **Formatter / linter:** `ruff format && ruff check`
- **State updates:** return `{"collected": {**state.collected, "key": val}}` — never mutate `state` directly
- **Async:** all I/O is `async`. No blocking `requests` calls.
- **Secrets:** never log them, never commit them. `.env` is gitignored.
- **PII:** `user_id` is HMAC-hashed at the API boundary. Raw JWTs, phone numbers, and emails never reach the LLM — Presidio redacts before `transfer_llm` calls.
- **LLM imports:** only `transfer_llm`, `llm_choose`, `open_llm_subgraph` nodes may import `app.adapters.llm`.
- **Commit prefix:** `[flows]`, `[engine]`, `[api]`, `[fix]`
- **Branch naming:** `feat/<desc>` / `fix/<ticket>`

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Validation fails — "dangling edge" | `next:` / `on_success:` references an id that doesn't exist | Check spelling; every edge target must be a node `id` in the same flow |
| "LLM node in deterministic flow" | `flow_type: deterministic` has a `transfer_llm` node | Change `flow_type` to `deterministic_with_llm_fallback` |
| `== null` not matching in branch rule | `simpleeval` uses Python syntax | Use `== None` not `== null` |
| `integration: zoho` not found | Wrong service key | Use `integration: zoho_desk_api` |
| Flow appears in `--validate` but not in menu | `metadata.menu_label` not set | Add `menu_label:` to the flow's `metadata:` block |
| `401 Unauthorized` on `/ai-chatbot/v1/sessions` | Missing `x-authenticated-user-token` header | Add `-H "x-authenticated-user-token: <uuid>"` (any UUID when `AUTH_DISABLED=true`) |
| `401 Unauthorized` in production | Invalid or expired Keycloak JWT | Refresh the token from the iGOT portal; ensure `AUTH_DISABLED=false` in `.env` |
| Vertex AI `403 Permission denied` | Missing IAM role | Grant `Vertex AI User` in GCP; check `GOOGLE_APPLICATION_CREDENTIALS` |
| Zoho `422 Unprocessable Entity` | Missing required Zoho field | Check `ZOHO_DEPARTMENT_ID` is set in `.env`; check `contact` block in `_zoho_ticket.yaml` |
| Zoho `401` repeated | OAuth token expired | Adapter auto-refreshes up to 3 times; if persists, check `ZOHO_REFRESH_TOKEN` in `.env` |
| Ticket created but shows `ticket_failed` banner | `ticket_failed` routed to wrong `end` node | Ensure `ticket_failed` → `ticket_failed_end` (outcome: `ticket_failed`), not `ticket_raised_end` |
| `pending_activities` empty | Engine ran but no UI-emitting node fired | Check `current_node` in debug panel; add logging in the node's `run()` |
| `ImportError: cannot import name 'Self'` | Python 3.10 (needs 3.11+) | `uv venv .venv --python 3.11 && uv sync` |
| Flow compiles but skipped at startup | One broken YAML prevents that flow | Check logs for `⚠️ Skipping flow` lines; run `--validate` to find the error |
| `watchfiles` log spam in terminal | Logger not suppressed | Should be suppressed by `logging_setup.py` — restart the server |
| Log file not created | `LOG_FILE` not set | Add `LOG_FILE=logs/igot-chatbot.log` to `.env` |
| `LANGFUSE_ENABLED=true` but no traces appear | Missing Langfuse keys | Ensure `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set in `.env`; check logs for `LangfuseWarning` |

---

## 10. Observability (Langfuse)

iGOT Deterministic Chatbot integrates with [Langfuse](https://langfuse.com) for distributed tracing of LLM calls and conversation turns. Tracing is opt-in and has zero overhead when disabled.

### Enabling Langfuse

Add to `.env`:

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com   # or your self-hosted URL
```

Leave `LANGFUSE_ENABLED=false` (the default) to disable tracing entirely. If `LANGFUSE_ENABLED=true` but the keys are missing, the app logs a warning at startup and continues without tracing.

### What is traced

| What | How it appears in Langfuse |
|---|---|
| One HTTP turn (`POST /ai-chatbot/v1/sessions` or `POST .../turn`) | One **Trace** |
| `user_id` and `session_id` | Set as `user_id` and `session_id` on every trace — use these to filter |
| LLM calls (ticket summary, `transfer_llm` node) | **Generation** spans nested inside the turn trace |
| Flow and node context | Span metadata (`flow_id`, `node_id`, `action`) |

### Langfuse Sessions view

Because every trace carries the same `session_id`, the Langfuse **Sessions** tab groups all turns of a single conversation into one timeline. This makes it easy to replay a full support interaction and see exactly what the LLM was asked and answered at each step.

### Sample rate

```dotenv
LANGFUSE_SAMPLE_RATE=1.0    # local dev: trace every request
LANGFUSE_SAMPLE_RATE=0.1    # production: trace 10% of requests
```

The implementation lives in `app/services/tracing.py`. It wraps the Langfuse Python SDK and is injected via the service registry — flow YAML and node code do not import Langfuse directly.

### Frontend / integration contract

See [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md) for the complete frontend integration specification, including session lifecycle and header requirements.
