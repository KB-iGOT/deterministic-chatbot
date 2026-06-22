# UC-04: Resource / Content Not Opening — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → POST /api/course/private/v4/user/enrollment/list/{user_id}
                ↓ Verify user is enrolled in the named course
                ↓
           course NOT found in enrollments → stop; no further API calls

STEP 2   → GET  /api/content/v2/read/{course_id}
                ↓ Fetch course hierarchy — children[] and leafNodes[]
                ↓ Collect all resource identifiers from hierarchy

STEP 3   → POST /api/content/v1/search
                ↓ Fetch name, mimeType for all resource identifiers
                ↓ Match resource by name (exact → partial)
                ↓
           resource NOT matched → return all_course_resources[] for re-selection

STEP 4   → GET  /api/content/v1/read/{resource_id}     (YouTube resources only)
                ↓ mimeType == "text/x-url"
                ↓ Fetch streamingUrl, artifactUrl, previewUrl
```

> **Note:** Steps 1–3 always run for web-browser issues. Step 4 runs only when `mimeType` is `text/x-url` (YouTube).

---

## Step 1 — Enrollment Verification

> Confirms the user is enrolled in the course before fetching its structure.

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
| `result.courses[].courseId` | Passed to Step 2 as `{course_id}` |
| `result.courses[].courseName` | Matched against user-supplied name (exact then partial) |

### Decision After Step 1

| Condition | Outcome |
|---|---|
| Course name found in `courses[]` | `matched_course = true`; extract `courseId` and proceed to Step 2 |
| Course name not found | `matched_course = false`; return `all_enrolled_courses[]`; no further API calls |

---

## Step 2 — Course Hierarchy Fetch

> Retrieves the full tree of modules and leaf-node resource identifiers for the matched course.

**Endpoint:** `GET /api/content/v2/read/{course_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v2/read/{course_id}" \
  -H "Content-Type: application/json"
```

> No `Authorization` header required for this endpoint.

### Response Fields Used

| Field | Used For |
|---|---|
| `result.content.children[]` | Module-level children; each has `identifier` and nested `leafNodes[]` |
| `result.content.leafNodes[]` | Flat list of all leaf resource identifiers in the course |

### Resource ID Collection Logic

```
all_ids = deduplicate(
    leafNodes[]
    + [child.identifier for child in children[]]
)
```

These `all_ids` are passed as `filters.identifier` in Step 3.

### Decision After Step 2

| Condition | Outcome |
|---|---|
| Hierarchy returned with `children[]` or `leafNodes[]` | Build `all_ids[]` and proceed to Step 3 |
| Hierarchy empty or API failure | Return error; no resource lookup possible |

---

## Step 3 — Resource Metadata Fetch

> Resolves resource identifiers to names and types so the requested resource can be matched and its `mimeType` determined.

**Endpoint:** `POST /api/content/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/content/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["<resource_id_1>", "<resource_id_2>"]
      },
      "isSecureSettingsDisabled": true,
      "sort_by": { "createdOn": "desc" },
      "fields": ["identifier", "name", "mimeType", "endDate", "startDate", "status", "versionKey", "createdOn", "duration"],
      "facets": ["status"],
      "limit": 1000
    }
  }'
```

> `filters.identifier` is the `all_ids[]` array collected in Step 2.

### Response Fields Used

| Field | Used For |
|---|---|
| `result.content[].identifier` | Resource ID — used to match back and as `{resource_id}` in Step 4 |
| `result.content[].name` | Matched against user-supplied resource name (exact then partial) |
| `result.content[].mimeType` | Determines resource type; drives Step 4 branch |

### mimeType Mapping

| `mimeType` value | Mapped Resource Type |
|---|---|
| `application/pdf` | PDF |
| `video/mp4` | MP4 Video |
| `audio/mpeg` | MP3 Audio |
| `text/x-url` | Youtube |
| `application/vnd.ekstep.html-archive` | SCORM / HTML Archive |
| `application/vnd.sunbird.questionset` | Assessment |

### Decision After Step 3

| Condition | Outcome |
|---|---|
| Resource name matched (`matched = true`) | Extract `resource_id` and `mimeType`; proceed per branch below |
| Resource name not matched | Return `all_course_resources[]` (all names) for re-selection |
| `mimeType = "text/x-url"` | Proceed to Step 4 to fetch playback URLs |
| Any other `mimeType` | No further API calls required |

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
| `result.content.streamingUrl` | `diagnostic_urls.streamingUrl` | Primary playback URL |
| `result.content.artifactUrl` | `diagnostic_urls.artifactUrl` | Fallback source URL |
| `result.content.previewUrl` | `diagnostic_urls.previewUrl` | Preview URL |

### Decision After Step 4

| Condition | Outcome |
|---|---|
| At least one URL present (`streamingUrl` / `artifactUrl` / `previewUrl`) | URLs are configured; resource is reachable if YouTube is not network-blocked |
| All URLs null or absent | Resource has missing playback URL configuration |

---

## API Dependency Table

| Step | Endpoint | Method | Auth Required | Purpose | Key Fields |
|---|---|---|---|---|---|
| 1 | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | Yes | Verify enrollment; get `courseId` | `courseId`, `courseName` |
| 2 | `/api/content/v2/read/{course_id}` | GET | No | Fetch course hierarchy; collect all resource identifiers | `children[].identifier`, `leafNodes[]` |
| 3 | `/api/content/v1/search` | POST | No | Resolve resource names and mimeTypes | `identifier`, `name`, `mimeType` |
| 4 (YouTube only) | `/api/content/v1/read/{resource_id}` | GET | Yes | Fetch playback URLs for YouTube resources | `streamingUrl`, `artifactUrl`, `previewUrl` |
