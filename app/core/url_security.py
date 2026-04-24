from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


class UnsafeServerFetchUrlError(ValueError):
    """Raised when a user-provided URL is unsafe for server-side fetching."""


@dataclass(frozen=True)
class ServerFetchUrlPolicy:
    allowed_ports: tuple[int, ...] = (80, 443)
    allow_private_networks: bool = False
    resolve_dns: bool = False


def validate_server_fetch_url(url: str, *, policy: ServerFetchUrlPolicy) -> None:
    """Reject URL targets that are unsafe for backend fetchers.

    This protects server-side scraping endpoints from fetching loopback,
    link-local, private, multicast, or otherwise non-global network targets.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeServerFetchUrlError("Only http and https URLs are supported")

    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise UnsafeServerFetchUrlError("URL host is required")

    explicit_port = parsed.port
    if explicit_port is not None and explicit_port not in policy.allowed_ports:
        raise UnsafeServerFetchUrlError("URL port is not allowed")

    _validate_host_addresses(
        host,
        allow_private_networks=policy.allow_private_networks,
    )

    if policy.resolve_dns and not _is_ip_literal(host):
        _validate_resolved_addresses(
            host,
            port=explicit_port or (443 if scheme == "https" else 80),
            allow_private_networks=policy.allow_private_networks,
        )


def _validate_host_addresses(host: str, *, allow_private_networks: bool) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    _validate_address(address, allow_private_networks=allow_private_networks)


def _validate_resolved_addresses(
    host: str,
    *,
    port: int,
    allow_private_networks: bool,
) -> None:
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeServerFetchUrlError("URL host could not be resolved") from exc

    addresses: set[str] = set()
    for row in resolved:
        sockaddr = row[4]
        if not sockaddr:
            continue
        addresses.add(str(sockaddr[0]))

    if not addresses:
        raise UnsafeServerFetchUrlError("URL host could not be resolved")

    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise UnsafeServerFetchUrlError("URL host resolved to an invalid address") from exc
        _validate_address(address, allow_private_networks=allow_private_networks)


def _validate_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private_networks: bool,
) -> None:
    if allow_private_networks:
        return
    if not address.is_global:
        raise UnsafeServerFetchUrlError("URL host resolves to a non-public network")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
