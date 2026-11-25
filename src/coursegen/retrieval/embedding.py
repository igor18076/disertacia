from functools import lru_cache
from hashlib import file_digest
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from coursegen.retrieval.model_spec import DIMENSION, FILES


class RetrievalError(RuntimeError):
    """Короткий диагностический код без пользовательских данных."""


class EmbeddingProvider(Protocol):
    def encode(self, text: str) -> list[float]: ...


class Encoder:
    def __init__(self, directory: Path):
        for origin, expected in FILES.items():
            path = directory / Path(origin).name
            try:
                with path.open("rb") as stream:
                    actual = file_digest(stream, "sha256").hexdigest()
            except OSError as exc:
                raise RetrievalError("embedding_files_unavailable") from exc
            if actual != expected:
                raise RetrievalError("embedding_hash_mismatch")
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "rubert-tiny2-int8.onnx"), options,
                                            providers=["CPUExecutionProvider"])

    def encode(self, text: str) -> list[float]:
        tokens = self.tokenizer.encode(text.strip())
        values = dict(input_ids=tokens.ids, attention_mask=tokens.attention_mask,
                      token_type_ids=tokens.type_ids)
        feed = {field.name: np.array([values[field.name]], dtype=np.int64)
                for field in self.session.get_inputs()}
        hidden, = self.session.run(["last_hidden_state"], feed)
        vector = hidden[0, 0]
        norm = np.linalg.norm(vector)
        if vector.shape != (DIMENSION,) or not np.isfinite(norm) or norm == 0:
            raise RetrievalError("invalid_embedding")
        return (vector / norm).tolist()


@lru_cache(maxsize=1)
def encoder(directory: Path) -> EmbeddingProvider:
    return Encoder(directory)
