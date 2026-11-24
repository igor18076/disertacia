"""Детерминированные окна по словам и знакам, с координатами в тексте страницы."""

from bisect import bisect_right
from hashlib import sha256
import re
from uuid import UUID, uuid5

from coursegen.documents.extraction import ExtractionError, Page


TOKEN = re.compile(r"\w+|[^\w\s]")


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def build_chunks(pages: list[Page], document_id: UUID, version: int, *,
                 strategy: str, size: int, overlap: float) -> list[dict]:
    if strategy not in {"fixed", "section", "hierarchical"} or size < 2 or not 0 <= overlap <= 0.5:
        raise ValueError("invalid_chunk_config")
    result = []
    ancestry: list[tuple[int, str]] = []

    def emit(page: Page, matches: list[re.Match[str]], start: int, stop: int,
             path: list[str], role: str, parent: UUID | None) -> UUID:
        a, b = matches[start].start(), matches[stop - 1].end()
        text = page.text[a:b]
        key = f"v1:{version}:{strategy}:{size}:{overlap}:{page.number}:{a}:{b}:{role}:{parent}"
        chunk_id = uuid5(document_id, key)
        if len(result) >= 100_000:
            raise ExtractionError("chunk_limit")
        result.append(dict(chunk_id=chunk_id, parent_id=parent, role=role, ordinal=len(result),
                           page_start=page.number, page_end=page.number, char_start=a, char_end=b,
                           section_path=list(path), text=text, content_hash=digest(text),
                           token_count=stop - start, extraction_status=page.status, embedding_model_version=None))
        return chunk_id

    for page in pages:
        boundaries = {0: [name for _, name in ancestry]}
        for offset, level, title in page.headings:
            ancestry = [entry for entry in ancestry if entry[0] < level]
            ancestry.append((level, title))
            boundaries[offset] = [name for _, name in ancestry]
        section_starts = sorted(boundaries)
        starts = section_starts if strategy != "fixed" else [0]
        for a, b in zip(starts, [*starts[1:], len(page.text)], strict=True):
            matches = list(TOKEN.finditer(page.text, a, b))
            step = size - int(size * overlap)
            for i in range(0, len(matches), step):
                end = min(len(matches), i + size)
                path = boundaries[section_starts[bisect_right(section_starts, matches[i].start()) - 1]]
                if strategy == "hierarchical" and end - i > size // 2:
                    parent = emit(page, matches, i, end, path, "parent", None)
                    child_size = size // 2
                    for j in range(i, end, child_size - int(child_size * overlap)):
                        stop = min(end, j + child_size)
                        emit(page, matches, j, stop, path, "leaf", parent)
                        if stop == end:
                            break
                else:
                    emit(page, matches, i, end, path, "leaf", None)
                if end == len(matches):
                    break
    return result
