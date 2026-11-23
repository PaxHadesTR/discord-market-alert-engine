import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_SECRET


def test_reject_unauthenticated_request(client: TestClient):
    payload = {"ticker": "BTCUSDT", "action": "BUY", "price": 60000}
    response = client.post("/webhook", json=payload)
    assert response.status_code == 401
    assert "Unauthorized" in response.json()["detail"]


def test_reject_wrong_secret(client: TestClient):
    payload = {"ticker": "BTCUSDT", "action": "BUY", "passphrase": "wrong_password"}
    response = client.post("/webhook", json=payload)
    assert response.status_code == 401


def test_auth_via_json_passphrase(client: TestClient):
    payload = {
        "passphrase": TEST_SECRET,
        "ticker": "BTCUSDT",
        "action": "BUY",
        "price": 67000.0,
    }
    response = client.post("/webhook", json=payload)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["ticker"] == "BTCUSDT"


def test_auth_via_query_parameter(client: TestClient):
    payload = {"ticker": "ETHUSDT", "action": "SELL", "price": 3500.0}
    response = client.post(f"/webhook?secret={TEST_SECRET}", json=payload)
    assert response.status_code == 202
    assert response.json()["ticker"] == "ETHUSDT"


def test_auth_via_header(client: TestClient):
    payload = {"ticker": "SOLUSDT", "action": "BUY", "price": 180.0}
    headers = {"X-Webhook-Secret": TEST_SECRET}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 202
    assert response.json()["ticker"] == "SOLUSDT"


def test_auth_via_path_token(client: TestClient):
    payload = {"ticker": "BNBUSDT", "action": "BUY", "price": 590.0}
    response = client.post(f"/webhook/{TEST_SECRET}", json=payload)
    assert response.status_code == 202
    assert response.json()["ticker"] == "BNBUSDT"


def test_auth_via_path_token_invalid(client: TestClient):
    payload = {"ticker": "BNBUSDT", "action": "BUY", "price": 590.0}
    response = client.post("/webhook/invalid_secret_token", json=payload)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "weak_secret",
    [
        "",
        "   ",
        "change_me_in_production",
        "your_super_secret_token_here",
        "buraya_guclu_bir_sifre_veya_token",
    ],
)
def test_webhook_fails_closed_for_empty_or_placeholder_secret(client: TestClient, monkeypatch, weak_secret: str):
    """F-61fc1f7627ba: Fail-closed when WEBHOOK_SECRET is empty, whitespace, or placeholder."""
    from app.config import get_settings

    monkeypatch.setenv("WEBHOOK_SECRET", weak_secret)
    get_settings.cache_clear()

    payload = {"ticker": "BTCUSDT", "action": "BUY", "price": 65000.0, "passphrase": weak_secret}
    resp_root = client.post("/webhook", json=payload)
    assert resp_root.status_code == 401

    resp_path = client.post(f"/webhook/{weak_secret.strip() or 'anything'}", json=payload)
    assert resp_path.status_code == 401


def test_verify_secret_uses_constant_time_compare_without_short_circuit(monkeypatch):
    """SEC-AUTH-002: _verify_secret uses hmac.compare_digest on UTF-8 bytes across all non-None candidates."""
    import hmac
    from unittest.mock import MagicMock
    from app.routes import webhook as webhook_mod

    spy = MagicMock(wraps=hmac.compare_digest)
    monkeypatch.setattr(webhook_mod.hmac, "compare_digest", spy)

    assert webhook_mod._verify_secret(
        configured_secret="my_strong_secret_42",
        path_secret="  my_strong_secret_42  ",
        query_secret="wrong",
        header_secret="also_wrong",
    ) is True
    # All 3 non-None candidates evaluated without short-circuiting
    assert spy.call_count == 3

    spy.reset_mock()
    assert webhook_mod._verify_secret(
        configured_secret="my_strong_secret_42",
        query_secret="wrong",
        payload_passphrase="",
    ) is False
    assert spy.call_count == 2


def test_webhook_rejects_oversized_body_before_parsing(client: TestClient, monkeypatch):
    """SEC-DOS-001: Bodies > 64 KiB (with Content-Length or streamed) return 413 without parsing."""
    from unittest.mock import MagicMock
    from app.routes import webhook as webhook_mod

    parse_spy = MagicMock(wraps=webhook_mod.parse_tradingview_payload)
    monkeypatch.setattr(webhook_mod, "parse_tradingview_payload", parse_spy)

    oversized = b"A" * (webhook_mod.MAX_WEBHOOK_BODY_BYTES + 1)

    # 1. Oversized POST /webhook with Content-Length -> 413, parser never called
    r1 = client.post("/webhook", content=oversized)
    assert r1.status_code == 413
    parse_spy.assert_not_called()

    # 2. Oversized POST /webhook/{valid_token} with chunked/streaming generator (no Content-Length) -> 413
    def _chunk_gen():
        for _ in range(17):
            yield b"B" * 4096  # 68 KiB total

    r2 = client.post(f"/webhook/{TEST_SECRET}", content=_chunk_gen())
    assert r2.status_code == 413
    parse_spy.assert_not_called()

    # 3. Invalid path/query/header secret short-circuits to 401 before parsing oversized body
    r3 = client.post("/webhook/bad_token", content=oversized)
    assert r3.status_code == 401
    parse_spy.assert_not_called()

    r4 = client.post("/webhook?secret=wrong", content=oversized)
    assert r4.status_code == 401
    parse_spy.assert_not_called()

    r5 = client.post("/webhook", content=oversized, headers={"X-Webhook-Secret": "wrong"})
    assert r5.status_code == 401
    parse_spy.assert_not_called()


def test_webhook_returns_503_when_queue_is_full(client: TestClient, monkeypatch):
    """V6-COVERAGE-001: Full AlertQueueService causes POST /webhook and POST /webhook/{token} to return HTTP 503."""
    from unittest.mock import MagicMock

    queue_service = client.app.state.queue_service
    monkeypatch.setattr(queue_service, "enqueue", MagicMock(return_value=False))

    payload = {"ticker": "BTCUSDT", "action": "BUY", "price": 67000.0, "passphrase": TEST_SECRET}
    r_root = client.post("/webhook", json=payload)
    assert r_root.status_code == 503

    r_path = client.post(f"/webhook/{TEST_SECRET}", json={"ticker": "ETHUSDT", "action": "SELL", "price": 3400.0})
    assert r_path.status_code == 503
