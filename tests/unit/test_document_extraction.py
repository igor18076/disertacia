from uuid import UUID

import pymupdf
import pytest
from fastapi.testclient import TestClient

from coursegen.api.main import app
from coursegen.chunking import build_chunks, digest
from coursegen.documents.extraction import ExtractionError, extract_document


def extract(data, fmt, **limits):
    return extract_document(data, fmt, max_pages=limits.get("pages", 20), max_chars=limits.get("chars", 2000))


def pdf_with_pages(*strings):
    with pymupdf.open() as pdf:
        for text in strings:
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text)
        return pdf.tobytes()


@pytest.mark.parametrize("strategy", ["fixed", "section", "hierarchical"])
def test_chunk_spans_and_identity(strategy):
    page1 = "# Введение\r\n" + "Фрагмент 1, " * 12
    doc = extract((page1 + "\f## Измерение\nЗначение 37.").encode(), "md")
    args = dict(strategy=strategy, size=8, overlap=0.25)
    chunks = build_chunks(doc, UUID(int=1), 2, **args)
    assert chunks == build_chunks(doc, UUID(int=1), 2, **args)
    assert len({c["chunk_id"] for c in chunks}) == len(chunks)
    for chunk in chunks:
        page = doc[chunk["page_start"] - 1]
        assert chunk["text"] == page.text[chunk["char_start"]:chunk["char_end"]]
        assert digest(chunk["text"]) == chunk["content_hash"]
        assert 0 < chunk["token_count"] <= 8
    assert chunks[-1]["page_start"] == chunks[-1]["page_end"] == 2
    assert chunks[-1]["section_path"] == ["Введение", "Измерение"]
    if strategy == "hierarchical":
        parents = {c["chunk_id"]: c for c in chunks if c["role"] == "parent"}
        assert parents
        for c in chunks:
            if c["parent_id"]:
                assert c["text"] in parents[c["parent_id"]]["text"]


def test_section_boundary_and_code_fence():
    pages = extract(b"# A\n~~~python\n# code\n~~~\n## B\nvalue\n", "md")
    sections = build_chunks(pages, UUID(int=2), 1, strategy="section", size=100, overlap=0)
    assert [s["section_path"] for s in sections] == [["A"], ["A", "B"]]
    assert "# code" in sections[0]["text"]
    assert "code_layout" in pages[0].warnings


def test_layout_hints_and_preservation_of_hash_in_heading():
    page = extract("# Язык C#\n|A|B|\n$$x=2$$".encode(), "md")[0]
    assert page.headings[0][2] == "Язык C#"
    assert {"table_layout", "formula_layout"} <= set(page.warnings)


def test_pdf_layer_page_numbers_boxes_and_ocr():
    pages = extract(pdf_with_pages("Value 21", "", "Value 37"), "pdf")
    assert [p.number for p in pages] == [1, 2, 3]
    assert pages[1].status == "needs_ocr" and not pages[1].text
    box = pages[2].blocks[0]
    assert len(box["bbox"]) == 4
    assert pages[2].text[box["start"]:box["end"]] == "Value 37"


@pytest.mark.parametrize(("data", "fmt", "code"), [
    (b"%PDF-broken", "pdf", "invalid_pdf"), (b"\xff\xfe", "txt", "invalid_utf8"),
    (b"data\0", "md", "binary_text"), (b"   ", "txt", "empty_text"),
])
def test_invalid_inputs(data, fmt, code):
    with pytest.raises(ExtractionError, match=code):
        extract(data, fmt)


def test_limits_apply_to_extracted_content():
    with pytest.raises(ExtractionError, match="page_limit"):
        extract(pdf_with_pages("one", "two"), "pdf", pages=1)
    with pytest.raises(ExtractionError, match="extraction_limit"):
        extract(b"abcde", "txt", chars=4)


def test_upload_limits_before_registration(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only")
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "8")
    with TestClient(app) as client:
        path = "/api/v1/documents"
        opts = {"params": {"filename": "a.txt"}, "headers": {"Content-Type": "text/plain"}}
        assert client.post(path, content=b"x" * 9, **opts).status_code == 413
        assert client.post(path, content=iter([b"12345", b"6789"]), **opts).status_code == 413
        assert client.post(path, content=b"", **opts).status_code == 422
        assert client.post(path, content=b"%PDF-1", **opts).status_code == 415
        assert client.post(path, params={"filename": "../x.txt"}, content=b"x",
                           headers=opts["headers"]).status_code == 422
