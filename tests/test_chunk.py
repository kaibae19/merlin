import os
from datetime import date
from pathlib import Path

import pytest

from conftest import CORPUS, read
from merlin.chunk import (HostMatcher, chunk_document, estimate_tokens, parse_front_matter,
                               split_to_cap)


def by_heading(doc, needle):
    return next(c for c in doc.chunks if needle in c.heading)


def test_host_matcher_aliases_and_ips(matcher):
    assert matcher.find("ssh beta.example.internal") == {"beta"}
    assert matcher.find("mounted from the NAS at 192.0.2.10") == {"alpha"}
    assert matcher.find("alphabet betamax 192.0.2.100") == set()     # word/IP boundaries
    assert "<name>" not in matcher.aliases.values()                   # template skipped


def test_host_text_ignore():
    from conftest import CORPUS
    docs = {str(p): p.read_text() for p in (CORPUS / "hosts").glob("*.md")}
    m = HostMatcher.from_host_docs(docs, {"NAS"})
    assert m.find("mounted from the NAS") == set()                     # a common word: not a mention
    assert m.find("ssh alpha.example.internal") == {"alpha"}           # the other names still match
    assert m.aliases["nas"] == "alpha"                                 # and it still resolves as a filter


def test_context_header_and_headings(matcher, layout):
    doc = chunk_document("runbooks/backup/README.md", read("runbooks/backup/README.md"), matcher, layout=layout)
    assert doc.type == "runbook" and doc.title == "Backup"
    verify = by_heading(doc, "Verify")
    assert verify.text.startswith("[runbook] runbooks/backup/README.md > Backup > Verify\n\n")
    assert verify.heading_path == ["Backup", "Verify"]
    assert verify.hosts == ["alpha"]


def test_host_record(matcher, layout):
    doc = chunk_document("hosts/alpha.md", read("hosts/alpha.md"), matcher, date(2030, 2, 1), layout=layout)
    headings = [c.heading for c in doc.chunks]
    assert not any(h == "Facts" for h in headings)                    # comment-only section dropped
    assert all("alpha" in c.hosts for c in doc.chunks)                # front matter host on every chunk
    assert "roles: [storage, nfs]" in doc.chunks[0].body               # front matter kept in the first chunk
    scrub = by_heading(doc, "Pool scrub")
    assert scrub.heading == "Pool scrub found checksum errors (2030-01-14)"
    assert scrub.text.startswith("[host] hosts/alpha.md > alpha > Notes > Pool scrub")
    assert scrub.chunk_date == date(2030, 1, 14)                       # dated heading
    assert by_heading(doc, "Undated").chunk_date == date(2030, 2, 1)  # falls back to the git date
    assert doc.doc_date == date(2030, 2, 1)


def test_journal_dates_and_hosts(matcher, layout):
    doc = chunk_document("journal/2030/01/2030-01-15.md", read("journal/2030/01/2030-01-15.md"), matcher,
                         date(2031, 1, 1), layout=layout)
    assert doc.doc_date == date(2030, 1, 15)                           # filename beats git date
    assert by_heading(doc, "beta root disk").chunk_date == date(2030, 1, 15)
    assert by_heading(doc, "beta after the prune").chunk_date == date(2030, 1, 16)   # session-2 block date
    assert by_heading(doc, "beta root disk").hosts == ["beta"]
    assert set(by_heading(doc, "Mount error").hosts) == {"alpha", "beta"}            # IP + "nas" alias


def test_journal_fence_not_split_and_no_host_leak(matcher, layout):
    doc = chunk_document("journal/2030/01/2030-01-14.md", read("journal/2030/01/2030-01-14.md"), matcher,
                         layout=layout)
    scrub = by_heading(doc, "alpha scrub errors")
    assert "## not a heading" in scrub.body
    assert not any("not a heading" in c.heading for c in doc.chunks)
    assert by_heading(doc, "Backup runbook").hosts == []              # day headline's hosts don't leak down


def test_front_matter_date_and_types(matcher, layout):
    mem = chunk_document("memory/scrub-cables.md", read("memory/scrub-cables.md"), matcher, layout=layout)
    assert mem.type == "memory" and mem.doc_date == date(2030, 1, 14) and mem.title == "scrub-cables"
    assert chunk_document("memory/MEMORY.md", read("memory/MEMORY.md"), matcher, layout=layout).type == "memory-index"
    assert chunk_document("hosts/_template.md", read("hosts/_template.md"), matcher, layout=layout) is None
    assert chunk_document("runbooks/backup/backup.sh", "x", matcher, layout=layout) is None


def test_cap_splits_at_paragraphs(matcher, layout):
    paras = [f"Paragraph {i} " + "word " * 300 for i in range(12)]
    text = "# Big\n\n## Long section\n\n" + "\n\n".join(paras)
    doc = chunk_document("runbooks/big/README.md", text, matcher, layout=layout, max_tokens=1500)
    assert len(doc.chunks) > 1
    assert all(estimate_tokens(c.text) <= 1500 for c in doc.chunks)
    assert all(c.body.startswith("Paragraph") for c in doc.chunks)    # cut on paragraph boundaries
    assert all(c.text.startswith("[runbook] runbooks/big/README.md > Big > Long section") for c in doc.chunks)


def test_cap_handles_one_huge_line():
    pieces = split_to_cap("x " * 5000, 200)
    assert len(pieces) > 1 and all(estimate_tokens(p) <= 200 for p in pieces)


def test_bad_front_matter_is_ignored():
    fm, raw, body = parse_front_matter("---\n: : bad [\n---\n# T\n")
    assert fm == {} and body == "# T\n"


@pytest.mark.skipif(not os.environ.get("CORPUS_DIR"), reason="CORPUS_DIR not set")
def test_real_corpus_respects_cap(layout):
    root = Path(os.environ["CORPUS_DIR"])
    hosts = HostMatcher.from_host_docs({str(p): p.read_text() for p in (root / "hosts").glob("*.md")})
    n = 0
    for p in root.rglob("*.md"):
        doc = chunk_document(str(p.relative_to(root)), p.read_text(errors="replace"), hosts, layout=layout)
        for c in doc.chunks if doc else []:
            n += 1
            assert estimate_tokens(c.text) <= 1500 and c.text.startswith(f"[{doc.type}] ")
    assert n > 0


def test_unclosed_fence_recovers_at_headings(matcher, layout):
    text = ("# Day\n\n### First\nHit:\n```\nout one\n\n### Second\nHit:\n```\nout two\n```\n\n"
            "### Third\nbody three\n")
    doc = chunk_document("journal/2030/01/2030-01-02.md", text, matcher, layout=layout)
    assert [c.heading for c in doc.chunks] == ["First", "Second", "Third"]
    assert "out two" in by_heading(doc, "Second").body and "body three" not in by_heading(doc, "Second").body
    assert doc.warnings and "balance" in doc.warnings[0]


def test_balanced_fence_keeps_quoted_markdown(matcher, layout):
    text = "# Guide\n\n## Template\n```\n## <date> headline\n### <topic>\n```\nafter\n"
    doc = chunk_document("runbooks/guide/README.md", text, matcher, layout=layout)
    assert [c.heading for c in doc.chunks] == ["Template"]
    assert "### <topic>" in doc.chunks[0].body and not doc.warnings


def test_invalid_front_matter_warns(matcher, layout):
    text = "---\nhost: gamma\naccess: ssh root (keys: a, b)\n---\n# gamma\n\nbody\n"
    doc = chunk_document("hosts/gamma.md", text, matcher, layout=layout)
    assert doc.warnings and "not valid YAML" in doc.warnings[0]
    assert all("gamma" not in c.hosts for c in doc.chunks)     # host field ignored, as before
