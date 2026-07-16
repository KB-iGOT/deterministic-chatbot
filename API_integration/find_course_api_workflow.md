# UC-FindCourse: User Unable to Find a Course / Event / Unable to Enroll — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order.
> Flow file: `flows/mode_b_find_course.yaml`

---

## Execution Flow

```
PATH A — Marketplace / External Course
  (no API calls — pure guidance messaging)

PATH B — Course / Program
  STEP 1 → GET  /api/user/private/v1/read/{user_id}
               ↓ Fetch user profile (rootOrgId, org channel, profileStatus) for
                 MDO lookup context and eligibility evaluation
               ↓
  STEP 2 → POST /api/composite/v4/search
               ↓ Search all statuses (Live/Review/Draft/Retired)
               ↓ Also extracts content[0].secureSettings for moderated course detection
               ↓
           0 results                 → retry loop (no ticket)
           status = Retired          → inform user, close
           status ≠ Live             → inform under review, close
           secureSettings present    → MODERATED COURSE PATH (Steps 3M below)
           1 or more results         →

  STEP 3 → GET  /api/accessSettings/read/{course_id}   (regular course path)
               ↓ Compare course userGroups criteria against user profile
               ↓
           no config / error         → course is public → show link, close
           user is eligible (True)   → show link, close
           user not eligible (False) → ↓

  STEP 4 → POST /api/private/user/v1/search             (MDO admin lookup)
               ↓ Find MDO_ADMIN for user's rootOrgId
               ↓
           MDO found             → show MDO contact, close
           MDO not found / error → data_lookup: yp_lookup (local Excel)
                                   YP found  → show YP contact, close
                                   YP not found → ask user to connect with MDO or YP, close

  MODERATED COURSE PATH (secureSettings detected in Step 2)
  STEP 3M-A → [No HTTP call] check_secure_settings_eligibility transform
                  Already evaluated during composite_search response_mapping.
                  Checks: secureSettings.organisation vs user rootOrgId / ministryOrStateId
                          secureSettings.isVerifiedKarmayogi vs user profileDetails.profileStatus
                  ↓
              metadata_eligible = False → STEP 4 MDO/YP escalation (skip Step 3M-B)
              metadata_eligible = True  → ↓

  STEP 3M-B → GET /api/accessSettings/read/{course_id}
                  Same endpoint as regular course Step 3.
                  ↓
              no config / 404           → user eligible (metadata was only gate) → show course link
              access eligible = True    → show course link, close
              access eligible = False   → STEP 4 MDO/YP escalation

PATH C — Event
  STEP 1 → POST /api/composite/v4/search
               ↓ Search contentType=Event, all statuses
               ↓
           0 results           → retry loop (no ticket)
           status = Retired    → inform user, close
           status ≠ Live       → inform under review, close
           status = Live       → ↓

  STEP 2 → GET  /api/accessSettings/read/{event_id}    (karmayogi — same portal base URL)
               ↓ Compare event userGroups criteria against user profile
               ↓
           no config / error         → event is public → show link, close
           user not eligible (False) → MDO/YP escalation (same as PATH B STEP 4)
```

---

## PATH B — Step 1: User Profile Read

> Fetches user profile to provide `rootOrgId`, `org_channel`, `profileStatus`, and `ministryOrStateId`
> for downstream MDO/YP lookups and moderated course eligibility evaluation.

**Endpoint:** `GET /api/user/private/v1/read/{user_id}`
**Integration:** `karmayogi`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}" \
  -H "Content-Type: application/json"
```

### Response Fields Used

| Field | Stored As | Used For |
|---|---|---|
| `result.response.firstName` | `collected.first_name` | Display name |
| `result.response.rootOrgId` | `collected.root_org_id` | MDO admin search filter |
| `result.response.channel` | `collected.org_channel` | YP lookup key |
| `result.response` (full object) | `collected.user_eligibility_ctx` | Built by `build_user_eligibility_ctx` transform — flat dict keyed by access-settings `criteriaKey` names (group, designation, rootOrgId, user, department, cadre, service, batch) **plus** `profile_status` and `ministry_or_state_id` used for moderated course checks. |

#### `user_eligibility_ctx` — Additional fields for moderated courses

| key in ctx | Source field | Used by |
|---|---|---|
| `profile_status` | `profileDetails.profileStatus` | `check_secure_settings_eligibility` — compared against `isVerifiedKarmayogi` |
| `ministry_or_state_id` | `profileDetails.ministryOrStateId` (fallback: `rootOrgId`) | `check_secure_settings_eligibility` — compared against `secureSettings.organisation` list |

---

## PATH B — Step 2: Composite Content Search (Course)

> Keyword search across all course/program statuses. Also extracts `secureSettings` to detect
> moderated courses and computes metadata eligibility in a single response-mapping pass.

**Endpoint:** `POST /api/composite/v4/search`
**Integration:** `karmayogi`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/composite/v4/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "query": "<user_search_term>",
      "filters": {
        "status": ["Live", "Review", "Draft", "Retired"]
      },
      "sort_by": { "createdOn": "desc" },
      "limit": 10
    }
  }'
```

> **No `primaryCategory` filter.** Karmayogi's content taxonomy has more course-like
> categories than just `"Course"`/`"Program"` (e.g. `"Curated Program"`). Filtering on
> `primaryCategory: ["Course", "Program"]` silently excluded live courses under those
> other categories from results, regardless of query match. Confirmed via manual API
> testing that dropping the filter surfaces them correctly (see GAP-5).

### Response Fields Used

| Field | Stored As | Used For |
|---|---|---|
| `result.count` | `collected.composite_count` | 0 → not found; >1 → multiple results |
| `result.content[0].name` | `collected.course_name_found` | Display to user |
| `result.content[0].status` | `collected.course_status` | Branch: Live / Retired / other |
| `result.content[0].identifier` | `collected.course_id` | Passed to access settings API |
| `result.content[0].secureSettings` | `collected.course_secure_settings` | Raw secureSettings dict; non-null = moderated course |
| `result.content[0].secureSettings` | `collected.metadata_eligible` | `check_secure_settings_eligibility` transform result (True/False) |

### secureSettings Structure (Moderated Course)

```json
{
  "isVerifiedKarmayogi": "Yes" | "No",
  "organisation": ["<rootOrgId-1>", "<rootOrgId-2>"],
  "version": 1
}
```

| Field | Logic |
|---|---|
| `organisation` | List of eligible org IDs. User's `rootOrgId` or `profileDetails.ministryOrStateId` must appear in this list. |
| `isVerifiedKarmayogi` | If `"Yes"`, user's `profileDetails.profileStatus` must equal `"VERIFIED"`. If `"No"`, no verification check applied. |

### Transform: `check_secure_settings_eligibility`

Applied in the `composite_search` response_mapping (Gate 1 of moderated course eligibility).
Called with `(secureSettings, user_eligibility_ctx)`:

- `secureSettings` is `None` / not a dict → not moderated → `True` (skip moderated path)
- `organisation` list non-empty: user's `rootOrgId` OR `ministry_or_state_id` must be in the list
- `isVerifiedKarmayogi == "Yes"`: user's `profile_status` must equal `"VERIFIED"`
- All applicable checks must pass (AND logic)

Returns `True` (eligible) or `False` (not eligible).

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `count == 0` or `content[0].name == None` | Course not found → ask user to retry |
| `content[0].status == 'Retired'` | Course retired — inform user, close |
| `content[0].status != 'Live'` | Course under review — inform user, close |
| `secureSettings != None` | **Moderated course** → go to Gate 1 eligibility check |
| `count > 1` and `status == 'Live'` (non-moderated) | Multiple results — access check on top result |
| `count == 1` and `status == 'Live'` (non-moderated) | Single result — proceed to access settings |

---

## PATH B — Step 3M: Moderated Course Eligibility (Dual-Gate)

> Applies only when `result.content[0].secureSettings` is non-null.
> Two independent gates must both pass. Evaluation order: Gate 1 (metadata) → Gate 2 (Access Settings).

### Gate 1 — Metadata Eligibility (secureSettings, no HTTP call)

Evaluated inside the `composite_search` response_mapping via `check_secure_settings_eligibility` transform.
No separate API call. Result stored as `collected.metadata_eligible`.

| Check | Source (course) | Source (user ctx) | Pass condition |
|---|---|---|---|
| Organisation | `secureSettings.organisation` (list of rootOrgIds) | `user_eligibility_ctx.rootOrgId` or `user_eligibility_ctx.ministry_or_state_id` | User's org ID appears in the list |
| Verified Karmayogi | `secureSettings.isVerifiedKarmayogi` | `user_eligibility_ctx.profile_status` | If `"Yes"`, user's `profileStatus` must be `"VERIFIED"` |

**Gate 1 outcome:**
- `metadata_eligible = False` → skip Gate 2, go directly to MDO/YP escalation
- `metadata_eligible = True` → proceed to Gate 2

### Gate 2 — Access Settings API (Step 3M-B)

**Endpoint:** `GET /api/accessSettings/read/{course_id}`
**Integration:** `karmayogi`

Same endpoint and `check_user_eligibility` transform as the regular course Step 3.
Result stored as `collected.moderated_access_eligible` (separate key, does not overwrite regular path).

| Condition | Outcome |
|---|---|
| API returns 404 / error | No access config — metadata was the only gate; user is eligible → show moderated course link |
| `moderated_access_eligible == True` | Both gates passed → show moderated course link |
| `moderated_access_eligible == False` | Second gate failed → MDO/YP escalation |

### Messaging — SOP §12 (Eligible)

> "Upon checking, we found that you are eligible for the requested moderated course."
> Course Name + Hyperlink
> "Kindly use the above link to access and enroll in the course."

### Messaging — SOP §13 (Not Eligible)

Same MDO/YP escalation as the regular course ineligibility path:

> "Upon checking, we found that the course access criteria does not match your current profile attributes.
> We request you to connect with the concerned MDO/YP for further assistance."

Then fetches MDO admin via `POST /api/private/user/v1/search` (same as regular Step 4).
Fallback to YP/AM lookup if MDO not found.

---

## PATH B — Step 3: Access Settings Read (Regular Course)

> Checks whether the course has access restrictions. Compares course `userGroups` criteria against the user's profile to determine eligibility.

**Endpoint:** `GET /api/accessSettings/read/{course_id}`
**Integration:** `karmayogi` (same portal base URL — `portal.uat.karmayogibharat.net`)

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/accessSettings/read/{course_id}" \
  -H "wid: {user_id_hash}" \
  -H "Content-Type: application/json"
```

### Response Fields Used

| Field | Stored As | Transform | Used For |
|---|---|---|---|
| `result.accessControl.userGroups` | `collected.access_control_eligible` | `check_user_eligibility` (ctx: `collected.user_eligibility_ctx`) | `True` = eligible; `False` = not eligible |

### Transform: `check_user_eligibility`

Implemented in `app/engine/nodes/api_call_node.py`. Called with `(userGroups, user_eligibility_ctx)`:
- `userGroups` is `None` / empty list → `True` (no restrictions — publicly accessible)
- **OR** across `userGroups`: user must satisfy **all** criteria of at least one group
- **AND** within a group: every `criteriaKey` must match a value in `user_eligibility_ctx`

Supported `criteriaKey` values and their source in the user profile:

| criteriaKey | Source field |
|---|---|
| `group` | `profileDetails.professionalDetails[].group` (list — any entry matches) |
| `designation` | `profileDetails.professionalDetails[].designation` (list — any entry matches) |
| `rootOrgId` | top-level `rootOrgId` (scalar) |
| `user` / `userid` | `identifier` UUID (scalar) |
| `department` | `profileDetails.employmentDetails.departmentName` (scalar) |
| `cadre` | `profileDetails.cadreDetails.cadreName` (scalar) |
| `service` | `profileDetails.cadreDetails.civilServiceName` (scalar) |
| `batch` | `profileDetails.cadreDetails.cadreBatch` as string (scalar) |

- Returns `True` (eligible) if user matches any group
- Returns `False` (not eligible) if no group matches → proceed to MDO lookup

### Decision After Step 3

| Condition | Outcome |
|---|---|
| API returns 404 / error | No access config — treat as public; show course link |
| `access_control_eligible == True` | User meets criteria; show course link |
| `access_control_eligible == False` | User does not meet criteria — proceed to MDO lookup |

> **SOP Note (§14):** If the Access Settings Read API does not return any access configuration, it indicates that no access restrictions have been configured. The course is considered publicly available and can be accessed by all users.

---

## PATH B — Step 4: MDO Admin Lookup

> Looks up the MDO_ADMIN for the user's organization to provide escalation contact.

**Endpoint:** `POST /api/private/user/v1/search`
**Integration:** `karmayogi`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "<collected.root_org_id>",
        "organisations.roles": ["MDO_ADMIN"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

### Response Fields Used

| Field | Stored As | Used For |
|---|---|---|
| `result.response.count` | `collected.mdo_admin_count` | 0 → fall back to YP lookup |
| `result.response.content[0].profileDetails.personalDetails.firstname` | `collected.mdo_admin_name` | Display to user |
| `result.response.content[0].profileDetails.personalDetails.primaryEmail` | `collected.mdo_admin_email` | Display to user |

### Decision After Step 4

| Condition | Outcome |
|---|---|
| `mdo_admin_count > 0` | Show MDO Name + Email, close |
| `mdo_admin_count == 0` or `None` | Fall back to YP/AM lookup (`data_lookup: yp_lookup`) |
| API error | Fall back to YP/AM lookup (`data_lookup: yp_lookup`) |

---

## PATH B — Step 4b: YP/AM Lookup (Fallback)

> Local data lookup from `data/Allocation_28.10.2025.xlsx` keyed by `org_channel`.
> No HTTP call — resolved in memory by the `yp_lookup` service.

**Integration:** `data_lookup: yp_lookup`

### Fields Stored

| Field | Stored As | Used For |
|---|---|---|
| `name` | `collected.yp_name` | Display to user |
| `email` | `collected.yp_email` | Display to user |
| `mobile` | `collected.yp_mobile` | Display to user |
| `cc_email` | `collected.yp_cc_email` | Display to user |

### Decision After Step 4b

| Condition | Outcome |
|---|---|
| YP record found | Show YP Name + Email + Mobile, close |
| No matching record | Inform user to connect with their MDO or YP directly, close |

---

## PATH C — Step 1: Composite Content Search (Event)

> Keyword search for events across all statuses.

**Endpoint:** `POST /api/composite/v4/search`
**Integration:** `karmayogi`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/composite/v4/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "query": "<user_event_name>",
      "filters": {
        "contentType": ["Event"],
        "status": ["Live", "Review", "Draft", "Retired"]
      },
      "sort_by": { "createdOn": "desc" },
      "limit": 10
    }
  }'
```

### Response Fields Used

| Field | Stored As | Used For |
|---|---|---|
| `result.count` | `collected.event_search_count` | 0 → not found |
| `result.Event[0].name` | `collected.event_name_found` | Display to user |
| `result.Event[0].status` | `collected.event_status` | Branch: Live / Retired / other |
| `result.Event[0].identifier` | `collected.event_id` | Passed to access settings API |

---

## PATH C — Step 2: Access Settings Read (Event)

Same endpoint and logic as PATH B Step 3, using `collected.event_id` in the URL.

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/accessSettings/read/{event_id}" \
  -H "wid: {user_id_hash}" \
  -H "Content-Type: application/json"
```

**Response field stored as:** `collected.event_access_control_eligible`

Same `check_user_eligibility` transform (ctx: `collected.user_eligibility_ctx`) and branching logic applies.

---

## Known Gaps

| Gap | Description |
|---|---|
| GAP-1 | SOP §4.2 requires semantic ≥90% similarity match. Approximated with keyword search via `query` field. No embedding/vector similarity in YAML. |
| GAP-2 | SOP §10 requires per-course eligibility loop for multiple results. Only top result is processed; user is prompted to refine search if needed. |
| GAP-3 | Course/event hyperlink URL pattern not confirmed. Placeholder used: `https://portal.karmayogibharat.net/app/toc/{id}/overview` |
| GAP-4 | **RESOLVED** — Access Settings `result.accessControl.userGroups[].userGroupCriteriaList[]` criteria are now compared field-by-field against the user's `profileDetails.professionalDetails` via the `check_user_eligibility` transform. |
| GAP-5 | **RESOLVED** — `primaryCategory` filter removed from composite search. Filtering to `["Course", "Program"]` silently excluded live content under other Karmayogi categories (e.g. `"Curated Program"`), so real courses never appeared in results regardless of query match. |
