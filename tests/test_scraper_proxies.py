from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.config import get_settings
from app.scraper import proxies


@dataclass
class _FakeResponse:
    text: str

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    get_settings.cache_clear()
    proxies.get_scrape_proxy_rotator.cache_clear()
    yield
    get_settings.cache_clear()
    proxies.get_scrape_proxy_rotator.cache_clear()


def test_parse_proxy_list_keeps_valid_proxy_urls() -> None:
    parsed = proxies._parse_proxy_list(
        """
        # comments are ignored
        http://1.2.3.4:8080
        socks5://5.6.7.8:1080
        bad-value
        http://1.2.3.4:8080
        """
    )

    assert parsed == ("http://1.2.3.4:8080", "socks5://5.6.7.8:1080")


def test_proxy_rotator_loads_remote_proxy_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPE_PROXY_URLS", "")
    monkeypatch.setenv("SCRAPE_PROXY_LIST_URLS", "https://proxy.example/list.txt")
    captured_urls: list[str] = []

    def _fake_get(url: str, **_kwargs: object) -> _FakeResponse:
        captured_urls.append(url)
        return _FakeResponse("http://1.2.3.4:8080\nhttps://5.6.7.8:8443\n")

    monkeypatch.setattr(proxies.httpx, "get", _fake_get)

    rotator = proxies.get_scrape_proxy_rotator()

    assert captured_urls == ["https://proxy.example/list.txt"]
    assert rotator is not None
    assert rotator.proxies == ["http://1.2.3.4:8080", "https://5.6.7.8:8443"]


def test_proxy_rotator_prefers_remote_list_before_manual_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPE_PROXY_URLS", "http://manual.example:8080")
    monkeypatch.setenv("SCRAPE_PROXY_LIST_URLS", "https://proxy.example/list.txt")

    def _fake_get(url: str, **_kwargs: object) -> _FakeResponse:  # noqa: ARG001
        return _FakeResponse("http://list.example:8080\n")

    monkeypatch.setattr(proxies.httpx, "get", _fake_get)

    rotator = proxies.get_scrape_proxy_rotator()

    assert rotator is not None
    assert rotator.proxies == ["http://list.example:8080", "http://manual.example:8080"]


def test_logging_rotation_selects_next_proxy(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="scraper_api.scraper")

    first_proxy, next_index = proxies._logging_cyclic_rotation(
        ["http://user:secret@proxy.example:8080", "http://proxy2.example:8080"],
        0,
    )

    assert first_proxy == "http://user:secret@proxy.example:8080"
    assert next_index == 1
    assert "scrape_proxy_selected index=0 proxy=http://***:***@proxy.example:8080" in caplog.text
