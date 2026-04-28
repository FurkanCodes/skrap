from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote_plus, urlparse

from app.tracking import AffiliateClickEvent, TrackedItem, TrackingRepository


class AffiliateLinkError(ValueError):
    """Raised when an outbound redirect token cannot be trusted."""


@dataclass(frozen=True)
class AffiliateRule:
    rule_id: str
    domains: tuple[str, ...]
    site_keys: tuple[str, ...]
    template: str


@dataclass(frozen=True)
class AffiliateLinkResult:
    outbound_url: str | None
    outbound_is_affiliate: bool
    target_url: str | None
    merchant_domain: str | None
    rule_id: str | None


@dataclass(frozen=True)
class AffiliateResolveResult:
    target_url: str
    is_affiliate: bool


class AffiliateLinkService:
    """Build and resolve signed outbound product links."""

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        redirect_base_url: str = "",
        signing_secret: str = "",
        rules_json: str = "",
        legacy_template: str = "",
        token_ttl_seconds: int = 60 * 60 * 24 * 30,
    ) -> None:
        self._repository = repository
        self._redirect_base_url = redirect_base_url.strip().rstrip("/")
        self._signing_secret = signing_secret.strip()
        self._rules = _parse_rules(rules_json)
        self._legacy_template = legacy_template.strip()
        self._token_ttl_seconds = token_ttl_seconds

    def build_outbound_link(
        self,
        *,
        item: TrackedItem,
        product_url: str | None = None,
        surface: str = "dashboard",
    ) -> AffiliateLinkResult:
        source_url = product_url or item.canonical_url or item.source_url
        if not _is_http_url(source_url):
            return AffiliateLinkResult(
                outbound_url=None,
                outbound_is_affiliate=False,
                target_url=None,
                merchant_domain=None,
                rule_id=None,
            )

        merchant_domain = _hostname(source_url)
        rule = self._match_rule(merchant_domain, item.site_key)
        is_affiliate = rule is not None
        target_url = _apply_template(rule.template, source_url) if rule else source_url

        if not self._redirect_base_url or not self._signing_secret:
            return AffiliateLinkResult(
                outbound_url=None,
                outbound_is_affiliate=False,
                target_url=target_url,
                merchant_domain=merchant_domain,
                rule_id=rule.rule_id if rule else None,
            )

        token = self._sign(
            {
                "url": source_url,
                "target_url": target_url,
                "item_id": item.item_id,
                "user_id": item.user_id,
                "surface": surface,
                "merchant_domain": merchant_domain,
                "rule_id": rule.rule_id if rule else None,
                "is_affiliate": is_affiliate,
                "exp": int(time.time()) + self._token_ttl_seconds,
            }
        )
        return AffiliateLinkResult(
            outbound_url=f"{self._redirect_base_url}/r/{token}",
            outbound_is_affiliate=is_affiliate,
            target_url=target_url,
            merchant_domain=merchant_domain,
            rule_id=rule.rule_id if rule else None,
        )

    def resolve(self, token: str) -> AffiliateResolveResult:
        if not self._signing_secret:
            raise AffiliateLinkError("Redirect signing is not configured")
        payload = self._verify(token)
        target_url = str(payload.get("target_url") or "")
        if not _is_http_url(target_url):
            raise AffiliateLinkError("Invalid redirect target")

        event = AffiliateClickEvent(
            event_id=str(uuid.uuid4()),
            item_id=_nullable_str(payload.get("item_id")),
            user_id=_nullable_str(payload.get("user_id")),
            surface=str(payload.get("surface") or "unknown"),
            merchant_domain=_nullable_str(payload.get("merchant_domain")),
            rule_id=_nullable_str(payload.get("rule_id")),
            is_affiliate=bool(payload.get("is_affiliate")),
            created_at=datetime.now(UTC).isoformat(),
        )
        self._repository.save_affiliate_click_event(event)
        return AffiliateResolveResult(
            target_url=target_url,
            is_affiliate=event.is_affiliate,
        )

    def _match_rule(
        self,
        merchant_domain: str | None,
        site_key: str | None,
    ) -> AffiliateRule | None:
        normalized_site_key = (site_key or "").strip().lower()
        if merchant_domain:
            for rule in self._rules:
                if normalized_site_key and normalized_site_key in rule.site_keys:
                    return rule
                if any(_domain_matches(merchant_domain, domain) for domain in rule.domains):
                    return rule
        elif normalized_site_key:
            for rule in self._rules:
                if normalized_site_key in rule.site_keys:
                    return rule
        if not self._rules and self._legacy_template:
            return AffiliateRule(
                rule_id="legacy-template",
                domains=("*",),
                site_keys=(),
                template=self._legacy_template,
            )
        return None

    def _sign(self, payload: dict[str, object]) -> str:
        payload_bytes = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded_payload = _b64encode(payload_bytes)
        signature = hmac.new(
            self._signing_secret.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_b64encode(signature)}"

    def _verify(self, token: str) -> dict[str, object]:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
        except ValueError as exc:
            raise AffiliateLinkError("Malformed redirect token") from exc

        expected_signature = hmac.new(
            self._signing_secret.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64encode(expected_signature), encoded_signature):
            raise AffiliateLinkError("Invalid redirect signature")

        try:
            payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AffiliateLinkError("Invalid redirect payload") from exc
        if not isinstance(payload, dict):
            raise AffiliateLinkError("Invalid redirect payload")

        expires_at = payload.get("exp")
        if not isinstance(expires_at, (int, float)) or expires_at < time.time():
            raise AffiliateLinkError("Expired redirect token")
        return payload


def _parse_rules(raw_rules: str) -> tuple[AffiliateRule, ...]:
    if not raw_rules.strip():
        return ()
    try:
        decoded = json.loads(raw_rules)
    except ValueError as exc:
        raise RuntimeError("AFFILIATE_LINK_RULES_JSON must be valid JSON") from exc
    if not isinstance(decoded, list):
        raise RuntimeError("AFFILIATE_LINK_RULES_JSON must be a JSON array")

    rules: list[AffiliateRule] = []
    for index, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            raise RuntimeError("AFFILIATE_LINK_RULES_JSON entries must be objects")
        rule_id = str(entry.get("id") or entry.get("rule_id") or f"rule-{index + 1}").strip()
        template = str(entry.get("template") or "").strip()
        domains_value = entry.get("domains")
        site_keys_value = entry.get("site_keys")
        if isinstance(domains_value, str):
            domains = (domains_value.strip().lower(),)
        elif isinstance(domains_value, list):
            domains = tuple(
                str(domain).strip().lower()
                for domain in domains_value
                if str(domain).strip()
            )
        else:
            domains = ()
        if isinstance(site_keys_value, str):
            site_keys = (site_keys_value.strip().lower(),)
        elif isinstance(site_keys_value, list):
            site_keys = tuple(
                str(key).strip().lower()
                for key in site_keys_value
                if str(key).strip()
            )
        else:
            site_keys = ()
        if not rule_id or not template or not (domains or site_keys):
            raise RuntimeError(
                "AFFILIATE_LINK_RULES_JSON entries require id, template, and domains or site_keys"
            )
        rules.append(
            AffiliateRule(
                rule_id=rule_id,
                domains=domains,
                site_keys=site_keys,
                template=template,
            )
        )
    return tuple(rules)


def _apply_template(template: str, source_url: str) -> str:
    encoded = quote_plus(source_url)
    if "{url_raw}" in template:
        return template.replace("{url_raw}", source_url)
    if "{url}" in template:
        return template.replace("{url}", encoded)
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}url={encoded}"


def _domain_matches(hostname: str, configured_domain: str) -> bool:
    if configured_domain == "*":
        return True
    domain = configured_domain.removeprefix("*.").lower()
    host = hostname.lower()
    return host == domain or host.endswith(f".{domain}")


def _hostname(url: str) -> str | None:
    parsed = urlparse(url)
    return parsed.hostname.lower() if parsed.hostname else None


def _is_http_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _nullable_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")
