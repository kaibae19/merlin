"""Typed, heading-scoped chunking of the corpus.

Each chunk is one `##`/`###` section (long ones split at paragraph boundaries). Its embedded text starts with a
context header `[type] path > H1 > H2 > H3`, so a section called "Verify" still says which runbook it is in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath

import yaml

from .layout import Layout, load_layout

MAX_TOKENS = 1500
DATE_RE = re.compile(r"\b(20\d\d-[01]\d-[0-3]\d)\b")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TOKEN_RE = re.compile(r"\w+|[^\w\s]")


@dataclass
class Chunk:
    ord: int
    heading: str
    body: str
    text: str                     # header + body: what gets embedded (before the provider prefix)
    hosts: list[str]
    chunk_date: date | None
    heading_path: list[str] = field(default_factory=list)   # [h1, h2, h3] without empty levels


@dataclass
class Document:
    path: str
    type: str
    title: str
    doc_date: date | None
    chunks: list[Chunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """Rough token count (words + punctuation); errs high for code, which is the safe side."""
    return len(TOKEN_RE.findall(text))


def parse_front_matter(text: str) -> tuple[dict, str, str]:
    """Split YAML front matter off; returns (parsed dict, raw yaml, remaining text)."""
    m = FRONT_RE.match(text)
    if not m:
        return {}, "", text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(1), text[m.end():]


def _yaml_ok(raw: str) -> bool:
    try:
        yaml.safe_load(raw)
        return True
    except yaml.YAMLError:
        return False


def _as_date(v: object) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    m = DATE_RE.search(str(v)) if v else None
    return date.fromisoformat(m.group(1)) if m else None


class HostMatcher:
    """Finds mentions of known hosts (names, aliases, IPs) and maps them to the canonical host name."""

    def __init__(self, aliases: dict[str, str], text_ignore: frozenset[str] | set[str] = frozenset()):
        self.aliases = {k.lower(): v for k, v in aliases.items()}
        # names that resolve as a host filter but are never matched in text (an alias that is also a common word)
        self.text_ignore = {n.lower() for n in text_ignore}
        alts = sorted((a for a in self.aliases if a not in self.text_ignore), key=len, reverse=True)
        self.regex = (re.compile(r"(?<![\w.-])(" + "|".join(map(re.escape, alts)) + r")(?![\w-])", re.I)
                      if alts else None)

    @classmethod
    def from_host_docs(cls, docs: dict[str, str], text_ignore: frozenset[str] | set[str] = frozenset()) -> "HostMatcher":
        """Build from {path: text} of hosts/*.md, using front matter host, aliases and ips."""
        aliases: dict[str, str] = {}
        for text in docs.values():
            fm, _, _ = parse_front_matter(text)
            host = str(fm.get("host") or "").strip()
            if not host or host.startswith("<"):
                continue
            aliases[host] = host
            for a in (fm.get("aliases") or []) + (fm.get("ips") or []):
                aliases[str(a)] = host
        return cls(aliases, text_ignore)

    def find(self, text: str) -> set[str]:
        """Canonical names of every host mentioned in text."""
        if not self.regex:
            return set()
        return {self.aliases[m.group(1).lower()] for m in self.regex.finditer(text)}


def unclosed_fence(body: str) -> int | None:
    """Line number (1-based) of a code fence that is never closed, or None if fences balance."""
    opened, fence = None, False
    for n, line in enumerate(body.splitlines(), 1):
        if FENCE_RE.match(line):
            fence = not fence
            opened = n if fence else None
    return opened


def _sections(body: str, h1_default: str, recover: bool = False) -> list[tuple[list[str], str]]:
    """Split markdown into (heading chain [h1, h2, h3], text) on #/##/### outside code fences.

    `recover` is for documents whose fences don't balance: one unclosed fence inverts every later fence, which
    would swallow all following sections into one. There, a `##`/`###` line inside a "fence" is taken as a
    heading and ends the fence. (Not done for balanced documents, which may quote markdown in a code block.)"""
    out: list[tuple[list[str], str]] = []
    chain, lines, fence = [h1_default, "", ""], [], False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            fence = not fence
        if fence and recover and re.match(r"^#{2,3}\s+\S", line):
            fence = False
        m = None if fence else re.match(r"^(#{1,3})\s+(.*?)\s*#*\s*$", line)
        if m:
            out.append((chain[:], "\n".join(lines)))
            level, title = len(m.group(1)), m.group(2)
            chain = [title, "", ""] if level == 1 else chain[:level - 1] + [title] + [""] * (3 - level)
            lines = []
        else:
            lines.append(line)
    out.append((chain, "\n".join(lines)))
    return out


def _blocks(text: str) -> list[str]:
    """Paragraphs separated by blank lines, never splitting inside a code fence."""
    blocks, cur, fence = [], [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            fence = not fence
        if not line.strip() and not fence:
            if cur:
                blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def split_to_cap(text: str, budget: int) -> list[str]:
    """Pack paragraphs into pieces of at most `budget` tokens; oversized paragraphs split by line, then by size."""
    units: list[str] = []
    for b in _blocks(text):
        if estimate_tokens(b) <= budget:
            units.append(b)
            continue
        for line in b.splitlines():
            while estimate_tokens(line) > budget:   # one enormous line: cut by characters
                cut = len(line) * budget // (estimate_tokens(line) + 1)
                units.append(line[:cut])
                line = line[cut:]
            units.append(line)
    pieces, cur, size = [], [], 0
    for u in units:
        t = estimate_tokens(u)
        if cur and size + t > budget:
            pieces.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(u)
        size += t
    if cur:
        pieces.append("\n\n".join(cur))
    return pieces


def chunk_document(path: str, text: str, hosts: HostMatcher, fallback_date: date | None = None,
                   max_tokens: int | None = None, layout: Layout | None = None) -> Document | None:
    """Chunk one (already redacted) document. Returns None for paths the layout does not index."""
    layout = layout or load_layout()
    max_tokens = max_tokens or layout.chunk_max_tokens
    dtype = layout.doc_type(path)
    if dtype is None:
        return None
    fm, raw_fm, body = parse_front_matter(text)
    h1 = next((m.group(1).strip() for m in re.finditer(r"^# (.+)$", body, re.M)), "")
    title = h1 or str(fm.get("name") or PurePosixPath(path).stem)
    name_date = _as_date(PurePosixPath(path).name) if dtype in layout.dated_filename_types else None
    doc_date = name_date or _as_date(fm.get("date") or fm.get("updated") or fm.get("created")) or fallback_date
    fm_host = str(fm.get("host") or "")
    base_hosts = {hosts.aliases.get(fm_host.lower(), fm_host)} if fm_host and not fm_host.startswith("<") else set()
    doc = Document(path, dtype, title, doc_date)
    if raw_fm.strip() and not fm and not _yaml_ok(raw_fm):
        doc.warnings.append("front matter is not valid YAML (quote values containing ': '); its fields are ignored")
    bad = unclosed_fence(body)
    if bad:
        doc.warnings.append(f"code fences don't balance (last unmatched opener at body line {bad}); "
                            "recovered at ##/### headings")
    for i, (chain, sec) in enumerate(_sections(body, "", recover=bad is not None)):
        if i == 0 and raw_fm:
            sec = raw_fm + "\n\n" + sec
        sec = COMMENT_RE.sub("", sec).strip()
        if not sec:
            continue
        headings = [h for h in chain if h]
        header = f"[{dtype}] {path}" + "".join(f" > {h}" for h in headings)
        heading = headings[-1] if headings else title   # own heading; the breadcrumb lives in the header
        dated = [d for d in (_as_date(h) for h in reversed(chain[1:]) if h) if d]
        chunk_date = dated[0] if dated else doc_date
        budget = max(100, max_tokens - estimate_tokens(header) - 2)
        for piece in split_to_cap(sec, budget):
            found = base_hosts | hosts.find((headings[-1] if headings else "") + "\n" + piece)
            doc.chunks.append(Chunk(len(doc.chunks), heading, piece, f"{header}\n\n{piece}",
                                    sorted(found), chunk_date, headings))
    return doc
