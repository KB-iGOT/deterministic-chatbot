# Karma Points Issue — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot for resolving Karma Points issues. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

The flow branches into three primary categories based on the user's issue:

**1. Course Karma Points Not Credited**
```
STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Fetches recently completed courses for the user to select
                ↓ User selects course → extract course_id, course_name
                ↓
STEP 2   → POST /api/karmapoints/read
                ↓ Fetch user's karma points history
                ↓ Transform: `kp_status_by_id` and `kp_monthly_rank`
                ↓ Check ACBP (Training Plan) flag, monthly rank, and credited status
```

**2. Event Karma Points Not Credited**
```
STEP 1   → GET /api/user/private/v1/events/list/{user_id}
                ↓ Fetches completed events for the user to select
                ↓ Filter by status=2 (Completed)
                ↓ User selects event → extract event_id, event_name, start_time
                ↓
STEP 2   → POST /api/karmapoints/read
                ↓ Fetch user's karma points history
                ↓ Transform: `kp_event_credited`
                ↓ Check 4-hour live participation window and whether karma is credited
```

**3. Incorrect Karma Points Received**
```
STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Fetches recently completed courses for the user to select
                ↓
STEP 2   → POST /api/karmapoints/read
                ↓ Fetch user's karma points history
                ↓ Transform: `kp_status_by_id`
                ↓ Check expected vs actual points (based on ACBP and Assessment flags)
```

---

## Step 1 — Course/Event Selection

### A. Completed Course Lookup

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true,
      "status": ["Completed"]
    }
  }'
```

**Response Fields Used:**
| Field | Extracted As | Purpose |
|---|---|---|
| `result.courses[].courseId` | `course_id` | Unique ID of the course |
| `result.courses[].courseName` | `course_name` | Name of the course |
| `result.courses[].completedOn` | `completed_on_iso` | Converted to ISO timestamp for analytics |

### B. Completed Event Lookup

**Endpoint:** `GET /api/user/private/v1/events/list/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/events/list/{user_id}" \
  -H "Content-Type: application/json"
```

**Response Fields Used:**
| Field | Extracted As | Purpose |
|---|---|---|
| `result.events[].contentId` | `course_id` | Unique ID of the event |
| `result.events[].event.name` | `course_name` | Name of the event |
| `result.events[].completedOn` | `completed_on_iso` | Converted to ISO; used for 4-hour rule check |
| `result.events[].event.startDateTimeInEpoch` | `start_time_iso` | Converted to ISO; used for 4-hour rule check |

---

## Step 2 — Fetch Karma Points History

> Called for both Course and Event flows to check the actual credits applied to the user's account.

**Endpoint:** `POST /api/karmapoints/read`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/karmapoints/read" \
  -H "Content-Type: application/json" \
  -H "x-authenticated-userid: {user_id}" \
  -H "x-authenticated-user-orgid: igot" \
  -d '{
    "limit": 200,
    "offset": 9999999999999
  }'
```

### Transforms Applied on Response

The raw API returns a list (`kpList`) of all karma point entries. The workflow applies specific Python transforms (found in `app/engine/nodes/api_call_node.py`) to extract relevant data.

#### 1. `kp_status_by_id` (Course Context)
Extracts status for a specific `course_id` by looking for `operation_type == "COURSE_COMPLETION"` and `"RATING"`. It parses the nested `addinfo` JSON string to retrieve metadata.

| Key Extracted | Source | Purpose |
|---|---|---|
| `completion_credited` | Presence of `COURSE_COMPLETION` entry | Determine if completion points are credited |
| `rating_credited` | Presence of `RATING` entry | Determine if rating points are credited |
| `acbp` | `addinfo.ACBP` | Identifies if the course is part of a Training Plan |
| `has_assessment` | `addinfo.ASSESSMENT` | Identifies if the course has an assessment |
| `completion_points` | `points` on completion entry | The actual points credited for completion |
| `rating_points` | `points` on rating entry | The actual points credited for rating |
| `course_name` | `addinfo.COURSENAME` (completion, else rating) | Used to display the course name in tickets/messages |

Matching is done on `entry.context_id == course_id` (string-compared).

#### 2. `kp_monthly_rank` (Course Context)
Counts how many `COURSE_COMPLETION` entries (matched by `context_id`) fall in the same UTC calendar month, ranked by `credit_date`, *up to and including* the selected course. Used to enforce the **monthly karma limit** (only the first 4 completed courses in a month get points). Returns `0` if no matching completion entry is found for the course.

#### 3. `kp_event_credited` (Event Context)
Scans `kpList` for any entry matching the `event_id` to determine if karma points were credited for event participation.

---

## Business Logic Evaluation

After data is fetched and transformed, the chatbot evaluates:

### Course Logic — "Course Karma Points Not Credited"

This branch only checks whether completion/rating credit is **present or missing** — it does
**not** check the specific point value (that check belongs to the "Incorrect Karma Points"
branch below).

- **Both `completion_credited` and `rating_credited` = True:** Already credited — informs the user and closes.
- **Neither credited (`kp_status_by_id` is `None`, or both flags False):** Raised as a discrepancy and a support ticket is created directly — the ACBP / monthly-limit checks below are **not** evaluated in this case.
- **Exactly one of completion/rating credited (partial credit):** Checks the `acbp` flag:
  - **`acbp = True` (Training Plan):** Bypasses the monthly limit entirely — a support ticket is raised immediately (no point-value check).
  - **`acbp = False` (Standard course):** Subject to the monthly limit — if `kp_monthly_rank >= 5`, informs the user of the 4-course monthly limit (no ticket); otherwise raises a support ticket for the discrepancy.

### Incorrect Points Logic — "Received Fewer/More Points Than Expected"

This branch (separate from the one above) checks the actual `completion_points` value against
what is expected, based on `acbp` and `has_assessment`:

| Condition | Expected Points | Outcome if mismatched |
|---|---|---|
| `acbp = True`, `has_assessment = True` | 15 | Support ticket raised |
| `acbp = True`, `has_assessment = False` | 10 | Support ticket raised |
| `acbp = False` | 5 | **No ticket** — informs the user of the 4-course monthly limit (mismatch is assumed to be due to the monthly cap) |
| Points match expectation | — | Informs the user the points are correct; no ticket |

### Event Logic
- **4-Hour Live Rule Check:** Compares `completedOn` and `startDateTimeInEpoch` (via `hours_since()` on each). If the gap is > 4 hours, it's considered non-live participation and no points are awarded (no ticket). If the event's start time is unavailable, this check is skipped entirely and the flow proceeds straight to the credited check.
- **Credited Check:** If ≤ 4 hours (or start time unknown), checks `kp_event_credited` — if already `True`, informs the user it's already credited; if `False`, raises a support ticket for the discrepancy.

---

## Ticket Escalation

Every discrepancy branch above (course not-credited, event not-credited, incorrect points —
except the `acbp = False` "expected 5" mismatch, which is informational only) routes through the
shared `_zoho_ticket` fragment: `ticket_confirm` (user confirms details) → `transfer_llm`
(auto-raises the ticket, `auto_raise: silent`, using an LLM-drafted subject/description) →
`confirm_ticket`, which performs `POST /tickets` against the ZohoDesk API. This flow imports the
fragment with `cf_category: platform`, `cf_sub_category: karma_points`, `cf_flow_id:
KARMA_POINTS_ISSUE` — these are set as custom fields (`cf`) on the created ticket.

---

## API Dependency Table

| Step | Endpoint | Method | Context | Key Data Extracted |
|---|---|---|---|---|
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Course Selection (Not Credited & Incorrect Points branches) | Completed course details |
| 1 | `/api/user/private/v1/events/list/{user_id}` | GET | Event Selection | Completed event details & start time |
| 2 | `/api/karmapoints/read` | POST | Karma Checking | Karma history, ACBP flag, credited points |
| 3 | `/tickets` (ZohoDesk, via `_zoho_ticket` fragment) | POST | Ticket Escalation | Raises a support ticket for confirmed discrepancies |

> Three additional entry points from the initial menu — **Leaderboard vs Overall Mismatch**,
> **Learner Pathway Points**, and **Course Added to Training Plan After Completion** — are static
> informational messages with no API calls, and are intentionally not covered above.
