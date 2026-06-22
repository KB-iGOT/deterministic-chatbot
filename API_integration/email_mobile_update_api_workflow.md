# UC-10: Multiple Accounts — Email ID / Mobile Number Update — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.
>
> **Flow ID:** `MULTIPLE_ACCOUNT` | **Flow Type:** `deterministic_with_llm_fallback`
>
> ⚠️ The chatbot does **NOT** generate or verify OTPs directly. After confirming the identifier is not registered to another account, the user is guided to complete the OTP steps themselves via **View Profile → Other Details → Edit**.

---

## Execution Flow

```
STEP 1   → Ask user: Email ID or Mobile Number?

STEP 2   → Collect the identifier from the user

STEP 2a  → [Email only] GET /api/user/v1/email/approvedDomains
                ↓ domain NOT in approved list
                    → GET  /api/user/private/v1/read/{user_id}        (get rootOrgId + channel)
                    → POST /api/private/user/v1/search                 (MDO_LEADER lookup)
                        ↓ MDO found  → show MDO contact. Stop.
                        ↓ MDO absent → YP lookup (static file). Stop.
                ↓ domain valid (or API error — treat as valid)
                    → proceed to STEP 3

STEP 3   → POST /api/private/user/v1/search     (check if identifier already registered)
                ↓ API error / timeout → show retry message
                ↓ count == 0 (NOT registered)
                    → Guide user through self-service OTP steps (View Profile → Edit → OTP)
                        ↓ User got OTP → close (self-served)
                        ↓ User did NOT receive OTP
                            → GET  /api/user/private/v1/read/{user_id}
                            → POST /api/private/user/v1/search          (MDO_LEADER lookup)
                                ↓ MDO found  → show MDO contact. Stop.
                                ↓ MDO absent → YP lookup (static file). Stop.
                ↓ count > 0 (ALREADY registered — conflict)

STEP 4   → POST /api/course/private/v4/user/enrollment/list/{conflict_user_id}
                ↓ Show conflict account: org name, in-progress, completed counts
                → Ask user: No / Merge / Yes proceed
                    Case A (No)    → close politely
                    Case B (Merge) → inform merge not supported. Close.
                    Case C (Yes)
                        → GET  /api/user/private/v1/read/{user_id}           (current account)
                        → POST /api/course/private/v4/user/enrollment/list/{user_id}  (current account enrollments)
                        → Show impact summary (current enrollments, conflict account deactivation warning)
                        → Final YES  → raise Zoho L2 support ticket
                        → Final NO   → restart from STEP 1
```

---

## Step 2a — Email Domain Validation (Email Path Only)

> Skipped entirely for Mobile Number updates.

**Endpoint:** `GET /api/user/v1/email/approvedDomains`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/v1/email/approvedDomains"
```

### Response Fields Used

| Field path | YAML path (after adapter unwrap) | Purpose |
|---|---|---|
| `result.domains` | `$.domains` | List of approved email domains. The Karmayogi adapter auto-unwraps `result`, so YAML uses `$.domains` directly. |

### Decision After Domain Validation

| Condition | Action |
|---|---|
| User's email domain is in `$.domains` | Proceed to Step 3 — registration check |
| `$.domains` is empty / null | Treat as invalid → fetch MDO leader |
| User's email domain is NOT in the list | Fetch MDO leader contact → show invalid-domain message. Stop. |
| API error on domain fetch | Treat domain as valid; proceed to Step 3 (fail-open) |
| Mobile number provided (not email) | Skip domain check entirely; go directly to Step 3 |

---

## Step 2a (invalid domain) — User Profile Read for MDO Lookup

> Called only when the email domain is NOT in the approved list.

**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `result.response.rootOrgId` | `$.response.rootOrgId` | Used to identify the user's organisation |
| `result.response.channel` | `$.response.channel` | Organisation channel name used as MDO search filter |

---

## Step 2a (invalid domain) — MDO Leader Lookup

> Called immediately after the profile read above, to find the MDO Leader of the user's organisation.

**Endpoint:** `POST /api/private/user/v1/search`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "channel": "{org_channel}",
        "organisations.roles": ["MDO_LEADER"],
        "status": 1
      },
      "limit": 1
    }
  }'
```

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `result.response.count` | `$.response.count` | If 0, fall back to YP lookup (static file) |
| `result.response.content[0].profileDetails.personalDetails.firstname` | `$.response.content[0].profileDetails.personalDetails.firstname` | MDO Leader first name |
| `result.response.content[0].profileDetails.personalDetails.surname` | `$.response.content[0].profileDetails.personalDetails.surname` | MDO Leader surname |
| `result.response.content[0].profileDetails.personalDetails.primaryEmail` | `$.response.content[0].profileDetails.personalDetails.primaryEmail` | MDO Leader email shown to user |
| `result.response.content[0].firstName` | `$.response.content[0].firstName` | Fallback name if `profileDetails` is absent |
| `result.response.content[0].email` | `$.response.content[0].email` | Fallback email if `profileDetails` is absent |

> If no MDO Leader is found (`count == 0`), the chatbot falls back to a **YP (Yojna Prabhari)** lookup using a static allocation file. No additional API call is made for the YP fallback.

---

## Step 3 — Registration Check

> Called for **both** Email and Mobile paths. Determines whether the provided identifier is already registered to another Karmayogi account.

**Endpoint:** `POST /api/private/user/v1/search`

### For Email:

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "email": "user@example.gov.in"
      }
    }
  }'
```

### For Mobile Number:

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/private/user/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "filters": {
        "mobile": "9876543210"
      }
    }
  }'
```

> ⚠️ The filter key for mobile lookups is **`"mobile"`** — not `"phone"`. Karmayogi indexes mobile numbers under the `mobile` field (consistent with `profileDetails.personalDetails.mobile` in the User Read API).

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `result.response.count` | `$.response.count` | If > 0, identifier is already registered to another account |
| `result.response.content[0].id` | `$.response.content[0].id` | Conflict account's user ID (used in Step 4 enrollment fetch) |
| `result.response.content[0].channel` | `$.response.content[0].channel` | Conflict account's organisation name |
| `result.response.content[0].rootOrgId` | `$.response.content[0].rootOrgId` | Conflict account's root org ID |

### Decision After Registration Check

| Condition | Action |
|---|---|
| API error / timeout | Show retry message with "🔄 Try again" quick reply |
| `count == 0` (not registered) | Guide user through self-service OTP path via View Profile |
| `count > 0` (already registered) | Fetch conflict account enrollments → show conflict details (Step 4) |

---

## Step 3 (not registered) — Self-Service OTP Guidance

> The chatbot does **not** generate or verify OTPs. It instructs the user to complete the update themselves via their profile page.

The chatbot shows these steps to the user:

1. Go to **View Profile**
2. Open **Other Details**
3. Click the ✏️ **Edit (Pen) Icon** next to the Email ID / Mobile Number field
4. Enter the new Email ID / Mobile Number
5. Click **Request OTP**
6. Enter the OTP received
7. Verify the OTP
8. Click **Save Changes**

Quick replies offered: `✅ Updated successfully` / `❌ Did not receive OTP` / `⚠️ Still getting an error`

---

## Step 3 (OTP not received) — MDO Leader Lookup Sub-flow

> Triggered if the user reports they did not receive the OTP after following the self-service steps.

**API 1:** `GET /api/user/private/v1/read/{user_id}`

Same as the profile read in Step 2a — fetches `rootOrgId` and `channel`.

**API 2:** `POST /api/private/user/v1/search`

Same MDO Leader search as Step 2a — filters by `channel` and `MDO_LEADER` role.

> If no MDO Leader is found, falls back to YP static lookup. Same fallback pattern as Step 2a.

---

## Step 4 — Conflict Account Enrollment Fetch

> Called only when the registration check returns `count > 0` (identifier already belongs to another account). Fetches the conflict account's enrollment summary to show the user.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{conflict_user_id}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{conflict_user_id}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true
    }
  }'
```

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `userCourseEnrolmentInfo.coursesInProgress` | `$.userCourseEnrolmentInfo.coursesInProgress` | In-progress course count for conflict account |
| `userCourseEnrolmentInfo.certificatesIssued` | `$.userCourseEnrolmentInfo.certificatesIssued` | Completed course count for conflict account |

> On API error (e.g. cross-user permission denied), the chatbot proceeds to show conflict details with whatever information it already has (org name from Step 3 response).

### Conflict Decision Tree

| User's choice | Action |
|---|---|
| ❌ No, don't proceed | Close conversation. No changes made. |
| 🔗 Can we merge accounts? | Inform user merging is NOT supported. Close. |
| ⚠️ Yes, I want to proceed | Fetch current account profile + enrollments → show impact → final confirmation |

---

## Step 4 (Case C) — Current Account Profile Read

> Called when the user confirms they want to proceed despite the conflict. Fetches their current registered email / mobile to show in the impact summary.

**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `result.response.profileDetails.personalDetails.primaryEmail` | `$.response.profileDetails.personalDetails.primaryEmail` | Current registered email (shown in impact summary) |
| `result.response.profileDetails.personalDetails.mobile` | `$.response.profileDetails.personalDetails.mobile` | Current registered mobile (shown in impact summary) |

---

## Step 4 (Case C) — Current Account Enrollment Fetch

> Called after the profile read above. Fetches the current user's own enrollment summary to display alongside the conflict account summary in the impact warning.

**Endpoint:** `POST /api/course/private/v4/user/enrollment/list/{user_id}`

```bash
curl -X POST \
  "https://portal.uat.karmayogibharat.net/api/course/private/v4/user/enrollment/list/{user_id}" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "retiredCoursesEnabled": true
    }
  }'
```

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `userCourseEnrolmentInfo.coursesInProgress` | `$.userCourseEnrolmentInfo.coursesInProgress` | Current account in-progress count shown in impact summary |
| `userCourseEnrolmentInfo.certificatesIssued` | `$.userCourseEnrolmentInfo.certificatesIssued` | Current account completed count shown in impact summary |

### Decision After Impact Summary

| User's final choice | Action |
|---|---|
| ✅ Yes, proceed | Raise Zoho L2 support ticket (LLM generates subject + description) |
| ❌ No, start over | Restart from Step 1 (ask identifier type again) |

> The Zoho ticket is raised by the `transfer_llm` node with `auto_raise: true`. No additional Karmayogi API call is made at this point. The ticket is tagged **P3 / Sev 3** and requires **manual L2 processing** to deactivate the conflict account.

---

## API Dependency Table

| Step | Endpoint | Method | Node (YAML) | Purpose | Key Fields |
|---|---|---|---|---|---|
| 2a (email, domain check) | `/api/user/v1/email/approvedDomains` | GET | `validate_email_domain` | Check if new email domain is whitelisted | `$.domains` |
| 2a (invalid domain) | `/api/user/private/v1/read/{user_id}` | GET | `domain_invalid_fetch_user_profile` | Get org channel for MDO lookup | `$.response.rootOrgId`, `$.response.channel` |
| 2a (invalid domain) | `/api/private/user/v1/search` | POST | `domain_invalid_lookup_mdo` | Find MDO Leader for user's org | `$.response.count`, `$.response.content[0].profileDetails.personalDetails` |
| 3 (both paths) | `/api/private/user/v1/search` | POST | `check_registration` | Check if identifier is already registered | `$.response.count`, `$.response.content[0].id`, `$.response.content[0].channel` |
| 3 (OTP not received) | `/api/user/private/v1/read/{user_id}` | GET | `otp_not_received_fetch_profile` | Get org channel for MDO lookup | `$.response.rootOrgId`, `$.response.channel` |
| 3 (OTP not received) | `/api/private/user/v1/search` | POST | `otp_not_received_lookup_mdo` | Find MDO Leader for OTP support contact | `$.response.count`, `$.response.content[0].profileDetails.personalDetails` |
| 4 (conflict path) | `/api/course/private/v4/user/enrollment/list/{conflict_user_id}` | POST | `fetch_conflict_enrollments` | Fetch conflict account enrollment summary | `$.userCourseEnrolmentInfo.coursesInProgress`, `$.userCourseEnrolmentInfo.certificatesIssued` |
| 4c (Case C) | `/api/user/private/v1/read/{user_id}` | GET | `case_c_fetch_profile` | Fetch current account email/mobile for impact summary | `$.response.profileDetails.personalDetails.primaryEmail`, `$.response.profileDetails.personalDetails.mobile` |
| 4c (Case C) | `/api/course/private/v4/user/enrollment/list/{user_id}` | POST | `case_c_fetch_current_enrollments` | Fetch current account enrollment count for impact summary | `$.userCourseEnrolmentInfo.coursesInProgress`, `$.userCourseEnrolmentInfo.certificatesIssued` |
