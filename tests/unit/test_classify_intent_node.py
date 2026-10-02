# CMN-C2-286 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_order / list_invoices / lookup_supplier (deterministic
# keyword heuristic, no model; every intent is a READ, and an unknown one
# falls back to the narrowest read, lookup_order).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_order(self):
        result = self.node(_state("Look up the status of order o-2001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_order"

    def test_keyword_list_invoices(self):
        result = self.node(_state("List the received invoices for this month"))
        assert result["intent"] == "list_invoices"

    def test_keyword_lookup_supplier(self):
        result = self.node(_state("Retrieve the supplier record for code s-77"))
        assert result["intent"] == "lookup_supplier"

    def test_specific_keyword_wins_over_generic_order(self):
        # Priority order is most-specific-first: "invoice" / "supplier"
        # vocabularies beat the generic order vocabulary even when both appear.
        result = self.node(_state("Check the invoice attached to order o-2001"))
        assert result["intent"] == "list_invoices"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_order"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_order" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the status of order o-2001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_order"
        assert payloads["classify_intent_complete"]["defaulted"] is False
