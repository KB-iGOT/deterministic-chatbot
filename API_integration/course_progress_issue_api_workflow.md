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
              ↓
         IF batch_id is NONE (not enrolled)  → STOP (inform user not enrolled)
              ↓

STEP 2 → GET   /api/content/v1/read/{course_id}
              ↓ Confirm primaryCategory (Course / Program)
              ↓
         IF Program / Curated Program        → STEP 3
         IF Course / other                    → STEP 4

STEP 3 → GET   /api/private/content/v3/hierarchy/{program_id}          [Programs only]
              ↓ Fetch child Course DO_IDs → child_course_ids
              ↓ continue ↓

STEP 4 → POST  /api/admin/content/state/read
              ↓ Cross-check enrollment status vs backend completion status
              ↓
         IF enrollment_status=1 AND admin_status=2 (mismatch) → Technical Issue path
         IF completion_pct == 100                              → Revalidation path
         ELSE                                                  → STEP 5

STEP 5 → POST  /api/course/private/v4/user/enrollment/list/{user_id}   (all statuses)
         GET   /api/content/v1/read/{course_id}
              ↓ Fetch every enrollment record (langContentStatus per course) so that a
                container course (e.g. a CAP) whose own record never carries its child
                course's completion still picks it up; then fetch leafNodes and compute
                true incomplete_ids = leafNodes − (completed IDs across ALL enrollments)
                (catches resources never opened and absent from langContentStatus)
              ↓
         IF incomplete_ids is empty          → Revalidation path
         IF API error                        → fall back to whatever incomplete_ids was
                                                already set (enrollment-based, from Step 1) ↓
         ELSE                                → STEP 6

STEP 6 → POST  /api/composite/v4/search
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
     IF no mismatch AND completion_pct==100 → Escalate internally (raise ticket)
     ELSE (no mismatch, still < 100%)       → Tell user which resources are still pending
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
| `courses[].langContentStatus` | `collected.completed_ids` | `extract_completed_ids` | Resource IDs where status `== 2`; **captured but not currently consumed** — Step 5's leaf-node diff now cross-references `all_enrollment_list_for_diff` instead |
| `courses[].langContentStatus` | `collected.incomplete_ids` | `extract_incomplete_ids` | Fallback incomplete IDs if Step 5 API fails |
| `courses[].langContentStatus` | `collected.lang_content_status` | — | Raw object passed to `compare_enrollment_vs_admin_state` in Step 4 |
| `courses[].issuedCertificates` | `collected.issued_certificates` | — | Raw certificate data |
| `courses[].issuedCertificates` | `collected.certificate_issued` | `has_issued_certificates` | `true` → stop immediately (certificate already generated) |
| `courses[].status` | `collected.enrollment_status` | `enrollment_status_to_int` | Enrollment state as integer |
| `courses[].completedOn` | `collected.completed_on_iso` | `unix_ms_to_iso` | Completion timestamp in ISO format |
| `courses[].batchId` | `collected.batch_id` | — | Required by Step 4 Admin Content State API |
| `courses[].batches` | `collected.batch_id` | `extract_batch_id` | Fallback batch ID for in-progress courses (nested in `batches[0].batchId`) |
| `courses[].primaryCategory` | `collected.primary_category` | — | `"Program"` → triggers Step 3 hierarchy call |
| `courses[].contentId` | `collected.content_do_id` | — | Leaf-level DO_ID; used in ticket descriptions (**not** in the assessment limit check — that uses `assessment_id` from Step 6, see below) |

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
| `certificate_issued == false` AND `batch_id` is unset/`"NONE"` | Inform user they are not currently enrolled. Stop. |
| `certificate_issued == false` AND `batch_id` present | Proceed to Step 2 |

> The `batch_id` check (`branch_on_enrollment`) guards against a course appearing in the picker with no batch reference — treated as "not enrolled" — before the flow calls any further course-detail APIs.

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
| `$.content.primaryCategory` | `collected.primary_category` | — | `"Program"` / `"Curated Program"` → proceed to Step 3; otherwise skip to Step 4 |

### Decision After Step 2

| Condition | Action |
|---|---|
| `primary_category in ["Program", "Curated Program"]` | Proceed to Step 3 (hierarchy fetch) |
| Any other value (e.g. `"Course"`) | Skip to Step 4 (Admin Content State) |

---

## Step 3 — Program Hierarchy Read *(Programs only)*

> Fetches the child Course DO_IDs nested under a Program. The Admin Content State API (Step 4) must be called with a Course DO_ID, not the Program DO_ID.

**Endpoint:** `GET /api/private/content/v3/hierarchy/{program_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/private/content/v3/hierarchy/do_1141986246718750721214"
```

> The `?mode=edit` query parameter was used in an earlier iteration of this integration and has since been dropped from the flow — the endpoint is called with no query string.

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

## Step 5 — Cross-Enrollment Leaf-Node Diff

> Computes the **true** set of incomplete resources. This is now a **two-call** sequence: first fetch every one of the user's enrollments (not just the selected course), then fetch the course's leaf-node list and diff it against completion recorded across *all* those enrollments — not just the selected course's own record.
>
> **Why the extra call:** a container course (e.g. a CAP — Comprehensive Assessment Program) whose own enrollment record never carries its child course's resource-level `langContentStatus` would otherwise show every leaf node as incomplete, since its own `completed_ids` is always empty even when the nested child course is 100% done. Scanning `langContentStatus` across every enrollment picks up completion recorded on the child course's own record too.

### Step 5a — Fetch All Enrollments

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": { "status": ["0", "1", "2"] }
    }
  }'
```

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.courses` | `collected.all_enrollment_list_for_diff` | — | Full enrollment list (every course/program), used as the diff source in Step 5b |

> **On API error:** falls through to Step 5b regardless (`collected.all_enrollment_list_for_diff` stays unset).

### Step 5b — Content Read (Leaf-Node Cross-Check)

**Endpoint:** `GET /api/content/v1/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/do_1141986246718750721214"
```

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.content.leafNodes` | `collected.incomplete_ids` | `diff_leaf_nodes_cross_enrollment` (ctx key: `collected.all_enrollment_list_for_diff`) | Overwrites enrollment-based `incomplete_ids` with the accurate cross-enrollment diff |

> **On API error:** the node skips directly to Step 6, using whatever `incomplete_ids` was already set (the enrollment-based value from Step 1's picker), and execution continues uninterrupted.

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

### `diff_leaf_nodes_cross_enrollment` Transform Logic

```
completed = { resource_id : status == 2, for every course in all_enrollment_list_for_diff,
              across every language in that course's langContentStatus }

incomplete_ids = leafNodes − completed
             = ["do_114_video1","do_114_video2","do_114_module1","do_114_assess1"]
               − {"do_114_video1"}   # completed somewhere across the user's enrollments
             = ["do_114_video2", "do_114_module1", "do_114_assess1"]
```

### Decision After Step 5

| Condition | Action |
|---|---|
| `incomplete_ids` empty after diff | Revalidation path (`branch_certificate_not_generated`) |
| `incomplete_ids` non-empty | Proceed to Step 6 |
| API error (Step 5b) | Skip directly to Step 6 with the pre-existing `incomplete_ids`. |

---

## Step 6 — Composite Search (Resource Metadata)

> Retrieves name, MIME type, primary category, and duration for each incomplete resource to determine the correct guidance branch.

**Endpoint:** `POST /api/composite/v4/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/composite/v4/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["do_114_video2", "do_114_module1", "do_114_assess1"],
        "status": ["Live", "Review", "Draft", "Retired"]
      },
      "isSecureSettingsDisabled": true,
      "sort_by": { "createdOn": "desc" },
      "fields": ["identifier", "name", "mimeType", "status", "duration", "primaryCategory", "maxAttempts", "maxAssessmentRetakeAttempts"],
      "facets": ["status"],
      "limit": 1000
    }
  }'
```

> Replace `identifier` array with `incomplete_ids[]` from Step 5. Request body is unchanged from the previous `/api/content/v1/search` integration except that `maxAttempts` and `maxAssessmentRetakeAttempts` were added to `fields` to support the assessment-retake check in Step 7.

### Request Filters

| Field | Value | Purpose |
|---|---|---|
| `filters.identifier` | `incomplete_ids[]` from Step 5 | Fetches metadata for only incomplete resources |
| `filters.status` | `["Live", "Review", "Draft", "Retired"]` | Includes non-Live resources; leaf nodes the user has never opened are often in Draft or Review state — omitting this causes blank resource names |
| `isSecureSettingsDisabled` | `true` | Required to retrieve content metadata |
| `fields` | See above | Restricts response payload to required fields only |
| `limit` | `1000` | Ensures all resources returned in one call |

### Response Fields Used

Before mapping the response, the node clears any stale `remaining_attempts` / `used_attempts` left over from a previous turn (`$._clear`).

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `content[*]` | `collected.all_resources_assessment` | `detect_assessment_only` | `true` if every incomplete resource has `primaryCategory == "Course Assessment"` |
| `content[*].mimeType` | `collected.has_scorm_resources` | `detect_scorm` | `true` if any resource has mimeType `application/vnd.ekstep.html-archive` |
| `content[*].name` | `collected.incomplete_resource_names` | `extract_all_names` | Bullet list of resource names shown in non-SCORM guidance message |
| `content[*].identifier` | `collected.missing_resource_ids` | `diff_missing_resource_ids` (ctx key: `collected.incomplete_ids`) | IDs from `incomplete_ids` that composite search did **not** return; triggers the Step 6a fallback |
| `content[*]` | `collected.scorm_resource_name` | `extract_scorm_resource_name` | Name of the first SCORM resource; shown in SCORM guidance message |
| `content[*]` | `collected.scorm_resource_duration_min` | `extract_scorm_duration_minutes` | `round(float(duration) / 60, 1)` — minimum time to spend on SCORM resource |
| `content[*]` | `collected.scorm_resource_names` | `extract_scorm_resource_names` | SCORM-only subset of names, shown separately in SCORM guidance message |
| `content[*]` | `collected.non_scorm_resource_names` | `extract_non_scorm_resource_names` | Non-SCORM subset of names, shown alongside SCORM guidance message |
| `content[0].identifier` | `collected.assessment_id` | — | The assessment DO_ID used as `assessmentIdentifier` in Step 7 (**not** `content_do_id` from Step 1) |
| `content[0].maxAttempts` | `collected.max_attempts` | — | Fallback attempt count if Step 7 is skipped/errors |
| `content[0].maxAssessmentRetakeAttempts` | `collected.max_retake_attempts` | — | Fallback retake-attempt count if Step 7 is skipped/errors |

> **On API error (outright HTTP failure):** the node proceeds to Step 6a below, which then falls back to fetching every ID in `incomplete_ids` individually (since `missing_resource_ids` was never populated).

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

> `/api/composite/v4/search` returns the same `result.content[*]` shape as the previous `/api/content/v1/search` endpoint (verified against a live UAT response), so no response-mapping changes were needed.

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

## Step 6a — Composite Search Name Fallback

> Composite search (Step 6) queries every `incomplete_id` in one request, but can miss some (e.g. Draft/Retired edge cases) or fail outright. For any resource whose name wasn't returned, this step fetches it individually — one call per ID, looped — and folds the name into `collected.incomplete_resource_names` (and the SCORM/non-SCORM sub-lists) so the user still sees the complete list.

**Endpoint:** `GET /api/content/v1/read/{resource_id}` — called once per missing ID via an `increment_and_branch` loop (`loop_missing_resource_names`, counter `missing_name_idx`).

> ID source: `collected.missing_resource_ids` if Step 6 succeeded but missed some IDs; otherwise (Step 6 failed outright) every ID in `collected.incomplete_ids`.

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$.content.name` | `collected.incomplete_resource_names` | `append_resource_name` | Appends the fetched name to the running list |
| `$.content` | `collected.scorm_resource_names` | `append_resource_name_if_scorm` | Appends the name only if the resource's mimeType is SCORM |
| `$.content` | `collected.non_scorm_resource_names` | `append_resource_name_if_non_scorm` | Appends the name only if the resource's mimeType is not SCORM |

> **On API error for any single ID:** skipped, and the loop still advances so remaining IDs are checked.

Once the loop completes (or if `missing_resource_ids` was empty), execution proceeds to `branch_on_resource_type` (routing decision below).

---

## Step 7 — Assessment Retake Count *(Assessment limit path only)*

> Called when the user reports "Assessment Limit Exceeded". Checks remaining attempts to determine whether a ticket needs to be raised.

**Endpoint:** `GET /api/admin/assesment/retake/count`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/admin/assesment/retake/count?assessmentIdentifier={assessment_id}&userId={user_id}&editMode=false"
```

### Query Parameters

| Parameter | Value | Purpose |
|---|---|---|
| `assessmentIdentifier` | `collected.assessment_id` | The assessment DO_ID captured in **Step 6** (`content[0].identifier` from the composite search), **not** `content_do_id` from Step 1 |
| `userId` | `ctx.user_id_hash` | Learner's user ID |
| `editMode` | `false` | Standard mode |

### Response Fields Used

> KarmayogiService unwraps the result envelope for this endpoint too, so the fields are read off the response root (`$`), not `$.result.*`.

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `$` | `collected.remaining_attempts` | `calculate_remaining_attempts` | `attemptsAllowed − attemptsMade`; `> 0` → show count, prompt retry |
| `$.attemptsAllowed` | `collected.max_attempts` | — | Total attempts permitted |
| `$.attemptsMade` | `collected.used_attempts` | — | Attempts already consumed |

> **On API error:** shows a generic "unable to process right now, try again later" message; no fallback attempt count is shown.

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

> **Ticket mechanics:** unlike the other tickets in this flow (Step 6/technical-issue, revalidation escalation, "any other error"), which go through the shared `_zoho_ticket` fragment via an LLM-drafted `transfer_llm` node, this ticket is raised by a **direct `POST /tickets` api_call node** (`assessment_limit_auto_ticket`) with a hard-coded subject/description/cf-block (`cf_category: assessment`, `cf_sub_category: other`, `cf_llm_involved: false`). No LLM drafting is involved.

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
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `langContentStatus` → `completed_ids` | Captured but no longer consumed — Step 5b's diff now uses `all_enrollment_list_for_diff` (Step 5a) instead of `completed_ids` |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `langContentStatus` → `incomplete_ids` | Fallback for Step 6 if Step 5 errors |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `langContentStatus` → `lang_content_status` | Step 4 `compare_enrollment_vs_admin_state` |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `courseId` | Step 2, 3, 4, 5 URL / body |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `batchId` / `batches` → `batch_id` | Step 4 request body |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `primaryCategory` → `primary_category` | Step 2 branch: Program vs Course |
| 1 | `POST .../enrollment/list/{user_id}` | Course/Program picker | `contentId` → `content_do_id` | Ticket description (technical-issue / certificate tickets) |

| 2 | `GET /api/content/v1/read/{course_id}` | Content type check | `$.content.primaryCategory` → `primary_category` | Branch: Program/Curated Program → Step 3, else Step 4 |
| 3 | `GET /api/private/content/v3/hierarchy/{program_id}` | Program child course IDs (no `?mode=edit` query param) | `children[*].identifier` → `child_course_ids` | Step 4 loop `courseId` field |
| 4 | `POST /api/admin/content/state/read` | Technical issue detection (loop — once per child course for Programs) | `consumptionRecords[*]` → `admin_content_states` (accumulated via `append_consumption_records`) | `compare_enrollment_vs_admin_state` |
| 5a | `POST .../enrollment/list/{user_id}` | Fetch every enrollment (status `0`/`1`/`2`) | `$.courses` → `all_enrollment_list_for_diff` | Step 5b `diff_leaf_nodes_cross_enrollment` context |
| 5b | `GET /api/content/v1/read/{course_id}` | Cross-enrollment leaf-node diff | `$.content.leafNodes` diff → `incomplete_ids` | Step 6 `filters.identifier` |
| 6 | `POST /api/composite/v4/search` | Resource metadata for guidance routing | `content[*].mimeType` → `has_scorm_resources` | Branch: SCORM vs standard guidance |
| 6 | `POST /api/composite/v4/search` | Resource metadata for guidance routing | `content[*].name` → `incomplete_resource_names` | Resource list in non-SCORM message |
| 6 | `POST /api/composite/v4/search` | Resource metadata for guidance routing | `content[*]` → `scorm_resource_name`, `scorm_resource_duration_min`, `scorm_resource_names`, `non_scorm_resource_names` | SCORM / non-SCORM guidance messages |
| 6 | `POST /api/composite/v4/search` | Resource metadata for guidance routing | `content[*]` → `all_resources_assessment` | Branch: assessment-only guidance |
| 6 | `POST /api/composite/v4/search` | Resource metadata for guidance routing | `content[0].identifier`, `content[0].maxAttempts`, `content[0].maxAssessmentRetakeAttempts` → `assessment_id`, `max_attempts`, `max_retake_attempts` | Step 7 `assessmentIdentifier`; fallback attempt counts |
| 6 | `POST /api/composite/v4/search` | Resource metadata for guidance routing | `content[*].identifier` diff → `missing_resource_ids` | Triggers Step 6a per-ID name fallback |
| 6a | `GET /api/content/v1/read/{resource_id}` (looped) | Fill in names composite search missed | `$.content.name` → `incomplete_resource_names` (+ SCORM/non-SCORM sub-lists) | Guidance messages |
| 7 | `GET /api/admin/assesment/retake/count` | Assessment attempt limit check | `attemptsAllowed`, `attemptsMade` → `remaining_attempts` (read from response root `$`, not `$.result`) | Branch: retry vs raise direct Zoho ticket |
| R1 | `POST .../enrollment/list/{user_id}` | Revalidation — refresh certificate/completion | `issuedCertificates` → `certificate_issued`; `langContentStatus` → `lang_content_status` | Branch: certificate issued or re-run Step 4 |
| R2 | `POST /api/admin/content/state/read` | Revalidation — re-check technical issue | `consumptionRecords[*]` → `admin_content_states` | `compare_enrollment_vs_admin_state`; if no mismatch and `completion_pct < 100`, tells user which resources are pending instead of escalating |
