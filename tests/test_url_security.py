from __future__ import annotations

import socket

import pytest

from app.core.url_security import (
    ServerFetchUrlPolicy,
    UnsafeServerFetchUrlError,
    validate_server_fetch_url,
)


def test_validate_server_fetch_url_allows_public_http_url() -> None:
    validate_server_fetch_url(
        "https://example.com/product",
        policy=ServerFetchUrlPolicy(resolve_dns=False),
    )


def test_validate_server_fetch_url_rejects_loopback_literal() -> None:
    with pytest.raises(UnsafeServerFetchUrlError, match="non-public network"):
        validate_server_fetch_url(
            "http://127.0.0.1:80/internal",
            policy=ServerFetchUrlPolicy(resolve_dns=False),
        )


def test_validate_server_fetch_url_rejects_private_literal() -> None:
    with pytest.raises(UnsafeServerFetchUrlError, match="non-public network"):
        validate_server_fetch_url(
            "http://10.0.0.5/private",
            policy=ServerFetchUrlPolicy(resolve_dns=False),
        )


def test_validate_server_fetch_url_rejects_unexpected_port() -> None:
    with pytest.raises(UnsafeServerFetchUrlError, match="port"):
        validate_server_fetch_url(
            "https://example.com:8443/product",
            policy=ServerFetchUrlPolicy(resolve_dns=False),
        )


def test_validate_server_fetch_url_rejects_dns_resolution_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("192.168.1.10", 443),
            )
        ],
    )

    with pytest.raises(UnsafeServerFetchUrlError, match="non-public network"):
        validate_server_fetch_url(
            "https://internal.example.com/product",
            policy=ServerFetchUrlPolicy(resolve_dns=True),
        )
