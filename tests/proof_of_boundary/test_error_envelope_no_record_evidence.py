"""Regression: an ERROR envelope must not disclose Infomart record evidence.

Molt source review, 2026-09-04 (wave-9 batch, family-level finding over sixteen
sibling repos): "existing-ERROR rebuilds truthy `formatted_output` with record
evidence; and caller-visible `error_log` can carry interpolated exception /
upstream text rather than closed-set labels."

The `errored` branch of PostProcessNode rebuilt `formatted_output` from
`record_id` / `record_ref` read straight back out of state, and returned ONLY
that plus `status` - so every other output-bearing field (`result`,
`record_label`, `confirmation`, `amount_total`, `infomart_payload`, `intent`,
`target_id`) survived in state untouched.

Those identifiers ARE the lookup evidence: this node's own gate
(`_security_gate_output`, is_success=True) REFUSES a SUCCESS that lacks them. An
error envelope carrying them tells a caller who is being informed of a FAILURE
that an Infomart record was nonetheless resolved, and which order or supplier it
was. `AgentBaseGraph.get_output()` projects `formatted_output or result` with NO
status check, so anything left in `result` ships in the error response too.

The containment contract is not "shape a nicer error mapping". It is: no
un-gated caller-facing content or record evidence survives on any error path -
neither in the replacement `formatted_output`, nor in the diagnostics it
carries, nor in the state left behind for a checkpoint or a downstream reader.

Reachability: on the compiled graph this branch is defence-in-depth.
`AgentBaseGraph.route()` sends an ERROR status to `finalize`, bypassing
`post_process`, and `BaseNode.__call__` short-circuits an already-errored state
before `execute()` runs. It is reachable by a direct `execute()` call, which is
how the tests below drive it - and how any future re-wiring would reach it.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.call_infomart_api_node import CallInfomartApiNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import to_json

# Record evidence + the trading content hanging off it.
_ORDER_NO = "ORD-77120"
_RECORD_REF = "infomart://orders/ORD-77120"
_SUPPLIER = "Tsukiji Foods K.K."
_LABEL = "#ORD-77120 (shipped)"
_AMOUNT = "JPY 1,235,000"


def _errored_state() -> dict:
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": ["CallInfomartApiNode: Infomart API error 500"],
        "record_id": _ORDER_NO,
        "record_ref": _RECORD_REF,
        "record_label": _LABEL,
        "record_count": 1,
        "amount_total": _AMOUNT,
        "target_id": _ORDER_NO,
        "intent": "lookup_order",
        "confirmation": f"Retrieved Infomart order '{_LABEL}' from {_SUPPLIER} - ref={_RECORD_REF}",
        "infomart_payload": to_json({"order_no": _ORDER_NO, "supplier": _SUPPLIER}),
        "result": {"record_id": _ORDER_NO, "confirmation": "done"},
    }


def _flatten(value) -> str:
    """Render every reachable string in a returned value - nesting is not cover."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


class TestErrorEnvelopeContainment:
    def test_error_envelope_carries_no_record_evidence(self):
        out = PostProcessNode().execute(_errored_state())

        assert out["status"] == AgentStatus.ERROR.value

        # formatted_output must be PRESENT and TRUTHY. The framework projects
        # `formatted_output or result` with no status check, so a falsy value
        # re-opens the fallback onto whatever survived in state.
        assert "formatted_output" in out
        assert out["formatted_output"], "falsy formatted_output re-opens the `or result` fallback"

        shipped = _flatten(out["formatted_output"])
        assert _ORDER_NO not in shipped, "order number shipped in the error envelope"
        assert _RECORD_REF not in shipped, "record ref shipped in the error envelope"
        assert "infomart://" not in shipped
        assert _SUPPLIER not in shipped
        assert _AMOUNT not in shipped
        assert _LABEL not in shipped
        assert "Retrieved Infomart order" not in shipped

    def test_error_path_clears_output_bearing_state(self):
        """Omitting a field from one envelope is not clearing it from state."""
        out = PostProcessNode().execute(_errored_state())
        for field in (
            "result",
            "confirmation",
            "record_label",
            "amount_total",
            "infomart_payload",
            "record_id",
            "record_ref",
            "target_id",
            "intent",
        ):
            assert field in out, f"{field} not cleared on the error path"
            assert not out[field], f"{field} still carries content on the error path"

    def test_success_path_still_returns_the_answer(self):
        """Control: containment must not empty out the clean path."""
        out = PostProcessNode().execute(
            {
                "status": AgentStatus.SUCCESS.value,
                "record_id": _ORDER_NO,
                "record_ref": _RECORD_REF,
                "record_label": _LABEL,
                "record_count": 1,
                "amount_total": _AMOUNT,
                "intent": "lookup_order",
                "confirmation": f"Retrieved Infomart order - ref={_RECORD_REF}",
                "infomart_payload": to_json({"order_no": _ORDER_NO}),
            }
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        shipped = _flatten(out["formatted_output"])
        assert _ORDER_NO in shipped, "the success path must still return the record evidence"
        assert _RECORD_REF in shipped


class TestErrorLogNamesNoRecord:
    """The diagnostics the error envelope carries are a channel of their own.

    `formatted_output["error"]` is `error_log`, so an upstream message that
    interpolates the order number - or the tenant's error body - puts the same
    evidence back in the envelope by another key. Reasons must be closed-set
    labels, never the record value.
    """

    def _state(self, **overrides) -> dict:
        state = {
            "infomart_payload": to_json({"order_no": _ORDER_NO}),
            "intent": "lookup_order",
            "target_id": _ORDER_NO,
            "correlation_id": "pb-error-envelope",
            "session_id": "pb-s1",
            "thread_id": "pb-th1",
            "trace_id": "pb-t1",
            "node_history": [],
            "error_log": [],
            "execution_time": {},
        }
        state.update(overrides)
        return state

    def test_order_not_found_reason_does_not_name_the_order(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_infomart_api_node.emit_trace_event", lambda *a, **k: None)

        class _EmptyLookupClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_order(self, order_no, api_token):
                return {"order_data": []}

        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _EmptyLookupClient)
        out = CallInfomartApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert _ORDER_NO not in joined, "the not-found reason names the order number"
        # The reason still has to be actionable: it names the record TYPE
        # (a closed-set label), not the record.
        assert "order" in joined

    def test_supplier_not_found_reason_does_not_name_the_code(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_infomart_api_node.emit_trace_event", lambda *a, **k: None)

        class _EmptySupplierClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_supplier(self, code, api_token):
                return {"supplier_data": []}

        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _EmptySupplierClient)
        out = CallInfomartApiNode().execute(
            self._state(
                intent="lookup_supplier", infomart_payload=to_json({"supplier_code": "SUP-8890"}), target_id="SUP-8890"
            )
        )
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "SUP-8890" not in joined, "the not-found reason names the supplier code"
        assert "supplier" in joined

    def test_upstream_api_failure_reason_carries_no_upstream_body(self, monkeypatch):
        """A live tenant's error body is unbounded third-party text; only the
        HTTP status - a closed-set signal - belongs in a caller-facing reason."""
        monkeypatch.setattr("src.nodes.call_infomart_api_node.emit_trace_event", lambda *a, **k: None)
        from src.services.infomart_client import InfomartApiError

        class _ApiErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_order(self, order_no, api_token):
                raise InfomartApiError(403, f"denied for order {_ORDER_NO} from {_SUPPLIER}")

        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _ApiErrorClient)
        out = CallInfomartApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "403" in joined, "the HTTP status is the actionable signal - keep it"
        assert "denied for order" not in joined, "upstream error body reached the caller-facing reason"
        assert _ORDER_NO not in joined
        assert _SUPPLIER not in joined

    def test_transport_failure_reason_carries_only_the_exception_type(self, monkeypatch):
        """A transport error string can carry the request URL and the record id."""
        monkeypatch.setattr("src.nodes.call_infomart_api_node.emit_trace_event", lambda *a, **k: None)

        class _TransportErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_order(self, order_no, api_token):
                raise ConnectionError(f"GET https://api.infomart.co.jp/orders/{_ORDER_NO} failed")

        monkeypatch.setattr("src.nodes.call_infomart_api_node.InfomartClient", _TransportErrorClient)
        out = CallInfomartApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "ConnectionError" in joined, "the exception TYPE is the actionable signal - keep it"
        assert "api.infomart.co.jp" not in joined, "the request URL reached the caller-facing reason"
        assert _ORDER_NO not in joined
