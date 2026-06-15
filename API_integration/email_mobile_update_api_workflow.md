# UC-06: Update Email ID / Mobile Number — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
[OTP NOT RECEIVED — Detect at any point]
  → GET  /api/user/private/v1/read/{user_id}
  → POST /api/user/v1/search              (MDO_ADMIN lookup; YP fallback if absent)
             ↓ Show MDO leader / YP contact. Stop.

STEP 1   → Collect Email ID or Mobile Number from user

STEP 2   → [Email only] GET /api/user/v1/email/approvedDomains
                ↓ domain NOT in approved list
                    → POST /api/user/v1/search   (MDO contact for rejection message)
                    → Inform user of invalid domain. Stop.
                ↓ domain valid (or mobile number)
                    → Ask user to confirm OTP generation

STEP 3   → POST /api/otp/ext/v1/generate
                ↓ OTP sent to new Email / Mobile
                → Ask user to enter OTP

STEP 4   → POST /api/otp/v1/verify
                ↓ OTP invalid → ask user to retry
                ↓ OTP valid

STEP 4a  → [check_account_registration — currently mock, no live API]
                        ↓ NOT already registered → STEP 5
                        ↓ ALREADY registered    → show org + enrollment summary
                                                   → get user confirmation
                            NO  → Inform user, no changes made. Stop.
                            Account merge → Inform user merge is NOT possible.
                            YES (deactivate second account)
                              → GET  /api/user/private/v1/read/{user_id}
                              → POST /api/course/private/v4/user/enrollment/list/{user_id}
                                (fetch current account enrollment count for confirmation message)
                        

STEP 5   → PATCH /api/user/private/v1/update
                ↓ HTTP 200 → inform user of successful update
                ↓ Non-200  → inform user of failure; ask to retry later
```

---

## OTP Not Received — Sub-flow

> Triggered immediately if the user reports not receiving an OTP at any point in the flow.

### MDO Contact Lookup — Profile Read

**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.rootOrgId` | Used to look up the MDO admin in the next call |
| `result.response.channel` | Org / department name displayed as MDO name |
| `result.response.profileDetails.professionalDetails[0].name` | Fallback department name for YP lookup |

### MDO Admin Search

**Endpoint:** `POST /api/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "rootOrgId": "{root_org_id}",
        "organisations.roles": ["MDO_ADMIN"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.count` | If 0, fall back to YP details from static allocation file |
| `result.response.content[0].profileDetails.personalDetails.firstname` | MDO admin first name |
| `result.response.content[0].profileDetails.personalDetails.surname` | MDO admin last name |
| `result.response.content[0].profileDetails.personalDetails.primaryEmail` | MDO admin email shown to user |

> If no MDO admin is found, the chatbot falls back to a YP (Yojna Prabhari) lookup using a static Excel-based allocation file. No additional API call is made for the YP fallback.

---

## Step 2 — Email Domain Validation (Email Updates Only)

> Called by `validate_update_identifier` tool when `identifier_type = "email"`.

**Endpoint:** `GET /api/user/v1/email/approvedDomains`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/v1/email/approvedDomains" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}"
```

### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.domains` | List of approved email domains; user's domain must be present to proceed |

### Decision After Domain Validation

| Condition | Action |
|---|---|
| User's email domain is in `result.domains` | Proceed to Step 3 — OTP generation |
| User's email domain is NOT in the list | Fetch MDO contact (same `POST /api/user/v1/search` as above), show invalid-domain message. Stop. |
| Mobile number provided (not email) | Skip domain check entirely; proceed to Step 3 |

---

## Step 3 — OTP Generation

> Called by `generate_and_send_otp` tool after user confirms they want to proceed.

**Endpoint:** `POST /api/otp/ext/v1/generate`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/otp/ext/v1/generate" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "type": "email",
      "key": "newuser@example.gov.in"
    }
  }'
```

> For mobile number updates, set `"type": "phone"` and `"key"` to the mobile number.

### Request Fields

| Field | Value | Notes |
|---|---|---|
| `request.type` | `"email"` or `"phone"` | Derived from whether `identifier_value` contains `@` |
| `request.key` | New email or mobile number | The destination to which OTP is sent |

### Response

| HTTP status | Chatbot action |
|---|---|
| `200` | Inform user OTP has been sent; ask them to enter it |
| Non-200 | Inform user OTP generation failed; ask to try again |

---

## Step 4 — OTP Verification

> After successful OTP verification the chatbot immediately calls `check_account_registration` (Step 4a below) to detect whether the new identifier is already in use by another account.

> Called by `verify_otp` tool after user submits the OTP.

**Endpoint:** `POST /api/otp/v1/verify`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/otp/v1/verify" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "type": "email",
      "key": "newuser@example.gov.in",
      "otp": "123456"
    }
  }'
```

### Request Fields

| Field | Value | Notes |
|---|---|---|
| `request.type` | `"email"` or `"phone"` | Same logic as OTP generation |
| `request.key` | New email or mobile number | Must match the key used in Step 3 |
| `request.otp` | OTP entered by user | 6-digit string |

### Response

| HTTP status | Chatbot action |
|---|---|
| `200` | OTP accepted; proceed to account registration check |
| Non-200 | Inform user the OTP is invalid; ask to re-enter or regenerate |

> **OTP Expiry:** If the user takes too long, `generate_and_send_otp` is called again and OTP is re-verified. The chatbot informs the user that OTPs are time-bound.

---

## Step 4a — Account Registration Check

> Called by `check_account_registration` tool immediately after OTP verification succeeds.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "email": "user@example.com"
      }
    }
  }'
```

> For mobile number lookups, replace `"email"` with `"phone"` in the filter.

### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.count` | If > 0, an account with that identifier already exists |
| `result.response.content[0].rootOrg.orgName` | Organisation name to show the user |
| `result.response.content[0].completions` / enrollment counts | In-progress and completed course counts |

### Response Schema

| Field | Type | Purpose |
|---|---|---|
| `is_registered` | bool | `false` = no conflict; proceed to update |
| `organization` | string | Name of the org the conflicting account belongs to |
| `total_enrollments` | int | Total enrolled courses in the conflicting account |
| `in_progress` | int | In-progress courses |
| `completed` | int | Completed courses |

### Decision After Registration Check

| Condition | Action |
|---|---|
| `is_registered` is `false` | Proceed directly to Step 5 — update |
| `is_registered` is `true` | Show org + enrollment summary; ask user how to proceed |
| User says NO (keep things as-is) | Inform no changes made. Close. |
| User requests account merge | Tell user merging is NOT possible. |
| User confirms YES (aware of deactivation) | Fetch current account enrollments (`get_user_details`), show count, ask final confirmation → escalate to L2 |

---

## Step 4b — Current Account Enrollment Fetch (Conflict Path Only)

> Called by `get_user_details` tool only when the identifier is already registered and the user confirms they want to proceed. Used to display the current account's enrollment count before escalating to L2.

### APIs called internally by `get_user_details`

**Profile Read:** `GET /api/user/private/v1/read/{user_id}`

(Same endpoint as in the OTP Not Received sub-flow above.)

**Course Enrollments:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id}" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true,
      "status": ["In-Progress", "Completed"]
    }
  }'
```

### Response Fields Used

| Field | Purpose |
|---|---|
| `enrollment_summary.total_enrollments` | Shown to user as their current account's enrollment count before L2 escalation |

---

## Step 5 — Apply the Update

> Called by `update_account_identifier` tool after OTP is verified and (if applicable) user has confirmed proceeding despite the conflict.

**Endpoint:** `PATCH /api/user/private/v1/update`

```bash
# Email update
curl -X PATCH \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/update" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "userId": "{user_id}",
      "email": "newuser@example.gov.in"
    }
  }'

# Mobile update
curl -X PATCH \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/update" \
  -H "Authorization: Bearer {{KARMAYOGI_API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "userId": "{user_id}",
      "phone": "9876543210"
    }
  }'
```

### Request Fields

| Field | Source | Notes |
|---|---|---|
| `request.userId` | Session `user_id` | Required |
| `request.email` | New email from user | Included only for email updates |
| `request.phone` | New mobile number from user | Included only for mobile updates; `email` key is omitted |

### Response

| HTTP status | Chatbot action |
|---|---|
| `200` | Inform user the Email ID / Mobile Number has been successfully updated |
| Non-200 | Inform user an error occurred during update; ask to try again later |

---

## API Dependency Table

| Step | Endpoint | Method | Tool | Purpose | Key Fields |
|---|---|---|---|---|---|
| OTP Not Received / MDO lookup | `/api/user/private/v1/read/{user_id}` | GET | `get_mdo_contact_details` | Fetch `rootOrgId` and `channel` for MDO admin search | `rootOrgId`, `channel` |
| OTP Not Received / MDO lookup | `/api/user/v1/search` | POST | `get_mdo_contact_details` | Find MDO_ADMIN for the user's org (YP fallback if none found) | `content[0].profileDetails.personalDetails` |
| 2 (email only) | `/api/user/v1/email/approvedDomains` | GET | `validate_update_identifier` | Check if new email domain is whitelisted | `result.domains` |
| 2 (email, invalid domain) | `/api/user/v1/search` | POST | `validate_update_identifier` | Fetch MDO leader to reference in rejection message | `content[0].profileDetails.personalDetails` |
| 3 | `/api/otp/ext/v1/generate` | POST | `generate_and_send_otp` | Send OTP to new Email ID or Mobile Number | HTTP 200 = sent |
| 4 | `/api/otp/v1/verify` | POST | `verify_otp` | Confirm OTP entered by user | HTTP 200 = verified |
| 4a | `/api/private/user/v1/search` | POST | `check_account_registration` | Detect if new identifier is already registered to another account | `result.response.count`, `content[0].rootOrg.orgName` |
| 4b (conflict path) | `/api/user/private/v1/read/{user_id}` | GET | `get_user_details` | Fetch current account enrollment count before L2 escalation | `enrollment_summary.total_enrollments` |
| 4b (conflict path) | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | `get_user_details` | Fetch course list for enrollment count | `completionPercentage`, `status` |
| 5 | `/api/user/private/v1/update` | PATCH | `update_account_identifier` | Apply new email or mobile number to the user's account | HTTP 200 = success |
