"""AgentCore Platform v1.0 - inner Infomart workflow graph (Cat 2 domain workflow).

Instantiated by InfomartWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_infomart_fields
          -> call_infomart_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    infomart - runtime integration section (base_url, timeout_s override);
               injected into State as the JSON `infomart_config` field via
               _extra_initial_state() so the no-arg nodes can read it
    agent    - runtime values from config/config.yaml (max_retry, timeout_s)

Nodes are registered WITHOUT constructor arguments (nodes are no-arg; ctor args
raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.call_infomart_api_node import CallInfomartApiNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.confirm_node import ConfirmNode
from src.nodes.infer_infomart_fields_node import InferInfomartFieldsNode
from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import State, to_json


class InfomartWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "infomart_order_invoice_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the infomart section is optional (the client
        # falls back to the documented default base_url + the network-free
        # stub transport), and a missing/unusable setting is handled at
        # CallInfomartApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_infomart_fields"] = InferInfomartFieldsNode()
        self._nodes["call_infomart_api"] = CallInfomartApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_infomart_fields")
        self._sg.add_edge("infer_infomart_fields", "call_infomart_api")
        self._sg.add_edge("call_infomart_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by the base ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        #
        # The annotation is this graph's OWN State on purpose. The graph
        # runtime reads a path callable's annotation as that callable's input
        # schema and PROJECTS AWAY every field the annotation does not declare:
        # annotating a route with the generic base state hands it a state where
        # the domain routing fields are always absent, so the branch that
        # depends on them never runs - while unit tests that call route()
        # directly keep passing, because they pass a full dict themselves.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Forward the `infomart` section (arriving under config["configurable"]
        # from _parent_config()) into State as a JSON string (msgpack-safe) so
        # the no-arg CallInfomartApiNode can read it via
        # state.get("infomart_config").
        extra: dict[str, Any] = {}
        configurable = self.config.get("configurable") or {}
        infomart = dict(configurable.get("infomart") or {})
        agent_cfg = configurable.get("agent") or {}
        # The request timeout the integration honours is the runtime
        # timeout_s, unless the integration section overrides it explicitly.
        if "timeout_s" in agent_cfg and "timeout_s" not in infomart:
            infomart["timeout_s"] = agent_cfg["timeout_s"]
        if infomart:
            extra["infomart_config"] = to_json(infomart)
        # The framework does not forward input_context into a subgraph, so the
        # validated caller contract is read back off the bridge here - this
        # hook runs inside subgraph.invoke(), after the outer state is out of
        # reach.
        extra["input_context"] = get_caller_input_context()
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "target_id": state.get("target_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "record_label": state.get("record_label", ""),
            "record_count": state.get("record_count", 0),
            "amount_total": state.get("amount_total", ""),
            "confirmation": state.get("confirmation", ""),
            "infomart_payload": state.get("infomart_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
