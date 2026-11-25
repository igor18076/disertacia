from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from coursegen.retrieval.embedding import Encoder, RetrievalError
from coursegen.retrieval.index import check_collection
from coursegen.retrieval.search import SearchRequest


def test_embedding_assets_are_verified_before_loading(tmp_path: Path):
    with pytest.raises(RetrievalError, match="embedding_files_unavailable"):
        Encoder(tmp_path)
    (tmp_path / "rubert-tiny2-int8.onnx").write_bytes(b"wrong model")
    with pytest.raises(RetrievalError, match="embedding_hash_mismatch"):
        Encoder(tmp_path)


def test_foreign_collection_is_rejected_without_writes():
    client = Mock()
    client.collection_exists.return_value = True
    client.get_collection.return_value.config = SimpleNamespace(
        metadata={"embedding": "other"}, params=SimpleNamespace(vectors={}, sparse_vectors={}))
    with pytest.raises(RetrievalError, match="collection_schema_mismatch"):
        check_collection(client, create=True)
    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"query": "  "}, {"query": "x", "top_k": 0}, {"query": "x", "mode": "invalid"},
    {"query": "x", "dense_candidates": 0}, {"query": "x", "filters": {"allowed_ids": []}},
])
def test_invalid_search_request(payload):
    with pytest.raises(ValidationError):
        SearchRequest(**payload)
