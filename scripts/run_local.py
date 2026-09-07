"""End-to-end runner against a running API: upload PDFs, drive processing and linking, export.

Usage:
    python scripts/run_local.py [--base http://127.0.0.1:8765] [--out samples/export.json] PDF [PDF ...]

Each PDF is hashed, ticketed, uploaded (via the API's own upload route or a signed storage URL),
finalized and processed in time-boxed batches until no page is pending. Linking then runs until no
pair awaits adjudication. Finally the whole layer is exported to JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def upload(client: httpx.Client, base: str, pdf: Path) -> str:
    data = pdf.read_bytes()
    ticket = client.post(
        f"{base}/documents",
        json={
            "filename": pdf.name,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    ticket.raise_for_status()
    body = ticket.json()
    document_id: str = body["document_id"]
    if body["existing"]:
        log(f"{pdf.name}: already in the layer as {document_id}")
        return document_id
    target = body["upload"]
    if target["method"] == "PUT":
        headers = {"Content-Type": "application/pdf"}
        if target.get("token"):
            headers["Authorization"] = f"Bearer {target['token']}"
        client.put(target["url"], content=data, headers=headers).raise_for_status()
    else:
        url = target["url"] if target["url"].startswith("http") else base + target["url"]
        client.post(url, files={"file": (pdf.name, data, "application/pdf")}).raise_for_status()
    finalized = client.post(f"{base}/documents/{document_id}/finalize")
    finalized.raise_for_status()
    log(f"{pdf.name}: uploaded as {document_id}, {finalized.json()['page_count']} pages")
    return document_id


def process(client: httpx.Client, base: str, document_id: str) -> dict[str, Any]:
    while True:
        response = client.post(f"{base}/documents/{document_id}/process", timeout=600)
        response.raise_for_status()
        progress: dict[str, Any] = response.json()
        log(
            f"{document_id[:8]}: {progress['done']} done, {progress['failed']} failed, "
            f"{progress['skipped']} skipped, {progress['pending']} pending "
            f"(~{progress['estimated_calls_remaining']} calls left)"
        )
        if progress["last_errors"]:
            log(f"  last error: {progress['last_errors'][-1][:160]}")
        if progress["pending"] == 0 or progress["processed_this_call"] == 0:
            return progress


def link(client: httpx.Client, base: str) -> dict[str, Any]:
    while True:
        response = client.post(f"{base}/link", timeout=600)
        response.raise_for_status()
        progress: dict[str, Any] = response.json()
        log(
            f"link: {progress['total_relations']} relations, {progress['final']} final, "
            f"{progress['pending_llm']} pending, {progress['failed']} failed; "
            f"verdicts {progress['by_verdict']}"
        )
        if progress["pending_llm"] == 0 or progress["adjudicated_this_call"] == 0:
            return progress


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--out", type=Path, default=Path("samples/export.json"))
    parser.add_argument("--skip-link", action="store_true")
    args = parser.parse_args(argv)

    with httpx.Client(timeout=120) as client:
        health = client.get(f"{args.base}/health").json()
        log(f"server: {health}")
        for pdf in args.pdfs:
            document_id = upload(client, args.base, pdf)
            process(client, args.base, document_id)
        if not args.skip_link:
            link(client, args.base)
        export = client.get(f"{args.base}/export", timeout=600)
        export.raise_for_status()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(export.json(), indent=1, ensure_ascii=False), encoding="utf-8"
        )
        body = export.json()
        log(
            f"exported {len(body['facts'])} facts, {len(body['relations'])} relations, "
            f"{len(body['failures'])} failures to {args.out}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
