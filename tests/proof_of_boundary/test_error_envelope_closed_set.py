# PB: the caller-visible ERROR envelope carries closed-set labels only (CMN-C2-286).
#
# Sibling test to test_error_envelope_no_record_evidence.py, on the axis that
# one does not cover. That file asks whether the envelope names the RECORD;
# this one asks whose WORDS it is made of.
#
# molt source review, 2026-09-04 (family-level finding across the Wave 9 CMN
# family): "That log can contain upstream exception/response text; truncation,
# path stripping, or credential-only redaction is not a closed-set error
# contract. Identifiers, names, emails, and arbitrary third-party response
# bodies remain possible. The caller-visible error must instead be closed-set
# labels only."
#
# The defect these tests hold closed: PostProcessNode built the envelope as
# {"reason": <constant>, "confirmation": "", "error": <error_log or the gate's
# violations>} and returned it under `formatted_output`. Every output-bearing
# field WAS cleared and the envelope WAS record-free - and the caller still
# received the node-authored lines, because AgentBaseGraph.get_output()
# projects `formatted_output or result` with no status check. Clearing the
# answer is a different property from bounding the error channel.
#
# The contract now: every non-success return is `_contain()`, and the envelope
# is {"reason": r} with r drawn from ERROR_REASONS. `error_log` stays the
# internal channel - the state reducer accumulates it and the audit trail needs
# it - and is never projected.
#
# Reachability, stated plainly because it decides what each test can prove:
#   * the INNER-ERROR branch is defence in depth on the real wheel.
#     AgentBaseGraph.route() sends an ERROR status straight to finalize and
#     BaseNode.__call__ short-circuits an already-errored state, so
#     post_process never runs on it. It is driven here by a direct execute()
#     call - which is also how any future re-wiring would reach it. A leak that
#     is unreachable only because of a routing decision elsewhere is a masked
#     leak, not a contained one.
#   * the GATE-VIOLATION branch IS reachable end to end, and the ASGI test
#     below drives it through the real /invoke.

import asyncio
import importlib
import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import (
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    ERROR_REASONS,
    PostProcessNode,
)
from src.schemas.state import to_json

try:
    import httpx

    _CLIENT_ERROR = None
except Exception as exc:  # pragma: no cover - only in stripped-down envs
    _CLIENT_ERROR = exc

# A recognisable stand-in for the shape molt named: an upstream response body,
# quoted into a node's error message, carrying a person and a record. Assembled
# so no credential-shaped literal is committed and so nothing here is masked by
# the platform PII filter before the assertion can see it - `_MARKER` is a
# lowercase alphanumeric token that no detector rewrites.
_MARKER = "zzmarker4d9f1a"
_SENTINEL = "CallInfomartApiNode: upstream said {'customer': '" + _MARKER + "', 'order': 'ORD-77120'}"

# Built at runtime so no credential-shaped literal is committed to the tree.
_BEARER_LIKE = "Bearer " + "a" * 24


def _strings(value, path="output"):
    """Every (path, string) in a nested structure - mapping KEYS included.

    Keys are walked because a leak can ride one: a violation label built from a
    caller-influenced key, or an envelope keyed by node-authored text, is
    invisible to a values-only scan.
    """
    found = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append((f"{path}.<key>", key))
            found.extend(_strings(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_strings(item, f"{path}[{index}]"))
    else:
        found.append((path, str(value)))
    return found


def _assert_absent(needle, value, where):
    for path, text in _strings(value):
        assert needle not in text, f"{needle!r} reached {where} at {path}"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _base_state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "o-2001",
        "record_ref": "infomart://orders/o-2001",
        "record_label": "#o-2001 (accepted)",
        "record_count": 1,
        "amount_total": "JPY 1,235,000",
        "intent": "lookup_order",
        "confirmation": "Retrieved order status '#o-2001 (accepted)'",
        "infomart_payload": to_json({"order_no": "o-2001"}),
        "correlation_id": "pb-closed-set",
        "node_history": [],
        # EVERY path below starts with the sentinel already in the internal
        # channel, so each one is a real test of whether that channel leaks.
        "error_log": [_SENTINEL],
        "execution_time": {},
    }
    state.update(overrides)
    return state


# Every non-success return of PostProcessNode.execute(), by the fault that
# produces it. The point of parameterising is that a fix applied to one branch
# and not another is the commonest way this defect survives a review.
_ERROR_PATHS = {
    # (1) a pre-existing inner-workflow error: the branch that published
    #     error_log verbatim.
    "inner_workflow_error": (
        {"status": AgentStatus.ERROR.value},
        _REASON_WORKFLOW_FAILED,
    ),
    # (2)-(5) the output gate refusing to release the response.
    "gate_missing_record_evidence": (
        {"record_id": "", "record_ref": ""},
        _REASON_OUTPUT_WITHHELD,
    ),
    "gate_credential_in_narrative_field": (
        {"record_label": _BEARER_LIKE},
        _REASON_OUTPUT_WITHHELD,
    ),
    "gate_credential_nested_in_payload": (
        {"infomart_payload": to_json({"filters": [{"name": "memo", "values": [_BEARER_LIKE]}]})},
        _REASON_OUTPUT_WITHHELD,
    ),
    # A credential carried as a mapping KEY. A values-only walk reports zero
    # findings on exactly this case.
    "gate_credential_as_a_payload_key": (
        {"infomart_payload": to_json({_BEARER_LIKE: "memo"})},
        _REASON_OUTPUT_WITHHELD,
    ),
}


@pytest.mark.parametrize("overrides,expected_reason", _ERROR_PATHS.values(), ids=list(_ERROR_PATHS))
class TestEveryErrorPathPublishesClosedSetLabelsOnly:
    def test_envelope_values_are_all_declared_constants(self, overrides, expected_reason):
        """The envelope is {"reason": r}, r in ERROR_REASONS - on every path."""
        result = PostProcessNode().execute(_base_state(**overrides))
        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, f"envelope grew a key: {sorted(envelope)}"
        assert envelope["reason"] == expected_reason
        for _path, text in _strings(envelope):
            assert text in ERROR_REASONS | {"reason"}, f"envelope carries an undeclared value: {text!r}"

    def test_envelope_stays_truthy(self, overrides, expected_reason):
        """A falsy formatted_output re-opens `formatted_output or result`.

        AgentBaseGraph.get_output() applies no status check, so an empty
        envelope would hand the caller whatever survived in state instead.
        """
        result = PostProcessNode().execute(_base_state(**overrides))
        assert result["formatted_output"], "falsy envelope re-opens the `or result` fallback"

    def test_seeded_error_log_appears_nowhere_in_the_returned_mapping(self, overrides, expected_reason):
        """The whole delta, not just the envelope - nesting is not cover.

        error_log itself is exempt: it IS the internal channel, and the delta
        may add NEW entries to it (gate violations). What must not happen is
        the seeded upstream text coming back out through any other key.
        """
        result = PostProcessNode().execute(_base_state(**overrides))
        published = {k: v for k, v in result.items() if k != "error_log"}
        _assert_absent(_MARKER, published, "the returned node result")
        _assert_absent(_SENTINEL, published, "the returned node result")

    def test_inner_error_log_entries_are_not_re_emitted(self, overrides, expected_reason):
        """The state reducer APPENDS, so re-emitting duplicates every line."""
        result = PostProcessNode().execute(_base_state(**overrides))
        assert _SENTINEL not in result.get("error_log", [])


class TestReasonsAreAClosedSet:
    def test_error_reasons_holds_exactly_the_two_declared_codes(self):
        assert ERROR_REASONS == frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})

    def test_a_credential_shaped_key_is_withheld_from_the_violation_label(self):
        """The label rides error_log, where the framework's own scan would raise.

        FunctionNode._security_gate_output scans every value of a node result
        and RAISES on a credential finding - so a violation label that repeated
        a credential-shaped key would make this node's cleared result blow up
        and be replaced by a bare error, re-opening the `or result` fallback
        the clearing had just closed.
        """
        result = PostProcessNode().execute(_base_state(infomart_payload=to_json({_BEARER_LIKE: "memo"})))
        violations = result["error_log"]
        assert violations, "a credential carried as a KEY must still be found"
        assert any("<withheld>" in entry for entry in violations)
        assert not any("a" * 24 in entry for entry in violations)


# ----------------------------------------------------------------------------
# End to end, through the deployed surface.
# ----------------------------------------------------------------------------

_TOKEN = "invoke-token-for-testing"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

pytestmark_e2e = pytest.mark.skipif(_CLIENT_ERROR is not None, reason=f"http client unavailable: {_CLIENT_ERROR}")


class _AsgiClient:
    """Minimal synchronous wrapper over the application's real ASGI interface."""

    def __init__(self, app):
        self._app = app

    def post(self, path, json=None, headers=None):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as http:
                return await http.post(path, json=json, headers=headers)

        return asyncio.run(_run())


@pytestmark_e2e
class TestErrorEnvelopeThroughTheRealInvoke:
    """The gate-violation branch, driven through the deployed /invoke.

    A DATA-path fault: the last inner node is made to hand the workflow back a
    result with the upstream text in `error_log` and its record evidence gone.
    The gate is untouched - it refuses on its own terms (a SUCCESS response
    with no record_id/record_ref misrepresents the Infomart lookup), which is
    what puts the request on the contained path with the sentinel already in
    the internal channel.
    """

    @pytest.fixture()
    def client(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)

        def _confirm_without_evidence(self, state):
            return {
                "confirmation": "",
                "result": {"record_id": "", "confirmation": ""},
                "record_id": "",
                "record_ref": "",
                "error_log": [_SENTINEL],
                "status": AgentStatus.SUCCESS.value,
            }

        monkeypatch.setattr(
            "src.nodes.confirm_node.ConfirmNode.execute",
            _confirm_without_evidence,
        )
        server = importlib.import_module("src.api.server")
        return _AsgiClient(server.app)

    def _blocked_body(self, client):
        response = client.post(
            "/invoke",
            json={
                "input": "Summarize the record on file for the flagged order.",
                "input_context": {"order": {"order_no": "o-9001", "status": "shipped", "amount": 250_400.0}},
            },
            headers=_AUTH,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_fault_actually_reaches_the_output_gate(self, client):
        """Positive control: without this the absence assertions prove nothing."""
        body = self._blocked_body(client)
        assert body["status"] == AgentStatus.ERROR.value
        assert "PostProcessNode" in body["node_history"], "the gate branch was never reached"

    def test_invoke_body_publishes_the_reason_code_and_nothing_else(self, client):
        body = self._blocked_body(client)
        assert body["output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_seeded_error_log_appears_nowhere_in_the_invoke_body(self, client):
        """Walk the whole response - nested values AND nested keys."""
        body = self._blocked_body(client)
        _assert_absent(_MARKER, body, "the /invoke response body")
        _assert_absent(_SENTINEL, body, "the /invoke response body")
        # Belt and braces on the serialized form the caller actually receives.
        assert _MARKER not in json.dumps(body, default=str)

    def test_no_node_authored_error_text_rides_the_body(self, client):
        """The gate's own violation sentences are node-authored too.

        They are bounded today, but they are not a closed set - which is the
        whole of molt's finding. They belong in error_log.
        """
        body = self._blocked_body(client)
        _assert_absent("PostProcess output gate", body, "the /invoke response body")
        _assert_absent("record_id/record_ref", body, "the /invoke response body")
