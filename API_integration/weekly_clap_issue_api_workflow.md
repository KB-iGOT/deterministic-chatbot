# UC-WC: Weekly Clap Not Updated / Reset to Zero / Not Credited — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1 → POST /api/private/user/v1/search
              ↓ Fetch user profile to retrieve rootOrgId
              ↓
STEP 2 → POST /api/chatbot/v2/insights
              ↓ Fetch weekly timespent for last 12 weeks (w1 = current, w12 = oldest)
                + total_claps, startDate, endDate
              ↓
         IF 404 (not found)                  → STOP (no activity data — new/inactive user; no ticket offered)
         IF all w1–w12 timespent == null      → STOP (routed to the SAME "no activity data" message as 404; no ticket offered)
         IF API error / timeout              → STOP (generic error message; no ticket offered)
         ELSE                                → continue ↓

TRAVERSAL → Check w1 → w2 → ... → w12 (find first week where timespent < 60 min)
              ↓
         RESET FOUND (any week < 60 min)    → Explain reset: show exact time and 60-min rule
                                               → User agrees    → self-served, close
                                               → User disagrees → raise Zoho ticket
         NO RESET (all 12 weeks ≥ 60 min)  → Discrepancy detected → raise Zoho ticket

         ✓ Diagnosis complete — no further API calls needed
```

---

## Step 1 — User Profile Fetch

> Fetches the user's profile to extract their `rootOrgId`, which is a required filter payload for the Insights API.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
        "request": {
            "filters": {
                "userId": "{user_id}"
            }
        }
      }'
```

> `{user_id}` is the hashed user identifier (`ctx.user_id_hash`).

### Response Fields Used

| JSON Path | Stored As | Transform | Used For |
|---|---|---|---|
| `$.result.response.content[0].rootOrgId` | `ctx.root_org_id` | — | Filter payload for the Insights API |

---

## Step 2 — Activity Insights (12 Weeks)

> Fetches the user's weekly learning time spent for the last 12 weeks and the total weekly clap count to diagnose whether the clap reset was legitimate or a system discrepancy.

**Endpoint:** `POST /api/chatbot/v2/insights`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/chatbot/v2/insights" \
  -H "x-authenticated-userid: {user_id}" \
  -H "Content-Type: application/json" \
  -d '{
        "request": {
            "filters": {
                "primaryCategory": "programs",
                "organisations": ["across", "{root_org_id}"]
            }
        }
      }'
```

> `{user_id}` is the hashed user identifier (`ctx.user_id_hash`), sent via the `x-authenticated-userid` header. `{root_org_id}` is obtained from Step 1 and passed inside the `organisations` filter array (alongside the fixed literal `"across"`). Note: the filter is **not** `{"root_org_id": ...}` — the actual filters are `primaryCategory: "programs"` and `organisations: ["across", root_org_id]`.

### Response Fields Used

> All fields are nested under `$.result.response.weekly-claps.*` (not top-level `$.weekly-claps.*`).

| JSON Path | Stored As | Transform | Used For |
|---|---|---|---|
| `$.result.response.weekly-claps.w1.timespent` | `collected.w1_mins` | — | Time spent in current/most-recent week (minutes) |
| `$.result.response.weekly-claps.w2.timespent` | `collected.w2_mins` | — | Time spent in week 2 (minutes) |
| ... | ... | — | ... |
| `$.result.response.weekly-claps.w12.timespent` | `collected.w12_mins` | — | Time spent in oldest available week (week 12) (minutes) |
| `$.result.response.weekly-claps.total_claps` | `collected.total_claps` | — | Current total weekly clap count; shown in ticket description |
| `$.result.response.weekly-claps.startDate` | `collected.week_start_date` | — | Start date of the current week (raw) |
| `$.result.response.weekly-claps.endDate` | `collected.week_end_date` | — | End date of the current week (raw) |
| `$.result.response.weekly-claps.startDate` | `collected.w1_label` | `week_label_w1` | Human-readable date range for w1 (e.g. "11 May – 17 May 2026") |
| `$.result.response.weekly-claps.startDate` | `collected.w2_label` | `week_label_w2` | Human-readable date range for w2 |
| ... | ... | ... | ... |
| `$.result.response.weekly-claps.startDate` | `collected.w12_label` | `week_label_w12` | Human-readable date range for w12 |

> **`timespent` values are in MINUTES.** The reset threshold is **60 minutes per week**.

### Error / Edge-Case Handling

| Condition | HTTP Status | Routed To | Behaviour |
|---|---|---|---|
| No activity data for user (new / inactive account) | `404` | `no_insights_data` | Inform user no activity data was found; ends the flow (`satisfied`) — **no ticket option offered** |
| All `w1–w12` timespent values are `null` in a `200` response | `200` | `no_insights_data` (via the `branch_has_data` guard) | Routed to the **same** "no activity data" message as the `404` case; ends the flow (`satisfied`) — **no ticket option offered**. Note: the flow defines a separate `no_week_data` node with "sync may be pending" wording, but it is dead code — no branch or `on_error`/`on_success` target in the flow points to it. |
| Timeout or server error | `5xx` / timeout | `api_error_fallback` | Generic "please try again" message; ends the flow (`satisfied`) — **no ticket option offered** |

---

## Decision Logic — Weekly Traversal

> After a successful API response with at least one non-null week, the flow checks weeks in order **w1 → w2 → ... → w12**, stopping at the **first week where `timespent < 60`**.

```
w1_mins < 60  →  explain reset using w1 data
     ↓ (else)
w2_mins < 60  →  explain reset using w2 data
     ↓ (else)
...
     ↓ (else)
w12_mins < 60 →  explain reset using w12 data
     ↓ (else)
all 12 weeks ≥ 60  →  discrepancy → raise ticket
```

| Week Checked | Condition | Outcome |
|---|---|---|
| w1 (most recent) | `timespent < 60` | Show reset explanation for w1; user confirms or disputes |
| w2..w11 | `timespent < 60` | Show reset explanation for the respective week; user confirms or disputes |
| w12 (oldest) | `timespent < 60` | Show reset explanation for w12 + warning that data beyond 12 weeks is unavailable |
| All weeks | `timespent ≥ 60` | Discrepancy detected — raise ticket with full weekly data |

> **Limitation:** The Insights API covers only the **last 12 weeks** (w1–w12). If the streak broke more than 12 weeks ago, the reset point is outside the available data window and a ticket is raised automatically.

---

## Final Routing Decision

| Priority | Condition | Resolution Branch |
|---|---|---|
| 1 | API returns `404` | No activity data found — informational message only; ends flow self-served, **no ticket offered** |
| 2 | All `wN_mins == null` in `200` response | Routed to the same "no activity data" message as the `404` case; ends flow self-served, **no ticket offered** |
| 3 | API error / timeout | Generic error fallback message; ends flow self-served, **no ticket offered** |
| 4 | First week found where `timespent < 60` | Reset explanation — show exact minutes and 60-min rule; user can agree (self-served) or dispute (ticket) |
| 5 | All 12 weeks `timespent ≥ 60` | Discrepancy — auto-raise ticket for technical team |

---

## Ticket Context Passed to LLM

Both escalation paths (`disagree_auto_ticket` and `discrepancy_auto_ticket`) pass the following collected fields to the LLM for Zoho ticket generation:

| Field | Content |
|---|---|
| `total_claps` | Current weekly clap count |
| `w1_label` + `w1_mins` | Date range and time spent for week 1 |
| ... | ... |
| `w12_label` + `w12_mins` | Date range and time spent for week 12 |
| `email` / `name` / `mobile` | User's profile details (pre-fetched by central routes.py logic) |

**Ticket subjects generated:**
- User disputes: `"Weekly Clap Issue — User Disputes Reset Explanation"`
- Discrepancy: `"Weekly Clap Discrepancy — Threshold Met But Clap Not Credited"`

### Ticket Creation Call (shared `_zoho_ticket` fragment)

> After the LLM drafts the ticket, the flow's imported `confirm_ticket` node (from `flows/_shared/_zoho_ticket.yaml`) makes the actual Zoho Desk API call. Not specific to this flow, but included here for completeness since it's a real API call in the execution path.

**Endpoint:** `POST /tickets` (Zoho Desk API integration)

Key body fields: `subject` (prefixed `[ITSM Support v2] - `), `description` (HTML table with name/user ID/email/phone + LLM-drafted summary), `email`, `phone`, `priority`, `departmentId`, and custom fields `cf_category: platform`, `cf_sub_category: weekly_clap`, `cf_flow_id: WEEKLY_CLAP_ISSUE` (set via this flow's `imports.with` params), plus `cf_source`, `cf_bot_session_id`, `cf_llm_involved`.

Response field used: `$.ticketNumber` → `collected.ticket_id` (shown to the user as the ticket confirmation ID).

---

## API Dependency Table

| Step | Endpoint | Purpose | Extracted Field | Passed To |
|---|---|---|---|---|
| 1 | `POST /api/private/user/v1/search` | Fetch user profile (body: `{"request": {"filters": {"userId": user_id_hash}}}`) | `$.result.response.content[0].rootOrgId` → `root_org_id` | Step 2 (Insights API payload, inside `organisations` filter) |
| 2 | `POST /api/chatbot/v2/insights` | Fetch weekly timespent for last 12 weeks (body filters: `primaryCategory: "programs"`, `organisations: ["across", root_org_id]`) | `$.result.response.weekly-claps.w1.timespent` → `w1_mins` (to w12) | Traversal: branch if `< 60` |
| 2 | `POST /api/chatbot/v2/insights` | Fetch total clap count and date range | `$.result.response.weekly-claps.total_claps`, `startDate`, `endDate` | Ticket description; week label transforms |
| 2 | `POST /api/chatbot/v2/insights` | Compute human-readable week labels | `$.result.response.weekly-claps.startDate` + `week_label_wN` transform | Reset explanation messages; ticket description |
