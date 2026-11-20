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
