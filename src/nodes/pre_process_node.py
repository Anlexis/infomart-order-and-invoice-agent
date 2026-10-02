"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and serialize it for the inner Infomart workflow
graph. This node OWNS the caller-data contract: every field a caller can supply
is checked here, against explicit bounds, before any of it reaches the
workflow. Business rules (intent, entity extraction) live in the inner graph.

Caller contract (`input_context`), every field optional:

    order_no / supplier_code / target_hint   the record the request is about
    order                                    an order head record the caller
                                             already holds:
                                             {order_no, supplier_code, status,
                                              amount}
    invoices                                 received-invoice records the
                                             caller already holds, a list of:
                                             {invoice_no, supplier_code,
                                              status, amount}
    supplier                                 a trading-partner record the
                                             caller already holds:
                                             {supplier_code, name}

Rules applied to all of them:
  - values must be the declared TYPE. A number, boolean, mapping or list where
    a string is required is refused outright, never coerced: str(float("nan"))
    is "nan", which passes an identifier shape check, so coercion would let a
    non-finite value name the order of a lookup.
  - every NUMBER goes through a finite + bounded parser. NaN and +/-Infinity
    parse fine through float() and arrive intact through raw JSON, and every
    comparison against NaN is False - a silent fail-OPEN on exactly the
    figures this agent totals. They are refused, along with out-of-range
    magnitudes.
  - every string that renders into the response is locked to an inert
    alphabet; free text there would be caller-controlled output.
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens) is
    REFUSED, fail closed, on BOTH caller text channels - the raw request text
    and every decoded string in input_context, keys included, at any depth -
    before anything is carried forward. The screen is the template's own
    (src/services/security.py), never delegated to the platform gate.
  - a credential-shaped string anywhere in the structured channel is REFUSED
    here as well as at the entry point. The framework scans every value of
    every node result for credential patterns and the backbone's first node
    returns this channel verbatim in its own result, so a credential in it
    fails the run at node one with an error the caller cannot act on. Refusing
    it in the node that owns the contract keeps that guarantee on every entry
    path, not only the one that goes through this repository's own adapter.
  - a refusal names the FIELD and never echoes the offending value; an
    unrecognised field NAME is masked, never echoed either.
  - absent fields are simply absent: the workflow falls back to what it can
    read out of the request text and to the built-in stub records.
"""

import json
import math
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    contains_instruction_override,
    find_credential_like,
    redact_sensitive,
    sanitize_query,
)

# An Infomart identifier (order number / invoice number / supplier code).
# Locked to an inert alphabet because these values render into the
# confirmation the caller reads back: anything wider would be
# caller-controlled output.
_INFOMART_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# A short display label (a supplier's trading name, an order status word).
# Wider than an identifier because trading names carry spaces and dots, but
# still inert: no markup characters, no quotes, no colons, no newlines.
_LABEL_RE = re.compile("^[A-Za-z0-9\u3040-\u30ff\u4e00-\u9fff]" "[A-Za-z0-9 ._\\-\u3040-\u30ff\u4e00-\u9fff]{0,63}$")

# Billed amounts are reported as an aggregate rounded to the nearest 1,000
# (docs/02 "External output schema"), so the bound is generous; the point of
# the cap is that a magnitude outside it is a malformed figure, not a large
# invoice.
_MAX_AMOUNT = 1_000_000_000_000.0
# How many invoice records one request may carry. A list read is summarised,
# so the cap bounds the work, not the answer.
_MAX_INVOICES = 200

_TARGET_ALIASES = ("order_no", "supplier_code", "target_hint")


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract. Carries the field name only.

    Two attributes, BOTH chosen by this module: `field` is the contract field
    path (a declared name, or a `invoices[N]` index), `code` is the refusal
    wording. The rejected VALUE is never part of either.

    They exist so the refusal notice can be rebuilt from them instead of
    interpolating the caught exception. `error_log` is the internal audit
    channel the state reducer accumulates, and an interpolated exception is
    precisely how text from outside this module gets into it.
    """

    def __init__(self, field: str, code: str) -> None:
        self.field = field
        self.code = code
        super().__init__(f"'{field}' {code}")


# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported path
# shows a placeholder - an unrecognised field NAME is never echoed either.
# Public: the entry point screens the same channel and masks names the same way.
KNOWN_CONTEXT_FIELDS = frozenset(
    {
        "order_no",
        "supplier_code",
        "target_hint",
        "order",
        "invoices",
        "invoice_no",
        "status",
        "amount",
        "supplier",
        "name",
    }
)


def _find_instruction_override(value: object, path: str = "input_context") -> "str | None":
    """Depth-first scan of every decoded string in the mapping - keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON \\u
    escaping cannot smuggle a phrase past it, and it covers undeclared keys
    too: the screen must hold on what the caller SENT, not only on what the
    contract keeps. Path components outside the declared contract are masked,
    so the returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in KNOWN_CONTEXT_FIELDS else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _require_text(value: object, field: str, max_len: int) -> str:
    """Return a bounded string, or raise naming the field (never the value).

    Rejects every non-string type, `bool` included, so numeric input cannot be
    stringified into something that satisfies a downstream shape check.
    """
    if not isinstance(value, str):
        raise CallerFieldError(field, "must be a string")
    text = value.strip()
    if not text:
        raise CallerFieldError(field, "must not be empty")
    if len(text) > max_len:
        raise CallerFieldError(field, f"exceeds the {max_len}-character limit")
    return text


def _validate_infomart_id(raw: object, field: str) -> str:
    """Validate an Infomart identifier: inert alphabet, length-bounded."""
    text = _require_text(raw, field, 32).lstrip("#")
    if not _INFOMART_ID_RE.match(text):
        raise CallerFieldError(field, "is not a valid Infomart identifier")
    return text


def _validate_label(raw: object, field: str) -> str:
    """Validate a short display label: inert alphabet, length-bounded."""
    text = _require_text(raw, field, 64)
    if not _LABEL_RE.match(text):
        raise CallerFieldError(field, "contains characters that are not permitted in a label")
    return text


def _finite_in_range(raw: object, field: str, minimum: float, maximum: float) -> float:
    """Parse a caller number that must be FINITE and inside an explicit range.

    Fails CLOSED, naming the field. `bool` is rejected before `int`/`float`
    (True is 1 in Python), strings are parsed rather than coerced, and the
    finiteness check is what stops NaN / Infinity - both of which survive
    float() and raw JSON, and both of which make every subsequent comparison
    False rather than raising.
    """
    if isinstance(raw, bool):
        raise CallerFieldError(field, "must be a number")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            raise CallerFieldError(field, "must be a number") from None
    else:
        raise CallerFieldError(field, "must be a number")
    if not math.isfinite(value):
        raise CallerFieldError(field, "must be a finite number")
    if not (minimum <= value <= maximum):
        raise CallerFieldError(field, "is outside the permitted range")
    return value


def _validate_record(raw: object, field: str, spec: "dict[str, str]") -> "dict[str, Any]":
    """Validate one caller-supplied Infomart record against its field spec.

    `spec` maps a record key to its kind: "id", "amount" or "label". Unknown
    keys are dropped rather than carried - the record that reaches the workflow
    contains only fields this contract validated.
    """
    if not isinstance(raw, dict):
        raise CallerFieldError(field, "must be a mapping")
    record: dict[str, Any] = {}
    for key, kind in spec.items():
        value = raw.get(key)
        if value is None:
            continue
        qualified = f"{field}.{key}"
        if kind == "id":
            record[key] = _validate_infomart_id(value, qualified)
        elif kind == "amount":
            record[key] = _finite_in_range(value, qualified, 0.0, _MAX_AMOUNT)
        else:  # "label"
            record[key] = _validate_label(value, qualified)
    return record


_ORDER_SPEC = {
    "order_no": "id",
    "supplier_code": "id",
    "status": "label",
    "amount": "amount",
}
_INVOICE_SPEC = {
    "invoice_no": "id",
    "supplier_code": "id",
    "status": "label",
    "amount": "amount",
}
_SUPPLIER_SPEC = {
    "supplier_code": "id",
    "name": "label",
}


def validate_caller_fields(input_context: object) -> "dict[str, Any]":
    """Validate the caller contract. Raises CallerFieldError on any breach."""
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("input_context", "must be a mapping")

    fields: dict[str, Any] = {}

    supplied = [k for k in _TARGET_ALIASES if input_context.get(k) is not None]
    if supplied:
        fields["target_hint"] = _validate_infomart_id(input_context[supplied[0]], supplied[0])

    if input_context.get("order") is not None:
        order = _validate_record(input_context["order"], "order", _ORDER_SPEC)
        if order:
            fields["order"] = order

    if input_context.get("supplier") is not None:
        supplier = _validate_record(input_context["supplier"], "supplier", _SUPPLIER_SPEC)
        if supplier:
            fields["supplier"] = supplier

    raw_invoices = input_context.get("invoices")
    if raw_invoices is not None:
        if not isinstance(raw_invoices, list):
            raise CallerFieldError("invoices", "must be a list")
        if len(raw_invoices) > _MAX_INVOICES:
            raise CallerFieldError("invoices", f"exceeds the {_MAX_INVOICES}-entry limit")
        invoices = [
            _validate_record(entry, f"invoices[{index}]", _INVOICE_SPEC) for index, entry in enumerate(raw_invoices)
        ]
        invoices = [entry for entry in invoices if entry]
        if invoices:
            fields["invoices"] = invoices

    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Infomart call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ────────
        # Runs on BOTH caller text channels before anything is carried
        # forward: a refusal leaves no validated_input, no target_hint and no
        # caller_fields for any downstream node. The screen lives in this
        # node's own execute() path - calling execute() directly still
        # refuses, so the guarantee does not depend on any platform gate being
        # present or configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        credential_at = find_credential_like(input_context, safe_names=KNOWN_CONTEXT_FIELDS)
        if credential_at is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "credential_in_context", "where": credential_at},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - credential-shaped value in {credential_at}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field only - the rejected value is never
            # echoed into the log. The notice is REBUILT from the exception's
            # field path and refusal code (both chosen by this module) rather
            # than interpolating the exception itself: an interpolated
            # exception is how text from outside a module reaches error_log,
            # and error_log is the audit channel the reducer accumulates.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - '{exc.field}' {exc.code}"],
            }

        # Strip markup + cap length, then flag-and-redact before serialization.
        sanitized_input, _ = redact_sensitive(sanitize_query(user_input.strip()))

        target_hint = str(caller_fields.get("target_hint", ""))
        validated_input = json.dumps({"text": sanitized_input, "target_hint": target_hint})

        # Audit the shaped request - field presence only, never the text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_target_hint": bool(target_hint),
                "caller_fields": sorted(caller_fields),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "target_hint": target_hint,
            # Stored as a JSON string (the state contract keeps every value
            # msgpack-safe); the graph node reads it back at the boundary.
            "caller_fields": to_json(caller_fields),
            "status": AgentStatus.SUCCESS.value,
        }
