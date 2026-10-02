"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the looked-up order / received-invoice list / supplier record
(reference + label + aggregate) into a human-readable confirmation message,
surfacing the referenced record for human review.

Rendering contract (docs/02, "External output schema"): every identifier is
emitted behind a `#` or inside an `infomart://` reference, never as a bare
token, so the output boundary can always tell an identifier from an amount.
Billed figures are emitted only as the already-rounded aggregate.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "lookup_order": "Retrieved order status",
    "list_invoices": "Listed received invoices",
    "lookup_supplier": "Retrieved supplier record",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-fetched data - the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        record_label = state.get("record_label", "")
        amount_total = state.get("amount_total", "")
        intent = state.get("intent", "lookup_order")

        if not record_id and not record_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed Infomart request")
        # Identifiers render only behind `#` (inside the label) or inside the
        # `infomart://` reference. A bare identifier is never emitted: at the
        # output boundary a standalone digit run is indistinguishable from an
        # unmarked amount, and a purely numeric order number would be rewritten
        # onto the monetary grid.
        parts = [f"{verb} '{record_label or ('#' + record_id)}'"]
        if record_ref:
            parts.append(f"ref={record_ref}")
        if amount_total:
            parts.append(f"total={amount_total}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_record_ref": bool(record_ref), "has_total": bool(amount_total)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
