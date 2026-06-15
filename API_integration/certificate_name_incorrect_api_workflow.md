# UC-02: Incorrect Name on Certificate — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
[optional] POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Only if user cannot supply the course name — show numbered list

STEP 1   → GET   /api/user/private/v1/read/{user_id}
           POST  /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Profile read: get current firstName + lastName
                ↓ Enrollment check: is the course completed? was a certificate issued?
                ↓
           course NOT completed          → inform user, stop
           course completed, no cert     → inform user, stop
           course completed, cert issued → show current name on certificate
                ↓
           Ask: "Do you want to change the name?"
           User says NO    → close politely, stop
           User says YES   → collect correct name → confirm "change X to Y?"
                ↓

STEP 2   → PATCH /api/user/private/v1/update
                ↓ Apply new firstName / lastName (also synced into profileDetails.personalDetails)
                ↓ HTTP 200 → reply with exact re-download sentence + 8-step guide
```

---

## Optional Step — Enrolled-Course List

> Called only when the user cannot supply a course name. Shows a numbered list the user can pick from.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true,
      "status": ["In-Progress", "Completed"]
    }
  }'
```

### Response Fields Used

| Field | Used For |
|---|---|
| `result.courses[].courseName` | Displayed as numbered course list for the user to choose from |

---

## Step 1 — Profile Name Fetch + Course Validation

> A single LLM tool call (`get_certificate_name_info`) triggers both API calls below.

### 1a — Profile Read

**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.firstName` | Current first name shown on certificate |
| `result.response.lastName` | Current last name shown on certificate |
| `result.response.profileDetails.personalDetails.firstname` | Cross-check display vs personal details |
| `result.response.profileDetails.personalDetails.surname` | Cross-check display vs personal details |

**Surname-Duplication Detection** (logic inside tool, no extra API call):

| Condition | `has_surname_duplication` |
|---|---|
| `lastName` token found inside `firstName.split()` | `true` |
| Last two tokens of full name are identical | `true` |

### 1b — Course Enrollment Validation

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

Same request payload as the Optional Step above.

### Response Fields Used

| Field | Key extracted as | Purpose |
|---|---|---|
| `result.courses[].courseName` | — | Matched against user-supplied name (exact then partial) |
| `result.courses[].completionPercentage` | `course_completed` | Must be 100 to proceed |
| `result.courses[].issuedCertificates` / `course_issued_certificate_id` | `has_certificate` | Confirms a certificate was actually generated |

### Decision After Step 1

| Condition | Action |
|---|---|
| Course not found | Show enrolled-course list; ask user to pick again |
| Course found, not completed | Inform user — certificates issued only after 100% completion. Stop. |
| Course found, completed, **no certificate issued** | Inform user — no certificate on record yet. Stop. |
| Course found, completed, certificate issued | Show current name → "Do you want to change it?" |
| `has_surname_duplication` is `true` | Highlight the duplication → ask if they want it corrected |
| User says NO | Close politely. Stop. |
| User says YES | Collect new name → confirm "Change from X to Y?" → proceed to Step 2 |

---

## Step 2 — Profile Name Update

> A single LLM tool call (`update_profile_name`) re-reads the profile first (to get the
> latest `profileDetails` object) and then sends the PATCH.

**Endpoint:** `PATCH /api/user/private/v1/update`

```bash
curl -X PATCH \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/update" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "userId": "{user_id}",
      "firstname": "Ramesh",
      "lastname": "Kumar",
      "profileDetails": {
        "personalDetails": {
          "firstname": "Ramesh",
          "surname": "Kumar"
        }
      }
    }
  }'
```

> `profileDetails` is the full object from the just-fetched profile with
> `personalDetails` overwritten with the new values before sending.

### Request Fields

| Field | Source | Notes |
|---|---|---|
| `request.userId` | Session `user_id` | Required |
| `request.firstname` | Corrected first name from user | Top-level display name |
| `request.lastname` | Corrected last name from user | Omit from payload if not changing last name |
| `request.profileDetails` | Full object from profile re-read | Avoids overwriting unrelated profile fields |
| `request.profileDetails.personalDetails.firstname` | Corrected first name | Kept in sync with top-level |
| `request.profileDetails.personalDetails.surname` | Corrected last name | Kept in sync with top-level |

### Response

| HTTP status | Chatbot action |
|---|---|
| `200` | Reply with exact re-download sentence + full 8-step certificate download guide |
| Non-200 | Inform user the update failed; ask them to try again |

---

## API Dependency Table

| Step | Endpoint | Method | Purpose | Key Fields |
|---|---|---|---|---|
| Optional | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Show course list when user cannot name a course | `courseName` |
| 1a | `/api/user/private/v1/read/{user_id}` | GET | Fetch current name on certificate; detect surname duplication | `firstName`, `lastName`, `profileDetails.personalDetails` |
| 1b | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Confirm course is completed and a certificate was issued | `completionPercentage`, `issuedCertificates` |
| 2 | `/api/user/private/v1/update` | PATCH | Apply corrected first/last name (top-level + personalDetails) | HTTP 200 = success |
