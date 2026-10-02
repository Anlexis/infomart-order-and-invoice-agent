# CMN-C2-286 - Unit tests: inner InfomartWorkflowGraph (BaseGraph) contract.
# Adapted from the peer template tool-calling golden. The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import InfomartWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return InfomartWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "infomart_order_invoice_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_infomart_config_as_json():
    g = _graph({"configurable": {"infomart": {"base_url": "https://infomart.example.test/v1"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict (msgpack-safe state).
    assert isinstance(extra["infomart_config"], str)
    assert from_json(extra["infomart_config"], {}) == {"base_url": "https://infomart.example.test/v1"}


def test_extra_initial_state_carries_the_runtime_timeout():
    """The declared timeout_s reaches the inner graph as an integration setting."""
    g = _graph({"configurable": {"infomart": {"base_url": "https://x.test/v1"}, "agent": {"timeout_s": 12}}})
    settings = from_json(g._extra_initial_state()["infomart_config"], {})
    assert settings["timeout_s"] == 12


def test_extra_initial_state_prefers_an_explicit_integration_timeout():
    g = _graph(
        {"configurable": {"infomart": {"base_url": "https://x.test/v1", "timeout_s": 5}, "agent": {"timeout_s": 12}}}
    )
    settings = from_json(g._extra_initial_state()["infomart_config"], {})
    assert settings["timeout_s"] == 5


def test_extra_initial_state_without_a_section_carries_only_the_caller_bridge():
    extra = _graph()._extra_initial_state()
    assert "infomart_config" not in extra
    assert extra["input_context"] == {}


def test_extra_initial_state_reads_the_caller_bridge():
    """The framework does not forward input_context into a subgraph, so the
    validated caller contract is read back off the bridge inside invoke()."""
    from src.graph.context_bridge import set_caller_input_context

    set_caller_input_context({"target_hint": "o-2001"})
    try:
        assert _graph()._extra_initial_state()["input_context"] == {"target_hint": "o-2001"}
    finally:
        set_caller_input_context(None)


def test_route_annotation_is_the_graphs_own_state():
    """The graph runtime reads a path callable's annotation as its input schema
    and projects away every field the annotation does not declare, so a route
    annotated with the generic base state would never see the domain fields."""
    import typing

    hints = typing.get_type_hints(InfomartWorkflowGraph.route)
    assert hints["state"] is State


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "o-2001", "record_ref": "infomart://orders/o-2001", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_order",
            "target_id": "o-2001",
            "record_id": "o-2001",
            "record_ref": "infomart://orders/o-2001",
            "record_label": "o-2001 (accepted)",
            "confirmation": "ok",
            "infomart_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_order"
    assert out["record_ref"] == "infomart://orders/o-2001"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "o-2001", "record_ref": "infomart://orders/o-2001", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_order_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node declares
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"infomart": {"base_url": "https://api.infomart.co.jp/v1"}}})
    g.compile()
    result = g.invoke(user_input="Look up the status of order o-2001 and summarize the record on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "o-2001"
    assert result["record_ref"] == "infomart://orders/o-2001"
    assert result["intent"] == "lookup_order"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferInfomartFieldsNode",
        "CallInfomartApiNode",
        "ConfirmNode",
    ]
