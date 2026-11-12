"""Profile-completion flow tests (KB-16286).

Covers the new field-presence transforms in api_call_node and the changes made
to flows/mode_b_profile_completion.yaml (extended profile fetch, branch rules,
missing-field summary, re-fetch loop).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.compiler import FlowCompiler
from app.engine.expression import ExpressionEvaluator
from app.engine.nodes.api_call_node import (
    _TRANSFORMS,
    _has_complete_location,
    _has_items,
    _jsonpath_get,
    _non_blank,
)
from app.engine.template import render

FLOW_PATH = Path(__file__).resolve().parents[2] / "flows" / "mode_b_profile_completion.yaml"

HAS_FLAGS = [
    "has_name",
    "has_avatar",
    "has_banner",
    "has_about_me",
    "has_designation",
    "has_group",
    "has_location",
    "has_education",
    "has_service_history",
]


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


class TestNonBlank:
    @pytest.mark.parametrize("value", ["abc", " a ", "0", 0, 1, False, True, 5.5, [], {}, ["x"]])
    def test_present_values(self, value):
        assert _non_blank(value) is True

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_blank_values(self, value):
        assert _non_blank(value) is False


class TestHasItems:
    def test_non_empty_list(self):
        assert _has_items([{"a": 1}]) is True
        assert _has_items([None]) is True

    @pytest.mark.parametrize("value", [[], None, "abc", {"a": 1}, 0, ("x",)])
    def test_empty_or_not_a_list(self, value):
        assert _has_items(value) is False


class TestHasCompleteLocation:
    def test_complete_entry(self):
        assert _has_complete_location([{"state": "Kerala", "district": "Kochi"}]) is True

    def test_any_complete_entry_is_enough(self):
        entries = [
            {"state": "Kerala", "district": ""},
            {"state": "Goa", "district": "North Goa"},
        ]
        assert _has_complete_location(entries) is True

    @pytest.mark.parametrize(
        "entries",
        [
            [{"state": "Kerala"}],
            [{"district": "Kochi"}],
            [{"state": "Kerala", "district": ""}],
            [{"state": "  ", "district": "Kochi"}],
            [{"state": None, "district": None}],
            [{}],
            [],
        ],
    )
    def test_incomplete_entries(self, entries):
        assert _has_complete_location(entries) is False

    @pytest.mark.parametrize("entries", [None, "Kerala", {"state": "a", "district": "b"}, 5])
    def test_not_a_list(self, entries):
        assert _has_complete_location(entries) is False

    def test_ignores_non_dict_entries(self):
        assert _has_complete_location(["Kerala", None, 3]) is False
        assert _has_complete_location(["x", {"state": "A", "district": "B"}]) is True


def test_transforms_registered():
    assert _TRANSFORMS["non_blank"] is _non_blank
    assert _TRANSFORMS["has_items"] is _has_items
    assert _TRANSFORMS["has_complete_location"] is _has_complete_location


# ---------------------------------------------------------------------------
# Flow YAML
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def flow():
    return FlowCompiler(services={}).load_flow(FLOW_PATH)


@pytest.fixture(scope="module")
def nodes(flow):
    return {n["id"]: n for n in flow["nodes"]}


def _mappings(node):
    return {m["to"]: m for m in node["response_mapping"]}


def _targets(node):
    """All node ids a node can route to."""
    out = set()
    if "next" in node:
        out.add(node["next"])
    if "default" in node:
        out.add(node["default"])
    out.update((node.get("on_reply") or {}).values())
    out.update((node.get("on_error") or {}).values())
    if isinstance(node.get("on_success"), str):
        out.add(node["on_success"])
    for rule in node.get("rules") or []:
        out.add(rule["then"])
    # Some input nodes store a value in ctx instead of routing; not edges.
    return {t for t in out if isinstance(t, str) and not t.startswith("collected.")}


def test_flow_loads_and_all_targets_exist(flow, nodes):
    for node in flow["nodes"]:
        for target in _targets(node):
            assert target in nodes, f"{node['id']} -> missing node {target!r}"


def test_removed_re_diagnose_nodes_are_gone(nodes):
    assert not [i for i in nodes if i.startswith("re_diagnose_skip_")]


class TestFetchUserProfile:
    def test_chains_to_extended_profile(self, nodes):
        assert nodes["fetch_user_profile"]["on_success"] == "fetch_extended_profile"

    def test_has_flags_use_non_blank(self, nodes):
        m = _mappings(nodes["fetch_user_profile"])
        for flag in (
            "has_name",
            "has_avatar",
            "has_banner",
            "has_about_me",
            "has_designation",
            "has_group",
        ):
            assert m[f"collected.{flag}"]["transform"] == "non_blank"

    def test_designation_and_group_read_from_professional_details(self, nodes):
        m = _mappings(nodes["fetch_user_profile"])
        assert m["collected.has_designation"]["from"].endswith("professionalDetails[0].designation")
        assert m["collected.has_group"]["from"].endswith("professionalDetails[0].group")

    def test_username_verification_not_used(self, nodes):
        m = _mappings(nodes["fetch_user_profile"])
        assert "collected.username_verified" not in m
        assert "verifiedKarmayogi" not in FLOW_PATH.read_text()

    def test_transform_names_are_registered(self, flow):
        for node in flow["nodes"]:
            for m in node.get("response_mapping") or []:
                if "transform" in m:
                    assert m["transform"] in _TRANSFORMS, m

    def test_error_escalates(self, nodes):
        assert nodes["fetch_user_profile"]["on_error"]["any"] == "escalate_api_error"


class TestFetchExtendedProfile:
    def test_request(self, nodes):
        node = nodes["fetch_extended_profile"]
        assert node["type"] == "api_call"
        assert node["request"]["method"] == "GET"
        assert node["request"]["url"] == "/api/chatbot/v2/admin/extended/all/{{ ctx.user_id_hash }}"

    def test_routes(self, nodes):
        node = nodes["fetch_extended_profile"]
        assert node["on_success"] == "diagnose_missing_fields"
        assert node["on_error"]["any"] == "escalate_api_error"

    def test_mappings(self, nodes):
        m = _mappings(nodes["fetch_extended_profile"])
        assert m["collected.has_location"]["transform"] == "has_complete_location"
        assert m["collected.has_education"]["transform"] == "has_items"
        assert m["collected.has_service_history"]["transform"] == "has_items"
        assert m["collected.has_location"]["from"] == "$.response.locationDetails.data"
        assert m["collected.has_education"]["from"] == "$.response.educationalQualifications.data"
        assert m["collected.has_service_history"]["from"] == "$.response.serviceHistory.data"

    def test_mapping_end_to_end(self, nodes):
        """Run the mapped paths + transforms over a sample response."""
        response = {
            "response": {
                "locationDetails": {"data": [{"state": "Kerala", "district": "Kochi"}]},
                "educationalQualifications": {"data": []},
                "serviceHistory": {"data": [{"org": "X"}]},
            }
        }
        out = {}
        for to, m in _mappings(nodes["fetch_extended_profile"]).items():
            value = _jsonpath_get(response, m["from"].removeprefix("$."))
            out[to] = _TRANSFORMS[m["transform"]](value)
        assert out == {
            "collected.has_location": True,
            "collected.has_education": False,
            "collected.has_service_history": True,
        }

    def test_mapping_missing_sections(self, nodes):
        out = {}
        for to, m in _mappings(nodes["fetch_extended_profile"]).items():
            value = _jsonpath_get({"response": {}}, m["from"].removeprefix("$."))
            out[to] = _TRANSFORMS[m["transform"]](value)
        assert not any(out.values())


class TestDiagnoseBranch:
    @pytest.fixture()
    def evaluate(self, nodes):
        branch = nodes["diagnose_missing_fields"]
        ev = ExpressionEvaluator()

        def run(collected):
            for rule in branch["rules"]:
                if ev.evaluate(rule["if"], {"collected": collected}):
                    return rule["then"]
            return branch["default"]

        return run

    @staticmethod
    def _all_present(**over):
        c = {f: True for f in HAS_FLAGS}
        c["completion_pct"] = 80
        c.update(over)
        return c

    @pytest.mark.parametrize("pct", [100, 100.0, "100.0", "100"])
    def test_complete(self, evaluate, pct):
        assert evaluate(self._all_present(completion_pct=pct)) == "already_complete"

    def test_complete_wins_even_with_missing_flags(self, evaluate):
        assert evaluate(self._all_present(completion_pct=100, has_group=False)) == "already_complete"

    def test_all_present_below_100_is_no_gap(self, evaluate):
        assert evaluate(self._all_present(completion_pct="85.5")) == "no_gap_found"

    def test_unknown_pct_all_present_is_no_gap(self, evaluate):
        assert evaluate(self._all_present(completion_pct=None)) == "no_gap_found"

    @pytest.mark.parametrize("flag", HAS_FLAGS)
    def test_any_missing_flag_shows_summary(self, evaluate, flag):
        assert evaluate(self._all_present(**{flag: False})) == "show_missing_summary"

    def test_no_gap_found_hands_off_to_support(self, nodes):
        node = nodes["no_gap_found"]
        assert node["type"] == "message"
        assert node["next"] == "transfer_to_llm"


class TestMissingSummary:
    @pytest.fixture()
    def text(self, nodes):
        return nodes["show_missing_summary"]["prompt"]["text"]

    @staticmethod
    def _items(rendered):
        return [ln.strip() for ln in rendered.splitlines() if ln.strip().startswith("- **")]

    def test_everything_missing(self, text):
        rendered = render(text, {"collected": {f: False for f in HAS_FLAGS}})
        assert self._items(rendered) == [
            "- **Name**",
            "- **Profile Photo**",
            "- **Cover Photo**",
            "- **About Me**",
            "- **Designation**",
            "- **Group**",
            "- **Location (State and District)**",
            "- **Service History**",
            "- **Educational Qualification**",
        ]

    def test_nothing_missing(self, text):
        rendered = render(text, {"collected": {f: True for f in HAS_FLAGS}})
        assert self._items(rendered) == []

    @pytest.mark.parametrize(
        "flag,label",
        [
            ("has_name", "Name"),
            ("has_avatar", "Profile Photo"),
            ("has_banner", "Cover Photo"),
            ("has_about_me", "About Me"),
            ("has_designation", "Designation"),
            ("has_group", "Group"),
            ("has_location", "Location (State and District)"),
            ("has_service_history", "Service History"),
            ("has_education", "Educational Qualification"),
        ],
    )
    def test_only_missing_field_listed(self, text, flag, label):
        collected = {f: True for f in HAS_FLAGS}
        collected[flag] = False
        assert self._items(render(text, {"collected": collected})) == [f"- **{label}**"]

    def test_no_username_verification(self, text, nodes):
        assert "Username" not in render(text, {"collected": {f: False for f in HAS_FLAGS}})
        replies = nodes["show_missing_summary"]["on_reply"]
        assert "username" not in replies

    def test_quick_replies_match_routes(self, nodes):
        node = nodes["show_missing_summary"]
        ids = [q["id"] for q in node["quick_replies"]]
        assert set(ids) == set(node["on_reply"])
        assert node["on_reply"] == {
            "name": "show_name_steps",
            "photo": "show_profile_photo_steps",
            "cover": "show_cover_photo_steps",
            "about": "show_about_me_steps",
            "designation": "show_designation_steps",
            "group": "show_group_steps",
            "location": "show_location_steps",
            "service": "svc_history_add_steps",
            "education": "edu_qual_steps",
        }


class TestResolutionNodes:
    @pytest.mark.parametrize("node_id", ["show_name_steps", "show_location_steps"])
    def test_new_resolution_nodes(self, nodes, node_id):
        node = nodes[node_id]
        assert node["type"] == "resolution"
        assert node["steps"]
        assert node["on_reply"] == {"yes": "satisfied", "more": "fetch_user_profile"}

    def test_location_mentions_state_and_district(self, nodes):
        text = nodes["show_location_steps"]["prompt"]["text"]
        assert "State" in text and "District" in text

    @pytest.mark.parametrize(
        "node_id",
        [
            "show_profile_photo_steps",
            "show_cover_photo_steps",
            "show_username_tick_steps",
            "show_about_me_steps",
            "show_group_steps",
            "show_designation_steps",
            "show_name_steps",
            "show_location_steps",
        ],
    )
    def test_another_field_refetches_profile(self, nodes, node_id):
        assert nodes[node_id]["on_reply"]["more"] == "fetch_user_profile"


@pytest.mark.parametrize("node_id", ["show_general_steps", "show_profile_menu"])
def test_menus_route_new_fields(nodes, node_id):
    if node_id not in nodes:
        pytest.skip(f"{node_id} not present")
    replies = nodes[node_id]["on_reply"]
    assert replies["go_name"] == "show_name_steps"
    assert replies["go_loc"] == "show_location_steps"
    assert replies["go_svc"] == "svc_history_add_steps"
    assert replies["go_edu"] == "edu_qual_steps"


def test_every_menu_with_go_name_routes_all_new_fields(nodes):
    menus = [n for n in nodes.values() if "go_name" in (n.get("on_reply") or {})]
    assert len(menus) >= 2
    for n in menus:
        assert {"go_name", "go_loc", "go_svc", "go_edu"} <= set(n["on_reply"])
        ids = {q["id"] for q in n.get("quick_replies") or n["follow_up"]["quick_replies"]}
        assert {"go_name", "go_loc", "go_svc", "go_edu"} <= ids
