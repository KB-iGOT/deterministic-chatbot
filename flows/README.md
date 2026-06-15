# Flows — YAML Reference

Each `.yaml` file in this directory defines one support use case. The engine reads and executes it directly — **no Python changes needed** to add, edit, or hide a flow.

---

## Table of Contents

1. [Top-level structure](#1-top-level-structure)
2. [Metadata block — menu + Zoho](#2-metadata-block--menu--zoho)
3. [Node types](#3-node-types)
   - [message](#message)
   - [collect — text field](#collect--text-field)
   - [collect — dynamic picker from API](#collect--dynamic-picker-from-api)
   - [branch](#branch)
   - [api_call](#api_call)
   - [resolution](#resolution)
   - [increment_and_branch](#increment_and_branch)
   - [transfer_llm (Mode B)](#transfer_llm-mode-b-only)
   - [end](#end)
4. [Template variables (ctx and env)](#4-template-variables-ctx-and-env)
5. [Response mapping directives](#5-response-mapping-directives)
6. [Branch rule expressions](#6-branch-rule-expressions)
7. [Imports — reusing shared fragments](#7-imports--reusing-shared-fragments)
8. [Adding a flow to the chat menu](#8-adding-a-flow-to-the-chat-menu)
9. [Bot persona and system messages](#9-bot-persona-and-system-messages)
10. [Validation](#10-validation)
11. [Common mistakes](#11-common-mistakes)

---

## 1. Top-level structure

```yaml
flow_id: CERTIFICATE_DOWNLOAD          # unique SCREAMING_SNAKE_CASE id
flow_type: deterministic               # deterministic | deterministic_with_llm_fallback
version: 1

metadata:                              # see section 2
  category:         course
  classification:   Query
  default_priority: P4
  default_severity: Sev 4
  menu_label:       "🎓 Certificate issue"
  menu_group:       "Learning"
  menu_order:       1

enabled_channels: [web]               # web is the only active channel right now

entry_node: ask_certificate_issue      # id of the first node to execute

nodes:
  - id: ask_certificate_issue
    type: message
    # ...
```

**`flow_type` quick guide:**

| When | Use |
|---|---|
| SOP is a pure decision tree, no AI needed | `deterministic` |
| Same, but want AI-written ticket summary at escalation | `deterministic_with_llm_fallback` |

All current flows are `deterministic_with_llm_fallback` so the LLM can write better ticket descriptions during escalation.

---

## 2. Metadata block — menu + Zoho

```yaml
metadata:
  # ── Chat menu ──────────────────────────────────────────────────────────
  menu_label:       "🎓 Certificate issue"   # button text shown to user (required to appear in menu)
  menu_group:       "Learning"               # logical group label (Account & Login / Learning / Profile / Reports & Data)
  menu_order:       1                        # sort order in menu (lower = higher up)
  menu_hidden:      false                    # true → hides from menu but still callable via API
  enabled:          true                     # false → blocked at API level (WIP / paused flows)

  # ── Zoho Desk defaults (used by _zoho_ticket fragment) ─────────────────
  category:         course                  # Zoho top-level ticket category
  classification:   Query                   # Service Request | Query | Incident
  default_priority: P4                      # P1 (critical) … P5 (low)
  default_severity: Sev 4
  default_portal:   Learner Portal
```

**Menu and access rules:**

| Field | Value | Menu visible | API accessible |
|---|---|---|---|
| `enabled` | `true` (default) | depends on `menu_label` / `menu_hidden` | ✅ yes |
| `enabled` | `false` | ❌ no | ❌ no — router returns "unknown topic" |
| `menu_hidden` | `false` (default) | ✅ if `menu_label` set | ✅ yes |
| `menu_hidden` | `true` | ❌ no | ✅ yes (internal / dev-only flows) |

- A flow only appears in the topic picker if `menu_label` is set **and** `menu_hidden` is not `true` **and** `enabled` is not `false`.
- Flows in `flows/on_hold/` are not auto-discovered — they are never shown.
- `menu_hidden: true` — hides from menu but keeps the flow callable via the raw API (useful for dev testing without offering it to users).
- `enabled: false` — disables the flow entirely; it is still compiled (so YAML errors surface at startup) but the router rejects it as if it did not exist. Use this for WIP flows or flows you want to pause without deleting.

---

## 3. Node types

### message

Sends text and optional quick-reply buttons.

```yaml
- id: ask_certificate_issue
  type: message
  prompt:
    text: |
      What's the issue with your certificate?
  quick_replies:
    - { id: C1, label: "Certificate not generated / not received", icon: "🎓" }
    - { id: C3, label: "Incorrect name on certificate",            icon: "✏️" }
  disable_input: true        # forces the user to pick a button (cannot type free text)
  on_reply:
    save_to: collected.sub_scenario   # stores the chosen button id into ctx.collected
    next:    branch_on_issue          # unconditional next node after any button
```

When different buttons should route to different nodes, use a choice map instead:

```yaml
  on_reply:
    YES: end_resolved
    NO:  collect_email_for_ticket
```

> **Note:** `icon:` is a web-UI decoration — shown on the button face. Omit if you don't need one.
> `dtmf:` and `spoken_label:` fields are **not used** — voice is disabled; do not add them.

---

### collect — text field

Prompts the user to type a value and stores it.

```yaml
- id: collect_email
  type: collect
  prompt:
    text: "Please enter your registered email address."
  field:
    name: collected.user_email   # stored at ctx.collected.user_email
    type: email                  # text | email | date — controls validation
    placeholder: "name@example.com"
  next: confirm_and_send
```

**Multi-field sequential collect** (prompts one field at a time in order):

```yaml
- id: collect_name_and_org
  type: collect
  prompts:
    - { field: collected.full_name,  text: "What is your full name?",        type: text }
    - { field: collected.org_name,   text: "What organisation do you work for?", type: text }
  next: branch_after_collect
```

---

### collect — dynamic picker from API

Renders a searchable list populated from a live API call.

```yaml
- id: pick_enrolled_course
  type: collect
  prompt:
    text: "Please select the course for which you haven't received the certificate."
  field:
    name: collected.course_id
    type: select
  dynamic_options:
    source:      api
    integration: karmayogi
    request:
      method: POST
      url: "/api/course/private/v4/user/enrollment/list/{{ ctx.user_id_hash }}"
      body:
        request:
          retiredCoursesEnabled: true
          status: ["In-Progress", "Completed"]
    response_mapping:
      list_path:       "$.courses"            # JSONPath to the array in the response
      id_field:        courseId               # stored as the selected option's value
      label_field:     courseName             # main display text
      sub_label_field: completionPercentage   # secondary text (optional)
      extra_fields:                           # values copied to collected when user picks
        - { from: courseName,        to: collected.course_name }
        - { from: completedOn,       to: collected.completed_on_iso, transform: unix_ms_to_iso }
        - { from: langContentStatus, to: collected.incomplete_ids, transform: extract_incomplete_ids }
    search:     { enabled: true, placeholder: "Search your enrolled courses..." }
    pagination: { enabled: true, page_size: 10 }
    cache_ttl:  300          # seconds; 0 to disable
  next: branch_after_pick
```

`extra_fields` avoids a second API call — data already fetched during picker load is stored directly when the user selects an item.

---

### branch

Evaluates rules top-to-bottom and routes to the first match.

```yaml
- id: branch_on_issue
  type: branch
  rules:
    - { if: "ctx.collected.sub_scenario == 'C1'", then: c1_pick_course }
    - { if: "ctx.collected.sub_scenario == 'C3'", then: c3_pick_course }
  default: resolution_generic     # required; taken when no rule matches
```

See [section 6](#6-branch-rule-expressions) for full expression syntax.

---

### api_call

Calls an external HTTP API and maps response fields into `collected`.

```yaml
- id: fetch_enrollment_status
  type: api_call
  integration: karmayogi           # karmayogi | zoho_desk_api
  request:
    method: POST
    url: "/api/course/private/v4/user/enrollment/list/{{ ctx.user_id_hash }}"
    body:
      request:
        retiredCoursesEnabled: true
        status: ["In-Progress", "Completed"]
    headers:                        # optional extra headers
      X-Channel: "{{ ctx.channel }}"
  response_mapping:
    - { from: "$.courses",
        find_where: { field: courseId, equals_ctx: collected.course_id },
        sub_path: courseName,
        to: collected.course_name }
    - { from: "$.courses",
        find_where: { field: courseId, equals_ctx: collected.course_id },
        sub_path: completedOn,
        to: collected.completed_on_iso,
        transform: unix_ms_to_iso }
  on_success: branch_after_fetch    # required
  on_error:
    any: resolution_generic          # any | timeout | http_4xx | http_5xx
  timeout_ms: 5000
```

**What you can use in `url`, `body`, `headers`, `params`:** see [section 4](#4-template-variables-ctx-and-env).

**Available integrations:**

| `integration:` key | Talks to | Auth method |
|---|---|---|
| `karmayogi` | `https://igot.gov.in` | Bearer API key (from `KARMAYOGI_API_KEY` env var) |
| `cbp_api` | `https://cbp.igotkarmayogi.gov.in` | Same `KARMAYOGI_API_KEY`; adds required CBP headers (`hostpath`, `rootorg`, `locale`). Pass user context via `headers.wid: "{{ ctx.user_id_hash }}"` |
| `zoho_desk_api` | Zoho Desk REST API | OAuth2 refresh token (auto-refreshed, cached) |

The adapter handles auth and base URL. All endpoint paths, body, and headers live in YAML.

**Karmayogi response envelope:** responses wrapped in `{result: {response: {...}}}` are unwrapped automatically — your JSONPath starts from the inner object (`$.courses`, not `$.result.response.courses`).

---

### resolution

Presents numbered self-service steps, then asks if resolved.

```yaml
- id: resolution_forgot_password
  type: resolution
  prompt:
    text: "Let's reset your password step by step:"
  steps:
    - "Go to the iGOT login page and click **Forgot Password**."
    - "Enter your registered email address."
    - "Check your inbox (and spam folder) for the reset link."
    - "Click the link and set a new password within 30 minutes."
  follow_up:
    text: "Did this resolve your issue?"
    quick_replies:
      - { id: YES, label: "✅ Yes, resolved" }
      - { id: NO,  label: "❌ No, still facing issue" }
  on_reply:
    YES: end_resolved
    NO:  track_dissatisfaction
```

---

### increment_and_branch

Increments a named counter and routes based on its new value. Used in Mode B flows to count how many times a user was not satisfied, then decide when to escalate to the LLM.

```yaml
- id: track_dissatisfaction
  type: increment_and_branch
  counter: dissatisfaction_count        # key in ctx.counters (auto-initialised to 0)
  rules:
    - { if: "ctx.counters.dissatisfaction_count < 2", then: ask_reclassify }
    - { if: "ctx.counters.dissatisfaction_count >= 2", then: transfer_to_llm }
  default: transfer_to_llm
```

The counter is incremented first, then the rules are evaluated against the new value.

Counters live in `ctx.counters.<name>` — accessible in branch rules and message templates.

---

### transfer_llm (Mode B only)

Calls the LLM (Vertex AI Gemini) to summarise the conversation and draft a Zoho ticket. Only valid in `flow_type: deterministic_with_llm_fallback` flows. Falls back to a template summary if the LLM is unavailable.

**Standard mode** — shows draft to user, asks for confirmation before raising ticket:

```yaml
- id: transfer_to_llm
  type: transfer_llm
  llm_context:
    include_messages:  true    # include conversation transcript
    include_collected: true    # include collected fields
    include_flow_meta: true    # include flow_id and channel
  llm_directives:
    objective: |
      The user has been through 2+ deterministic attempts and remains unsatisfied.
      Do NOT try to re-resolve. Your job is to:
        1. Acknowledge briefly.
        2. Confirm core issue.
        3. Draft a Zoho support ticket.
        4. Confirm with user → create ticket.
    priority_override: P3
  on_complete: confirm_ticket   # where to go after user confirms
```

**Auto-raise mode** — generates ticket summary and proceeds immediately (no user confirmation):

```yaml
- id: auto_ticket_summary
  type: transfer_llm
  auto_raise: true
  llm_context:
    include_messages:  true
    include_collected: true
  llm_directives:
    objective: |
      The user could not resolve their issue. Generate a concise Zoho support ticket.
      Subject: short description.
      Description: include all key data from the conversation.
    priority_override: P4
  on_complete: confirm_ticket
```

> **LLM cost cap:** only one LLM call is allowed per session. If already used, the node falls back automatically to a template-based summary. The kill-switch `LLM_KILL_SWITCH=true` in `.env` disables all LLM calls globally.

---

### end

Terminates the flow. `outcome` is required.

```yaml
- id: satisfied
  type: end
  outcome: self_served          # user resolved their own issue
  prompt:
    text: "Glad I could help! 🙏"

- id: ticket_raised_end
  type: end
  outcome: ticket_raised        # Zoho ticket was raised successfully

- id: ticket_failed_end
  type: end
  outcome: ticket_failed        # Zoho API call failed; show support email
  prompt:
    text: "⚠️ We couldn't raise a ticket right now. Please email support@igotkarmayogi.gov.in."
```

**Outcomes and their UI banner colours:**

| outcome | Banner | Use when |
|---|---|---|
| `self_served` | ✅ Green | User confirmed the steps resolved the issue |
| `ticket_raised` | 🎫 Blue | Zoho ticket created successfully |
| `ticket_failed` | ⚠️ Orange | Zoho API call failed |
| `ended` | 👋 Grey | Generic end (not recommended — use one of the above) |

---

## 4. Template variables (ctx and env)

Jinja2 templates are supported in `message.prompt.text`, `api_call.request.url`, `api_call.request.body`, `api_call.request.headers`, and `api_call.request.params`.

### `ctx` — current session state

| Variable | Type | Description |
|---|---|---|
| `ctx.collected.<field>` | any | Everything stored by `collect` nodes, `response_mapping`, or `on_reply.save_to` |
| `ctx.counters.<name>` | int | Named counters maintained by `increment_and_branch` nodes |
| `ctx.user_id_hash` | string | HMAC hash of the Karmayogi user ID (safe to send in API URLs) |
| `ctx.session_id` | string | UUID of the current chat session |
| `ctx.flow_id` | string | Current flow ID (e.g. `"CERTIFICATE_DOWNLOAD"`) |
| `ctx.channel` | string | Active channel: `"web"` / `"whatsapp"` / `"mobile"` / `"voice"` |
| `ctx.language` | string | BCP-47 preferred language (e.g. `"en"`, `"hi"`) |
| `ctx.ticket_draft.subject` | string | LLM/template-generated ticket subject (set after `transfer_llm`) |
| `ctx.ticket_draft.description` | string | Full ticket description |
| `ctx.ticket_draft.category` | string | Ticket category |
| `ctx.ticket_draft.sub_category` | string | Ticket sub-category (may be empty) |
| `ctx.ticket_draft.priority` | string | `P1` … `P5` |
| `ctx.ticket_draft.severity` | string | `Sev 1` … `Sev 5` |

**Examples:**
```jinja
"Hello {{ ctx.collected.first_name | default('there') }}!"
"Your course: {{ ctx.collected.course_name }}"
"/api/user/private/v1/read/{{ ctx.user_id_hash }}"
```

### `env` — server-side environment variables

Server config values exposed to templates — **not** user-controlled.

| Variable | What it is |
|---|---|
| `env.ZOHO_DEPARTMENT_ID` | Zoho Desk department ID (from `ZOHO_DEPARTMENT_ID` in `.env`) |

**Example** (used in `_zoho_ticket.yaml`):
```jinja
departmentId: "{{ env.ZOHO_DEPARTMENT_ID }}"
```

### Jinja2 filters

Standard Jinja2 filters work: `default`, `upper`, `lower`, `truncate`, `replace`, etc.

```jinja
{{ ctx.collected.email | default('not provided') }}
{{ ctx.collected.course_name | truncate(60) }}
```

---

## 5. Response mapping directives

### Basic `from/to`

```yaml
{ from: "$.ticketNumber",     to: collected.ticket_id }
{ from: "$.content[0].name",  to: collected.resource_name }
```

`from` is a JSONPath expression. Karmayogi `{result:{response:{...}}}` envelopes are unwrapped automatically — start from the inner data (`$.courses`, not `$.result.response.courses`).

### `find_where` — search an array

Find the first item where a field matches a value from `collected`, then extract a sub-field.

```yaml
{ from: "$.courses",
  find_where: { field: courseId, equals_ctx: collected.course_id },
  sub_path: courseName,
  to: collected.course_name }
```

> `equals_ctx` takes a dotted path **without** the `ctx.` prefix — write `collected.field`, not `ctx.collected.field`.

### `transform` — convert values

| name | Input | Output | Use when |
|---|---|---|---|
| `unix_ms_to_iso` | Unix milliseconds (int) | ISO-8601 UTC string | `completedOn`, `enrolledDate` fields |
| `enrollment_status_to_int` | `"Completed"` / `"In-Progress"` / `"NotStarted"` | `2` / `1` / `0` | branch rules that compare status numerically |
| `extract_incomplete_ids` | `langContentStatus` object | list of resource ID strings | feed into content API `filters.identifier` |
| `duration_to_minutes` | seconds (string or int) | float minutes | display human-readable duration |
| `detect_scorm` | list of mimeType strings | `true` / `false` | check if any resource is SCORM type |
| `count_courses_total` | `$.courses` array | int count | total enrollments for a user |
| `count_courses_inprogress` | `$.courses` array | int count | enrollments with status 1 (In Progress) |
| `count_courses_completed` | `$.courses` array | int count | enrollments with status 2 (Completed) |
| `extract_child_course_ids` | program hierarchy `children` array | list of identifier strings | extract child course IDs from `/api/content/v2/read/{id}` |
| `access_settings_is_restricted` | CBP `accessSettings` object | `true` / `false` | detect if a course has access restrictions (use with `cbp_api` integration) |

---

## 6. Branch rule expressions

Rules are evaluated with `simpleeval` (a safe Python expression evaluator).

| Pattern | Example |
|---|---|
| String equality | `ctx.collected.sub_scenario == 'C1'` |
| None check | `ctx.collected.field == None` |
| Integer comparison | `ctx.counters.dissatisfaction_count >= 2` |
| List length | `len(ctx.collected.incomplete_ids) == 0` |
| Time check | `hours_since(ctx.collected.completed_on_iso) > 24` |
| Truthiness | `has(ctx.collected.email)` |
| Compound | `ctx.collected.field == None or hours_since(ctx.collected.field) > 24` |

> **`None` not `null`:** simpleeval uses Python syntax. Write `== None`, not `== null`.
>
> **Guard before `hours_since`:** the field may be `None` if the API call didn't populate it. Always check `== None or hours_since(...) > N` to avoid a runtime error.

---

## 7. Imports — reusing shared fragments

```yaml
imports:
  - _terminal              # adds: satisfied, ticket_raised_end

  - fragment: _zoho_ticket
    with:
      cf_category:     account        # required
      cf_sub_category: login_issue    # required
      cf_flow_id:      LOGIN_ISSUE    # required

  - _otp_flow              # adds OTP send/verify sub-flow (see prerequisites below)
```

### `_terminal`

Nodes added: `satisfied` (outcome: self_served), `ticket_raised_end` (outcome: ticket_raised).

Include this in every flow that has a self-service resolution path.

---

### `_zoho_ticket`

Nodes added: `confirm_ticket`, `ticket_confirmation`, `ticket_failed`, `ticket_failed_end`, `ticket_raised_end`.

```yaml
- fragment: _zoho_ticket
  with:
    cf_category:     account         # required — Zoho custom field category
    cf_sub_category: login_issue     # required — Zoho custom field sub-category
    cf_flow_id:      LOGIN_ISSUE     # required — Zoho custom field to track source flow
```

**What this fragment does:**
1. `confirm_ticket` — POSTs to Zoho Desk with a full ticket body built from `ctx.ticket_draft` and `ctx.collected`
2. On success → `ticket_confirmation` (shows ticket number) → `ticket_raised_end`
3. On failure → `ticket_failed` (shows error message) → `ticket_failed_end`

**Fields sent to Zoho automatically** (no YAML changes needed):
- Subject: `[ITSM Support v2] {{ ctx.ticket_draft.subject }}`
- Description, priority, severity from `ticket_draft`
- Contact details from `collected.first_name`, `.last_name`, `.email`, `.mobile`
- `departmentId` from server env (`ZOHO_DEPARTMENT_ID`)
- Custom fields: `cf_source = "ITSM Support v2"`, `cf_portal = "Learner Portal"`, `cf_channel_source`, `cf_bot_session_id`, `cf_flow_id`, `cf_category`, `cf_sub_category`

> The `ctx.ticket_draft` fields are populated by the `transfer_llm` node (AI-generated) or a deterministic template fallback. Your flow only needs to route to `transfer_to_llm` → `confirm_ticket`.

---

### `_otp_flow`

Nodes added: `send_otp`, `ask_otp`, `verify_otp`, `otp_invalid_retry`, `otp_failed_final`, `otp_send_failed`.

**Prerequisites — must be in `collected` before this fragment runs:**
- `collected.otp_type` — `"email"` or `"phone"`
- `collected.otp_key` — the new email address or mobile number to verify

**Your flow must also define these target nodes:**
- `otp_verified_next` — where to go after successful OTP verification
- `otp_max_retries_node` — where to go after 3 failed OTP attempts

**Field set on success:** `collected.otp_verified = true`

---

### `_karmayogi_user`

Nodes added: `fetch_user_profile`.

Calls `GET /api/user/private/v1/read/{{ ctx.user_id_hash }}` and populates:

| Field | Description |
|---|---|
| `collected.first_name` | User's first name |
| `collected.has_profile_photo` | `true/false` |
| `collected.has_cover_photo` | `true/false` |
| `collected.has_about_me` | `true/false` |
| `collected.username_verified` | `true/false` |
| `collected.has_designation` | `true/false` |
| `collected.has_group` | `true/false` |
| `collected.has_ehrms_id` | `true/false` |
| `collected.completion_pct` | Profile completion percentage |

Routes to `diagnose_missing_fields` on success, `escalate_api_error` on error — **you must define both nodes** in your flow.

---

### Fragment override rule

If a node `id` from a fragment already exists in your flow's `nodes:` list, **your node wins** — the fragment node is silently skipped. Use this to customise fragment behaviour without forking the fragment file.

---

## 8. Adding a flow to the chat menu

**No Python changes needed.** Add three fields to the flow's `metadata:` block:

```yaml
metadata:
  menu_label:  "🆕 My new support topic"   # button text shown in the picker
  menu_group:  "Learning"                   # logical group (for future UI grouping)
  menu_order:  11                           # position in list (lower = higher)
```

The engine auto-generates the topic picker from all loaded flows with a `menu_label`. The button's `choice_id` is the `flow_id` — no mapping table needed.

**To hide a flow from the menu but keep it callable via the raw API** (e.g. for dev/QA testing):
```yaml
metadata:
  menu_hidden: true   # hidden from picker; still starts if flow_id sent as choice_id
```

**To disable a flow entirely** (hidden from menu AND blocked at API level — e.g. a WIP flow you don't want users reaching):
```yaml
metadata:
  enabled: false      # compiled at startup (YAML errors still surface), but router rejects it
```

**To permanently remove a flow:** delete or move the YAML file to `flows/on_hold/`. Files in `on_hold/` are never auto-loaded.

---

## 9. Bot persona and system messages

All user-facing strings (greeting, error messages) live in:

```
flows/_shared/system_messages.yaml
```

Edit this file to change any bot message **without touching Python**. Changes take effect on next server restart.

```yaml
greeting: |
  👋 Hi! I'm the **iGOT Karmayogi** support assistant.

  What can I help you with today?

unknown_topic:         "🤔 I didn't catch that — please choose one of the options below."
conversation_ended:    "This conversation has ended. Please start a new session to continue."
validation_empty:      "❌ This field can't be empty — please enter a value."
validation_email:      "❌ That doesn't look like a valid email address.\nPlease enter a valid email, e.g. **name@example.com**"
validation_date:       "❌ Please enter a recognisable date, e.g. **12 May 2026** or **2026-05-12**"
```

---

## 10. Validation

### Step 1 — Compiler validation (always run before committing)

```bash
source .venv/bin/activate
python -m app.engine.compiler --validate flows/
```

This catches:
- Missing required fields (`entry_node`, `on_success`, etc.)
- Dangling edges (a `next:` targeting a node that doesn't exist)
- LLM nodes in `deterministic` flows
- Unknown node types
- Import fragment resolution failures

### Step 2 — LLM-as-judge quality evaluation (run after writing a new flow)

The judge runner exhaustively walks every user-choice path through your flow, simulates each conversation, then asks Claude to evaluate correctness against the SOP document. It produces a standalone HTML report with `PASS` / `WARN` / `FAIL` verdicts and fix suggestions.

```bash
# Run on your specific flow
python scripts/llm_judge_runner.py --flow MY_NEW_FLOW

# Open the HTML report
open test_reports/MY_NEW_FLOW_*.html     # macOS
xdg-open test_reports/MY_NEW_FLOW_*.html  # Linux
```

**Requires** `ANTHROPIC_API_KEY` and `IGOT_TEST_USER_ID` in `.env`.

The SOP document for your flow should exist at `../reference/SOPs_md/`. The judge uses the SOP as ground truth — disagreements between the YAML and the SOP are marked `FAIL`.

---

## 11. Common mistakes

| Mistake | Fix |
|---|---|
| `== null` in branch rule | Use `== None` (Python syntax) |
| `integration: zoho` | Use `integration: zoho_desk_api` |
| `end` node using `message:` key | Use `prompt:` + `outcome:` |
| Missing `on_success:` in `api_call` | Required — the engine will reject the flow |
| Missing `default:` in `branch` | Required — validation will catch it |
| Adding `dtmf:` or `spoken_label:` to quick_replies | Remove them — voice is disabled; they are ignored |
| `equals_ctx: ctx.collected.field` (with `ctx.` prefix) | Write `equals_ctx: collected.field` — no `ctx.` inside `find_where` |
| `hours_since()` on a field that may be `None` | Guard first: `ctx.collected.field == None or hours_since(ctx.collected.field) > 24` |
| Omitting `disable_input: true` on a message with `quick_replies` | User can type free text; `save_to` stores raw text, not the button id |
| Fragment node id same as flow node id | Your flow's node wins — this is intentional (override pattern); make sure it's deliberate |
| `_zoho_ticket` fragment without all three `with:` params | `cf_category`, `cf_sub_category`, and `cf_flow_id` are all required |
| Routing `ticket_failed` to `ticket_raised_end` | Always route to `ticket_failed_end` (different outcome badge in UI) |
| Adding `menu_label` without `menu_order` | Defaults to 99 — will appear at the bottom; always set an explicit order |
| Setting `menu_hidden: true` expecting the flow to be unreachable | `menu_hidden` only hides from the picker; the flow is still callable if you know the `flow_id`. Use `enabled: false` to block API access entirely |
