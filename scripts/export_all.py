#!/usr/bin/env python3
"""Export the entire public Zakovat klubi question bank to JSON.

The site itself is a React app; its open API is used directly:

  GET https://api.zakovatklubi.uz/v1/package?per-page=100&page=1
  GET https://api.zakovatklubi.uz/v1/package/{id}?include=questions

No login, BotFather token or browser is needed.  Results are written as
sharded JSON files so a full dump (tens of thousands of questions) stays
Git-friendly.

Example:
  python3 scripts/export_all.py --out-dir data/all
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.zakovatklubi.uz/v1"
USER_AGENT = "zakovat-full-export/1.0"
SHARD_SIZE = 5000  # questions per JSON file


def api_get(path: str, params: dict | None = None, retries: int = 4) -> dict:
    url = f"{API_ROOT}{path}"
    if params:
        url += "?" + urlencode(params)
    last_error = None
    for attempt in range(retries):
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": USER_AGENT,
                },
            )
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if str(payload.get("code", "1")) != "1" and payload.get("message") != "success":
                raise RuntimeError(f"API error at {url}: {payload}")
            return payload.get("data", payload)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def strip_html(value: object) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def attachment_urls(items: object) -> list[str]:
    urls = []
    for item in items or []:
        if isinstance(item, dict) and item.get("src"):
            urls.append(str(item["src"]))
    return urls


def extract_questions(package: dict) -> list[dict]:
    raw = package.get("questions") or package.get("items") or []
    if not isinstance(raw, list):
        return []

    questions = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sub_items = entry.get("items") or [entry]
        for item in sub_items:
            if not isinstance(item, dict):
                continue
            questions.append(
                {
                    "id": item.get("id"),
                    "question_id": item.get("question_id") or entry.get("id"),
                    "package_id": package.get("id"),
                    "package_title": strip_html(package.get("title")),
                    "text": strip_html(item.get("text")),
                    "answer": strip_html(item.get("answer")),
                    "comment": strip_html(item.get("comment")),
                    "type": item.get("type"),
                    "answer_type": item.get("answer_type"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "attachments": attachment_urls(item.get("attachments")),
                    "answer_attachments": attachment_urls(item.get("answerAttachments")),
                }
            )
    return questions


def write_shard(out_dir: Path, shard_no: int, questions: list[dict]) -> None:
    path = out_dir / "questions" / f"questions-{shard_no:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(questions, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"  wrote {path.name}: {len(questions)} questions", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data/all")
    parser.add_argument("--sleep", type=float, default=0.1, help="Delay between API calls")
    parser.add_argument("--limit-packages", type=int, default=0, help="Debug: only N packages")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching package list...", flush=True)
    first = api_get("/package", {"per-page": 100, "page": 1})
    packages = list(first.get("items") or first.get("packages") or [])
    meta = first.get("meta") or {}
    page_count = int(meta.get("pageCount") or 1)
    total_packages = int(meta.get("totalCount") or len(packages))

    for page in range(2, page_count + 1):
        data = api_get("/package", {"per-page": 100, "page": page})
        packages.extend(data.get("items") or data.get("packages") or [])
        print(f"  package page {page}/{page_count}", flush=True)
        time.sleep(args.sleep)

    if args.limit_packages:
        packages = packages[: args.limit_packages]

    print(f"Found {len(packages)} packages (API total: {total_packages})", flush=True)

    manifest = {
        "source": "https://zakovatklubi.uz",
        "api_root": API_ROOT,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "package_count": len(packages),
        "question_count": 0,
        "shards": [],
        "packages": [],
    }
    buffer: list[dict] = []
    shard_no = 1

    for index, package in enumerate(packages, start=1):
        package_id = package.get("id")
        if not package_id:
            continue
        full = api_get(
            f"/package/{package_id}",
            {"include": "questions,items"},
        )
        questions = extract_questions(full)

        manifest["packages"].append(
            {
                "id": package_id,
                "title": strip_html(full.get("title") or package.get("title")),
                "questions": len(questions),
            }
        )
        manifest["question_count"] += len(questions)
        buffer.extend(questions)

        while len(buffer) >= SHARD_SIZE:
            write_shard(out_dir, shard_no, buffer[:SHARD_SIZE])
            manifest["shards"].append(f"questions/questions-{shard_no:04d}.json")
            buffer = buffer[SHARD_SIZE:]
            shard_no += 1

        if index % 50 == 0:
            print(
                f"  {index}/{len(packages)} packages, {manifest['question_count']} questions",
                flush=True,
            )
        time.sleep(args.sleep)

    if buffer:
        write_shard(out_dir, shard_no, buffer)
        manifest["shards"].append(f"questions/questions-{shard_no:04d}.json")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Done: {manifest['question_count']} questions from "
        f"{manifest['package_count']} packages -> {out_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
