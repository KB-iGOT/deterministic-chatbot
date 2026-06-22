# UC-03: Certificate Not Generated — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id} (via picker)
                ↓ User selects course → extract status, completedOn, langContentStatus
                ↓
           enrollment_status = 0 (not started) → inform user, stop
           enrollment_status = 2 (completed)   → check hours_since_completion
                ↓
              hours_since_completion > 24  → certificate should be available via UI, stop
              hours_since_completion ≤ 24  → certificate not yet generated (< 24 h), stop
           
           enrollment_status = 1 (in-progress) → proceed to STEP 2
                ↓
STEP 2   → POST /api/content/v1/search           (only when course is in-progress)
                ↓ Fetch name, mimeType for each incomplete resource ID
                ↓ Determine: has_scorm_resources = any(mimeType == "application/vnd.ekstep.html-archive")
                ↓ Provide SCORM or Standard progress guidance
```

---

## Step 1 — Course Selection and Status Check

> Executed via an enrollment picker fragment. Allows the user to select the course and extracts its enrollment status and completion details.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

Same request payload as the Optional Step above.

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id}" \
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

### `hours_since_completion` Computation

If `completedOn` is present, it is checked to see if 24 hours have elapsed. If `completedOn` is null, it is treated as `> 24` hours for UX purposes.

### Decision After Step 1

| Condition | Outcome |
|---|---|
| `enrollment_status = 2` AND `hours_since_completion > 24` | Inform user certificate is available, provide steps to download. |
| `enrollment_status = 2` AND `hours_since_completion <= 24` | Inform user to wait 24 hours for generation. |
| `enrollment_status = 1` (in progress) | Proceed to Step 2 to identify incomplete resources. |
| `enrollment_status = 0` (not started) | Guide user to start the course. |

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
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Course status, completedOn, hours_since_completion via picker | `status`, `completedOn`, `langContentStatus` |
| 2 (in-progress only) | `/api/content/v1/search` | POST | Fetch incomplete resource details; detect SCORM vs non-SCORM | `mimeType`, `name` |
