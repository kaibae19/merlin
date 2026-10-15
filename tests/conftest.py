"""Shared fixtures: the synthetic corpus and a host matcher built from it."""
from pathlib import Path

import pytest

from merlin.chunk import HostMatcher
from merlin.layout import DEFAULT_TOML, Layout

CORPUS = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture(scope="session")
def layout() -> Layout:
    return Layout.from_toml(DEFAULT_TOML)


@pytest.fixture(scope="session")
def matcher() -> HostMatcher:
    return HostMatcher.from_host_docs({str(p): p.read_text() for p in (CORPUS / "hosts").glob("*.md")})


def read(rel: str) -> str:
    return (CORPUS / rel).read_text()
