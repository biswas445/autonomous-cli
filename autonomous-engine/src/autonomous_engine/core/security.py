"""Security policy: endpoint validation, secret hygiene, path containment.

The runtime executes model-generated work on the host, so every outbound URL
it dials is validated before a request is made: only http/https, and no
loopback / private / link-local / reserved hosts. Endpoints that a user
explicitly opts into (e.g. a locally hosted model) require an explicit
override flag rather than silently bypassing the check.
"""

from __future__ import annotations

import ipaddress
import math
import re
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

# Hostnames that always resolve to the local machine.
_LOOPBACK_NAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}
# Names that commonly denote in-cluster or metadata services.
_BLOCKED_NAMES = {
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style secret key
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),  # Anthropic secret key
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub token
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


class EndpointNotAllowed(ValueError):
    """Raised when a configured model endpoint fails the network policy."""


def _is_forbidden_ip(host: str) -> str | None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_loopback:
        return "loopback address"
    if ip.is_private:
        return "private address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved address"
    return None


def validate_endpoint_url(url: str, *, allow_private: bool = False) -> str:
    """Validate a model/API endpoint before any request is issued.

    Returns the URL unchanged when it passes policy. Raises EndpointNotAllowed
    otherwise. `allow_private=True` is an explicit, caller-visible opt-in used
    only when the operator has configured a local model endpoint.
    """
    if not url or not isinstance(url, str):
        raise EndpointNotAllowed("endpoint URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise EndpointNotAllowed(
            f"endpoint scheme {parts.scheme or '<none>'!r} is not allowed; use http or https"
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise EndpointNotAllowed("endpoint URL has no host")
    if allow_private:
        return url
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        raise EndpointNotAllowed(
            f"refusing to contact loopback host {host!r}; "
            "set allow_private_endpoints=true to permit a local model endpoint"
        )
    if host in _BLOCKED_NAMES or host.endswith(".internal"):
        raise EndpointNotAllowed(f"refusing to contact internal host {host!r}")
    reason = _is_forbidden_ip(host)
    if reason:
        raise EndpointNotAllowed(f"refusing to contact {host!r}: {reason}")
    return url


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character (0.0 for empty/one-symbol strings)."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


# High-entropy string literals are what API keys, tokens, and passwords look
# like (the gitleaks/trufflehog "generic API key" heuristic). Tuned to avoid
# false positives on prose, hashes, and placeholder text.
_ENTROPY_MIN_LENGTH = 20
_ENTROPY_THRESHOLD = 4.5
_ENTROPY_ALLOWLIST = re.compile(
    r"example|placeholder|changeme|dummy|redacted|xxxx|<[^>]+>|\{\{|\$\{|https?://|"
    r"^[0-9a-f]{32,}$|^[0-9a-f-]{36}$",  # hex digests and UUIDs are not secrets
    re.IGNORECASE,
)
_QUOTED_LITERAL = re.compile(r"""["']([^"'
]{20,120})["']""")


def find_high_entropy_strings(text: str) -> list[str]:
    """Describe (never reveal) quoted literals that look like secrets.

    Returns redacted descriptors: value length and entropy, never the value.
    """
    found: list[str] = []
    for match in _QUOTED_LITERAL.finditer(text):
        value = match.group(1)
        if len(value) < _ENTROPY_MIN_LENGTH or _ENTROPY_ALLOWLIST.search(value):
            continue
        classes = sum(
            bool(pattern.search(value))
            for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"))
        )
        if classes < 2:
            continue
        entropy = shannon_entropy(value)
        if entropy >= _ENTROPY_THRESHOLD:
            found.append(f"high-entropy literal ({len(value)} chars, {entropy:.1f} bits/char)")
    return found


def find_secrets(text: str) -> list[str]:
    """Return redacted descriptions of any secret-looking substrings.

    Used by the security agent to scan repository content; the matched values
    are never returned, only their pattern names.
    """
    found: list[str] = []
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            found.append(pattern.pattern)
    return found


def is_safe_relative_path(path: str) -> bool:
    """True when a path stays inside its root (no absolute paths / traversal)."""
    if not path or path.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:", path):  # windows drive
        return False
    parts = path.replace("\\", "/").split("/")
    return ".." not in parts
