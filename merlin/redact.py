"""Secret redaction applied to every document before it is chunked, embedded or stored, and to every document served.

Two layers, both configured in the layout's `[redact]` table:
- **Secret hashes** (`secret_hashes = "<path>"`, or env SECRET_HASHES): a file of salted sha256 hashes of the live
  secrets, written where the secrets live (only hashes leave that host). Any token whose salted hash matches becomes
  `[REDACTED:<name>]`. Once configured it is required: a missing or empty file refuses to run (fail closed).
  Format: line 1 `salt <hex>`, then `<sha256(salt + value) hex>\t<name>` per secret.
- **Patterns** for well-known secret shapes, in named families. `disable = [...]` turns families off for corpora
  where a shape is mostly evidence, not secrets (e.g. `hex32` in a record full of checksums and commit IDs).
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from .layout import Layout, load_layout

R = "[REDACTED]"
FAMILIES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "private_key": [(re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                                re.S), R)],
    "url_credentials": [
        (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@", re.I), rf"\1{R}@"),      # scheme://user:pass@
        (re.compile(r"\b([a-z][a-z0-9+.-]*://)[A-Za-z0-9_-]{20,}@", re.I), rf"\1{R}@"),      # scheme://token@
    ],
    # sk- keys are mixed-case or one long unhyphenated run; hyphenated lowercase names (sk-alpha-boxes-r1234) are not
    "sk_key": [(re.compile(r"\bsk-(?=[\w-]*[A-Z]|[\w-]*?[A-Za-z0-9]{20})[A-Za-z0-9_-]{16,}"), R)],
    "vendor_token": [
        (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), R),
        (re.compile(r"\b(?:hf_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})"),
         R),
        (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), R),                                   # Telegram bot token
    ],
    "jwt": [(re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), R)],  # HA tokens
    "bearer": [(re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{20,}=*"), rf"\1{R}")],
    # key=value, not $VAR/<x>; NOPASSWD is a sudoers tag, not a password key
    "key_value": [(re.compile(r"(?i)\b(?!nopasswd\b)([\w-]*(?:password|passwd|secret|token|api[_-]?key|_pw)\s*[:=]\s*"
                              r"['\"]?)(?![$<\[{])([^\s'\"]{6,})"), rf"\1{R}")],
    "hex32": [(re.compile(r"\b[0-9a-fA-F]{32,}\b"), R)],
}
# whole tokens for the hash check; a trailing '=' run is kept so base64 padding can be part of a secret
TOKEN_RE = re.compile(r"[^\s`'\"=:,()<>\[\]{}|;]+=*")


class SecretHashError(RuntimeError):
    """The configured secret-hash file is missing, unreadable or empty (redaction fails closed)."""


@dataclass(frozen=True)
class SecretHashes:
    salt: str
    names: dict[str, str]          # sha256(salt + value) hex -> secret name


@lru_cache(maxsize=4)
def load_secret_hashes(path: str) -> SecretHashes:
    """Parse a secret-hash file. Refuses (SecretHashError) when it is missing, malformed or has no entries."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as e:
        raise SecretHashError(f"refusing to run: cannot read secret-hash file {path}: {e.strerror} (fail closed)") from e
    if not lines or not lines[0].startswith("salt ") or not lines[0][5:].strip():
        raise SecretHashError(f"refusing to run: {path} does not start with 'salt <hex>' (fail closed)")
    names = {}
    for line in lines[1:]:
        h, sep, name = line.partition("\t")
        if sep and re.fullmatch(r"[0-9a-f]{64}", h):
            names[h] = name.strip()
    if not names:
        raise SecretHashError(f"refusing to run: {path} has no secret hashes (fail closed)")
    return SecretHashes(lines[0][5:].strip(), names)


def hashes_path(layout: Layout) -> str | None:
    """The configured secret-hash file: env SECRET_HASHES, else the layout's [redact] secret_hashes."""
    return os.environ.get("SECRET_HASHES") or layout.secret_hashes or None


def check(layout: Layout | None = None) -> None:
    """Fail fast before any work: load the secret hashes (if configured) and validate `disable`."""
    layout = layout or load_layout()
    unknown = set(layout.redact_disable) - set(FAMILIES)
    if unknown:
        raise ValueError(f"[redact] disable: unknown families {sorted(unknown)}; known: {sorted(FAMILIES)}")
    if (path := hashes_path(layout)):
        load_secret_hashes(path)


def redact_hashed(text: str, hashes: SecretHashes) -> tuple[str, int]:
    """Replace every whole token whose salted hash is a live secret with [REDACTED:<name>]. The token is also tried
    without its '=' padding and without trailing sentence punctuation."""
    n = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal n
        tok = m.group(0)
        for cand in dict.fromkeys((tok, tok.rstrip("="), tok.rstrip(".,!?*"), tok.rstrip("=").rstrip(".,!?*"))):
            name = hashes.names.get(hashlib.sha256((hashes.salt + cand).encode()).hexdigest())
            if cand and name:
                n += 1
                return f"[REDACTED:{name}]" + tok[len(cand):]
        return tok
    return TOKEN_RE.sub(sub, text), n


def redact(text: str, layout: Layout | None = None) -> tuple[str, int]:
    """Redact live secrets (by hash, if configured) and then the enabled pattern families.
    Returns (text, number of replacements)."""
    layout = layout or load_layout()
    total = 0
    if (path := hashes_path(layout)):
        text, total = redact_hashed(text, load_secret_hashes(path))
    for family, patterns in FAMILIES.items():
        if family in layout.redact_disable:
            continue
        for pattern, repl in patterns:
            text, n = pattern.subn(repl, text)
            total += n
    return text, total
