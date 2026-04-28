from __future__ import annotations

import pytest

from app.application.affiliate_link_service import AffiliateLinkError, AffiliateLinkService
from app.tracking import AffiliateClickEvent, TrackedItem


class _AffiliateClickRepository:
    def __init__(self) -> None:
        self.events: list[AffiliateClickEvent] = []

    def save_affiliate_click_event(self, event: AffiliateClickEvent) -> None:
        self.events.append(event)


def _item(
    url: str = "https://merchant.example.com/p/1",
    *,
    site_key: str | None = None,
) -> TrackedItem:
    return TrackedItem(
        item_id="trk_1",
        user_id="usr_1",
        source_url=url,
        canonical_url=None,
        product_id=None,
        site_key=site_key,
        adapter_key=None,
        product_name="Test Product",
        brand=None,
        model=None,
        sku=None,
        gtin=None,
        image_url=None,
        availability=None,
        currency="USD",
        last_amount="100",
        last_raw_text=None,
        last_source=None,
        status="active",
        check_interval_minutes=60,
        target_amount=None,
        created_at="2026-04-29T00:00:00Z",
        updated_at="2026-04-29T00:00:00Z",
        last_checked_at=None,
    )


def test_inactive_config_returns_no_outbound_url() -> None:
    service = AffiliateLinkService(repository=_AffiliateClickRepository())

    result = service.build_outbound_link(item=_item(), surface="dashboard")

    assert result.outbound_url is None
    assert result.outbound_is_affiliate is False
    assert result.target_url == "https://merchant.example.com/p/1"


def test_signed_direct_redirect_logs_non_affiliate_click() -> None:
    repo = _AffiliateClickRepository()
    service = AffiliateLinkService(
        repository=repo,
        redirect_base_url="https://app.example.com",
        signing_secret="secret",
    )
    link = service.build_outbound_link(item=_item(), surface="dashboard")

    assert link.outbound_url is not None
    assert link.outbound_url.startswith("https://app.example.com/r/")
    result = service.resolve(link.outbound_url.rsplit("/", 1)[1])

    assert result.target_url == "https://merchant.example.com/p/1"
    assert result.is_affiliate is False
    assert len(repo.events) == 1
    assert repo.events[0].item_id == "trk_1"
    assert repo.events[0].surface == "dashboard"
    assert repo.events[0].is_affiliate is False


def test_signed_affiliate_rule_redirect_logs_affiliate_click() -> None:
    repo = _AffiliateClickRepository()
    service = AffiliateLinkService(
        repository=repo,
        redirect_base_url="https://app.example.com/",
        signing_secret="secret",
        rules_json=(
            '[{"id":"merchant-main","domains":["merchant.example.com"],'
            '"template":"https://go.example.com/out?u={url}"}]'
        ),
    )
    link = service.build_outbound_link(item=_item(), surface="telegram")

    assert link.outbound_url is not None
    assert link.outbound_is_affiliate is True
    assert (
        link.target_url
        == "https://go.example.com/out?u=https%3A%2F%2Fmerchant.example.com%2Fp%2F1"
    )
    result = service.resolve(link.outbound_url.rsplit("/", 1)[1])

    assert result.target_url == link.target_url
    assert result.is_affiliate is True
    assert repo.events[0].merchant_domain == "merchant.example.com"
    assert repo.events[0].rule_id == "merchant-main"


def test_legacy_template_is_used_only_without_rules_json() -> None:
    service = AffiliateLinkService(
        repository=_AffiliateClickRepository(),
        redirect_base_url="https://app.example.com",
        signing_secret="secret",
        legacy_template="https://legacy.example.com/out?url={url}",
    )

    result = service.build_outbound_link(item=_item())

    assert result.outbound_is_affiliate is True
    assert (
        result.target_url
        == "https://legacy.example.com/out?url=https%3A%2F%2Fmerchant.example.com%2Fp%2F1"
    )
    assert result.rule_id == "legacy-template"


def test_rule_can_match_site_key() -> None:
    service = AffiliateLinkService(
        repository=_AffiliateClickRepository(),
        redirect_base_url="https://app.example.com",
        signing_secret="secret",
        rules_json=(
            '[{"id":"generic-site","site_keys":["generic"],'
            '"template":"https://go.example.com/site?u={url}"}]'
        ),
    )

    result = service.build_outbound_link(item=_item(site_key="generic"))

    assert result.outbound_is_affiliate is True
    assert result.rule_id == "generic-site"


def test_tampered_token_is_rejected() -> None:
    service = AffiliateLinkService(
        repository=_AffiliateClickRepository(),
        redirect_base_url="https://app.example.com",
        signing_secret="secret",
    )
    link = service.build_outbound_link(item=_item())
    token = link.outbound_url.rsplit("/", 1)[1]  # type: ignore[union-attr]

    with pytest.raises(AffiliateLinkError):
        service.resolve(f"{token}x")
