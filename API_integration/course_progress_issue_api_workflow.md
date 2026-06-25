# UC-01: Course / Program Progress Issue — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

### Course / Program Path

```
STEP 1 → POST  /api/course/private/v4/user/enrollment/list/{user_id}
              ↓ User selects course/program; store courseId, courseName, completionPercentage,
                completed_ids, incomplete_ids, lang_content_status, certificate_issued,
                batch_id, primary_category, content_do_id
              ↓
         IF certificate_issued == true       → STOP (certificate already generated)
         ELSE                                → continue ↓

STEP 2 → GET   /api/content/v1/read/{course_id}
              ↓ Confirm primaryCategory (Course / Program)
              ↓
         IF Program                          → STEP 3
         IF Course                          → STEP 4

STEP 3 → GET   /api/private/content/v3/hierarchy/{program_id}?mode=edit          [Programs only]
              ↓ Fetch child Course DO_IDs → child_course_ids
              ↓ continue ↓

STEP 4 → POST  /api/admin/content/state/read
              ↓ Cross-check enrollment status vs backend completion status
              ↓
         IF enrollment_status=1 AND admin_status=2 (mismatch) → Technical Issue path
         IF completion_pct == 100                              → Revalidation path
         ELSE                                                  → STEP 5

STEP 5 → GET   /api/content/v1/read/{course_id}
              ↓ Fetch leafNodes; compute true incomplete_ids = leafNodes − completed_ids
                (catches resources never opened and absent from langContentStatus)
              ↓
         IF incomplete_ids is empty          → Revalidation path
         IF API error                        → fall back to enrollment-based incomplete_ids ↓
         ELSE                                → STEP 6

STEP 6 → POST  /api/content/v1/search
              ↓ Fetch name, mimeType, primaryCategory, duration for each incomplete resource
              ↓ Detect: all_resources_assessment, has_scorm_resources
              ↓
         all_resources_assessment = true    → Assessment guidance
         has_scorm_resources = true         → SCORM guidance
         default                            → Standard (non-SCORM) guidance

STEP 7 → GET   /api/admin/assesment/retake/count               [Assessment limit path only]
              ↓ Verify remaining attempt count
              ↓
         remaining_attempts > 0             → Show remaining count, prompt retry
         remaining_attempts == 0            → Raise ticket

         ✓ Diagnosis complete
```



### Revalidation Path (completion = 100 but no certificate)

```
R1 → POST  /api/course/private/v4/user/enrollment/list/{user_id}
          ↓ Re-fetch issuedCertificates, completionPercentage, langContentStatus
          ↓
     IF certificate_issued == true          → STOP (certificate now generated)
     ELSE                                   → R2

R2 → POST  /api/admin/content/state/read
          ↓ Re-check enrollment vs admin status
          ↓
     IF mismatch found                      → Technical Issue path
     ELSE                                   → Escalate internally
```

---

## Step 1 — Course / Program Enrollment List

> Renders the course/program picker; captures all required fields for downstream steps in a single call.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

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

### Request Filters

| Field | Value | Purpose |
|---|---|---|
| `retiredCoursesEnabled` | `true` | Includes archived/retired courses |
| `status` | `["In-Progress", "Completed"]` | Fetches both — a course may show Completed but still have incomplete resources |

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `courses[].courseId` | picker `id_field` | — | Course selection value; passed to Step 2 and Step 4 URLs |
| `courses[].courseName` | `collected.course_name` | — | Display label in resolution messages |
| `courses[].completionPercentage` | `collected.completion_pct` | — | `== 100` → trigger revalidation path |
| `courses[].langContentStatus` | `collected.completed_ids` | `extract_completed_ids` | Resource IDs where status `== 2`; used in Step 5 leaf-node diff |
| `courses[].langContentStatus` | `collected.incomplete_ids` | `extract_incomplete_ids` | Fallback incomplete IDs if Step 5 API fails |
| `courses[].langContentStatus` | `collected.lang_content_status` | — | Raw object passed to `compare_enrollment_vs_admin_state` in Step 4 |
| `courses[].issuedCertificates` | `collected.issued_certificates` | — | Raw certificate data |
| `courses[].issuedCertificates` | `collected.certificate_issued` | `has_issued_certificates` | `true` → stop immediately (certificate already generated) |
| `courses[].status` | `collected.enrollment_status` | `enrollment_status_to_int` | Enrollment state as integer |
| `courses[].completedOn` | `collected.completed_on_iso` | `unix_ms_to_iso` | Completion timestamp in ISO format |
| `courses[].batchId` | `collected.batch_id` | — | Required by Step 4 Admin Content State API |
| `courses[].batches` | `collected.batch_id` | `extract_batch_id` | Fallback batch ID for in-progress courses (nested in `batches[0].batchId`) |
| `courses[].primaryCategory` | `collected.primary_category` | — | `"Program"` → triggers Step 3 hierarchy call |
| `courses[].contentId` | `collected.content_do_id` | — | Leaf-level DO_ID; used in ticket descriptions and assessment limit check |

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
        "primaryCategory": "Course",
        "contentId": "do_114_content1",
        "issuedCertificates": [],
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

### Decision After Step 1

| Condition | Action |
|---|---|
| `certificate_issued == true` | Inform user certificate is already generated. Stop. |
| `certificate_issued == false` | Proceed to Step 2 |

---



## Step 2 — Content Type Check

> Confirms the `primaryCategory` of the selected DO_ID to determine whether to call the hierarchy API (Programs only).

**Endpoint:** `GET /api/content/v1/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/do_1141986246718750721214"
```

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.content.primaryCategory` | `collected.primary_category` | — | `"Program"` → proceed to Step 3; otherwise skip to Step 4 |

### Decision After Step 2

| Condition | Action |
|---|---|
| `primary_category == "Program"` | Proceed to Step 3 (hierarchy fetch) |
| Any other value (`"Course"`) | Skip to Step 4 (Admin Content State) |

---

## Step 3 — Program Hierarchy Read *(Programs only)*

> Fetches the child Course DO_IDs nested under a Program. The Admin Content State API (Step 4) must be called with a Course DO_ID, not the Program DO_ID.

**Endpoint:** `GET /api/private/content/v3/hierarchy/{program_id}?mode=edit`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/private/content/v3/hierarchy/do_1141986246718750721214?mode=edit"
```

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.content.children[*].identifier` | `collected.child_course_ids` | `extract_all_identifiers` | First element passed as `courseId` to Step 4 Admin Content State API |

### Sample Response (trimmed)

```json
{
  "responseCode": "OK",
  "result": {
    "content": {
      "identifier": "do_1141986246718750721214",
      "name": "Leadership Program",
      "children": [
        { "identifier": "do_114_child_course1" },
        { "identifier": "do_114_child_course2" }
      ]
    }
  }
}
```

---

## Step 4 — Admin Content State API

> Cross-references the learner's enrollment-side resource statuses against the backend server-side statuses to detect technical issues (backend recorded completion but portal not updated).

**Endpoint:** `POST /api/admin/content/state/read`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/admin/content/state/read" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "userId": "{user_id}",
      "courseId": "{course_do_id}",
      "batchId": "{batch_id}"
    }
  }'
```

> **Programs:** called in a loop once for **each** `child_course_ids[i]`. Consumption records are accumulated across all iterations before the technical-issue comparison runs.
> **Courses:** called once using `course_id` directly.

### Request Fields

| Field | Value | Purpose |
|---|---|---|
| `userId` | `ctx.user_id_hash` | Learner's user ID |
| `courseId` | `child_course_ids[loop_index]` for Programs; `course_id` for Courses | Must be a Course DO_ID (not Program DO_ID) |
| `batchId` | `collected.batch_id` | Batch reference from enrollment; may be null for in-progress courses |

### Response Fields Used

| Path | Stored As | Transform | Used For |
|---|---|---|---|
| `$.consumptionRecords[*]` | `collected.admin_content_states` | `append_consumption_records` (Programs loop) / `extract_consumption_records` (Course) | Accumulated list of `{contentid, language, status}` records passed to `compare_enrollment_vs_admin_state` |

> Each record: `contentid` = leaf resource DO_ID, `language` = content language (e.g. `"english"`), `status` = `0` (not started) / `1` (in-progress) / `2` (completed).

### Program Loop Mechanics

The flow uses an `increment_and_branch` node (`loop_child_course`) to iterate through all child courses:

```
counter: child_loop_idx  (starts at 0)

Iteration 1: call Admin Content State with child_course_ids[0] → append records
             increment counter to 1
             1 < len(child_course_ids)? YES → loop back

Iteration 2: call Admin Content State with child_course_ids[1] → append records
             increment counter to 2
             2 < len(child_course_ids)? NO → proceed to branch_on_technical_issue
```

`append_consumption_records` (with `transform_ctx_key: collected.admin_content_states`) merges each response into the accumulating list. On API error for any single child course, the loop advances to the next course without stopping.

> **On API error (Course path — missing batchId):** falls through directly to Step 5 without technical issue detection.

### Technical Issue Detection Logic

```
compare_enrollment_vs_admin_state(lang_content_status, admin_content_states)

→ Returns True if any resource has:
     enrollment langContentStatus.status == 1   (In-Progress for learner)
  AND admin consumptionRecords.status     == 2   (Completed on server)

This means the backend recorded completion but the portal has not reflected it.
```

### Decision After Step 4

| Condition | Action |
|---|---|
| Technical issue detected (status mismatch) | Confirm with user → raise Zoho ticket |
| `completion_pct == 100` and no mismatch | Revalidation path |
| No issue, `completion_pct < 100` | Proceed to Step 5 |
| API error | Skip to Step 5 |

---

## Step 5 — Content Read (Leaf-Node Cross-Check)

> Fetches the course's full leaf-node list and computes the **true** set of incomplete resources by subtracting `completed_ids` (from Step 1). This catches resources the user has never opened that are absent from `langContentStatus`.

**Endpoint:** `GET /api/content/v1/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/do_1141986246718750721214"
```

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.content.leafNodes` | `collected.incomplete_ids` | `diff_leaf_nodes` (ctx key: `collected.completed_ids`) | Overwrites enrollment-based `incomplete_ids` with the accurate diff |

> **On API error:** the node falls back to the `incomplete_ids` already populated in Step 1, and execution continues to Step 6 uninterrupted.

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

### Decision After Step 5

| Condition | Action |
|---|---|
| `incomplete_ids` empty after diff | Revalidation path |
| `incomplete_ids` non-empty | Proceed to Step 6 |
| API error | Fall back to enrollment-based `incomplete_ids`. Proceed to Step 6. |

---

## Step 6 — Content Search (Resource Metadata)

> Retrieves name, MIME type, primary category, and duration for each incomplete resource to determine the correct guidance branch.

**Endpoint:** `POST /api/content/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/content/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["do_114_video2", "do_114_module1", "do_114_assess1"],
        "status": ["Live", "Review", "Draft", "Retired"]
      },
      "isSecureSettingsDisabled": true,
      "sort_by": { "createdOn": "desc" },
      "fields": ["identifier", "name", "mimeType", "status", "duration", "primaryCategory"],
      "facets": ["status"],
      "limit": 1000
    }
  }'
```

> Replace `identifier` array with `incomplete_ids[]` from Step 5.

### Request Filters

| Field | Value | Purpose |
|---|---|---|
| `filters.identifier` | `incomplete_ids[]` from Step 5 | Fetches metadata for only incomplete resources |
| `filters.status` | `["Live", "Review", "Draft", "Retired"]` | Includes non-Live resources; leaf nodes the user has never opened are often in Draft or Review state — omitting this causes blank resource names |
| `isSecureSettingsDisabled` | `true` | Required to retrieve content metadata |
| `fields` | See above | Restricts response payload to required fields only |
| `limit` | `1000` | Ensures all resources returned in one call |

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `content[*]` | `collected.all_resources_assessment` | `detect_assessment_only` | `true` if every incomplete resource has `primaryCategory == "Course Assessment"` |
| `content[*].mimeType` | `collected.has_scorm_resources` | `detect_scorm` | `true` if any resource has mimeType `application/vnd.ekstep.html-archive` |
| `content[*].name` | `collected.incomplete_resource_names` | `extract_all_names` | Bullet list of resource names shown in non-SCORM guidance message |
| `content[*]` | `collected.scorm_resource_name` | `extract_scorm_resource_name` | Name of the first SCORM resource; shown in SCORM guidance message |
| `content[*]` | `collected.scorm_resource_duration_min` | `extract_scorm_duration_minutes` | `round(float(duration) / 60, 1)` — minimum time to spend on SCORM resource |

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

## Step 7 — Assessment Retake Count *(Assessment limit path only)*

> Called when the user reports "Assessment Limit Exceeded". Checks remaining attempts to determine whether a ticket needs to be raised.

**Endpoint:** `GET /api/admin/assesment/retake/count`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/admin/assesment/retake/count?assessmentIdentifier={content_do_id}&userId={user_id}&editMode=false"
```

### Query Parameters

| Parameter | Value | Purpose |
|---|---|---|
| `assessmentIdentifier` | `collected.content_do_id` | The assessment leaf resource DO_ID captured in Step 1 |
| `userId` | `ctx.user_id_hash` | Learner's user ID |
| `editMode` | `false` | Standard mode |

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.result` | `collected.remaining_attempts` | `calculate_remaining_attempts` | `attemptsAllowed − attemptsMade`; `> 0` → show count, prompt retry |
| `$.result.attemptsAllowed` | `collected.max_attempts` | — | Total attempts permitted |
| `$.result.attemptsMade` | `collected.used_attempts` | — | Attempts already consumed |

### `calculate_remaining_attempts` Logic

```python
remaining = int(attemptsAllowed) - int(attemptsMade)
return remaining if remaining > 0 else 0
```

### Decision After Step 7

| Condition | Action |
|---|---|
| `remaining_attempts > 0` | Inform user of remaining count; prompt retry |
| `remaining_attempts == 0` | Raise Zoho support ticket |

---

## Final Routing Decision (Step 6 output)

| Priority | Condition | Resolution Branch |
|---|---|---|
| 1 | `all_resources_assessment == true` | Assessment guidance — prompt user to complete the pending assessment |
| 2 | `has_scorm_resources == true` | SCORM guidance — session completion, speed warning, "Next" button instruction |
| 3 | default | Standard guidance — revisit and complete pending resources |

---

## API Dependency Table

| Step | Endpoint | Purpose | Extracted Field | Passed To |
|---|---|---|---|---|
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `issuedCertificates` → `certificate_issued` | Branch: stop if `== true` |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `completionPercentage` → `completion_pct` | Step 4 branch: revalidation if `== 100` |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `langContentStatus` → `completed_ids` | Step 5 `diff_leaf_nodes` context |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `langContentStatus` → `incomplete_ids` | Fallback for Step 6 if Step 5 errors |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `langContentStatus` → `lang_content_status` | Step 4 `compare_enrollment_vs_admin_state` |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `courseId` | Step 2, 3, 4, 5 URL / body |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `batchId` / `batches` → `batch_id` | Step 4 request body |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `primaryCategory` → `primary_category` | Step 2 branch: Program vs Course |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `contentId` → `content_do_id` | Step 7 `assessmentIdentifier`; ticket description |

| 2 | `GET /api/content/v1/read/{course_id}` | Content type check | `$.content.primaryCategory` → `primary_category` | Branch: Program → Step 3, else Step 4 |
| 3 | `GET /api/private/content/v3/hierarchy/{program_id}?mode=edit` | Program child course IDs | `children[*].identifier` → `child_course_ids` | Step 4 loop `courseId` field |
| 4 | `POST /api/admin/content/state/read` | Technical issue detection (loop — once per child course for Programs) | `consumptionRecords[*]` → `admin_content_states` (accumulated via `append_consumption_records`) | `compare_enrollment_vs_admin_state` |
| 5 | `GET /api/content/v1/read/{course_id}` | Leaf-node cross-check | `$.content.leafNodes` diff → `incomplete_ids` | Step 6 `filters.identifier` |
| 6 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `content[*].mimeType` → `has_scorm_resources` | Branch: SCORM vs standard guidance |
| 6 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `content[*].name` → `incomplete_resource_names` | Resource list in non-SCORM message |
| 6 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `content[*]` → `scorm_resource_name`, `scorm_resource_duration_min` | SCORM guidance message |
| 6 | `POST /api/content/v1/search` | Resource metadata for guidance routing | `content[*]` → `all_resources_assessment` | Branch: assessment-only guidance |
| 7 | `GET /api/admin/assesment/retake/count` | Assessment attempt limit check | `attemptsAllowed`, `attemptsMade` → `remaining_attempts` | Branch: retry vs raise ticket |
| R1 | `POST .../enrollment/list/{user_id}` | Revalidation — refresh certificate/completion | `issuedCertificates` → `certificate_issued`; `langContentStatus` → `lang_content_status` | Branch: certificate issued or re-run Step 4 |
| R2 | `POST /api/admin/content/state/read` | Revalidation — re-check technical issue | `consumptionRecords[*]` → `admin_content_states` | `compare_enrollment_vs_admin_state` |
