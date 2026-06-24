# UC-01: Access Revoked — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → POST /api/private/user/v1/search
                ↓ Fetch user profile + transfer request status
                ↓
           user NOT found → stop; show support email

STEP 2   → POST /api/org/v1/search             (only if wfTransferRequest exists)
                ↓ Resolve org name from rootOrgId (fallback if orgName missing)
                ↓ Present pending transfer org to user for confirmation

STEP 3   → POST /api/private/user/v1/search    (only if user confirms correct org)
                ↓ Fetch MDO Admin by rootOrgId + role filter
                ↓
           MDO Admin found → share contact details
           MDO Admin NOT found → fallback to YP/SPOC lookup

STEP 4   → POST /api/org/hierarchy/ministry/search  OR
           POST /api/org/hierarchy/state/search      (Path B — org not in dropdown)
                ↓ Fetch parent-level list (ministries or states)

STEP 5   → POST /api/org/hierarchy/search            (Path B — department/org drill-down)
                ↓ Fetch departments and organizations under selected parent
```

> **Note:** Steps 1–2 always run when a transfer request exists (Path A). Steps 4–5 run only for Path B when the user cannot find their organisation in the dropdown.

---

## Step 1 — User Profile & Transfer Request Fetch

> Confirms the user exists and checks whether a transfer request has already been raised.

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
| `response.content[0].wfTransferRequest` | `collected.wf_transfer_request` | Full transfer object for pending-request detection |
| `response.content[0].wfTransferRequest.wfId` | `collected.wf_transfer_id` | Transfer request ID; presence triggers Path A |
| `response.content[0].wfTransferRequest.departmentName` | `collected.transfer_dept_name` | Target dept name from transfer request |
| `response.content[0].wfTransferRequest.rootOrgId` | `collected.transfer_root_org_id` | Target org ID; passed to Steps 2 & 3 |
| `response.content[0].wfTransferRequest.orgName` | `collected.transfer_org_name` | Target org name (may be null; Step 2 resolves it) |
| `response.content[0].organisations[0].orgName` | `collected.profile_org_name` | Current org name (fallback display) |
| `response.content[0].organisations[0].organisationId` | `collected.profile_root_org_id` | Current org ID (fallback) |
| `response.content[0].channel` | `collected.user_channel` / `collected.org_channel` | Used for YP/SPOC lookup key |
| `response.content[0].rootOrgId` | `collected.root_org_id` | User's root org |
| `response.content[0].profileStatus` | `collected.profile_status` | Profile status |

### Decision After Step 1

| Condition | Outcome |
|---|---|
| `user_found_count == 0` | User not found; show support email; no further API calls |
| `wf_transfer_id` is present and non-empty | `matched = true`; proceed to Step 2 (Path A) |
| `wf_transfer_id` is absent or empty | No transfer request raised; guide user to raise one (Path B) |

---

## Step 2 — Organisation Details Fetch (Path A only)

> Resolves the organisation name from `rootOrgId` in case `orgName` is missing from the transfer request object.

**Endpoint:** `POST /api/org/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/org/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "{{transfer_root_org_id}}"
      },
      "limit": 1
    }
  }'
```

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.content[0].orgName` | `collected.fetched_org_name` | Resolved org name for display if `transfer_org_name` is null |

### Decision After Step 2

| Condition | Outcome |
|---|---|
| Org name resolved | Display pending transfer with resolved name; ask user to confirm |
| API failure / org not found | Proceed to transfer confirmation using available name fields (`transfer_org_name` → `transfer_dept_name` → `profile_org_name`) |

> **Org name resolution priority:** `transfer_org_name` → `transfer_dept_name` → `fetched_org_name` → `profile_org_name` → `"organization"` (fallback literal)

---

## Step 3 — MDO Admin Fetch (Path A — after org confirmed)

> Identifies the MDO Admin for the target organisation so the user can contact them to approve the transfer request.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "{{transfer_root_org_id}}",
        "organisations.roles": ["MDO_ADMIN"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

> `rootOrgId` is always the target org from the transfer request (`transfer_root_org_id`). Never fall back to the current user's org for this call.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.count` | `collected.mdo_admin_count` | Determine if an MDO Admin exists |
| `response.content[0].profileDetails.personalDetails.firstname` | `collected.mdo_admin_name` | MDO Admin display name |
| `response.content[0].profileDetails.personalDetails.primaryEmail` | `collected.mdo_admin_email` | MDO Admin contact email |

### Decision After Step 3

| Condition | Outcome |
|---|---|
| `mdo_admin_count > 0` | Display MDO Admin name and email; resolution complete |
| `mdo_admin_count == 0` | No MDO Admin found; fall back to YP/SPOC lookup |
| API failure | Fall back to YP/SPOC lookup |

---

## Step 4 — Parent Org List Fetch (Path B — org not in dropdown)

> Fetches the top-level list of Central Ministries or State Governments so the user can navigate to their organisation.

### 4a — Central Ministries

**Endpoint:** `POST /api/org/hierarchy/ministry/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/org/hierarchy/ministry/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {}
  }'
```

### 4b — State Governments

**Endpoint:** `POST /api/org/hierarchy/state/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/org/hierarchy/state/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {}
  }'
```

### Response Fields Used (both endpoints)

| Field | Mapped To | Used For |
|---|---|---|
| `response.content[]` | `collected.parent_list` | Populate ministry/state picker dropdown |
| `response.content[].id` | Option `id` field | Selected parent ID; passed as `levelZeroOrgId` in Step 5 |
| `response.content[].channel` | Option `label` field | Display name in dropdown |

### Decision After Step 4

| Condition | Outcome |
|---|---|
| `parent_list` is populated | Show searchable dropdown picker to user |
| `parent_list` is empty or API failure | Fall back to YP/SPOC lookup |

> Cache TTL: **3600 seconds** for both endpoints.

---

## Step 5 — Department / Organisation Drill-Down (Path B)

> Fetches departments and leaf organisations under the selected parent using a shared hierarchy search endpoint.

**Endpoint:** `POST /api/org/hierarchy/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/org/hierarchy/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "status": 1,
        "levelZeroOrgId": "{{selected_parent_id}}"
      },
      "query": "",
      "limit": 50,
      "offset": 0,
      "fields": [
        "identifier", "orgName", "description",
        "orgHierarchyFrameworkId", "orgHierarchyFrameworkStatus",
        "sbOrgType", "sbOrgSubType", "channel"
      ]
    }
  }'
```

> For State sub-calls, add `"sbOrgType": "state"` to `filters`.

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `response.content[]` | `collected.filtered_orgs` | Populate org/department picker |
| `response.content[].identifier` | Option `id` field | Selected org ID |
| `response.content[].orgName` | Option `label` field / `collected.dropdown_org_name` | Display name; shared with user on resolution |

> **`append_others_org` transform:** an "Others" option is appended to `filtered_orgs` by the `append_others_org` transform. If the user selects "Others", the flow falls back to YP/SPOC lookup.

### Decision After Step 5

| Condition | Outcome |
|---|---|
| Orgs returned (more than just "Others") | Show searchable org picker; user selects and confirms |
| Only "Others" or empty list | Fall back to YP/SPOC lookup |
| User selects "Others" | Fall back to YP/SPOC lookup |
| User confirms org selection | Share exact org name for use in transfer request; resolution complete |

> Cache TTL: **3600 seconds**.

---

## YP/SPOC Fallback Lookup

> When no MDO Admin is found (Path A) or the org cannot be located in the hierarchy (Path B), the flow looks up the YP/SPOC contact via an internal data service (not a Karmayogi REST API).

**Service:** `yp_lookup` (internal data lookup service)

### Lookup Key Priority

| Priority | Key Used | Trigger |
|---|---|---|
| 1st | `org_channel` (user's department channel) | Path A MDO not found; Path B dept-level |
| 2nd | `selected_parent_name` (ministry/state) | Fallback when dept-level lookup fails |

### Response Fields Used

| Field | Mapped To | Used For |
|---|---|---|
| `name` | `collected.yp_name` | YP/SPOC display name |
| `email` | `collected.yp_email` | YP/SPOC contact email |
| `mobile` | `collected.yp_mobile` | YP/SPOC contact mobile |
| `cc_email` | `collected.yp_cc_email` | CC email for correspondence |

### Decision After YP/SPOC Lookup

| Condition | Outcome |
|---|---|
| YP/SPOC found | Display contact details; resolution complete |
| YP/SPOC not found | Auto-raise a Zoho support ticket; notify user |

---

## API Dependency Table

| Step | Endpoint | Method | Auth Required | Caching | Purpose | Key Fields |
|---|---|---|---|---|---|---|
| 1 | `/api/private/user/v1/search` | POST | Yes | No | Fetch user profile + transfer request | `wfTransferRequest`, `wfId`, `rootOrgId` |
| 2 | `/api/org/v1/search` | POST | Yes | No | Resolve org name from `rootOrgId` | `orgName` |
| 3 | `/api/private/user/v1/search` | POST | Yes | No | Find MDO Admin for target org | `mdo_admin_name`, `mdo_admin_email` |
| 4a | `/api/org/hierarchy/ministry/search` | POST | Yes | 3600s | Fetch Central Ministry list | `id`, `channel` |
| 4b | `/api/org/hierarchy/state/search` | POST | Yes | 3600s | Fetch State Government list | `id`, `channel` |
| 5 | `/api/org/hierarchy/search` | POST | Yes | 3600s | Fetch departments/orgs under selected parent | `identifier`, `orgName` |

---

