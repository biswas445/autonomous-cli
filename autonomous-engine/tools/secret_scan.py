"""Pre-commit secret scanner (directive #9): gitleaks-style, dogfooded.

Reuses the runtime's own detection heuristics (`core/security`) so what the
agents are forbidden from writing is exactly what this hook forbids from being
committed. Scans the files pre-commit passes on argv and exits non-zero when a
potential credential is found. Values are never printed — only file/line.
"""

from __future__ import annotations

import sys
from pathlib import Path

from autonomous_engine.core.security import find_high_entropy_strings, find_secrets

# Committing example/placeholder credentials is fine; everything else that
# matches a known provider pattern or high entropy is not.
_SKIP_NAMES = (".env.example", "apikeys.txt.example")
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".sqlite"}


def scan_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if find_secrets(line):
            findings.append(f"{path}:{line_number}: known credential pattern")
        elif find_high_entropy_strings(line):
            findings.append(f"{path}:{line_number}: high-entropy literal")
    return findings


def main(argv: list[str]) -> int:
    failures: list[str] = []
    for arg in argv:
        path = Path(arg)
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        if any(path.name == skip for skip in _SKIP_NAMES):
            continue
        failures.extend(scan_file(path))
    for failure in failures:
        print(f"secret-scan: {failure}")
    if failures:
        print(
            "secret-scan: potential credential detected — move it to .env "
            "(gitignored) and rotate the key if it was ever committed."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
