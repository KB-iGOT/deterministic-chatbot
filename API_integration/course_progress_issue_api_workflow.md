# UC-01: Course / Program / Event Progress Issue — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1 → POST  /api/course/private/v4/user/enrollment/list/{user_id}
              ↓ User selects course; store courseId, courseName, completionPercentage,
                completed_ids (status == 2) and incomplete_ids (status != 2)
              ↓
         IF completionPercentage == 100     → STOP (course already complete)
         ELSE                               → continue ↓

STEP 2 → GET   /api/content/v1/read/{course_id}
              ↓ Fetch leafNodes; compute true incomplete_ids = leafNodes − completed_ids
                (catches resources never opened and absent from langContentStatus)
              ↓
         IF incomplete_ids is empty         → STOP (course actually complete)
         IF API error                       → fall back to enrollment-based incomplete_ids ↓
         ELSE                               → continue ↓

STEP 3 → POST  /api/content/v1/search
              ↓ Fetch name, mimeType, duration for each incomplete resource ID
              ↓ Detect: all_resources_assessment, has_scorm_resources
              ↓
         all_resources_assessment = true   → Assessment guidance
         has_scorm_resources = true        → SCORM guidance
         default                           → Standard (non-SCORM) guidance

         ✓ Diagnosis complete — no further API calls needed
```

---

## Step 1 — Enrollment List

> Renders the course picker; stores per-course completion data so the flow can branch immediately without additional calls.

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

### Request Filters

| Field | Value | Purpose |
|---|---|---|
| `retiredCoursesEnabled` | `true` | Includes archived/retired courses |
| `status` | `["In-Progress", "Completed"]` | Fetches both — a course may show Completed but still have incomplete resources |

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `courses[].courseId` | picker `id_field` | — | Course selection value; passed to Step 2 URL |
| `courses[].courseName` | `collected.course_name` | — | Display label; matched against user input |
| `courses[].completionPercentage` | `collected.completion_pct` | — | `== 100` → stop immediately |
| `courses[].langContentStatus` | `collected.completed_ids` | `extract_completed_ids` | Resource IDs where status `== 2`; used in Step 2 leaf-node diff |
| `courses[].langContentStatus` | `collected.incomplete_ids` | `extract_incomplete_ids` | Fallback incomplete IDs if Step 2 API fails |
| `courses[].status` | `collected.enrollment_status` | `enrollment_status_to_int` | Enrollment state as integer |
| `courses[].completedOn` | `collected.completed_on_iso` | `unix_ms_to_iso` | Completion timestamp in ISO format |
| `courses[].issuedCertificates` | `collected.issued_certificates` | — | Certificate info for downstream nodes |
| `courses[].batchId` | `collected.batch_id` | — | Batch reference for ticket generation |

### Sample Response (trimmed)

```json
{
  "responseCode": "OK",
  "result": {
    "courses": [
      {
        "courseId": "do_1141986246718750721214",
        "courseName": "Leadership Program",
        "completionPercentage": 75.5,
        "batchId": "0141986246730670081215",
        "langContentStatus": {
          "en": {
            "do_114_video1": 2,
            "do_114_video2": 0,
            "do_114_module1": 1
          }
        }
      }
    ]
  }
}
```

### Fields Passed to Step 2

```json
{
  "course_id":     "do_1141986246718750721214",
  "completed_ids": ["do_114_video1"],
  "incomplete_ids": ["do_114_video2", "do_114_module1"]
}
```

### Decision After Step 1

| Condition | Action |
|---|---|
| `completion_pct == 100` | Inform user course is complete. Stop. |
| `completion_pct < 100` | Proceed to Step 2 |

---

## Step 2 — Content Read (Leaf-Node Cross-Check)

> Fetches the course's full leaf-node list and computes the **true** set of incomplete resources by subtracting `completed_ids` (from Step 1). This catches resources the user has never opened that are absent from `langContentStatus`.

**Endpoint:** `GET /api/content/v1/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/do_1141986246718750721214" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.content.leafNodes` | `collected.incomplete_ids` | `diff_leaf_nodes` (ctx key: `collected.completed_ids`) | Overwrites enrollment-based `incomplete_ids` with the accurate diff |

> **On API error:** the node falls back to the `incomplete_ids` already populated in Step 1, and execution continues to Step 3 uninterrupted.

### Sample Response (trimmed)

```json
{
  "responseCode": "OK",
  "result": {
    "content": {
      "identifier": "do_1141986246718750721214",
      "name": "Leadership Program",
      "leafNodes": [
        "do_114_video1",
        "do_114_video2",
        "do_114_module1",
        "do_114_assess1"
      ]
    }
  }
}
```

### `diff_leaf_nodes` Transform Logic

```
incomplete_ids = leafNodes − completed_ids
             = ["do_114_video1","do_114_video2","do_114_module1","do_114_assess1"]
               − ["do_114_video1"]
             = ["do_114_video2", "do_114_module1", "do_114_assess1"]
```

### Decision After Step 2

| Condition | Action |
|---|---|
| `incomplete_ids` empty after diff | Course is actually complete. Show "already complete" message. Stop. |
| `incomplete_ids` non-empty | Proceed to Step 3 |
| API error | Fall back to enrollment-based `incomplete_ids`. Proceed to Step 3. |

---

## Step 3 — Content Search (Resource Metadata)

> Retrieves name, MIME type, primary category, and duration for each incomplete resource to determine the correct guidance branch.

**Endpoint:** `POST /api/content/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/content/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["do_114_video2", "do_114_module1", "do_114_assess1"]
      },
      "isSecureSettingsDisabled": true,
      "sort_by": { "createdOn": "desc" },
      "fields": ["identifier", "name", "mimeType", "status", "duration", "primaryCategory"],
      "facets": ["status"],
      "limit": 1000
    }
  }'
```

> Replace `identifier` array with `incomplete_ids[]` from Step 2.

### Request Filters

| Field | Value | Purpose |
|---|---|---|
| `filters.identifier` | `incomplete_ids[]` from Step 2 | Fetches metadata for only incomplete resources |
| `isSecureSettingsDisabled` | `true` | Required to retrieve content metadata |
| `fields` | See above | Restricts response payload to required fields only |
| `limit` | `1000` | Ensures all resources returned in one call |

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `content[*].name` | `collected.incomplete_resource_names` | `extract_all_names` | Resource names shown in non-SCORM guidance message |
| `content[*].mimeType` | `collected.has_scorm_resources` | `detect_scorm` | `true` if any resource has mimeType `application/vnd.ekstep.html-archive` |
| `content[*]` | `collected.scorm_resource_name` | `extract_scorm_resource_name` | Name of the first SCORM resource; shown in SCORM guidance message |
| `content[*]` | `collected.scorm_resource_duration_min` | `extract_scorm_duration_minutes` | `round(float(duration) / 60, 1)` — minimum time to spend on SCORM resource |
| `content[*]` | `collected.all_resources_assessment` | `detect_assessment_only` | `true` if all incomplete resources have `primaryCategory == "Course Assessment"` |

### Sample Response (trimmed)

```json
{
  "responseCode": "OK",
  "result": {
    "count": 3,
    "content": [
      {
        "identifier": "do_114_video2",
        "name": "Video: Introduction to Leadership",
        "mimeType": "application/vnd.ekstep.html-archive",
        "primaryCategory": "Learning Resource",
        "duration": "600"
      },
      {
        "identifier": "do_114_module1",
        "name": "Module 2: Communication Skills",
        "mimeType": "application/vnd.ekstep.html-archive",
        "primaryCategory": "Learning Resource",
        "duration": "900"
      },
      {
        "identifier": "do_114_assess1",
        "name": "Final Assessment",
        "mimeType": "application/vnd.sunbird.questionset",
        "primaryCategory": "Course Assessment",
        "duration": "1200"
      }
    ]
  }
}
```

### MIME Type → Guidance Branch

| `mimeType` | `has_scorm_resources` | Guidance Branch |
|---|---|---|
| `application/vnd.ekstep.html-archive` | `true` | SCORM guidance |
| `video/mp4`, `video/webm` | `false` | Standard guidance |
| `application/pdf` | `false` | Standard guidance |
| `application/vnd.sunbird.questionset` | `false` | Standard guidance |
| Any other | `false` | Standard guidance |

### `primaryCategory` → Assessment Detection

The `detect_assessment_only` transform inspects the `primaryCategory` field of every item in `content[*]` and sets `collected.all_resources_assessment = true` **only when every incomplete resource** has `primaryCategory == "Course Assessment"`.

| `primaryCategory` value | Treated as Assessment? |
|---|---|
| `"Course Assessment"` | Yes |
| `"Learning Resource"` | No |
| `"Course"` | No |
| `"Program"` | No |
| Any other value | No |

**Logic (pseudocode):**

```python
all_resources_assessment = all(
    item["primaryCategory"] == "Course Assessment"
    for item in content
)
```

> This check takes **priority 1** in the routing branch (evaluated before SCORM detection). If even one incomplete resource is not an Assessment, `all_resources_assessment` stays `false` and the MIME-type branch runs next.

---

## Final Routing Decision

| Priority | Condition | Resolution Branch |
|---|---|---|
| 1 | `all_resources_assessment == true` | Assessment guidance — prompt user to complete the pending assessment |
| 2 | `has_scorm_resources == true` | SCORM guidance — session completion, speed warning, "Next" button instruction |
| 3 | default | Standard guidance — revisit and complete pending resources |

---

## API Dependency Table

| Step | Endpoint | Purpose | Extracted Field | Passed To |
|---|---|---|---|---|
| 1 | `POST .../enrollment/list/{user_id}` | Course picker; store per-course completion data | `completionPercentage` → `completion_pct` | Branch: stop if `== 100` |
| 1 | `POST .../enrollment/list/{user_id}` | Course picker; store per-course completion data | `langContentStatus` → `completed_ids` | Step 2 `diff_leaf_nodes` context |
| 1 | `POST .../enrollment/list/{user_id}` | Course picker; store per-course completion data | `langContentStatus` → `incomplete_ids` | Fallback for Step 3 if Step 2 errors |
| 1 | `POST .../enrollment/list/{user_id}` | Course picker; store per-course completion data | `courseId` | Step 2 URL parameter |
| 2 | `GET /api/content/v1/read/{course_id}` | Leaf-node cross-check; compute true incomplete set | `leafNodes` diff → `incomplete_ids` | Step 3 `filters.identifier` |
| 3 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `mimeType` → `has_scorm_resources` | Branch: SCORM vs standard guidance |
| 3 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `name` → `incomplete_resource_names` | Resource list in non-SCORM message |
| 3 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `name`, `duration` → `scorm_resource_name`, `scorm_resource_duration_min` | SCORM guidance message |
| 3 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `primaryCategory` → `all_resources_assessment` | Branch: assessment-only guidance |
