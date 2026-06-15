#!/usr/bin/env python3
"""
iGOT Deterministic Chatbot — LLM-as-Judge Flow Test Runner
===========================================================
Exhaustively walks every user-choice path through a YAML flow, simulates each
conversation against the live LangGraph engine (with real services), then asks
Claude to act as a QA judge and evaluate quality. Produces a standalone HTML
report with pass/warn/fail verdicts, response samples, and fix suggestions.

Usage (run from project root directory):

    # Test a single flow
    python scripts/llm_judge_runner.py --flow LEADERBOARD_ISSUE

    # Test all active flows
    python scripts/llm_judge_runner.py --all

    # Custom output path
    python scripts/llm_judge_runner.py --flow LOGIN_ISSUE --output reports/login.html

    # Dry run — extract paths and print them without running conversations
    python scripts/llm_judge_runner.py --flow CERTIFICATE_DOWNLOAD --dry-run

Requires ANTHROPIC_API_KEY in .env (or environment).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import html
import json
import os
import re
import sys
import textwrap
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

# ── Bootstrap ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent   # project root
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ruamel.yaml import YAML as _YAML                          # noqa: E402
from langgraph.checkpoint.memory import MemorySaver            # noqa: E402

from app.config import settings                                # noqa: E402
from app.engine.compiler import FlowCompiler                   # noqa: E402
from app.engine.state import initial_state                     # noqa: E402
from app.services.registry import ServiceRegistry             # noqa: E402

_yaml = _YAML(typ="safe", pure=True)

TEST_USER_ID: str = os.getenv("IGOT_TEST_USER_ID", "")
FLOWS_DIR  = ROOT / "flows"
SOPS_DIR   = ROOT.parent / "reference" / "SOPs_md"
REPORTS_DIR = ROOT / "test_reports"
REPORTS_DIR.mkdir(exist_ok=True)

# ── Flow → SOP file(s) mapping ────────────────────────────────────────────────
# The judge uses SOP content as ground truth, not the YAML
FLOW_TO_SOPS: dict[str, list[str]] = {
    "CERTIFICATE_DOWNLOAD":  [
        "Certificate is Not Generated.txt",
        "Incorrect Name in Certificate.txt",
    ],
    "COURSE_PROGRESS_ISSUE": [
        "Course _ Program _ Event Progress Issue.txt",
    ],
    "EMAIL_MOBILE_UPDATE":   [
        "Multiple Account Issue.txt",       # SOP content is actually email/mobile update
    ],
    "LEADERBOARD_ISSUE":     [
        "Leaderboard _ Top Karmayogi Dashboard Not Updated _ Not Displayed (1).txt",
    ],
    "BULK_PROFILE_UPDATE":   [
        "Bulk Profile Update.txt",
    ],
    "DOWNLOAD_REPORT_ISSUE": [
        "Use Case – Unable to Download Reports.txt",
    ],
    "PROFILE_COMPLETION":    [
        "Profile Update Document (SOP)_Updated.txt",
    ],
    "PROFILE_VERIFICATION":  [
        "Profile Verification Flow – Designation or Group Not Verified.txt",
    ],
    "RESOURCE_NOT_OPENING":  [
        "Resource _ Content Not Opening_Updated.txt",
    ],
    # LOGIN_ISSUE — no dedicated SOP file; judge uses general principles
}


def _load_sop(flow_id: str) -> str:
    """Load SOP text for a flow. Returns empty string if not found."""
    filenames = FLOW_TO_SOPS.get(flow_id, [])
    parts: list[str] = []
    for fname in filenames:
        p = SOPS_DIR / fname
        if p.exists():
            parts.append(f"=== SOP: {fname} ===\n{p.read_text(encoding='utf-8', errors='replace')}")
        else:
            # Try case-insensitive / partial match
            matches = [f for f in SOPS_DIR.glob("*.txt")
                       if fname.lower()[:20] in f.name.lower()]
            if matches:
                parts.append(f"=== SOP: {matches[0].name} ===\n"
                              f"{matches[0].read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(parts)

# ── Flow → topic choice mapping (mirrors routes.py) ───────────────────────────
FLOW_TO_TOPIC: dict[str, str] = {
    "CERTIFICATE_DOWNLOAD":  "CERT_HELP",
    "LOGIN_ISSUE":           "LOGIN_HELP",
    "PROFILE_COMPLETION":    "PROFILE_HELP",
    "COURSE_PROGRESS_ISSUE": "PROGRESS_HELP",
    "RESOURCE_NOT_OPENING":  "RESOURCE_HELP",
    "EMAIL_MOBILE_UPDATE":   "EMAIL_MOBILE_HELP",
    "PROFILE_VERIFICATION":  "VERIFY_HELP",
    "LEADERBOARD_ISSUE":     "LEADERBOARD_HELP",
    "BULK_PROFILE_UPDATE":   "BULK_PROFILE_HELP",
    "DOWNLOAD_REPORT_ISSUE": "REPORT_HELP",
}

TERMINAL_STATUSES = frozenset({"satisfied", "ticket_raised", "ended", "error"})

# ── Test value heuristics for collect nodes ───────────────────────────────────
def _test_value_for_field(node: dict[str, Any]) -> str:
    """Generate a plausible test value for a collect node's field."""
    fld   = node.get("field") or {}
    name  = (fld.get("name") or "").lower()
    ftype = (fld.get("type") or "text").lower()
    pat   = (fld.get("validation") or {}).get("pattern", "")

    if ftype == "email" or "email" in name:
        return "officer.test@ias.gov.in"
    if "mobile" in name or "phone" in name or (pat and "9][0-9]{9}" in pat):
        return "9876543210"
    if "otp" in name:
        return "123456"
    if "course" in name:
        return "Leadership and Management"
    if "name" in name:
        return "Suresh Kumar"
    return "automated-test-input"


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UserAction:
    """One pre-determined user action in a test scenario."""
    node_id: str
    action_type: str     # "quick_reply" | "text" | "item"
    value: str
    label: str           # human-readable (for path name + report)


@dataclass
class TestScenario:
    """A complete scripted path through a flow."""
    flow_id: str
    name: str            # human-readable path description
    actions: list[UserAction]


@dataclass
class ConversationTurn:
    """One turn in a recorded conversation."""
    speaker: str         # "bot" | "user"
    node_id: str | None
    content: str         # rendered text
    raw: dict[str, Any]  # original activity dict


@dataclass
class ConversationRecord:
    """Full recorded simulation of one scenario."""
    scenario: TestScenario
    turns: list[ConversationTurn] = field(default_factory=list)
    terminal_outcome: str = "unknown"
    error: str | None = None
    duration_s: float = 0.0


@dataclass
class JudgeVerdict:
    verdict: str         # "PASS" | "WARN" | "FAIL" | "ERROR"
    score: int           # 1–10
    issues: list[str]    # things that are wrong
    highlights: list[str]
    summary: str
    raw_response: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Path extractor
# ─────────────────────────────────────────────────────────────────────────────

class PathExtractor:
    """
    DFS through a compiled flow's node graph.

    Forks at every `message` node that has `quick_replies` (user-controlled
    branching). Treat `collect`, `api_call`, `branch`, `transfer_llm` as
    automatic (follows the "happy/primary" path in static analysis; actual
    runtime may deviate — the judge evaluates what actually happened).
    """

    def __init__(self, flow: dict[str, Any]) -> None:
        self.flow = flow
        self.node_map: dict[str, dict] = {n["id"]: n for n in flow.get("nodes", [])}

    def extract(self) -> list[TestScenario]:
        scenarios: list[TestScenario] = []
        entry = self.flow["entry_node"]

        def dfs(
            node_id: str,
            actions: list[UserAction],
            visited: frozenset[str],
            path_name: str,
        ) -> None:
            if node_id in visited:
                # Cycle — record as terminal
                scenarios.append(TestScenario(
                    flow_id=self.flow["flow_id"],
                    name=path_name + " [LOOP]",
                    actions=list(actions),
                ))
                return

            node = self.node_map.get(node_id)
            if not node:
                return   # fragment node not resolved — skip

            ntype   = node.get("type", "")
            visited = visited | {node_id}

            # ── TERMINAL ──────────────────────────────────────────────────────
            if ntype == "end":
                scenarios.append(TestScenario(
                    flow_id=self.flow["flow_id"],
                    name=path_name,
                    actions=list(actions),
                ))
                return

            # ── MESSAGE ───────────────────────────────────────────────────────
            if ntype == "message":
                qr_list  = node.get("quick_replies") or []
                on_reply = node.get("on_reply") or {}

                if qr_list and isinstance(on_reply, dict):
                    for qr in qr_list:
                        cid   = qr["id"]
                        label = qr.get("label", cid)
                        # Where does this choice route?
                        target = on_reply.get(cid) or on_reply.get("next")
                        if not target:
                            continue
                        # Skip control keys (save_to, next) handled separately
                        if isinstance(target, str) and target not in ("save_to",):
                            act = UserAction(node_id=node_id, action_type="quick_reply",
                                             value=cid, label=label)
                            dfs(target, actions + [act], visited,
                                path_name + f" → {label}")
                    return   # all branches explored from this node

                # Plain message with just a `next`
                nxt = node.get("next")
                if nxt:
                    dfs(nxt, actions, visited, path_name)
                return

            # ── COLLECT ───────────────────────────────────────────────────────
            if ntype == "collect":
                val = _test_value_for_field(node)
                act = UserAction(node_id=node_id, action_type="text",
                                 value=val, label=f'"{val}"')
                nxt = node.get("next")
                if nxt:
                    dfs(nxt, actions + [act], visited,
                        path_name + f" → [{val}]")
                return

            # ── BRANCH ────────────────────────────────────────────────────────
            # Follow DEFAULT path only — branch outcome depends on API data,
            # not user choice, so we don't fork here (avoids path explosion).
            if ntype == "branch":
                default = node.get("default")
                if default:
                    dfs(default, actions, visited, path_name)
                    return
                # No default — follow the first rule's then as fallback
                for rule in node.get("rules", []):
                    t = rule.get("then")
                    if t:
                        dfs(t, actions, visited, path_name)
                        return
                return

            # ── API_CALL ──────────────────────────────────────────────────────
            # Follow on_success only — actual outcome depends on live API.
            if ntype == "api_call":
                nxt = node.get("on_success")
                if nxt:
                    dfs(nxt, actions, visited, path_name)
                return

            # ── TRANSFER_LLM ─────────────────────────────────────────────────
            if ntype == "transfer_llm":
                on_complete = node.get("on_complete")
                if on_complete:
                    dfs(on_complete, actions, visited, path_name + " [→llm]")
                return

            # ── RESOLUTION ───────────────────────────────────────────────────
            if ntype == "resolution":
                # quick_replies can be at top level OR nested under follow_up
                follow_up = node.get("follow_up") or {}
                qr_list   = node.get("quick_replies") or follow_up.get("quick_replies") or []
                on_reply  = node.get("on_reply") or {}
                if qr_list and isinstance(on_reply, dict):
                    for qr in qr_list:
                        cid, label = qr["id"], qr.get("label", qr["id"])
                        target = on_reply.get(cid) or on_reply.get("next")
                        if target and isinstance(target, str):
                            act = UserAction(node_id=node_id, action_type="quick_reply",
                                             value=cid, label=label)
                            dfs(target, actions + [act], visited,
                                path_name + f" → {label}")
                    return
                nxt = node.get("next") or node.get("on_complete")
                if nxt:
                    dfs(nxt, actions, visited, path_name)
                return

            # ── FALLTHROUGH: follow `next` ────────────────────────────────────
            nxt = (
                node.get("next")
                or node.get("on_success")
                or node.get("on_complete")
            )
            if nxt:
                dfs(nxt, actions, visited, path_name)

        dfs(entry, [], frozenset(), self.flow["flow_id"])

        # Deduplicate scenarios that share identical action sequences
        seen_actions: set[str] = set()
        unique: list[TestScenario] = []
        for sc in scenarios:
            key = "|".join(f"{a.node_id}:{a.value}" for a in sc.actions)
            if key not in seen_actions:
                seen_actions.add(key)
                unique.append(sc)

        return unique

    def extract_capped(self, max_scenarios: int = 60) -> list[TestScenario]:
        """Extract paths, capping at max_scenarios to avoid branch explosion.

        When the cap is hit, we prefer scenarios with more user-choice actions
        (quick_reply paths) over pure branch/api paths, since those are more
        representative of what a real user would experience.
        """
        all_scenarios = self.extract()
        if len(all_scenarios) <= max_scenarios:
            return all_scenarios

        # Sort: prefer scenarios with more quick_reply actions (user-driven paths)
        # then by shorter path (less noise from branch traversal)
        def priority(sc: TestScenario) -> tuple:
            qr_count = sum(1 for a in sc.actions if a.action_type == "quick_reply")
            return (-qr_count, len(sc.actions))

        sorted_sc = sorted(all_scenarios, key=priority)
        selected  = sorted_sc[:max_scenarios]
        print(f"  ⚠️  {len(all_scenarios)} paths extracted — capped to {max_scenarios} "
              f"(use --max-scenarios N to change)")
        return selected


# ─────────────────────────────────────────────────────────────────────────────
# 2. Conversation runner
# ─────────────────────────────────────────────────────────────────────────────

def _activities_to_text(activities: list[dict]) -> str:
    """Flatten a list of bot activities into readable text for the transcript."""
    parts: list[str] = []
    for act in activities:
        t = act.get("type", "")
        if t in ("text", "markdown"):
            parts.append(act.get("content", ""))
        elif t == "quick_replies":
            choices = [f"[{c['id']}] {c['label']}" for c in act.get("choices", [])]
            parts.append("Options: " + " | ".join(choices))
        elif t == "picker":
            items = [f"[{i['id']}] {i['label']}" for i in (act.get("items") or [])[:5]]
            extra = " …" if len(act.get("items") or []) > 5 else ""
            parts.append("Pick: " + ", ".join(items) + extra)
        elif t == "input":
            parts.append(f"[Input field: {act.get('input_id', 'text')}]")
        elif t == "end":
            parts.append(f"[Ended: {act.get('outcome', 'ended')}]")
    return "\n".join(p for p in parts if p)


def _get_node_config(flow: dict, node_id: str | None) -> dict:
    if not node_id:
        return {}
    for n in flow.get("nodes", []):
        if n["id"] == node_id:
            return n
    return {}


def _get_save_to(flow: dict, node_id: str | None) -> str | None:
    node = _get_node_config(flow, node_id)
    on_reply = node.get("on_reply") or {}
    if isinstance(on_reply, dict):
        val = (on_reply.get("save_to") or "").removeprefix("collected.")
        return val or None
    return None


def _get_qr_choices(node: dict) -> list[dict]:
    """Return quick_reply list from a node, handling follow_up nesting."""
    top = node.get("quick_replies") or []
    if top:
        return top
    follow_up = node.get("follow_up") or {}
    return follow_up.get("quick_replies") or []


def _get_on_reply(node: dict) -> dict:
    return node.get("on_reply") or {}


def _get_collect_field(flow: dict, node_id: str | None) -> str | None:
    node = _get_node_config(flow, node_id)
    return ((node.get("field") or {}).get("name") or "").removeprefix("collected.") or None


class ConversationRunner:
    """Run a TestScenario through the LangGraph engine and record the transcript."""

    def __init__(self, flow: dict, graph: Any, services: Any) -> None:
        self.flow     = flow
        self.graph    = graph
        self.services = services

    async def run(self, scenario: TestScenario) -> ConversationRecord:
        record = ConversationRecord(scenario=scenario)
        t0     = asyncio.get_event_loop().time()

        session_id = uuid4()
        config     = {"configurable": {"thread_id": str(session_id)}}
        state      = initial_state(
            session_id=session_id,
            user_id_hash=TEST_USER_ID,
            channel="web",
            language="en",
            session_ttl_minutes=30,
        )
        state_dict            = state.model_dump(mode="json")
        state_dict["flow_id"] = self.flow["flow_id"]

        # Pointer into the scenario's action list — advanced when we consume
        action_idx = 0
        max_turns  = 40   # safety limit

        try:
            result = await self.graph.ainvoke(state_dict, config)
        except Exception as exc:
            record.error = f"Initial invoke failed: {exc}"
            record.terminal_outcome = "error"
            return record

        turn = 0
        while turn < max_turns:
            activities = result.get("pending_activities") or []
            current    = result.get("current_node")
            status     = result.get("status", "active")

            # Record bot turn
            bot_text = _activities_to_text(activities)
            record.turns.append(ConversationTurn(
                speaker="bot", node_id=current,
                content=bot_text, raw={"activities": activities},
            ))

            # status may be a FlowStatus enum or a plain string
            status_str = status.value if hasattr(status, "value") else str(status)
            if status_str in TERMINAL_STATUSES:
                record.terminal_outcome = status_str
                break
            # Also handle "FlowStatus.SATISFIED" etc. as strings
            if any(ts in status_str for ts in TERMINAL_STATUSES):
                record.terminal_outcome = next(ts for ts in TERMINAL_STATUSES
                                               if ts in status_str)
                break

            # Find the action for this node from the scenario script
            # We match by node_id if possible, otherwise take next in order
            action: UserAction | None = None
            if action_idx < len(scenario.actions):
                candidate = scenario.actions[action_idx]
                if candidate.node_id == current or action_idx == 0:
                    action = candidate
                    action_idx += 1
                else:
                    # Look ahead in remaining actions for a match
                    for i in range(action_idx, len(scenario.actions)):
                        if scenario.actions[i].node_id == current:
                            action = scenario.actions[i]
                            action_idx = i + 1
                            break

            # Build state update based on activity types in bot response
            has_qr     = any(a.get("type") == "quick_replies" for a in activities)
            has_picker = any(a.get("type") == "picker"        for a in activities)
            has_input  = any(a.get("type") == "input"         for a in activities)

            # If no activity-based interaction detected but current node has quick_replies
            # (e.g. resolution with follow_up), treat it as quick_replies
            if not has_qr and not has_picker and not has_input:
                node_cfg = _get_node_config(self.flow, current)
                if _get_qr_choices(node_cfg):
                    has_qr = True

            update: dict[str, Any] = {"pending_activities": []}
            collected = dict(result.get("collected") or {})

            if has_qr and action and action.action_type == "quick_reply":
                choice_id = action.value
                collected["_last_choice_id"] = choice_id
                save_to = _get_save_to(self.flow, current)
                if save_to:
                    collected[save_to] = choice_id
                update["collected"] = collected
                record.turns.append(ConversationTurn(
                    speaker="user", node_id=current,
                    content=f"[Chose: {choice_id} — {action.label}]",
                    raw={"choice_id": choice_id},
                ))

            elif has_picker and action and action.action_type == "item":
                item_id = action.value
                collected["_last_choice_id"] = item_id
                fname = _get_collect_field(self.flow, current)
                if fname:
                    collected[fname] = item_id
                update["collected"] = collected
                record.turns.append(ConversationTurn(
                    speaker="user", node_id=current,
                    content=f"[Selected item: {item_id}]",
                    raw={"item_id": item_id},
                ))

            elif has_picker:
                # Picker present but no action assigned — auto-pick first item
                picker_act = next(a for a in activities if a.get("type") == "picker")
                items = picker_act.get("items") or []
                if items:
                    item_id = items[0]["id"]
                    collected["_last_choice_id"] = item_id
                    fname = _get_collect_field(self.flow, current)
                    if fname:
                        collected[fname] = item_id
                    update["collected"] = collected
                    record.turns.append(ConversationTurn(
                        speaker="user", node_id=current,
                        content=f"[Auto-selected first item: {item_id} — {items[0]['label']}]",
                        raw={"item_id": item_id, "auto": True},
                    ))
                else:
                    # Empty picker — can't proceed
                    record.turns.append(ConversationTurn(
                        speaker="user", node_id=current,
                        content="[Picker empty — no items to select]",
                        raw={},
                    ))
                    record.terminal_outcome = "error"
                    break

            elif has_input and action and action.action_type == "text":
                text_val = action.value
                fname = _get_collect_field(self.flow, current)
                if fname:
                    collected[fname] = text_val
                update["collected"] = collected
                record.turns.append(ConversationTurn(
                    speaker="user", node_id=current,
                    content=f"[Typed: {text_val}]",
                    raw={"text": text_val},
                ))

            elif has_input:
                # Input expected but no action — use generic value
                fname = _get_collect_field(self.flow, current)
                val = "automated-test-input"
                if fname:
                    collected[fname] = val
                update["collected"] = collected
                record.turns.append(ConversationTurn(
                    speaker="user", node_id=current,
                    content=f"[Auto-typed: {val}]",
                    raw={"text": val, "auto": True},
                ))

            else:
                # No interaction needed — just resume
                record.turns.append(ConversationTurn(
                    speaker="user", node_id=current,
                    content="[Continue / no input needed]",
                    raw={},
                ))

            # Resume graph
            await self.graph.aupdate_state(config, update)
            try:
                result = await self.graph.ainvoke(None, config)
            except Exception as exc:
                record.error = f"ainvoke failed at turn {turn}: {exc}\n{traceback.format_exc()}"
                record.terminal_outcome = "error"
                break

            turn += 1

        if turn >= max_turns and record.terminal_outcome == "unknown":
            record.terminal_outcome = "timeout"
            record.error = f"Reached {max_turns}-turn safety limit without reaching terminal"

        record.duration_s = asyncio.get_event_loop().time() - t0
        return record


# ─────────────────────────────────────────────────────────────────────────────
# 3. LLM judge
# ─────────────────────────────────────────────────────────────────────────────

_JUDGE_SYSTEM = """\
You are a senior QA engineer reviewing automated test runs of the iGOT Deterministic Chatbot, an iGOT
Karmayogi government learning-platform support chatbot built with LangGraph.

You will be given:
1. The SOP document(s) for this use case — this is the GROUND TRUTH.
   Judge correctness against the SOP, not against the YAML flow.
   If the bot's behavior contradicts the SOP, that is a FAIL.
2. A conversation transcript from an automated test simulation.

Evaluation criteria (score each 1–10):
1. SOP_COMPLIANCE       — Steps/guidance match the SOP exactly. Any deviation = issue.
2. TEMPLATE_RESOLUTION  — No {{ }} placeholders visible in any bot message.
3. ROUTING_ACCURACY     — User choice led to the correct SOP-defined next step.
4. MESSAGE_QUALITY      — Messages are clear, grammatically correct, concise.
5. TERMINAL_CORRECTNESS — Final status (satisfied/ticket_raised) is appropriate per SOP.

Overall verdict rules:
- PASS  : All criteria >= 7. No unresolved templates. Follows SOP intent.
- WARN  : Any criterion 5-6, or minor deviation from SOP, or mild message issue.
- FAIL  : Contradicts SOP, unresolved {{ }}, wrong terminal, dead end, or criterion < 5.

If no SOP is provided, judge based on general customer support quality standards.

Respond ONLY with a JSON object — no prose outside JSON:
{
  "verdict": "PASS" or "WARN" or "FAIL",
  "score": <1-10 overall>,
  "issues": ["specific issue 1", ...],
  "highlights": ["thing done well 1", ...],
  "summary": "one sentence describing overall quality"
}
"""

_JUDGE_USER_PREFIX = "Please evaluate this iGOT Deterministic Chatbot conversation against the SOP and criteria above.\n\n"


def _detect_judge_provider() -> str:
    """
    Detect which LLM provider to use for the judge.

    Priority:
    1. JUDGE_LLM_PROVIDER env var (explicit override: "gemini" | "anthropic")
    2. GOOGLE_API_KEY present → "gemini" (direct Gemini API)
    3. GOOGLE_PROJECT_ID set in settings (Vertex AI) → "gemini_vertex"
    4. ANTHROPIC_API_KEY present → "anthropic"
    """
    explicit = os.getenv("JUDGE_LLM_PROVIDER", "").lower()
    if explicit in ("gemini", "anthropic", "gemini_vertex"):
        return explicit

    if os.getenv("GOOGLE_API_KEY"):
        return "gemini"

    if getattr(settings, "google_project_id", ""):
        return "gemini_vertex"

    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"

    # Default — let the call fail gracefully if nothing is configured
    return "anthropic"


class LLMJudge:
    """
    LLM-as-judge supporting both Gemini (direct API or Vertex AI) and Anthropic.

    Provider auto-detected from environment:
    - GOOGLE_API_KEY          → Gemini direct API (google-generativeai SDK)
    - GOOGLE_PROJECT_ID       → Vertex AI Gemini (google-genai SDK, same as the app)
    - ANTHROPIC_API_KEY       → Anthropic Claude
    - JUDGE_LLM_PROVIDER=...  → explicit override ("gemini" | "gemini_vertex" | "anthropic")
    """

    def __init__(self) -> None:
        self.provider = _detect_judge_provider()
        self.model    = os.getenv("JUDGE_MODEL", "")   # optional override
        print(f"  LLM judge provider: {self.provider}")

    def _build_conversation(self, record: ConversationRecord) -> str:
        sc  = record.scenario
        sop = _load_sop(sc.flow_id)

        lines: list[str] = []

        # SOP reference (ground truth)
        if sop:
            lines += ["--- SOP REFERENCE (GROUND TRUTH) ---", sop, "--- END SOP ---", ""]
        else:
            lines += ["--- SOP REFERENCE: (none available — use general QA judgment) ---", ""]

        # Conversation
        lines += [
            f"FLOW: {sc.flow_id}",
            f"SCENARIO: {sc.name}",
            f"TERMINAL: {record.terminal_outcome}",
            f"RUNTIME ERROR: {record.error or 'none'}",
            "",
            "CONVERSATION TRANSCRIPT:",
            "-" * 60,
        ]
        for t in record.turns:
            prefix = "BOT" if t.speaker == "bot" else "USER"
            node   = f" [{t.node_id}]" if t.node_id else ""
            lines.append(f"{prefix}{node}:")
            for ln in t.content.splitlines():
                lines.append(f"   {ln}")
            lines.append("")
        lines.append("-" * 60)
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> JudgeVerdict:
        m    = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group() if m else raw)
        return JudgeVerdict(
            verdict=data.get("verdict", "WARN"),
            score=int(data.get("score", 5)),
            issues=data.get("issues") or [],
            highlights=data.get("highlights") or [],
            summary=data.get("summary", ""),
            raw_response=raw,
        )

    def _sync_gemini_direct(self, conversation: str) -> str:
        """Direct Gemini API — called via asyncio.to_thread."""
        try:
            import google.generativeai as _gen
            _gen.configure(api_key=os.getenv("GOOGLE_API_KEY"))
            model_name = self.model or os.getenv("GENAI_MODEL_NAME", "gemini-2.0-flash")
            model = _gen.GenerativeModel(model_name=model_name,
                                         system_instruction=_JUDGE_SYSTEM)
            return model.generate_content(_JUDGE_USER_PREFIX + conversation).text.strip()
        except ImportError:
            pass
        from google import genai as _genai
        client = _genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        model_name = self.model or os.getenv("GENAI_MODEL_NAME", "gemini-2.0-flash")
        return client.models.generate_content(
            model=model_name,
            contents=_JUDGE_SYSTEM + "\n\n" + _JUDGE_USER_PREFIX + conversation,
        ).text.strip()

    def _sync_gemini_vertex(self, conversation: str) -> str:
        """Vertex AI Gemini — called via asyncio.to_thread."""
        from google import genai as _genai
        client = _genai.Client(
            vertexai=True,
            project=settings.google_project_id,
            location=getattr(settings, "google_location", "us-central1"),
        )
        model_name = self.model or settings.genai_model_name
        return client.models.generate_content(
            model=model_name,
            contents=_JUDGE_SYSTEM + "\n\n" + _JUDGE_USER_PREFIX + conversation,
        ).text.strip()

    def _sync_anthropic(self, conversation: str) -> str:
        """Anthropic Claude — called via asyncio.to_thread."""
        import anthropic as _ant
        client = _ant.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        model_name = self.model or "claude-haiku-4-5-20251001"
        msg = client.messages.create(
            model=model_name, max_tokens=512, system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": _JUDGE_USER_PREFIX + conversation}],
        )
        return msg.content[0].text.strip()

    async def judge(self, record: ConversationRecord) -> JudgeVerdict:
        """Async judge — calls the appropriate LLM in a thread pool."""
        if record.terminal_outcome == "error" and record.error:
            return JudgeVerdict(
                verdict="FAIL", score=1,
                issues=[f"Runtime error: {record.error}"],
                highlights=[],
                summary="Conversation crashed before completing.",
            )

        conversation = self._build_conversation(record)
        sync_fn = {
            "gemini":         self._sync_gemini_direct,
            "gemini_vertex":  self._sync_gemini_vertex,
            "anthropic":      self._sync_anthropic,
        }.get(self.provider, self._sync_anthropic)

        try:
            raw = await asyncio.to_thread(sync_fn, conversation)
            return self._parse_response(raw)
        except Exception as exc:
            return JudgeVerdict(
                verdict="WARN", score=5,
                issues=[f"Judge LLM call failed: {exc}"],
                highlights=[],
                summary="Could not evaluate — judge error.",
                raw_response=str(exc),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. HTML report generator
# ─────────────────────────────────────────────────────────────────────────────

_VERDICT_COLOR = {"PASS": "#22c55e", "WARN": "#f59e0b", "FAIL": "#ef4444", "ERROR": "#9ca3af"}
_VERDICT_BG    = {"PASS": "#f0fdf4", "WARN": "#fffbeb", "FAIL": "#fef2f2", "ERROR": "#f9fafb"}


def _esc(s: str) -> str:
    return html.escape(str(s))


def _verdict_badge(v: str) -> str:
    c = _VERDICT_COLOR.get(v, "#6b7280")
    return (
        f'<span style="background:{c};color:#fff;padding:2px 10px;'
        f'border-radius:12px;font-weight:700;font-size:0.85em">{_esc(v)}</span>'
    )


def _score_bar(score: int) -> str:
    pct = score * 10
    col = "#22c55e" if score >= 8 else "#f59e0b" if score >= 6 else "#ef4444"
    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:80px;display:inline-block;vertical-align:middle">'
        f'<div style="background:{col};height:100%;width:{pct}%;border-radius:4px"></div></div> '
        f'<span style="font-size:0.9em;color:#6b7280">{score}/10</span>'
    )


def generate_html_report(
    flow_id: str,
    records: list[ConversationRecord],
    verdicts: list[JudgeVerdict],
    duration_total: float,
) -> str:
    now      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total    = len(records)
    n_pass   = sum(1 for v in verdicts if v.verdict == "PASS")
    n_warn   = sum(1 for v in verdicts if v.verdict == "WARN")
    n_fail   = sum(1 for v in verdicts if v.verdict in ("FAIL", "ERROR"))
    avg_sc   = round(sum(v.score for v in verdicts) / total, 1) if total else 0
    pass_pct = round(100 * n_pass / total) if total else 0

    # Summary bar colour
    bar_col  = "#22c55e" if pass_pct >= 80 else "#f59e0b" if pass_pct >= 60 else "#ef4444"

    rows = []
    for i, (rec, ver) in enumerate(zip(records, verdicts), 1):
        rows.append(f"""
        <tr onclick="toggle('sc{i}')" style="cursor:pointer;border-bottom:1px solid #e5e7eb">
          <td style="padding:8px 12px;font-weight:600">#{i}</td>
          <td style="padding:8px 12px;font-size:0.85em;color:#374151">{_esc(rec.scenario.name)}</td>
          <td style="padding:8px 12px">{_esc(rec.terminal_outcome)}</td>
          <td style="padding:8px 12px">{_verdict_badge(ver.verdict)}</td>
          <td style="padding:8px 12px">{_score_bar(ver.score)}</td>
          <td style="padding:8px 12px;font-size:0.85em;color:#6b7280">{rec.duration_s:.1f}s</td>
          <td style="padding:8px 12px;font-size:0.85em;color:#374151">{_esc(ver.summary[:80])}</td>
        </tr>""")

    detail_sections = []
    for i, (rec, ver) in enumerate(zip(records, verdicts), 1):
        # Build transcript HTML
        transcript_rows = []
        for t in rec.turns:
            if t.speaker == "bot":
                bg, align = "#f9fafb", "left"
                label = f'🤖 <strong>Bot</strong> [{_esc(t.node_id or "")}]'
            else:
                bg, align = "#eff6ff", "right"
                label = "👤 <strong>User</strong>"
            transcript_rows.append(f"""
            <div style="background:{bg};border-radius:8px;padding:10px 14px;
                        margin:4px 0;text-align:{align}">
              <div style="font-size:0.78em;color:#6b7280;margin-bottom:4px">{label}</div>
              <div style="font-size:0.9em;white-space:pre-wrap">{_esc(t.content)}</div>
            </div>""")

        issues_html = "".join(
            f'<li style="color:#ef4444;margin:2px 0">⚠ {_esc(i)}</li>'
            for i in ver.issues
        ) or '<li style="color:#9ca3af">— none —</li>'

        highlights_html = "".join(
            f'<li style="color:#22c55e;margin:2px 0">✓ {_esc(h)}</li>'
            for h in ver.highlights
        ) or '<li style="color:#9ca3af">— none —</li>'

        err_section = (
            f'<div style="background:#fee2e2;border-radius:6px;padding:8px 12px;'
            f'margin:8px 0;font-size:0.85em;color:#991b1b">Runtime error: {_esc(rec.error)}</div>'
            if rec.error else ""
        )

        vbg = _VERDICT_BG.get(ver.verdict, "#f9fafb")
        detail_sections.append(f"""
        <div id="sc{i}" style="display:none;background:#fff;border:1px solid #e5e7eb;
                                border-radius:10px;margin:10px 0;padding:16px">
          <h3 style="margin:0 0 8px;font-size:1em;color:#1f2937">
            #{i} {_esc(rec.scenario.name)}
          </h3>
          {err_section}
          <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px">
            <div style="background:{vbg};border-radius:8px;padding:10px 16px;flex:1;min-width:200px">
              <div style="font-size:0.75em;color:#6b7280;margin-bottom:4px">VERDICT</div>
              <div>{_verdict_badge(ver.verdict)} {_score_bar(ver.score)}</div>
              <div style="margin-top:6px;font-size:0.85em;color:#374151">{_esc(ver.summary)}</div>
            </div>
            <div style="background:#f9fafb;border-radius:8px;padding:10px 16px;flex:1;min-width:200px">
              <div style="font-size:0.75em;color:#6b7280;margin-bottom:4px">ISSUES</div>
              <ul style="margin:0;padding-left:16px;font-size:0.85em">{issues_html}</ul>
            </div>
            <div style="background:#f9fafb;border-radius:8px;padding:10px 16px;flex:1;min-width:200px">
              <div style="font-size:0.75em;color:#6b7280;margin-bottom:4px">HIGHLIGHTS</div>
              <ul style="margin:0;padding-left:16px;font-size:0.85em">{highlights_html}</ul>
            </div>
          </div>
          <div style="font-size:0.75em;color:#6b7280;margin-bottom:6px">TRANSCRIPT
            ({len(rec.turns)} turns · {rec.duration_s:.1f}s · terminal: {_esc(rec.terminal_outcome)})</div>
          <div style="max-height:600px;overflow-y:auto;border:1px solid #e5e7eb;
                      border-radius:6px;padding:8px">
            {"".join(transcript_rows)}
          </div>
        </div>""")

    # Pre-build cards HTML (avoids nested f-string quoting issues on Python 3.10)
    card_items = [
        (str(n_pass),          "PASS",       "#22c55e"),
        (str(n_warn),          "WARN",       "#f59e0b"),
        (str(n_fail),          "FAIL",       "#ef4444"),
        (f"{avg_sc}/10",       "Avg Score",  "#3b82f6"),
        (f"{pass_pct}%",       "Pass Rate",  bar_col),
    ]
    cards_html = "".join(
        '<div style="background:#fff;border-radius:10px;padding:16px 24px;'
        'flex:1;min-width:130px;text-align:center">'
        '<div style="font-size:2em;font-weight:700;color:' + c + '">' + v + '</div>'
        '<div style="font-size:0.8em;color:#6b7280">' + l + '</div></div>'
        for (v, l, c) in card_items
    )
    rows_html     = "".join(rows)
    details_html  = "".join(detail_sections)
    user_display  = (TEST_USER_ID[:8] + "…") if TEST_USER_ID else "not set"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iGOT Deterministic Chatbot Flow Test — {_esc(flow_id)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background:#f3f4f6;color:#111827;margin:0;padding:24px }}
  table {{ width:100%;border-collapse:collapse;background:#fff;
           border-radius:10px;overflow:hidden }}
  th    {{ background:#f9fafb;color:#6b7280;font-size:0.78em;text-transform:uppercase;
           letter-spacing:.05em;padding:8px 12px;text-align:left }}
  tr:hover td {{ background:#f9fafb }}
</style>
</head>
<body>
<div style="max-width:1200px;margin:0 auto">

  <!-- Header -->
  <div style="background:#1e3a5f;color:#fff;border-radius:12px;padding:24px;margin-bottom:20px">
    <div style="font-size:0.8em;opacity:.7">iGOT Deterministic Chatbot LLM-as-Judge Test Report</div>
    <h1 style="margin:4px 0;font-size:1.6em">{_esc(flow_id)}</h1>
    <div style="font-size:0.85em;opacity:.8">Generated {now} · {total} scenarios · {duration_total:.1f}s total</div>
  </div>

  <!-- Summary cards -->
  <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
    {cards_html}
  </div>

  <!-- Pass rate bar -->
  <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:20px">
    <div style="font-size:0.8em;color:#6b7280;margin-bottom:6px">Overall Pass Rate</div>
    <div style="background:#e5e7eb;border-radius:6px;height:14px">
      <div style="background:{bar_col};height:100%;width:{pass_pct}%;border-radius:6px;
                  transition:width .3s"></div>
    </div>
  </div>

  <!-- Summary table -->
  <div style="background:#fff;border-radius:10px;overflow:hidden;margin-bottom:20px">
    <table>
      <thead><tr>
        <th>#</th><th>Scenario</th><th>Terminal</th><th>Verdict</th>
        <th>Score</th><th>Time</th><th>Summary</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <!-- Detail sections (toggle on row click) -->
  <div>{details_html}</div>

  <div style="text-align:center;color:#9ca3af;font-size:0.8em;margin-top:32px;padding-bottom:24px">
    iGOT Deterministic Chatbot LLM Judge · {now} · Test user: {_esc(user_display)}
  </div>
</div>

<script>
function toggle(id) {{
  var el = document.getElementById(id);
  el.style.display = (el.style.display === 'none') ? 'block' : 'none';
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_flow(
    flow_id: str,
    dry_run: bool = False,
    output: Path | None = None,
    max_scenarios: int = 60,
) -> tuple[int, int]:
    """Run all scenarios for one flow. Returns (pass_count, total_count)."""

    # Find the YAML file
    yaml_file = next(
        (f for f in FLOWS_DIR.glob("*.yaml")
         if _yaml.load(f.read_text(encoding="utf-8")).get("flow_id") == flow_id),
        None,
    )
    if not yaml_file:
        print(f"  ❌  No YAML found for flow_id={flow_id}")
        return 0, 0

    print(f"\n{'═'*64}")
    print(f"  Flow: {flow_id}  ({yaml_file.name})")
    print(f"{'═'*64}")

    # Load and compile
    services   = ServiceRegistry.from_env()
    checkpointer = MemorySaver()
    compiler   = FlowCompiler(services=services)
    flow       = compiler.load_flow(yaml_file)
    graph      = compiler.compile_flow(flow, checkpointer=checkpointer)

    # Extract scenarios
    extractor  = PathExtractor(flow)
    scenarios  = extractor.extract_capped(max_scenarios=max_scenarios)
    print(f"  Extracted {len(scenarios)} scenario path(s)")

    if dry_run:
        for i, sc in enumerate(scenarios, 1):
            print(f"    {i:>3}. {sc.name}")
            for a in sc.actions:
                print(f"         [{a.action_type}] {a.node_id} → {a.value!r}")
        await services.aclose()
        return 0, len(scenarios)

    print(f"  Running conversations + LLM judge …\n")

    runner = ConversationRunner(flow, graph, services)
    judge  = LLMJudge()

    records:  list[ConversationRecord] = []
    verdicts: list[JudgeVerdict]       = []
    t_start = asyncio.get_event_loop().time()

    for i, sc in enumerate(scenarios, 1):
        print(f"  [{i:>2}/{len(scenarios)}] {sc.name[:70]} ", end="", flush=True)
        rec = await runner.run(sc)
        ver = await judge.judge(rec)
        records.append(rec)
        verdicts.append(ver)

        icon = "✅" if ver.verdict == "PASS" else "⚠️" if ver.verdict == "WARN" else "❌"
        print(f"{icon} {ver.verdict}  ({ver.score}/10)  terminal={rec.terminal_outcome}")
        if ver.issues:
            for issue in ver.issues[:2]:
                print(f"          ↳ {issue}")

    duration = asyncio.get_event_loop().time() - t_start

    # Generate report
    out_path = output or REPORTS_DIR / f"{flow_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    html_content = generate_html_report(flow_id, records, verdicts, duration)
    out_path.write_text(html_content, encoding="utf-8")

    n_pass = sum(1 for v in verdicts if v.verdict == "PASS")
    n_warn = sum(1 for v in verdicts if v.verdict == "WARN")
    n_fail = len(verdicts) - n_pass - n_warn

    print(f"\n  {'─'*60}")
    print(f"  Results: ✅ {n_pass} PASS  ⚠️  {n_warn} WARN  ❌ {n_fail} FAIL  ({duration:.1f}s)")
    print(f"  Report:  {out_path}")

    await services.aclose()
    return n_pass, len(scenarios)


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--flow", metavar="FLOW_ID",
        help="Run tests for a single flow (e.g. LEADERBOARD_ISSUE)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run tests for all active flows",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extract and print paths without running conversations",
    )
    parser.add_argument(
        "--output", metavar="FILE", type=Path,
        help="Custom output HTML path (default: test_reports/<FLOW_ID>_<ts>.html)",
    )
    parser.add_argument(
        "--max-scenarios", metavar="N", type=int, default=60,
        help="Max scenarios to test per flow (default 60; use 0 for unlimited)",
    )
    args = parser.parse_args()

    if not args.flow and not args.all:
        parser.print_help()
        return

    flows_to_test: list[str] = []
    if args.flow:
        flows_to_test = [args.flow.upper()]
    else:
        # Discover all active flow_ids from YAML files
        for yf in sorted(FLOWS_DIR.glob("*.yaml")):
            try:
                data = _yaml.load(yf.read_text(encoding="utf-8"))
                fid  = data.get("flow_id")
                if fid:
                    flows_to_test.append(fid)
            except Exception:
                pass

    print(f"\niGOT Deterministic Chatbot LLM-as-Judge Test Runner")
    print(f"Test user: {TEST_USER_ID[:8]}…" if TEST_USER_ID else "⚠️  IGOT_TEST_USER_ID not set in .env")
    print(f"Flows to test: {', '.join(flows_to_test)}")

    max_sc = args.max_scenarios if args.max_scenarios > 0 else 999_999
    total_pass = total_all = 0
    for fid in flows_to_test:
        p, t = await run_flow(fid, dry_run=args.dry_run,
                              output=args.output, max_scenarios=max_sc)
        total_pass += p
        total_all  += t

    if not args.dry_run and len(flows_to_test) > 1:
        pct = round(100 * total_pass / total_all) if total_all else 0
        print(f"\n{'═'*64}")
        print(f"  OVERALL: {total_pass}/{total_all} scenarios passed ({pct}%)")
        print(f"{'═'*64}\n")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
