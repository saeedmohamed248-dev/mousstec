"""With MOUSS_ERP_API + MOUSS_ROBOT_TOKEN set, the kiosk's tools answer from
the real ERP (robot API), keep their return shapes, and never fall back to
mock data when the ERP is unreachable."""

import importlib
import io
import json
import urllib.error

import pytest


@pytest.fixture()
def erp_db(monkeypatch):
    monkeypatch.setenv("MOUSS_ERP_API", "https://shop.example/api/robot/v1")
    monkeypatch.setenv("MOUSS_ROBOT_TOKEN", "tok")
    from backend import database, erp_client
    importlib.reload(erp_client)
    db = importlib.reload(database)
    yield db, erp_client
    monkeypatch.delenv("MOUSS_ERP_API")
    monkeypatch.delenv("MOUSS_ROBOT_TOKEN")
    importlib.reload(erp_client)
    importlib.reload(database)


def _reply(payload, seen):
    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return io.BytesIO(json.dumps(payload).encode())
    return fake_urlopen


def test_stock_comes_from_the_erp(erp_db, monkeypatch):
    db, erp = erp_db
    seen = []
    monkeypatch.setattr(erp.urllib.request, "urlopen", _reply({
        "found": True, "part_number": "11517586925", "name": "Water pump",
        "in_stock": True, "stock": 4, "location": "B3", "fits": ["BMW E90"],
        "price": 3200.0, "currency": "EGP"}, seen))
    res = db.check_part_availability("water pump")
    assert res["stock"] == 4 and res["location"] == "B3"
    assert "price" not in res                        # same shape as the mock
    assert seen[0].full_url.startswith("https://shop.example/api/robot/v1/kiosk/part/")
    assert seen[0].get_header("X-robot-token") == "tok"


def test_unreachable_erp_is_not_replaced_by_mock_data(erp_db, monkeypatch):
    db, erp = erp_db

    def down(req, timeout=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(erp.urllib.request, "urlopen", down)
    res = db.check_part_availability("31126794339")   # exists in the mock table
    assert res["found"] is False
    assert res["error"] == "erp_unavailable"
