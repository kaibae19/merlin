import pytest

from merlin.layout import Layout, glob_to_regex


@pytest.mark.parametrize("path,expected", [
    ("journal/2030/01/2030-01-14.md", "journal"),
    ("journal/README.md", "journal"),
    ("hosts/alpha.md", "host"),
    ("hosts/_template.md", None),
    ("hosts/sub/x.md", None),
    ("runbooks/backup/README.md", "runbook"),
    ("runbooks/backup/backup.sh", None),
    ("memory/MEMORY.md", "memory-index"),
    ("memory/scrub-cables.md", "memory"),
    ("planning/backlog.md", "planning"),
    ("skills/daily-check/SKILL.md", "skill"),
    ("README.md", "doc"),
    ("index/notes.md", None),
])
def test_default_mapping(layout, path, expected):
    assert layout.doc_type(path) == expected


def test_glob_semantics():
    assert glob_to_regex("*.md").match("README.md")
    assert not glob_to_regex("*.md").match("a/README.md")
    assert glob_to_regex("**/_template.md").match("_template.md")
    assert glob_to_regex("**/_template.md").match("a/b/_template.md")
    assert glob_to_regex("docs/**").match("docs/x/y.md")


def test_custom_toml():
    lay = Layout.from_toml("""
layers = [{ glob = "notes/**", type = "note" }]
history_types = ["note"]
[weights]
note = "scratch"
""")
    assert lay.doc_type("notes/a/b.md") == "note"
    assert lay.doc_type("README.md") is None
    assert lay.history_types == ("note",)
    assert lay.weighting_text() == "- note: scratch"


def test_weighting_text_covers_types(layout):
    text = layout.weighting_text()
    for t in ("journal", "host", "runbook", "memory", "planning"):
        assert f"- {t}:" in text
    assert "superseded" in text and "intent" in text


def test_example_config_is_the_default(layout):
    from pathlib import Path
    example = Layout.from_toml((Path(__file__).parent.parent / "merlin.example.toml").read_text())
    assert example.weights == layout.weights and example.history_types == layout.history_types
    assert [(rx.pattern, t) for rx, t in example.layers] == [(rx.pattern, t) for rx, t in layout.layers]


MULTI = '''
[[repos]]
name = "notes"
dir = "/srv/notes.git"
[[repos]]
name = "infra"
dir = "/srv/infra"
'''


def test_repos_and_virtual_paths(layout):
    assert layout.repos == () and layout.sources("/x") == [("", "/x")]
    assert layout.vpath("", "a/b.md") == "a/b.md" and layout.split("a/b.md") == ("", "a/b.md")
    multi = Layout.from_toml(MULTI)
    assert multi.sources("/ignored") == [("notes", "/srv/notes.git"), ("infra", "/srv/infra")]
    assert multi.vpath("infra", "hosts/a.md") == "infra/hosts/a.md"
    assert multi.split("infra/hosts/a.md") == ("infra", "hosts/a.md")
    with pytest.raises(ValueError, match="not under a configured repo"):
        multi.split("other/x.md")


@pytest.mark.parametrize("bad", ['[[repos]]\nname = "a/b"\ndir = "/x"',
                                 '[[repos]]\nname = "a"\ndir = "/x"\n[[repos]]\nname = "a"\ndir = "/y"'])
def test_repo_names_must_be_unique_segments(bad):
    with pytest.raises(ValueError, match="unique path segments"):
        Layout.from_toml(bad)


def test_hosts_text_ignore(layout):
    assert layout.host_text_ignore == frozenset()
    assert Layout.from_toml('[hosts]\ntext_ignore = ["LDAP"]').host_text_ignore == frozenset({"ldap"})


def test_chunk_and_search_settings(layout):
    assert (layout.chunk_max_tokens, layout.search_fusion) == (1500, "hybrid")
    lay = Layout.from_toml('[chunk]\nmax_tokens = 400\n[search]\nfusion = "vector"')
    assert (lay.chunk_max_tokens, lay.search_fusion) == (400, "vector")
    with pytest.raises(ValueError, match="fusion must be"):
        Layout.from_toml('[search]\nfusion = "bm25"')


def test_history_section():
    assert (Layout.from_toml("").history_record_chunks, Layout.from_toml("").history_exclude) == (0, [])
    lay = Layout.from_toml('[history]\nrecord_chunks = 1\nexclude = ["notes/journal/handoffs/**"]\n')
    assert lay.history_record_chunks == 1
    assert lay.history_exclude[0].match("notes/journal/handoffs/2026-10-09-morning.md")
    assert not lay.history_exclude[0].match("notes/journal/2026/10/2026-10-09.md")
