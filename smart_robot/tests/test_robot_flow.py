"""End-to-end tests for the robot's brain, running the offline mock provider.

These cover the conversation the robot actually has on the shop floor: a
stock question, a price question, the three return outcomes, and a camera
scan of both an invoice and a part sticker.

Run from the `smart_robot/` directory:

    pip install -r requirements.txt pytest
    pytest
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend import database as db
from backend.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def session(client: TestClient) -> str:
    return client.post("/api/session").json()["session_id"]


def say(client: TestClient, session: str, message: str) -> str:
    res = client.post("/api/chat", json={"message": message, "session_id": session})
    assert res.status_code == 200
    return res.json()["reply"]


def scan(client: TestClient, session: str, code: str) -> str:
    res = client.post("/api/scan", json={"code": code, "session_id": session})
    assert res.status_code == 200
    return res.json()["reply"]


def test_health_reports_the_active_provider(client: TestClient):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["provider"]  # "mock" with no API key set


def test_session_opens_with_a_greeting(client: TestClient):
    body = client.post("/api/session").json()
    assert body["session_id"]
    assert "MOUS" in body["reply"]


def test_part_in_stock_is_quoted_with_price(client: TestClient, session: str):
    reply = say(client, session, "I need a steering rack for my F30")
    assert "Steering Rack" in reply
    assert "14250" in reply


def test_part_out_of_stock_is_reported(client: TestClient, session: str):
    reply = say(client, session, "Do you have front brake pads for an F30?")
    assert "out of stock" in reply.lower()


def test_price_question_answers_with_the_price(client: TestClient, session: str):
    reply = say(client, session, "How much is an oil filter?")
    assert "320" in reply


def test_return_inside_the_window_goes_to_the_cashier(client: TestClient, session: str):
    # INV-2025-0788 was bought 5 days ago, inside the 14-day window.
    reply = say(client, session, "I want to return this, my invoice is INV-2025-0788")
    assert "refund" in reply.lower()


def test_past_the_window_but_under_warranty_is_a_warranty_claim(
    client: TestClient, session: str
):
    # INV-2025-0455 was bought 40 days ago on a 6-month warranty.
    reply = say(client, session, "I want to return invoice INV-2025-0455")
    assert "warranty" in reply.lower()


def test_past_window_and_warranty_is_declined_kindly(client: TestClient, session: str):
    # INV-2024-0912 was bought 400 days ago on a 6-month warranty.
    reply = say(client, session, "Return for invoice INV-2024-0912")
    assert "sorry" in reply.lower()


def test_a_return_can_be_found_by_phone_number(client: TestClient, session: str):
    reply = say(client, session, "I want a return, my phone is 01001234567")
    assert "Oil Filter" in reply  # their most recent invoice


def test_scanning_an_invoice_validates_the_return(client: TestClient, session: str):
    reply = scan(client, session, "INV-2025-0788")
    assert "refund" in reply.lower()


def test_scanning_a_part_sticker_is_a_sale_not_a_return(client: TestClient, session: str):
    # The camera reads invoices and part stickers alike, so the code itself —
    # not the wording of the prompt — has to decide which flow runs.
    reply = scan(client, session, "11427953129")
    assert "Oil Filter" in reply
    assert "in stock" in reply.lower()
    assert "invoice" not in reply.lower()


def test_an_unreadable_code_asks_the_customer_to_try_again(
    client: TestClient, session: str
):
    reply = scan(client, session, "XYZ-NOT-A-CODE")
    assert "couldn't match" in reply.lower()


def test_the_frontend_is_served(client: TestClient):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_return_eligibility_is_decided_by_the_purchase_date():
    res = db.validate_return(invoice_number="INV-2025-0788")
    assert res["found"] is True
    assert res["return_eligible"] is True
    assert res["return_window_days"] == db.RETURN_WINDOW_DAYS

    stale = db.validate_return(invoice_number="INV-2024-0912")
    assert stale["return_eligible"] is False
    assert stale["under_warranty"] is False


def test_an_unknown_invoice_is_not_found():
    assert db.validate_return(invoice_number="INV-9999-0001")["found"] is False


def test_mock_invoice_dates_stay_relative_to_today():
    # The fixtures are built from date.today(), so the "recent purchase"
    # scenario keeps working as time passes instead of ageing out.
    res = db.validate_return(invoice_number="INV-2025-0788")
    expected = (date.today() - timedelta(days=5)).isoformat()
    assert res["purchase_date"] == expected
