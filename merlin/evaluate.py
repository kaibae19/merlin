"""Retrieval eval: run a fixed question set through search() and report hit@1 / hit@3 / MRR.

Cases live in the corpus repo (YAML: `cases: [{q, expect: [{path, heading?}]}]`), because what counts as the right
answer is a fact about the corpus, not the code. A result counts if its path matches and, when `heading` is given,
the shown section heading contains it (case-insensitive). Optional per case:
  entity: + topic: run history(entity, topic) instead of search(q); q is then just the label (host: = entity:)
  stale: [...]     outdated docs (same form as expect); "newer wins" if an expected answer ranks above all of them
Run after every ingest or ranking change:

    python -m merlin.evaluate /repo/runbooks/merlin/eval.yaml [-k 10] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import re
from dataclasses import dataclass, field, replace

import yaml

from . import config
from .layout import load_layout
from .search import Hit, host_history, search


@dataclass
class Result:
    q: str
    rank: int | None          # 1-based rank of the first acceptable result, None if not in the top k
    top: list[str]            # "path § heading" of the top 3 results
    self_quote: list[tuple[int, str, bool]] = field(default_factory=list)  # top-3 hits quoting the question: (rank, path, is answer)
    clean_rank: int | None = None   # rank once documents that quote the question (and aren't answers) are dropped
    quote_checked: bool = False     # clean_rank was computed (run() always does; hand-built Results fall back to rank)
    newer_wins: bool | None = None  # cases with `stale`: an expected answer ranks above every stale doc


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quotes_question(doc_text: str, q: str) -> bool:
    """Does a document contain the question verbatim (case/whitespace-insensitive, trailing punctuation ignored)?
    Real questions get pasted into journals and retrieval logs; such a document can rank for the question by quoting it."""
    needle = _norm(q).rstrip("?.! ")
    return len(needle) >= 20 and needle in _norm(doc_text)


def self_quote_check(hits: list[Hit], q: str, expects: list[dict], doc_text) -> tuple[list[tuple[int, str, bool]], int | None]:
    """(top-3 hits whose document quotes the question, the rank with non-answer quoters removed)."""
    quoting = {h.path for h in hits if quotes_question(doc_text(h.path), q)}
    is_answer = lambda h: any(matches(h, e) for e in expects)
    flags = [(i, h.path, is_answer(h)) for i, h in enumerate(hits[:3], 1) if h.path in quoting]
    clean = [h for h in hits if h.path not in quoting or is_answer(h)]
    return flags, first_rank(clean, expects)


def matches(hit: Hit, expect: dict) -> bool:
    """Does this result satisfy one expected answer?"""
    if hit.path != expect["path"]:
        return False
    want = expect.get("heading")
    return not want or want.lower() in (hit.heading or "").lower()


def first_rank(hits: list[Hit], expects: list[dict]) -> int | None:
    """1-based rank of the first result matching any expected answer."""
    return next((i for i, h in enumerate(hits, 1) if any(matches(h, e) for e in expects)), None)


def newer_wins(hits: list[Hit], expects: list[dict], stale: list[dict]) -> bool:
    """Does an expected (current) answer rank above every stale doc? A stale doc that's absent can't win."""
    rank, stale_rank = first_rank(hits, expects), first_rank(hits, stale)
    return rank is not None and (stale_rank is None or rank < stale_rank)


def load_cases(path: str) -> list[dict]:
    with open(path) as f:
        cases = (yaml.safe_load(f) or {}).get("cases") or []
    for c in cases:
        if not c.get("q") or not c.get("expect"):
            raise ValueError(f"case needs q and expect: {c!r}")
    return cases


def _scores(ranks: list[int | None], n: int) -> dict:
    return {"hit@1": sum(r == 1 for r in ranks) / n,
            "hit@3": sum(r is not None and r <= 3 for r in ranks) / n,
            "mrr": sum(1 / r for r in ranks if r) / n}


def summarize(results: list[Result]) -> dict:
    """hit@1 / hit@3 / MRR as ranked, plus `self_quote_cases` and the same scores with self-quoting non-answers dropped."""
    n = len(results) or 1
    stale = [r.newer_wins for r in results if r.newer_wins is not None]
    return {"cases": len(results), **_scores([r.rank for r in results], n),
            "self_quote_cases": sum(any(not gold for _, _, gold in r.self_quote) for r in results),
            "clean": _scores([r.clean_rank if r.quote_checked else r.rank for r in results], n),
            "newer_wins": sum(stale), "stale_cases": len(stale)}


def run(conn, cases: list[dict], k: int = 10, layout=None) -> list[Result]:
    texts: dict[str, str] = {}

    def doc_text(path: str) -> str:
        if path not in texts:
            texts[path] = "\n".join(f"{h}\n{b}" for h, b in conn.execute(
                "SELECT heading, body FROM chunks WHERE path = %s ORDER BY ord", (path,)).fetchall())
        return texts[path]

    out = []
    for c in cases:
        if c.get("entity") or c.get("host"):
            hits = host_history(conn, c.get("entity") or c["host"], topic=c.get("topic"), limit=k, layout=layout)
        else:
            hits = search(conn, c["q"], k=k, layout=layout)
        flags, clean = self_quote_check(hits, c["q"], c["expect"], doc_text)
        out.append(Result(c["q"], first_rank(hits, c["expect"]), [f"{h.path} § {h.heading}" for h in hits[:3]],
                          flags, clean, True, newer_wins(hits, c["expect"], c["stale"]) if c.get("stale") else None))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--boost", help='override type boosts, JSON, e.g. \'{"journal": 1.0}\' or \'{}\' for none')
    ap.add_argument("--recency", type=float, help="override the recency weight (0 = off)")
    a = ap.parse_args(argv)
    layout = load_layout()
    if a.boost is not None:
        layout = replace(layout, boost={k: float(v) for k, v in json.loads(a.boost).items()})
    if a.recency is not None:
        layout = replace(layout, recency_weight=a.recency)
    conn = config.connect()
    if config.DB_SCHEMA:
        config.require_index(conn)           # never score another schema's index by accident
    results = run(conn, load_cases(a.cases), a.k, layout)
    s = summarize(results)
    if a.json:
        print(json.dumps({"summary": s, "results": [r.__dict__ for r in results]}, indent=2))
        return 0
    for r in results:
        mark = "✅" if r.rank == 1 else ("⚠ " if r.rank and r.rank <= 3 else "⛔")
        print(f"{mark} {r.rank or '-':>2}  {r.q}" + ("" if r.newer_wins is None else "   [newer wins]" if r.newer_wins else "   [STALE WINS]"))
        if r.rank != 1:
            for t in r.top:
                print(f"         · {t[:110]}")
        for i, path, gold in r.self_quote:
            print(f"         ⚑ #{i} {path} quotes the question verbatim{' (an accepted answer)' if gold else ' — not an answer; clean rank ' + str(r.clean_rank or '-')}")
    print(f"\ncases {s['cases']} · hit@1 {s['hit@1']:.0%} · hit@3 {s['hit@3']:.0%} · MRR {s['mrr']:.3f}")
    c = s["clean"]
    print(f"self-quote: {s['self_quote_cases']} case(s) with a non-answer quoting its question in the top 3 · without such docs: "
          f"hit@1 {c['hit@1']:.0%} · hit@3 {c['hit@3']:.0%} · MRR {c['mrr']:.3f}")
    if s["stale_cases"]:
        print(f"newer wins: {s['newer_wins']}/{s['stale_cases']} cases with a stale doc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
