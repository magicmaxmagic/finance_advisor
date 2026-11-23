"""Personal revisions persist independently of any price warehouse."""

from fastapi.testclient import TestClient

from finlab.api.app import app


def test_local_ledger_persists_revisions_detects_conflicts_and_validates(monkeypatch, settings):
    monkeypatch.setattr("finlab.config.get_settings", lambda: settings)
    url = "/api/portfolio/ledger"
    first = TestClient(app)
    assert first.get(url).json()["revision"] == 0
    legacy = {
        "base": "USD",
        "trades": [
            {
                "id": "one",
                "ticker": "US:123:AAPL",
                "date": "2020-01-02",
                "side": "buy",
                "quantity": 5,
                "price": 100,
                "currency": "USD",
                "fee": 0,
                "rate": 1,
            }
        ],
    }
    written = first.put(url, json={"expected_revision": 0, "ledger": legacy})
    assert written.status_code == 200
    assert written.json()["revision"] == 1
    recovered = TestClient(app)
    assert recovered.get(url).json()["ledger"] == legacy
    assert recovered.put(url, json={"expected_revision": 0, "ledger": legacy}).status_code == 409
    duplicate = {**legacy, "trades": legacy["trades"] * 2}
    assert recovered.put(url, json={"expected_revision": 1, "ledger": duplicate}).status_code == 422
    assert (
        recovered.put(
            url, json={"expected_revision": 1, "ledger": {"base": "USD", "trades": []}}
        ).json()["revision"]
        == 2
    )
    assert recovered.get(url, params={"revision": 1}).json()["ledger"] == legacy
    assert (settings.data_dir / "personal" / "portfolio.sqlite").is_file()
    assert not settings.warehouse_dir.exists()
