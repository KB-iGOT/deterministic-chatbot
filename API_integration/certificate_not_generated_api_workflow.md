# UC-03: Certificate Not Generated — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.
>
> Flow file: `flows/mode_b_certificate_download.yaml` (sub-scenario **C1** — "Certificate not generated / not received", reached from `branch_on_issue` when `collected.sub_scenario == 'C1'`). The sibling sub-scenario C3 ("Incorrect name on certificate") is documented separately in `certificate_name_incorrect_api_workflow.md`.

---

## Execution Flow

```
STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id_hash}  (via c1_pick_course picker)
                ↓ User selects course → extract course_name, enrollment_status, completed_on_iso,
                  issued_certificates, provisional completed_ids/incomplete_ids (from langContentStatus)
                ↓
STEP 1.5 → GET /api/content/v1/read/{course_id}                              (c1_read_course_content)
                ↓ Fetch leafNodes + primaryCategory
                ↓ Recompute incomplete_ids = leafNodes not in completed_ids (diff_leaf_nodes)
                ↓ course_name missing → no enrollment found, re-prompt for course
                ↓
           enrollment_status = 0 (not started) → guide user to start the course, stop
                                                  (wording branches on primaryCategory: course vs program)
           enrollment_status = 2 (completed)   → check issued_certificates, then hours_since(completed_on_iso)
                ↓
              issued_certificates non-empty        → certificate available, stop
              completed_on_iso is None OR > 24h old → certificate available (wait guard), stop
              completed_on_iso <= 24h old           → not yet generated → offer to raise a ticket
           
           enrollment_status = 1 (in-progress) → if incomplete_ids is empty, treat as "wait" case (stop);
                                                  otherwise proceed to STEP 2
                ↓
STEP 2   → POST /api/composite/v4/search           (only when course is in-progress with incomplete_ids)
                ↓ Fetch name, mimeType for each incomplete resource ID
                ↓ Determine: has_scorm = any(mimeType == "application/vnd.ekstep.html-archive")
                ↓ (fallback) any incomplete ID missing from the composite result is looked up
                  individually via GET /api/content/v1/read/{resource_id}
                ↓ Provide SCORM or Standard progress guidance (wording branches on primaryCategory)
```

---

## Step 1 — Course Selection and Status Check

> Executed via the shared `_enrollment_picker` fragment (`flows/_shared/_enrollment_picker.yaml`, imported with `node_id: c1_pick_course`). Renders a searchable course picker and stores per-item fields into `ctx.collected` (prefixed `c1_`).

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id_hash}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id_hash}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true,
      "status": ["In-Progress", "Completed"]
    }
  }'
```

### Response Fields Used

The response returns the enrollment list at the top level (`$.courses[]`), **not** nested under `result` — the picker's `list_path` is `$.courses`.

| Field | Extracted As | Purpose |
|---|---|---|
| `courses[].courseId` | picker id field | Selected course identifier |
| `courses[].courseName` | `c1_course_name` | Display name |
| `courses[].completionPercentage` | `c1_completion_pct` | Stored, but **not read** by any branch in this flow (no decision uses it) |
| `courses[].status` | `c1_enrollment_status` | Via `enrollment_status_to_int`: `0`=not started, `1`=in progress, `2`=completed |
| `courses[].completedOn` | `c1_completed_on_iso` | Via `unix_ms_to_iso`; used to compute `hours_since(c1_completed_on_iso)` |
| `courses[].issuedCertificates` | `c1_issued_certificates` | Stored as the raw list; only its presence/length is checked (`len(...) > 0`) — no sub-fields such as `identifier`/`lastIssuedOn`/`token` are extracted or used |
| `courses[].batchId` | `c1_batch_id` | Stored, but **not read** by any branch in this flow |
| `courses[].langContentStatus` | `c1_completed_ids` / `c1_incomplete_ids` | Via `extract_completed_ids` / `extract_incomplete_ids`; these are provisional and get overwritten in Step 1.5 below |
| `courses[].contentStatus` | `c1_completed_ids` / `c1_incomplete_ids` (fallback) | Only overwrites when `langContentStatus` was empty (Program/CAP enrollments) |

### `hours_since` Computation

`hours_since(ctx.collected.c1_completed_on_iso)` is a built-in expression function. If `c1_completed_on_iso` is `None`, the branch condition treats it the same as `> 24` (same UX either way — certificate should be available).

---

## Step 1.5 — Course Content Read (recompute incomplete resources)

> Not part of the picker; a dedicated `api_call` node (`c1_read_course_content`) that always runs right after course selection, regardless of enrollment status.

**Endpoint:** `GET /api/content/v1/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/{course_id}"
```

### Response Fields Used

| Field | Extracted As | Purpose |
|---|---|---|
| `content.leafNodes` | `c1_incomplete_ids` | Via `diff_leaf_nodes(leafNodes, c1_completed_ids)` — any leaf node ID absent from `c1_completed_ids` is considered incomplete. This catches resources the learner never opened (and so never appeared in `langContentStatus`), overwriting the provisional Step 1 value |
| `content.primaryCategory` | `c1_primary_category` | Used throughout to pick "course" vs "program" wording (`'program' in lower(str(c1_primary_category))`) |

On error, the flow falls through to the same branch node (`c1_branch_enrollment`) as on success — i.e. it degrades gracefully using whatever Step 1 already populated.

### Decision After Step 1 / 1.5

| Condition | Outcome |
|---|---|
| `c1_course_name` empty/None | No enrollment found for the selection — re-prompt to pick a course |
| `enrollment_status = 2` AND `issued_certificates` non-empty | Certificate available — provide download steps |
| `enrollment_status = 2` AND `issued_certificates` empty AND (`completed_on_iso` is None OR `hours_since(...) > 24`) | Certificate available (24h window guard) — provide download steps |
| `enrollment_status = 2` AND `issued_certificates` empty AND `hours_since(...) <= 24` | Certificate not yet generated — offer to raise a support ticket (collects/confirms email + mobile, then auto-raises via `transfer_llm`) |
| `enrollment_status = 1` (in progress) AND `incomplete_ids` empty | Treated the same as the "wait" branch above (sync/cache edge case) |
| `enrollment_status = 1` (in progress) AND `incomplete_ids` non-empty | Proceed to Step 2 to identify incomplete resources |
| `enrollment_status = 0` (not started), or no other rule matched | Guide user to start the course/program |

---

## Step 2 — Incomplete Resource Lookup (only when course is in-progress with incomplete resources)

> Only reached when `enrollment_status = 1` and `c1_incomplete_ids` is non-empty. Identifies which resources are pending and determines whether they are SCORM-based.

**Endpoint:** `POST /api/composite/v4/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/composite/v4/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["<incomplete_resource_id_1>", "<incomplete_resource_id_2>"],
        "status": ["Live", "Review", "Draft", "Retired"]
      },
      "isSecureSettingsDisabled": true,
      "sort_by": { "createdOn": "desc" },
      "fields": ["identifier", "name", "mimeType", "status", "duration"],
      "facets": ["status"],
      "limit": 1000
    }
  }'
```

> `filters.identifier` is populated from `c1_incomplete_ids`, which by this point comes from the Step 1.5 `diff_leaf_nodes` computation (course leaf nodes minus completed IDs), not directly from `langContentStatus`.

### Response Fields Used

| Field | Used For |
|---|---|
| `content[0].name` | First incomplete resource's name → `c1_incomplete_resource_name` (used in guidance text) |
| `content[*].name` | All incomplete resource names → `c1_incomplete_resource_names` |
| `content[*].mimeType` | SCORM detection via `detect_scorm`: `"application/vnd.ekstep.html-archive"` = SCORM → `c1_has_scorm` |
| `content[*].identifier` | Diffed against `c1_incomplete_ids` (`diff_missing_resource_ids`) to find IDs the composite search didn't return → `c1_missing_resource_ids` |

`fields` also requests `status` and `duration`, but neither is mapped into `ctx.collected` or referenced by any message in this flow — `duration` is not converted to minutes anywhere in `mode_b_certificate_download.yaml`.

### Missing-Resource Name Fallback

If `c1_missing_resource_ids` is non-empty (composite search returned but missed some IDs), **or** the composite search call fails outright (in which case the fallback covers the *entire* `c1_incomplete_ids` list), the flow loops over the missing IDs one at a time via:

**Endpoint:** `GET /api/content/v1/read/{resource_id}` (looped, one call per missing ID, via `increment_and_branch` counter `c1_missing_name_idx`)

| Field | Used For |
|---|---|
| `content.name` | Appended to `c1_incomplete_resource_names` (`append_resource_name_to_list`); also fills `c1_incomplete_resource_name` if not already set (`set_first_resource_name`) |

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `has_scorm = true` | At least one incomplete resource is a SCORM HTML archive — SCORM-specific completion guidance (wording branches on course vs program) |
| `has_scorm = false` | All incomplete resources are non-SCORM — standard progress guidance (wording branches on course vs program) |

---

## API Dependency Table

| Step | Endpoint | Method | Purpose | Key Fields |
|---|---|---|---|---|
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id_hash}` | POST | Course selection; status, completedOn, issuedCertificates via picker | `status`, `completedOn`, `issuedCertificates`, `langContentStatus` |
| 1.5 | `/api/content/v1/read/{course_id}` | GET | Recompute incomplete resource IDs against full course structure; get primaryCategory | `leafNodes`, `primaryCategory` |
| 2 (in-progress only) | `/api/composite/v4/search` | POST | Fetch incomplete resource details; detect SCORM vs non-SCORM | `mimeType`, `name`, `identifier` |
| 2 (fallback) | `/api/content/v1/read/{resource_id}` | GET | Per-resource name lookup for IDs the composite search missed | `name` |
