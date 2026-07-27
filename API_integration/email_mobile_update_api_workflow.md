# UC-10: Multiple Accounts — Email ID / Mobile Number Update — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.
>
> **Flow ID:** `MULTIPLE_ACCOUNT` | **Flow Type:** `deterministic_with_llm_fallback`
>
> ⚠️ The chatbot does **NOT** generate or verify OTPs directly. After confirming the identifier is not registered to another account, the user is guided to complete the OTP steps themselves via **View Profile → Other Details → Edit**.

**Source:** all nodes below live in `flows/_shared/_multiple_account.yaml` (a fragment imported by `flows/mode_b_multiple_account.yaml`, entry node `ask_identifier_type`).

> **Related but separate entry point:** `flows/mode_b_profile_completion.yaml` (§4 "EMAIL / MOBILE UPDATE") also imports `_multiple_account` and its **"👥 Multiple account issue / Invalid domain"** quick reply redirects straight into this same `ask_identifier_type` flow — so everything below applies there too. However, that file's **"❌ Not receiving OTP"** quick reply is a *different, standalone* mini-flow (`fetch_root_org_for_email` → `lookup_mdo_for_email` → `show_mdo_contact_email`) that does **not** go through domain/registration checks at all, and looks up an **`MDO_ADMIN`** (filtered by `rootOrgId`), not an `MDO_LEADER` (filtered by `channel`) as this doc's flow does. That parallel mini-flow is out of scope for this document.

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

STEP 2b  → GET /api/user/private/v1/read/{user_id}   (fetch current registered email/mobile)
                ↓ new identifier == user's own current identifier
                    → show "already your active identifier" message. Ask to retry or close.
                ↓ otherwise (or on API error) → proceed to STEP 3

STEP 3   → POST /api/private/user/v1/search     (check if identifier already registered)
                ↓ API error / timeout → show retry message
                ↓ count == 0 (NOT registered)
                    → Guide user through self-service OTP steps (View Profile → Edit → OTP)
                        ↓ User got OTP → close (self-served)
                        ↓ User did NOT receive OTP
                            → GET  /api/user/private/v1/read/{user_id}
                            → POST /api/private/user/v1/search          (MDO_LEADER lookup)
                                ↓ MDO found  → show MDO contact. Stop.
                                ↓ MDO absent → YP lookup (in-memory allocation file). Stop
                                  (no ticket is raised on YP-lookup failure here — just a
                                  generic "contact YP/MDO" message).
                ↓ count > 0 AND the matched account IS the current user
                    → show "already your active identifier" message (same as STEP 2b). Stop.
                ↓ count > 0 (ALREADY registered to a DIFFERENT account — conflict)

STEP 4   → POST /api/course/private/v4/user/enrollment/list/{conflict_user_id}
                (counts are collected for the internal ticket only — NOT shown in chat)
                → Show impact message (deactivation warning + already-fetched current
                  email/mobile from STEP 2b) and ask user to Confirm / Cancel
                    Cancel  → restart from STEP 1
                    Confirm → raise Zoho L2 support ticket (includes conflict org +
                              enrollment counts in the ticket description)
```

> ⚠️ There is **no "Merge accounts" option**. `show_impact_and_confirm` is a `ticket_confirm` node, which always renders exactly two quick replies (**✅ Confirm** / **❌ Cancel**) — a three-way No/Merge/Yes decision does not exist in the current implementation.

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

> If no MDO Leader is found (`count == 0`), the chatbot falls back to a **YP (Young Professional / SPOC)** lookup (`data_lookup` against the in-memory index loaded from `data/Allocation_28.10.2025.xlsx`, via the `_yp_lookup` fragment). If the YP lookup itself also fails to find a match, this path (unlike the OTP-not-received path below) offers to **raise a Zoho support ticket** (`yp_not_found` → `yp_not_found_ticket_summary` → `yp_not_found_raise_ticket`) rather than just showing a generic message.

---

## Step 2b — Current Profile Fetch & Same-Identifier Check

> Called for **both** Email and Mobile paths, right after the domain check passes (or immediately, for Mobile). Fetches the user's own current email/mobile so the flow can short-circuit if the "new" identifier is actually already active on the user's own account.

**Endpoint:** `GET /api/user/private/v1/read/{user_id}` (node: `fetch_current_profile`)

### Response Fields Used

| Field path | YAML path | Purpose |
|---|---|---|
| `result.response.profileDetails.personalDetails.primaryEmail` | `$.response.profileDetails.personalDetails.primaryEmail` | Current registered email — compared against the new identifier |
| `result.response.profileDetails.personalDetails.mobile` | `$.response.profileDetails.personalDetails.mobile` | Current registered mobile — compared against the new identifier |

### Decision

| Condition | Action |
|---|---|
| New identifier equals the user's current email/mobile (`check_same_identifier`) | Show "this is already your active identifier" message; offer to try another identifier or close |
| Otherwise | Proceed to Step 3 |
| API error on this profile read | Fail-open — skip the same-identifier check and proceed directly to Step 3 |

> The `current_email` / `current_phone` values fetched here are reused later in Step 4's impact-confirmation message — no separate "current account profile" call is made at that point.

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
        "phone": "9876543210"
      }
    }
  }'
```

> ⚠️ The filter key for mobile lookups is **`"phone"`** — not `"mobile"`, even though the User Read API returns the same value under `profileDetails.personalDetails.mobile`. The `check_registration` node picks the filter key dynamically: `"{{ 'email' if update_type == 'EMAIL' else 'phone' }}"`.

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
| `count > 0` **and** `content[0].id` equals the current user's own ID | Show "already your active identifier" message (`same_identifier_message`) — treated the same as the Step 2b same-identifier case |
| `count > 0` (registered to a different account) | Fetch conflict account enrollments → show conflict details (Step 4) |

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

Quick replies offered (node `guide_self_service_update`): `✅ Updated successfully` / `❌ Did not receive OTP` — there is no third "⚠️ Still getting an error" option in the current implementation.

---

## Step 3 (OTP not received) — MDO Leader Lookup Sub-flow

> Triggered if the user reports they did not receive the OTP after following the self-service steps.

**API 1:** `GET /api/user/private/v1/read/{user_id}`

Same as the profile read in Step 2a — fetches `rootOrgId` and `channel`.

**API 2:** `POST /api/private/user/v1/search`

Same MDO Leader search as Step 2a — filters by `channel` and `MDO_LEADER` role.

> If no MDO Leader is found, this sub-flow does its own inline YP `data_lookup` (node `otp_yp_data_lookup`) rather than reusing the shared `_yp_lookup` fragment. **The fallback behavior differs from Step 2a**: if the YP lookup here also fails, the chatbot just shows a generic "contact YP/MDO" message (`otp_no_mdo_fallback`) — it does **not** offer to raise a Zoho support ticket, unlike Step 2a's `yp_not_found` path.

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
| `userCourseEnrolmentInfo.coursesInProgress` | `$.userCourseEnrolmentInfo.coursesInProgress` | In-progress course count for conflict account — **used only in the internal Zoho ticket description, not shown to the user in chat** |
| `userCourseEnrolmentInfo.certificatesIssued` | `$.userCourseEnrolmentInfo.certificatesIssued` | Completed course count for conflict account — same as above |

> On API error (e.g. cross-user permission denied), `fetch_conflict_enrollments` still proceeds to `show_impact_and_confirm` (`on_error: any: show_impact_and_confirm`) with whatever counts it managed to collect.

### Step 4.3 — Impact Message & Confirmation

> ⚠️ **No separate "Current Account Profile Read" / "Current Account Enrollment Fetch" calls are made here.** There are no node IDs `case_c_fetch_profile` or `case_c_fetch_current_enrollments` in the codebase. The impact message (`show_impact_and_confirm`, a `ticket_confirm` node) reuses `collected.current_email` / `collected.current_phone` already fetched back in **Step 2b**, and does **not** display the conflict account's org name or course counts to the user — it only warns that the conflicting account will be deactivated and shows the current vs. new identifier.

`show_impact_and_confirm` always renders exactly two quick replies:

| User's choice | Action |
|---|---|
| ❌ Cancel | Restart from Step 1 (`restart_flow` → `ask_identifier_type`) |
| ✅ Confirm | Raise Zoho L2 support ticket (`raise_support_ticket`) |

> There is no "Can we merge accounts?" option — that case does not exist in the current flow.

### Decision After Impact Summary

| User's final choice | Action |
|---|---|
| ✅ Confirm | Raise Zoho L2 support ticket (ticket description generated from static fields, not LLM-authored — see below) |
| ❌ Cancel | Restart from Step 1 (ask identifier type again) |

> The Zoho ticket is raised by the `transfer_llm` node `raise_support_ticket` with `auto_raise: silent` and `llm_context.skip_llm: true` — the subject/description are built from a static template (`subject_hint` / `static_description`), not generated by the LLM. The description includes the user ID, current email, new identifier, conflict org (`conflict_org_name`), and the conflict account's in-progress/completed course counts fetched above. No additional Karmayogi API call is made at this point. The ticket is tagged **P3 / Sev 3** and requires **manual L2 processing** to deactivate the conflict account.

---

## API Dependency Table

| Step | Endpoint | Method | Node (YAML) | Purpose | Key Fields |
|---|---|---|---|---|---|
| 2a (email, domain check) | `/api/user/v1/email/approvedDomains` | GET | `validate_email_domain` | Check if new email domain is whitelisted | `$.domains` |
| 2a (invalid domain) | `/api/user/private/v1/read/{user_id}` | GET | `domain_invalid_fetch_user_profile` | Get org channel for MDO lookup | `$.response.rootOrgId`, `$.response.channel` |
| 2a (invalid domain) | `/api/private/user/v1/search` | POST | `domain_invalid_lookup_mdo` | Find MDO Leader for user's org | `$.response.count`, `$.response.content[0].profileDetails.personalDetails` |
| 2b (both paths) | `/api/user/private/v1/read/{user_id}` | GET | `fetch_current_profile` | Fetch current email/mobile for same-identifier check and later reuse in Step 4 | `$.response.profileDetails.personalDetails.primaryEmail`, `$.response.profileDetails.personalDetails.mobile` |
| 3 (both paths) | `/api/private/user/v1/search` | POST | `check_registration` | Check if identifier is already registered (filter key: `email` or `phone`) | `$.response.count`, `$.response.content[0].id`, `$.response.content[0].channel`, `$.response.content[0].rootOrgId` |
| 3 (OTP not received) | `/api/user/private/v1/read/{user_id}` | GET | `otp_not_received_fetch_profile` | Get org channel for MDO lookup | `$.response.rootOrgId`, `$.response.channel` |
| 3 (OTP not received) | `/api/private/user/v1/search` | POST | `otp_not_received_lookup_mdo` | Find MDO Leader for OTP support contact | `$.response.count`, `$.response.content[0].profileDetails.personalDetails` |
| 4 (conflict path) | `/api/course/private/v4/user/enrollment/list/{conflict_user_id}` | POST | `fetch_conflict_enrollments` | Fetch conflict account enrollment summary (used in ticket description only) | `$.userCourseEnrolmentInfo.coursesInProgress`, `$.userCourseEnrolmentInfo.certificatesIssued` |

> Note: earlier versions of this doc referenced `case_c_fetch_profile` / `case_c_fetch_current_enrollments` nodes for a "current account" re-fetch in Step 4. **These nodes do not exist** in `flows/_shared/_multiple_account.yaml` — removed from this table.
