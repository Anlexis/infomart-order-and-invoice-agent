"""AgentCore Platform v1.0 - Infomart BtoB Platform REST API client.

Service layer: a thin wrapper around the Infomart BtoB Platform (order &
invoice SaaS) REST API read endpoints - orders, received invoices, and
supplier (trading-partner) records. Contains NO business logic, NO routing,
and NO credentials - the integration token is passed in per call by the node
(which reads it via ctx.secrets). This module imports no framework internals -
pure stdlib, which is what keeps the service layer independently testable.

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Infomart response shapes (an ``order_data`` list for order-status
    lookups, an ``invoice_data`` list for received-invoice listings, and a
    ``supplier_data`` list for supplier-record lookups, derived from the
    request) so the pipeline is runnable and testable without a live Infomart
    tenant or an HTTP client package - it does NOT perform a live Infomart
    call. Never fake a live call; document the limitation.

    To perform real Infomart calls, inject a live transport at construction
    time; the method contracts and parameter shapes follow the Infomart BtoB
    Platform API surface (orders / invoices / suppliers), so no business-logic
    change is needed to go live. A live transport also requires a real
    integration token (see CallInfomartApiNode - the stub runs without one
    because no request ever leaves the process).

    A transport is called as ``get(url, headers, params, timeout_s)``. The
    timeout is passed on every call rather than fixed at construction, so the
    value declared in ``config/config.yaml`` is the one a live HTTP client
    actually applies.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable

# A transport callable: (url, headers, params, timeout_s) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, str]", "dict[str, Any]", float], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.infomart.co.jp/v1"
# Applied when the runtime config declares no timeout.
DEFAULT_TIMEOUT_S = 30.0
# A request timeout outside this range is a malformed setting, not a policy.
_MAX_TIMEOUT_S = 600.0


class InfomartApiError(Exception):
    """Raised when the Infomart REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Infomart API error {status_code}: {message}")


class InfomartClient:
    """Infomart BtoB Platform order / invoice / supplier read client.

    Args:
        base_url: Infomart API base URL (default https://api.infomart.co.jp/v1).
        get: optional injected transport (tests or a live client). When none is
            injected, a deterministic NETWORK-FREE stub is used (see the module
            docstring - it returns the documented shape without a live Infomart
            call). Every operation is a READ (GET) - this client exposes no
            write methods.
        timeout_s: per-request timeout handed to the transport on every call.
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        get: Transport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._get = get
        self._timeout_s = DEFAULT_TIMEOUT_S
        self.timeout_s = timeout_s

    # -- settings --------------------------------------------------------------

    @property
    def timeout_s(self) -> float:
        """Per-request timeout, in seconds, handed to the transport."""
        return self._timeout_s

    @timeout_s.setter
    def timeout_s(self, value: float) -> None:
        """Set the per-request timeout. Fails CLOSED on a malformed setting.

        A non-finite value is the case worth naming: NaN and Infinity both
        survive ``float()``, and every comparison against NaN is False - so an
        unchecked NaN would pass a range test and reach an HTTP client as its
        timeout.

        The rejected value is never part of the message: the setting is named,
        not echoed.
        """
        if isinstance(value, bool):
            raise ValueError("timeout_s must be a number of seconds")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise ValueError("timeout_s must be a number of seconds") from None
        if not math.isfinite(seconds):
            raise ValueError("timeout_s must be a finite number of seconds")
        if not (0 < seconds <= _MAX_TIMEOUT_S):
            raise ValueError(f"timeout_s must be greater than 0 and at most {_MAX_TIMEOUT_S:g} seconds")
        self._timeout_s = seconds

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the Infomart REST API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- deterministic stub transport (default; NO network) -------------------

    def _stub_transport(
        self, url: str, headers: "dict[str, str]", params: "dict[str, Any]", timeout_s: float
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free stub - returns the documented Infomart shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the
        limitation and how to inject a live transport.
        """
        seed = url + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = params.get("_infomart_op")
        if op == "order":
            code = str(params.get("order_no", "")) or f"o-{digest[:8]}"
            # Documented GET /orders shape: {"updated_at": ..., "order_data": [...]}
            return 200, {
                "updated_at": "1970-01-01 00:00:00",
                "order_data": [
                    {
                        "order_no": code,
                        "status": "accepted",
                        "items": [],
                    }
                ],
                "_stub": True,  # marks the network-free stub response
            }
        if op == "invoices":
            # Documented GET /invoices shape: {"updated_at": ..., "invoice_data": [...]}
            return 200, {
                "updated_at": "1970-01-01 00:00:00",
                "invoice_data": [
                    {
                        "invoice_no": f"inv-{digest[:8]}",
                        "status": "received",
                    }
                ],
                "_stub": True,  # marks the network-free stub response
            }
        # supplier (trading-partner) record lookup - documented GET /suppliers
        # shape: {"updated_at": ..., "supplier_data": [...]}
        code = str(params.get("supplier_code", "")) or f"s-{digest[:8]}"
        return 200, {
            "updated_at": "1970-01-01 00:00:00",
            "supplier_data": [
                {
                    "code": code,
                    "name": f"supplier-{code}",
                }
            ],
            "_stub": True,  # marks the network-free stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API (READ-ONLY) -----------------------------------------------

    def get_order(self, order_no: str, api_token: str) -> "dict[str, Any]":
        """GET /orders - look up an order's status record by order number.

        The live Infomart endpoint returns the order list; a live ``get``
        transport adapter is expected to filter to ``order_no`` (the stub
        returns the matching record directly). Returns the parsed response dict
        (containing ``order_data``). Raises InfomartApiError on non-2xx.
        """
        url = f"{self._base_url}/orders"
        transport = self._resolve(self._get)
        status, body = transport(
            url, self._headers(api_token), {"_infomart_op": "order", "order_no": order_no}, self._timeout_s
        )
        if not (200 <= status < 300):
            raise InfomartApiError(status, _err_message(body))
        return body

    def list_invoices(self, params: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """GET /invoices - list received invoices.

        ``params`` carries the read filters assembled by the caller (scope +
        optional "Key: value" filters). Returns the parsed response dict
        (containing ``invoice_data``). Raises InfomartApiError on a non-2xx
        status.
        """
        url = f"{self._base_url}/invoices"
        transport = self._resolve(self._get)
        query = dict(params)
        query["_infomart_op"] = "invoices"
        status, body = transport(url, self._headers(api_token), query, self._timeout_s)
        if not (200 <= status < 300):
            raise InfomartApiError(status, _err_message(body))
        return body

    def get_supplier(self, supplier_code: str, api_token: str) -> "dict[str, Any]":
        """GET /suppliers - look up a supplier (trading-partner) record by code.

        The live Infomart endpoint returns the supplier list; a live ``get``
        transport adapter is expected to filter to ``supplier_code`` (the stub
        returns the matching record directly). Returns the parsed response dict
        (containing ``supplier_data``). Raises InfomartApiError on non-2xx.
        """
        url = f"{self._base_url}/suppliers"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_infomart_op": "supplier", "supplier_code": supplier_code},
            self._timeout_s,
        )
        if not (200 <= status < 300):
            raise InfomartApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from an Infomart error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
