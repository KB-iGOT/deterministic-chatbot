# UC-08 to UC-20: Profile Update Workflows — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot across profile-related use cases (UC-08 through UC-20), in execution order. Intended for iGot developers integrating or extending these workflows.
>
> Source of truth: `flows/mode_b_profile_completion.yaml` (UC-08 – UC-19) and `flows/mode_b_leaderboard.yaml` (UC-20), plus shared fragments `flows/_shared/_zoho_ticket.yaml` and `flows/_shared/_yp_lookup.yaml`. Last verified against these files 2026-07-24.
>
> **Note on shared fragments:** `flows/_shared/_karmayogi_user.yaml` and `flows/_shared/_mdo_admin_lookup.yaml` exist in the repo and define reusable profile-read / MDO-admin-search nodes, but `mode_b_profile_completion.yaml` does **not** import either of them — it inlines its own copy of the profile-read call and the MDO-admin-search call separately for each use case (different node IDs per UC, e.g. `fetch_root_org_for_email`, `fetch_root_org_for_ehrms`, `fetch_user_for_svc_history`, `fetch_user_for_desig`). The request/response shapes are the same as the shared fragments, but there is no single shared node actually wired into this flow.

---

## Quick Reference — API Usage Per Use Case

| UC | Title | APIs Called |
|---|---|---|
| UC-08 | Profile Name Update | `GET /user/private/v1/read/{user_id}` |
| UC-09 | Display Name / Username Update | **No API** |
| UC-10 | Educational Qualification Update | **No API** |
| UC-11 | Profile Photo Update | **No API** |
| UC-12 | Cover Photo Update | **No API** |
| UC-13 | Profile Completion Not 100% | `GET /user/private/v1/read/{user_id}` |
| UC-14 | EHRMS ID / External System ID Update | `GET /user/private/v1/read/{user_id}` · `POST /private/user/v1/search` (MDO_ADMIN lookup, conditional) |
| UC-15 | Mother Tongue Update | `GET /masterData/v1/languages` · ZohoDesk ticket API (conditional) |
| UC-16 | Date of Retirement Blank or Cannot Edit | `GET /user/private/v1/read/{user_id}` (EHRMS check) · `GET /user/private/v1/read/{user_id}` + `POST /private/user/v1/search` (conditional MDO lookup) |
| UC-17 | Request to Add Service | `GET /data/v2/system/settings/get/cadreConfig` · `GET /user/private/v1/read/{user_id}` (conditional profile-match check) · ZohoDesk ticket API (conditional) |
| UC-18 | Service History Update | `GET /user/private/v1/read/{user_id}` · `POST /private/user/v1/search` (conditional MDO lookup) |
| UC-19 | Designation Not Found in List | `POST /apis/public/v8/designation/search` · `GET /user/private/v1/read/{user_id}` · `GET /framework/v1/read/{rootOrgId}_odcs` · `POST /private/user/v1/search` (conditional) · ZohoDesk ticket API (conditional) |
| UC-20 | Leaderboard Not Displayed or Not Updated | **No API** |

---

## UC-08: Profile Name Update

### Execution Flow

```
STEP 1 → GET /user/private/v1/read/{user_id}
             ↓ Fetch current firstName, lastName, personalDetails.surname
             ↓ Show current name to user

STEP 2 → Show generic UI guidance to update the name (View Profile → Name → Edit → Save)
```

> **Current implementation note:** the flow does **not** detect surname duplication or casing issues, and does **not** call a PATCH/update endpoint on the user's behalf. It only fetches and displays the current name, then walks the user through updating it themselves via the UI. There is no bot-driven name update or success/failure handling in this flow.

### Step 1 — Fetch Current Name

**Node:** `fetch_name_for_guide`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `response.firstName` | Current first name |
| `response.lastName` | Current last name |
| `response.profileDetails.personalDetails.surname` | Current surname (preferred over `lastName` when present) |

On both success **and** error, the flow proceeds to the same guidance step (`name_update_steps`) — an API failure does not block or escalate this use case.

### Step 2 — Guidance

**Node:** `name_update_steps` (resolution)

Steps shown: `View Profile → Name section → Edit Profile → Update the Name → Save / Submit`.

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
- File size: ≤ 200 KB
- Dimensions: 1200 × 300 pixels

---

## UC-13: Profile Completion Not 100%

### Execution Flow

```
STEP 1 → GET /user/private/v1/read/{user_id}
             ↓ Fetch completion_pct + individual field values (avatar, banner,
               username verification, about-me, designation status, group status)

             completion_pct >= 100  → "already 100%" message
             completion_pct < 100   → chatbot evaluates each field itself and
                                       lists which mandatory fields are missing
```

### Step 1 — Fetch User Profile

**Node:** `fetch_user_profile`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

The chatbot itself evaluates each of these fields (there is **no** pre-computed `missing_profile_fields` list from the service layer — that was true of an earlier version but is no longer how this flow works):

| Field checked | Source | Considered "missing" when |
|---|---|---|
| Profile Photo | `response.profileDetails.profileImageUrl` | falsy |
| Cover Photo | `response.profileDetails.profileBannerUrl` | falsy |
| Username Verification | `response.profileDetails.verifiedKarmayogi` | `"False"` or falsy |
| About Me | `response.profileDetails.employmentDetails.aboutme` | falsy |
| Designation | `response.profileDetails.profileDesignationStatus` | not `"VERIFIED"` |
| Group | `response.profileDetails.profileGroupStatus` | not `"VERIFIED"` |

`response.profileCompletionPercentage` is used directly to short-circuit to "already complete" when `>= 100.0`.

> If the username verification tick is missing for more than 2 working days, the user can request MDO contact details — see `lookup_mdo_for_tick` (same MDO Admin Search pattern as UC-14, endpoint corrected below).

---

## UC-14: EHRMS ID / External System ID Update

### Execution Flow

```
STEP 0 → Informational message: individual users cannot update EHRMS ID; only MDO can.
         Ask: "Would you like MDO contact details?"

STEP 1 (if yes) → GET /user/private/v1/read/{user_id}
             ↓ Fetch rootOrgId, channel (MDO org name), email, mobile

STEP 2 → POST /private/user/v1/search
             ↓ filters: rootOrgId + organisations.roles = MDO_ADMIN, status: 1
             ↓ MDO_ADMIN found     → show admin_name, admin_email
             ↓ MDO_ADMIN NOT found → YP fallback (static allocation file, no further API call)
             ↓ API error on either call → straight to YP fallback (no error message shown)
```

> This is an informational flow — no profile update is performed. The user is directed to contact their MDO for updating the EHRMS ID.

### Step 1 — Profile Read

**Node:** `fetch_root_org_for_ehrms`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `response.rootOrgId` | Used to look up MDO Admin |
| `response.channel` | Displayed as MDO Name / used as YP lookup key |
| `response.profileDetails.personalDetails.primaryEmail` | Shown back to user as their registered email |
| `response.profileDetails.personalDetails.mobile` | Shown back to user as their registered mobile |

### Step 2 — MDO Admin Search

**Node:** `lookup_mdo_for_ehrms`
**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "{rootOrgId}",
        "organisations.roles": ["MDO_ADMIN"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `response.content[0].profileDetails.personalDetails.firstname` | MDO Admin name |
| `response.content[0].profileDetails.personalDetails.primaryEmail` | MDO Admin email |

**Fallback:** If no MDO_ADMIN is found (or the search call errors), the YP (Young Professional) contact is looked up via the `_yp_lookup` fragment from the in-memory index built off `data/Allocation_28.10.2025.xlsx`, keyed on `collected.org_channel` (the org/department name from Step 1) — not "state and department".

---

## UC-15: Mother Tongue Update

### Execution Flow

```
STEP 1 → GET /masterData/v1/languages
             ↓ Present full language list (+ an "Others" option appended) as a picker

             User selects a real language → guide user to update via UI steps, no ticket
             User selects "Others"        → collect free-text language name → confirm → ZohoDesk ticket
             API error / empty list       → confirm → ZohoDesk ticket directly (no picker shown)
```

> Unlike an earlier version of this flow, the chatbot does **not** compare the user's existing mother tongue against the master list. It presents the live list (with an "Others" fallback) and lets the user pick directly.

### Step 1 — Fetch Language List

**Node:** `mother_tongue_fetch_languages`
**Endpoint:** `GET /api/masterData/v1/languages`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/masterData/v1/languages"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `languages[]` | List of available languages for the picker (an "Others" entry is appended in-memory) |

#### Decision

| Condition | Outcome |
|---|---|
| User picks a language from the list (not "Others") | Show UI update steps; no ticket |
| User picks "Others" | Collect the language name as free text; raise ZohoDesk ticket on confirmation |
| API error or empty list | Skip the picker; raise ZohoDesk ticket on confirmation |

### Step 2 (Conditional) — Raise Ticket

**Node:** `raise_mother_tongue_ticket` (API-error/empty-list path) or `raise_mother_tongue_ticket_others` ("Others" path)
**Backend:** ZohoDesk support ticket creation API (`POST /tickets`, via the shared `_zoho_ticket` fragment)

Ticket includes: requester name/email/mobile from the session's already-collected profile context (`collected.first_name`, `last_name`, `email`, `mobile` — no fresh profile-read call is made specifically for the ticket) plus the mother tongue name to be added.

Returns `ticket_number` (`ctx.collected.ticket_id`) on success. Shared with the user.

---

## UC-16: Date of Retirement Blank or Cannot Edit

### Execution Flow

```
STEP 1 → GET /user/private/v1/read/{user_id}?fields=profileDetails
             ↓ Check profileDetails.additionalProperties.externalSystemId

             has_ehrms_id = true  → Inform user to update Date of Retirement in EHRMS portal (auto-fetches to iGOT within 24h). Stop.
             has_ehrms_id = false → Show guidance, then proceed to STEP 2

STEP 2 → GET /user/private/v1/read/{user_id}   (fetch rootOrgId, channel, email, mobile)
         POST /private/user/v1/search           (MDO_ADMIN lookup)
             ↓ Show MDO contact details, or YP fallback if not found
```

### Step 1 — EHRMS ID Check

**Node:** `fetch_user_profile_retirement`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}` (with `params: {fields: profileDetails}`)

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}?fields=profileDetails"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `response.profileDetails.additionalProperties.externalSystemId` | Present → `has_ehrms_id = true`; absent/empty → `has_ehrms_id = false` |

On API error, the flow goes straight to the LLM fallback (`transfer_to_llm`), not to the MDO-lookup path.

> **Note:** this step does **not** call `POST /private/user/v1/search` and does **not** fetch `rootOrgId` here — `rootOrgId` is fetched separately in Step 2 below.

### Step 2 (Conditional) — MDO Contact Details

**Nodes:** `fetch_root_org_for_retirement` (`GET /api/user/private/v1/read/{user_id}` → `rootOrgId`, `channel`, `primaryEmail`, `mobile`) then `lookup_mdo_for_retirement` (`POST /api/private/user/v1/search`, same MDO_ADMIN filter shape as UC-14 Step 2).

Only executed when `has_ehrms_id = false`. Falls back to YP contact lookup if no MDO_ADMIN is found or either call errors.

---

## UC-17: Request to Add Service

### Execution Flow

```
STEP 1 → GET /data/v2/system/settings/get/cadreConfig
             ↓ Flatten civilServiceType.civilServiceTypeList[].serviceList[] into a picker list
             ↓ User searches/selects a service from the picker

             User confirms selected service is theirs →
STEP 2 →        GET /user/private/v1/read/{user_id}
                   ↓ Compare profileDetails.cadreDetails.civilServiceName to the selection
                   ↓ match     → "already linked in profile" message + UI steps
                   ↓ no match  → UI steps to update service in profile
             User says selection is not their service, or picker is empty/errors →
                 Collect exact service name + Cadre Controlling Authority → confirm → raise ZohoDesk ticket
```

### Step 1 — Fetch Service List

**Node:** `service_fetch_cadre_config`
**Endpoint:** `GET /api/data/v2/system/settings/get/cadreConfig`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/data/v2/system/settings/get/cadreConfig" \
  -H "x-authenticated-user-token: {SESSION_TOKEN}"
```

> **Note:** the current flow forwards the **requesting user's own session token** (`x-authenticated-user-token: __SESSION_TOKEN__`) — it does **not** use a separately-obtained system admin Keycloak token via OAuth2 password grant.

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `response.value.civilServiceType.civilServiceTypeList[].serviceList[]` | Flattened (via `flatten_cadre_services` transform) into `collected._service_list`, used as the picker's options (matched/searched by `name`) |

The result is cached for 3600 seconds. On error or an empty result, the flow skips straight to the "service not found" ticket-collection path.

#### Step 2 — Profile Match Check (after user confirms a selection)

**Node:** `fetch_user_for_service_check`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

| Field path | Purpose |
|---|---|
| `response.profileDetails.cadreDetails.civilServiceName` | Compared against the user's selected service name |

If they match, the user is told the service is already mapped (still shown the UI update steps); otherwise the flow shows UI steps to set the service. On API error, it defaults to showing the UI update steps.

### Step 3 (Conditional) — Raise Ticket

**Node:** `raise_service_ticket`
**Backend:** ZohoDesk support ticket creation API

Ticket includes:
- Requester name/email/mobile from the session's already-collected context (no fresh profile-read call specifically for the ticket)
- Service name (full form): `collected.service_requested`
- Cadre Controlling Authority: `collected.service_cadre_authority`

Returns `ticket_number` on success. Shared with the user.

---

## UC-18: Service History Update

### Execution Flow

```
CASE 1 — User Cannot Edit Service History:

  STEP 1 → GET /user/private/v1/read/{user_id}
               ↓ Fetch organisation (from channel), designation (professionalDetails[0].designation),
                 and rootOrgId (reused in Step 2, no second GET needed)
               ↓ Show to user, ask for confirmation

               User confirms correct  → Inform that service history auto-populates. No further API call.
               User says it's wrong   → Show Transfer Request UI steps, then STEP 2

  STEP 2 (conditional, if wrong) → POST /private/user/v1/search  (MDO_ADMIN lookup, using rootOrgId already fetched)
               ↓ Show MDO contact details, or YP fallback if not found

CASE 2 — User Wants to Add Previous Employment History:
  → No API call. Pure UI guidance only.
```

### Step 1 — Fetch Org and Designation

**Node:** `fetch_user_for_svc_history`
**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `response.channel` | Current organisation display name (used both for display and as the YP-lookup key) |
| `response.rootOrgId` | Used directly for the MDO Admin lookup in Step 2 — no second profile-read call is made |
| `response.profileDetails.professionalDetails[0].designation` | Current designation |

> **Note:** organisation comes from `channel`, not `employmentDetails.departmentName`.

### Step 2 (Conditional) — MDO Contact Details

**Node:** `lookup_mdo_for_svc_history` — `POST /api/private/user/v1/search`, same MDO_ADMIN filter shape as UC-14 Step 2, reusing `rootOrgId` already captured in Step 1. Only executed when the user says organisation/designation is incorrect. Falls back to YP contact if no MDO_ADMIN is found or the call errors.

---

## UC-19: Designation Not Found in List

### Execution Flow

```
STEP 1 → POST /apis/public/v8/designation/search
             ↓ Fetch the full (paginated, cached 1h) active designation list
             ↓ User searches/selects from the picker

             User confirms a selection → STEP 2
             Picker empty/errors, or user says none match → STEP 3 (ticket path)

STEP 2 → GET /user/private/v1/read/{user_id}
             ↓ Extract rootOrgId

         GET /framework/v1/read/{rootOrgId}_odcs
             ↓ Check if the selected designation's ID is among the MDO's imported framework term codes

             found in framework  → Show UI update steps
             not found            → POST /private/user/v1/search (MDO_ADMIN lookup, reusing rootOrgId
                                     from this same step) → show MDO contact / YP fallback
             framework read fails → API error message; escalate to LLM fallback

STEP 3 (ticket path) → Collect designation name + organisation name → confirm → ZohoDesk ticket creation API
```

### Step 1 — Fetch Designation List

**Node:** `designation_fetch_list`
**Endpoint:** `POST /apis/public/v8/designation/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/apis/public/v8/designation/search" \
  -H "Content-Type: application/json" \
  -d '{
    "pageNumber": 1,
    "pageSize": 100,
    "filterCriteriaMap": {"status": "Active"},
    "requestedFields": ["id", "designation"]
  }'
```

> **Note:** No `Authorization` header is required — this is a public API. Unlike an earlier version of this flow, there is no per-query `searchString` parameter — the flow paginates through and caches the entire active designation list, and the user searches/selects within it client-side via the picker.

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.data[].designation` | Designation display name (picker label) |
| `result.data[].id` | Designation ID (e.g. `DESG-001021`); passed to the framework check in Step 2 |
| `result.totalCount` | Used to drive pagination while building the cached list |

### Step 2 — Check if Imported by MDO

**Sub-step 2a — Profile Read (for rootOrgId):**
**Node:** `fetch_user_for_desig` — **Endpoint:** `GET /api/user/private/v1/read/{user_id}`

| Field path | Purpose |
|---|---|
| `response.rootOrgId` | Used to build framework ID: `{rootOrgId}_odcs`, and reused directly for the MDO Admin lookup if needed — no second profile-read call is made |

**Sub-step 2b — ORG Framework Read:**
**Node:** `check_mdo_import` — **Endpoint:** `GET /api/framework/v1/read/{rootOrgId}_odcs`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/framework/v1/read/{rootOrgId}_odcs"
```

#### Response Fields Used

| Field path | Purpose |
|---|---|
| `framework.categories[].terms[].code` | Collected as `framework_term_codes`; the selected designation ID is checked for membership in this list |

#### Decision Table

| Result | Outcome |
|---|---|
| Selected designation ID found in `framework_term_codes` | Designation is in dropdown — show UI update steps |
| Not found | Not imported by MDO — `POST /private/user/v1/search` (MDO_ADMIN lookup) and share contact, or YP fallback |
| Framework read errors | Show a "couldn't fetch data" message and escalate to the LLM fallback (no automatic "both options" display) |

### Step 3 (Conditional) — MDO Contact Details

**Node:** `lookup_mdo_for_desig` — `POST /api/private/user/v1/search`, same MDO_ADMIN filter shape as UC-14 Step 2, reusing `rootOrgId` already fetched in Step 2a.

### Step 4 (Conditional) — Raise Ticket

**Node:** `raise_designation_ticket`
**Backend:** ZohoDesk support ticket creation API

Ticket includes:
- Requester name/email/mobile from the session's already-collected context (no fresh profile-read call specifically for the ticket)
- Designation name (full form, no abbreviations): `collected.designation_requested`
- Organisation name: `collected.designation_org_name`

Returns `ticket_number` on success. Shared with the user.

> The flow's `llm_directives` instruct the ticket-raising LLM step to **add "Randhir" in CC** ("Randhir verifies new designations before they are added; adding him in CC expedites the process"). No CC email address is hardcoded in the flow — the previously-documented `ranpratap.ext@deloitte.com` does not appear anywhere in the flow files and should not be assumed current.

---

## UC-20: Leaderboard / Top Karmayogi Dashboard Not Displayed or Not Updated

**No API calls.** Pure guidance use case (`flows/mode_b_leaderboard.yaml`).

Two paths:
- **Sub-flow A (Not Displayed):** Guide user to `Home Page → Leader Dashboard / Leaderboard → Leader Card / Top Karmayogi Card`
- **Sub-flow B (Not Updated):** Inform user that the Leaderboard updates **once every month on the 1st**. Rankings reflect the previous month's data until the next update.

No tickets are raised for this use case.

---

## Shared API Reference

### Private User Profile Read

Used by: UC-08, UC-13, UC-14, UC-16, UC-17 (conditional), UC-18, UC-19

```
GET /api/user/private/v1/read/{user_id}
```

Response envelope is unwrapped to `response.*` (i.e. the top-level `result` wrapper is stripped by the integration layer; JSONPaths in this doc are written relative to that, e.g. `response.rootOrgId`).

### MDO Admin Search (User Search by Role)

Used by: UC-14, UC-16 (conditional), UC-18 (conditional), UC-19 (conditional). (UC-17 does not use this endpoint — see its section above.)

```
POST /api/private/user/v1/search
Body:
{
  "request": {
    "filters": {
      "rootOrgId": "{rootOrgId}",
      "organisations.roles": ["MDO_ADMIN"],
      "status": 1
    },
    "limit": 1
  }
}
```

Response fields used: `response.content[0].profileDetails.personalDetails.firstname` (admin name), `response.content[0].profileDetails.personalDetails.primaryEmail` (admin email). The per-UC nodes in `mode_b_profile_completion.yaml` branch on whether either of these is truthy (they do not read a `response.count` field).

The shared fragment `flows/_shared/_mdo_admin_lookup.yaml` implements the same endpoint/body shape and additionally maps `response.count` → `collected.mdo_admin_count`, but that fragment is not imported by this flow.

**Fallback:** If no MDO_ADMIN found (or the call errors), YP contact is looked up via `flows/_shared/_yp_lookup.yaml` from the in-memory index built off `data/Allocation_28.10.2025.xlsx`, keyed on the org/department name (`collected.org_channel`) — not state + department.

### ZohoDesk Ticket Creation

Used by: UC-15 (conditional), UC-17 (conditional), UC-19 (conditional), plus the general LLM fallback

```
POST /tickets   (via flows/_shared/_zoho_ticket.yaml, imported with cf_category: profile, cf_sub_category: profile_update, cf_flow_id: PROFILE_COMPLETION)
```

Requester name/email/mobile come from `collected.first_name` / `last_name` / `email` / `mobile`, which are already present in session context by the time these tickets are raised — none of UC-15/17/19's ticket paths make a fresh `GET /user/private/v1/read/{user_id}` call solely to populate the ticket.
