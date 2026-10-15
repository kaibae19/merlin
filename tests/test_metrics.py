from merlin.metrics import render


def test_render_matches_v1_series():
    body = render(1700000000.4, {"infra": 1699999000}, {"infra": False, "notes": True}, {"infra": (3, 12)})
    assert body.splitlines() == [
        "# HELP merlin_index_last_run_timestamp_seconds Last successful ingest run.",
        "# TYPE merlin_index_last_run_timestamp_seconds gauge",
        "merlin_index_last_run_timestamp_seconds 1700000000",
        'merlin_index_repo_indexed_timestamp_seconds{repo="infra"} 1699999000',
        'merlin_index_repo_behind_mirror{repo="infra"} 0',
        'merlin_index_repo_documents{repo="infra"} 3',
        'merlin_index_repo_chunks{repo="infra"} 12',
        'merlin_index_repo_indexed_timestamp_seconds{repo="notes"} 0',     # configured but empty: still reported
        'merlin_index_repo_behind_mirror{repo="notes"} 1',
        'merlin_index_repo_documents{repo="notes"} 0',
        'merlin_index_repo_chunks{repo="notes"} 0',
    ]


def test_render_single_repo_label():
    assert 'merlin_index_repo_behind_mirror{repo="repo"} 0' in render(1, {"": 1}, {"": False}, {"": (1, 1)})
