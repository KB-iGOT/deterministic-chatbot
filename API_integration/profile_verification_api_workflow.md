# UC-05: Profile Verification — Designation / Group Transfer Request — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → POST /private/user/v1/search
                ↓ Fetch user's full private profile — reads wfTransferRequest
                ↓
           wfTransferRequest exists (has_pending_request = true)
             → Proceed to Step 2 (transfer dept MDO lookup)

           wfTransferRequest absent (has_pending_request = false)
             → Guide user with submission steps; no further API calls

           No profile returned → return error; no further API calls

STEP 2   → POST /private/user/v1/search        (pending request only)
                ↓ filters: channel = departmentName, role = MDO_LEADER
                ↓ Returns transfer-target dept's MDO leader contact
                ↓
           MDO_LEADER found → return admin_name, admin_email
           MDO_LEADER NOT found → YP fallback (no further API call)
```

> **Note:** Step 2 runs only when `wfTransferRequest` is present (pending request path). When no pending request exists, the chatbot skips Step 2 entirely and guides the user directly with submission steps — no admin lookup API call is made.

---

## Step 1 — Private Profile Fetch (includes `wfTransferRequest`)

> Fetches the user's private profile to determine whether a designation/group transfer request is already pending. The `wfTransferRequest` field is not available in the public read API.

**Endpoint:** `POST /private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/private/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
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
| `result.response.content[0].wfTransferRequest` | Determines if a pending transfer request exists |
| `result.response.content[0].wfTransferRequest.wfId` | Used to confirm `has_pending_request = true` |
| `result.response.content[0].wfTransferRequest.departmentName` | Target department for the transfer request |
| `result.response.content[0].rootOrgId` | Available in profile; not used in further API calls |
| `result.response.content[0].profileDetails.professionalDetails[0].designation` | User's current designation |
| `result.response.content[0].profileDetails.professionalDetails[0].group` | User's current group |
| `result.response.content[0].profileDetails.professionalDetails[0].name` | User's current department/org name |
| `result.response.content[0].profileDetails.profileDesignationStatus` | Raw designation status (PENDING / VERIFIED / NOT_VERIFIED) |
| `result.response.content[0].channel` | Fallback org/department name if professionalDetails is absent |

### Decision After Step 1

| Condition | Outcome |
|---|---|
| `wfTransferRequest.wfId` is present OR `wfTransferRequest.departmentName` is present | `has_pending_request = true`; extract `departmentName`; proceed to Step 2 |
| `wfTransferRequest` is absent or empty | `has_pending_request = false`; guide user with submission steps; no further API calls |
| No content returned / API failure | Return error; no further API calls |

---

## Step 2 — MDO Leader Lookup for Transfer Department (Pending Request path)

> Fetches the MDO_LEADER contact details for the target transfer department so the user knows who will approve their pending request.

**Endpoint:** `POST /private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/private/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "query": "",
      "filters": {
        "channel": "{department_name}",
        "organisations.roles": ["MDO_LEADER"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

> `{department_name}` is `wfTransferRequest.departmentName` from Step 1.

### Response Fields Used

| Field | Used For |
|---|---|
| `result.response.count` | Determines if any MDO_LEADER was found |
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

> When no Org Admin or MDO Leader is found, the chatbot uses a static YP allocation file (loaded in-memory) to look up the responsible Young Professional (YP) by state and department. No additional API call is made.

| Condition | Response |
|---|---|
| YP details found in allocation file | Return `yp_name`, `yp_email` to the user |
| YP details not found | Return generic message: "Please connect with the concerned YP for further assistance." |

---

## API Dependency Table

| Step | Endpoint | Method | Auth Required | Purpose | Key Fields |
|---|---|---|---|---|---|
| 1 | `/private/user/v1/search` | POST | Yes | Fetch private profile; read `wfTransferRequest`, `rootOrgId`, `designation`, `group` | `wfTransferRequest`, `rootOrgId`, `professionalDetails` |
| 2 (pending request only) | `/private/user/v1/search` | POST | Yes | Fetch MDO_LEADER contact for transfer target department | `firstname`, `surname`, `primaryEmail` |
| — (fallback) | YP Allocation File | — | — | Static YP lookup by state + department when no MDO_LEADER found | `yp_name`, `yp_email` |
