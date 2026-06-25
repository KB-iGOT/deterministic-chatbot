# Event Related Issues API Workflow

## Overview
This document outlines the API integrations and data extraction logic for the `EVENT_RELATED_ISSUES` flow, which handles four Use Cases:
1. Event Video Missing
2. Event Video Not Playing
3. Event Progress Not Updating
4. Event Certificate Not Generated

## 1. Event Enrollment List API
**Endpoint:** `GET /api/user/private/v1/events/list/{user_id}`

### Purpose
Retrieves the list of events the user is enrolled in. Used to dynamically populate the event picker list across all use cases, and specifically to validate progress and certificate status for Use Cases 3 and 4 directly from the list response.

### JSON Field Mappings (Extracting Picker Items)
- **List Path:** `$.events`
- **ID Field:** `contentId`
- **Label Field:** `event.name`

### Additional Context Variables Extracted
For Use Cases 3 and 4, we extract specific fields from the root event item (using the path `$`) to perform validations without needing extra API calls.

- **Time Spent (Duration):** Extracted using the custom `extract_event_time_spent` transform on the `$` object.
  - **Logic:** Checks the `lrcProgressDetails` JSON string. If empty or invalid, falls back to the `userEventConsumption[0].progressdetails` JSON string.
  - **Path parsed:** Parses the string and extracts the `"duration"` key.
  - **Stored as:** `collected.time_spent_seconds`

- **Certificate Status:** Extracted from the `issuedCertificates` array using the `has_issued_certificates` transform.
  - **Logic:** If the array is empty `[]`, it returns `False`. If it contains items, it returns `True`.
  - **Stored as:** `collected.certificate_issued`

---

## 2. Event Read API
**Endpoint:** `GET /api/event/v4/read/{eventId}`

### Purpose
Retrieves the configuration metadata for a specific event. Used to validate if the event has a valid video duration or a valid registration link for Use Cases 1 and 2.

### JSON Field Mappings
*Note: The Karmayogi backend adapter automatically unwraps the `{"result": ...}` envelope. The paths below begin inside the result envelope.*

- **Event Duration (Use Case 1):**
  - **Path:** `$.event.duration`
  - **Stored as:** `collected.event_duration`
  - **Validation Rule:** Checks if `collected.event_duration < 60.0`. If true, triggers a configuration issue technical ticket.

- **Registration Link (Use Case 2):**
  - **Path:** `$.event.registrationLink`
  - **Stored as:** `collected.registration_link`
  - **Validation Rule:** Extracted through the `is_youtube_embed_url` custom python transform (checks for `youtube.com/embed/` via regex).
  - **Stored as:** `collected.is_valid_youtube`
  - **Logic:** If `registration_link` is missing or `is_valid_youtube` is False, triggers a configuration issue technical ticket.

---

## Ticket Escalation
If a technical issue or configuration error is detected, the flow initiates a standard Zoho service request ticket via the `_zoho_ticket` fragment. No engineering database logging is performed.
