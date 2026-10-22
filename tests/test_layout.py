from pathlib import Path

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
    assert (example.entity_name, example.entity_key, example.entity_alias_fields, example.host_types) == \
        (layout.entity_name, layout.entity_key, layout.entity_alias_fields, layout.host_types)
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


def test_entities_default_is_hosts(layout):
    assert (layout.entity_name, layout.entity_plural, layout.entity_key) == ("host", "hosts", "host")
    assert layout.entity_alias_fields == ("aliases", "ips") and layout.host_types == ("host",)


def test_entities_custom():
    t = Layout.from_toml('[entities]\nname = "project"\ntypes = ["project"]\nalias_fields = ["aka"]\n'
                         'text_ignore = ["Core"]')
    assert (t.entity_name, t.entity_plural, t.entity_key) == ("project", "projects", "project")
    assert t.entity_alias_fields == ("aka",) and t.host_types == ("project",)
    assert t.host_text_ignore == frozenset({"core"})
    assert Layout.from_toml('[entities]\nname = "person"\nplural = "people"\nkey = "name"').entity_plural == "people"


def test_entities_legacy_keys():
    t = Layout.from_toml('host_types = ["server"]\n[hosts]\ntext_ignore = ["LDAP"]')
    assert t.host_types == ("server",) and t.host_text_ignore == frozenset({"ldap"}) and t.entity_name == "host"


@pytest.mark.parametrize("bad", ["Host", "my host", "1x", ""])
def test_entity_name_must_be_identifier(bad):
    with pytest.raises(ValueError):
        Layout.from_toml(f'[entities]\nname = "{bad}"')


EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*/merlin.toml"))


def test_example_layouts_exist():
    assert {p.parent.name for p in EXAMPLES} >= {"software-project", "learning"}   # not lost to an ignore rule


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.parent.name)
def test_example_layouts_parse(example):
    t = Layout.from_toml(example.read_text())
    assert t.layers and set(t.host_types) <= {ty for _, ty in t.layers}       # entity records are an indexed type
    assert set(t.history_types) <= {ty for _, ty in t.layers}
    assert set(t.weights) == {ty for _, ty in t.layers}                       # every type tells the model how to weigh it


def test_prefer_text_follows_boost(layout):
    assert "(host/runbook/memory over an old journal entry)" in layout.prefer_text()
    t = Layout.from_toml('dated_filename_types = ["journal"]\n[boost]\ntopic = 1.3\nsource = 1.2\njournal = 1.0')
    assert "(topic/source over an old journal entry)" in t.prefer_text()
    assert Layout.from_toml("[boost]\njournal = 1.0").prefer_text() == \
        "When sources disagree, prefer the newer and the more canonical, and say so."


def test_canonical_overrides_boost(layout):
    assert layout.canonical_types is None                                     # default: the boosted types
    t = Layout.from_toml('canonical = ["host", "runbook"]\nlayers = [{ glob = "j/**", type = "journal" }, '
                         '{ glob = "h/*.md", type = "host" }, { glob = "r/**", type = "runbook" }, '
                         '{ glob = "m/*.md", type = "memory" }]\n[boost]\nhost = 1.3\nrunbook = 1.3\nmemory = 1.3')
    assert "(host/runbook over an old journal entry)" in t.prefer_text()     # memory is boosted but not named
    assert t.boost["memory"] == 1.3                                           # ranking is unchanged
    none = Layout.from_toml('canonical = []\nlayers = [{ glob = "h/*.md", type = "host" }]\n[boost]\nhost = 1.3')
    assert none.prefer_text() == "When sources disagree, prefer the newer and the more canonical, and say so."
    with pytest.raises(ValueError, match="hosts"):
        Layout.from_toml('canonical = ["hosts"]\nlayers = [{ glob = "h/*.md", type = "host" }]')
