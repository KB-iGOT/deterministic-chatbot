#!/usr/bin/env python3
"""
iGOT Deterministic Chatbot — Local End-to-End Test
====================================================
Tests the Certificate Download flow with real credentials from .env.

Run from the project root directory so pydantic-settings picks up .env:

    python scripts/test_local.py                # interactive: you choose at each step
    python scripts/test_local.py --auto         # scripted: C2 → "yes, fixed" → satisfied
    python scripts/test_local.py --karmayogi    # direct Karmayogi API probe (no flow)
    python scripts/test_local.py --zoho         # direct Zoho ticket creation test
    python scripts/test_local.py --translate    # translation service test (Gemini)

Test user: set IGOT_TEST_USER_ID in .env to a real Karmayogi user UUID.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
from pathlib import Path
from uuid import uuid4

# ── Bootstrap: point Python at project root and load .env ────────────────────
ROOT = Path(__file__).resolve().parent.parent          # project root
os.chdir(ROOT)                                         # so pydantic-settings finds .env
sys.path.insert(0, str(ROOT))

# NOW import app modules — settings read .env from cwd
from langgraph.checkpoint.memory import MemorySaver    # noqa: E402

from app.api.auth import hash_user_id                  # noqa: E402
from app.config import settings                        # noqa: E402
from app.engine.compiler import FlowCompiler           # noqa: E402
from app.engine.state import initial_state             # noqa: E402
from app.services.registry import ServiceRegistry      # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
# Set IGOT_TEST_USER_ID in .env to a real Karmayogi user UUID.
# Karmayogi API calls (course list, cert status) only return real data when this
# matches an actual enrolled user. Leave empty to skip live API assertions.
TEST_USER_ID: str = os.getenv("IGOT_TEST_USER_ID", "")
# In dev mode hash_user_id() is a no-op, so the UUID passes through to Karmayogi.
TEST_USER_HASH = TEST_USER_ID

CERT_FLOW_PATH = ROOT / "flows" / "mode_a_certificate_download.yaml"
W = 64  # display width


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def sep(char: str = "─", width: int = W) -> None:
    print(char * width)


def header(title: str) -> None:
    sep("═")
    print(f"  {title}")
    sep("═")


def section(title: str) -> None:
    print()
    sep()
    print(f"  {title}")
    sep()


def print_activities(activities: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pretty-print bot activities. Returns (choices, picker_items)."""
    choices: list[dict] = []
    items: list[dict] = []

    for act in activities:
        t = act.get("type", "")

        if t in ("text", "markdown"):
            content = act.get("content", "")
            # Indent and wrap long lines
            for line in content.splitlines():
                wrapped = textwrap.fill(line, width=W - 4, subsequent_indent="    ")
                print(f"  🤖  {wrapped}" if line else "")

        elif t == "quick_replies":
            print()
            print("  Choose an option:")
            for i, c in enumerate(act.get("choices", []), 1):
                icon = c.get("icon", "")
                print(f"    {i}. [{c['id']}]  {icon} {c['label']}")
                choices.append(c)

        elif t == "picker":
            print()
            placeholder = act.get("placeholder", "Select from list")
            print(f"  📋  {placeholder}")
            picker_items = act.get("items") or []
            if picker_items:
                for i, item in enumerate(picker_items, 1):
                    meta = f"  ({item['meta']})" if item.get("meta") else ""
                    print(f"    {i}. [{item['id']}]  {item['label']}{meta}")
                    items.append(item)
            else:
                print("    (no items — API returned empty list or stub is active)")

        elif t == "input":
            field = act.get("input_id", "value")
            placeholder = act.get("input_placeholder", "")
            print()
            print(f"  ✏️   Enter {field}" + (f"  ({placeholder})" if placeholder else ""))

        elif t == "end":
            outcome = act.get("outcome", "ended")
            content = act.get("content", "")
            if content:
                print(f"\n  ✅  {content}")
            print(f"\n  ── Conversation ended ── outcome: {outcome}")

        elif t == "trace":
            for line in (act.get("trace_lines") or []):
                print(f"  [TRACE] {line}")

    return choices, items


def prompt_choice(choices: list[dict], auto: str | None = None) -> str | None:
    """Ask the user to pick one of the quick_reply choices."""
    if not choices:
        return None
    if auto:
        matched = next((c for c in choices if c["id"] == auto), choices[0])
        print(f"\n  → [AUTO] {matched['id']} — {matched['label']}")
        return matched["id"]
    while True:
        raw = input("\n  Your choice (number or ID, q=quit): ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]["id"]
        for c in choices:
            if c["id"].upper() == raw.upper():
                return c["id"]
        print("  ⚠  Not a valid choice — try again.")


def prompt_item(items: list[dict], auto: str | None = None) -> str | None:
    """Ask the user to pick from a picker list."""
    if not items:
        print("  (picker is empty — skipping)")
        return None
    if auto:
        matched = next((it for it in items if it["id"] == auto), items[0])
        print(f"\n  → [AUTO] {matched['id']} — {matched['label']}")
        return matched["id"]
    while True:
        raw = input("\n  Select item (number or ID, q=quit): ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return items[idx]["id"]
        for it in items:
            if it["id"] == raw:
                return it["id"]
        print("  ⚠  Not a valid selection — try again.")


def prompt_text(prompt_msg: str = "Enter value", auto: str | None = None) -> str:
    """Ask the user to enter free text."""
    if auto is not None:
        print(f"\n  → [AUTO] entering: {auto!r}")
        return auto
    raw = input(f"\n  {prompt_msg}: ").strip()
    return raw or "(skipped)"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Certificate Download Flow — via LangGraph
# ─────────────────────────────────────────────────────────────────────────────

async def run_flow_scenario(
    auto_script: list[tuple[str, str | None]] | None = None,
) -> None:
    """Drive the CERTIFICATE_DOWNLOAD flow turn-by-turn.

    auto_script: list of (action_type, value) tuples for scripted mode.
      action_type: "choice" | "item" | "text"
      value:       the choice_id / item_id / text, or None for interactive.
    """
    header("CERTIFICATE DOWNLOAD FLOW — End-to-End Test")
    print(f"  User:       {TEST_USER_ID}")
    print(f"  Flow YAML:  {CERT_FLOW_PATH.name}")
    print(f"  Session:    fresh (MemorySaver, no Redis needed)")
    print(f"  Mode:       {'scripted (--auto)' if auto_script is not None else 'interactive'}")

    # ── 1. Init ───────────────────────────────────────────────────────────────
    section("1/3  Initialising services + compiler")
    services = ServiceRegistry.from_env()
    checkpointer = MemorySaver()
    compiler = FlowCompiler(services=services)

    if not CERT_FLOW_PATH.exists():
        print(f"  ❌  Flow YAML not found: {CERT_FLOW_PATH}")
        return

    print(f"  Loading {CERT_FLOW_PATH.name} …")
    flow = compiler.load_flow(CERT_FLOW_PATH)
    graph = compiler.compile_flow(flow, checkpointer=checkpointer)
    print(f"  ✅  Compiled '{flow['flow_id']}' ({flow['flow_type']}, "
          f"{len(flow['nodes'])} nodes)")

    # ── 2. Create session ─────────────────────────────────────────────────────
    section("2/3  Starting session")
    session_id = uuid4()
    config = {"configurable": {"thread_id": str(session_id)}}

    state = initial_state(
        session_id=session_id,
        user_id_hash=TEST_USER_HASH,   # raw user_id so Karmayogi API works
        channel="web",
        language="en",
        session_ttl_minutes=60,
    )
    state_dict = state.model_dump(mode="json")
    state_dict["flow_id"] = flow["flow_id"]

    print(f"  Session ID: {session_id}")
    print(f"  User hash:  {TEST_USER_HASH[:8]}…")

    # ── 3. Conversation loop ──────────────────────────────────────────────────
    section("3/3  Conversation")

    script_idx = 0
    turn = 0
    result = None
    terminal = {"satisfied", "ticket_raised", "ended", "error"}

    def next_auto() -> tuple[str, str | None] | None:
        nonlocal script_idx
        if auto_script is not None and script_idx < len(auto_script):
            val = auto_script[script_idx]
            script_idx += 1
            return val
        return None

    # ── First invocation ───────────────────────────────────────────────────
    print(f"\n  Turn {turn + 1}  [start]")
    sep("·")
    try:
        result = await graph.ainvoke(state_dict, config)
    except Exception as exc:
        print(f"\n  ❌  LangGraph error on start: {exc}")
        raise

    turn += 1
    activities = result.get("pending_activities") or []
    choices, items = print_activities(activities)
    status = result.get("status", "active")
    print(f"\n  [status: {status}  node: {result.get('current_node')}]")

    # ── Conversation turns ─────────────────────────────────────────────────
    while status not in terminal:
        print(f"\n  Turn {turn + 1}  [user input]")
        sep("·")

        update: dict = {"pending_activities": []}
        collected = dict(result.get("collected") or {})

        auto = next_auto()

        # Determine what the user should provide based on activities
        if choices:
            # quick_replies → select_choice
            auto_val = auto[1] if auto else None
            choice_id = prompt_choice(choices, auto=auto_val)
            if choice_id is None:
                print("\n  Exiting.")
                break
            # Look up on_reply.save_to in YAML
            current_node = result.get("current_node")
            save_to_field = _get_save_to(flow, current_node)
            collected["_last_choice_id"] = choice_id
            if save_to_field:
                collected[save_to_field] = choice_id
            update["collected"] = collected

        elif items:
            # picker → pick_item
            auto_val = auto[1] if auto else None
            item_id = prompt_item(items, auto=auto_val)
            if item_id is None:
                print("\n  Exiting.")
                break
            # Look up field name from current collect node
            field_name = _get_collect_field(flow, result.get("current_node"))
            collected["_last_choice_id"] = item_id
            if field_name:
                collected[field_name] = item_id
            update["collected"] = collected

        else:
            # text input or no input needed — look for an input activity
            input_acts = [a for a in activities if a.get("type") == "input"]
            if input_acts:
                field_id = input_acts[0].get("input_id", "value")
                auto_val = auto[1] if auto else None
                text = prompt_text(f"Enter {field_id}", auto=auto_val)
                fname = field_id.removeprefix("collected.")
                collected[fname] = text
                update["collected"] = collected
            else:
                # No user input needed (e.g. message-only node, no quick_replies)
                # Just resume — nothing to update
                pass

        # Apply update + resume
        await graph.aupdate_state(config, update)
        try:
            result = await graph.ainvoke(None, config)
        except Exception as exc:
            print(f"\n  ❌  LangGraph error on resume: {exc}")
            raise

        turn += 1
        activities = result.get("pending_activities") or []
        choices, items = print_activities(activities)
        status = result.get("status", "active")
        print(f"\n  [status: {status}  node: {result.get('current_node')}]")

    sep("═")
    print(f"\n  ✅  Flow complete after {turn} turn(s).  Final status: {status}")
    print()
    await services.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Direct Karmayogi API probe
# ─────────────────────────────────────────────────────────────────────────────

async def run_karmayogi_test() -> None:
    header("KARMAYOGI API — Direct Probe")
    print(f"  Base URL: {settings.karmayogi_portal_base_url}")
    print(f"  User ID:  {TEST_USER_ID}")

    services = ServiceRegistry.from_env()
    svc = services["karmayogi"]

    # ── Test 1: enrollment list ───────────────────────────────────────────────
    section("Test 1: Enrollment list (completed courses)")
    try:
        data = await svc.execute_request(
            method="POST",
            url=f"/api/course/private/v4/user/enrollment/list/{TEST_USER_ID}",
            body={"request": {"filters": {"status": ["completed"]}}},
        )
        courses = (data.get("courses") or [])
        print(f"  ✅  API responded — {len(courses)} completed course(s) found")
        for i, c in enumerate(courses[:5], 1):  # show at most 5
            cid = c.get("courseId") or c.get("course_id") or "?"
            name = c.get("courseName") or c.get("courseName") or c.get("name") or "Unknown"
            pct = c.get("completionPercentage") or c.get("completionPct") or "?"
            print(f"    {i}. {name[:60]}  ({pct}%)  [{cid[:20]}]")
        if len(courses) > 5:
            print(f"    … and {len(courses) - 5} more")
    except Exception as exc:
        print(f"  ❌  Enrollment list failed: {exc}")

    # ── Test 2: user profile ──────────────────────────────────────────────────
    section("Test 2: User profile lookup")
    try:
        data = await svc.execute_request(
            method="GET",
            url=f"/api/user/v2/read/{TEST_USER_ID}",
            params={"fields": "firstName,lastName,profileDetails"},
        )
        fname = data.get("firstName") or data.get("first_name") or "?"
        lname = data.get("lastName") or data.get("last_name") or "?"
        print(f"  ✅  User: {fname} {lname}  [{TEST_USER_ID[:8]}…]")
    except Exception as exc:
        print(f"  ⚠   Profile lookup failed (may need different endpoint): {exc}")

    await services.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Direct Zoho Desk ticket creation
# ─────────────────────────────────────────────────────────────────────────────

async def run_zoho_test() -> None:
    header("ZOHO DESK — Test Ticket Creation")
    print(f"  Base URL:  {settings.zoho_base_url}")
    print(f"  Org ID:    {settings.zoho_org_id}")
    print(f"  Dept ID:   {settings.zoho_department_id}")

    services = ServiceRegistry.from_env()
    svc = services["zoho_desk_api"]

    # ── Test 1: OAuth token ───────────────────────────────────────────────────
    section("Test 1: OAuth token refresh")
    try:
        token = await svc._ensure_token()
        masked = token[:10] + "…" + token[-5:] if len(token) > 20 else token
        print(f"  ✅  Access token obtained: {masked}")
    except Exception as exc:
        print(f"  ❌  Token refresh failed: {exc}")
        await services.aclose()
        return

    # ── Test 2: Create test ticket ────────────────────────────────────────────
    section("Test 2: Create test Zoho ticket")
    print("  ⚠   This creates a REAL ticket in Zoho. Press Ctrl+C to cancel.")
    try:
        _ = input("  Press Enter to continue, or Ctrl+C to abort: ")
    except KeyboardInterrupt:
        print("\n  Aborted — no ticket created.")
        await services.aclose()
        return

    import datetime as _dt
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        result = await svc.execute_request(
            method="POST",
            url="/tickets",
            body={
                "subject": f"[TEST] iGOT Deterministic Chatbot E2E Test — {ts}",
                "description": (
                    "This is an automated test ticket created by scripts/test_local.py.\n"
                    f"Timestamp: {ts}\n"
                    f"Test user: {TEST_USER_ID}\n"
                    "Please delete this ticket."
                ),
                "email": "igot-test@igotkarmayogi.gov.in",
                "priority": "P4",
                "classification": "Query",
                "departmentId": settings.zoho_department_id,
                "channel": "Bot",
                "cf": {
                    "cf_category": "test",
                    "cf_bot_session_id": "test-local-e2e",
                    "cf_flow_id": "MANUAL_TEST",
                    "cf_llm_involved": "false",
                },
            },
        )
        ticket_id = result.get("ticketNumber") or result.get("id") or "?"
        print(f"  ✅  Ticket created successfully — ID: {ticket_id}")
        print(f"  🔗  https://desk.zoho.in/support/igot-chatbot/ShowHomePage.do#Tickets/dv/")
    except Exception as exc:
        print(f"  ❌  Ticket creation failed: {exc}")

    await services.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Translation service
# ─────────────────────────────────────────────────────────────────────────────

async def run_translation_test() -> None:
    header("TRANSLATION SERVICE — Composite Failover Chain")
    print(f"  Primary:   {settings.translation_primary}")
    print(f"  Enabled:   {settings.translation_enabled}")

    services = ServiceRegistry.from_env()
    svc = services.get("translation")
    if svc is None:
        print("  ❌  Translation service not registered")
        return

    test_pairs = [
        ("hi", "मेरा प्रमाण पत्र दिखाई नहीं दे रहा है।",
         "My certificate is not visible."),
        ("hi", "मुझे पाठ्यक्रम का प्रमाण पत्र कहाँ मिलेगा?",
         "Where will I get the course certificate?"),
        ("ta", "என் சான்றிதழ் காணவில்லை.",
         "My certificate is not visible."),
    ]

    for lang, src_text, expected_en in test_pairs:
        section(f"Translate {lang.upper()} → EN")
        print(f"  Source ({lang}): {src_text}")
        try:
            en = await svc.to_english(src_text, src=lang)
            print(f"  English:        {en}")
            print(f"  Expected ~:     {expected_en}")
            print(f"  ✅  Translation succeeded")
        except Exception as exc:
            print(f"  ❌  Translation failed: {exc}")

    section("Translate EN → HI (outbound)")
    en_text = "Your certificate was generated on 12 May 2026. Please clear your browser cache."
    print(f"  English: {en_text}")
    try:
        hi = await svc.from_english(en_text, tgt="hi")
        print(f"  Hindi:   {hi}")
        print(f"  ✅  Outbound translation succeeded")
    except Exception as exc:
        print(f"  ❌  Translation failed: {exc}")

    await services.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# YAML helpers (used by flow scenario)
# ─────────────────────────────────────────────────────────────────────────────

def _get_save_to(flow: dict, node_id: str | None) -> str | None:
    """Return on_reply.save_to field name (without 'collected.' prefix) for a node."""
    if not node_id:
        return None
    for node in flow.get("nodes", []):
        if node["id"] == node_id:
            on_reply = node.get("on_reply")
            if isinstance(on_reply, dict):
                save_to = on_reply.get("save_to", "")
                if save_to:
                    return save_to.removeprefix("collected.")
    return None


def _get_collect_field(flow: dict, node_id: str | None) -> str | None:
    """Return the field.name (without 'collected.' prefix) for a collect node."""
    if not node_id:
        return None
    for node in flow.get("nodes", []):
        if node["id"] == node_id and node.get("type") == "collect":
            return (node.get("field") or {}).get("name", "").removeprefix("collected.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--auto",
        action="store_true",
        help="Scripted path: Certificate → C2 (download fails) → Yes (fixed) → satisfied",
    )
    p.add_argument(
        "--karmayogi",
        action="store_true",
        help="Direct Karmayogi API probe (enrollment list + user profile)",
    )
    p.add_argument(
        "--zoho",
        action="store_true",
        help="Direct Zoho Desk OAuth + ticket creation test",
    )
    p.add_argument(
        "--translate",
        action="store_true",
        help="Translation service test (Gemini + Google Translate fallback)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Run all tests in sequence",
    )
    return p


async def async_main() -> None:
    args = build_parser().parse_args()

    print()
    header("iGOT Deterministic Chatbot — Local E2E Test Suite")
    print(f"  IGOT_ENV:    {settings.igot_env}")
    print(f"  GCP project: {settings.google_project_id or '(not set)'}")
    print(f"  Zoho org:    {settings.zoho_org_id or '(not set)'}")
    print(f"  Karmayogi:   {settings.karmayogi_portal_base_url}")
    print()

    any_specific = any([args.karmayogi, args.zoho, args.translate])

    # Run the flow if: --auto, --all, or no specific flag was given
    run_flow = args.auto or args.all or not any_specific

    if run_flow:
        if args.auto or args.all:
            # Scripted path: C2 (download fails) → yes (fixed) → satisfied
            script = [
                ("choice", "C2"),   # What's the issue? → Download fails
                ("choice", "yes"),  # Did it work? → Yes, downloaded!
            ]
            await run_flow_scenario(auto_script=script)
        else:
            # Interactive mode: user chooses at each step
            await run_flow_scenario(auto_script=None)

    if args.karmayogi or args.all:
        await run_karmayogi_test()

    if args.translate or args.all:
        await run_translation_test()

    if args.zoho:
        # Zoho creates real tickets — only on explicit --zoho flag
        await run_zoho_test()

    print("\nDone. ✅\n")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n\nAborted by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
