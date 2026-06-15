# iGOT Chatbot Dev UI

A single-file HTML chat widget for testing iGOT Deterministic Chatbot flows during local development.

## Usage

Start the API server:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --reload-include "*.yaml" --reload-exclude "logs" --port 8000
```

Open in browser:

```
http://localhost:8000/dev-ui
```

No extra install, no npm, no build step. The UI is served by FastAPI itself (same origin), so JWT auth and CORS work automatically.

> Only available when `IGOT_ENV=dev` (default) or `IGOT_ENV=staging`. Not mounted in production.

---

## Supported activity types

| Activity | Rendered as |
|----------|-------------|
| `markdown` | Formatted chat bubble (bold, italic, lists) |
| `text` | Plain chat bubble |
| `quick_replies` | Tappable buttons below the last message |
| `picker` | Searchable scrollable list |
| `input` | Free-text input field |
| `typing` | Animated three-dot indicator |
| `end` | Coloured outcome banner (see below) |
| `trace` | Collapsible debug JSON block |

### End-of-conversation outcomes

| Outcome | Banner | Colour |
|---|---|---|
| `self_served` | ✅ Issue resolved | Green |
| `ticket_raised` | 🎫 Ticket raised (shows ticket number) | Blue |
| `ticket_failed` | ⚠️ Ticket could not be raised | Orange |
| `ended` | 👋 Conversation ended | Grey |

---

## Features

- **Topic picker** — auto-generated from flow YAML `metadata.menu_label` fields; no hardcoded list
- **Language selector** — change preferred language (hi, ta, te, kn, …); changing language resets the session and tests the translation pipeline
- **Reset button** — starts a new session without a page reload
- **Debug panel** — session ID, flow ID, current node, status, last raw JSON response
- **Health check** — pings `GET /health` to confirm API is up before first message
- **Copy ID** — copies the session UUID to clipboard for manual `curl` testing
- **Settings** — configure API base URL and Bearer token if running on a non-default port or against a remote server

---

## Testing tips

### Test a specific flow

1. Click **Reset** to start a fresh session.
2. The topic picker appears with all active flows (from YAML metadata).
3. Click the topic button for the flow you want to test.
4. Step through the conversation.

To test a flow not yet in the menu (no `menu_label` set), use `curl` directly:

```bash
SESSION=$(curl -s -X POST http://localhost:8000/chat/sessions \
  -H "Authorization: Bearer dev-stub" \
  -H "Content-Type: application/json" \
  -d '{"channel": "web", "language": "en"}' | jq -r .session_id)

# Use the flow_id directly as the choice_id
curl -s -X POST "http://localhost:8000/chat/sessions/$SESSION/turn" \
  -H "Authorization: Bearer dev-stub" \
  -H "Content-Type: application/json" \
  -d '{"action": "select_choice", "choice_id": "MY_FLOW_ID"}' | jq .
```

### Test session expiry

Set `IGOT_WEB_SESSION_TTL_MINUTES=1` in `.env`, start a session, wait 1 minute, send any message. The runner should emit the restart prompt.

### Test translation

Select Hindi (hi) from the language dropdown → Reset → type a message in Hindi. The engine runner translates to English, runs the flow, translates the response back.

### Test Mode B LLM escalation

Use any Mode B flow (e.g. Login issue). Follow the deterministic path and mark the issue as unresolved twice. The `increment_and_branch` counter hits the threshold, routes to `transfer_to_llm`, which calls Vertex AI Gemini to write the ticket summary. Requires `GOOGLE_APPLICATION_CREDENTIALS` and Vertex AI credentials in `.env`.

If Vertex AI is unavailable, the node falls back automatically to a template-based ticket summary — no error is shown to the user.

### Test Zoho ticket creation

Complete any escalation path and confirm the ticket. Watch the server logs (terminal) for:
```
INFO  zoho   POST /tickets → 200 OK
INFO  zoho   Token refreshed. expires_in=3600s
```
The ticket number appears in the `ticket_raised` banner. Check Zoho Desk directly to verify the ticket content, subject prefix `[ITSM Support v2]`, and custom fields.

If you see the `ticket_failed` banner instead, check:
- `ZOHO_DEPARTMENT_ID` is set in `.env`
- `ZOHO_REFRESH_TOKEN`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_ORG_ID` are all correct
- Server logs show the full HTTP error body

### Test the quick_replies overflow guard

For WhatsApp testing (future): temporarily set `max_quick_reply_buttons = 3` in `WebAdapter`, reload, trigger a flow with >3 choices — confirm it renders as a picker instead of buttons.

---

## Architecture note

The Dev UI is:
- A **single HTML file** (`dev_ui/index.html`) — no dependencies, no bundler
- Served by FastAPI at `/dev-ui` via an `HTMLResponse` route
- **Same-origin** as the API — no CORS adjustments needed
- **Not deployed to production** — the `if settings.igot_env in ("dev", "staging")` guard in `main.py` ensures it's never exposed in prod
