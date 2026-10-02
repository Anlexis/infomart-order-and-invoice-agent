# CMN-C2-286 - Unit tests: InferInfomartFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. Positive payloads are PII-free: the framework
# input gate rewrites Title-Case bigrams in validated_input, so quoted display
# names use a single-word name.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_infomart_fields_node import InferInfomartFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_infomart_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_order", target_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "target_hint": target_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferInfomartFieldsNode:
    def setup_method(self):
        self.node = InferInfomartFieldsNode()

    def test_lookup_extracts_code_from_text(self):
        result = self.node(_state("Look up the status of order o-2001 and summarize the record on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["target_id"] == "o-2001"
        # infomart_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["infomart_payload"], str)
        assert from_json(result["infomart_payload"], {}) == {"order_no": "o-2001"}

    def test_code_shaped_hint_used_when_text_has_no_code(self):
        result = self.node(_state("Summarize the current record on file", target_hint="o-3005"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["target_id"] == "o-3005"
        assert from_json(result["infomart_payload"], {}) == {"order_no": "o-3005"}

    def test_supplier_lookup_builds_supplier_params(self):
        # Qualified code mention ("supplier code: s-77") resolves via the text;
        # the quoted display label stays a single word (the framework name mask
        # rewrites Title-Case word pairs even ACROSS newlines).
        text = 'Retrieve the record for "Acme"\nSupplier code: s-77'
        result = self.node(_state(text, intent="lookup_supplier"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["target_id"] == "s-77"
        assert result["record_label"] == "Acme"
        assert from_json(result["infomart_payload"], {}) == {"supplier_code": "s-77"}

    def test_invoice_listing_builds_scope_and_filters(self):
        text = "List the received invoices\nPeriod: june\nStatus: unpaid"
        result = self.node(_state(text, intent="list_invoices"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["infomart_payload"], {})
        assert payload["scope"] == "received"
        assert {"name": "Period", "values": ["june"]} in payload["filters"]
        assert {"name": "Status", "values": ["unpaid"]} in payload["filters"]

    def test_invoice_listing_excludes_code_and_name_keys_from_filters(self):
        text = "List the received invoices\nSupplier code: s-77\nPeriod: june"
        result = self.node(_state(text, intent="list_invoices"))
        payload = from_json(result["infomart_payload"], {})
        assert payload["filters"] == [{"name": "Period", "values": ["june"]}]

    def test_unresolved_code_left_empty_never_invented(self):
        result = self.node(_state("Summarize the record on file for the flagged order"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["target_id"] == ""
        assert from_json(result["infomart_payload"], {}) == {"order_no": ""}

    def test_non_code_shaped_hint_left_unresolved(self):
        result = self.node(_state("Summarize the record on file", target_hint="not a valid code!"))
        assert result["target_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class TestCallerRecordsAndRenderedText:
    """The caller's validated record resolves the target; anything lifted out
    of the request text is re-checked against the same inert alphabets."""

    def setup_method(self):
        self.node = InferInfomartFieldsNode()

    def test_caller_order_record_resolves_the_target(self):
        result = self.node(_state("Summarize the record on file", input_context={"order": {"order_no": "o-4242"}}))
        assert result["target_id"] == "o-4242"

    def test_caller_supplier_name_is_used_as_the_label(self):
        result = self.node(
            _state(
                "Summarize the record on file",
                intent="lookup_supplier",
                input_context={"supplier": {"supplier_code": "s-77", "name": "Yamada Foods Co."}},
            )
        )
        assert result["target_id"] == "s-77"
        assert result["record_label"] == "Yamada Foods Co."

    def test_text_in_the_request_beats_a_caller_record(self):
        """An explicit code in the request is the most specific instruction."""
        result = self.node(_state("look up order o-2001", input_context={"order": {"order_no": "o-4242"}}))
        assert result["target_id"] == "o-2001"

    def test_quoted_label_outside_the_display_alphabet_is_dropped(self):
        """Dropping beats escaping: an escaped term still travels into the
        response as caller-authored text."""
        result = self.node(_state('Retrieve the record for "<img src=x>"'))
        assert result["record_label"] == ""

    def test_filter_terms_outside_the_display_alphabet_are_dropped(self):
        text = "List the received invoices\nPeriod: june\nMemo: <script>alert(1)</script>"
        result = self.node(_state(text, intent="list_invoices"))
        payload = from_json(result["infomart_payload"], {})
        assert payload["filters"] == [{"name": "Period", "values": ["june"]}]

    def test_filter_count_is_capped(self):
        lines = ["List the received invoices"] + [f"Field{i}: value{i}" for i in range(40)]
        result = self.node(_state("\n".join(lines), intent="list_invoices"))
        payload = from_json(result["infomart_payload"], {})
        assert len(payload["filters"]) == 20
