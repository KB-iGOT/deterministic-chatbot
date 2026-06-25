# UC-02: Incorrect Name on Certificate — API Integration Guide

> Karmayogi platform APIs consumed by the chatbot, in execution order. Intended for iGot developers integrating or extending this workflow.

---

## Execution Flow

```
STEP 1   → GET   /api/user/private/v1/read/{user_id}
                ↓ Profile read: get current firstName, lastName, and personalDetails
                ↓ Show current name on certificate
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
| `result.response.firstName` | Current first name shown on certificate |
| `result.response.lastName` | Current last name shown on certificate |
| `result.response.profileDetails.personalDetails.firstname` | Cross-check display vs personal details |
| `result.response.profileDetails.personalDetails.surname` | Cross-check display vs personal details |

**Surname-Duplication Detection** (Handled downstream by the UI/Platform, the chatbot directly presents the retrieved name for user confirmation).

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
| 1 | `/api/user/private/v1/read/{user_id}` | GET | Fetch current name on certificate | `firstName`, `lastName`, `profileDetails.personalDetails` |
