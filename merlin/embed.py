"""Pluggable embedding backends (TEI, OpenAI-compatible, Voyage, Ollama) and the model-identity guard.

Vectors travel as pgvector text literals ("[0.1,0.2,...]") so they can be stored and reused without parsing.
The API key is only ever read from the file named by EMBED_API_KEY_FILE and is never logged.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import httpx

Kind = Literal["document", "query"]
NOMIC_DOC, NOMIC_QUERY = "search_document: ", "search_query: "
DEFAULTS = {  # provider -> (default url, default model)
    "tei": ("http://127.0.0.1:8080", "nomic-ai/nomic-embed-text-v1.5"),
    "ollama": ("http://127.0.0.1:11434", "nomic-embed-text"),
    "openai": ("https://api.openai.com/v1", "text-embedding-3-small"),
    "voyage": ("https://api.voyageai.com/v1", "voyage-3.5"),
}


class EmbedError(RuntimeError):
    """The embedding backend failed or returned something unusable."""


class ModelMismatch(RuntimeError):
    """The index was built with a different embedding model than the one configured."""


@dataclass(frozen=True)
class EmbedConfig:
    provider: str
    model: str
    dim: int
    url: str
    query_url: str
    api_key_file: str | None
    doc_prefix: str
    query_prefix: str
    batch: int = 16

    @property
    def model_id(self) -> str:
        """Identity stored in meta; vectors from different ids must never be mixed."""
        return f"{self.provider}:{self.model}:{self.dim}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "EmbedConfig":
        """Parse EMBED_* variables. Unset prefixes default to nomic's for nomic models on tei/ollama."""
        provider = env.get("EMBED_PROVIDER", "tei").strip().lower()
        if provider not in DEFAULTS:
            raise ValueError(f"EMBED_PROVIDER={provider!r}; expected one of {', '.join(DEFAULTS)}")
        url, model = DEFAULTS[provider]
        url = env.get("EMBED_URL") or url
        model = env.get("EMBED_MODEL") or model
        nomic = provider in ("tei", "ollama") and "nomic" in model.lower()
        doc_p, query_p = env.get("EMBED_DOC_PREFIX"), env.get("EMBED_QUERY_PREFIX")
        return cls(
            provider=provider, model=model, dim=int(env.get("EMBED_DIM", "768")),
            url=url.rstrip("/"), query_url=(env.get("EMBED_URL_QUERY") or url).rstrip("/"),
            api_key_file=env.get("EMBED_API_KEY_FILE") or None,
            doc_prefix=doc_p if doc_p is not None else (NOMIC_DOC if nomic else ""),
            query_prefix=query_p if query_p is not None else (NOMIC_QUERY if nomic else ""),
            batch=int(env.get("EMBED_BATCH", "16")),
        )

    def prefix(self, kind: Kind) -> str:
        """The text prefix for documents or queries."""
        return self.doc_prefix if kind == "document" else self.query_prefix


def check_model_id(stored: str | None, cfg: EmbedConfig) -> None:
    """Raise ModelMismatch if the index's stored model id differs from the configured one."""
    if stored and stored != cfg.model_id:
        raise ModelMismatch(f"index was built with {stored}; configured {cfg.model_id}; "
                            "run `ingest --reembed-all` to rebuild it with the configured model")


def read_key(path: str | None) -> str | None:
    """Read the API key from a file (never from an env var)."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return f.read().strip() or None


# --- provider adapters: (client, url, model, dim, key, texts, kind) -> list of float lists -------------------

def _tei(c: httpx.Client, url: str, model: str, dim: int, key: str | None, texts: list[str], kind: Kind):
    r = c.post(f"{url}/embed", json={"inputs": texts, "normalize": True, "truncate": True},
               headers={"Authorization": f"Bearer {key}"} if key else {})
    r.raise_for_status()
    return r.json()


def _openai(c: httpx.Client, url: str, model: str, dim: int, key: str | None, texts: list[str], kind: Kind):
    body: dict = {"model": model, "input": texts}
    if model.startswith("text-embedding-3"):
        body["dimensions"] = dim
    r = c.post(f"{url}/embeddings", json=body, headers={"Authorization": f"Bearer {key}"} if key else {})
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def _voyage(c: httpx.Client, url: str, model: str, dim: int, key: str | None, texts: list[str], kind: Kind):
    if not key:
        raise EmbedError("voyage needs EMBED_API_KEY_FILE")
    r = c.post(f"{url}/embeddings", headers={"Authorization": f"Bearer {key}"},
               json={"model": model, "input": texts, "input_type": kind, "output_dimension": dim})
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def _ollama(c: httpx.Client, url: str, model: str, dim: int, key: str | None, texts: list[str], kind: Kind):
    r = c.post(f"{url}/api/embed", json={"model": model, "input": texts, "truncate": True})
    r.raise_for_status()
    return r.json()["embeddings"]


ADAPTERS: dict[str, Callable] = {"tei": _tei, "openai": _openai, "voyage": _voyage, "ollama": _ollama}


def to_literal(vec: list[float]) -> str:
    """Format a vector as a pgvector text literal."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


class Embedder:
    """Embeds documents or queries against the backend configured for that kind."""

    def __init__(self, cfg: EmbedConfig, kind: Kind, client: httpx.Client | None = None):
        self.cfg, self.kind = cfg, kind
        self.url = cfg.query_url if kind == "query" else cfg.url
        self.client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))
        self._key = read_key(cfg.api_key_file)
        self._checked = False

    def check(self) -> None:
        """Verify reachability and model identity (TEI reports its model id on /info)."""
        try:
            if self.cfg.provider == "tei":
                r = self.client.get(f"{self.url}/info")
                r.raise_for_status()
                served = r.json().get("model_id")
                if served and served != self.cfg.model:
                    raise ModelMismatch(f"TEI at {self.url} serves {served}; configured {self.cfg.model}")
        except httpx.HTTPError as e:
            raise EmbedError(f"{self.kind} embedding backend {self.url} unreachable: {e}") from e
        self._checked = True

    def embed(self, texts: list[str]) -> list[str]:
        """Embed texts (prefix already applied) in batches; returns pgvector literals."""
        if not self._checked:
            self.check()
        out: list[str] = []
        adapter = ADAPTERS[self.cfg.provider]
        for i in range(0, len(texts), self.cfg.batch):
            batch = texts[i:i + self.cfg.batch]
            try:
                vecs = adapter(self.client, self.url, self.cfg.model, self.cfg.dim, self._key, batch, self.kind)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                raise EmbedError(f"{self.cfg.provider} at {self.url} failed: {type(e).__name__}: {e}") from e
            if len(vecs) != len(batch) or any(len(v) != self.cfg.dim for v in vecs):
                got = {len(v) for v in vecs}
                raise EmbedError(f"expected {len(batch)} vectors of dim {self.cfg.dim}, got {len(vecs)} of {got}")
            out.extend(to_literal(v) for v in vecs)
        return out

    def embed_one(self, text: str) -> str:
        """Embed a single text with this kind's prefix applied."""
        return self.embed([self.cfg.prefix(self.kind) + text])[0]
