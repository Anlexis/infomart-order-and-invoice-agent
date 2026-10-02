"""AgentCore Platform v1.0 - CMN-C2-286 Infomart Order & Invoice Agent state."""

# State must be a flat TypedDict - never a Pydantic model. Graph checkpoints use
# msgpack serialization; model objects (and nested dict/list containers) are not
# msgpack-safe. Extend AgentState with agent-specific fields only, and declare
# every domain field NotRequired[...] (fields are absent until their producer
# node writes them). infomart_payload / infomart_config / caller_fields /
# redaction_flags are dicts/lists at the point of use but are stored in State as
# JSON strings via to_json/from_json below. Do NOT add credentials, secrets, or
# model objects. The Infomart integration token is NEVER stored here - it is
# read via ctx.secrets in CallInfomartApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Infomart Order & Invoice agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Infomart-workflow fields are added below, all NotRequired (the state
    contract). All values are JSON/msgpack-serializable primitives - the
    Infomart integration token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hint (order number or supplier code from
    # input_context / the request envelope). Never inferred; resolution to an
    # Infomart order number / supplier code is explicit-only (pass-through when
    # the hint or request text already carries a code).
    target_hint: NotRequired[str]
    target_id: NotRequired[str]  # resolved Infomart order number / supplier code

    # The VALIDATED caller contract produced by PreProcessNode: the order,
    # invoice and supplier records the caller already holds, each field checked
    # against its declared type, shape, range and length. Stored as a JSON
    # string; the graph node reads it back at the subgraph boundary and puts it
    # on the caller-context bridge (the framework does not forward
    # input_context into a subgraph).
    caller_fields: NotRequired[Optional[str]]

    # ValidateInput (deterministic sensitive-string scan)
    # JSON list[str] of pattern categories redacted from the text before
    # logging (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferInfomartFields
    record_label: NotRequired[str]  # human-readable record label (supplier name, order status)
    # JSON - assembled Infomart BtoB Platform API request parameters (stored as
    # a JSON string, not a native dict; (de)serialize via to_json/from_json).
    infomart_payload: NotRequired[Optional[str]]

    # Manifest `infomart:` runtime section forwarded by _parent_config() and
    # injected by the inner graph's _extra_initial_state() (JSON string).
    infomart_config: NotRequired[Optional[str]]

    # CallInfomartApi
    record_id: NotRequired[str]  # order number / invoice number / supplier code returned by Infomart
    record_ref: NotRequired[str]  # human-readable reference (infomart://orders/<no>)
    record_count: NotRequired[int]  # number of records in a list read (structural, never monetary)
    # Aggregate billed amount, already rendered onto the external precision
    # grid. A string because it is display text, not a figure to compute with -
    # per-record amounts are never carried here or rendered.
    amount_total: NotRequired[str]

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
