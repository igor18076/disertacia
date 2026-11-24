import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import httpx2
import pymupdf
import pytest


@pytest.fixture
def api():
    with httpx2.Client(base_url=os.environ.get("API_BASE_URL", "http://api:8000") + "/api/v1/documents",
                       trust_env=False, timeout=15) as client:
        yield client


def upload(api, name, content, media, **extra):
    response = api.post(str(api.base_url).rstrip("/"), params={"filename": name, "language": "ru", **extra},
                        content=content, headers={"Content-Type": media})
    assert response.status_code == 202, response.text
    return response.json()


def processed(api, row):
    deadline = time.monotonic() + max(30, 2 * float(os.environ.get("WORKER_POLL_SECONDS", 1)) + 5)
    params = {"document_version": row["document_version"]}
    while time.monotonic() < deadline:
        r = api.get(row["document_id"], params=params)
        r.raise_for_status()
        state = r.json()
        if state["status"] != "queued":
            return state
        time.sleep(0.15)
    pytest.fail("Worker не завершил обработку тестовой версии за время ожидания")


def test_versioned_corpus(api):
    source = Path("datasets/synthetic/stage2.md").read_bytes()
    initial = upload(api, "stage2.md", source, "text/markdown", source_type="manual")
    row = processed(api, initial)
    doc_id = row["document_id"]
    v1 = {"document_version": 1}
    assert row["status"] == "ready" and row["content_hash"] == hashlib.sha256(source).hexdigest()
    assert row["source_meta"]["source_type"] == "manual"
    assert api.get(doc_id + "/original", params=v1).content == source
    def chunks():
        r = api.get(doc_id + "/chunks", params=v1)
        r.raise_for_status()
        return r.json()
    before = chunks()
    quote = next(c for c in before if "37" in c["text"])
    verify = api.get(f"{doc_id}/chunks/{quote['chunk_id']}/verify", params=v1)
    assert verify.json()["verified"] is True
    page = api.get(f"{doc_id}/pages/{quote['page_start']}", params=v1).json()
    assert page["text"][quote["char_start"]:quote["char_end"]] == quote["text"]
    duplicate = upload(api, "stage2.md", source, "text/markdown", source_type="manual")
    assert (duplicate["document_id"], duplicate["document_version"]) == (doc_id, 1)
    assert api.post(doc_id + "/reprocess", params=v1).status_code == 202
    assert processed(api, row)["status"] == "ready"
    assert chunks() == before
    updated = upload(api, "stage2.md", source + b"\nRevision 2.", "text/markdown",
                     document_id=doc_id, source_type="manual")
    assert updated["document_version"] == 2
    assert processed(api, updated)["status"] == "ready"
    assert api.get(doc_id).json()["document_version"] == 2
    assert api.get(doc_id + "/original", params=v1).content == source
    listing = api.get(str(api.base_url).rstrip("/"), params={"limit": 1})
    assert listing.status_code == 200 and len(listing.json()) == 1


def test_pdf_txt_and_failure_states(api):
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Reference value 37.")
        pdf.new_page()
        data = pdf.tobytes(no_new_id=True)
    row = processed(api, upload(api, "sample.pdf", data, "application/pdf"))
    assert row["status"] == "partial" and row["page_count"] == 2
    assert api.get(row["document_id"] + "/pages/2").json()["status"] == "needs_ocr"
    with pymupdf.open() as scan:
        image = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 8, 8), False)
        image.clear_with(128)
        scan.new_page().insert_image(pymupdf.Rect(40, 40, 80, 80), pixmap=image)
        assert processed(api, upload(api, "scan.pdf", scan.tobytes(no_new_id=True),
                                     "application/pdf"))["status"] == "needs_ocr"
    txt = processed(api, upload(api, "two.txt", b"page one\fpage two", "text/plain"))
    assert txt["page_count"] == 2 and txt["status"] == "ready"
    failed = processed(api, upload(api, "broken.pdf", b"%PDF-broken", "application/pdf"))
    assert failed["status"] == "failed" and failed["error_code"] == "invalid_pdf"
    assert api.get(failed["document_id"] + "/chunks").status_code == 409
    assert api.get("00000000-0000-0000-0000-000000000000").status_code == 404
    assert api.post(str(api.base_url).rstrip("/"), params={"filename": "bad.exe"}, content=b"abc",
                    headers={"Content-Type": "application/octet-stream"}).status_code == 415


def test_concurrent_version_registration(api):
    initial = upload(api, "parallel.txt", b"First edition", "text/plain")
    doc_id = initial["document_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        def submit(_):
            return upload(api, "parallel.txt", b"Second edition", "text/plain", document_id=doc_id)
        results = list(pool.map(submit, range(4)))
    assert {r["document_version"] for r in results} == {2}
    assert processed(api, results[0])["chunk_count"] == 1
