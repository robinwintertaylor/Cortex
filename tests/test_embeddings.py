"""Embed model selection: fastembed 0.8 dropped bge-m3 from TextEmbedding."""

from cortex.embeddings import _resolve_embed_model


class _FakeTextEmbedding:
    @staticmethod
    def list_supported_models():
        return [{"model": "BAAI/bge-large-en-v1.5"}, {"model": "BAAI/bge-base-en-v1.5"}]


def test_resolve_keeps_supported_model(monkeypatch):
    monkeypatch.setattr(
        "cortex.embeddings.get_config",
        lambda: type("C", (), {"embed_model": "BAAI/bge-large-en-v1.5", "embed_dim": 1024})(),
    )
    assert _resolve_embed_model(_FakeTextEmbedding) == "BAAI/bge-large-en-v1.5"


def test_resolve_falls_back_to_1024d_dense_model(monkeypatch):
    monkeypatch.setattr(
        "cortex.embeddings.get_config",
        lambda: type("C", (), {"embed_model": "BAAI/bge-m3", "embed_dim": 1024})(),
    )
    assert _resolve_embed_model(_FakeTextEmbedding) == "BAAI/bge-large-en-v1.5"
