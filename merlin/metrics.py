"""Index freshness as Prometheus metrics, written to a node_exporter textfile-collector file (env METRICS_FILE).

Written after every successful ingest only, so a failing ingest shows as merlin_index_last_run_timestamp_seconds
growing old. Per repo (label `repo`; "repo" for a single-repo corpus):
    merlin_index_repo_indexed_timestamp_seconds   when that repo's index last moved to a new HEAD
    merlin_index_repo_behind_mirror               1 if the repo's HEAD, re-read after the run, is not the indexed one
    merlin_index_repo_documents / _chunks         what the index holds for it
The names are those of merlin v1's exporter, kept so the series continue across the switch.
"""
from __future__ import annotations

import os

import psycopg

from . import config


def render(now: float, indexed_at: dict[str, float], behind: dict[str, bool], counts: dict[str, tuple[int, int]]) -> str:
    """The textfile body. Repos come from `behind` (every configured repo appears, even an empty one)."""
    lines = ["# HELP merlin_index_last_run_timestamp_seconds Last successful ingest run.",
             "# TYPE merlin_index_last_run_timestamp_seconds gauge",
             f"merlin_index_last_run_timestamp_seconds {now:.0f}"]
    for name in sorted(behind):
        label = f'{{repo="{name or "repo"}"}}'
        docs, chunks = counts.get(name, (0, 0))
        lines += [f"merlin_index_repo_indexed_timestamp_seconds{label} {indexed_at.get(name, 0):.0f}",
                  f"merlin_index_repo_behind_mirror{label} {int(behind[name])}",
                  f"merlin_index_repo_documents{label} {docs}",
                  f"merlin_index_repo_chunks{label} {chunks}"]
    return "\n".join(lines) + "\n"


def write(conn: psycopg.Connection, path: str, sources: list[tuple[str, str]], heads: dict[str, str],
          indexed_at: dict[str, float], now: float) -> None:
    """Re-read each repo's HEAD, count what is indexed per repo, and replace the file atomically."""
    behind = {}
    for name, d in sources:
        try:
            behind[name] = config.git("rev-parse", "HEAD", repo=d).strip() != heads.get(name)
        except RuntimeError:
            behind[name] = True                     # cannot read it: never report it as caught up
    rows = conn.execute("SELECT d.repo, count(DISTINCT d.path), count(c.id) FROM documents d "
                        "LEFT JOIN chunks c ON c.path = d.path GROUP BY d.repo").fetchall()
    body = render(now, indexed_at, behind, {r: (int(n), int(c)) for r, n, c in rows})
    tmp = path + ".tmp"                             # not *.prom, so the collector never reads a partial file
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, path)
