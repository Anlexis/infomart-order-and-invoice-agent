# CMN-C2-286 - Unit tests: PostProcessNode, the external output boundary.
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The
# domain output gate is the MODULE-LEVEL _security_gate_output() helper (the
# framework gate methods are final and the SDK auto-wraps _extra_ hooks), so
# the helper is also unit-tested directly as a plain function.
#
# The precision-grid cases below are the enumerated leak forms: form-based and
# context-based recognition, symmetric marker placement, signs, arbitrary
# horizontal whitespace, decimals, and the structural tokens that must stay
# byte-identical. Both directions are asserted - a missed leak and a false snap
# are both defects, and only the second one is visible to a caller.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    _NARRATIVE_FIELDS,
    _STRUCTURED_FIELDS,
    PostProcessNode,
    _enforce_precision,
    _security_gate_output,
)
from src.schemas.state import to_json

# Built at runtime so no credential-shaped literal is committed.
_BEARER_LIKE = "Bearer " + "a" * 24


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "o-2001",
        "record_ref": "infomart://orders/o-2001",
        "record_label": "#o-2001 (accepted)",
        "record_count": 1,
        "amount_total": "",
        "intent": "lookup_order",
        "confirmation": "Retrieved order status '#o-2001 (accepted)' - ref=infomart://orders/o-2001",
        "infomart_payload": to_json({"order_no": "o-2001"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "o-2001"
        assert out["record_ref"] == "infomart://orders/o-2001"
        assert out["record_label"] == "#o-2001 (accepted)"
        assert out["intent"] == "lookup_order"
        assert out["confirmation"].startswith("Retrieved order status")
        # The JSON infomart_payload string surfaces parsed.
        assert out["infomart_payload"] == {"order_no": "o-2001"}
        # The schema note travels with the response it describes.
        assert "nearest 1,000" in out["schema_note"]

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success.

        Real pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated.
        """
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallInfomartApiNode: Infomart API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Infomart API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """A SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record_id/record_ref" in entry for entry in result["error_log"])

    def test_violation_clears_every_output_bearing_field(self):
        """Blocking is containment, not just an error status.

        The response envelope falls back to state["result"] even on an error
        status, so a gate that merely reported the violation would still ship
        the un-gated inner answer inside the error envelope.
        """
        result = self.node(
            _state(
                confirmation=f"Retrieved order status - {_BEARER_LIKE}",
                record_label="#o-2001",
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] == ""
        assert result["confirmation"] == ""
        assert result["record_label"] == ""
        assert result["amount_total"] == ""
        assert result["infomart_payload"] == ""
        # The record evidence is cleared too - a caller told the operation
        # failed must not learn which order or supplier was resolved.
        assert result["record_id"] == ""
        assert result["record_ref"] == ""
        assert result["target_id"] == ""
        assert result["intent"] == ""
        # The replacement envelope is the withheld NOTICE: truthy on purpose (a
        # falsy value re-opens the framework's `formatted_output or result`
        # projection), carrying the reason code and NOTHING else - the block
        # reasons go to error_log, the internal channel.
        assert result["formatted_output"] == {"reason": "output_withheld_by_gate"}
        assert "o-2001" not in repr(result["formatted_output"])
        # The violation names the location, never the credential itself.
        assert not any("a" * 24 in entry for entry in result["error_log"])

    def test_credential_nested_in_payload_is_caught(self):
        """The gate walks nested structures, not just top-level strings."""
        result = self.node(_state(infomart_payload=to_json({"filters": [{"name": "memo", "values": [_BEARER_LIKE]}]})))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"]["reason"] == "output_withheld_by_gate"
        assert _BEARER_LIKE not in repr(result["formatted_output"])

    def test_control_top_level_credential_is_caught(self):
        """The control that proves the scan itself works, not just the nested probe."""
        result = self.node(_state(record_label=_BEARER_LIKE))
        assert result["status"] == AgentStatus.ERROR.value

    def test_error_log_never_reaches_the_error_envelope(self):
        """An error_log entry cannot ship, whatever it says.

        The envelope no longer carries the log at all, so there is nothing to
        scan: a credential-bearing entry and an ordinary one produce the same
        constant envelope. Called through execute() rather than the node: the
        backbone routes an errored state straight to finalize, and
        BaseNode.__call__ skips execute() on an incoming error, so this branch
        is defence in depth for a caller that reaches the formatter with an
        error in hand.
        """
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[f"upstream said {_BEARER_LIKE}"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": "infomart_workflow_failed"}
        assert _BEARER_LIKE not in repr(result["formatted_output"])
        assert result["result"] == ""

    def test_error_envelope_is_the_reason_code_alone(self):
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=["no such order"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": "infomart_workflow_failed"}
        # Truthy: a falsy formatted_output re-opens `formatted_output or result`.
        assert result["formatted_output"]
        # The inner entries are NOT re-emitted - the state reducer appends, so
        # re-emitting would duplicate every line as well as publish it.
        assert "error_log" not in result

    def test_off_grid_total_is_snapped(self):
        result = self.node(_state(amount_total="JPY 1,234,567", record_count=3))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["amount_total"] == "JPY 1,235,000"

    def test_structural_fields_are_never_rewritten(self):
        """A purely numeric order number is an identifier, not an amount."""
        result = self.node(
            _state(record_id="20260712001", record_ref="infomart://orders/20260712001", record_label="#20260712001")
        )
        out = result["formatted_output"]
        assert out["record_id"] == "20260712001"
        assert out["record_ref"] == "infomart://orders/20260712001"
        assert out["record_label"] == "#20260712001"

    def test_output_fields_are_classified(self):
        """Every caller-facing field is either narrative (gridded) or structured.

        A new field added to the response without a decision about which of the
        two it is fails here rather than silently escaping the grid.
        """
        result = self.node(_state())
        classified = set(_NARRATIVE_FIELDS) | set(_STRUCTURED_FIELDS)
        assert set(result["formatted_output"]) <= classified


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "o-2001", "record_ref": "infomart://orders/o-2001", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        violations = _security_gate_output({"record_id": "o-2001", "note": _BEARER_LIKE}, is_success=True)
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []


class TestPrecisionGrid:
    """The grammar, probed on both directions of every enumerated form."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            # form-based: comma-grouped and long unformatted runs, any magnitude
            ("total 1,234,567 billed", "total 1,235,000 billed"),
            ("total 9,999 billed", "total 10,000 billed"),
            ("total 1234567 billed", "total 1,235,000 billed"),
            # currency context, marker BEFORE the value
            ("JPY 9999", "JPY 10,000"),
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
            ("JPY 1,234", "JPY 1,000"),
            ("¥9999", "¥10,000"),
            ("￥9999", "￥10,000"),
            # currency context, marker AFTER the value
            ("9999 JPY", "10,000 JPY"),
            ("9999円", "10,000円"),
            # signs preserved
            ("JPY -9999", "JPY -10,000"),
            ("JPY +9999", "JPY +10,000"),
            # decimal amount in currency context snaps as ONE number
            ("JPY 1234.56", "JPY 1,000"),
        ],
    )
    def test_leak_forms_snap(self, text, expected):
        snapped, count = _enforce_precision(text)
        assert snapped == expected
        assert count == 1

    @pytest.mark.parametrize(
        "text",
        [
            # already on the grid
            "JPY 1,000",
            "total 10,000 billed",
            # structural tokens: identifiers, counts, years, horizons
            "ref=infomart://orders/20260712001",
            "#20260712001",
            "order o-2001 accepted",
            "3 received invoice(s)",
            "delivered within 90d",
            "STAR 2026",
            # a part number is not a negative amount
            "SKF-6205",
            "ENE-FAC-20260712-001",
            # decimals that are not amounts survive intact
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            # the fraction cannot be backtracked out of
            "JPY 1234.56m",
            # a marker ending a line must not bind across a paragraph break
            "Currency: JPY\n\n3. Received invoices",
        ],
    )
    def test_structural_tokens_are_byte_identical(self, text):
        snapped, count = _enforce_precision(text)
        assert snapped == text
        assert count == 0

    def test_pattern_scan_runs_before_the_snap(self):
        """A labelled sequence must reach the credential scan un-mangled.

        The grammar treats any standalone 3-letter uppercase word as a currency
        marker, so snapping first would rewrite the digits of a labelled
        sequence and destroy the shape the credential scan looks for. The gate
        therefore scans, then snaps, then re-scans.
        """
        state = _state(confirmation=f"contact reference AKIA{'B' * 16}")
        result = PostProcessNode()(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"]["reason"] == "output_withheld_by_gate"
        assert "AKIA" not in repr(result["formatted_output"])
