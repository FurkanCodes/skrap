from __future__ import annotations

from app.jobs.notifier import _render_telegram_message


def test_render_telegram_message_includes_affiliate_and_product_links() -> None:
    payload = {
        "current_amount": "90.00",
        "target_amount": "100.00",
        "currency": "USD",
        "affiliate_url": "https://go.example.com/out?u=https%3A%2F%2Fexample.com%2Fp%2F1",
        "listing_url": "https://merchant.example.com/buy/1",
        "product_url": "https://example.com/p/1",
        "item_id": "trk_123",
        "triggered_at": "2026-04-18T12:00:00Z",
    }

    message = _render_telegram_message(payload)

    assert "Affiliate: https://go.example.com/out?u=https%3A%2F%2Fexample.com%2Fp%2F1" in message
    assert "Listing: https://merchant.example.com/buy/1" in message
    assert "Product: https://example.com/p/1" in message
