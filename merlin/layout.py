"""Corpus layout: which paths are indexed, as which doc type, and how the MCP tells the model to weigh each type.

Loaded from the TOML file named by MERLIN_CONFIG (default `./merlin.toml` when present); otherwise the shipped
default below, which matches a files-canonical layout of journal / hosts / runbooks / memory / planning / skills.

A corpus may span several git repos: `[[repos]]` entries (`name`, `dir`) are indexed together, each file under the
virtual path `<name>/<path>`, which is what the layer globs match and what search returns. Without `[[repos]]` the
corpus is the single REPO_DIR and paths carry no prefix.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DEFAULT_TOML = r"""
exclude = ["**/_template.md"]
host_types = ["host"]              # front matter `host:` + `aliases:` + `ips:` of these docs define known hosts
dated_filename_types = ["journal"] # a YYYY-MM-DD in the filename is the doc date
history_types = ["journal", "host"]

layers = [                         # first match wins; only *.md files are ever indexed
  { glob = "journal/**",        type = "journal" },
  { glob = "hosts/*.md",        type = "host" },
  { glob = "runbooks/**",       type = "runbook" },
  { glob = "memory/MEMORY.md",  type = "memory-index" },
  { glob = "memory/*.md",       type = "memory" },
  { glob = "planning/**",       type = "planning" },
  { glob = "skills/**",         type = "skill" },
  { glob = "*.md",              type = "doc" },
]

[weights]
journal = "what was seen on that date: verbatim evidence, may be superseded by later entries"
host = "current practice: what each host is and how it is set up now"
runbook = "current practice: how to do it"
memory = "lessons and gotchas, short, with pointers to the detail"
memory-index = "the index of lessons"
planning = "intent: what is planned, not what is done"
skill = "session procedures for the assistant"
doc = "orientation and conventions"

[boost]                            # multiplies a chunk's fused (RRF) score by doc type; unlisted types = 1.0
host = 1.3                         # canonical current practice outranks the journal entry that produced it
runbook = 1.3
memory = 1.3
journal = 1.0
planning = 0.9
doc = 0.8
skill = 0.7
memory-index = 0.6                 # the index only points at lessons; the lesson itself should win

[recency]                          # tiebreak: score *= 1 + weight * 0.5 ** (age_days / half_life_days)
weight = 0.05
half_life_days = 30

[stale]                            # a section that says it is stale: score *= factor
heading = '(?i)\b(superseded|obsolete)\b'          # anywhere in the section heading
lead = '(?i)^\W*(superseded|obsolete)\b'           # or opening the section's first line: "(superseded …", "⛔ OBSOLETE"
factor = 0.5
# inherit = ["journal"]                            # an overview (day index) above a matched stale section is stale too
"""


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Path-aware glob: `**` spans directories, `*` and `?` stay within one path segment."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out + r"\Z")


@dataclass
class Layout:
    layers: list[tuple[re.Pattern[str], str]]
    exclude: list[re.Pattern[str]]
    weights: dict[str, str] = field(default_factory=dict)
    host_types: tuple[str, ...] = ("host",)
    dated_filename_types: tuple[str, ...] = ("journal",)
    history_types: tuple[str, ...] = ("journal", "host")
    history_record_chunks: int = 0             # [history] record_chunks: host_history leads with this many chunks of the host's own record
    history_exclude: list[re.Pattern[str]] = field(default_factory=list)   # [history] exclude: globs left out of host_history
    boost: dict[str, float] = field(default_factory=dict)
    recency_weight: float = 0.0
    recency_half_life: float = 30.0
    stale_heading: re.Pattern[str] | None = None
    stale_lead: re.Pattern[str] | None = None
    stale_factor: float = 1.0
    stale_inherit_types: tuple[str, ...] = ()   # [stale] inherit: doc types whose overview chunks take a matched child section's stale mark
    repos: tuple[tuple[str, str], ...] = ()   # (name, git dir); empty = the single REPO_DIR, paths unprefixed
    secret_hashes: str | None = None          # [redact] salted-hash file of live secrets; set = required
    redact_disable: frozenset[str] = frozenset()   # [redact] pattern families turned off
    host_text_ignore: frozenset[str] = frozenset()  # [hosts] text_ignore: aliases never matched in body text
    chunk_max_tokens: int = 1500               # [chunk] max_tokens: a section longer than this is split
    search_fusion: str = "hybrid"              # [search] fusion: "hybrid" (vector + full-text, RRF) or "vector"
    corpus: str = "a files-canonical record (journal, host records, runbooks, lessons, planning)"   # shown to the model

    @classmethod
    def from_toml(cls, text: str) -> "Layout":
        """Parse a layout TOML document."""
        d = tomllib.loads(text)
        repos = tuple((str(r["name"]), str(r["dir"])) for r in d.get("repos", []))
        names = [n for n, _ in repos]
        bad = [n for n in names if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", n)]
        if bad or len(set(names)) != len(names):
            raise ValueError(f"[[repos]] names must be unique path segments, got {names}")
        red = d.get("redact", {})
        fusion = str(d.get("search", {}).get("fusion", "hybrid"))
        if fusion not in ("hybrid", "vector"):
            raise ValueError(f'[search] fusion must be "hybrid" or "vector", got {fusion!r}')
        return cls(repos=repos,
                   chunk_max_tokens=int(d.get("chunk", {}).get("max_tokens", 1500)),
                   search_fusion=fusion,
                   **({"corpus": str(d["corpus"])} if d.get("corpus") else {}),
                   host_text_ignore=frozenset(str(n).lower() for n in d.get("hosts", {}).get("text_ignore", [])),
                   secret_hashes=str(red["secret_hashes"]) if red.get("secret_hashes") else None,
                   redact_disable=frozenset(map(str, red.get("disable", []))),
                   layers=[(glob_to_regex(l["glob"]), l["type"]) for l in d.get("layers", [])],
                   exclude=[glob_to_regex(g) for g in d.get("exclude", [])],
                   weights=dict(d.get("weights", {})),
                   host_types=tuple(d.get("host_types", ["host"])),
                   dated_filename_types=tuple(d.get("dated_filename_types", ["journal"])),
                   history_types=tuple(d.get("history_types", ["journal", "host"])),
                   history_record_chunks=int(d.get("history", {}).get("record_chunks", 0)),
                   history_exclude=[glob_to_regex(g) for g in d.get("history", {}).get("exclude", [])],
                   boost={k: float(v) for k, v in d.get("boost", {}).items()},
                   recency_weight=float(d.get("recency", {}).get("weight", 0.0)),
                   recency_half_life=float(d.get("recency", {}).get("half_life_days", 30.0)),
                   stale_heading=re.compile(d["stale"]["heading"]) if d.get("stale", {}).get("heading") else None,
                   stale_lead=re.compile(d["stale"]["lead"]) if d.get("stale", {}).get("lead") else None,
                   stale_factor=float(d.get("stale", {}).get("factor", 1.0)),
                   stale_inherit_types=tuple(map(str, d.get("stale", {}).get("inherit", []))))

    def sources(self, default_dir: str) -> list[tuple[str, str]]:
        """The (name, git dir) pairs to index: the configured [[repos]], else default_dir with no name."""
        return list(self.repos) or [("", default_dir)]

    @staticmethod
    def vpath(name: str, path: str) -> str:
        """The indexed (virtual) path of a file: `<repo name>/<path>`, or just the path for an unnamed repo."""
        return f"{name}/{path}" if name else path

    def split(self, vpath: str) -> tuple[str, str]:
        """(repo name, path in that repo) for a virtual path; ("", vpath) when no [[repos]] are configured."""
        for name, _ in self.repos:
            if vpath.startswith(name + "/"):
                return name, vpath[len(name) + 1:]
        if self.repos:
            raise ValueError(f"{vpath!r} is not under a configured repo ({', '.join(n for n, _ in self.repos)})")
        return "", vpath

    def is_stale(self, heading: str, body: str) -> bool:
        """Does the section mark itself stale (in its heading, or at the start of its first line)?"""
        first = next((l for l in body.splitlines() if l.strip()), "")
        return bool((self.stale_heading and self.stale_heading.search(heading or ""))
                    or (self.stale_lead and self.stale_lead.search(first)))

    def doc_type(self, path: str) -> str | None:
        """The doc type for a repo-relative path, or None if it is not indexed."""
        if not path.lower().endswith(".md") or any(x.match(path) for x in self.exclude):
            return None
        return next((t for rx, t in self.layers if rx.match(path)), None)

    def weighting_text(self) -> str:
        """One line per type, for MCP tool descriptions."""
        return "\n".join(f"- {t}: {w}" for t, w in self.weights.items())


@lru_cache(maxsize=1)
def load_layout(path: str | None = None) -> Layout:
    """Load the layout from MERLIN_CONFIG / ./merlin.toml, else the shipped default."""
    path = path or os.environ.get("MERLIN_CONFIG") or ("merlin.toml" if Path("merlin.toml").exists() else None)
    return Layout.from_toml(Path(path).read_text() if path else DEFAULT_TOML)
