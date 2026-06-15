# iGOT Deterministic Chatbot — Development Plan

> **Scope of this document**: What the L1/SOP team must deliver, how to pick the right flow type, weekly delivery targets, and acceptance criteria.
> Day-by-day task breakdown lives in sprint tickets, not here.

---

## Table of Contents

1. [Before Development Starts — Pre-Dev Checklist](#1-before-development-starts--pre-dev-checklist)
2. [What the L1 / SOP Team Must Provide](#2-what-the-l1--sop-team-must-provide)
3. [How to Decide Which Flow Type to Use](#3-how-to-decide-which-flow-type-to-use)
4. [SOP Requirements per Flow Type](#4-sop-requirements-per-flow-type)
5. [Weekly Delivery Plan](#5-weekly-delivery-plan)
6. [Integration Acceptance — Web / Mobile Team](#6-integration-acceptance--web--mobile-team)
7. [Testing Criteria](#7-testing-criteria)

---

## 1. Before Development Starts — Pre-Dev Checklist

These must be in place **before June 1**. A missing item here directly delays the first working flow.

### Karmayogi infra (platform team)

| Item | Owner | Status |
|------|-------|--------|
| PostgreSQL schema provisioned on shared cluster | Karmayogi infra | ⏳ |
| Redis access credentials provided to dev team | Karmayogi infra | ⏳ |
| Karmayogi API key for dev environment shared | Karmayogi API team | ⏳ |
| Keycloak JWKS endpoint accessible from dev network | Karmayogi infra | ⏳ |
| EKS namespace + ingress provisioned for staging | Karmayogi infra | ⏳ |

### Google Cloud / AI (procurement / tech)

| Item | Owner | Status |
|------|-------|--------|
| GCP project created in `asia-south1` (Mumbai) | Tarento / Karmayogi | ⏳ |
| Vertex AI API enabled on project | GCP admin | ⏳ |
| Service account created with Vertex AI User role | GCP admin | ⏳ |
| Monthly budget alarm at ₹10,000 configured | GCP admin | ⏳ |
| Cloud Translation API enabled on same project | GCP admin | ⏳ |
| Translation budget alarm configured (separate from LLM budget) | GCP admin | ⏳ |

### Zoho Desk

| Item | Owner | Status |
|------|-------|--------|
| OAuth refresh token for bot service account | Karmayogi support team | ⏳ |
| Department ID, ticket layout names confirmed | Support ops | ⏳ |
| Category + sub-category values for each intent confirmed | Support ops | ⏳ |

### SOP documents (L1 team)

| Item | Owner | Status |
|------|-------|--------|
| SOP documents for committed intents received | L1 / SOP team | ⏳ |
| Flow type decision confirmed (Mode A or B per intent) | Tech lead | ⏳ |
| Zoho ticket priority / severity per intent confirmed | Support ops | ⏳ |

**If any of the above infra items are missing on June 1, the team will build against stubs and switch to real credentials when they land. The committed pilot date (June 15) assumes real credentials arrive by June 5 at the latest.**

---

## 2. What the L1 / SOP Team Must Provide

The chatbot is only as good as the SOP it follows. The L1 team does not need to write YAML or code — but they must document the SOP in a structured way. Developers translate that document directly into a flow.

### What is a "flow"?

A flow is the script the chatbot follows for one specific support issue. Think of it as a structured phone-banking script:

- The bot asks one question at a time
- Each answer leads to the next scripted response
- It never improvises — it only does what the SOP says
- If the SOP doesn't cover a scenario, the bot raises a ticket

### What a good SOP document looks like

A usable SOP document answers all of the following:

```
1. What triggers this flow?
   → What phrases does a user type that mean this is the issue?
   → Examples: "certificate not visible", "can't download my cert", "cert missing"

2. What is the first question the bot asks?
   → What are the answer options? (keep to 2–5 maximum)

3. For each answer option — what happens next?
   → Does the bot show steps? Which steps exactly?
   → Does the bot need to look something up? (which API? what data?)
   → Does the bot ask another question? What are the options?

4. What does "resolved" look like for the user?
   → They followed the steps and it worked — end the conversation.

5. What triggers a ticket?
   → User says it didn't work after steps
   → Issue requires backend investigation
   → Any other condition?

6. What data goes on the ticket?
   → What does L2 need to know to resolve it?
   → Email, course ID, error message, steps tried — list them.

7. What is the Zoho ticket category / sub-category?
   → Confirm with support ops.
```

**A document that says "escalate to L2 if unresolved" is not sufficient.** The bot needs to know: after how many tries, with what data collected, under what conditions.

---

## 3. How to Decide Which Flow Type to Use

Answer these questions in order:

```
Q1 — Can you write every bot response as a fixed sentence today?
     (No "it depends on context" — just scripted text)

     No  → The SOP is not ready. Document it first.
     Yes → Continue to Q2.

Q2 — Does the SOP have a finite, known set of branches?
     (e.g., "if cert is generated → show steps A; if pending → show steps B")

     No  → Mode C or D. Go to Q4.
     Yes → Continue to Q3.

Q3 — Does L2 benefit from a richer, natural-language description
     of what the user went through — beyond just the collected fields?

     No  → Mode A (pure deterministic, zero AI cost)
     Yes → Mode B (deterministic + AI ticket summary at escalation)

Q4 — Is the user's opening message genuinely ambiguous?
     (They describe a "course problem" and you need AI to classify which one)

     Yes → Mode C (AI picks one of N declared sub-intents)

     Otherwise — is the request truly open-ended?
     (recommendation, knowledge base Q&A — no fixed decision tree)

     Yes → Mode D (full LLM conversation, Phase 3)
```

### Quick-reference table

| Pattern | Mode | AI cost | Voice-safe | When to use |
|---------|------|---------|------------|-------------|
| SOP is finite, template ticket is fine | **A** | ₹0 | ✅ | Most "show steps" flows |
| SOP is finite, L2 needs richer ticket | **B** | ~₹0.20 / escalated ticket | ✅ | Default for Phase 1 committed intents |
| User describes in free text, genuinely ambiguous | **C** | ~₹0.05 / classification | ⚠️ | When same opening phrase covers 3+ distinct issues |
| Open-ended, no decision tree (recommendation, Q&A) | **D** | ₹1–5 / session | ❌ | Phase 3 only |

**When in doubt, start with Mode A. Upgrading to B takes half a day. Downgrading from D is painful.**

---

## 4. SOP Requirements per Flow Type

### Mode A — Pure Deterministic

What the L1 team must deliver before development starts on this intent:

- [ ] List of trigger phrases (minimum 3–5 examples users actually type)
- [ ] Complete decision tree — every branch, every outcome, no "TBD"
- [ ] Exact bot text for every message (the developer should be able to copy-paste)
- [ ] Quick reply option labels (max 4 per question; keep them short)
- [ ] Which data to collect from the user (email, course name, etc.) and at which point
- [ ] What APIs to call (endpoint name, what data to look up, what response means what)
- [ ] Zoho ticket: category, sub-category, priority, severity, what fields to fill
- [ ] Confirmation message text when ticket is raised

**Deliverable format**: A document or spreadsheet. A decision-tree diagram is ideal. A detailed Word document also works. Raw conversation examples are helpful but not sufficient alone.

**What NOT to include**: Implementation details, API URLs, JSON payloads — those are the developer's job.

---

### Mode B — Deterministic + AI Ticket Summary

Everything in Mode A, plus:

- [ ] For the Zoho ticket description — list what information L2 needs. The AI will paraphrase the conversation into a paragraph using these as a guide.
- [ ] Confirm: after how many failed attempts should the bot escalate? (default: 2)
- [ ] Any phrases that should trigger immediate escalation without retries ("talk to human", "urgent", etc.)

The AI does not change conversation routing. It only writes the ticket description. The L1 team does not need to worry about prompt engineering.

---

### Mode C — LLM-Guided FSM *(Phase 3)*

Everything in Mode A for each sub-intent, plus:

- [ ] List of sub-intents under this category (each sub-intent becomes a Mode A/B flow)
- [ ] For each sub-intent — 5–10 example phrases users might type to describe it
- [ ] What the fallback should be if the AI can't classify (i.e., if user's message matches none clearly)

Example: "Course problem" → sub-intents: `progress_not_updating`, `certificate_missing`, `content_not_opening`. The AI reads the user's free-text description and picks one. If confidence is low, it asks a clarifying question.

---

### Mode D — Open LLM *(Phase 3)*

- [ ] Knowledge corpus: what content should the bot be allowed to answer from? (list of documents, FAQs, or dataset)
- [ ] Allowed actions (what can the bot do — search courses, check enrolment status?)
- [ ] Banned topics (what must the bot explicitly refuse — legal advice, medical, off-platform topics)
- [ ] Success definition — when has the user been served?
- [ ] Expected volume and session length (affects cost projection)

**Note**: Mode D flows require a developer to write a Python LangGraph subgraph, not just YAML. Budget 1–2 additional weeks per Mode D intent. This is not a SOP authoring task alone.

---

## 5. Weekly Delivery Plan

> Development starts **June 1, 2026**. Internal pilot target **June 15**. 2 developers.
> Weeks are Mon–Sun. Estimates are realistic, not optimistic.

---

### Week 0 — May 26–31 (Pre-Dev)

**Not development. Everything that must be in place before dev starts.**

**Target**: All blockers identified and either resolved or formally acknowledged with an ETA.

| Task | Who |
|------|-----|
| Share all SOP documents for committed intents (Cert Download, Profile Completion) | L1 team |
| Confirm Zoho categories + ticket fields for each intent | Support ops |
| Share Karmayogi dev API key + Redis + Postgres credentials | Karmayogi infra |
| Confirm GCP project + Vertex AI access | Tech lead |
| Share Keycloak JWKS endpoint for dev env | Karmayogi infra |
| Confirm web widget integration approach (iframe? npm package? REST URL?) | Web team |

**Acceptance criteria for Week 0**:
- SOP documents for at least the 2 committed intents are in the developer's hands
- At least one of the infra credentials (Karmayogi API key or Redis URL) is shared so dev can start
- Zoho ticket structure confirmed (no guessing during Week 1)

---

### Week 1 — June 1–7: Engine + Infrastructure + First Working Flow

**Target**: One end-to-end conversation working in a browser — user picks an issue, bot shows steps, ticket is raised in Zoho.

**What gets built**:
- LangGraph engine fully wired: YAML compiles → runs → Redis checkpoints
- FastAPI turn handling working (start session, submit action, get activities back)
- Engine runner (`engine/runner.py`) wired: AsyncIterator[Activity] → REST collects list
- WebAdapter instantiated at startup; channel routing in place (web + mobile)
- Session expiry check active (30-min sliding TTL for web; restart message on expiry)
- Karmayogi API calls validated against real API (or stubbed if credentials not yet available)
- Zoho ticket creation working with real credentials (or stubbed)
- Keycloak JWT validation wired (dev stub accepted if real JWKS not available)
- Mode A: Certificate Download flow — working end-to-end
- Mode B: Profile Completion flow — deterministic path working (LLM tail wired if Vertex AI available)
- Translation service instantiated (English-only in Week 1 is fine — translation is a no-op when src == tgt)

**What does NOT need to be done this week**:
- Multi-language translation calls (translation chain is wired but only en→en tested)
- Polish (error messages, edge cases) — that's Week 2
- Web widget integration — that's Week 2
- Mode C or D — Phase 3

**Acceptance criteria for Week 1**:
- `python -m app.engine.compiler --validate flows/` passes for all 4 reference flows
- `POST /chat/sessions` + `POST /chat/sessions/{id}/turn` returns valid Activity payloads
- Certificate Download flow: walk through all 4 sub-scenarios manually, each reaches either `self_served` or a Zoho ticket
- Profile Completion flow: at least the happy path (already 100%) and one escalation path working
- Zoho ticket appears in the desk with correct category and collected fields (even if from stub)
- Session expiry: set a 1-minute TTL in dev and confirm restart message appears on next turn
- No unhandled exceptions on any tested path

---

### Week 2 — June 8–14: Second Intent + QA + Pilot Prep

**Target**: Both committed intents stable and user-testable. Web widget connected. Pilot-ready by Friday June 13.

**What gets built**:
- Profile Completion flow: all branches working including Mode B LLM tail + template fallback
- All real integrations switched from stubs to live (Karmayogi APIs, Zoho, Vertex AI)
- PII redaction (Presidio) verified — Aadhaar, PAN, phone, email masked before LLM
- **Hindi translation end-to-end**: user sends Hindi text → engine runner translates to English → flow runs → response translated back to Hindi. Test one complete conversation in Hindi.
- Web widget receives Activity payloads and renders them (quick_replies, picker, text)
- Session resumption tested — user refreshes page, conversation continues
- Smoke test suite: Certificate Download and Profile Completion passing automated tests
- Langfuse traces visible for every conversation turn
- Stretch intent (Cert Name Correction or Designation Not Found) — if pace allows

**What does NOT need to be done this week**:
- All regional languages (Hindi first; others can follow if straightforward)
- Mobile channel — Phase 2
- WhatsApp — Phase 3
- Mode C or D flows — Phase 3
- Full test coverage — pilot comes first

**Acceptance criteria for Week 2 (pilot gate)**:
- Both committed intents pass manual test scripts: happy path + each branch + ticket fallback
- A user outside the dev team (L1 agent or PM) completes a full conversation without dev intervention
- Zoho ticket raised with correct fields, priority, and AI-paraphrased description (or template fallback visible in ticket)
- Template fallback tested: force LLM timeout → ticket still created with template summary
- LLM kill-switch tested: `LLM_KILL_SWITCH=true` → flows continue, template used, no error
- Session survives a server restart (Redis checkpointer)
- No personally identifiable data visible in Langfuse traces (Presidio verified)

---

### Week 3 — June 15–21: Pilot + Bug Fixes + Stretch Intents

**Target**: Live with a small set of real users. Fix what breaks. Add stretch intents if bandwidth allows.

**What gets built**:
- Pilot launched with a controlled group of L1 agents or internal users
- Bug fixes from pilot feedback (expected: edge cases in flows, rendering issues in widget, API error handling gaps)
- Stretch intent #1 (Cert Name Correction) — Mode B flow authored and tested
- Stretch intent #2 (Designation Not Found) — Mode B flow authored and tested
- Prometheus metrics endpoint working (session count, ticket rate, LLM call count, latency)
- Budget alarm confirmed active in GCP (70 / 90 / 100% of ₹10k)

**Acceptance criteria for Week 3**:
- Zero crash reports from pilot users (unhandled 500s)
- At least one stretch intent live
- Cost-per-session tracked in Langfuse and within projections
- Ops runbook written: how to deploy, rollback, check logs, trigger LLM kill-switch

---

### Week 4+ — June 22 onwards: Iteration + Phase 2 Planning

At this point Phase 1 core is stable. Decisions to make:

- Mode C implementation (if there are high-volume intents where users describe issues in free text)
- Mobile channel (Phase 2): wire WebSocket, test Activity rendering on mobile
- WhatsApp adapter (Phase 3)
- Additional Mode A/B intents from the 25-intent backlog
- Phase 2 scoping based on pilot data (which intents have highest volume? which have highest escalation rate?)

---

## 6. Integration Acceptance — Web / Mobile Team

The web widget team integrates against the iGOT Deterministic Chatbot REST API. These are the conditions under which integration can be declared complete.

### What the web team must implement

| Feature | Activity type | Notes |
|---------|---------------|-------|
| Render plain text | `text` | Simple paragraph |
| Render markdown | `markdown` | Bold, bullets, links — no arbitrary HTML |
| Render quick reply buttons | `quick_replies` | Tap = `select_choice` action |
| Render searchable course picker | `picker` | Search + scroll; tap = `select_choice` |
| Render free-text input | `input` | Submit = `submit_input` action |
| Show typing indicator | `typing` | Animated dots while bot is processing |
| Close / end chat | `end` | Carries `outcome` field |
| `disable_input` flag | all types | When true, text input box is hidden/disabled |

### REST contract

```
Start session:
  POST /chat/sessions
  Headers: Authorization: Bearer <Keycloak JWT>
  Body:    { "channel": "web", "language": "en" }
  Returns: { "session_id": "uuid", "activities": [...] }

Submit action:
  POST /chat/sessions/{session_id}/turn
  Headers: Authorization: Bearer <same JWT>
  Body:    { "action": "select_choice", "choice_id": "C1" }
           { "action": "submit_input",  "value": "user typed text" }
  Returns: { "activities": [...], "status": "awaiting_user" | "ended" }

Resume session (page refresh):
  GET /chat/sessions/{session_id}
  Returns: { "status": "...", "recent_activities": [...] }
```

### Web team integration acceptance criteria

- [ ] All 8 Activity types render correctly on Chrome, Firefox, Edge (latest)
- [ ] Responsive: works on 320px wide (mobile browser) through desktop
- [ ] `disable_input` hides free-text input when set
- [ ] `quick_replies` buttons are not submittable twice (prevent double-tap)
- [ ] Picker: search filters in real time (client-side filter on returned items)
- [ ] Session ID stored in `localStorage` — page refresh resumes conversation
- [ ] JWT refresh handled: if token expires mid-session, widget re-authenticates silently
- [ ] Error state: if `/turn` returns 5xx, show "Something went wrong. Please try again." — do not crash widget
- [ ] DPDP consent banner shown on first chat open (before session is created)
- [ ] Tested against a real iGOT Deterministic Chatbot staging instance, not just mocks

### Mobile team (Phase 2)

Same criteria as web, plus:

- [ ] WebSocket connection (`/chat/sessions/{id}/ws`) replaces polling
- [ ] Activity types render in native iOS and Android components
- [ ] Push notification on bot reply (when app is backgrounded)
- [ ] Offline handling: queue user action, send when connectivity restores

---

## 7. Testing Criteria

### Unit tests (engine + nodes)

**Target**: ≥ 80% coverage on `app/engine/`

Each node handler must have tests for:
- Happy path: given state X + config Y, returns expected state delta
- Validation errors: malformed YAML config raises `FlowCompilationError` with clear message
- Error routing: `api_call` node with `on_error` routes to correct node on timeout / 404 / any

```python
# Pattern for every node test
async def test_<node>_<scenario>(mocked_services):
    handler = SomeNode(services=mocked_services)
    fn = handler.build(cfg={...})
    result = await fn(state=initial_state(...))
    assert result["current_node"] == "expected_node"
    assert result["collected"]["key"] == "expected_value"
```

### Flow integration tests

For each YAML flow, cover:
- Happy path (user self-served)
- Every branch that leads to a ticket
- API call failure → `on_error` routing
- `transfer_llm` → LLM call succeeds → ticket
- `transfer_llm` → LLM times out → template fallback → ticket

Flows must be testable with fully mocked services (no real HTTP calls in CI).

### Manual test scripts (per intent, before pilot)

Before any intent goes live, a test script covering:

```
Intent: Certificate Download
Pass criteria: all paths below complete without error

[ ] C1 — cert generated but invisible → cache steps shown → user says resolved → self_served end
[ ] C1 — cert generated but invisible → steps don't help → email collected → Zoho ticket raised
[ ] C1 — cert pending < 24h → wait message shown → satisfied end
[ ] C1 — cert pending > 24h → email + completion date collected → ticket raised
[ ] C1 — not eligible → completion% shown, steps shown → satisfied end
[ ] C1 — not eligible → user disputes → evidence collected → ticket raised
[ ] C2 — download fails → steps shown → resolved
[ ] C2 — download fails → steps don't help → browser/device collected → ticket raised
[ ] C3 — wrong name → email + correct + wrong name collected → ticket raised
[ ] C4 — course incomplete → course picked → ticket raised
[ ] API error on cert status check → generic resolution shown
[ ] Zoho ticket creation fails → support email shown
```

### Performance benchmarks (pre-pilot)

| Metric | Target |
|--------|--------|
| `POST /chat/sessions/{id}/turn` p95 latency (no API calls) | < 200 ms |
| `POST /chat/sessions/{id}/turn` p95 latency (with Karmayogi API call) | < 3 s |
| `transfer_llm` node (Vertex AI call) p95 latency | < 8 s |
| Concurrent sessions supported (staging) | ≥ 50 |

### Security checks (pre-pilot)

- [ ] No raw `user_id` in any log line (HMAC only)
- [ ] Aadhaar, PAN, phone, email masked in Langfuse traces (Presidio verified)
- [ ] `Authorization` header not logged
- [ ] Zoho ticket description: Presidio-redacted transcript attached, not raw
- [ ] `service-account.json` not in repository (`.gitignore` verified)
- [ ] `.env` not in repository (`.gitignore` verified)
- [ ] DPDP consent banner functional
