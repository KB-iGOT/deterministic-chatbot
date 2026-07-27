# UC-APAR: Training Plan / APAR Courses Not Visible — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → POST /api/private/user/v1/search
                ↓ Fetch user profile (id, rootOrgId, channel, org, profileStatus)
                ↓
           user NOT found → stop; apologize and ask user to try again later (no ticket, no support email shown)
           org == iGOT / Prarambh → redirect to Transfer Request guide

STEP 2   → GET  /api/supportportal/cbplan/v2/admin/user/list/{userId}
                ↓ Header: x-authenticated-user-orgid: igot (static)
                ↓ Fetch CBP training plan (count + content list)
                ↓
           count == 0 / API error → proceed to Step 3 (no-plan path)
           count  > 0             → proceed to Step 2b

STEP 2b  → POST /api/course/private/v4/user/enrollment/list/{userId}
                ↓ Fetch all enrollments for status calculation
                ↓ Show APAR / Non-APAR summary + navigation steps

STEP 3   → GET  /api/user/private/v1/read/{userId}          (no-plan path only)
                ↓ Fetch org, designation, group, cadre/service/batch/deputation
                ↓
           org == iGOT / Prarambh  → Transfer Request guide
           profile NOT verified     → profile verification guide (SOP §7)
           profile verified         → confirm details with user → Step 4

STEP 4   → POST /api/private/user/v1/search                 (MDO Admin lookup)
                ↓ filters: rootOrgId + role MDO_ADMIN + status 1
                ↓
           MDO found     → share contact details
           MDO NOT found → YP/SPOC data lookup fallback
                                ↓
                           YP/SPOC found     → share contact details
                           YP/SPOC NOT found → offer to raise a Zoho support ticket
                                                (POST /tickets, category=course/apar_not_visible, P3)

EC1      → POST /api/course/private/v4/user/enrollment/list/{userId}
                ↓ Edge Case 1: specific course missing — check enrollment & completion

EC2      → GET  /api/user/private/v1/read/{userId}
                ↓ Edge Case 2: wrong CBP/course — re-fetch designation & group
```

> **Note:** Step 2b runs only when a CBP plan exists. Step 3 onwards runs only when no plan is found. EC1/EC2 are triggered from the plan summary screen by the user.

---

## Step 1 — User Profile Fetch

> Confirms the user exists and collects org context needed for all downstream decisions.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "userId": "{{user_id_hash}}"
      },
      "limit": 1
    }
  }'
```

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.count` | `collected.user_found_count` | Verify user exists |
| `response.content[0].id` | `collected.fetched_user_id` | Primary key for all subsequent API calls |
| `response.content[0].maskedEmail` | `collected.user_email` | Display / ticket reference |
| `response.content[0].rootOrgId` | `collected.root_org_id` | Passed to MDO Admin lookup |
| `response.content[0].channel` | `collected.org_channel` | YP/SPOC lookup key; org identity check |
| `response.content[0].organisations[0].orgName` | `collected.org_name` | Org identity check; display |
| `response.content[0].profileDetails.profileStatus` | `collected.profile_status` | Verified / unverified branch |
| `response.content[0].profileDetails.personalDetails.primaryEmail` | `collected.user_primary_email` | Ticket description |

### Decision After Step 1

| Condition | Outcome |
|---|---|
| `user_found_count == 0` or `fetched_user_id == null` | User not found; apologize and stop (no support email is displayed, no ticket raised) |
| `org_channel == "igot"` or `org_name` contains "prarambh" | Wrong org; guide to raise Transfer Request |
| Default | Proceed to Step 2 |

---

## Step 2 — CBP Training Plan Fetch

> Retrieves the full CBP training plan assigned to the user, including APAR and Non-APAR course lists.

**Endpoint:** `GET /api/supportportal/cbplan/v2/admin/user/list/{userId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/supportportal/cbplan/v2/admin/user/list/{{fetched_user_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "x-authenticated-user-orgid: igot"
```

> The `x-authenticated-user-orgid: igot` header is **static** and required for this endpoint regardless of the user's actual org.

> **JSONPath note:** The engine does not support filter expressions (`[?(@.xxx==yyy)]`). Map the full `content` list and use Jinja2 `selectattr` in templates to filter APAR vs Non-APAR (`isApar == true/false`).

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `content` | `collected.all_courses` | Full plan list; filtered by `isApar` in display templates |
| `count` | `collected.cbp_total_count` | Determines whether a plan exists |

### Plan Object Structure (per item in `all_courses`)

| Field | Used For |
|---|---|
| `isApar` | Filter into APAR (`true`) or Non-APAR (`false`) lists |
| `endDate` | Displayed as plan last date (formatted `DD/MM/YYYY`) |
| `contentList[].contentType` | Filter to `"Course"` items only |
| `contentList[].identifier` | Used to match against enrollment list; also builds TOC URL |
| `contentList[].name` | Course display name; used for EC1 name matching |

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `cbp_total_count > 0` | Plan exists; proceed to Step 2b (enrollment fetch) |
| `cbp_total_count == 0` or API error / 404 | No plan found; proceed to Step 3 (no-plan path) |

---

## Step 2b — Enrollment List Fetch (plan exists)

> Fetches all enrollments so that per-course completion status can be calculated and displayed in the plan summary.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{userId}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{{fetched_user_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true
    }
  }'
```

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `courses` | `collected.all_enrollment_list` | Raw enrollment list (not directly displayed) |
| `courses` (transform: `nested_apar_courses`) | `collected.apar_picker_list` | Picker options for the APAR course list (grouped by Plan) |
| `courses` (transform: `nested_non_apar_courses`) | `collected.non_apar_picker_list` | Picker options for the Non-APAR course list (grouped by Plan) |

> Both transforms take `courses` (this enrollment response) plus the already-fetched `collected.all_courses` (Step 2 plan content) as input.

### Status Calculation Logic (applied by the `nested_apar_courses` / `nested_non_apar_courses` transforms)

> **Note:** an older `flatten_apar_courses` / `flatten_non_apar_courses` transform pair (percentage-based "Incomplete (X%)" status using `progress` / `leafNodesCount`) exists in the codebase but is **not referenced by this flow** — do not rely on it for current behavior.

The two Plans (one per `isApar` value) from Step 2's `all_courses` are grouped into a picker list of `"Plan 1"`, `"Plan 2"`, … entries — sorted ascending by the Plan's `endDate` (nearest due date first; Plans with no parseable `endDate` sort last) — each with a `children` array of its individual courses:

| Enrollment `status` (matched by `contentList[].identifier` == enrollment `courseId`) | Derived Display Status |
|---|---|
| `2` | `Completed` |
| `1` | `In Progress` |
| Not enrolled / no match | `Not Started` |

- Each Plan's `combinedMeta` shown in the picker is `"Ends: DD/MM/YYYY"` (or blank if no parseable `endDate`).
- Each course's `courseId` / `courseName` come straight from `contentList[].identifier` / `contentList[].name`.
- **Cross-category exclusion:** if the same course `identifier` appears in a Plan of the *opposite* `isApar` value, it is excluded from both the APAR and Non-APAR picker lists (the portal doesn't surface such a course as either category).
- Plans that end up with zero qualifying courses are dropped from the picker entirely (and do not consume a "Plan N" number).

### Decision After Step 2b

Always proceeds to show the plan summary. API errors fall through to the same display (plan data already fetched in Step 2).

---

## Step 3 — User Read (no-plan path)

> Fetches extended profile details — org mapping, verification status, designation, group, and All India Service fields — to determine why no plan is assigned.

**Endpoint:** `GET /api/user/private/v1/read/{userId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{{fetched_user_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> Timeout: **5000 ms**. On API error, show a static "couldn't verify your organization / MDO details" message advising the user to contact their MDO Admin directly, and end — **no ticket is offered** on this error path.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.channel` | `collected.org_channel` | Refresh org identity check |
| `response.organisations[0].orgName` | `collected.org_name` | Refresh org name |
| `response.profileDetails.verifiedKarmayogi` | `collected.profile_verified` | Verified status (boolean) |
| `response.profileDetails.professionalDetails[0].designation` | `collected.user_designation` | Display; profile confirm screen |
| `response.profileDetails.professionalDetails[0].group` | `collected.user_group` | Display; profile confirm screen |
| `response.profileDetails.cadreDetails` | `collected.cadre_details` | AIS eligibility check |
| `response.profileDetails.serviceDetails` | `collected.service_details` | AIS eligibility check |
| `response.profileDetails.batch` | `collected.batch_details` | AIS eligibility check |
| `response.profileDetails.centralDeputation` | `collected.central_deputation` | AIS eligibility check |

> **TODO:** Verify exact API paths for `cadreDetails`, `serviceDetails`, `batch`, and `centralDeputation` against live API response before release.

### Decision After Step 3

| Condition | Outcome |
|---|---|
| `org_channel == "igot"` or `org_name` contains "prarambh" | Wrong org; guide Transfer Request |
| `profile_status == "VERIFIED"` or `profile_verified == true` | Profile verified; show details for user confirmation → Step 4 |
| Default | Profile not verified; guide profile verification (SOP §7) |

> **Note:** the "verified" path does not go straight to Step 4. After the user confirms their org/designation/group, they're asked an All India Services (AIS) eligibility question. If AIS and any of `cadre_details` / `service_details` / `batch_details` / `central_deputation` (all fetched in this step) is missing, the user is told to complete those profile fields instead of proceeding to Step 4. Only when AIS is "no", or AIS is "yes" with all four fields present, does the flow proceed to the Step 4 MDO Admin lookup.

---

## Step 4 — MDO Admin Lookup

> Identifies the MDO Admin for the user's org so they can be contacted to assign a training plan or resolve a course discrepancy.

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

### Decision After Step 4

| Condition | Outcome |
|---|---|
| `mdo_admin_count > 0` | Display MDO Admin contact details; resolution complete |
| `mdo_admin_count == 0` | No MDO Admin found; fall back to YP/SPOC data lookup |
| API failure | Fall back to YP/SPOC data lookup |

---

## EC1 — Specific Course Missing (Edge Case 1)

> Triggered when user reports a particular course is not visible in their plan. Checks if the course exists in the already-fetched plan, then verifies completion status.

### EC1 — Enrollment Check

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{userId}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{{fetched_user_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true,
      "status": ["In-Progress", "Completed"]
    }
  }'
```

> Timeout: **5000 ms**. On error, treat as not completed.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `courses` | `collected.ec1_enrollment_list` | Match against user-supplied course name to check completion |

### EC1 Decision Logic

| Condition | Outcome |
|---|---|
| Course name NOT found in `all_courses[].contentList` (partial/exact match) | Course not in plan; escalate to MDO Admin |
| Course found in plan AND enrollment `status == 2` or `completionPercentage == 100` | Course completed; guide user to Completed tab |
| Course found in plan AND not completed | Guide user to view it via APAR / Upcoming / All tabs |

> Name matching uses case-insensitive partial match in both directions: user input contained in course name, or course name contained in user input.

---

## EC2 — Wrong CBP / Course (Edge Case 2)

> Triggered when user reports the wrong CBP or course is showing. Re-fetches designation and group to confirm profile accuracy before escalating to MDO.

**Endpoint:** `GET /api/user/private/v1/read/{userId}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{{fetched_user_id}}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

> Timeout: **5000 ms**. On error, proceed to confirmation screen with available data.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.profileDetails.professionalDetails[0].designation` | `collected.user_designation` | Refresh for confirm screen |
| `response.profileDetails.professionalDetails[0].group` | `collected.user_group` | Refresh for confirm screen |

### EC2 Decision After Confirmation

| Condition | Outcome |
|---|---|
| User confirms profile details are correct | Escalate to MDO Admin (Step 4) |
| User says details are incorrect | Branch to incorrect-details guide (org / designation / group update) |

---

## YP/SPOC Fallback Lookup

> When no MDO Admin is found, the flow looks up the YP/SPOC contact via an internal data service.

**Service:** `yp_lookup` (internal data lookup — not a Karmayogi REST API)

**Lookup key:** `org_channel`

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `name` | `collected.yp_name` | Display name |
| `email` | `collected.yp_email` | Contact email |
| `mobile` | `collected.yp_mobile` | Contact mobile (displayed if present) |
| `cc_email` | `collected.yp_cc_email` | CC email for correspondence |

### Decision After YP/SPOC Lookup

| Condition | Outcome |
|---|---|
| YP/SPOC found | Display contact details; resolution complete |
| YP/SPOC not found | Offer to raise a Zoho support ticket (see below) — **not** a generic helpdesk message |

---

## Ticket Creation — No MDO Admin AND No YP/SPOC Found

> Reached only when both the MDO Admin lookup (Step 4) and the YP/SPOC data lookup return nothing. The user is asked to confirm before a ticket is raised; declining ends the conversation with no ticket.

**Endpoint:** `POST /tickets` (integration: `zoho_desk_api`, via the shared `_zoho_ticket` fragment)

- Ticket is generated by an LLM step (`transfer_llm`, `auto_raise: silent`) with directives: subject `"Training Plan / APAR Not Visible — No Admin or YP Contact Found"`, description including the user's organization/channel and noting neither contact could be found, `priority_override: P3`.
- Custom fields set via the flow's `_zoho_ticket` import parameters: `cf_category: course`, `cf_sub_category: apar_not_visible`, `cf_flow_id: APAR_NOT_VISIBLE`.
- On success: `$.ticketNumber` → `collected.ticket_id`; user is shown the ticket number. On failure: generic "couldn't raise the ticket" message.

### Decision

| Condition | Outcome |
|---|---|
| User confirms ticket creation | Raise Zoho ticket; show ticket ID |
| User declines | End conversation; no ticket raised |
| Ticket API fails | Show failure message; end |

---

## API Dependency Table

| Step | Endpoint | Method | Auth Required | Special Headers | Purpose | Key Fields |
|---|---|---|---|---|---|---|
| 1 | `/api/private/user/v1/search` | POST | Yes | — | Fetch user profile + org context | `id`, `rootOrgId`, `channel`, `profileStatus` |
| 2 | `/api/supportportal/cbplan/v2/admin/user/list/{userId}` | GET | Yes | `x-authenticated-user-orgid: igot` | Fetch CBP training plan | `count`, `content[].isApar`, `contentList` |
| 2b | `/api/course/private/v4/user/enrollment/list/{userId}` | POST | Yes | — | Fetch enrollments for status calculation | `courses[].courseId`, `status` |
| 3 | `/api/user/private/v1/read/{userId}` | GET | Yes | — | Fetch extended profile for no-plan diagnosis | `designation`, `group`, `verifiedKarmayogi`, cadre/service fields |
| 4 | `/api/private/user/v1/search` | POST | Yes | — | MDO Admin lookup | `mdo_admin_name`, `mdo_admin_email` |
| EC1 | `/api/course/private/v4/user/enrollment/list/{userId}` | POST | Yes | — | Check completion of specific course | `courses[].courseName`, `status`, `completionPercentage` |
| EC2 | `/api/user/private/v1/read/{userId}` | GET | Yes | — | Re-fetch designation/group for EC2 profile confirm | `designation`, `group` |
| Ticket | `/tickets` (zoho_desk_api) | POST | Yes | — | Raise support ticket when neither MDO Admin nor YP/SPOC contact is found | `ticketNumber` |

---

```
