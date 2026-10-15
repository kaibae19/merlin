import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from merlin.mcp_server import excerpt, format_hit, freshness
from merlin.search import (Hit, build_filters, escape_like, fts_sql, group_per_document, ilike_sql, rrf,
                                vector_sql)


def test_rrf_maths():
    fused = dict(rrf([[1, 2, 3], [3, 1]], k=60))
    assert fused[1] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[3] == pytest.approx(1 / 63 + 1 / 61)
    assert fused[2] == pytest.approx(1 / 62)
    assert [i for i, _ in rrf([[1, 2, 3], [3, 1]])] == [1, 3, 2]


def test_rrf_ties_and_empty():
    assert rrf([]) == [] and rrf([[], []]) == []
    assert [i for i, _ in rrf([[5], [4]])] == [4, 5]          # equal scores: stable by id


def test_build_filters():
    assert build_filters() == ("", {})
    sql, p = build_filters(["journal", "host"], "alpha", "2030-01-01", date(2030, 2, 1))
    assert sql == (" AND d.type = ANY(%(types)s) AND %(host)s = ANY(c.hosts)"
                   " AND coalesce(c.chunk_date, d.doc_date) >= %(since)s"
                   " AND coalesce(c.chunk_date, d.doc_date) <= %(until)s")
    assert p == {"types": ["journal", "host"], "host": "alpha", "since": date(2030, 1, 1), "until": date(2030, 2, 1)}
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_filters(since="last week")
    assert build_filters(repos=["infra"]) == (" AND d.repo = ANY(%(repos)s)", {"repos": ["infra"]})


def test_query_builders():
    v = vector_sql(" AND x")
    assert "<=> %(qv)s::vector" in v and "AS MATERIALIZED" in v and v.rstrip().endswith("ORDER BY dist")
    assert "WHERE c.embedding IS NOT NULL AND x ORDER BY dist LIMIT %(n)s" in v
    f = fts_sql(" AND x")
    assert "websearch_to_tsquery('english', %(q)s)" in f and "ts_rank_cd(c.tsv, query) DESC" in f
    assert "c.tsv @@ query AND x" in f
    i = ilike_sql("")
    assert "c.body ILIKE %(like)s" in i and "DESC NULLS LAST" in i
    assert escape_like("50%_a\\b") == "50\\%\\_a\\\\b"


def hit(i, path, score=0.0):
    return Hit(i, path, "journal", "", date(2030, 1, 1), None, "h", [], "body", score)


def test_group_per_document():
    hits = [hit(1, "a.md", 0.3), hit(2, "b.md", 0.2), hit(3, "a.md", 0.1), hit(4, "a.md", 0.05)]
    grouped = group_per_document(hits)
    assert [(h.id, h.more) for h in grouped] == [(1, 2), (2, 0)]
    assert hits[0].more == 0                                   # input not mutated


def sec(i, path, heading, score, day):
    return Hit(i, path, "host", "", None, date(2030, 1, day), heading, [], "body", score)


def test_group_newer_section_wins_near_tie_and_names_others():
    hits = [sec(1, "a.md", "Old plan", 0.0300, 3), sec(2, "a.md", "What it took", 0.0285, 5),
            sec(3, "b.md", "Other", 0.0200, 1), sec(4, "a.md", "Filters", 0.0100, 6)]
    a, b = group_per_document(hits)
    assert (a.id, a.score, a.more) == (2, 0.0300, 2)            # newer section within 10% shown, doc keeps best score
    assert a.also == ["Old plan", "Filters"] and b.also == []
    far = [sec(1, "a.md", "Old", 0.0300, 3), sec(2, "a.md", "New", 0.0200, 5)]
    assert group_per_document(far)[0].id == 1                   # not a near-tie: best stays
    same_day = [replace(sec(1, "a.md", "Groundwork", 0.0288, 3), stale=True), sec(2, "a.md", "Live", 0.0286, 3)]
    assert group_per_document(same_day)[0].id == 2              # unmarked beats stale on a near-tie


def test_group_undated_intro_does_not_win_on_commit_date():
    # host record edited today: its undated intro inherits the doc (commit) date, the answer has a dated heading
    intro = Hit(1, "a.md", "host", "", date(2030, 1, 12), date(2030, 1, 12), "cameras", [], "body", 0.0204,
                heading_path=["cameras"])
    answer = Hit(2, "a.md", "host", "", date(2030, 1, 12), date(2030, 1, 9), "Bridge now (2030-01-09)", [], "body",
                 0.0223, heading_path=["cameras", "Notes", "Bridge now (2030-01-09)"])
    assert group_per_document([answer, intro])[0].id == 2
    facts = Hit(4, "b.md", "host", "", date(2030, 1, 12), date(2030, 1, 12), "Facts", [], "body", 0.0300,
                heading_path=["server1", "Facts"])
    older = Hit(5, "b.md", "host", "", date(2030, 1, 12), date(2030, 1, 3), "Console (2030-01-03)", [], "body", 0.0290,
                heading_path=["server1", "Notes", "Console (2030-01-03)"])
    assert group_per_document([facts, older])[0].id == 4        # an undated best (generated Facts) stays
    same = Hit(3, "a.md", "host", "", date(2030, 1, 12), date(2030, 1, 12), "Today (2030-01-12)", [], "body", 0.0220,
               heading_path=["cameras", "Today (2030-01-12)"])
    assert group_per_document([answer, same])[0].id == 3        # a heading dated the commit day still counts


def test_rerank_stale_penalty():
    from merlin.layout import load_layout
    from merlin.search import rerank
    lay = load_layout()
    today = date(2030, 3, 1)
    stale = Hit(1, "a.md", "runbook", "", today, None, "`~/resetcamera.sh` — ⛔ OBSOLETE", [], "x", score=0.044)
    lead = Hit(2, "b.md", "host", "", today, None, "Groundwork", [], "\n(superseded later: see below)", score=0.044)
    fine = Hit(3, "c.md", "runbook", "", today, None, "Per stack", [], "drop the obsolete `version:`", score=0.030)
    assert lay.is_stale(stale.heading, stale.body) and lay.is_stale(lead.heading, lead.body)
    assert not lay.is_stale(fine.heading, fine.body)
    assert rerank([stale, lead, fine], lay, today)[0].id == 3


def test_stale_inherits_to_overview_only():
    """10-06 journal: the day index ("gitea-mcp healthy via compose override") outranked the runbook although its
    own section was marked superseded. With `inherit`, the ancestor takes the mark; a sibling doesn't."""
    from merlin.layout import load_layout
    from merlin.search import rerank
    today = date(2030, 3, 1)
    j = "journal/2030/02/2030-02-01.md"
    index = Hit(1, j, "journal", "", today, None, "session 1", [], "Day index: override added", score=0.032,
                heading_path=["2030-02-01", "session 1"])
    old = Hit(2, j, "journal", "", today, None, "override", [], "(superseded 2030-02-09: see runbook)\nDid: …",
              score=0.020, heading_path=["2030-02-01", "session 1", "override"])
    sibling = Hit(3, j, "journal", "", today, None, "other", [], "unrelated", score=0.010,
                  heading_path=["2030-02-01", "session 1", "other"])
    runbook = Hit(4, "runbooks/x.md", "runbook", "", today, None, "x", [], "fixed upstream", score=0.0224 / 1.3,
                  heading_path=["x"])
    off = load_layout()
    assert rerank([index, old, sibling, runbook], off, today)[0].id == 1             # default: the overview wins
    on = replace(off, stale_inherit_types=("journal",))
    out = {h.id: h for h in rerank([index, old, sibling, runbook], on, today)}
    assert out[1].stale and out[2].stale and not out[3].stale and not out[4].stale
    assert rerank([index, old, sibling, runbook], on, today)[0].id == 4
    assert not {h.id: h for h in rerank([index, old], replace(on, stale_inherit_types=("runbook",)), today)}[1].stale


def test_excerpt_and_format():
    body = "x" * 1000 + " Failed to restore initial working directory " + "y" * 1000
    ex = excerpt(body, "failed to restore")
    assert "Failed to restore" in ex and ex.startswith("…") and ex.endswith("…")
    h = Hit(1, "journal/2030/01/2030-01-15.md", "journal", "", date(2030, 1, 15), date(2030, 1, 16),
            "Mount error", ["alpha", "beta"], "short body", 0.0325, 2)
    out = format_hit(1, h)
    assert out.splitlines()[0] == "1. [journal] journal/2030/01/2030-01-15.md · 2030-01-16 · score 0.0325 (+2 more in this doc)"
    assert "§ Mount error · hosts: alpha, beta" in out and "   short body" in out and "also:" not in out
    out = format_hit(1, replace(h, also=["Fix", "Verify"]))
    assert "   also: § Fix · § Verify\n" in out


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


def _meta(minutes_ago=5, **heads):
    m = {"last_ingest_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "counts": "{}"}
    if set(heads) == {""}:
        m["head_sha"] = heads[""]
    else:
        m["heads"] = json.dumps(heads)
    return m


def _at(sha, minutes_ago):
    return (sha, NOW - timedelta(minutes=minutes_ago))


def test_freshness_single_repo():
    a, b = "a" * 40, "b" * 40
    assert freshness(_meta(**{"": a}), {"": _at(a, 90)}, NOW)[0] == "index fresh (at HEAD)"
    v, lines = freshness(_meta(**{"": a}), {"": _at(b, 3)}, NOW)       # a normal timer gap: not stale
    assert v == "index fresh; catching up on commits from the last few minutes: repo (3 min)"
    assert "repo: indexed aaaaaaaaaaaa · head bbbbbbbbbbbb" in lines
    v, _ = freshness(_meta(**{"": a}), {"": _at(b, 45)}, NOW)
    assert v.startswith("⚠ INDEX STALE: repo: commit bbbbbbb is 45 min old and not indexed")
    assert freshness(_meta(45, **{"": a}), {"": _at(a, 90)}, NOW)[0].startswith("⚠ INDEX STALE: last ingest 45 min ago")
    assert freshness({}, {"": _at(a, 1)}, NOW)[0].startswith("⚠ INDEX STALE: never ingested")
    assert "repo HEAD unreadable" in freshness(_meta(**{"": a}), {"": (None, None)}, NOW)[0]   # unknown is not fresh
    failed = {**_meta(**{"": a}), "last_ingest_error": "2030-01-15T11:58Z embedder down"}
    assert "last ingest failed: 2030-01-15T11:58Z embedder down" in freshness(failed, {"": _at(a, 90)}, NOW)[0]


def test_freshness_per_repo():
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    meta = _meta(infra=a, notes=b)
    assert freshness(meta, {"infra": _at(a, 60), "notes": _at(b, 60)}, NOW)[0] == "index fresh (all repos at their HEAD)"
    assert freshness(meta, {"infra": _at(a, 60), "notes": _at(c, 2)}, NOW)[0].endswith("notes (2 min)")
    v, _ = freshness(meta, {"infra": _at(a, 60), "notes": _at(c, 31)}, NOW)
    assert v.startswith("⚠ INDEX STALE: notes: commit ccccccc is 31 min old") and "infra" not in v
    assert "new never indexed" in freshness(meta, {"infra": _at(a, 60), "new": _at(c, 1)}, NOW)[0]
    assert "infra HEAD unreadable" in freshness(meta, {"infra": (None, None), "notes": _at(b, 60)}, NOW)[0]


def test_rerank_boost_and_recency():
    from merlin.layout import Layout
    from merlin.search import rerank
    lay = Layout(layers=[], exclude=[], boost={"host": 1.3, "memory-index": 0.6}, recency_weight=0.05,
                 recency_half_life=30)
    today = date(2030, 3, 1)
    j = Hit(1, "j.md", "journal", "", today, None, "", [], "", score=0.0328)
    h = Hit(2, "h.md", "host", "", today, None, "", [], "", score=0.0300)
    ix = Hit(3, "MEMORY.md", "memory-index", "", today, None, "", [], "", score=0.0330)
    out = rerank([j, h, ix], lay, today)
    assert [x.path for x in out] == ["h.md", "j.md", "MEMORY.md"]
    assert out[0].score == pytest.approx(0.0300 * 1.3 * 1.05)
    old = Hit(4, "old.md", "journal", "", date(2029, 3, 1), None, "", [], "", score=0.0328)
    assert [x.path for x in rerank([old, j], lay, today)] == ["j.md", "old.md"]      # recency breaks the tie
    nob = Layout(layers=[], exclude=[])
    assert [x.id for x in rerank([j, h, ix], nob, today)] == [3, 1, 2]               # no config: raw RRF order
