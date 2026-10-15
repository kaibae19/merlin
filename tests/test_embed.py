import json

import httpx
import pytest

from merlin.embed import (NOMIC_DOC, NOMIC_QUERY, EmbedConfig, EmbedError, Embedder, ModelMismatch,
                               check_model_id, read_key)


def test_defaults_tei_nomic():
    cfg = EmbedConfig.from_env({})
    assert (cfg.provider, cfg.model, cfg.dim) == ("tei", "nomic-ai/nomic-embed-text-v1.5", 768)
    assert cfg.url == cfg.query_url == "http://127.0.0.1:8080"
    assert cfg.model_id == "tei:nomic-ai/nomic-embed-text-v1.5:768"
    assert (cfg.prefix("document"), cfg.prefix("query")) == (NOMIC_DOC, NOMIC_QUERY)


@pytest.mark.parametrize("provider,model,prefixed", [
    ("openai", "text-embedding-3-small", False),
    ("voyage", "voyage-3.5", False),
    ("ollama", "nomic-embed-text", True),
])
def test_provider_defaults_and_prefixes(provider, model, prefixed):
    cfg = EmbedConfig.from_env({"EMBED_PROVIDER": provider.upper(), "EMBED_DIM": "1024"})
    assert cfg.model == model and cfg.dim == 1024
    assert (cfg.doc_prefix == NOMIC_DOC) is prefixed and (cfg.query_prefix == NOMIC_QUERY) is prefixed


def test_overrides():
    cfg = EmbedConfig.from_env({"EMBED_URL": "http://gpu:8081/", "EMBED_URL_QUERY": "http://cpu:8080",
                                "EMBED_DOC_PREFIX": "", "EMBED_QUERY_PREFIX": "q: ", "EMBED_MODEL": "x/nomic-y"})
    assert cfg.url == "http://gpu:8081" and cfg.query_url == "http://cpu:8080"
    assert cfg.doc_prefix == "" and cfg.query_prefix == "q: "     # explicit empty prefix is honoured


def test_bad_provider():
    with pytest.raises(ValueError):
        EmbedConfig.from_env({"EMBED_PROVIDER": "nope"})


def test_guard():
    cfg = EmbedConfig.from_env({})
    check_model_id(None, cfg)
    check_model_id(cfg.model_id, cfg)
    with pytest.raises(ModelMismatch, match=r"index was built with openai:x:768; configured tei:.*--reembed-all"):
        check_model_id("openai:x:768", cfg)


def test_key_only_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "from-env")
    assert EmbedConfig.from_env({"EMBED_API_KEY": "from-env"}).api_key_file is None
    f = tmp_path / "key"
    f.write_text("secret-key\n")
    assert read_key(str(f)) == "secret-key" and read_key(None) is None


def mock(handler):
    calls = []

    def wrapped(req: httpx.Request):
        calls.append(req)
        return handler(req)
    return httpx.Client(transport=httpx.MockTransport(wrapped)), calls


def test_tei_request_batches_and_query_url():
    cfg = EmbedConfig.from_env({"EMBED_DIM": "3", "EMBED_BATCH": "2", "EMBED_URL_QUERY": "http://q:1"})

    def h(req):
        if req.url.path == "/info":
            return httpx.Response(200, json={"model_id": cfg.model})
        return httpx.Response(200, json=[[0.1, 0.2, 0.3]] * len(json.loads(req.content)["inputs"]))
    client, calls = mock(h)
    out = Embedder(cfg, "document", client).embed(["a", "b", "c"])
    assert out == ["[0.1,0.2,0.3]"] * 3
    bodies = [json.loads(c.content) for c in calls if c.url.path == "/embed"]
    assert [b["inputs"] for b in bodies] == [["a", "b"], ["c"]] and bodies[0]["truncate"] is True
    q = Embedder(cfg, "query", client)
    q.embed_one("hello")
    assert str(calls[-1].url) == "http://q:1/embed" and json.loads(calls[-1].content)["inputs"] == [NOMIC_QUERY + "hello"]


def test_tei_wrong_model_refused():
    client, _ = mock(lambda req: httpx.Response(200, json={"model_id": "other/model"}))
    with pytest.raises(ModelMismatch):
        Embedder(EmbedConfig.from_env({}), "document", client).check()


def test_unreachable_is_embed_error():
    def h(req):
        raise httpx.ConnectError("refused")
    client, _ = mock(h)
    with pytest.raises(EmbedError, match="unreachable"):
        Embedder(EmbedConfig.from_env({}), "document", client).embed(["x"])


def test_dim_mismatch_is_embed_error():
    client, _ = mock(lambda req: httpx.Response(200, json={"model_id": "nomic-ai/nomic-embed-text-v1.5"})
                     if req.url.path == "/info" else httpx.Response(200, json=[[0.1, 0.2]]))
    with pytest.raises(EmbedError, match="dim 768"):
        Embedder(EmbedConfig.from_env({}), "document", client).embed(["x"])


def test_voyage_input_type_and_auth(tmp_path):
    key = tmp_path / "k"
    key.write_text("vk")
    cfg = EmbedConfig.from_env({"EMBED_PROVIDER": "voyage", "EMBED_DIM": "2", "EMBED_API_KEY_FILE": str(key)})
    client, calls = mock(lambda req: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]}))
    Embedder(cfg, "query", client).embed_one("hi")
    body = json.loads(calls[-1].content)
    assert body["input_type"] == "query" and body["input"] == ["hi"] and body["output_dimension"] == 2
    assert calls[-1].headers["authorization"] == "Bearer vk"


def test_openai_and_ollama_shapes():
    cfg = EmbedConfig.from_env({"EMBED_PROVIDER": "openai", "EMBED_DIM": "2", "EMBED_URL": "http://local/v1"})
    client, calls = mock(lambda req: httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]},
                                                                        {"index": 0, "embedding": [1, 0]}]}))
    assert Embedder(cfg, "document", client).embed(["a", "b"]) == ["[1,0]", "[0,1]"]
    assert json.loads(calls[-1].content)["dimensions"] == 2 and str(calls[-1].url) == "http://local/v1/embeddings"
    cfg = EmbedConfig.from_env({"EMBED_PROVIDER": "ollama", "EMBED_DIM": "2"})
    client, calls = mock(lambda req: httpx.Response(200, json={"embeddings": [[0.5, 0.5]]}))
    Embedder(cfg, "document", client).embed(["a"])
    assert calls[-1].url.path == "/api/embed" and json.loads(calls[-1].content)["model"] == "nomic-embed-text"
