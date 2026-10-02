"""AgentCore Platform v1.0 - inner workflow Step 4: CallInfomartApi (tool side-effect).

Produces the order-status / received-invoice / supplier-record answer. Every
operation is a READ - this node performs no write.

Two data sources, in this order:

  1. the records the CALLER already holds, arriving on the request's structured
     channel and validated field by field by the outer pre_process before they
     reach here. When they are present the pipeline reports on the caller's own
     data and the aggregate is computed from it - real arithmetic on real
     figures, no network call needed;
  2. otherwise the Infomart BtoB Platform REST API via
     src/services/infomart_client.py, which by default runs the deterministic
     network-free stub described in that module.

Security posture:
  * trust: required_trust_level = ANONYMOUS. The single external trust gate
    lives on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this
    inner node. GraphNode.execute() passes the caller's InvocationContext into
    the inner subgraph UNCHANGED (no trust elevation), so a real external
    caller runs this call under its own VERIFIED_EXTERNAL context; declaring
    INTERNAL here would deny that already-gated external caller before the call
    ever runs. The node therefore stays ANONYMOUS.
  * credentials: the integration token is read via
    ctx.secrets.get("INFOMART_TOKEN") (InvocationContext.from_state(state)) -
    never os.environ, never stored in state. While the network-free stub
    transport is active a missing token is tolerated (a sentinel placeholder is
    used - it is never sent anywhere because no request leaves the process);
    with a LIVE transport injected, a missing token is a hard status=error - a
    real API is never called unauthenticated.
  * audit: emit_trace_event() is called on the success path - a side-effect
    against an external platform; HTTP 4xx/5xx surfaces as status=error +
    error_log (no silent pass).

Configuration: this node takes NO constructor arguments (nodes are no-arg).
Infomart settings (base_url, timeout_s) arrive as the JSON `infomart_config`
state field - injected by the inner graph's _extra_initial_state() from
config/config.yaml via InfomartWorkflowGraphNode._parent_config() - or via the
optional `config["configurable"]["infomart"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.infomart_client import InfomartApiError, InfomartClient

_SECRET_KEY = "INFOMART_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Fixed reasons for the two failures whose natural message would otherwise be
# written by something outside this module - a caught exception's `str()`, or a
# value read out of state. error_log is the audit channel and the state reducer
# accumulates it, so what goes in has to be this module's own words.
_REASON_BAD_TIMEOUT_SETTING = "CallInfomartApiNode: config/config.yaml timeout_s is not a usable number of seconds"
_REASON_UNKNOWN_INTENT = "CallInfomartApiNode: unknown intent - no Infomart read is defined for it"

# The external output schema reports billed amounts as an aggregate rounded to
# the nearest 1,000 (docs/02). The pipeline RENDERS on this grid here; the
# output gate in post_process independently ENFORCES it.
_EXTERNAL_ROUND_UNIT = 1000
# Infomart BtoB Platform is a domestic B2B service and settles in yen.
_CURRENCY = "JPY"


def render_aggregate(total: float) -> str:
    """Render a billed total onto the approved external grid.

    Aggregates only: the caller is told what a set of invoices comes to, never
    what any single one of them was.
    """
    snapped = round(total / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
    return f"{_CURRENCY} {snapped:,d}"


class CallInfomartApiNode(FunctionNode):
    """Answer the order / received-invoice / supplier read."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph),
    # so it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("infomart_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallInfomartApiNode: missing infomart_payload"],
            }

        intent = state.get("intent", "lookup_order") or "lookup_order"
        caller = state.get("input_context") or {}
        target_id = state.get("target_id", "") or str(
            payload.get("order_no", "") or payload.get("supplier_code", "") or ""
        )
        record_label = state.get("record_label", "") or ""

        # ── 1. the caller's own records, when supplied ──────────────────────
        answered = self._answer_from_caller_records(intent, caller, target_id, record_label)
        if answered is not None:
            emit_trace_event(
                "call_infomart_api_complete",
                {"intent": intent, "source": "caller_records", "has_record_id": bool(answered.get("record_id"))},
                state,
            )
            return answered

        # ── 2. the Infomart integration ────────────────────────────────────
        # Settings: runtime section from state (graph-injected), overridable via
        # an explicit config["configurable"]["infomart"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("infomart_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("infomart") or {}
        settings.update(override)

        base_url = str(settings.get("base_url", "") or "").strip()
        client = InfomartClient(base_url=base_url) if base_url else InfomartClient()
        timeout_s = settings.get("timeout_s")
        if timeout_s is not None:
            try:
                client.timeout_s = timeout_s
            except ValueError:
                # The SETTING is named, never the rejected value, and never the
                # caught exception: an interpolated exception is how upstream
                # text reaches error_log in the first place, so the reason is a
                # fixed label of this module's own.
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [_REASON_BAD_TIMEOUT_SETTING],
                }

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with a
                # non-credential placeholder (see the module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallInfomartApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        try:
            result = self._answer_from_integration(client, intent, payload, target_id, record_label, api_token)
        except InfomartApiError as exc:
            # HTTP status only. A live tenant's error body is unbounded
            # third-party text that can echo the record it refused (order,
            # supplier, amount), and InfomartApiError's own message embeds that
            # body - so the status is bound to a LOCAL first and the exception
            # itself never appears in the f-string. Interpolating `exc` (or
            # letting it near one) is exactly how an upstream response body
            # reaches error_log.
            status_code = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallInfomartApiNode: Infomart API error {status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception CLASS only, bound to a local for the same reason: a
            # transport error string can carry the request URL and the order
            # number.
            exc_class = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallInfomartApiNode: Infomart call failed ({exc_class})"],
            }

        if result.get("status") == AgentStatus.ERROR.value:
            return result

        # Audit the tool side-effect - intent + presence signals only, never
        # record content or credentials.
        emit_trace_event(
            "call_infomart_api_complete",
            {
                "intent": intent,
                "source": "infomart_api",
                "has_record_id": bool(result.get("record_id")),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )
        return result

    # -- caller-supplied records ----------------------------------------------

    def _answer_from_caller_records(
        self,
        intent: str,
        caller: "dict[str, Any]",
        target_id: str,
        record_label: str,
    ) -> "dict[str, Any] | None":
        """Answer from the records the caller sent, or None when it sent none.

        Every value here already passed the caller contract in pre_process, so
        no re-validation is needed - only the aggregate arithmetic, which is
        performed on the validated finite amounts.
        """
        if intent == "list_invoices":
            invoices = caller.get("invoices")
            if not isinstance(invoices, list) or not invoices:
                return None
            total = sum(float(entry.get("amount", 0.0) or 0.0) for entry in invoices)
            first_no = str((invoices[0] or {}).get("invoice_no", "") or "")
            return {
                "record_id": first_no,
                "record_ref": "infomart://invoices/received",
                "target_id": target_id or first_no,
                "record_label": record_label or f"{len(invoices)} received invoice(s)",
                "record_count": len(invoices),
                "amount_total": render_aggregate(total),
                "status": AgentStatus.SUCCESS.value,
            }

        if intent == "lookup_supplier":
            supplier = caller.get("supplier")
            if not isinstance(supplier, dict) or not supplier.get("supplier_code"):
                return None
            code = str(supplier["supplier_code"])
            return {
                "record_id": code,
                "record_ref": f"infomart://suppliers/{code}",
                "target_id": target_id or code,
                "record_label": record_label or str(supplier.get("name", "") or ""),
                "record_count": 1,
                "amount_total": "",
                "status": AgentStatus.SUCCESS.value,
            }

        order = caller.get("order")
        if not isinstance(order, dict) or not order.get("order_no"):
            return None
        order_no = str(order["order_no"])
        order_status = str(order.get("status", "") or "")
        amount = order.get("amount")
        return {
            "record_id": order_no,
            "record_ref": f"infomart://orders/{order_no}",
            "target_id": target_id or order_no,
            "record_label": record_label or (f"#{order_no} ({order_status})" if order_status else f"#{order_no}"),
            "record_count": 1,
            "amount_total": render_aggregate(float(amount)) if amount is not None else "",
            "status": AgentStatus.SUCCESS.value,
        }

    # -- the Infomart integration ---------------------------------------------

    def _answer_from_integration(
        self,
        client: InfomartClient,
        intent: str,
        payload: "dict[str, Any]",
        target_id: str,
        record_label: str,
        api_token: str,
    ) -> "dict[str, Any]":
        if intent == "lookup_order":
            if not target_id:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallInfomartApiNode: unresolved order number - cannot look up order status"],
                }
            resp = client.get_order(target_id, api_token) or {}
            orders = resp.get("order_data") or []
            if not orders:
                # The reason names the record TYPE (a closed-set label), never
                # the order number: error_log rides the caller-facing error
                # envelope, so interpolating the id would put the record
                # evidence straight back into it.
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallInfomartApiNode: no matching order record found"],
                }
            record = orders[0]
            record_id = str(record.get("order_no", "")) or target_id
            order_status = str(record.get("status", ""))
            label = record_label or (f"#{record_id} ({order_status})" if order_status else f"#{record_id}")
            return {
                "record_id": record_id,
                "record_ref": f"infomart://orders/{record_id}",
                "target_id": target_id or record_id,
                "record_label": label,
                "record_count": 1,
                "amount_total": "",
                "status": AgentStatus.SUCCESS.value,
            }

        if intent == "list_invoices":
            resp = client.list_invoices(payload, api_token) or {}
            invoices = resp.get("invoice_data") or []
            record_id = str((invoices[0] or {}).get("invoice_no", "")) if invoices else ""
            return {
                "record_id": record_id,
                # A list read always has a stable reference, even when empty.
                "record_ref": "infomart://invoices/received",
                "target_id": target_id or record_id,
                "record_label": f"{len(invoices)} received invoice(s)",
                "record_count": len(invoices),
                "amount_total": "",
                "status": AgentStatus.SUCCESS.value,
            }

        if intent == "lookup_supplier":
            if not target_id:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallInfomartApiNode: unresolved supplier code - cannot look up supplier record"],
                }
            resp = client.get_supplier(target_id, api_token) or {}
            suppliers = resp.get("supplier_data") or []
            if not suppliers:
                # Record TYPE only - see the order branch above.
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallInfomartApiNode: no matching supplier record found"],
                }
            record = suppliers[0]
            record_id = str(record.get("code", "")) or target_id
            return {
                "record_id": record_id,
                "record_ref": f"infomart://suppliers/{record_id}",
                "target_id": target_id or record_id,
                "record_label": record_label or str(record.get("name", "")),
                "record_count": 1,
                "amount_total": "",
                "status": AgentStatus.SUCCESS.value,
            }

        # The intent VALUE is not echoed: it is read out of state, so it is not
        # a label this module chose, and the branch is only reachable when it
        # is something the classifier never produces.
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [_REASON_UNKNOWN_INTENT],
        }
