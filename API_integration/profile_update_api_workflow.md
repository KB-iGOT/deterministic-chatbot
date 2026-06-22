# UC-08 to UC-20: Profile Update Workflows — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot across profile-related use cases (UC-08 through UC-20), in execution order. Intended for iGot developers integrating or extending these workflows.

---

## Quick Reference — API Usage Per Use Case

| UC | Title | APIs Called |
|---|---|---|
| UC-08 | Profile Name Update | `GET /user/private/v1/read/{user_id}` · `PATCH /user/private/v1/update` |
| UC-09 | Display Name / Username Update | **No API** |
| UC-10 | Educational Qualification Update | **No API** |
| UC-11 | Profile Photo Update | **No API** |
| UC-12 | Cover Photo Update | **No API** |
| UC-13 | Profile Completion Not 100% | `GET /user/private/v1/read/{user_id}` |
| UC-14 | EHRMS ID / External System ID Update | `GET /user/private/v1/read/{user_id}` · `POST /user/v1/search` |
| UC-15 | Mother Tongue Update | `GET /masterData/v1/languages` · ZohoDesk ticket API (conditional) |
| UC-16 | Date of Retirement Blank or Cannot Edit | `POST /private/user/v1/search` · `GET /user/private/v1/read/{user_id}` · `POST /user/v1/search` (conditional) |
| UC-17 | Request to Add Service | `GET /data/v2/system/settings/get/cadreConfig` · ZohoDesk ticket API (conditional) |
| UC-18 | Service History Update | `POST /private/user/v1/search` · `GET /user/private/v1/read/{user_id}` · `POST /user/v1/search` (conditional) |
| UC-19 | Designation Not Found in List | `POST /apis/public/v8/designation/search` · `GET /user/private/v1/read/{user_id}` · `GET /framework/v1/read/{framework_id}` · `POST /user/v1/search` (conditional) · ZohoDesk ticket API (conditional) |
| UC-20 | Leaderboard Not Displayed or Not Updated | **No API** |

---

## UC-08: Profile Name Update

### Execution Flow

```
Path A (How-to Guidance):
  → No API call. Pure informational steps.

Path B (Surname Repeating Twice):
  STEP 1 → GET /user/private/v1/read/{user_id}
               ↓ Fetch firstName, lastName
               ↓ Detect surname duplication

  STEP 2 → Collect correct name from user
           → Confirm: "Change from X to Y?" → User says YES

  STEP 3 → PATCH /user/private/v1/update
               ↓ HTTP 200 → inform user of successful update
               ↓ Non-200 → inform user of failure, offer escalation

Path C (Incorrect Casing — All Caps / All Lowercase):
  → Same as Path B (STEP 1 → STEP 3), but detect casing issue instead of duplication.
```

### Step 1 — Fetch Current Name

**Tool:** `get_certificate_name_info`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.firstName` | Current first name |
| `result.response.lastName` | Current last name |
| `result.response.profileDetails.personalDetails.firstname` | Profile first name (used for duplication check) |
| `result.response.profileDetails.personalDetails.surname` | Profile surname (used for duplication check) |

#### Duplication Detection Logic

- `has_surname_duplication = true` if `lastName` (lowercased) appears as a word within `firstName`
- Also true if last two words of the full name are identical

### Step 3 — Update Profile Name

**Tool:** `update_profile_name`
**Endpoint:** `PATCH /api/user/private/v1/update`

```bash
curl -X PATCH \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/update" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "userId": "{user_id}",
      "firstName": "{new_first_name}",
      "lastName": "{new_last_name}"
    }
  }'
```

#### Success / Failure Handling

| HTTP Status | Outcome |
|---|---|
| 200 | Name updated successfully — inform user |
| Non-200 | Update failed — inform user, offer to escalate |

> **Note:** After a successful update for UC-08, the bot says ONLY "The name has been successfully updated. Please verify the changes in your profile." It does NOT show certificate download steps (that belongs to UC-02 only).

---

## UC-09: Display Name / Username Update

**No API calls.** This is a purely informational response.

The Display Name / Username is system-generated and cannot be manually changed. The chatbot informs the user of this limitation and offers to help with actual name (firstName/lastName) update instead.

---

## UC-10: Educational Qualification Update

**No API calls.** Pure step-by-step guidance.

The chatbot guides the user through the UI path:
`View Profile → Educational Qualification → Plus (+) icon → fill fields → Add`

Key guidance points: if a degree or institute is not in the dropdown, choose "Other" and enter free text. Do NOT suggest ticket creation.

---

## UC-11: Profile Photo Update

**No API calls.** Pure step-by-step guidance.

Two paths:
- **Path A (Normal Upload):** `View Profile → ⋮ menu → Edit Profile → Profile Photo → select → Apply Changes → Save Changes`
- **Path B (Remove and Re-Upload):** Same as Path A but delete existing photo first.

Photo requirements always shared:
- File size: ≤ 1 MB
- Image resolution: ≤ 180 × 180 pixels

---

## UC-12: Cover Photo Update

**No API calls.** Pure step-by-step guidance.

Path: `View Profile → ⋮ menu → Edit Cover Photo → Change Cover Photo → select → Apply Changes`

Cover photo requirements always shared:
- File size: ≤ 150 KB
- Dimensions: 1200 × 291 pixels

---

## UC-13: Profile Completion Not 100%

### Execution Flow

```
STEP 1 → GET /user/private/v1/read/{user_id}
             ↓ Read missing_profile_fields (pre-computed by service layer)
             ↓ List non-empty → show missing fields to user
             ↓ List empty    → inform all mandatory fields are filled
```

### Step 1 — Fetch User Profile

**Tool:** `get_user_details`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

The service layer computes `missing_profile_fields` by checking these mandatory fields:

| Field checked | Source |
|---|---|
| Profile Photo | `result.response.profileDetails.profileImageUrl` |
| Cover Photo | `result.response.profileDetails.coverImageUrl` |
| Username Verification | `result.response.profileDetails.userVerified` |
| About Me | `result.response.profileDetails.personalDetails.about` |
| Designation | `result.response.profileDetails.professionalDetails[0].designation` |
| Group | `result.response.profileDetails.professionalDetails[0].group` |

> **Note:** The chatbot uses ONLY the `missing_profile_fields` list from the tool response. It does NOT inspect individual profile fields or guess which are missing.

---

## UC-14: EHRMS ID / External System ID Update

### Execution Flow

```
STEP 1 → GET /user/private/v1/read/{user_id}
             ↓ Fetch rootOrgId, channel (for MDO org name)

STEP 2 → POST /user/v1/search
             ↓ filters: rootOrgId + role = MDO_ADMIN
             ↓ MDO_ADMIN found    → return admin_name, admin_email
             ↓ MDO_ADMIN NOT found → YP fallback (static allocation file, no further API call)
             ↓ Show MDO contact to user. Inform that EHRMS ID can only be updated by MDO.
```

> This is an informational flow — no profile update is performed. The user is directed to contact their MDO for updating the EHRMS ID.

### Step 1 — Profile Read

**Tool:** `get_mdo_contact_details` (internally calls `fetch_user_profile`)
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.rootOrgId` | Used to look up MDO Admin |
| `result.response.channel` | Displayed as MDO Name |

### Step 2 — MDO Admin Search

**Endpoint:** `POST /api/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "{rootOrgId}",
        "roles": ["MDO_ADMIN"]
      }
    }
  }'
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.content[0].firstName` + `lastName` | MDO Admin name |
| `result.response.content[0].email` | MDO Admin email |

**Fallback:** If no MDO_ADMIN is found, YP (Young Professional) contact is looked up from a static allocation Excel file (`Data/Allocation_28.10.2025.xlsx`) using the user's state and department.

---

## UC-15: Mother Tongue Update

### Execution Flow

```
STEP 1 → GET /masterData/v1/languages
             ↓ Check if user's mother tongue is in the list

             exists = true  → guide user to update via UI steps, no ticket
             exists = false → ask user confirmation to raise ticket
                → User YES → ZohoDesk ticket creation API
                → User NO  → inform, no action taken
```

### Step 1 — Validate Mother Tongue

**Tool:** `validate_mother_tongue`
**Endpoint:** `GET /api/masterData/v1/languages`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/masterData/v1/languages"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `languages[].name` | List of available language names for exact match comparison |

#### Decision

| Condition | Outcome |
|---|---|
| `exists = true` | Show UI update steps; no ticket |
| `exists = false` | Ask user confirmation; raise ZohoDesk ticket on YES |

### Step 2 (Conditional) — Raise Ticket

**Tool:** `raise_mother_tongue_ticket`
**Backend:** ZohoDesk support ticket creation API

Ticket includes:
- User name, email, mobile (fetched from `GET /user/private/v1/read/{user_id}`)
- Mother tongue name to be added

Returns `ticket_number` on success. Share with the user.

---

## UC-16: Date of Retirement Blank or Cannot Edit

### Execution Flow

```
STEP 1 → POST /private/user/v1/search
             ↓ Check additionalProperties.externalSystemId

             has_ehrms_id = true  → Inform user to update Date of Retirement in EHRMS portal (auto-fetchs to iGOT). Stop.
             has_ehrms_id = false → Proceed to STEP 2

STEP 2 → GET /user/private/v1/read/{user_id}      (via get_mdo_contact_details)
         POST /user/v1/search                       (MDO_ADMIN lookup)
             ↓ Return mdo_name, mdo_leader_name, mdo_leader_email
             ↓ Inform user their EHRMS ID is missing; ask them to contact MDO
```

### Step 1 — EHRMS ID Check

**Tool:** `check_ehrms_id_and_retirement_status`
**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "userId": "{user_id}"
      },
      "limit": 1
    }
  }'
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.content[0].additionalProperties.externalSystemId` | Present → `has_ehrms_id = true`; absent/empty → `has_ehrms_id = false` |
| `result.response.content[0].rootOrgId` | Used for MDO Admin lookup in Step 2 |

### Step 2 (Conditional) — MDO Contact Details

Same API calls as **UC-14 Steps 1 & 2** (`GET /user/private/v1/read/{user_id}` + `POST /user/v1/search` for MDO_ADMIN).

Only executed when `has_ehrms_id = false`.

---

## UC-17: Request to Add Service

### Execution Flow

```
STEP 1 → GET /data/v2/system/settings/get/cadreConfig
             ↓ Search civilServiceType.civilServiceTypeList[].serviceList[] for service name

             found = true   → show matching services; user confirms match → show UI update steps
             found = false  → ask for Cadre Controlling Authority → confirm → raise ZohoDesk ticket
             user confirms no match → same as found = false
```

### Step 1 — Search Service in Master

**Tool:** `search_service_in_master`
**Endpoint:** `GET /api/data/v2/system/settings/get/cadreConfig`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/data/v2/system/settings/get/cadreConfig" \
  -H "x-authenticated-user-token: {{SYSTEM_ADMIN_TOKEN}}"
```

> **Note:** This endpoint requires an additional `x-authenticated-user-token` header using a system admin Keycloak token obtained from the OAuth2 password grant.

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.value.civilServiceType.civilServiceTypeList[].serviceList[].name` | Service display name |
| `result.response.value.civilServiceType.civilServiceTypeList[].serviceList[].id` | Service ID |

Matching is done in-memory (case-insensitive partial match on `name`).

#### Decision

| Condition | Outcome |
|---|---|
| `found = true`, user confirms a match | Show UI steps to update service in profile |
| `found = false` OR user confirms no match | Collect Cadre Controlling Authority; raise ZohoDesk ticket on YES |

### Step 2 (Conditional) — Raise Ticket

**Tool:** `raise_service_ticket`
**Backend:** ZohoDesk support ticket creation API

Ticket includes:
- User name, email, mobile (fetched from `GET /user/private/v1/read/{user_id}`)
- Service name (full form)
- Cadre Controlling Authority

Returns `ticket_number` on success. Share with the user.

---

## UC-18: Service History Update

### Execution Flow

```
CASE 1 — User Cannot Edit Service History:

  STEP 1 → POST /private/user/v1/search
               ↓ Fetch organisation (from employmentDetails.departmentName)
               ↓ Fetch designation (from professionalDetails[0].designation)
               ↓ Show to user, ask for confirmation

               User confirms correct  → Inform that service history auto-populates. No further API call.
               User says it's wrong   → Show Transfer Request steps.

  STEP 2 (conditional, if wrong) → GET /user/private/v1/read/{user_id}
                                    POST /user/v1/search  (MDO_ADMIN lookup)
               ↓ Show MDO contact details

CASE 2 — User Wants to Add Previous Employment History:
  → No API call. Pure UI guidance only.
```

### Step 1 — Fetch Org and Designation

**Tool:** `get_user_details` (internally calls `fetch_service_history_org_details`)
**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "userId": "{user_id}"
      },
      "limit": 1
    }
  }'
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.content[0].profileDetails.employmentDetails.departmentName` | Current organisation |
| `result.response.content[0].profileDetails.professionalDetails[0].designation` | Current designation |

### Step 2 (Conditional) — MDO Contact Details

Same API calls as **UC-14 Steps 1 & 2** (`GET /user/private/v1/read/{user_id}` + `POST /user/v1/search` for MDO_ADMIN).

Only executed when user says organisation/designation is incorrect.

---

## UC-19: Designation Not Found in List

### Execution Flow

```
STEP 1 → POST /apis/public/v8/designation/search
             ↓ Search designation by partial name (paginated)

             found = true  → Show list → User confirms match → STEP 2
             found = false → Skip to STEP 3 (raise ticket)
             user confirms no match → STEP 3

STEP 2 → GET /user/private/v1/read/{user_id}
             ↓ Extract rootOrgId

         GET /framework/v1/read/{rootOrgId}_odcs
             ↓ Check if designation is in MDO's org framework

             imported = true  → Show UI update steps; user can update directly
             imported = false → Contact MDO to import; get_mdo_contact_details
             imported = null  → API unavailable; show both options (update steps + MDO contact)

STEP 3 (ticket path) → ZohoDesk ticket creation API
             ↓ Requires: designation name + organization name + user confirmation
```

### Step 1 — Search Designation in Master

**Tool:** `search_designation_in_master`
**Endpoint:** `POST /portal/apis/public/v8/designation/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/apis/public/v8/designation/search" \
  -H "Content-Type: application/json" \
  -d '{
    "pageNumber": 1,
    "pageSize": 100,
    "filterCriteriaMap": {},
    "requestedFields": [],
    "searchString": "{designation_name}"
  }'
```

> **Note:** No `Authorization` header is required — this is a public API.

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.result.data[].designation` | Designation display name |
| `result.result.data[].id` | Designation ID (e.g. `DESG-001021`); passed to framework check |
| `result.result.totalCount` | Total matches |

Matching priority: exact match → partial match (term contained in designation name).

### Step 2 — Check if Imported by MDO

**Tool:** `check_designation_imported_by_mdo`

**Sub-step 2a — Profile Read (for rootOrgId):**
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

| Field path | Purpose |
|---|---|
| `result.response.rootOrgId` | Used to build framework ID: `{rootOrgId}_odcs` |

**Sub-step 2b — ORG Framework Read:**
**Endpoint:** `GET /api/framework/v1/read/{rootOrgId}_odcs`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/framework/v1/read/{rootOrgId}_odcs"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `params.status` | If `"failed"` → framework not configured → `imported = false` |
| `result.framework.categories[].terms[].code` | Match against `designation_id`; found → `imported = true` |

#### Decision Table

| Result | Outcome |
|---|---|
| `imported = true` | Designation is in dropdown — show UI update steps |
| `imported = false` | Not imported by MDO — call `get_mdo_contact_details`; share contact |
| `imported = null` (API error) | Show both options (UI steps + MDO contact as fallback) |

### Step 3 (Conditional) — MDO Contact Details

Same API calls as **UC-14 Steps 1 & 2** (`GET /user/private/v1/read/{user_id}` + `POST /user/v1/search` for MDO_ADMIN).

### Step 4 (Conditional) — Raise Ticket

**Tool:** `raise_designation_ticket`
**Backend:** ZohoDesk support ticket creation API

Ticket includes:
- User name, email, mobile (fetched from `GET /user/private/v1/read/{user_id}`)
- Designation name (full form, no abbreviations)
- Organization name

Returns `ticket_number` on success. Share with the user.

> `ranpratap.ext@deloitte.com` is automatically CC'd on every designation ticket.

---

## UC-20: Leaderboard / Top Karmayogi Dashboard Not Displayed or Not Updated

**No API calls.** Pure guidance use case.

Two paths:
- **Sub-flow A (Not Displayed):** Guide user to `Home Page → Leader Dashboard / Leaderboard → Leader Card / Top Karmayogi Card`
- **Sub-flow B (Not Updated):** Inform user that the Leaderboard updates **once every month on the 1st**. Rankings reflect the previous month's data until the next update.

No tickets are raised for this use case.

---

## Shared API Reference

### Private User Profile Read

Used by: UC-08, UC-13, UC-14, UC-16 (fallback), UC-17 (ticket path), UC-18 (conditional), UC-19

```
GET /api/user/private/v1/read/{user_id}
```

### Private User Search

Used by: UC-16, UC-18 (org/designation fetch)

```
POST /api/private/user/v1/search
Body: { "request": { "filters": { "userId": "{user_id}" }, "limit": 1 } }
```

### MDO Admin Search (User Search by Role)

Used by: UC-14, UC-16 (conditional), UC-18 (conditional), UC-19 (conditional)

```
POST /api/user/v1/search
Body: { "request": { "filters": { "rootOrgId": "{rootOrgId}", "roles": ["MDO_ADMIN"] } } }
```

**Fallback:** If no MDO_ADMIN found, YP contact is looked up from static allocation file `Data/Allocation_28.10.2025.xlsx` by state + department — no further API call.
