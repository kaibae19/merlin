import pytest

from merlin.evaluate import Result, first_rank, load_cases, matches, newer_wins, summarize
from merlin.search import Hit


def hit(path, heading=""):
    return Hit(0, path, "runbook", "", None, None, heading, [], "")


def test_matches_path_and_heading():
    assert matches(hit("a.md", "Cutover (done)"), {"path": "a.md"})
    assert matches(hit("a.md", "Cutover (done)"), {"path": "a.md", "heading": "cutover"})
    assert not matches(hit("a.md", "Build"), {"path": "a.md", "heading": "Cutover"})
    assert not matches(hit("b.md", "Cutover"), {"path": "a.md"})


def test_first_rank_any_expected():
    hits = [hit("x.md"), hit("b.md"), hit("a.md")]
    assert first_rank(hits, [{"path": "a.md"}, {"path": "b.md"}]) == 2
    assert first_rank(hits, [{"path": "zz.md"}]) is None


def test_summarize():
    s = summarize([Result("q1", 1, []), Result("q2", 3, []), Result("q3", None, []), Result("q4", 2, [])])
    assert s["cases"] == 4 and s["hit@1"] == 0.25 and s["hit@3"] == 0.75
    assert s["mrr"] == pytest.approx((1 + 1 / 3 + 1 / 2) / 4)


def test_load_cases_validates(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("cases:\n  - q: hi\n    expect: [{path: a.md}]\n")
    assert load_cases(str(p))[0]["q"] == "hi"
    p.write_text("cases:\n  - q: hi\n")
    with pytest.raises(ValueError):
        load_cases(str(p))


from merlin.evaluate import quotes_question, self_quote_check

Q = "How do I read or post Matrix messages as House Bot from the shell?"


def test_quotes_question_rules():
    assert quotes_question("notes:\n  \"how do I read  or post matrix messages as house bot\nfrom the shell\" → #1", Q)  # case, spaces, ?
    assert not quotes_question("how to read Matrix messages from the shell", Q)            # paraphrase is not a quote
    assert not quotes_question("short q", "short q")                                       # too short to mean anything


def test_self_quote_check_flags_and_clean_rank():
    hits = [hit("journal/day.md"), hit("memory/matrix.md"), hit("runbooks/matrix-api/README.md"), hit("x.md"), hit("log.md"), hit("y.md")]
    texts = {"journal/day.md": f"re-test: {Q} → #1", "runbooks/matrix-api/README.md": f"FAQ: {Q}", "log.md": Q}
    expects = [{"path": "runbooks/matrix-api/README.md"}, {"path": "y.md"}]
    flags, clean = self_quote_check(hits, Q, expects, lambda p: texts.get(p, ""))
    assert flags == [(1, "journal/day.md", False), (3, "runbooks/matrix-api/README.md", True)]   # top 3 only; answer kept
    assert clean == 2                       # journal (non-answer quoter) dropped; the answer that quotes it stays
    flags, clean = self_quote_check(hits, Q, [{"path": "y.md"}], lambda p: texts.get(p, ""))
    assert clean == 3                       # quoters below the top 3 (log.md at #5) are dropped from the clean rank too


def test_summarize_clean_scores():
    rs = [Result("q1", 1, [], [(1, "j.md", False)], 2, True), Result("q2", 2, [], [], 2, True), Result("q3", 1, [])]
    s = summarize(rs)
    assert s["self_quote_cases"] == 1 and s["hit@1"] == pytest.approx(2 / 3)
    assert s["clean"]["hit@1"] == pytest.approx(1 / 3) and s["clean"]["mrr"] == pytest.approx((0.5 + 0.5 + 1) / 3)


def test_newer_wins():
    hits = [hit("old.md"), hit("new.md"), hit("other.md")]
    new, old = [{"path": "new.md"}], [{"path": "old.md"}]
    assert not newer_wins(hits, new, old)                     # the stale doc ranks first
    assert newer_wins(hits[1:], new, old)                     # stale doc absent: the current one wins
    assert not newer_wins(hits, [{"path": "missing.md"}], old)  # no current answer at all
    assert newer_wins(hits, old, new)


def test_summarize_counts_stale_cases():
    s = summarize([Result("a", 1, [], newer_wins=True), Result("b", 2, [], newer_wins=False), Result("c", 1, [])])
    assert (s["newer_wins"], s["stale_cases"]) == (1, 2)
