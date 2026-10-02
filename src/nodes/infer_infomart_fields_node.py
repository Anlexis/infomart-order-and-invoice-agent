"""AgentCore Platform v1.0 - inner workflow Step 3: InferInfomartFields.

Extracts the order number / supplier code, record label, and "Key: value"
filter fields from the (redacted) request and assembles a validated Infomart
BtoB Platform API request-parameter set for the classified intent.

The order/supplier code is taken only from an explicit code in the text, the
caller-supplied target hint, or the caller's own record - an unresolved code is
left empty rather than invented (never read the wrong partner's records; the
executor surfaces the miss as status=error). Deterministic - no model is
invoked.

Everything this node carries forward is rendered back to the caller, so every
value it lifts out of the request text is re-checked against the same inert
alphabets the caller contract enforces: an identifier shape for codes, a
bounded label alphabet for names and filter terms. A term that does not fit is
dropped, not escaped.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# An Infomart order number / supplier code: short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit code mention in the request text, EN or JA
# ("order number 2001" / "supplier code: s-77" / "order o-2001").
# The qualified form accepts any code token; the bare form ("order o-2001")
# requires a digit in the token so plain words ("order status") never match.
_CODE_IN_TEXT_RE = re.compile(
    r"(?:order|invoice|supplier|partner)\s+(?:no\.?|number|code|id)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:order|invoice|supplier)\s+([A-Za-z0-9_-]{0,6}\d[A-Za-z0-9_-]{0,13})"
    r"|(?:\u6ce8\u6587\u756a\u53f7|\u53d7\u6ce8\u756a\u53f7|\u767a\u6ce8\u756a\u53f7|\u8acb\u6c42\u66f8\u756a\u53f7"
    r"|\u53d6\u5f15\u5148\u30b3\u30fc\u30c9|\u4ed5\u5165\u5148\u30b3\u30fc\u30c9)"
    r"\s*[:\uff1a#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted record label: named "Foo" / called "Foo" (supplier display names).
# Curly quotes as escapes so the source stays pure ASCII.
_NAME_QUOTED_RE = re.compile(r'(?:named|called|for)\s+["\u201c]([^"\u201d\n]+)["\u201d]', re.IGNORECASE)
# "Key: value" filter lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as escapes.
_KV_RE = re.compile(
    "^\\s*([A-Za-z\u3040-\u30ff\u4e00-\u9fff][\\w \\-\u3040-\u30ff\u4e00-\u9fff]{0,40})[:\uff1a]\\s*(.+?)\\s*$"
)
# A bounded, inert display alphabet - the same one the caller contract enforces
# on a supplier name. Anything outside it (markup, quotes, colons, newlines) is
# not escaped on the way out; the term is dropped instead.
_LABEL_RE = re.compile("^[A-Za-z0-9\u3040-\u30ff\u4e00-\u9fff]" "[A-Za-z0-9 ._\\-\u3040-\u30ff\u4e00-\u9fff]{0,63}$")
# Keys that are the code/label themselves, not filter fields.
_CODE_KEYS = ("code", "order no", "order number", "order code", "order id", "supplier code", "partner code", "id")
_NAME_KEYS = ("name", "supplier", "supplier name", "partner name")
# How many "Key: value" filters one request may contribute.
_MAX_FILTERS = 20


def _inert_label(value: str) -> str:
    """Return the value when it fits the display alphabet, else "".

    Dropping beats escaping: an escaped term still travels into the response as
    caller-authored text, and this template has no need for one.
    """
    text = value.strip()
    return text if _LABEL_RE.match(text) else ""


class InferInfomartFieldsNode(FunctionNode):
    """Extract entities and assemble the Infomart API request parameters."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_order") or "lookup_order"
        target_hint = state.get("target_hint", "") or ""
        caller = state.get("input_context") or {}

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferInfomartFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        target_id = self._resolve_code(text, target_hint, fields, caller)
        record_label = self._resolve_name(text, fields, caller)

        if intent == "list_invoices":
            payload = self._build_invoice_params(fields)
        elif intent == "lookup_supplier":
            payload = {"supplier_code": target_id}
        else:  # lookup_order (read-only default)
            payload = {"order_no": target_id}

        # Audit the assembled parameter shape - field signals only, not content.
        emit_trace_event(
            "infer_infomart_fields_complete",
            {"intent": intent, "has_target_id": bool(target_id), "n_fields": len(fields)},
            state,
        )

        return {
            "target_id": target_id,
            "record_label": record_label,
            "infomart_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_code(
        self,
        text: str,
        target_hint: str,
        fields: "list[tuple[str, str]]",
        caller: "dict[str, Any]",
    ) -> str:
        """Explicit code only, never invented.

        Precedence: a code named in the request text > the caller's target hint
        > a code on the caller's own record > a "Code:" filter line.
        """
        m = _CODE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or m.group(3) or ""
        hint = target_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        for record_key, code_key in (("order", "order_no"), ("supplier", "supplier_code")):
            record = caller.get(record_key)
            if isinstance(record, dict):
                code = str(record.get(code_key, "") or "")
                if code and _CODE_SHAPE_RE.match(code):
                    return code
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, fields: "list[tuple[str, str]]", caller: "dict[str, Any]") -> str:
        supplier = caller.get("supplier")
        if isinstance(supplier, dict) and supplier.get("name"):
            # Already validated against the caller contract's label alphabet.
            return str(supplier["name"])
        m = _NAME_QUOTED_RE.search(text)
        if m:
            return _inert_label(m.group(1))
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS:
                return _inert_label(value)
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] filter fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- parameter assembly (Infomart read-filter shape) -----------------------

    def _build_invoice_params(self, fields: "list[tuple[str, str]]") -> "dict[str, Any]":
        """Received-invoice listing parameters: scope + optional 'Key: value' filters.

        Both halves of every filter go back to the caller inside the rendered
        request parameters, so both are held to the display alphabet and a term
        that does not fit is dropped.
        """
        params: dict[str, Any] = {"scope": "received"}
        filters = []
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS + _NAME_KEYS:
                continue
            name, term = _inert_label(key), _inert_label(value)
            if not name or not term:
                continue
            filters.append({"name": name, "values": [term]})
            if len(filters) >= _MAX_FILTERS:
                break
        if filters:
            params["filters"] = filters
        return params
