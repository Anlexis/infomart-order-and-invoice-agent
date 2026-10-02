"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_order / list_invoices /
lookup_supplier using a deterministic keyword heuristic, so the template is
testable and runnable without a language model (docs/02_design.md).
Low-confidence / unknown falls back to the read-only "lookup_order" default
with a note - every intent is a read, and the default is the narrowest one.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_order", "list_invoices", "lookup_supplier")

# Deterministic keyword signals (checked in priority order, most-specific
# first: "invoice" / "supplier" vocabularies are unambiguous, while "order"
# also appears in invoice/supplier phrasing - so the generic order vocabulary
# is matched last).
_KEYWORDS = (
    ("list_invoices", ("invoice", "invoices", "billing", "bill", "amount billed", "請求書", "請求", "支払")),
    (
        "lookup_supplier",
        ("supplier", "vendor", "trading partner", "partner record", "partner code", "取引先", "仕入先", "サプライヤ"),
    ),
    (
        "lookup_order",
        (
            "order",
            "orders",
            "status",
            "track",
            "delivery",
            "deliveries",
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "check",
            "受注",
            "注文",
            "発注",
            "納品",
            "照会",
            "検索",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into an Infomart order/invoice operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_order (read-only)"]
            intent = "lookup_order"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: dict[str, Any] = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
