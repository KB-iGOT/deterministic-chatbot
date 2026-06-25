# iGOT Deterministic Chatbot — Frontend Integration Contract

**Audience:** Web / mobile frontend developers integrating the iGOT Deterministic Chatbot chat widget into the iGOT Karmayogi portal.  
**Base URL:** Configured per environment (see §12).  
**Auth:** Every request requires two headers:
- `x-authenticated-user-token: <keycloak_jwt>` — the user's live Keycloak session token
- `Authorization: Bearer <kong_jwt>` — Kong API gateway token (required when calling through Kong in dev/UAT/prod)

---

## TL;DR — How It Works

The chatbot is a request-response API. Every response is a list of **activities** (messages, buttons, pickers) that you render in order. You never parse free text or make decisions — the server drives all logic.

**The flow every frontend follows:**

```
App opens
    ↓
GET  /ai-chatbot/v1/sessions/list     ← check if user has an active session
    │
    ├─ session found → GET /ai-chatbot/v1/sessions/history/{id}
    │                      │
    │                      └─ messages[] always non-empty → render full thread
    │                                                        last role:bot entry = current prompt
    │                         (if user opened but hadn't picked a topic yet, history returns
    │                          the initial greeting + category menu — same as /sessions/create)
    │
    └─ no session   → POST /ai-chatbot/v1/sessions/create        ← start fresh
                            ↓
              POST /ai-chatbot/v1/sessions/turn/{id}   ← user picks a category (5 buttons)
                            ↓
              POST /ai-chatbot/v1/sessions/turn/{id}   ← user picks a flow within the category
                            ↓
              POST /ai-chatbot/v1/sessions/turn/{id}   ← one call per subsequent user action
              (repeat until response contains type:"end")
```

**Session TTL:** 30 minutes sliding (resets on every turn). Configured via `IGOT_WEB_SESSION_TTL_MINUTES`.

---

## Table of Contents

0. [Frontend Quick Start — What You Need to Build](#0-frontend-quick-start--what-you-need-to-build)
1. [Overview](#1-overview)
2. [Authentication](#2-authentication)
3. [Session Lifecycle](#3-session-lifecycle)
4. [Request — User Actions (complete reference)](#4-request--user-actions-complete-reference)
5. [Response — Activity Types (complete reference)](#5-response--activity-types-complete-reference)
6. [Response — Top-Level Fields](#6-response--top-level-fields)
7. [Error Responses](#7-error-responses)
8. [Edge Cases and Guardrails](#8-edge-cases-and-guardrails)
9. [Session History and Resumption](#9-session-history-and-resumption)
10. [Complete Worked Examples (curl)](#10-complete-worked-examples-curl)
11. [Multi-language Support](#11-multi-language-support)
12. [Environments and Base URLs](#12-environments-and-base-urls)
13. [API Reference — Per-Endpoint](#13-api-reference)
14. [Current Limitations](#14-current-limitations)

---

## 0. Frontend Quick Start — What You Need to Build

> **Read this first.** This section gives mobile and web engineers a complete picture of what to build before going into the detailed spec. Think of it as your integration checklist.

---

### 0.1 Prerequisites

| # | What you need | Details |
|---|---------------|---------|
| 1 | **Keycloak JWT** | The user's live portal session token. Read it from the browser cookie or `localStorage` where the iGOT portal already stores it. Send it as `x-authenticated-user-token` on every request. |
| 2 | **Kong JWT** | API gateway token required when calling through Kong (dev/UAT/prod). Send it as `Authorization: Bearer <token>`. Not needed for local direct calls. |
| 2 | **API base URL** | One URL, configured per environment — see §12. Local dev: `http://localhost:8000` |
| 3 | **Markdown renderer** | Bot messages use bold, italics, bullet lists, and line breaks. Use `react-markdown` (React/Next.js), `marked` (vanilla JS), `flutter_markdown` (Flutter), or equivalent. |
| 4 | **Session storage** | `localStorage` (web) or secure storage (mobile). You store exactly one value: the `session_id` UUID from the first API call. |

---

### 0.2 API Surface — 4 Endpoints

| # | Endpoint | When to call |
|---|----------|-------------|
| 1 | `GET /ai-chatbot/v1/sessions/list` | On app open — check if the user has an active session |
| 2a | `GET /ai-chatbot/v1/sessions/history/{id}` | Active session found — load full conversation thread; last role:bot entry = current prompt. Always returns at least one bot entry (initial greeting if no topic selected yet). |
| 2b | `POST /ai-chatbot/v1/sessions/create` | No active session (sessions/list returned null) |
| 3 | `POST /ai-chatbot/v1/sessions/turn/{id}` | On every user action (button tap, text submit, picker selection) |

The frontend **never** calls Karmayogi, Zoho, OTP, or any other backend service directly. All of that happens server-side.

---

### 0.3 Why each endpoint exists

| Endpoint | Why it exists |
|----------|--------------|
| `GET /health` | Load balancer and k8s liveness probe — confirms the pod is up |
| `POST /sessions/create` | Creates a new conversation — allocates session_id, shows greeting + topic menu |
| `POST /sessions/turn/{id}` | The main conversation driver — every user tap/input goes here; server runs the flow and returns the next bot activities |
| `GET /sessions/list` | Cross-device resume — Redis maps user_id → session_id so any device can find the active session without the client storing anything |
| `GET /sessions/history/{id}` | Resume + full thread — returns every message from start of session. The last role:bot entry is the current prompt. Always returns at least one bot entry — if the user opened the chat but hadn't picked a topic yet, returns the initial greeting + category menu (same as /sessions/create). |
| `GET /admin/sessions/{id}/trace` | Debugging — full node-by-node trace of what the engine did (not yet wired) |
| `DELETE /admin/sessions/{id}` | DPDP compliance — right-to-erasure, deletes all stored conversation data for a user (not yet wired) |

---

### 0.5 UI Components to Build

Every API response contains `activities: [...]`. Each object in that array maps directly to one UI component. There are **7 component types**:

| `type` | What to render | Notes |
|--------|---------------|-------|
| `markdown` | Chat bubble — render with a Markdown library | Bold (`**text**`), bullet lists, `\n\n` = paragraph break |
| `text` | Chat bubble — plain text, no Markdown | Same layout as `markdown`, just skip the parser |
| `quick_replies` | Row of tappable button chips | On tap → send `select_choice`; see §4 |
| `input` | Free-text input box + submit | 3 variants: plain text, email, OTP — see §0.6 |
| `picker` | Searchable scrollable list | With secondary text, optional "Other" button — see §0.7 |
| `typing` | Typing / loading animation | Always followed by real content in the same response (Phase 1) |
| `end` | End-of-conversation banner | 4 outcome variants — see §0.8 |

> ℹ️ A single response may contain **multiple activities in one array** — render all of them, top to bottom, in order before waiting for the next user action.

**`disable_input` flag** — present on `markdown`, `quick_replies`, and `picker` activities:

| Value | Frontend action |
|-------|----------------|
| `true` | Hide or disable the free-text input bar. The user must use the buttons/picker shown. |
| `false` | Keep the text bar enabled alongside the buttons (user may do either). |

---

### 0.6 Input Field Variants

The `input` activity type covers three use cases, distinguished by the `input_id` or `validate_regex` field:

| Variant | How to detect | Keyboard / UX |
|---------|--------------|----------------|
| **Plain text** | No `validate_regex`; `input_id` like `collected.description`, `collected.ministry_name` | Default text keyboard |
| **Email** | `validate_regex` is set (email pattern), or `input_id` contains `email` | Email keyboard; show validation hint beneath the field |
| **OTP code** | `input_id` ends in `_otp` or `otp_entered` | Numeric pad; auto-advance or auto-submit when 6 digits entered |

On submit, always send a `send_message` action with `text` = the user's value.  
If `validate_regex` is present, validate client-side first — don't call the API with an invalid value. Server-side validation is a safety net, not a replacement.

**Fields collected across all active flows:**

| Field | Type | Flows that collect it |
|-------|------|-----------------------|
| Email address | Email | Course Progress, Resource Not Opening, Bulk Profile Update, Weekly Clap, Access Revoked |
| First name / Last name | Text | Certificate Download (name correction) |
| Organisation details (ministry, state, dept, org name) | Text (multi-field) | Access Revoked |
| Course name, resource name, mobile model, app version | Text (multi-field) | Resource Not Opening (mobile path) |
| Correct org details (free description) | Text | Download Report |
| OTP code | Numeric / text | OTP flow (shared fragment — not yet active) |

---

### 0.7 Picker Types

All pickers use the same `picker` activity schema — you build **one reusable picker component**. The server fetches and shapes the data; you just render `items[]`. Across all 12 active flows you will encounter these picker shapes:

| `picker_id` (in response) | What it lists | Search? | Items shown |
|---------------------------|--------------|---------|-------------|
| `course_picker` | User's enrolled courses (In-Progress + Completed) | ✅ Yes | 10 per page |
| `completed_course_picker` | User's completed courses only | ✅ Yes | 10 per page |
| `events_picker` | User's completed events | ✅ Yes | 10 per page |
| `resource_picker` | Learning resources inside a specific course | ✅ Yes | 15 per page |
| *(flow-defined static pickers)* | Fixed list from the flow YAML (e.g. issue sub-types) | ❌ No | All at once |

**Picker item anatomy:**

```json
{
  "id":    "do_1142232871610777601410",        ← send back as item_id in pick_item action
  "label": "Micronutrient Supplementation",   ← primary display text
  "meta":  "Completed · 21 Jul 2025"          ← secondary / sub-label (nullable)
}
```

**Picker component requirements:**
- Render `placeholder` as search box hint text
- Show `items[].label` (primary) and `items[].meta` (secondary, smaller, muted) for each item
- If `search_enabled: true` — show a search/filter input; filter `items[]` client-side by label
- If `other_option` is present — show a "My item isn't listed" / "Other" button at the bottom of the list
- If `items` is empty and `other_option` is present — show only the "Other" button (empty state)
- On item tap → send `pick_item` with `picker_id`, `item_id`, `item_label`
- On "Other" tap → show a free-text input → send `request_other` with `other_query`

---

### 0.8 End-of-Conversation Banners

When `activities[]` contains `{ "type": "end" }`, the conversation is over. Show a status banner based on `outcome`:

| `outcome` | Suggested banner | Extra data |
|-----------|-----------------|------------|
| `self_served` | ✅ Green — "Issue resolved!" | — |
| `ticket_raised` | 🎫 Blue — "Support ticket raised" | Show `ticket_id` from the response root: *"Ticket #12345678 raised. L2 team will reach out within 2 business days."* |
| `ticket_failed` | ⚠️ Yellow — "Could not raise ticket automatically" | Bot message will say to email support directly. No `ticket_id` in this case. |
| `unresolved` | ⚠️ Yellow — "We'll follow up shortly" | — |
| `ended` | ⬜ Neutral — conversation closed | — |

After showing the banner, display a **"Start a new conversation"** button. On tap, call `POST /ai-chatbot/v1/sessions/create` to start a new session.

---

### 0.9 Session Lifecycle (frontend logic)

```
On app / page load
──────────────────
1. Call GET /ai-chatbot/v1/sessions/list
2. If response.session_id is not null:
     Call GET /ai-chatbot/v1/sessions/{session_id}
     Render the returned activities — user continues where they left off
3. If response.session_id is null (no active session, expired, or Redis unavailable):
     Call POST /ai-chatbot/v1/sessions/create  { "channel": "web", "language": "en" }
     Save response.session_id to localStorage
     Render response.activities[]

On every user action (button tap / text submit / picker select)
──────────────────────────────────────────────────────────────
1. POST /ai-chatbot/v1/sessions/turn/{session_id}  { action + payload }
2. Render all activities[] in order
3. If any activity has type = "end":
     show outcome banner
     show "Start new conversation" button
     DELETE "igot_session_id" from storage
     stop sending turns to this session

On error responses
──────────────────
  401 → refresh Keycloak token, retry once
  404 → session gone → start a new session (step 3 above)
  410 → session expired → start a new session (step 3 above)
  422 → request body wrong (developer error — fix the payload, do not retry)
  5xx → wait 2 seconds, retry once; if still failing, show error state
```

---

### 0.10 Quick Replies — Key Rules

- **Always send back the `id`, never the `label`** — labels may be translated; IDs are always English internal identifiers.
- **Never show `id` to the user** — always display `label`.
- **`disable_input: true`** — hide the keyboard / text bar. The user can only tap a button.

---

## 1. Overview

The iGOT Deterministic Chatbot is a **structured chatbot API**. Every response is a list of typed **activities** that tell the frontend exactly what to render. The frontend never parses free text to decide what to show — the server handles all logic.

```
Frontend                          iGOT Deterministic Chatbot API
   |                                  |
   |  POST /ai-chatbot/v1/sessions/create             |  ← start a new session
   |  ─────────────────────────────→  |
   |  ←─────────────────────────────  |  → [markdown greeting, quick_replies (5 categories)]
   |                                  |
   |  POST /ai-chatbot/v1/sessions/turn/{id}   |  ← user picks a category
   |  ─────────────────────────────→  |
   |  ←─────────────────────────────  |  → [markdown, quick_replies (flows in that category)]
   |                                  |
   |  POST /ai-chatbot/v1/sessions/turn/{id}   |  ← user picks a flow
   |  ─────────────────────────────→  |
   |  ←─────────────────────────────  |  → [markdown, quick_replies] or [picker] or [input]
   |                                  |
   |  POST /ai-chatbot/v1/sessions/turn/{id}   |  ← user responds
   |  ─────────────────────────────→  |
   |  ←─────────────────────────────  |  → ... (repeat)
   |                                  |
   |  POST /ai-chatbot/v1/sessions/turn/{id}   |  ← last user action
   |  ─────────────────────────────→  |
   |  ←─────────────────────────────  |  → [markdown, end]   ← conversation complete
```

**Key design principles:**
- One session = one conversation thread (tied to `session_id`)
- The server is stateful — session state persists across browser refreshes via Postgres checkpointer
- `choice_id` values in `quick_replies` are **internal identifiers** — never display them; display `label` instead
- A response can contain **multiple activities** — render them all, top to bottom, in order

---

## 2. Authentication

Two integration patterns are supported. Choose based on your client type:

### Pattern A — Kong Direct (mobile / backend)

Two headers required on every request:

```
x-authenticated-user-token: <keycloak_jwt>
Authorization: Bearer <kong_jwt>
```

- `x-authenticated-user-token` — user's Keycloak JWT (extracted from portal session)
- `Authorization: Bearer` — Kong API gateway JWT

The backend extracts `user_id` from the JWT `sub` claim (`f:<federation-id>:<user-uuid>` → last segment).

### Pattern B — UI Proxy (web frontend)

One header only — the browser session cookie:

```
cookie: connect.sid=<session_cookie>
```

The proxy resolves the user session internally — no JWT handling needed in the frontend code. Get `connect.sid` from DevTools → Application → Cookies after logging in to the portal.

> See §12 for full base URLs and curl examples for each pattern.

### Local dev (`AUTH_DISABLED=true`)

When `AUTH_DISABLED=true` in `.env`, JWT validation is bypassed entirely:

| What you send | What the server uses as user_id |
|---------------|--------------------------------|
| Any UUID string | That UUID directly |
| Nothing / empty | `IGOT_TEST_USER_ID` from `.env` |

```bash
curl -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

> ⚠️ `AUTH_DISABLED=true` must **never** be set in staging or production. It is enforced via the `IGOT_ENV` check — set `IGOT_ENV=prod` to block this flag.

---

## 3. Session Lifecycle

### Phase 1 — Start a session

```
POST /ai-chatbot/v1/sessions/create
```

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `channel` | string | ✅ | `"web"` \| `"mobile"` \| `"whatsapp"` |
| `language` | string | ✅ | BCP-47 language code: `"en"`, `"hi"`, etc. |

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | UUID | **Save this immediately to localStorage** — all subsequent turns use it |
| `activities` | array | Greeting message + category picker (5 quick-reply buttons) |
| `status` | string | Always `"awaiting_user"` on session start |
| `flow_id` | null | No flow selected yet |
| `current_node` | null | No flow running yet |

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "activities": [
    {
      "type": "markdown",
      "content": "👋 Hi! I'm the **iGOT Karmayogi** support assistant.\n\nWhat can I help you with today?"
    },
    {
      "type": "quick_replies",
      "choices": [
        { "id": "__cat__Profile & User Management",          "label": "Profile & User Management" },
        { "id": "__cat__Content Related Issue",              "label": "Content Related Issue" },
        { "id": "__cat__CA/APAR Issue",                      "label": "CA/APAR Issue" },
        { "id": "__cat__Recognition & Engagement",           "label": "Recognition & Engagement" },
        { "id": "__cat__Application Crashing / Not Loading", "label": "Application Crashing / Not Loading" }
      ],
      "disable_input": true
    }
  ],
  "status": "awaiting_user",
  "flow_id": null,
  "current_node": null,
  "ticket_id": null
}
```

> **Frontend:** Save `session_id` to localStorage immediately. The first `quick_replies` shows **5 top-level categories** — `choice_id` values start with `__cat__`. On selection, the server returns the sub-flows for that category. Only in that second `quick_replies` will `choice_id` be a flow ID (e.g. `CERTIFICATE_DOWNLOAD`). Hide the free-text input box when `disable_input: true` — the user must pick a button.

---

### Phase 2 — Send turns

```
POST /ai-chatbot/v1/sessions/turn/{session_id}
```

Repeat until the response contains a `{ "type": "end" }` activity.

**Path parameter:**

| Param | Type | Description |
|-------|------|-------------|
| `session_id` | UUID | From the `session_id` returned by `POST /ai-chatbot/v1/sessions/create` |

**Request body:** See §4 for full action reference.

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | UUID | Same session UUID |
| `activities` | array | Bot response — one or more activity objects; render in order |
| `status` | string | Current session status; see §6 |
| `flow_id` | string \| null | Active flow ID (e.g. `"CERTIFICATE_DOWNLOAD"`) |
| `current_node` | string \| null | Current YAML node ID (useful for debugging) |
| `ticket_id` | string \| null | Zoho ticket ID if a ticket was raised |

---

### Phase 3 — Conversation ends

When `activities` contains `{ "type": "end" }`, the session is complete.
- Show the `content` message and outcome banner
- Offer a "Start over" button — clicking it calls `POST /ai-chatbot/v1/sessions/create` again (new session)
- Do **not** send further turns to the same session — the server will respond with a "conversation ended" message

---

## 4. Request — User Actions (complete reference)

Every turn sends **exactly one** action object. Choose the correct action type based on what the bot last sent:

### `select_choice` — user tapped a quick-reply button

Use when the bot sent a `quick_replies` activity.

```json
{
  "action": "select_choice",
  "choice_id": "CERTIFICATE_DOWNLOAD"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `"select_choice"` | ✅ | Fixed |
| `choice_id` | string | ✅ | The `id` from the `choices` array in the `quick_replies` activity |

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400... \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}'
```

**Edge cases:**
- Sending an unknown `choice_id` at the **category selection** phase → server re-shows the 5 category buttons with an error message (HTTP 200)
- Sending an unknown `choice_id` at the **flow selection** phase (after picking a category) → server re-shows only the sub-flows for the already-selected category (HTTP 200)
- Sending an unknown `choice_id` inside an active flow → behaviour depends on the node; usually re-prompts

---

### `send_message` — user typed in an input field

Use when the bot sent an `input` activity.

```json
{
  "action": "send_message",
  "text": "user.name@nic.gov.in"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `"send_message"` | ✅ | Fixed |
| `text` | string | ✅ | User-entered text. Validated server-side for email/date formats when `validate_regex` is set in the `input` activity. |

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400... \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "send_message", "text": "user.name@nic.gov.in"}'
```

**Client-side validation:** If the `input` activity includes `validate_regex`, validate before sending. Example regex for email: `^[^@\s]+@[^@\s]+\.[^@\s]{2,}$`. Show inline error if invalid — do not call the API.

**Edge cases:**
- Empty string → server returns validation error activity + re-prompts the same field (HTTP 200)
- Invalid email when field type is email → server returns validation error (HTTP 200)
- Sending `send_message` when bot is expecting `select_choice` → server re-prompts with the original question

---

### `pick_item` — user selected from a picker / dropdown

Use when the bot sent a `picker` activity.

```json
{
  "action": "pick_item",
  "picker_id": "course_picker",
  "item_id": "do_1141985687873863681190",
  "item_label": "Foundation Course on AI"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `"pick_item"` | ✅ | Fixed |
| `picker_id` | string | ✅ | The `picker_id` from the `picker` activity (e.g. `"course_picker"`) |
| `item_id` | string | ✅ | The `id` from the chosen `items[]` entry |
| `item_label` | string | ✅ | The `label` from the chosen `items[]` entry — used in ticket summaries |

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400... \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{
    "action": "pick_item",
    "picker_id": "course_picker",
    "item_id": "do_1141985687873863681190",
    "item_label": "Foundation Course on AI"
  }'
```

**Edge cases:**
- `item_id` not in the original `items[]` list → server may still accept it (no whitelist check); pass exactly what the picker showed
- Empty picker (zero items) → server will typically show a fallback message instead of a picker; handle gracefully

---

### `request_other` — user typed freely when shown a picker

Use when the bot sent a `picker` activity that has `other_option` set, and the user tapped the "My item isn't listed" button and entered free text.

```json
{
  "action": "request_other",
  "other_query": "My ministry is not in the list — I work in the PMO"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `"request_other"` | ✅ | Fixed |
| `other_query` | string | ✅ | Free text the user typed |

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400... \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "request_other", "other_query": "My ministry is not listed"}'
```

**Only valid** when the current `picker` activity has `other_option` set. Sending this on a picker without `other_option` → server re-prompts.

---

### `start` — restart the flow from the top

Rarely needed. Equivalent to starting a new session. Use when the user clicks "Start over" within the same session.

```json
{ "action": "start" }
```

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400... \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "start"}'
```

> **Preferred:** create a new session via `POST /ai-chatbot/v1/sessions/create` instead of sending `start`. This gives a clean state.

---

## 5. Response — Activity Types (complete reference)

Every turn response contains `activities: [ ...one or more... ]`. Render them **in order**, top to bottom.

---

### `markdown` — bot message with Markdown formatting

```json
{
  "type": "markdown",
  "content": "Your certificate will be issued within **4–5 hours**.\n\nPlease try after waiting.",
  "disable_input": false
}
```

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `type` | `"markdown"` | ✅ | |
| `content` | string | ✅ | Markdown-formatted text. Render bold, italic, bullet lists, line breaks. |
| `disable_input` | bool | ❌ (default false) | If true, hide/disable the free-text input box |

**Rendering notes:**
- Use a Markdown renderer (e.g. `react-markdown`, `marked`)
- `\n\n` = paragraph break; `\n` = soft line break
- Never show raw Markdown syntax to the user

---

### `text` — plain text (no formatting)

```json
{
  "type": "text",
  "content": "Please choose an option below."
}
```

Same fields as `markdown` but render as plain text — no Markdown parsing.

---

### `quick_replies` — clickable option buttons

```json
{
  "type": "quick_replies",
  "choices": [
    { "id": "not_eligible", "label": "⚠️ Yes — Not Eligible to Rate", "icon": null },
    { "id": "other_error",  "label": "❌ No — different error",        "icon": null }
  ],
  "disable_input": true
}
```

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `type` | `"quick_replies"` | ✅ | |
| `choices` | array | ✅ | List of buttons to display |
| `choices[].id` | string | ✅ | Send back as `choice_id` in `select_choice` action |
| `choices[].label` | string | ✅ | Display text — always show this, never the `id` |
| `choices[].icon` | string \| null | ❌ | Optional emoji or icon name |
| `disable_input` | bool | ❌ (default false) | When `true` — hide/disable the free-text input. User must tap a button. |

**Behaviour:**
- On tap → send `select_choice` with the button's `id`
- When `disable_input: true` — the user has no choice but to pick a button; hide or disable the text input field

---

### `input` — free-text / email / number entry

```json
{
  "type": "input",
  "input_id": "collected.email",
  "input_placeholder": "your.email@example.com",
  "validate_regex": "^[^@\\s]+@[^@\\s]+\\.[^@\\s]{2,}$",
  "disable_input": false
}
```

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `type` | `"input"` | ✅ | |
| `input_id` | string | ✅ | Field identifier (opaque to frontend — do not display) |
| `input_placeholder` | string | ✅ | Hint text inside the input box |
| `validate_regex` | string \| null | ❌ | Client-side validation pattern. Validate before sending — show inline error if mismatch |
| `disable_input` | bool | ❌ (default false) | Always false for `input` (the point is to enable typing) |

**On submit:**
1. If `validate_regex` present and value doesn't match → show inline error, don't call API
2. If valid → send `send_message` action with the entered text

**Common field types inferred from `input_id`:**
- `collected.email` → email field (also signaled by `validate_regex` containing `@`)
- `collected.date` → date field

---

### `picker` — searchable item list / dropdown

```json
{
  "type": "picker",
  "picker_id": "course_picker",
  "placeholder": "Search your course...",
  "search_enabled": true,
  "total_items": 42,
  "items": [
    {
      "id": "do_1141985687873863681190",
      "label": "Foundation Course on AI",
      "meta": "Completed · 21 Jul 2025"
    },
    {
      "id": "do_1142232871610777601410",
      "label": "Micronutrient Supplementation",
      "meta": "Completed · 21 Jul 2025"
    }
  ],
  "other_option": {
    "id": "other",
    "label": "My course isn't listed"
  },
  "disable_input": true
}
```

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `type` | `"picker"` | ✅ | |
| `picker_id` | string | ✅ | Send back in `pick_item.picker_id` |
| `placeholder` | string | ✅ | Search box hint text |
| `search_enabled` | bool | ✅ | Show search/filter input if true |
| `total_items` | integer | ✅ | Total number of items available (before pagination). Use this to show a count badge or pre-emptively detect an empty state. Always ≥ 0; `items[]` may be a page-sized slice. |
| `items` | array | ✅ | List of selectable items (may be paginated) |
| `items[].id` | string | ✅ | Internal identifier — send back as `item_id` |
| `items[].label` | string | ✅ | Primary display text |
| `items[].meta` | string \| null | ❌ | Sub-label (status, date, progress) |
| `other_option` | object \| null | ❌ | "None of the above" button. If shown and tapped → send `request_other` |
| `other_option.id` | string | ✅ if present | Usually `"other"` |
| `other_option.label` | string | ✅ if present | Display text for the "other" option |
| `disable_input` | bool | ❌ (default true) | Almost always true — user must pick from the list |

**Empty state:** When a dynamic picker would have zero items, the server routes to a message node instead — you will **never** receive a `picker` activity with `items: []`. The `total_items` field therefore always reflects the count of a non-empty list. You do not need to handle the zero-item picker case in the UI.

**On selection → send `pick_item`:**
```json
{
  "action": "pick_item",
  "picker_id": "course_picker",
  "item_id": "do_1141985687873863681190",
  "item_label": "Foundation Course on AI"
}
```

**On "other" tapped → send `request_other`:**
```json
{
  "action": "request_other",
  "other_query": "My course is not in the list"
}
```

---

### `end` — conversation complete

```json
{
  "type": "end",
  "outcome": "self_served",
  "content": "Glad I could help! 🙏"
}
```

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `type` | `"end"` | ✅ | |
| `outcome` | string | ✅ | See outcome table below |
| `content` | string | ✅ | Closing message to display to user |

**Outcome values:**

| `outcome` | Meaning | Suggested UI |
|-----------|---------|-------------|
| `self_served` | Issue resolved without a ticket | ✅ Green banner — "Issue resolved!" |
| `ticket_raised` | Zoho support ticket created | 🎫 Blue banner — "Ticket raised: #<ticket_id>" (use `ticket_id` from response root) |
| `unresolved` | Could not resolve — user directed to email/phone | ⚠️ Yellow banner — "We'll follow up shortly" |
| `ended` | Generic end (e.g. user dismissed) | Neutral grey banner |

**After `end`:**
- The session is complete — do not send more turns
- Show a "Start over" / "Back to menu" button that calls `POST /ai-chatbot/v1/sessions/create` (new session)
- Do NOT call the same session again

---

### `typing` — bot is processing

```json
{ "type": "typing" }
```

Show a typing/loading animation. In Phase 1 this appears in the `activities` array followed by the real activity — so it's always immediately followed by content in the same response. In Phase 2 (streaming) it will be sent as a hint before the actual content arrives.

---

### `trace` — debug info (dev/staging only)

```json
{
  "type": "trace",
  "trace_lines": [
    "node=fetch_user_profile",
    "branch=not_mdo_leader",
    "api_call → 200 /api/user/v2/read"
  ]
}
```

**Do not render in production.** Only present in `IGOT_ENV=dev` or `IGOT_ENV=staging` with debug mode on. Filter by checking `type !== "trace"` before rendering.

---

## 6. Response — Top-Level Fields

Every turn response (both `StartSessionResponse` and `TurnResponse`) shares these top-level fields:

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | UUID | The conversation session identifier |
| `activities` | array | Ordered list of activity objects to render |
| `status` | string | Conversation state — see status table below |
| `flow_id` | string \| null | Active flow (e.g. `"CERTIFICATE_DOWNLOAD"`), null before topic selection |
| `current_node` | string \| null | YAML node ID (useful for debugging; not for display) |
| `ticket_id` | string \| null | Zoho Desk ticket ID (only populated when `status = "ticket_raised"`) |

**Status values:**

| `status` | Meaning | Frontend action |
|----------|---------|-----------------|
| `awaiting_user` | Bot waiting for user input | Keep input enabled; render activities |
| `active` | Flow is running (internal state) | Show typing indicator; wait |
| `escalating` | LLM is generating ticket summary | Show "Raising ticket…" indicator |
| `ticket_raised` | Ticket created in Zoho Desk | Show `ticket_id` from response root |
| `satisfied` | Flow ended — self-served | Show ✅ success banner |
| `ended` | Flow ended — generic | Show neutral banner |
| `error` | Something went wrong | Show error state with retry option |

**Internal session statuses** (returned by `GET /sessions/list`, not in turn responses):

| `status` | Meaning |
|----------|---------|
| `selecting_category` | User is on the top-level category menu |
| `selecting_topic` | User has picked a category and is on the sub-flow menu |
| `in_flow` | A flow is actively running |
| `done` | Session has ended |

---

## 7. Error Responses

All errors follow standard HTTP + JSON pattern:

```json
{
  "detail": "Human-readable error message"
}
```

| HTTP Status | When | Frontend action |
|-------------|------|-----------------|
| `401 Unauthorized` | Missing or invalid `x-authenticated-user-token` header | Re-authenticate (refresh Keycloak token) then retry |
| `403 Forbidden` | JWT valid but user lacks required role (if `AUTH_REQUIRED_ROLE` set) | Show permission denied message |
| `404 Not Found` | `session_id` not found in server state | Session expired or server restarted — start a new session |
| `422 Unprocessable Entity` | Request body missing required fields or wrong types | Fix the request body (developer error) |
| `500 Internal Server Error` | Flow execution error | Show generic error; log `detail` for debugging |
| `503 Service Unavailable` | A required flow was not loaded at startup | Notify backend team |

### Retry Guidance

| Error | Should retry? | How |
|-------|--------------|-----|
| Network timeout / connection refused | Yes | Wait **2 seconds**, retry **once** |
| `5xx` server error | Yes | Wait **2 seconds**, retry **once** |
| `401 Unauthorized` | Yes, but refresh first | Refresh the Keycloak token, then retry once |
| `404 Not Found` | No — start fresh | Session gone — call `POST /sessions/create` instead |
| `410 Gone` | No — start fresh | Session expired — call `POST /sessions/create` instead |
| `422 Unprocessable Entity` | No | Fix the request body (developer error) |
| Other `4xx` | No | Show user an error message |

**Rule of thumb:** never retry more than once automatically. If the retry also fails, show an error state and let the user initiate a retry manually.

**Example 401:**
```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -d '{"channel": "web", "language": "en"}'
# → HTTP 401
# {"detail": "Missing authentication token (expected header: x-authenticated-user-token)"}
```

**Example 404 (expired session):**
```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/nonexistent-uuid \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "course"}'
# → HTTP 404
# {"detail": "Session not found"}
```

---

## 8. Edge Cases and Guardrails

### Unknown selection at category or flow menu

The menu is two-level. Invalid selections are handled per level:

**Invalid category** (session is at `selecting_category` phase):
```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/$SID \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "BOGUS"}'
```
Response: 5 category buttons re-shown.

**Invalid flow** (session is at `selecting_topic` phase, user already picked a category):
```bash
-d '{"action": "select_choice", "choice_id": "BOGUS_FLOW"}'
```
Response: only the sub-flows for the already-selected category are re-shown — not all 16 flows, not the top categories.

Both cases return HTTP 200 — not an error. Do not treat as an error.

### Validation failure on text input

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/$SID \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "send_message", "text": "not-an-email"}'
```

Response (HTTP 200 — not an error):
```json
{
  "activities": [
    { "type": "markdown", "content": "❌ That doesn't look like a valid email address.\nPlease enter a valid email, e.g. **name@example.com**" },
    { "type": "input", "input_id": "collected.email", "input_placeholder": "your.email@example.com" }
  ],
  "status": "awaiting_user"
}
```

The graph state is **not changed** — the user gets another chance to enter the correct value.

### Empty picker items

If the API call that populates a picker returns no results (e.g. no enrolled courses):

```json
{
  "activities": [
    { "type": "markdown", "content": "It looks like you don't have any completed courses on your account." },
    {
      "type": "picker",
      "picker_id": "course_picker",
      "items": [],
      "other_option": { "id": "other", "label": "My course isn't listed" },
      "disable_input": true
    }
  ]
}
```

Only the "other" option is shown. The user taps it → sends `request_other`.

### Session already ended

If `status` is `"done"` (conversation complete) and the user sends another turn:

```json
{
  "activities": [
    { "type": "markdown", "content": "This conversation has ended. Please start a new session to continue." }
  ],
  "status": "ended"
}
```

Always show a "Start new session" button after the `end` activity to prevent this state.

### Multiple activities in one response

A single response can contain many activities. Always render **all** of them:

```json
{
  "activities": [
    { "type": "markdown", "content": "Let me check your enrollment..." },
    { "type": "typing" },
    { "type": "markdown", "content": "Here are your enrolled courses:" },
    { "type": "picker", "picker_id": "course_picker", "items": [...] }
  ]
}
```

Render top-to-bottom: first message, then typing indicator (briefly), then second message, then the picker.

---

## 9. Session History and Resumption

Sessions are stored in two places:
- **Redis** — maps `user_id → session_id` with a sliding 30-minute TTL
- **Postgres checkpointer** — stores the full LangGraph conversation state, survives pod restarts

### Session resume flow

On every app open, call `GET /ai-chatbot/v1/sessions/list`:

```bash
curl http://localhost:8000/ai-chatbot/v1/sessions/list \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001"
```

**Response — active session exists:**
```json
{ "session_id": "550e8400-e29b-41d4-a716-446655440000", "status": "in_flow", "flow_id": "CERTIFICATE_DOWNLOAD" }
```

**Response — no active session:**
```json
{ "session_id": null }
```

When `session_id` is returned, call `GET /ai-chatbot/v1/sessions/history/{id}` to restore the conversation:

```bash
curl http://localhost:8000/ai-chatbot/v1/sessions/history/550e8400-e29b-41d4-a716-446655440000 \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001"
```

Returns the last `activities[]` and `current_node` so the UI renders exactly where the user left off. From that point, continue sending turns as normal.

### Session TTL (sliding window)

| Channel | Default TTL | Config key |
|---------|------------|------------|
| `web` | 30 minutes | `IGOT_WEB_SESSION_TTL_MINUTES` |
| `mobile` | 30 minutes | `IGOT_WEB_SESSION_TTL_MINUTES` |
| `whatsapp` | 1440 minutes (24 h) | hardcoded for Meta's 24h window |

TTL resets on every user turn. After expiry, `GET /sessions/list` returns `null` and `POST /sessions/turn/{id}` returns an expired message — both signal the client to start a new session.

### Starting fresh

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: <token>" \
  -d '{"channel": "web", "language": "en"}'
```

Save the new `session_id` to localStorage.

### Redis unavailable

`GET /sessions/list` returns `{ "session_id": null }` — client starts a new session. Nothing breaks.

### Full conversation history

Call `GET /ai-chatbot/v1/sessions/history/{id}` to get every message from the start of the session:

```bash
curl http://localhost:8000/ai-chatbot/v1/sessions/history/{id} \
  -H "x-authenticated-user-token: <token>"
```

```json
{
  "session_id": "...",
  "messages": [
    {
      "role": "bot",
      "activities": [{ "type": "markdown", "content": "Android or iPhone?" }, { "type": "quick_replies", "choices": [...] }],
      "ts": "2026-06-16T10:01:00Z"
    },
    {
      "role": "user",
      "action": "select_choice",
      "text": "Android",
      "ts": "2026-06-16T10:01:15Z"
    },
    {
      "role": "bot",
      "activities": [{ "type": "markdown", "content": "Clear cache steps..." }, { "type": "quick_replies", "choices": [...] }],
      "ts": "2026-06-16T10:01:16Z"
    }
  ]
}
```

**Rendering rules:**
- `role: "bot"` — render `activities[]` exactly as you would a normal turn response (same rendering code)
- `role: "user"` — render `text` as the user's message bubble
- Entries are in chronological order — render top to bottom
- The **last entry is always `role: "bot"`** — that's the current state still awaiting input; its activities[] contain the pending buttons/picker

**When to call:**
- On resume (`GET /sessions/list` returned a session_id): call history first to render the thread, then the last bot entry's activities are already shown at the bottom
- On initial load of a conversation view (if you want to show a scrollable thread)

**Note:** History starts from the first topic selection. The initial greeting (welcome message + topic picker shown at session start) is not included — it is always identical and the frontend can prepend it locally if needed.

Returns `messages: []` for sessions where no flow has been selected yet, or for sessions started before history tracking was added.

---

## 10. Complete Worked Examples (curl)

Each example is broken into numbered steps. Every step shows:
1. The exact `curl` command to run
2. What the response looks like
3. What value to note/copy for the next step

Common variables used throughout (set these in your shell once):

```bash
BASE=http://localhost:8000
TOKEN=00000000-0000-0000-0000-000000000001   # dev mode: pass UUID directly as the token
```

---

### Example A — Certificate issue, self-resolved (5 steps)

#### Step 1 — Start a session

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

**Response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "awaiting_user",
  "flow_id": null,
  "current_node": null,
  "activities": [
    { "type": "markdown", "content": "👋 Hi! I'm the **iGOT Karmayogi** support assistant.\n\nWhat can I help you with today?" },
    { "type": "quick_replies", "disable_input": true, "choices": [
        { "id": "CERTIFICATE_DOWNLOAD", "label": "🎓 Certificate issue" },
        { "id": "COURSE_PROGRESS_ISSUE", "label": "📊 Course progress issue" }
      ]
    }
  ]
}
```

📋 **Copy:** `session_id` → use it as `{session_id}` in every subsequent request.  
👀 **Look at:** `quick_replies.choices[].id` — these are the 5 category buttons. Step 2 picks one.

---

#### Step 2 — Select category: Content Related Issue

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "__cat__Content Related Issue"}'
```

**Response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "awaiting_user",
  "flow_id": null,
  "current_node": null,
  "activities": [
    { "type": "markdown", "content": "Please choose the specific issue you're facing:" },
    { "type": "quick_replies", "disable_input": true, "choices": [
        { "id": "CERTIFICATE_DOWNLOAD",  "label": "🎓 Certificate issue" },
        { "id": "COURSE_PROGRESS_ISSUE", "label": "📊 Course progress issue" },
        { "id": "RESOURCE_NOT_OPENING",  "label": "📂 Resource not opening" },
        { "id": "FEEDBACK_RATING_ISSUE", "label": "📝 Feedback / Rating issue" },
        { "id": "FIND_COURSE",           "label": "🔍 Can't find a course or event" },
        { "id": "UNENROLL_REQUEST",      "label": "🚫 Unenroll from a course/program/event" }
      ]
    }
  ]
}
```

👀 **Look at:** sub-flow `choices[].id` — these are now flow IDs. Step 3 picks one.

---

#### Step 3 — Select flow: CERTIFICATE_DOWNLOAD

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}'
```

**Response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "awaiting_user",
  "flow_id": "CERTIFICATE_DOWNLOAD",
  "current_node": "ask_cert_type",
  "activities": [
    { "type": "markdown", "content": "Which type of certificate?" },
    { "type": "quick_replies", "disable_input": true, "choices": [
        { "id": "course", "label": "Course certificate" },
        { "id": "event",  "label": "Event certificate" }
      ]
    }
  ]
}
```

👀 **Look at:** the new `quick_replies.choices[].id` values — pass one as `choice_id` in Step 4.

---

#### Step 4 — Pick certificate type: course

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "course"}'
```

**Response:**
```json
{
  "status": "awaiting_user",
  "current_node": "pick_course",
  "activities": [
    { "type": "markdown", "content": "Which course?" },
    {
      "type": "picker",
      "picker_id": "course_picker",
      "placeholder": "Search your course...",
      "items": [
        { "id": "do_1142232871610777601410", "label": "Micronutrient Supplementation", "meta": "Completed · 21 Jul 2025" },
        { "id": "do_1141985687873863681190", "label": "Foundation Course on AI",        "meta": "Completed · 14 Jun 2025" }
      ]
    }
  ]
}
```

📋 **Copy from `items[]`:** the `id` and `label` of the course you want to select → use them in Step 5.

---

#### Step 5 — Pick a course from the picker

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{
    "action": "pick_item",
    "picker_id": "course_picker",
    "item_id": "do_1142232871610777601410",
    "item_label": "Micronutrient Supplementation"
  }'
```

**Response:**
```json
{
  "status": "awaiting_user",
  "current_node": "ask_resolved",
  "activities": [
    { "type": "markdown", "content": "Your certificate for **Micronutrient Supplementation** is available.\n\nDid this resolve your issue?" },
    { "type": "quick_replies", "disable_input": true, "choices": [
        { "id": "yes_resolved", "label": "✅ Yes — resolved" },
        { "id": "no_still_issue", "label": "❌ No — still an issue" }
      ]
    }
  ]
}
```

👀 **Look at:** the two `choice_id` options. Pick `yes_resolved` (Step 6a) or `no_still_issue` (Step 6b).

---

#### Step 6 — Confirm resolved

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "yes_resolved"}'
```

**Response:**
```json
{
  "status": "satisfied",
  "flow_id": "CERTIFICATE_DOWNLOAD",
  "current_node": null,
  "activities": [
    { "type": "markdown", "content": "Great! Glad that helped. 🙏" },
    { "type": "end", "outcome": "self_served", "content": "Glad I could help!" }
  ]
}
```

✅ **Done.** `status: "satisfied"` and `type: "end"` signal the conversation is complete. Show the outcome banner and a "Start over" button.

---

### Example B — Access revoked, ticket raised

#### Step 1 — Start session

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

📋 **Copy:** `session_id` from the response.

---

#### Step 2 — Select category: Profile & User Management

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "__cat__Profile & User Management"}'
```

**Response:** sub-flow menu with Access revoked, Profile verification, etc.

---

#### Step 3 — Select flow: ACCESS_REVOKED

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "ACCESS_REVOKED"}'
```

**Response (abridged):**
```json
{
  "status": "awaiting_user",
  "current_node": "ask_user_role",
  "activities": [
    { "type": "markdown", "content": "What is your role on the platform?" },
    { "type": "quick_replies", "choices": [
        { "id": "learner",   "label": "Learner" },
        { "id": "mdo_admin", "label": "MDO Admin" },
        { "id": "spo",       "label": "SPO" }
      ]
    }
  ]
}
```

👀 **Look at:** `quick_replies.choices[].id` — pick the relevant role for Step 4.

---

#### Step 4 — Select role

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "learner"}'
```

Continue sending `select_choice` based on `quick_replies.choices` in each response, until you reach the end. When a ticket is raised, the final response looks like:

```json
{
  "status": "ticket_raised",
  "ticket_id": "12345678",
  "activities": [
    { "type": "markdown", "content": "✅ Ticket #12345678 raised. The L2 team will reach out within 2 business days." },
    { "type": "end", "outcome": "ticket_raised", "content": "Your ticket has been raised." }
  ]
}
```

📋 **Show** `ticket_id` to the user in the UI.

---

### Example C — Invalid topic choice (edge case)

#### Step 1 — Start session

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

📋 **Copy:** `session_id`.

---

#### Step 2 — Send a garbage choice_id (category phase)

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "BOGUS"}'
```

**Response (HTTP 200 — not an error):** 5 category buttons re-shown.

---

#### Step 3 — Pick a valid category, then send a garbage flow choice_id

```bash
# First pick a category
curl ... -d '{"action": "select_choice", "choice_id": "__cat__CA/APAR Issue"}'

# Then send a bad flow id
curl ... -d '{"action": "select_choice", "choice_id": "BOGUS_FLOW"}'
```

**Response:** Only the CA/APAR Issue sub-flows are re-shown (not all flows, not top categories).

**Do not treat either case as an error** — just re-render what the server returns.

---

### Example D — Hindi language session

#### Step 1 — Start session in Hindi

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "hi"}'
```

📋 **Copy:** `session_id`.

---

#### Step 2 — Select a topic

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}'
```

**Response:**
```json
{
  "status": "awaiting_user",
  "activities": [
    { "type": "markdown", "content": "आपका प्रमाणपत्र किस प्रकार का है?" },
    { "type": "quick_replies", "choices": [
        { "id": "course", "label": "कोर्स प्रमाणपत्र" },
        { "id": "event",  "label": "इवेंट प्रमाणपत्र" }
      ]
    }
  ]
}
```

⚠️ **Note:** `choice_id` values (`"course"`, `"event"`) stay in English — they are internal identifiers. Only the `label` is translated. Always send back the untouched `id` value.

---

### Example E — Email input validation

#### Step 1 — Start session

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

📋 **Copy:** `session_id`.

---

#### Step 2 — Select category, then a flow that asks for email (e.g. COURSE_PROGRESS_ISSUE)

```bash
# Pick category
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "__cat__Content Related Issue"}'

# Pick flow
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "COURSE_PROGRESS_ISSUE"}'
```

**Response:**
```json
{
  "status": "awaiting_user",
  "activities": [
    { "type": "markdown", "content": "Please share the MDO admin's registered email." },
    { "type": "input", "input_id": "collected.email", "input_placeholder": "admin@example.gov.in" }
  ]
}
```

👀 **Look at:** `activities[1].type == "input"` → send `send_message` with the user's text.

---

#### Step 3a — Send an invalid email

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "send_message", "text": "not-a-valid-email"}'
```

**Response (HTTP 200 — graph state unchanged, user gets another chance):**
```json
{
  "status": "awaiting_user",
  "activities": [
    { "type": "markdown", "content": "❌ That doesn't look like a valid email address.\nPlease enter a valid email, e.g. **name@example.com**" },
    { "type": "input", "input_id": "collected.email", "input_placeholder": "admin@example.gov.in" }
  ]
}
```

The same `input` is re-shown. Try again with a valid email.

---

#### Step 3b — Send a valid email

```bash
curl -s -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/{session_id} \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "send_message", "text": "admin@nic.gov.in"}'
```

**Response:**
```json
{
  "status": "awaiting_user",
  "current_node": "ask_issue_type",
  "activities": [
    { "type": "markdown", "content": "What type of update do you need?" },
    { "type": "quick_replies", "choices": [...] }
  ]
}
```

✅ The email was accepted and the flow advanced to the next node.

---

## 11. Multi-language Support

Pass `language` when starting a session. Supported values: `"en"`, `"hi"`, and other BCP-47 codes supported by the translation chain (Gemini → Google Translate → Bhashini).

```json
POST /ai-chatbot/v1/sessions/create
{ "channel": "web", "language": "hi" }
```

All bot messages in `activities` will be in the requested language.

**Important:** `choice_id` values are always English internal identifiers — never translate or modify them before sending back.

**Language fallback:** If translation fails for any reason, the original English text is returned — the session never crashes due to translation errors.

---

## 12. Environments and Base URLs

There are two ways to call the chatbot API depending on your integration type:

### Option A — Kong Direct (mobile / backend)

Two auth headers required on every request:

| Header | Value |
|--------|-------|
| `x-authenticated-user-token` | Keycloak JWT (user's portal session token) |
| `Authorization` | `Bearer <kong-jwt>` (API gateway token) |

| Environment | Base URL |
|-------------|----------|
| Local dev | `http://localhost:8000/ai-chatbot/v1` |
| Dev | `https://portal.dev.karmayogibharat.net/api/ai/chatbot/v1` |

```bash
curl -X POST https://portal.dev.karmayogibharat.net/api/ai/chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: <keycloak-jwt>" \
  -H "Authorization: Bearer <kong-jwt>" \
  -d '{"channel": "web", "language": "en"}'
```

---

### Option B — UI Proxy (web frontend)

**Recommended for web team.** Auth is handled automatically via the browser session cookie — no JWT setup needed. The portal proxy resolves the session and forwards requests to the chatbot service internally.

Only one header required:

| Header | Value |
|--------|-------|
| `cookie` | `connect.sid=<session_cookie>` |

**How to get the cookie:**
1. Open the portal in Chrome and log in
2. DevTools → Application → Cookies → find `connect.sid`
3. Copy the value

| Environment | Base URL |
|-------------|----------|
| Dev | `https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1` |
| UAT | `https://portal.uat.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1` |

```bash
# Create session via UI proxy
curl -X POST https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "cookie: connect.sid=<session_cookie>" \
  -d '{"channel": "web", "language": "en"}'

# Send a turn
curl -X POST https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1/sessions/turn/<session_id> \
  -H "Content-Type: application/json" \
  -H "cookie: connect.sid=<session_cookie>" \
  -d '{"action": "select_choice", "choice_id": "AUTO_LOGOUT_CRASH", "user_says": "Auto logout / App crash"}'
```

> **Postman:** Use the `iGOT_Chatbot_WebProxy_API` collection — it has `session_cookie` as a collection variable and all requests pre-configured for the proxy.

---

### Local dev (`AUTH_DISABLED=true`)

No Kong, no cookie. Pass any UUID as `x-authenticated-user-token`:

```bash
curl -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

---

## 13. API Reference

Complete parameter reference for every endpoint. Each section shows exactly what to send and what you get back.

**All paths are relative to your base URL.** See §12 for base URLs per environment.

**Auth headers at a glance:**

| | Local dev | Kong (dev/UAT) | UI Proxy (web) |
|--|-----------|----------------|----------------|
| `Content-Type` | `application/json` | `application/json` | `application/json` |
| `x-authenticated-user-token` | Any UUID | Keycloak JWT | ❌ Not needed |
| `Authorization` | ❌ Not needed | `Bearer <kong_jwt>` | ❌ Not needed |
| `cookie` | ❌ Not needed | ❌ Not needed | `connect.sid=<value>` |

---

### Health check

`GET /health`

No auth required. Returns `{"status": "ok"}` when the server is running. Use for uptime monitoring / k8s liveness probes.

```bash
curl http://localhost:8000/health
# → {"status": "ok"}
```

---

### 1. Start a new session

`POST /ai-chatbot/v1/sessions/create`

**What it does:** Creates a conversation, saves it in Redis, and returns the greeting message + category menu (5 buttons). The user must first pick a category, then pick the specific flow from the sub-menu shown in the next turn.

**When to call:** On app/page open when no active session exists, or when the user taps "Start over".

**Request body:**

| Field | Type | Required? | Default | Allowed values | Description |
|-------|------|-----------|---------|----------------|-------------|
| `channel` | string | Optional | `"web"` | `"web"`, `"mobile"`, `"whatsapp"`, `"voice"` | Where the user is accessing from. Affects session TTL (WhatsApp gets 24h instead of 30 min). |
| `language` | string | Optional | `"en"` | Any BCP-47 code: `"en"`, `"hi"`, etc. | Language for all bot messages in this session. |

**Response fields:**

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `session_id` | UUID string | ✅ | **Save this immediately.** Pass it in every turn and history call. |
| `activities` | array | ✅ | Bot messages to render. Always contains one `markdown` greeting + one `quick_replies` topic menu. |
| `status` | string | ✅ | Always `"awaiting_user"` on session start. |
| `flow_id` | null | ✅ | Always `null` — no topic selected yet. |
| `current_node` | null | ✅ | Always `null` — no flow running yet. |
| `ticket_id` | null | ✅ | Always `null` — no ticket raised yet. |

**Curl — local dev:**
```bash
curl -X POST http://localhost:8000/ai-chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"channel": "web", "language": "en"}'
```

**Curl — Kong (dev):**
```bash
curl -X POST https://portal.dev.karmayogibharat.net/api/ai/chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: <keycloak-jwt>" \
  -H "Authorization: Bearer <kong-jwt>" \
  -d '{"channel": "web", "language": "en"}'
```

**Curl — UI Proxy (web dev):**
```bash
curl -X POST https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1/sessions/create \
  -H "Content-Type: application/json" \
  -H "cookie: connect.sid=<your-cookie>" \
  -d '{"channel": "web", "language": "en"}'
```

**Example response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "activities": [
    {
      "type": "markdown",
      "content": "👋 Hi! I'm the **iGOT Karmayogi** support assistant.\n\nWhat can I help you with today?",
      "disable_input": false
    },
    {
      "type": "quick_replies",
      "choices": [
        { "id": "__cat__Profile & User Management",          "label": "Profile & User Management" },
        { "id": "__cat__Content Related Issue",              "label": "Content Related Issue" },
        { "id": "__cat__CA/APAR Issue",                      "label": "CA/APAR Issue" },
        { "id": "__cat__Recognition & Engagement",           "label": "Recognition & Engagement" },
        { "id": "__cat__Application Crashing / Not Loading", "label": "Application Crashing / Not Loading" }
      ],
      "disable_input": true
    }
  ],
  "status": "awaiting_user",
  "flow_id": null,
  "current_node": null,
  "ticket_id": null
}
```

> **Next step:** copy `session_id` and store it in `localStorage`. The first turn must send one of the `__cat__*` category `choice_id` values. The server will respond with the sub-flows for that category.

---

### 2. Check for an active session

`GET /ai-chatbot/v1/sessions/list`

**What it does:** Looks up whether the current user already has an active session in Redis. Returns the `session_id` if found, or `null` if not.

**When to call:** On every app open / page load — before deciding whether to resume or start fresh.

**Request body:** None (GET request).

**Path parameters:** None.

**Response fields:**

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `session_id` | UUID or `null` | ✅ | The active session UUID. `null` when no active session, session expired, or Redis is unavailable. |
| `status` | string or `null` | Optional | Current session status, e.g. `"in_flow"` or `"selecting_topic"`. Only present when session exists and server has it in memory. |
| `flow_id` | string or `null` | Optional | The flow currently in progress, e.g. `"CERTIFICATE_DOWNLOAD"`. Only present when a flow is running. |

**What to do with the response:**

| `session_id` value | What to do next |
|-------------------|-----------------|
| A UUID | Call `GET /sessions/history/{session_id}` to load the conversation thread, then continue sending turns. |
| `null` | Call `POST /sessions/create` to start a new session. |

**Curl — local dev:**
```bash
curl http://localhost:8000/ai-chatbot/v1/sessions/list \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001"
```

**Curl — Kong (dev):**
```bash
curl https://portal.dev.karmayogibharat.net/api/ai/chatbot/v1/sessions/list \
  -H "x-authenticated-user-token: <keycloak-jwt>" \
  -H "Authorization: Bearer <kong-jwt>"
```

**Curl — UI Proxy (web dev):**
```bash
curl https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1/sessions/list \
  -H "cookie: connect.sid=<your-cookie>"
```

**Example response — active session exists:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "in_flow",
  "flow_id": "CERTIFICATE_DOWNLOAD"
}
```

**Example response — no active session:**
```json
{
  "session_id": null
}
```

---

### 3. Send a user action (turn)

`POST /ai-chatbot/v1/sessions/turn/{session_id}`

**What it does:** Sends the user's action (button tap, typed message, or picker selection) to the bot and returns the next set of messages. This is the main call you repeat in a loop until the conversation ends.

**When to call:** Every time the user does something — taps a button, types text, picks from a list.

**Path parameters:**

| Parameter | Type | Required? | Description |
|-----------|------|-----------|-------------|
| `session_id` | UUID | ✅ | The session UUID returned by `POST /sessions/create`. |

**Request body:**

The `action` field is always required. The other fields depend on what action you're sending:

| Field | Type | Required? | Used with action | Description |
|-------|------|-----------|-----------------|-------------|
| `action` | string | ✅ Always | All | Type of action. Must be one of the values below. |
| `choice_id` | string | ✅ | `select_choice` | The `id` from `quick_replies.choices[]` that the user tapped. Never the `label` — always the `id`. |
| `user_says` | string | Optional | `select_choice` | Human-readable label for history display (e.g. `"Certificate issue"`). Not used by the engine. |
| `text` | string | ✅ | `send_message` | The text the user typed in the input box. |
| `picker_id` | string | ✅ | `pick_item` | The `picker_id` from the `picker` activity (e.g. `"course_picker"`). |
| `item_id` | string | ✅ | `pick_item` | The `id` of the item the user tapped in the picker list. |
| `item_label` | string | ✅ | `pick_item` | The `label` of the selected item. Used in ticket summaries — must match what was shown. |
| `other_query` | string | ✅ | `request_other` | Free text the user typed after tapping the "My item isn't listed" / "Other" button. |

**Allowed `action` values and when to use each:**

| `action` value | When to send it | What the bot just showed you |
|---------------|-----------------|------------------------------|
| `"select_choice"` | User tapped a button | A `quick_replies` activity |
| `"send_message"` | User typed and submitted text | An `input` activity |
| `"pick_item"` | User selected an item from a list | A `picker` activity |
| `"request_other"` | User tapped "My item isn't listed" and typed text | A `picker` activity with `other_option` |
| `"start"` | User wants to restart from the top menu | Any point in the conversation |

**Request body examples:**

```json
// Button tap (select_choice)
{ "action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD" }

// Text input (send_message)
{ "action": "send_message", "text": "admin@example.gov.in" }

// Picker selection (pick_item)
{
  "action": "pick_item",
  "picker_id": "course_picker",
  "item_id": "do_1141985687873863681190",
  "item_label": "Foundation Course on AI"
}

// "Other" free text (request_other)
{ "action": "request_other", "other_query": "My ministry is not in the list" }
```

**Response fields:**

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `session_id` | UUID | ✅ | Same session UUID — useful for confirming you got the right response. |
| `activities` | array | ✅ | One or more bot activities to render in order (see §5 for all types). |
| `status` | string | ✅ | Current conversation state. See status table in §6. |
| `flow_id` | string or `null` | ✅ | Active flow ID (e.g. `"CERTIFICATE_DOWNLOAD"`), `null` before topic selection. |
| `current_node` | string or `null` | ✅ | Internal YAML node ID — useful for debugging, not for displaying to users. |
| `ticket_id` | string or `null` | ✅ | Zoho Desk ticket ID. Only set when `status = "ticket_raised"`. Show this in the UI. |

**Curl — local dev:**
```bash
# Button tap
curl -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}'

# Text input
curl -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "send_message", "text": "admin@example.gov.in"}'

# Picker selection
curl -X POST http://localhost:8000/ai-chatbot/v1/sessions/turn/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001" \
  -d '{"action": "pick_item", "picker_id": "course_picker", "item_id": "do_1141985687873863681190", "item_label": "Foundation Course on AI"}'
```

**Curl — Kong (dev):**
```bash
curl -X POST https://portal.dev.karmayogibharat.net/api/ai/chatbot/v1/sessions/turn/<session_id> \
  -H "Content-Type: application/json" \
  -H "x-authenticated-user-token: <keycloak-jwt>" \
  -H "Authorization: Bearer <kong-jwt>" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}'
```

**Curl — UI Proxy (web dev):**
```bash
curl -X POST https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1/sessions/turn/<session_id> \
  -H "Content-Type: application/json" \
  -H "cookie: connect.sid=<your-cookie>" \
  -d '{"action": "select_choice", "choice_id": "CERTIFICATE_DOWNLOAD"}'
```

**Example response — conversation continuing:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "activities": [
    { "type": "markdown", "content": "Which type of certificate?" },
    {
      "type": "quick_replies",
      "choices": [
        { "id": "course", "label": "Course certificate" },
        { "id": "event",  "label": "Event certificate" }
      ],
      "disable_input": true
    }
  ],
  "status": "awaiting_user",
  "flow_id": "CERTIFICATE_DOWNLOAD",
  "current_node": "ask_cert_type",
  "ticket_id": null
}
```

**Example response — conversation complete (ticket raised):**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "activities": [
    { "type": "markdown", "content": "✅ Ticket #12345678 raised. L2 team will reach out within 2 business days." },
    { "type": "end", "outcome": "ticket_raised", "content": "Your ticket has been raised." }
  ],
  "status": "ticket_raised",
  "flow_id": "ACCESS_REVOKED",
  "current_node": null,
  "ticket_id": "12345678"
}
```

> **When you see `type: "end"` in `activities[]`** — the conversation is over. Show the outcome banner, show a "Start new conversation" button, and stop sending turns to this session.

---

### 4. Get conversation history

`GET /ai-chatbot/v1/sessions/history/{session_id}`

**What it does:** Returns every message in the session from start to the current state, in chronological order. Use this to rebuild the chat thread when resuming a session.

**When to call:** After `GET /sessions/list` returns a `session_id` — load history to render the full thread before continuing.

**Path parameters:**

| Parameter | Type | Required? | Description |
|-----------|------|-----------|-------------|
| `session_id` | UUID | ✅ | The session UUID. |

**Request body:** None (GET request).

**Response fields:**

| Field | Type | Always present? | Description |
|-------|------|-----------------|-------------|
| `session_id` | UUID | ✅ | The session UUID. |
| `messages` | array | ✅ | All messages in chronological order. Empty array `[]` if no topic has been selected yet. |

**Each item in `messages[]`:**

| Field | Type | Present when | Description |
|-------|------|-------------|-------------|
| `role` | string | ✅ Always | `"user"` or `"bot"`. |
| `activities` | array | Bot messages only (`role: "bot"`) | The activities to render — same format as any turn response. |
| `action` | string or `null` | User messages only (`role: "user"`) | The action type the user sent, e.g. `"select_choice"`. |
| `text` | string or `null` | User messages only | Human-readable display text of what the user did, e.g. `"Certificate issue"`. Render as the user's chat bubble. |
| `ts` | string | ✅ Always | ISO 8601 UTC timestamp, e.g. `"2026-06-16T10:01:00Z"`. |

**Rendering rules:**
- `role: "bot"` → pass `activities[]` to your normal activity renderer (same code as for turn responses)
- `role: "user"` → render `text` as the user's message bubble
- Entries are in chronological order — render top to bottom
- The **last entry is always `role: "bot"`** — its `activities[]` contain the current pending prompt waiting for input

**Curl — local dev:**
```bash
curl http://localhost:8000/ai-chatbot/v1/sessions/history/550e8400-e29b-41d4-a716-446655440000 \
  -H "x-authenticated-user-token: 00000000-0000-0000-0000-000000000001"
```

**Curl — Kong (dev):**
```bash
curl https://portal.dev.karmayogibharat.net/api/ai/chatbot/v1/sessions/history/<session_id> \
  -H "x-authenticated-user-token: <keycloak-jwt>" \
  -H "Authorization: Bearer <kong-jwt>"
```

**Curl — UI Proxy (web dev):**
```bash
curl https://portal.dev.karmayogibharat.net/apis/proxies/v8/ai/chatbot/v1/sessions/history/<session_id> \
  -H "cookie: connect.sid=<your-cookie>"
```

**Example response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "messages": [
    {
      "role": "user",
      "action": "select_choice",
      "text": "Certificate issue",
      "ts": "2026-06-16T10:01:00Z"
    },
    {
      "role": "bot",
      "activities": [
        { "type": "markdown", "content": "Which type of certificate?" },
        { "type": "quick_replies", "choices": [
            { "id": "course", "label": "Course certificate" },
            { "id": "event",  "label": "Event certificate" }
          ], "disable_input": true
        }
      ],
      "ts": "2026-06-16T10:01:01Z"
    }
  ]
}
```

**Example — no flow started yet (empty history):**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "messages": []
}
```
When `messages` is empty, the user only saw the greeting and topic menu but never picked a topic. Treat this the same as no session — call `POST /sessions/create` to start fresh.

---

## 14. Current Limitations

| Item | Status |
|------|--------|
| `GET /admin/sessions/{id}/trace` | Not yet wired (returns 501) |
| `DELETE /admin/sessions/{id}` | Not yet wired (returns 501) |
| Free-text before topic selection | Bot re-shows menu; no NLP intent matching |
| WebSocket / streaming | All activities sent at once; no token-by-token streaming |
