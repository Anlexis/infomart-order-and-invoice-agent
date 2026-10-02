"""AgentCore Platform v1.0 - CMN-C2-286 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in InfomartWorkflowGraphNode (`main`
slot), which wraps the inner InfomartWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import _REASON_WORKFLOW_FAILED, PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json

if TYPE_CHECKING:  # import cycle: the inner graph imports the nodes this module wires
    from src.graph.domain_workflow_graph import InfomartWorkflowGraph

# Runtime parameters: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Runtime keys forwarded to the inner graph as its `agent` section.
_RUNTIME_KEYS = ("max_retry", "timeout_s")


def load_runtime_config() -> "dict[str, Any]":
    """Load config/config.yaml -> the dict passed to the graph as `config=`.

    The platform registry loads this file and constructs the agent with it; a
    standalone entry point must do the same, otherwise every declared runtime
    value (max_retry, timeout_s, the Infomart settings) is silently absent and
    the agent runs on defaults it never declared. Returns {} when the file is
    missing or unreadable - the pipeline then runs on its documented defaults
    rather than failing to start.
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}


class InfomartWorkflowGraphNode(GraphNode):
    """Wraps the inner Infomart workflow graph; assigned to the `main` slot.

    No constructor arguments (nodes are no-arg) - configuration reaches the
    subgraph via _parent_config(), which loads the runtime config.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "InfomartWorkflowGraph":
        from src.graph.domain_workflow_graph import InfomartWorkflowGraph

        return InfomartWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string);
        # the first inner node parses it back.
        #
        # The validated caller contract cannot ride along in that string: the
        # framework masks validated_input at every node boundary, so caller
        # values could be rewritten between hops. It is stashed on the bridge
        # here instead - the last point that still sees the outer state before
        # the framework invokes the subgraph without forwarding input_context.
        set_caller_input_context(from_json(state.get("caller_fields"), {}))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "target_id": sub_result.get("target_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "record_label": sub_result.get("record_label", ""),
            "record_count": sub_result.get("record_count", 0),
            "amount_total": sub_result.get("amount_total", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "infomart_payload": sub_result.get("infomart_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Reads config/config.yaml and forwards the `infomart:` integration
        section plus the runtime values (max_retry, timeout_s) as the `agent`
        section. An empty result would make every declared setting dead, so the
        values are read from the live file rather than assumed.
        """
        runtime = load_runtime_config()
        configurable: dict[str, Any] = {}
        infomart = runtime.get("infomart")
        if isinstance(infomart, dict) and infomart:
            configurable["infomart"] = infomart
        agent_cfg = {k: runtime[k] for k in _RUNTIME_KEYS if k in runtime}
        if agent_cfg:
            configurable["agent"] = agent_cfg
        return {"configurable": configurable}


class InfomartOrderInvoiceAgent(AgentBaseGraph):
    """CMN-C2-286 outer graph - Infomart Order & Invoice Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in InfomartWorkflowGraphNode (`main` slot); Infomart
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_286"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = InfomartWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
    # The backbone's own conditional edge (main -> route) uses the inherited
    # route(self, state), whose parameter carries NO annotation, so the graph
    # runtime hands it the full State. A path callable annotated with a
    # NARROWER schema than the graph's own would have its other fields
    # projected away before the call - see the note on
    # InfomartWorkflowGraph.route().
    #
    # Consequence of not overriding route(): AgentBaseGraph.route() sends any
    # non-SUCCESS status from `main` straight to `finalize`, so `post_process`
    # - and with it PostProcessNode's whole containment path, including the
    # closed-set `_REASON_WORKFLOW_FAILED` envelope - never runs when the
    # inner workflow fails. Without this override the caller (and the
    # platform runner) gets a bare `output: None` on every such failure, with
    # nothing to tell them the request was rejected rather than the Pod being
    # dead. get_output() reads directly off `state`, so it is reachable
    # regardless of which node the graph stopped at; it reuses
    # PostProcessNode's own reason constant rather than a second literal, so
    # the one label a caller can ever see for this outcome has one source.
    # `output.error` matches docs/07_operation_guide.md's documented error
    # shape; the `domain_error` sibling matches this fleet's own convention
    # for a runner-level diagnostic field, used by other get_output()
    # overrides across the template set.
    #
    # Scoped to a request pre_process ALREADY ACCEPTED: `validated_input` is
    # only ever written on PreProcessNode's success path (every refusal return
    # omits it), so its presence is what tells apart the two very different
    # non-SUCCESS shapes that reach this method with an empty `output` -
    # a request the inner workflow accepted and then failed on (this branch),
    # versus one pre_process itself refused (trust-gate denial, instruction-
    # override, malformed caller field, empty input). PB-6
    # (test_pb_invoke_order.py::test_under_trusted_caller_is_denied) and the
    # caller-contract e2e tests assert the SECOND shape stays completely bare
    # - a refusal this early must not confirm to an unvouched or malformed
    # caller that anything was even parsed - so this override must not widen
    # to cover it.
    def get_output(self, state: "dict[str, Any]") -> "dict[str, Any]":
        out = super().get_output(state)
        if (
            state.get("status") != AgentStatus.SUCCESS.value
            and not out.get("output")
            and state.get("validated_input")
        ):
            out["output"] = {"error": _REASON_WORKFLOW_FAILED}
            out["domain_error"] = _REASON_WORKFLOW_FAILED
        return out
