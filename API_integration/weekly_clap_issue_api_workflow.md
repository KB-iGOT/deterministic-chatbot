# UC-WC: Weekly Clap Not Updated / Reset to Zero / Not Credited — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1 → GET  /api/user/v1/activity/insights/{user_id}
              ↓ Fetch weekly timespent for last 4 weeks (w1 = current, w4 = oldest)
                + total_claps, startDate, endDate
              ↓
         IF 404 (not found)                  → STOP (no activity data — new/inactive user)
         IF all w1–w4 timespent == null       → STOP (data unavailable — sync pending)
         IF API error / timeout              → STOP (error fallback — offer to raise ticket)
         ELSE                                → continue ↓

TRAVERSAL → Check w1 → w2 → w3 → w4 (find first week where timespent < 60 min)
              ↓
         RESET FOUND (any week < 60 min)    → Explain reset: show exact time and 60-min rule
                                               → User agrees    → self-served, close
                                               → User disagrees → raise Zoho ticket
         NO RESET (all 4 weeks ≥ 60 min)   → Discrepancy detected → raise Zoho ticket

         ✓ Diagnosis complete — no further API calls needed
```

---

## Step 1 — Activity Insights

> Fetches the user's weekly learning time spent for the last 4 weeks and the total weekly clap count to diagnose whether the clap reset was legitimate or a system discrepancy.

**Endpoint:** `GET /api/user/v1/activity/insights/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/v1/activity/insights/{user_id}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> No request body required. `{user_id}` is the hashed user identifier (`ctx.user_id_hash`).

### Response Fields Used

| JSON Path | Stored As | Transform | Used For |
|---|---|---|---|
| `$.weekly-claps.w1.timespent` | `collected.w1_mins` | — | Time spent in current/most-recent week (minutes) |
| `$.weekly-claps.w2.timespent` | `collected.w2_mins` | — | Time spent in week 2 (minutes) |
| `$.weekly-claps.w3.timespent` | `collected.w3_mins` | — | Time spent in week 3 (minutes) |
| `$.weekly-claps.w4.timespent` | `collected.w4_mins` | — | Time spent in oldest available week (minutes) |
| `$.weekly-claps.total_claps` | `collected.total_claps` | — | Current total weekly clap count; shown in ticket description |
| `$.weekly-claps.startDate` | `collected.week_start_date` | — | Start date of the current week (raw) |
| `$.weekly-claps.endDate` | `collected.week_end_date` | — | End date of the current week (raw) |
| `$.weekly-claps.startDate` | `collected.w1_label` | `week_label_w1` | Human-readable date range for w1 (e.g. "11 May – 17 May 2026") |
| `$.weekly-claps.startDate` | `collected.w2_label` | `week_label_w2` | Human-readable date range for w2 |
| `$.weekly-claps.startDate` | `collected.w3_label` | `week_label_w3` | Human-readable date range for w3 |
| `$.weekly-claps.startDate` | `collected.w4_label` | `week_label_w4` | Human-readable date range for w4 |

> **`timespent` values are in MINUTES.** The reset threshold is **60 minutes per week**.

### Sample Response (trimmed)

```json
{
  "responseCode": "OK",
  "result": {
    "weekly-claps": {
      "total_claps": 3,
      "startDate": "2026-05-11",
      "endDate": "2026-05-17",
      "w1": { "timespent": 72.5 },
      "w2": { "timespent": 38.0 },
      "w3": { "timespent": 91.3 },
      "w4": { "timespent": 65.0 }
    }
  }
}
```

### `week_label_wN` Transform Logic

Each `week_label_wN` transform derives a human-readable week label by offsetting the `startDate` backward by `(N-1)` full weeks.

| Transform | Offset from `startDate` | Example Output |
|---|---|---|
| `week_label_w1` | 0 weeks back (current week) | `"11 May – 17 May 2026"` |
| `week_label_w2` | 1 week back | `"4 May – 10 May 2026"` |
| `week_label_w3` | 2 weeks back | `"27 Apr – 3 May 2026"` |
| `week_label_w4` | 3 weeks back | `"20 Apr – 26 Apr 2026"` |

### Error / Edge-Case Handling

| Condition | HTTP Status | Routed To | Behaviour |
|---|---|---|---|
| No activity data for user (new / inactive account) | `404` | `no_insights_data` | Inform user; offer ticket |
| All `w1–w4` timespent values are `null` in a `200` response | `200` | `no_week_data` | Sync may be pending; advise waiting 24 hours; offer ticket |
| Timeout or server error | `5xx` / timeout | `api_error_fallback` | Generic error message; offer ticket |

---

## Decision Logic — Weekly Traversal

> After a successful API response with at least one non-null week, the flow checks weeks in order **w1 → w2 → w3 → w4**, stopping at the **first week where `timespent < 60`**.

```
w1_mins < 60  →  explain reset using w1 data
     ↓ (else)
w2_mins < 60  →  explain reset using w2 data
     ↓ (else)
w3_mins < 60  →  explain reset using w3 data
     ↓ (else)
w4_mins < 60  →  explain reset using w4 data
     ↓ (else)
all 4 weeks ≥ 60  →  discrepancy → raise ticket
```

| Week Checked | Condition | Outcome |
|---|---|---|
| w1 (most recent) | `timespent < 60` | Show reset explanation for w1; user confirms or disputes |
| w2 | `timespent < 60` | Show reset explanation for w2; user confirms or disputes |
| w3 | `timespent < 60` | Show reset explanation for w3; user confirms or disputes |
| w4 (oldest) | `timespent < 60` | Show reset explanation for w4 + warning that data beyond 4 weeks is unavailable |
| All weeks | `timespent ≥ 60` | Discrepancy detected — raise ticket with full weekly data |

> **Limitation:** The Insights API covers only the **last 4 weeks** (w1–w4). If the streak broke more than 4 weeks ago, the reset point is outside the available data window and a ticket is raised automatically.

---

## Final Routing Decision

| Priority | Condition | Resolution Branch |
|---|---|---|
| 1 | API returns `404` | No activity data — explain 60-min rule; offer ticket |
| 2 | All `wN_mins == null` in `200` response | Data unavailable / sync pending — advise waiting; offer ticket |
| 3 | API error / timeout | Error fallback — offer ticket |
| 4 | First week found where `timespent < 60` | Reset explanation — show exact minutes and 60-min rule; user can agree (self-served) or dispute (ticket) |
| 5 | All 4 weeks `timespent ≥ 60` | Discrepancy — auto-raise ticket for technical team |

---

## Ticket Context Passed to LLM

Both escalation paths (`disagree_auto_ticket` and `discrepancy_auto_ticket`) pass the following collected fields to the LLM for Zoho ticket generation:

| Field | Content |
|---|---|
| `total_claps` | Current weekly clap count |
| `w1_label` + `w1_mins` | Date range and time spent for week 1 |
| `w2_label` + `w2_mins` | Date range and time spent for week 2 |
| `w3_label` + `w3_mins` | Date range and time spent for week 3 |
| `w4_label` + `w4_mins` | Date range and time spent for week 4 |
| `email` | User's registered email address |

**Ticket subjects generated:**
- User disputes: `"Weekly Clap Issue — User Disputes Reset Explanation"`
- Discrepancy: `"Weekly Clap Discrepancy — Threshold Met But Clap Not Credited"`

---

## API Dependency Table

| Step | Endpoint | Purpose | Extracted Field | Passed To |
|---|---|---|---|---|
| 1 | `GET /api/user/v1/activity/insights/{user_id}` | Fetch weekly timespent for last 4 weeks | `w1.timespent` → `w1_mins` | Traversal: branch if `< 60` |
| 1 | `GET /api/user/v1/activity/insights/{user_id}` | Fetch weekly timespent for last 4 weeks | `w2.timespent` → `w2_mins` | Traversal: branch if `< 60` |
| 1 | `GET /api/user/v1/activity/insights/{user_id}` | Fetch weekly timespent for last 4 weeks | `w3.timespent` → `w3_mins` | Traversal: branch if `< 60` |
| 1 | `GET /api/user/v1/activity/insights/{user_id}` | Fetch weekly timespent for last 4 weeks | `w4.timespent` → `w4_mins` | Traversal: branch if `< 60` |
| 1 | `GET /api/user/v1/activity/insights/{user_id}` | Fetch total clap count and date range | `total_claps`, `startDate`, `endDate` | Ticket description; week label transforms |
| 1 | `GET /api/user/v1/activity/insights/{user_id}` | Compute human-readable week labels | `startDate` + `week_label_wN` transform | Reset explanation messages; ticket description |
