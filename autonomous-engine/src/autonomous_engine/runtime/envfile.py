"""`.env` loading for real-model runs (never logs or exports secret values).

The operator's `.env` (searched from the project root upward) groups providers
into blank-line-separated sections:

    baseurl = https://kiosapi.com/v1
    apikey  = sk-...
    models1 = muse-spark-1.3-contributor

    baseurl = https://api.atria-asi.ai/v1
    apikey  = sk-...
    models  = Atria-Dawn-Preview

Keys are case-insensitive; section identity comes from the base URL host
(kiosapi → kios, atria → atria). The parsed values are exposed ONLY through
structured objects; `str()`/logging of a ProviderSection must never print the
key, so the secret is held in a private field with a redacted repr.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass
class ProviderSection:
    """One provider block from `.env`. The API key is redacted in repr."""

    name: str
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    models: list[str] = field(default_factory=list)
    rpm: int = 0  # 0 = no explicit limit recorded

    def env_pairs(self) -> dict[str, str]:
        """Env-var names this section exports (upper-case, safe to log)."""
        prefix = f"AUTO_{self.name.upper()}_"
        pairs = {f"{prefix}BASE_URL": self.base_url}
        for index, model in enumerate(self.models):
            suffix = "" if len(self.models) == 1 else str(index + 1)
            pairs[f"{prefix}MODEL{suffix}"] = model
        return pairs

    def apply_env(self) -> None:
        """Export into os.environ without overriding existing values."""
        for key, value in self.env_pairs().items():
            if value and not os.environ.get(key):
                os.environ[key] = value
        # The key is exported under the provider-adapter's expected name so
        # the OpenAI-compat adapter finds it without code changes.
        if self.api_key:
            var = f"AUTO_{self.name.upper()}_API_KEY"
            if not os.environ.get(var):
                os.environ[var] = self.api_key


def _classify(base_url: str) -> str:
    host = urlparse(base_url if "://" in base_url else f"https://{base_url}").hostname or ""
    host = host.lower()
    if "kios" in host:
        return "kios"
    if "atria" in host or "aria" in host:
        return "atria"
    slug = re.sub(r"[^a-z0-9]+", "-", host.split(".")[-2] if host.count(".") >= 1 else host)
    return slug or "provider"


def _model_rpm(name: str) -> int:
    """Documented rate limits (requests/minute); env override wins."""
    override = os.environ.get(f"AUTO_{name.upper()}_RPM")
    if override and override.isdigit():
        return int(override)
    return {"kios": 5, "atria": 30}.get(name, 0)


def parse_env_sections(text: str) -> list[ProviderSection]:
    """Parse blank-line-separated blocks into provider sections."""
    sections: list[ProviderSection] = []
    current: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if current:
                sections.append(_section_from(current))
                current = {}
            continue
        match = _KV.match(line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip().strip("'\"")
        current[key] = value
    if current:
        sections.append(_section_from(current))
    # Merge sections for the same provider (later blocks extend earlier ones).
    merged: dict[str, ProviderSection] = {}
    for section in sections:
        existing = merged.get(section.name)
        if existing is None:
            merged[section.name] = section
            continue
        if section.base_url:
            existing.base_url = section.base_url
        if section.api_key:
            existing.api_key = section.api_key
        for model in section.models:
            if model and model not in existing.models:
                existing.models.append(model)
    return list(merged.values())


def _section_from(block: dict[str, str]) -> ProviderSection:
    base_url = block.get("baseurl") or block.get("base_url") or ""
    models = [
        v.strip()
        for k, v in block.items()
        if k.startswith("model") and v.strip()
    ]
    name = _classify(base_url) if base_url else "provider"
    return ProviderSection(
        name=name,
        base_url=base_url,
        api_key=block.get("apikey") or block.get("api_key") or "",
        models=models,
        rpm=_model_rpm(name),
    )


def find_env_file(start: Path) -> Path | None:
    """Walk up from `start` looking for a `.env` file."""
    current = Path(start).resolve()
    for candidate in [current, *current.parents]:
        dot_env = candidate / ".env"
        if dot_env.is_file():
            return dot_env
    return None


def load_repo_env(start: Path) -> list[ProviderSection]:
    """Load `.env` provider sections (idempotent; missing file is fine)."""
    path = find_env_file(start)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    sections = parse_env_sections(text)
    for section in sections:
        section.apply_env()
    return sections
