# CMN-C2-286 - Unit tests: CallInfomartApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. The ONE documented exception: the config-override
# call passes a 2nd (config) argument, which __call__ cannot forward - that
# single test stays a DIRECT execute(state, config=...) call (ANONYMOUS node,
# the trust gate is unaffected).
#
# The node builds its client locally (nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's InfomartClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_infomart_api_node import CallInfomartApiNode
from src.services.infomart_client import InfomartApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_infomart_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "infomart_payload": to_json({"order_no": "o-2001"}),
        "intent": "lookup_order",
        "target_id": "o-2001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-infomart-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for InfomartClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_order(self, order_no, api_token):
        raise InfomartApiError(403, "forbidden by integration permissions")


class _FakeLiveClient:
    """Stands in for InfomartClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_order(self, order_no, api_token):
        _FakeLiveClient.captured = {"order_no": order_no, "api_token": api_token}
        return {"order_data": [{"order_no": order_no, "status": "accepted"}]}


class TestCallInfomartApiNode:
    def setup_method(self):
        self.node = CallInfomartApiNode()

    def test_order_lookup_success_via_default_stub(self):
        # Default transport = deterministic, network-free stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "o-2001"
        assert result["record_ref"] == "infomart://orders/o-2001"
        assert result["target_id"] == "o-2001"
        # Identifiers render behind a `#` so the output boundary can tell an
        # identifier from an amount.
        assert result["record_label"] == "#o-2001 (accepted)"

    def test_invoice_listing_success_via_default_v1_stub(self):
        state = _state(
            intent="list_invoices",
            target_id="",
            infomart_payload=to_json({"scope": "received"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "infomart://invoices/received"
        assert result["record_id"].startswith("inv-")
        assert result["record_label"] == "1 received invoice(s)"

    def test_supplier_lookup_success_via_default_v1_stub(self):
        state = _state(
            intent="lookup_supplier",
            target_id="s-77",
            infomart_payload=to_json({"supplier_code": "s-77"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "s-77"
        assert result["record_ref"] == "infomart://suppliers/s-77"
        assert result["record_label"] == "supplier-s-77"

    def test_infomart_config_state_field_sets_base_url(self):
        # The inner graph injects the runtime `infomart:` section as the JSON
        # infomart_config state field; the stub transport still serves the call.
        state = _state(infomart_config=to_json({"base_url": "https://infomart.example.test/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "infomart://orders/o-2001"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"infomart": {"base_url": "https://infomart.example.test/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "o-2001"

    def test_missing_payload_errors(self):
        result = self.node(_state(infomart_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_order_lookup_with_unresolved_code_errors(self):
        state = _state(target_id="", infomart_payload=to_json({"order_no": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved order number" in entry for entry in result["error_log"])

    def test_supplier_lookup_with_unresolved_code_errors(self):
        state = _state(
            intent="lookup_supplier",
            target_id="",
            infomart_payload=to_json({"supplier_code": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved supplier code" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_order"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing INFOMART_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"INFOMART_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["order_no"] == "o-2001"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_infomart_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_infomart_api_complete"]
        assert payload["intent"] == "lookup_order"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
        assert payload["source"] == "infomart_api"


class TestAnswersFromCallerRecords:
    """The caller's own records are the first data source: real arithmetic on
    validated figures, no network call needed."""

    def setup_method(self):
        self.node = CallInfomartApiNode()

    def test_invoice_aggregate_is_computed_and_rendered_on_the_grid(self):
        state = _state(
            intent="list_invoices",
            target_id="",
            infomart_payload=to_json({"scope": "received"}),
            input_context={
                "invoices": [
                    {"invoice_no": "inv-1", "amount": 412_300.0},
                    {"invoice_no": "inv-2", "amount": 822_267.0},
                ]
            },
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_count"] == 2
        # 412,300 + 822,267 = 1,234,567 -> reported on the nearest-1,000 grid.
        assert result["amount_total"] == "JPY 1,235,000"
        assert result["record_id"] == "inv-1"
        assert result["record_ref"] == "infomart://invoices/received"

    def test_order_record_answers_without_the_integration(self, monkeypatch):
        # If the caller record were ignored, this fake would raise instead.
        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _FakeErrorClient)
        state = _state(input_context={"order": {"order_no": "o-9001", "status": "shipped", "amount": 250_400.0}})
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "o-9001"
        assert result["record_label"] == "#o-9001 (shipped)"
        assert result["amount_total"] == "JPY 250,000"

    def test_supplier_record_answers_without_the_integration(self):
        state = _state(
            intent="lookup_supplier",
            target_id="s-77",
            infomart_payload=to_json({"supplier_code": "s-77"}),
            input_context={"supplier": {"supplier_code": "s-77", "name": "Yamada Foods"}},
        )
        result = self.node(state)
        assert result["record_id"] == "s-77"
        assert result["record_label"] == "Yamada Foods"

    def test_absent_caller_records_fall_back_to_the_integration(self):
        result = self.node(_state(input_context={"target_hint": "o-2001"}))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "o-2001"

    def test_audit_names_the_caller_record_source(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_infomart_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(input_context={"order": {"order_no": "o-9001"}}))
        payloads = {args[0]: args[1] for args in events}
        assert payloads["call_infomart_api_complete"]["source"] == "caller_records"


class TestRuntimeTimeout:
    """The declared timeout_s must reach the transport, not merely be stored."""

    def setup_method(self):
        self.node = CallInfomartApiNode()

    def test_configured_timeout_is_applied(self, monkeypatch):
        seen = {}

        class _TimeoutSpyClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                self.timeout_s = kwargs.get("timeout_s", 30.0)

            def get_order(self, order_no, api_token):
                seen["timeout_s"] = self.timeout_s
                return {"order_data": [{"order_no": order_no, "status": "accepted"}]}

        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _TimeoutSpyClient)
        state = _state(infomart_config=to_json({"base_url": "https://x.test/v1", "timeout_s": 7}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert seen["timeout_s"] == 7.0

    def test_malformed_timeout_fails_closed(self):
        state = _state(infomart_config=to_json({"base_url": "https://x.test/v1", "timeout_s": "soon"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("timeout_s" in entry for entry in result["error_log"])
