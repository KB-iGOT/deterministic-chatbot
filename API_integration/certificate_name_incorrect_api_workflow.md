# UC-02: Incorrect Name on Certificate — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.
>
> **Source:** `flows/mode_b_certificate_download.yaml`, the `C3` sub-scenario (nodes prefixed `c3_`). This flow file is shared with UC-03 (Certificate Not Generated / Not Received, the `C1` sub-scenario) — the user first picks which certificate issue they have via the `ask_certificate_issue` node, and `C3` is a distinct path with no course lookup or ticket-raising of its own.

---

## Execution Flow

```
STEP 1   → GET   /api/user/private/v1/read/{user_id}
                ↓ Profile read: get current firstName (fallback: profileDetails.personalDetails.firstname)
                ↓ Show current first name on certificate
                ↓
           Ask: "Is the name correct?"
           User says YES   → Guide to re-download certificate, stop
           User says NO    → Ask if they want to update profile name
                ↓
           User says YES   → Provide self-service steps to edit profile name via portal, stop
           User says NO    → Close politely, stop
```

---

## Step 1 — Profile Name Fetch

> The flow calls this API via the `c3_fetch_user_profile` node to retrieve the user's current profile name before confirming it with them.

**Endpoint:** `GET /api/user/private/v1/read/{user_id}`

```bash
curl -X GET \
  "https://portal.uat.karmayogibharat.net/api/user/private/v1/read/{user_id}"
```

### Response Fields Used

| Field path | Purpose |
|---|---|
| `result.response.firstName` | Current first name shown on certificate; primary value displayed to the user |
| `result.response.profileDetails.personalDetails.firstname` | Fallback used only if `firstName` is empty (`c3_first_name or c3_pd_firstname`) |

> **Note:** Only the first name is fetched and shown. `lastName` and `profileDetails.personalDetails.surname` are **not** requested by this node — there is no surname-duplication check in this flow (that logic lives in UC-08's profile update flow, see `API_integration/profile_update_api_workflow.md`).

### Decision After Step 1

| Condition | Action |
|---|---|
| User confirms name is correct | Provide steps to re-download the certificate. Stop. |
| User says name is incorrect | Ask if they want to update their profile name. |
| User wants to update name | Provide **self-service guidance** on how to edit the profile name on the iGOT portal (Edit Profile -> Name). **No API call is made to update the name.** |
| User does not want to update | Close politely. Stop. |

---

## API Dependency Table

| Step | Endpoint | Method | Purpose | Key Fields |
|---|---|---|---|---|
| 1 | `/api/user/private/v1/read/{user_id}` | GET | Fetch current first name to confirm with the user | `firstName`, `profileDetails.personalDetails.firstname` (fallback only) |
