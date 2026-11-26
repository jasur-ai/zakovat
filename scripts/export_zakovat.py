#!/usr/bin/env python3
"""Export one Zakovat klubi quiz package (questions + answers) to JSON.

Example:
    python3 scripts/export_zakovat.py \
        --url https://zakovatklubi.uz/quiz/5/10/959/0/0 \
        --out data/zakovat-959.json \
        --flat-out data/zakovat-959.flat.json
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.zakovatklubi.uz/v1"
QUIZ_RE = re.compile(r"/quiz/(\d+)/(\d+)/(\d+)")


def parse_quiz_url(url: str) -> tuple[int, int, int]:
    match = QUIZ_RE.search(url)
    if not match:
        raise ValueError(
            f"Not a Zakovat quiz URL: {url!r}. "
            "Expected https://zakovatklubi.uz/quiz/<rounds>/<per-round>/<package-id>/..."
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def api_get(path: str, params: dict | None = None) -> dict:
    url = f"{API_ROOT}{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "zakovat-json-export/1.0",
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("code", "1")) != "1" and payload.get("message") != "success":
        raise RuntimeError(f"API error for {url}: {payload}")
    return payload.get("data", payload)


def strip_html(value: object) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def attachment_urls(items: object) -> list[dict]:
    result = []
    for item in items or []:
        if isinstance(item, dict) and item.get("src"):
            result.append({"url": item["src"], "mime_type": item.get("mime_type", "")})
    return result


def find_raw_questions(package: dict) -> list[dict]:
    for key in ("questions", "items", "questionRefs"):
        value = package.get(key)
        if isinstance(value, list):
            return value
    raise RuntimeError(
        "Package response has no questions list. Try: "
        f"{API_ROOT}/package/<id>?include=questions,items"
    )


def normalize_package(package: dict, rounds: int, per_round: int, quiz_url: str) -> dict:
    raw_questions = find_raw_questions(package)
    flat: list[dict] = []

    for index, question in enumerate(raw_questions, start=1):
        round_no = (index - 1) // per_round + 1
        number = (index - 1) % per_round + 1
        sub_items = question.get("items") or [question]
        for sub_index, item in enumerate(sub_items, start=1):
            if not isinstance(item, dict):
                continue
            flat.append(
                {
                    "id": item.get("id"),
                    "question_id": item.get("question_id") or question.get("id"),
                    "round": round_no,
                    "number": number,
                    "part": sub_index if len(sub_items) > 1 else None,
                    "text": strip_html(item.get("text")),
                    "answer": strip_html(item.get("answer")),
                    "comment": strip_html(item.get("comment")),
                    "type": item.get("type"),
                    "answer_type": item.get("answer_type"),
                    "attachments": attachment_urls(item.get("attachments")),
                    "answer_attachments": attachment_urls(item.get("answerAttachments")),
                }
            )

    return {
        "source": {
            "site": "https://zakovatklubi.uz",
            "quiz_url": quiz_url,
            "api_url": f"{API_ROOT}/package/{package.get('id')}?include=questions",
            "package_id": package.get("id"),
            "title": strip_html(package.get("title")),
            "rounds": rounds,
            "questions_per_round": per_round,
        },
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(flat),
        "questions": flat,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="https://zakovatklubi.uz/quiz/5/10/959/0/0",
    )
    parser.add_argument("--out", default="data/zakovat.json")
    parser.add_argument("--flat-out", default=None)
    args = parser.parse_args()

    try:
        rounds, per_round, package_id = parse_quiz_url(args.url)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    package = api_get(f"/package/{package_id}", {"include": "questions,items,packageRefs"})
    result = normalize_package(package, rounds, per_round, args.url)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {result['count']} questions to {out_path}")

    if args.flat_out:
        flat_path = Path(args.flat_out)
        flat_path.parent.mkdir(parents=True, exist_ok=True)
        flat_path.write_text(
            json.dumps(result["questions"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote flat list to {flat_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
