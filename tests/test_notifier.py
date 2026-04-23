from __future__ import annotations

import json
from datetime import UTC, datetime

from app.jobs.notifier import (
    TelegramNotificationSender,
    _extract_telegram_retry_after_seconds,
    _render_telegram_message,
)
from app.tracking import NotificationEvent


class _FakeHttpxResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self.text = json.dumps(body)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


def _telegram_event(*, notification_id: str = "nev_test") -> NotificationEvent:
    now = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "chat_id": "12345",
        "notification_type": "alert",
        "current_amount": "90.00",
        "target_amount": "100.00",
        "currency": "USD",
        "item_id": "trk_1",
    }
    return NotificationEvent(
        notification_id=notification_id,
        idempotency_key=f"{notification_id}:telegram",
        alert_event_id=None,
        item_id="trk_1",
        channel="telegram",
        payload=json.dumps(payload),
        status="queued",  # type: ignore[arg-type]
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now,
        last_attempt_at=None,
        sent_at=None,
        provider_name=None,
        provider_message_id=None,
        last_error=None,
        lease_owner=None,
        lease_until=None,
        created_at=now,
        updated_at=now,
    )


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


def test_extract_telegram_retry_after_seconds_prefers_parameters() -> None:
    payload = {
        "ok": False,
        "description": "Too Many Requests",
        "parameters": {"retry_after": 7},
    }
    assert _extract_telegram_retry_after_seconds(payload, "Too Many Requests") == 7


def test_extract_telegram_retry_after_seconds_falls_back_to_description() -> None:
    payload = {"ok": False, "description": "Too Many Requests: retry after 3"}
    assert _extract_telegram_retry_after_seconds(payload, payload["description"]) == 3


def test_telegram_sender_honors_retry_after_with_local_backoff(monkeypatch) -> None:
    clock = {"now": 0.0}
    sleeps: list[float] = []
    post_calls = {"count": 0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    def fake_post(*_args, **_kwargs):
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return _FakeHttpxResponse(
                429,
                {
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 2},
                },
            )
        return _FakeHttpxResponse(200, {"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr("app.jobs.notifier.time.monotonic", fake_monotonic)
    monkeypatch.setattr("app.jobs.notifier.time.sleep", fake_sleep)
    monkeypatch.setattr("app.jobs.notifier.httpx.post", fake_post)

    sender = TelegramNotificationSender(bot_token="token", min_interval_ms=0)
    first = sender.send(_telegram_event(notification_id="nev_first"))
    second = sender.send(_telegram_event(notification_id="nev_second"))

    assert first.success is False
    assert first.retryable is True
    assert second.success is True
    assert post_calls["count"] == 2
    assert sleeps
    assert abs(sleeps[0] - 2.0) < 1e-9


def test_telegram_sender_applies_min_interval_pacing(monkeypatch) -> None:
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    def fake_post(*_args, **_kwargs):
        return _FakeHttpxResponse(200, {"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr("app.jobs.notifier.time.monotonic", fake_monotonic)
    monkeypatch.setattr("app.jobs.notifier.time.sleep", fake_sleep)
    monkeypatch.setattr("app.jobs.notifier.httpx.post", fake_post)

    sender = TelegramNotificationSender(bot_token="token", min_interval_ms=200)
    first = sender.send(_telegram_event(notification_id="nev_1"))
    second = sender.send(_telegram_event(notification_id="nev_2"))

    assert first.success is True
    assert second.success is True
    assert sleeps
    assert abs(sleeps[0] - 0.2) < 1e-9
