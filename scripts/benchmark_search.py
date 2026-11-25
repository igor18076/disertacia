"""Сравнить три режима на размеченном наборе коротких учебных документов."""

import json
import math
import os
import statistics
import time
from pathlib import Path

import httpx2

CORPUS = Path(__file__).resolve().parents[1] / "datasets/synthetic/search-v1.json"
TOP_K = 3


def wait_for(client: httpx2.Client, path: str, target: str) -> dict:
    deadline = time.monotonic() + max(60, 3 * float(os.environ.get("WORKER_POLL_SECONDS", 1)))
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        row = response.json()
        if row["status"] == target:
            return row
        if row["status"] in ("failed", "remove_failed", "needs_ocr"):
            raise RuntimeError(row.get("error_code") or row["status"])
        time.sleep(0.2)
    raise TimeoutError(f"Истекло время ожидания статуса {target}")


def compare(client: httpx2.Client) -> dict:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    documents = {}
    for slug, text in corpus["documents"].items():
        uploaded = client.post("documents", params=dict(filename=f"search-v1-{slug}.txt", language="ru",
                               source_type="manual", topic="search-v1", currency="current"),
                               headers={"Content-Type": "text/plain"}, content=text.encode())
        uploaded.raise_for_status()
        doc_id = uploaded.json()["document_id"]
        documents[doc_id] = slug
        path = f"documents/{doc_id}"
        wait_for(client, path, "ready")
        client.post(path + "/index").raise_for_status()
        wait_for(client, path + "/index", "indexed")

    report = {}
    for mode in ("bm25", "dense", "hybrid"):
        parameters = dict(mode=mode, top_k=TOP_K, filters={"allowed_document_ids": list(documents)},
                          bm25_candidates=10, dense_candidates=10)
        client.post("search", json=dict(query="тест", **parameters)).raise_for_status()
        rows, latency = [], []
        for case in corpus["queries"]:
            start = time.perf_counter()
            response = client.post("search", json=dict(query=case["text"], **parameters))
            response.raise_for_status()
            latency.append((time.perf_counter() - start) * 1000)
            sources = dict.fromkeys(documents[hit["document_id"]] for hit in response.json())
            rank = next((i for i, slug in enumerate(sources, 1) if slug == case["relevant"]), None)
            rows.append({"query": case["text"], "relevant": case["relevant"], "rank": rank})
        ranks = [r["rank"] for r in rows if r["rank"]]
        n = len(rows)
        report[mode] = {
            "recall_at_3": len(ranks) / n, "precision_at_3": len(ranks) / (TOP_K * n),
            "mrr_at_3": sum(1 / r for r in ranks) / n,
            "ndcg_at_3": sum(1 / math.log2(1 + r) for r in ranks) / n,
            "latency_ms_p50": round(statistics.median(latency), 2),
            "latency_ms_p95": round(sorted(latency)[math.ceil(0.95 * n) - 1], 2), "queries": rows,
        }
    return report


if __name__ == "__main__":
    base = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    with httpx2.Client(base_url=base + "/api/v1/", timeout=60, trust_env=False) as api:
        print(json.dumps(compare(api), ensure_ascii=False, indent=2))
