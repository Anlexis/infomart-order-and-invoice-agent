# CMN-C2-286 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "o-2001",
        "record_ref": "infomart://orders/o-2001",
        "record_label": "#o-2001 (accepted)",
        "amount_total": "",
        "intent": "lookup_order",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_order_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved order status" in result["confirmation"]
        assert "#o-2001 (accepted)" in result["confirmation"]
        assert "ref=infomart://orders/o-2001" in result["confirmation"]
        assert result["result"]["record_id"] == "o-2001"
        assert result["result"]["record_ref"] == "infomart://orders/o-2001"

    def test_invoice_listing_verb_and_aggregate(self):
        result = self.node(
            _state(
                intent="list_invoices",
                record_id="inv-1",
                record_ref="infomart://invoices/received",
                record_label="3 received invoice(s)",
                amount_total="JPY 1,235,000",
            )
        )
        assert "Listed received invoices" in result["confirmation"]
        assert "total=JPY 1,235,000" in result["confirmation"]

    def test_supplier_lookup_verb(self):
        result = self.node(
            _state(
                intent="lookup_supplier",
                record_id="s-77",
                record_ref="infomart://suppliers/s-77",
                record_label="supplier-s-77",
            )
        )
        assert "Retrieved supplier record" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed Infomart request" in result["confirmation"]

    def test_absent_total_is_not_rendered(self):
        result = self.node(_state(amount_total=""))
        assert "total=" not in result["confirmation"]

    def test_no_reference_still_confirms(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_hashed_record_id_when_label_missing(self):
        """A bare identifier is never emitted - it renders behind a `#`.

        At the output boundary a standalone digit run is indistinguishable from
        an unmarked amount, so a purely numeric order number rendered bare
        would be rewritten onto the monetary grid and name a different record.
        """
        result = self.node(_state(record_label="", record_id="20260712001"))
        assert "'#20260712001'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
