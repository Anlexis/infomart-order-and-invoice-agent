# CMN-C2-286 - Unit tests: InfomartClient service (Infomart BtoB Platform API shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.
# The client is READ-ONLY (orders / received invoices / supplier records - GET only).

import pytest

from src.services.infomart_client import DEFAULT_TIMEOUT_S, InfomartApiError, InfomartClient


def test_get_order_success_with_injected_get():
    captured = {}

    def get(url, headers, params, timeout_s):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return 200, {"updated_at": "2026-01-01 00:00:00", "order_data": [{"order_no": "o-2001", "status": "accepted"}]}

    client = InfomartClient("https://infomart.example.test/v1/", get=get)
    resp = client.get_order("o-2001", "tok123")
    assert resp["order_data"][0]["order_no"] == "o-2001"
    assert captured["url"] == "https://infomart.example.test/v1/orders"
    # Infomart REST API auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["params"]["order_no"] == "o-2001"


def test_list_invoices_success_with_injected_get():
    captured = {}

    def get(url, headers, params, timeout_s):
        captured["url"] = url
        captured["params"] = params
        return 200, {
            "updated_at": "2026-01-01 00:00:00",
            "invoice_data": [{"invoice_no": "inv-9", "status": "received"}],
        }

    client = InfomartClient("https://infomart.example.test/v1", get=get)
    resp = client.list_invoices({"scope": "received"}, "tok")
    assert resp["invoice_data"][0]["invoice_no"] == "inv-9"
    assert captured["url"] == "https://infomart.example.test/v1/invoices"
    # The caller's read filters are forwarded (plus the op discriminator).
    assert captured["params"]["scope"] == "received"


def test_get_supplier_success_with_injected_get():
    captured = {}

    def get(url, headers, params, timeout_s):
        captured["url"] = url
        captured["params"] = params
        return 200, {"updated_at": "2026-01-01 00:00:00", "supplier_data": [{"code": "s-77", "name": "Acme"}]}

    client = InfomartClient("https://infomart.example.test/v1", get=get)
    resp = client.get_supplier("s-77", "tok")
    assert resp["supplier_data"][0]["code"] == "s-77"
    assert captured["url"] == "https://infomart.example.test/v1/suppliers"
    assert captured["params"]["supplier_code"] == "s-77"


def test_non_2xx_raises_infomart_api_error():
    def get(url, headers, params, timeout_s):
        return 400, {"errors": ["order_no is malformed"]}

    client = InfomartClient("https://infomart.example.test/v1", get=get)
    with pytest.raises(InfomartApiError) as exc:
        client.get_order("o-2001", "tok")
    assert exc.value.status_code == 400
    assert "order_no is malformed" in str(exc.value)


def test_default_stub_transport_order_shape():
    # No transport injected -> deterministic, network-free stub.
    client = InfomartClient()
    assert client.uses_stub_transport is True
    resp = client.get_order("o-2001", "tok")
    assert resp.get("_stub") is True
    record = resp["order_data"][0]
    assert record["order_no"] == "o-2001"
    assert record["status"] == "accepted"


def test_default_stub_transport_invoice_shape():
    client = InfomartClient()
    resp = client.list_invoices({"scope": "received"}, "tok")
    assert resp.get("_stub") is True
    record = resp["invoice_data"][0]
    assert record["invoice_no"].startswith("inv-")
    assert record["status"] == "received"


def test_default_stub_transport_supplier_echoes_code():
    client = InfomartClient()
    resp = client.get_supplier("s-77", "tok")
    assert resp.get("_stub") is True
    record = resp["supplier_data"][0]
    assert record["code"] == "s-77"
    assert record["name"] == "supplier-s-77"


def test_injected_transport_disables_stub_flag():
    client = InfomartClient(get=lambda url, headers, params, timeout_s: (200, {"order_data": []}))
    assert client.uses_stub_transport is False


def test_configured_timeout_reaches_the_transport():
    """The declared request timeout is applied per call, not merely stored."""
    seen = {}

    def get(url, headers, params, timeout_s):
        seen["timeout_s"] = timeout_s
        return 200, {"order_data": [{"order_no": "o-2001"}]}

    client = InfomartClient("https://infomart.example.test/v1", get=get, timeout_s=12.5)
    client.get_order("o-2001", "tok")
    assert seen["timeout_s"] == 12.5


def test_default_timeout_is_applied_when_none_is_configured():
    seen = {}

    def get(url, headers, params, timeout_s):
        seen["timeout_s"] = timeout_s
        return 200, {"invoice_data": []}

    client = InfomartClient("https://infomart.example.test/v1", get=get)
    client.list_invoices({"scope": "received"}, "tok")
    assert seen["timeout_s"] == DEFAULT_TIMEOUT_S


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 0, -1, 10_000, True, "soon", None, ["30"]])
def test_malformed_timeout_fails_closed(bad):
    """NaN and Infinity survive float() and make every comparison False, so an
    unchecked value would reach an HTTP client as its timeout. Rejected."""
    with pytest.raises(ValueError) as exc:
        InfomartClient("https://infomart.example.test/v1", timeout_s=bad)
    assert "timeout_s" in str(exc.value)


def test_rejected_timeout_is_not_echoed():
    """The setting is named; the value that failed it is not."""
    with pytest.raises(ValueError) as exc:
        InfomartClient("https://infomart.example.test/v1", timeout_s="whenever-you-like")
    assert "timeout_s" in str(exc.value)
    assert "whenever-you-like" not in str(exc.value)
