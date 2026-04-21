from __future__ import annotations

import json
import logging
import os
import socket
import time

import httpx

from app.application.notification_worker_service import (
    NotificationSendResult,
    NotificationWorkerService,
)
from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.tracking import NotificationEvent


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class LoggingNotificationSender:
    """MVP sender that logs payloads and acknowledges immediately."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("scraper_api.notifier.sender")

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        payload: object
        try:
            payload = json.loads(event.payload)
        except json.JSONDecodeError:
            payload = {"raw_payload": event.payload}

        self._logger.info(
            "notification_send channel=%s notification_id=%s alert_event_id=%s payload=%s",
            event.channel,
            event.notification_id,
            event.alert_event_id,
            payload,
        )
        return NotificationSendResult(
            success=True,
            provider_name="log",
            provider_message_id=event.notification_id,
            error=None,
            retryable=False,
        )


class TelegramNotificationSender:
    """Telegram Bot API sender for `channel=telegram` notification events."""

    def __init__(self, *, bot_token: str) -> None:
        self._bot_token = bot_token.strip()

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        if not self._bot_token:
            return NotificationSendResult(
                success=False,
                provider_name="telegram",
                provider_message_id=None,
                error="telegram_not_configured",
                retryable=False,
            )

        payload = _parse_payload(event.payload)
        chat_id = str(payload.get("chat_id", "")).strip()
        if not chat_id:
            return NotificationSendResult(
                success=False,
                provider_name="telegram",
                provider_message_id=None,
                error="telegram_chat_id_missing",
                retryable=False,
            )

        message_text = _render_telegram_message(payload)
        api_url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            response = httpx.post(
                api_url,
                json={
                    "chat_id": chat_id,
                    "text": message_text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as exc:
            return NotificationSendResult(
                success=False,
                provider_name="telegram",
                provider_message_id=None,
                error=f"telegram_network_error: {exc}",
                retryable=True,
            )

        response_payload = _parse_payload(response.text)
        if response.is_success and bool(response_payload.get("ok", False)):
            result = response_payload.get("result")
            message_id = None
            if isinstance(result, dict):
                raw_message_id = result.get("message_id")
                message_id = str(raw_message_id) if raw_message_id is not None else None
            return NotificationSendResult(
                success=True,
                provider_name="telegram",
                provider_message_id=message_id,
                error=None,
                retryable=False,
            )

        description = str(response_payload.get("description", "telegram_send_failed")).strip()
        retryable = _is_retryable_telegram_status(response.status_code, description)
        return NotificationSendResult(
            success=False,
            provider_name="telegram",
            provider_message_id=None,
            error=f"telegram_error_{response.status_code}: {description}",
            retryable=retryable,
        )


class MultiplexNotificationSender:
    """Routes notification events by channel."""

    def __init__(
        self,
        *,
        log_sender: LoggingNotificationSender,
        telegram_sender: TelegramNotificationSender,
    ) -> None:
        self._log_sender = log_sender
        self._telegram_sender = telegram_sender

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        if event.channel == "telegram":
            return self._telegram_sender.send(event)
        return self._log_sender.send(event)


def _parse_payload(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _render_telegram_message(payload: dict) -> str:
    notification_type = str(payload.get("notification_type", "alert")).strip().lower()
    current = str(payload.get("current_amount", "?")).strip() or "?"
    previous = str(payload.get("previous_amount", "?")).strip() or "?"
    target = str(payload.get("target_amount", "?")).strip() or "?"
    currency = str(payload.get("currency", "")).strip()
    item_id = str(payload.get("item_id", "")).strip()
    product_name = str(payload.get("product_name", "")).strip()
    tracking_mode = str(payload.get("tracking_mode", "")).strip().lower()
    triggered_at = str(payload.get("triggered_at", "")).strip()
    affiliate_url = str(payload.get("affiliate_url", "")).strip()
    listing_url = str(payload.get("listing_url", "")).strip()
    product_url = str(payload.get("product_url", "")).strip()

    currency_suffix = f" {currency}" if currency else ""
    if notification_type == "price_drop":
        lines = [
            "Price dropped",
            f"Previous: {previous}{currency_suffix}",
            f"Now: {current}{currency_suffix}",
        ]
    elif notification_type == "tracking_started":
        lines = ["Tracking started"]
        if current != "?":
            lines.append(f"Current price: {current}{currency_suffix}")
        if target != "?":
            lines.append(f"Target price: {target}{currency_suffix}")
        if tracking_mode == "monitor":
            lines.append("I will notify you when the price drops.")
        else:
            lines.append("I will notify you when your target price is reached.")
    else:
        lines = [
            "Target reached",
            f"Current: {current}{currency_suffix}",
            f"Target: {target}{currency_suffix}",
        ]
    if product_name:
        lines.insert(1, f"Item: {product_name}")
    if affiliate_url:
        lines.append(f"Buy: {affiliate_url}")
    if listing_url:
        lines.append(f"Listing: {listing_url}")
    if product_url:
        lines.append(f"Product: {product_url}")
    if item_id and not product_name:
        lines.append(f"Item: {item_id}")
    if triggered_at:
        lines.append(f"At: {triggered_at}")
    return "\n".join(lines)


def _is_retryable_telegram_status(status_code: int, description: str) -> bool:
    normalized = description.lower()
    if status_code == 429:
        return True
    if 500 <= status_code <= 599:
        return True
    if "too many requests" in normalized:
        return True
    if "bot was blocked by the user" in normalized:
        return False
    if "chat not found" in normalized:
        return False
    if "forbidden" in normalized:
        return False
    return False


def _resolve_lease_owner() -> str:
    configured = os.getenv("NOTIFIER_LEASE_OWNER", "").strip()
    if configured:
        return configured
    host = socket.gethostname()
    return f"{host}-{os.getpid()}"


def _build_worker_service() -> tuple[NotificationWorkerService, int]:
    settings = get_settings()
    repository = PostgresTrackingRepository(
        database_url=settings.tracking_database_url,
        min_pool_size=settings.tracking_db_pool_min_size,
        max_pool_size=settings.tracking_db_pool_max_size,
    )
    repository.initialize()
    worker_service = NotificationWorkerService(
        repository=repository,
        sender=MultiplexNotificationSender(
            log_sender=LoggingNotificationSender(),
            telegram_sender=TelegramNotificationSender(
                bot_token=settings.telegram_bot_token,
            ),
        ),
        claim_batch_size=settings.notifier_claim_batch_size,
        lease_seconds=settings.notifier_lease_seconds,
        lease_owner=_resolve_lease_owner(),
        max_attempts=settings.notifier_max_attempts,
        base_backoff_seconds=settings.notifier_base_backoff_seconds,
        backoff_max_seconds=settings.notifier_backoff_max_seconds,
        retry_jitter_percent=settings.notifier_retry_jitter_percent,
    )
    return worker_service, settings.notifier_poll_seconds


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.notifier")
    worker_service, poll_seconds = _build_worker_service()

    logger.info("notifier_started poll_seconds=%s", poll_seconds)
    while True:
        started = time.perf_counter()
        try:
            result = worker_service.run_once()
            logger.info(
                (
                    "notifier_cycle claimed=%s sent=%s retried=%s dead=%s failures=%s max_attempt_depth=%s duration_ms=%.2f"
                ),
                result.claimed_count,
                result.sent_count,
                result.retry_count,
                result.dead_count,
                result.failure_count,
                result.max_attempt_depth,
                result.duration_seconds * 1000,
            )
        except Exception:
            logger.exception("notifier_cycle_failed")

        elapsed = time.perf_counter() - started
        sleep_seconds = max(0.0, float(poll_seconds) - elapsed)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
