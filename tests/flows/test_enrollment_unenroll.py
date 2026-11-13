"""Unenroll sub-flow tests (KB-16292).

Covers the changes to flows/mode_b_enrollment_issues.yaml: the Course vs
Program/Event question and the course-specific unenroll steps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.compiler import FlowCompiler

FLOW_PATH = Path(__file__).resolve().parents[2] / "flows" / "mode_b_enrollment_issues.yaml"


@pytest.fixture(scope="module")
def flow():
    return FlowCompiler(services={}).load_flow(FLOW_PATH)


@pytest.fixture(scope="module")
def nodes(flow):
    return {n["id"]: n for n in flow["nodes"]}


def _reply_ids(node):
    return [q["id"] for q in node["quick_replies"]]


class TestEntry:
    def test_unenroll_routes_to_content_type_question(self, nodes):
        assert nodes["ask_enrollment_issue_type"]["on_reply"]["unenroll"] == "ask_unenroll_content_type"

    def test_find_enroll_path_unchanged(self, nodes):
        assert nodes["ask_enrollment_issue_type"]["on_reply"]["find_enroll"] == "ask_marketplace_question"


class TestContentTypeQuestion:
    def test_has_exactly_two_options(self, nodes):
        assert _reply_ids(nodes["ask_unenroll_content_type"]) == ["course", "program_event"]

    def test_routing(self, nodes):
        on_reply = nodes["ask_unenroll_content_type"]["on_reply"]
        assert on_reply["course"] == "inform_unenroll_course"
        assert on_reply["program_event"] == "inform_unavailable"

    def test_selection_is_saved(self, nodes):
        assert nodes["ask_unenroll_content_type"]["on_reply"]["save_to"] == "collected.unenroll_content_type"

    def test_free_text_disabled(self, nodes):
        assert nodes["ask_unenroll_content_type"]["disable_input"] is True

    def test_all_reply_targets_exist(self, nodes):
        on_reply = nodes["ask_unenroll_content_type"]["on_reply"]
        for key, target in on_reply.items():
            if key != "save_to":
                assert target in nodes, target


class TestCourseUnenroll:
    @pytest.fixture()
    def text(self, nodes):
        return nodes["inform_unenroll_course"]["prompt"]["text"]

    @pytest.mark.parametrize(
        "snippet",
        [
            "Log in to your iGOT Karmayogi account",
            "Locate the course you wish to unenroll from",
            "three dots",
            "Select the **Unenroll** option",
            "removed from your In-Progress/Continue Learning list",
            "reach out to us if you require any further assistance",
        ],
    )
    def test_text_contains_step(self, text, snippet):
        assert snippet in text

    def test_steps_are_in_order(self, text):
        positions = [text.index(f"{n}. ") for n in range(1, 5)]
        assert positions == sorted(positions)

    def test_no_stale_unavailable_wording(self, text):
        assert "no functionality" not in text

    def test_closes_without_ticket(self, nodes):
        node = nodes["inform_unenroll_course"]
        assert node["next"] == "unenroll_end"
        assert nodes["unenroll_end"]["type"] == "end"
        assert nodes["unenroll_end"]["outcome"] == "self_served"


class TestProgramEventUnenroll:
    @pytest.fixture()
    def text(self, nodes):
        return nodes["inform_unavailable"]["prompt"]["text"]

    def test_says_not_available_for_program_or_event(self, text):
        assert "no functionality available on our platform to unenroll" in text
        assert "program or event" in text

    def test_keeps_continue_or_ignore_guidance(self, text):
        assert "continue and complete" in text
        assert "ignore the enrolled content" in text

    def test_closes_without_ticket(self, nodes):
        assert nodes["inform_unavailable"]["next"] == "unenroll_end"
        assert nodes["unenroll_end"]["outcome"] == "self_served"
