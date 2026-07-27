# UC-04: Resource / Content Not Opening — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Populate a searchable, paginated course picker
                ↓ User selects the course directly (no free-text matching)
                ↓
           no enrolled courses returned → show empty-state message; end

STEP 2   → GET  /api/content/v2/read/{course_id}
                ↓ Fetch course hierarchy — leafNodes[] (primary) and children[] (fallback)
                ↓ Collect all resource identifiers from hierarchy

STEP 3   → POST /api/composite/v4/search
                ↓ Fetch name, mimeType for all resource identifiers
                ↓ Populate a searchable, paginated resource picker; user selects directly
STEP 3b  → GET  /api/content/v1/read/{id}     (per missing ID, added 2026-07-23)
                ↓ Fills in any resource ID that composite search silently dropped
                ↓ (e.g. Draft/Retired status content)

STEP 4   → GET  /api/content/v1/read/{resource_id}     (YouTube resources only)
                ↓ mimeType == "text/x-url"
                ↓ Fetch streamingUrl, artifactUrl, previewUrl
```

> **Note:** Steps 1–3 always run for web-browser issues (mobile-app users are routed to
> device guidance before reaching the course/resource pickers, then straight to a ticket
> after resource selection — they never hit Step 4). Step 4 runs only when the selected
> resource's `mimeType` is `text/x-url` (YouTube), and only for web-browser users.
> Course/resource selection is UI-driven (dropdown pickers populated by the API), not a
> free-text name-matching algorithm.

---

## Step 1 — Course Picker (Enrollment List)

> Populates a searchable, paginated dropdown of the user's enrolled courses. The user
> picks the course directly from this list — there is no server-side name matching.

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

### Response Fields Used

| Field | Used For |
|---|---|
| `result.courses[].courseId` | Picker option ID; becomes `{course_id}` for Step 2 once selected |
| `result.courses[].courseName` | Picker option label |
| `result.courses[].completionPercentage` | Picker option sub-label |
| `result.courses[].langContentStatus` | Extracted (`extract_completed_ids`) into `collected.resource_completed_ids` — carried as context, not branched on in this flow |
| `result.courses[].batchId` / `result.courses[].batches[]` | Extracted (`extract_batch_id` as fallback) into `collected.batch_id` — carried as context, not branched on in this flow |

Courses of `content.courseCategory == "Learning Pathway"` are filtered out of the picker.
The list is client-side searchable, paginated (10 per page), and cached for 300s.

### Decision After Step 1

| Condition | Outcome |
|---|---|
| One or more courses returned | Show the picker; user selects a course, its `courseId` is stored and Step 2 runs |
| No enrolled courses returned | Show empty-state message ("no enrolled courses"); conversation ends — no further API calls |

---

## Step 2 — Course Hierarchy Fetch

> Retrieves the full tree of modules and leaf-node resource identifiers for the matched course.

**Endpoint:** `GET /api/content/v2/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v2/read/{course_id}" \
  -H "Content-Type: application/json"
```

### Response Fields Used

| Field | Used For |
|---|---|
| `result.content.leafNodes[]` | **Primary** — flat list of all leaf resource identifiers in the course |
| `result.content.children[]` | **Defensive fallback only**, used solely if `leafNodes` is absent — top-level `identifier` of each child is extracted (`extract_child_course_ids`); nested children are not recursed into |

### Resource ID Collection Logic

```
all_resource_ids = leafNodes[]                       # used if non-empty
all_child_ids     = [c.identifier for c in children[]]  # fallback only

filters.identifier for Step 3 = all_resource_ids OR all_child_ids OR []
```

`leafNodes` and `children` are **not** merged/deduplicated together — `children` is only
consulted when `leafNodes` is missing or empty.

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `leafNodes[]` or `children[]` yields at least one ID | Proceed to Step 3 with those IDs |
| Both empty (no IDs found at all) | Show "no resources found" message, then offer a support ticket — Step 3 is skipped entirely (an empty `filters.identifier` would otherwise make Step 3 return unrelated platform-wide content) |
| API call fails (network/5xx/4xx) | Offer a support ticket via the generic API-error fallback |

---

## Step 3 — Resource Picker (Metadata Fetch)

> Resolves resource identifiers to names and types, and populates a searchable, paginated resource dropdown. The user selects the resource directly (no name matching); the picked item's `mimeType` then drives the branch logic below.

**Endpoint:** `POST /api/composite/v4/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/composite/v4/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["<resource_id_1>", "<resource_id_2>"]
      },
      "isSecureSettingsDisabled": true,
      "fields": ["identifier", "name", "mimeType"],
      "limit": 1000
    }
  }'
```

> `filters.identifier` is `all_resource_ids` (or `all_child_ids` fallback) collected in Step 2.

### Response Fields Used

| Field | Used For |
|---|---|
| `result.content[].identifier` | Resource ID — picker option ID; becomes `{resource_id}`/`{id}` for Steps 3b/4 |
| `result.content[].name` | Picker option label; stored as `collected.resource_name` |
| `result.content[].mimeType` | Stored as `collected.resource_mime_type`; determines branch below |

The resource list is client-side searchable and paginated (15 per page).

---

## Step 3b — Missing-Resource Fallback Read (added 2026-07-23)

> Composite search (Step 3) can silently drop known resource IDs (e.g. Draft/Retired
> status) from its response. Any ID present in `all_resource_ids`/`all_child_ids` (Step 2)
> but absent from the Step 3 result is fetched individually so it still appears as a
> selectable option.

**Endpoint:** `GET /api/content/v1/read/{id}` — called once per missing ID

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/{id}"
```

### Response Fields Used

| Field | Used For |
|---|---|
| `result.content.name` | Picker option label / `collected.resource_name` if selected |
| `result.content.mimeType` | `collected.resource_mime_type` if selected |

If the individual read also fails or has no `name`, that ID is silently skipped (not added
to the picker).

### mimeType Mapping

| `mimeType` value | Mapped Resource Type |
|---|---|
| `application/pdf` | PDF |
| `video/mp4` | MP4 Video |
| `audio/mpeg` | MP3 Audio |
| `text/x-url` | Youtube |
| `application/vnd.ekstep.html-archive` | SCORM / HTML Archive |
| `application/vnd.sunbird.questionset` | Assessment |

> Only `application/pdf`, `video/mp4`, and `application/vnd.sunbird.questionset` get
> bespoke wording ("PDF document" / "video file" / "assessment") in the final ticket
> message. `audio/mpeg` and any other unmatched `mimeType` (that isn't YouTube or SCORM)
> fall through to the generic "content module" wording.

### Decision After Step 3

| Condition | Outcome |
|---|---|
| No resources found at all (picker empty) | Show "no resources found" message, then offer a support ticket |
| Device type is mobile (from the start of the flow) | Skip the branches below entirely; go straight to a mobile-specific ticket-confirmation summary |
| Selected `mimeType == "text/x-url"` | Proceed to Step 4 to fetch playback URLs |
| Selected `mimeType == "application/vnd.ekstep.html-archive"` | SCORM message → offer a support ticket (no further API calls) |
| Any other `mimeType` | Generic "content module/PDF/video/assessment" message → offer a support ticket (no further API calls) |

---

## Step 4 — Content Read (YouTube resources only)

> Fetches the actual playback URLs for a YouTube-type resource to diagnose URL configuration.

**Endpoint:** `GET /api/content/v1/read/{resource_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v1/read/{resource_id}"
```

### Response Fields Used

| Field | Key Returned As | Purpose |
|---|---|---|
| `result.content.streamingUrl` | `collected.streaming_url` | Primary playback URL |
| `result.content.artifactUrl` | `collected.artifact_url` | Fallback source URL |
| `result.content.previewUrl` | `collected.preview_url` | Preview URL |

### Decision After Step 4

The branch requires all three URLs to be present **and identical** to be treated as
correctly configured — it is not simply "at least one URL present":

| Condition | Outcome |
|---|---|
| `streaming_url` is non-null **and** `streaming_url == artifact_url == preview_url` | Treated as correctly configured → ask the user whether YouTube is network-restricted (restricted → mobile-app workaround; not restricted → offer a ticket for further investigation) |
| Any other case (a URL is null, or the three URLs differ) | Treated as a configuration issue → offer a support ticket directly |

---

## API Dependency Table

> **Auth note:** all Karmayogi calls carry the same static Bearer API-key `Authorization`
> header, added uniformly by the integration service layer (`app/services/karmayogi.py`),
> not per-endpoint from the flow YAML. There is no differential auth between these calls.

| Step | Endpoint | Method | Purpose | Key Fields |
|---|---|---|---|---|
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Populate enrolled-course picker | `courseId`, `courseName`, `completionPercentage`, `langContentStatus`, `batchId`/`batches` |
| 2 | `/api/content/v2/read/{course_id}` | GET | Collect resource identifiers (`leafNodes` primary, `children` fallback) | `leafNodes[]`, `children[].identifier` |
| 3 | `/api/composite/v4/search` | POST | Populate resource picker; resolve `mimeType` | `identifier`, `name`, `mimeType` |
| 3b (per missing ID) | `/api/content/v1/read/{id}` | GET | Fill in resource IDs composite search dropped | `content.name`, `content.mimeType` |
| 4 (YouTube only) | `/api/content/v1/read/{resource_id}` | GET | Fetch playback URLs for YouTube resources | `streamingUrl`, `artifactUrl`, `previewUrl` |
