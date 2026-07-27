# UC-05: Profile Verification — Designation / Group Verification Request — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

> Implemented in `flows/mode_b_designation_not_verified.yaml` (flow_id `DESIGNATION_GROUP_NOT_VERIFIED`, menu entry "Designation / Group not verified"). The identical node graph and API sequence is also duplicated verbatim in `flows/mode_b_karmayogi_badge_check.yaml` (flow_id `KARMAYOGI_BADGE_CHECK`, menu entry "The Verified Karmayogi Badge is not visible.") — everything below applies to both flows.

---

## Execution Flow

```
STEP 1   → POST /api/private/user/v1/search
                ↓ Fetch user's full private profile — reads wfProfileDesignationRequest, wfProfileGroupRequest
                ↓
           wfProfileDesignationRequest or wfProfileGroupRequest exists (has_pending_request = true)
             → Proceed to Step 2 (target dept MDO_ADMIN lookup)

           Both absent (has_pending_request = false)
             → Guide user with submission steps; no further API calls

           No profile returned → return error; no further API calls

STEP 2   → POST /api/private/user/v1/search    (pending request only)
                ↓ filters: channel = departmentName, role = MDO_ADMIN
                ↓ Returns target dept's MDO_ADMIN contact
                ↓
           MDO_ADMIN found → return admin_name, admin_email
           MDO_ADMIN NOT found → YP fallback (no further API call)
```

> **Note:** Step 2 runs only when `wfProfileDesignationRequest` or `wfProfileGroupRequest` is present (pending request path). When neither exists, the chatbot skips Step 2 entirely and guides the user directly with submission steps — no admin lookup API call is made.

---

## Step 1 — Private Profile Fetch (includes `wfProfileDesignationRequest`, `wfProfileGroupRequest`)

> Fetches the user's private profile to determine whether a designation and/or group verification request is already pending. These fields are not available in the public read API.

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

### Response Fields Used

| Field | Used For |
|---|---|
| `result.response.content[0].wfProfileDesignationRequest.wfId` | Used to confirm a pending designation request (`has_pending_request = true`) |
| `result.response.content[0].wfProfileDesignationRequest.departmentName` | Target department for the pending designation request |
| `result.response.content[0].wfProfileGroupRequest.wfId` | Used to confirm a pending group request (`has_pending_request = true`) |
| `result.response.content[0].wfProfileGroupRequest.departmentName` | Target department for the pending group request |
| `result.response.content[0].rootOrgId` | Available in profile; not used in further API calls |
| `result.response.content[0].profileDetails.professionalDetails[0].designation` | User's current designation |
| `result.response.content[0].profileDetails.professionalDetails[0].group` | User's current group |
| `result.response.content[0].profileDetails.professionalDetails[0].name` | User's current department/org name |
| `result.response.content[0].profileDetails.profileDesignationStatus` | Raw designation status (PENDING / VERIFIED / NOT_VERIFIED) |
| `result.response.content[0].profileDetails.profileGroupStatus` | Raw group status (PENDING / VERIFIED / NOT_VERIFIED) |
| `result.response.content[0].profileDetails.profileStatus` | Overall profile verification status (PENDING / VERIFIED / NOT_VERIFIED) |
| `result.response.content[0].channel` | Fallback org/department name if professionalDetails is absent |

### Decision — Is the profile already verified? (SOP §1.1)

| Condition | Outcome |
|---|---|
| `profileStatus == VERIFIED` (or both `profileDesignationStatus == VERIFIED` and `profileGroupStatus == VERIFIED`), designation/group/department details are present, and neither `wfProfileDesignationRequest` nor `wfProfileGroupRequest` is pending | Profile already verified — show green-tick confirmation; close conversation |
| Otherwise | Not (fully) verified — continue to Decision After Step 1 below |

### Decision After Step 1

| Condition | Outcome |
|---|---|
| `wfProfileDesignationRequest.wfId`/`departmentName` present OR `wfProfileGroupRequest.wfId`/`departmentName` present | `has_pending_request = true`; extract `departmentName` (designation request takes priority, falling back to the group request's department); proceed to Step 2 |
| Both `wfProfileDesignationRequest` and `wfProfileGroupRequest` are absent or empty | `has_pending_request = false`; guide user with submission steps; no further API calls |
| No content returned / API failure | Return error; no further API calls |

---

## Step 2 — MDO Admin Lookup for Target Department (Pending Request path)

> Fetches the MDO_ADMIN contact details for the target department so the user knows who will approve their pending designation/group request.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "query": "",
      "filters": {
        "channel": "{department_name}",
        "organisations.roles": ["MDO_ADMIN"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

> `{department_name}` is `wfProfileDesignationRequest.departmentName`, falling back to `wfProfileGroupRequest.departmentName` from Step 1.

### Response Fields Used

| Field | Used For |
|---|---|
| `result.response.count` | Determines if any MDO_ADMIN was found |
| `result.response.content[0].profileDetails.personalDetails.firstname` | Admin's first name |
| `result.response.content[0].profileDetails.personalDetails.surname` | Admin's last name |
| `result.response.content[0].profileDetails.personalDetails.primaryEmail` | Admin's email |
| `result.response.content[0].firstName` | Fallback first name if personalDetails absent |
| `result.response.content[0].email` | Fallback email if personalDetails absent |

### Decision After Step 2

| Condition | Outcome |
|---|---|
| `count > 0` and `content` is present | `org_admin_present = true`; return `admin_name`, `admin_email` to the user |
| `count = 0` or `content` empty | `org_admin_present = false`; fall back to YP allocation file (no further API call) |
| API returns 401 / 403 / 404 | Treat as not found; fall back to YP allocation file |

---

## YP Fallback (No Admin Available)

> When no Org Admin/MDO_ADMIN is found, the chatbot uses a static YP allocation file (loaded in-memory) to look up the responsible Young Professional (YP) by org channel. No additional API call is made.

| Condition | Response |
|---|---|
| YP details found in allocation file | Return `yp_name`, `yp_email` to the user |
| YP details not found | Offer to raise a support ticket; return generic message otherwise |

---

## API Dependency Table

| Step | Endpoint | Method | Auth Required | Purpose | Key Fields |
|---|---|---|---|---|---|
| 1 | `/api/private/user/v1/search` | POST | Yes | Fetch private profile; read `wfProfileDesignationRequest`, `wfProfileGroupRequest`, `rootOrgId`, `designation`, `group` | `wfProfileDesignationRequest`, `wfProfileGroupRequest`, `rootOrgId`, `professionalDetails` |
| 2 (pending request only) | `/api/private/user/v1/search` | POST | Yes | Fetch MDO_ADMIN contact for target department | `firstname`, `surname`, `primaryEmail` |
| — (fallback) | YP Allocation File | — | — | Static YP lookup by org channel when no MDO_ADMIN found | `yp_name`, `yp_email` |
