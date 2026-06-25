# UC-CAP: Comprehensive Assessment Program (CAP) Not Visible — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → GET  /api/user/private/v1/read/{userId}
                ↓ Fetch profile (org, designation, group, cadre/service/batch/deputation, verification)
                ↓
           profile NOT verified → stop; guide profile verification
           AIS details missing  → inform user to update; stop

STEP 2   → POST /api/supportportal/admin/user/v2/assignedcourses/{userId}
                ↓ body: courseCategory = "Comprehensive Assessment Program"
                ↓ Fetch all assigned CAPs
                ↓
           count == 0 / API 400 → no CAP assigned; MDO Admin lookup
           count  > 0           → branch on issue type

── Issue: CAP Not Visible / Enrollment Issue ──────────────────────────────────

STEP 3a  → Confirm expected CAP with user
           CAP confirmed + end date passed → guide to search bar; stop
           CAP confirmed + active          → share direct link; stop
           CAP not in list                 → MDO Admin lookup

── Issue: Final Assessment Locked ────────────────────────────────────────────

STEP 3b  → POST /api/course/private/v4/user/enrollment/list/{userId}
                ↓ Fetch all enrollments; check CAP enrollment status

STEP 4   → GET  /api/private/content/v3/hierarchy/{capId}
                ↓ Fetch CAP hierarchy; identify incomplete child courses

STEP 5   → POST /api/admin/content/state/read               (per incomplete child)
                ↓ Check consumption records; detect technical issues

── Issue: Assessment Limit Exceeded ──────────────────────────────────────────

STEP 6a  → GET  /api/content/v2/read/{courseId}             (or hierarchy for Program)
                ↓ Fetch leaf nodes; diff against completed IDs

STEP 6b  → POST /api/content/v1/search
                ↓ Fetch resource metadata (mimeType, maxAttempts, SCORM detection)

STEP 6c  → GET  /api/admin/assesment/retake/count           (assessment-only resources)
                ↓ Fetch attemptsMade + attemptsAllowed
                ↓
           attempts remaining → guide retry
           limit exceeded     → raise support ticket
```

---

## Step 1 — User Profile Fetch

> Retrieves full profile details to gate CAP access: verification status, professional details, and All India Service fields.

**Endpoint:** `GET /api/user/private/v1/read/{userId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{{user_id_hash}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> Timeout: **5000 ms**. On API error, escalate to support message.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.channel` | `collected.org_channel` | Org identity; YP/SPOC lookup key |
| `response.rootOrgId` | `collected.root_org_id` | MDO Admin lookup |
| `response.organisations[0].orgName` | `collected.org_name` | Display; confirm screen |
| `response.profileDetails.verifiedKarmayogi` | `collected.profile_verified` | Verification gate |
| `response.profileDetails.profileStatus` | `collected.profile_status` | Verification gate (alternate field) |
| `response.profileDetails.professionalDetails[0].designation` | `collected.user_designation` | Profile confirm screen |
| `response.profileDetails.professionalDetails[0].group` | `collected.user_group` | Profile confirm screen |
| `response.profileDetails.cadreDetails` | `collected.cadre_details` | AIS completeness check |
| `response.profileDetails.serviceDetails` | `collected.service_details` | AIS completeness check |
| `response.profileDetails.batch` | `collected.batch_details` | AIS completeness check |
| `response.profileDetails.centralDeputation` | `collected.central_deputation` | AIS completeness check |

### Decision After Step 1

| Condition | Outcome |
|---|---|
| `profile_status != "VERIFIED"` and `profile_verified != true` | Profile not verified; guide verification steps; stop |
| All four AIS fields present | Show full profile details (org, designation, group, cadre, service, batch) for confirmation |
| Any AIS field missing | Show limited profile details (org, designation, group); ask AIS eligibility |
| User confirms details correct + AIS eligible + AIS fields missing | Inform user to update missing service details; stop |
| User confirms details correct (non-AIS or AIS fields complete) | Proceed to Step 2 |
| User says details are incorrect | Branch to update guide (Transfer Request / Designation / Group) |

> **Verification check:** `profile_status.upper() == "VERIFIED"` OR `str(profile_verified).lower() == "true"`

---

## Step 2 — Assigned CAP Fetch

> Fetches all CAPs assigned to the user filtered by `courseCategory`.

**Endpoint:** `POST /api/supportportal/admin/user/v2/assignedcourses/{userId}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/supportportal/admin/user/v2/assignedcourses/{{user_id_hash}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "x-authenticated-user-token: " \
  -H "Content-Type: application/json" \
  -d '{
    "courseCategory": "Comprehensive Assessment Program"
  }'
```

> The `x-authenticated-user-token` header must be present but can be empty string for this endpoint.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `count` | `collected.cap_count` | Determine if any CAPs are assigned |
| `content` | `collected.all_caps` | Full CAP list for display and selection |
| `content[0].identifier` | `collected.cap_id` | Default CAP ID (single-CAP case) |
| `content[0].name` | `collected.cap_name` | Default CAP name |
| `content[0].endDate` | `collected.cap_end_date` | Expiry check for visibility issue |

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `all_caps` is empty / null | No CAP assigned; escalate to MDO Admin lookup |
| API returns 400 | No CAP assigned; escalate to MDO Admin lookup |
| `cap_issue_type == "cap_assessment_locked"` and `len(all_caps) > 1` | Show CAP picker (multi-CAP) → proceed to Step 3b |
| `cap_issue_type == "cap_assessment_locked"` and single CAP | Proceed directly to Step 3b |
| `cap_issue_type == "cap_not_visible"` | Proceed to Step 3a (confirm expected CAP) |
| `cap_issue_type == "cap_enrollment_issue"` | Proceed to Step 3a (confirm CAP name) |

---

## Step 3a — CAP Visibility / Enrollment Confirmation

> Presents assigned CAPs to the user and confirms whether the expected CAP is in the list. No additional API call required — uses `all_caps` from Step 2.

### Decision After Step 3a

| Condition | Outcome |
|---|---|
| User confirms CAP is correct + `cap_end_date` is in the past | CAP expired; guide user to search bar; stop |
| User confirms CAP is correct + `cap_end_date` is future or null | Share direct link (`/app/toc/{identifier}/overview`); stop |
| User says CAP is not in list | CAP incorrectly assigned; escalate to MDO Admin lookup |

> **Expiry check:** `hours_since(cap_end_date) > 0`

> **TOC URL pattern:** `https://portal.uat.karmayogibharat.net/app/toc/{identifier}/overview`

---

## Step 3b — Enrollment List Fetch (Final Assessment Locked)

> Checks whether the user is enrolled in the target CAP before attempting hierarchy analysis.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{userId}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{{user_id_hash}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "status": ["0", "1", "2"]
      }
    }
  }'
```

> Timeout: **8000 ms**.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `courses` | `collected.all_enrollment_list` | Match against CAP identifier to verify enrollment; used in Step 4 for child-course completion checks |

### Enrollment Status Values

| `status` value | Meaning |
|---|---|
| `0` | Not started |
| `1` | In-progress |
| `2` | Completed |

### Decision After Step 3b

| Condition | Outcome |
|---|---|
| CAP identifier found in `all_enrollment_list` | Enrolled; proceed to Step 4 (hierarchy fetch) |
| CAP identifier not found | Not enrolled; guide user to enroll via TOC link; stop |

---

## Step 4 — CAP Hierarchy Fetch

> Retrieves the CAP's course tree to identify child courses and check their completion status against the enrollment list.

**Endpoint:** `GET /api/private/content/v3/hierarchy/{capId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/private/content/v3/hierarchy/{{selected_cap_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> `selected_cap_id` = user-selected CAP (multi-CAP picker) or `all_caps[0].identifier` (single CAP).  
> Timeout: **8000 ms**. On error, treat as not enrolled.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `content.children` | `collected.cap_hierarchy_children` | Iterate child courses for completion check |
| `content` | `collected.incomplete_child_courses` | `extract_incomplete_child_courses` transform applied using `all_enrollment_list` as context |

### Child-Course Completion Logic (applied in display templates)

A child course is considered **complete** if any of the following are true for its enrollment record:

- `enrollment.status == 2`
- `enrollment.completionPercentage == 100`
- `enrollment.issuedCertificates` is non-empty

Child courses whose `name` contains `"assessment"` (case-insensitive) are excluded from the incomplete check.

### Pending Resource Detection

For each incomplete child course, pending leaf resources are identified via `langContentStatus`:

- If `langContentStatus` is present: a resource is pending if no language key maps the resource `identifier` to status `2`.
- If `langContentStatus` is absent: all leaf resources in the module are listed as pending.
- Resources whose `name` contains `"assessment"` are excluded.

### Decision After Step 4

| Condition | Outcome |
|---|---|
| Incomplete child courses found | Display pending resource list; proceed to Step 5 for tech-issue detection |
| No incomplete child courses | All prerequisites complete but assessment still locked; show access-issue options (Mobile / Other Error) |

---

## Step 5 — Child Course Content State Read (per incomplete child)

> For each incomplete child course, fetches admin-side consumption records to detect technical sync issues.

**Endpoint:** `POST /api/admin/content/state/read`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/admin/content/state/read" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "userId":   "{{user_id_hash}}",
      "courseId": "{{incomplete_child_courses[idx].courseId}}",
      "batchId":  "{{incomplete_child_courses[idx].batchId}}"
    }
  }'
```

> Called iteratively for each entry in `incomplete_child_courses` using counter `cap_child_idx`. Timeout: **8000 ms**.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `consumptionRecords[*]` | `collected.admin_content_states` | `extract_consumption_records` transform; fed into `check_cap_technical_issue()` |

### Decision After Step 5

| Condition | Outcome |
|---|---|
| `check_cap_technical_issue(all_enrollment_list, admin_content_states, courseId) == true` | Technical issue detected; raise Engineering Excel ticket; stop |
| No technical issue | Increment `cap_child_idx`; loop to next incomplete child |
| All children processed with no tech issue | Proceed to final assessment access-issue options |

---

## Step 6a — Content Read / Program Hierarchy (Assessment Limit path)

> Fetches leaf nodes for the selected course to identify incomplete resources. Branches on `primaryCategory`.

### 6a-i — Content Read (non-Program)

**Endpoint:** `GET /api/content/v2/read/{courseId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/content/v2/read/{{course_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

### 6a-ii — Program Hierarchy (if `primaryCategory == "Program"`)

**Endpoint:** `GET /api/private/content/v3/hierarchy/{courseId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/private/content/v3/hierarchy/{{course_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> Timeout: **8000 ms** for both.

### Response Fields Used (both endpoints)

| Field | Mapped To | Used For |
|---|---|---|
| `content.primaryCategory` | `collected.cap_limit_primary_category` | Switch between content read vs hierarchy |
| `content.mimeType` | `collected.cap_limit_mime_type` | Resource type context |
| `content.leafNodes` | `collected.cap_limit_incomplete_leaf_nodes` | `diff_leaf_nodes` transform against `completed_ids` |
| `content.leafNodes` | `collected.cap_limit_raw_leaf_nodes` | Raw list; used to detect if course is already complete |
| `content.children` | `collected.cap_limit_child_ids` | `extract_child_course_ids` transform |

### Decision After Step 6a

| Condition | Outcome |
|---|---|
| `raw_leaf_nodes` non-empty AND `incomplete_leaf_nodes` empty | All resources complete → course already finished; guide certificate download |
| Incomplete leaf nodes remain | Proceed to Step 6b (resource metadata fetch) |

---

## Step 6b — Resource Metadata Fetch

> Resolves names, mimeTypes, and attempt limits for all incomplete leaf node identifiers.

**Endpoint:** `POST /api/content/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/content/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "identifier": ["<incomplete_leaf_id_1>", "<incomplete_leaf_id_2>"]
      },
      "fields": ["identifier", "name", "mimeType", "status", "duration", "primaryCategory", "maxAttempts"],
      "limit": 1000
    }
  }'
```

> `filters.identifier` = `cap_limit_incomplete_leaf_nodes` (or `cap_limit_child_ids` or `[course_id]` as fallback).  


### Response Fields Used

| Field | Mapped To | Transform | Used For |
|---|---|---|---|
| `content[*].name` | `collected.incomplete_resource_names` | `extract_all_names` | Display list of pending resources |
| `content[*].mimeType` | `collected.has_scorm_resources` | `detect_scorm` | Branch to SCORM vs Non-SCORM guidance |
| `content[*]` | `collected.scorm_resource_name` | `extract_scorm_resource_name` | SCORM resource display name |
| `content[*]` | `collected.scorm_resource_duration_min` | `extract_scorm_duration_minutes` | Minimum time-on-task guidance |
| `content[*]` | `collected.all_resources_assessment` | `detect_assessment_only` | Detect if only assessment items remain |
| `content[0].maxAttempts` | `collected.assessment_max_attempts` | — | Attempt limit for Step 6c comparison |
| `content[0].identifier` | `collected.assessment_id` | — | Assessment ID for Step 6c API call |
| `content` | `collected.cap_limit_resources_metadata` | — | Full metadata object for downstream use |

### Decision After Step 6b

| Condition | Outcome |
|---|---|
| `incomplete_resource_names` is null/empty | Fetch hierarchy names fallback (`GET /api/course/v1/hierarchy/{courseId}`) |
| `all_resources_assessment == true` | Only assessment pending; proceed to Step 6c (attempt count check) |
| `has_scorm_resources == true` | SCORM content pending; show SCORM-specific guidance |
| Default | Non-SCORM content pending; show standard pending resource guidance |

---

## Step 6c — Assessment Attempt Count Check

> Fetches the number of attempts made and allowed for the specific assessment item.

**Endpoint:** `GET /api/admin/assesment/retake/count`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/admin/assesment/retake/count\
?assessmentIdentifier={{assessment_id}}&userId={{user_id_hash}}&editMode=false" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> Timeout: On API error, treat as limit exceeded and raise support ticket.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `result.attemptsMade` | `collected.assessment_attempts_made` | Remaining attempts calculation |
| `result.attemptsAllowed` | `collected.assessment_attempts_allowed` | Maximum allowed attempts |

### Decision After Step 6c

| Condition | Outcome |
|---|---|
| `attemptsAllowed - attemptsMade <= 0` | Limit exceeded; raise Zoho support ticket |
| Attempts remaining | Display remaining count; guide user to retry |

---

## MDO Admin Lookup

> Triggered when no CAP is assigned, an incorrect CAP is confirmed, or as a fallback when YP/SPOC lookup also fails.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "{{root_org_id}}",
        "organisations.roles": ["MDO_ADMIN"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.count` | `collected.mdo_admin_count` | Determine if MDO Admin exists |
| `response.content[0].profileDetails.personalDetails.firstname` | `collected.mdo_admin_name` | Display name |
| `response.content[0].profileDetails.personalDetails.primaryEmail` | `collected.mdo_admin_email` | Contact email |

### Decision

| Condition | Outcome |
|---|---|
| `mdo_admin_count > 0` | Show MDO Admin contact details; resolution complete |
| `mdo_admin_count == 0` or API failure | Fall back to YP/SPOC data lookup |

---

## YP/SPOC Fallback Lookup

> Internal data lookup service used when no MDO Admin is found.

**Service:** `yp_lookup` (internal — not a Karmayogi REST API)  
**Lookup key:** `org_channel`

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `name` | `collected.yp_name` | Display name |
| `email` | `collected.yp_email` | Contact email |
| `mobile` | `collected.yp_mobile` | Contact mobile (shown if present) |
| `cc_email` | `collected.yp_cc_email` | CC email for correspondence |

---

## API Dependency Table

| Step | Endpoint | Method | Auth | Special Headers | Timeout | Purpose | Key Fields |
|---|---|---|---|---|---|---|---|
| 1 | `/api/user/private/v1/read/{userId}` | GET | Yes | — | Full profile fetch | `verifiedKarmayogi`, `profileStatus`, AIS fields |
| 2 | `/api/supportportal/admin/user/v2/assignedcourses/{userId}` | POST | Yes | `x-authenticated-user-token: ` (empty)  | Fetch assigned CAPs | `count`, `content[].identifier`, `endDate` |
| 3b | `/api/course/private/v4/user/enrollment/list/{userId}` | POST | Yes | —  | Check CAP enrollment; completion status | `courses[].courseId`, `status`, `langContentStatus` |
| 4 | `/api/private/content/v3/hierarchy/{capId}` | GET | Yes | —  | Fetch CAP hierarchy; identify incomplete children | `children[].identifier`, `children[].name` |
| 5 | `/api/admin/content/state/read` | POST | Yes | — | Detect tech issue in incomplete child course | `consumptionRecords` |
| 6a-i | `/api/content/v2/read/{courseId}` | GET | Yes | —  | Fetch leaf nodes (non-Program) | `leafNodes`, `primaryCategory` |
| 6a-ii | `/api/private/content/v3/hierarchy/{courseId}` | GET | Yes | — | 8000 ms | Fetch leaf nodes (Program type) | `leafNodes`, `children` |
| 6b | `/api/content/v1/search` | POST | No | —  | Resolve resource names, mimeTypes, attempt limits | `name`, `mimeType`, `maxAttempts`, `identifier` |
| 6b fallback | `/api/course/v1/hierarchy/{courseId}` | GET | Yes | — | 8000 ms | Fallback hierarchy name fetch if search returns empty | `incomplete_resource_names` |
| 6c | `/api/admin/assesment/retake/count` | GET | Yes | — |  | Check attempts made vs allowed | `attemptsMade`, `attemptsAllowed` |
| MDO | `/api/private/user/v1/search` | POST | Yes | — | — | MDO Admin lookup | `mdo_admin_name`, `mdo_admin_email` |

---

