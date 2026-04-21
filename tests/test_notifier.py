from __future__ import annotations

from app.jobs.notifier import _render_telegram_message


def test_render_price_drop_message_includes_links_and_drop_values() -> None:
    payload = {
        "notification_type": "price_drop",
        "product_name": "Test Product",
        "previous_amount": "100.00",
        "current_amount": "90.00",
        "currency": "USD",
        "affiliate_url": "https://go.example.com/out?u=https%3A%2F%2Fexample.com%2Fp%2F1",
        "listing_url": "https://merchant.example.com/buy/1",
        "product_url": "https://example.com/p/1",
        "triggered_at": "2026-04-18T12:00:00Z",
    }

    message = _render_telegram_message(payload)

    assert "Price dropped" in message
    assert "Item: Test Product" in message
    assert "Previous: 100.00 USD" in message
    assert "Now: 90.00 USD" in message
    assert "Buy: https://go.example.com/out?u=https%3A%2F%2Fexample.com%2Fp%2F1" in message
    assert "Listing: https://merchant.example.com/buy/1" in message
    assert "Product: https://example.com/p/1" in message
    assert "monitor update" not in message.lower()


def test_render_alert_message_uses_target_reached_template() -> None:
    payload = {
        "notification_type": "alert",
        "current_amount": "90.00",
        "target_amount": "100.00",
        "currency": "USD",
        "item_id": "trk_123",
    }

    message = _render_telegram_message(payload)

    assert "Target reached" in message
    assert "Current: 90.00 USD" in message
    assert "Target: 100.00 USD" in message


def test_render_tracking_started_message_includes_onboarding_text() -> None:
    payload = {
        "notification_type": "tracking_started",
        "product_name": "Starter Item",
        "tracking_mode": "monitor",
        "current_amount": "90.00",
        "currency": "USD",
        "product_url": "https://example.com/p/1",
    }

    message = _render_telegram_message(payload)

    assert "Tracking started" in message
    assert "Item: Starter Item" in message
    assert "Current price: 90.00 USD" in message
    assert "I will notify you when the price drops." in message
    assert "Product: https://example.com/p/1" in message
