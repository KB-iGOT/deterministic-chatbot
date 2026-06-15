# UC-03: Certificate Not Generated — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
[optional] POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Only if user cannot supply the course name — show numbered list

STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Match course by name → extract status, completedOn, issuedCertificates
                ↓
           course NOT matched              → show enrolled-course list; ask user to pick again
           enrollment_status_raw = 1       → course in-progress; show incomplete resources (→ STEP 2)
           enrollment_status_raw = 2       → course completed → check hours_since_completion
                ↓
           certificate_issued = true               → certificate available
           certificate_issued = false
             AND hours_since_completion > 24  → certificate should be available via UI
             AND hours_since_completion ≤ 24  → certificate not yet generated (< 24 h)

STEP 2   → POST /api/content/v1/search           (only when course is in-progress)
                ↓ Fetch name, mimeType, duration for each incomplete resource
                ↓ Determine: has_scorm_resources = any(mimeType == "application/vnd.ekstep.html-archive")
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
| `result.courses[].courseName` | Course name — used to build a numbered list for course selection |

---

## Step 1 — Certificate Status Check

> Called via the `check_certificate_status` tool. Determines whether the course is completed and whether a certificate has been issued.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

Same request payload as the Optional Step above.

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

| Field | Extracted As | Purpose |
|---|---|---|
| `result.courses[].courseName` | `course_name` | Matched against user-supplied name (exact then partial) |
| `result.courses[].status` | `enrollment_status_raw` | `0`=not started, `1`=in progress, `2`=completed |
| `result.courses[].completionPercentage` | `completion_percentage` | Secondary check alongside raw status |
| `result.courses[].completedOn` | `completed_on` (ISO UTC) | Unix ms timestamp → ISO; used to compute `hours_since_completion` |
| `result.courses[].enrolledDate` | `enrolled_date` (ISO UTC) | Unix ms timestamp → ISO |
| `result.courses[].issuedCertificates` | `issued_certificates` | Non-empty list → `certificate_issued = true` |
| `result.courses[].issuedCertificates[].identifier` | — | Certificate identifier |
| `result.courses[].issuedCertificates[].lastIssuedOn` | — | When the certificate was last issued |
| `result.courses[].issuedCertificates[].name` | — | Certificate template name |
| `result.courses[].issuedCertificates[].token` | — | Certificate token / download reference |
| `result.courses[].progress` | `progress` | Leaf nodes completed |
| `result.courses[].leafNodesCount` | `leaf_nodes_count` | Total leaf nodes in course |
| `result.courses[].batchId` | `batch_id` | Batch reference |
| `result.courses[].langContentStatus` | `lang_content_status` | Per-resource completion map (used by Step 2) |

### `hours_since_completion` Computation (inside tool, no extra API call)

```
completed_dt = datetime.fromisoformat(completedOn)   # Unix ms → ISO → datetime
hours_since_completion = (now_utc - completed_dt).total_seconds() / 3600
```

### Decision After Step 1

| Condition | Outcome |
|---|---|
| Course not found (`matched = false`) | Return `partial_matches` + `all_enrolled_courses` for re-selection |
| `enrollment_status_raw = 1` (in progress) | `certificate_issued = false`; proceed to Step 2 to identify incomplete resources |
| `enrollment_status_raw = 2` AND `completed_on` is null | `hours_since_completion = null`; treat as within window |
| `certificate_issued = true` | Certificate present in `issued_certificates[]` |
| `certificate_issued = false` AND `hours_since_completion > 24` | Course completed > 24 h ago but no certificate record |
| `certificate_issued = false` AND `hours_since_completion ≤ 24` | Course completed < 24 h ago; certificate not yet generated |

---

## Step 2 — Incomplete Resource Lookup (only when course is in-progress)

> Only reached when `enrollment_status_raw = 1`. Identifies which resources are pending and determines whether they are SCORM-based.

**Endpoint:** `POST /api/content/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/content/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["<incomplete_resource_id_1>", "<incomplete_resource_id_2>"]
      },
      "isSecureSettingsDisabled": true,
      "sort_by": { "createdOn": "desc" },
      "fields": ["identifier", "name", "mimeType", "endDate", "startDate", "status", "versionKey", "createdOn", "duration"],
      "facets": ["status"],
      "limit": 1000
    }
  }'
```

> `filters.identifier` is populated from `langContentStatus` — collect all resource IDs where the status value `!= 2` (not completed).

### How Incomplete IDs Are Derived from Step 1

```
for lang_key, status_map in langContentStatus.items():
    for resource_id, status in status_map.items():
        if status != 2:
            incomplete_ids.append(resource_id)   # 0=not_started, 1=in_progress
```

### Response Fields Used

| Field | Used For |
|---|---|
| `result.content[].identifier` | Match back to `langContentStatus` resource ID |
| `result.content[].name` | Human-readable resource name |
| `result.content[].mimeType` | SCORM detection: `"application/vnd.ekstep.html-archive"` = SCORM |
| `result.content[].duration` | Raw seconds → `round(float(duration) / 60, 1)` minutes |

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `has_scorm_resources = true` | At least one incomplete resource is a SCORM HTML archive |
| `has_scorm_resources = false` | All incomplete resources are non-SCORM (PDF, MP4, etc.) |
| `incomplete_ids` empty AND `completion_percentage < 100` | No incomplete IDs in `langContentStatus` despite sub-100% progress (sync/cache issue) |

---

## API Dependency Table

| Step | Endpoint | Method | Purpose | Key Fields |
|---|---|---|---|---|
| Optional | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Show course list when user cannot name a course | `courseName` |
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Course status, completedOn, issuedCertificates, hours_since_completion | `status`, `completedOn`, `issuedCertificates`, `langContentStatus` |
| 2 (in-progress only) | `/api/content/v1/search` | POST | Fetch incomplete resource details; detect SCORM vs non-SCORM | `mimeType`, `name`, `duration` |
