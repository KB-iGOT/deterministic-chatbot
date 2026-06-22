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

#### 2. `kp_monthly_rank` (Course Context)
Counts how many courses were completed in the same calendar month *before or at the same time* as the selected course. Used to enforce the **monthly karma limit** (only the first 4 completed courses in a month get points).

#### 3. `kp_event_credited` (Event Context)
Scans `kpList` for any entry matching the `event_id` to determine if karma points were credited for event participation.

---

## Business Logic Evaluation

After data is fetched and transformed, the chatbot evaluates:

### Course Logic
- **Training Plan (ACBP = True):** Bypasses the monthly limit. If points are missing or incorrect (should be 15 with assessment, 10 without), a support ticket is raised.
- **Standard Course (ACBP = False):** Subject to the monthly limit. If `monthly_rank >= 5`, informs the user of the 4-course limit. Otherwise, if points are missing or not 5, raises a ticket.

### Event Logic
- **4-Hour Live Rule Check:** Compares `completedOn` and `startDateTimeInEpoch`. If the difference is > 4 hours, it's considered non-live participation and no points are awarded. If ≤ 4 hours, it checks `kp_event_credited` and raises a ticket if missing.

---

## API Dependency Table

| Step | Endpoint | Method | Context | Key Data Extracted |
|---|---|---|---|---|
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Course Selection | Completed course details |
| 1 | `/api/user/private/v1/events/list/{user_id}` | GET | Event Selection | Completed event details & start time |
| 2 | `/api/karmapoints/read` | POST | Karma Checking | Karma history, ACBP flag, credited points |
