#!/usr/bin/env python3
"""Export the public turnirlar.uz question base into the archive format.

turnirlar.uz/savollar is the big sibling of zakovatklubi.uz: 7 798 packages and
roughly 559 000 questions. It has no JSON API, so the pages are read directly:

    GET https://turnirlar.uz/savollar/paketlar?page=N   — the package list
    GET https://turnirlar.uz/savollar/paket/<id>        — one package, inline

Two things make the raw pages usable here:

* the site writes Uzbek in a different orthography (``ö ç ş ğ ı`` instead of
  ``o‘ ch sh g‘ i``), so every string is normalised before it is stored — the
  answer checker and the players read standard Latin;
* most of the base overlaps with the export in ``data/all``, so questions whose
  normalised text is already in the archive are dropped instead of duplicated.

The result is sharded and gzipped: ``manifest.json`` (small, deployed with the
bot) plus ``shards/*.json.gz`` (large, fetched on demand — see
``.vercelignore``). Question ids inside a shard are contiguous, so one question
can be found without loading everything.

Examples:

    python3 scripts/export_turnirlar.py --mode probe --probe-dir /tmp/probe
    python3 scripts/export_turnirlar.py --mode import --max-packages 20
    python3 scripts/export_turnirlar.py --mode import            # the whole base

The import is resumable: packages already written to the output directory are
skipped, so an interrupted run continues where it stopped.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SITE = "https://turnirlar.uz/savollar"
USER_AGENT = ("Mozilla/5.0 (compatible; zakovat-archive/1.0; "
              "+https://github.com/jasur-ai/zakovat)")
LIST_PER_PAGE = 24          # what one page of /paketlar shows
ID_OFFSET = 900_000         # turnirlar package 7800 becomes archive package 907800
SHARD_PACKAGES = 150        # packages per shard file
RETRY_SLEEP = 1.5

# ---------------------------------------------------------------- orthography
# The site transliterates Cyrillic with Turkish-flavoured letters. Uzbek Latin
# has one spelling, so every string is mapped back before it is stored.
SCRIPT_MAP = {
    "ö": "o‘", "Ö": "O‘",
    "ç": "ch", "Ç": "Ch",
    "ş": "sh", "Ş": "Sh",
    "ğ": "g‘", "Ğ": "G‘",
    "ı": "i", "İ": "I",
    "ä": "a", "Ä": "A",
    "ü": "u", "Ü": "U",
    "ñ": "ng", "Ñ": "Ng",
    "é": "e", "É": "E",
    "â": "a", "Â": "A",
    "î": "i", "Î": "I",
    "û": "u", "Û": "U",
    "ô": "o", "Ô": "O",
    "’": "‘", "ʼ": "‘", "ʻ": "‘",
}


def normalize_script(text: str) -> str:
    """Turn the site's orthography into the standard Uzbek Latin one."""
    out = []
    for char in str(text or ""):
        mapped = SCRIPT_MAP.get(char)
        out.append(mapped if mapped is not None else char)
    return "".join(out)


def clean_text(value: object) -> str:
    """One string, however the page delivered it: no tags, no double blanks."""
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = normalize_script(text)
    text = text.replace("\u00a0", " ").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fingerprint(text: str) -> str:
    """A hash that survives punctuation, spacing, letter case and orthography.

    Both archives have to agree on what "the same question" means, so the text
    is normalised to standard Latin first and then reduced to its letters.
    """
    folded = normalize_script(str(text or "")).casefold()
    folded = re.sub(r"[^0-9a-z\u0400-\u04ff]+", "", folded)
    return hashlib.sha1(folded.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------- fetch
def fetch(url: str, retries: int = 4, timeout: int = 45) -> tuple[int, str]:
    """GET a page; returns (status, body). Retries on network hiccups only."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "uz,en;q=0.7",
            })
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return int(response.status), raw.decode(charset, errors="replace")
        except HTTPError as exc:
            if exc.code in (404, 410):
                return exc.code, ""
            last = exc
        except (URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries - 1:
            time.sleep(RETRY_SLEEP * (attempt + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def page_text(markup: str) -> str:
    """Visible text of a page: scripts and styles out, block tags become lines."""
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", markup)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</(p|div|li|h[1-6]|section|article|tr|button|span)>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return clean_text(body)


# --------------------------------------------------------------------- probe
def probe(out_dir: Path, package_id: int, list_page: int) -> int:
    """Save raw pages and print what a parser can rely on.

    Run this in CI first: the sandbox that writes the parser has no route to the
    site, so the shape of the pages has to be reported from somewhere that does.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = {
        f"package-{package_id}.html": f"{SITE}/paket/{package_id}",
        f"list-{list_page}.html": f"{SITE}/paketlar?" + urlencode({"page": list_page}),
        f"list-next.html": f"{SITE}/paketlar?" + urlencode({"page": list_page + 1}),
    }
    failures = 0
    for name, url in urls.items():
        try:
            status, markup = fetch(url)
        except RuntimeError as exc:
            print(f"[probe] {name}: {exc}")
            failures += 1
            continue
        (out_dir / name).write_text(markup, encoding="utf-8")
        print(f"[probe] {name}: HTTP {status} · {len(markup)} chars → {url}")
        report(markup)
    return failures


def report(markup: str) -> None:
    """Everything a parser needs to know about one page, in a few lines."""
    markers = ("Javob", "Qabul", "Izoh", "Manba", "Muallif", "Muharrir", "Savol:", "-tur")
    found = {marker: markup.count(marker) for marker in markers}
    question_ids = set(re.findall(r"/savollar/savol/(\d+)", markup))
    package_ids = set(re.findall(r"/savollar/paket/(\d+)", markup))
    print(f"[probe]   markers: {found}")
    print(f"[probe]   question links: {len(question_ids)} (first: {sorted(question_ids)[:3]})")
    print(f"[probe]   package links: {len(package_ids)} (first: {sorted(package_ids)[:5]})")
    print(f"[probe]   next.js payload: {'self.__next_f' in markup} · "
          f"__NEXT_DATA__: {'__NEXT_DATA__' in markup} · "
          f"json-ld: {markup.count('application/ld+json')}")
    for tag in ("article", "section", "details", "summary", "table"):
        print(f"[probe]   <{tag}>: {markup.count('<' + tag)}")
    letters = collections.Counter(char for char in markup
                                  if ord(char) > 127 and char.isalpha())
    print("[probe]   non-ascii letters: "
          + ", ".join(f"{char}×{count}" for char, count in letters.most_common(20)))
    text = page_text(markup)
    print(f"[probe]   visible text: {len(text)} chars")
    print("[probe]   --- first 700 chars of visible text ---")
    print(text[:700])
    print("[probe]   --- end ---")


# --------------------------------------------------------------- list parsing
PACKAGE_LINK = re.compile(r"/savollar/paket/(\d+)")


def parse_list(markup: str) -> list[dict]:
    """Package cards from one page of /paketlar, newest first."""
    text = page_text(markup)
    rows: dict[int, dict] = {}
    # The rendered text keeps a stable order: title, "<N> savol · <M> tur",
    # "<dd.mm.yyyy> · <section>", authors, "Zakovat klubi".
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    ids = [int(item) for item in PACKAGE_LINK.findall(markup)]
    order: list[int] = []
    for package_id in ids:
        if package_id not in order:
            order.append(package_id)
    # Titles are the link texts of those ids, in the same order.
    titles = re.findall(r"/savollar/paket/(\d+)[^>]*>([^<]{3,200})<", markup)
    title_of = {int(pid): clean_text(title) for pid, title in titles}
    counts = re.findall(r"(\d+)\s*savol(?:\s*·\s*(\d+)\s*tur)?", text)
    dates = re.findall(r"(\d{2}\.\d{2}\.\d{4})\s*·\s*([^\n]{2,60})", text)
    for position, package_id in enumerate(order):
        row = {"source_id": package_id, "title": title_of.get(package_id, "")}
        if position < len(counts):
            row["questions"] = int(counts[position][0])
            row["rounds"] = int(counts[position][1] or 0)
        if position < len(dates):
            row["date"] = dates[position][0]
            row["section"] = clean_text(dates[position][1])[:60]
        rows[package_id] = row
    return [rows[pid] for pid in order if rows[pid].get("title")]


# ------------------------------------------------------------ package parsing
LABELS = (("Javob:", "answer"), ("Qabul:", "accepted"), ("Izoh:", "comment"),
          ("Muallif:", "author"), ("Muharrir:", "editor"), ("Manba:", "sources"),
          ("Manbalar:", "sources"))


def parse_package(markup: str, package: dict) -> list[dict]:
    """Every question of one package page, in order, with its round number."""
    text = page_text(markup)
    ids = []
    for question_id in re.findall(r"/savollar/savol/(\d+)", markup):
        value = int(question_id)
        if value not in ids:
            ids.append(value)
    if not ids:
        return []
    # Split the visible text on the "Savol <n>" headings that precede each item.
    chunks = re.split(r"(?m)^Savol\s+\d+\s*$", text)
    body = chunks[1:] if len(chunks) > 1 else []
    rounds = round_marks(text, len(ids))
    questions = []
    for position, question_id in enumerate(ids):
        block = body[position] if position < len(body) else ""
        fields = split_labels(block)
        item = {
            "id": ID_OFFSET * 1000 + question_id,       # globally unique, ordered by source id
            "question_id": question_id,
            "package_id": int(package.get("id") or 0),
            "package_title": package.get("title") or "",
            "source": f"{SITE}/savol/{question_id}",
            "round": rounds[position] if position < len(rounds) else 1,
            "text": fields.get("text", ""),
            "answer": fields.get("answer", ""),
            "accepted": fields.get("accepted", ""),
            "comment": fields.get("comment", ""),
            "author": fields.get("author", ""),
            "editor": fields.get("editor", ""),
            "sources": fields.get("sources", ""),
        }
        if item["text"] and item["answer"]:
            questions.append(item)
    return questions


def round_marks(text: str, total: int) -> list[int]:
    """Which round each question belongs to, from the "<n>-tur" headings."""
    marks = [(match.start(), int(match.group(1)))
             for match in re.finditer(r"(?m)^(\d+)-tur\s*$", text)]
    positions = [match.start() for match in re.finditer(r"(?m)^Savol\s+\d+\s*$", text)]
    if not marks or not positions:
        return [1] * total
    result = []
    for position in positions:
        current = 1
        for offset, number in marks:
            if offset < position:
                current = number
        result.append(current)
    return result[:total] or [1] * total


def split_labels(block: str) -> dict[str, str]:
    """Cut one question block into text / answer / accepted / comment / people."""
    fields: dict[str, str] = {}
    pattern = "|".join(re.escape(label) for label, _ in LABELS)
    pieces = re.split(rf"(?m)^({pattern})\s*", block)
    fields["text"] = clean_text(pieces[0]) if pieces else ""
    for index in range(1, len(pieces) - 1, 2):
        label, value = pieces[index], pieces[index + 1]
        key = dict(LABELS).get(label, "")
        if not key:
            continue
        value = clean_text(value)
        # The next block's question text must not be swallowed by the last label.
        value = re.split(r"(?m)^Savol\s+\d+\s*$", value)[0].strip()
        if key in fields and fields[key]:
            fields[key] += "\n" + value
        else:
            fields[key] = value
    for key in ("text", "answer", "accepted", "comment"):
        fields.setdefault(key, "")
    # The question body ends where "Savol:" asks for the answer; keep both.
    return fields


# -------------------------------------------------------------------- output
def existing_fingerprints(paths: list[Path]) -> set[str]:
    """Fingerprints of the questions already in the archive (dedupe source)."""
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        for row in rows:
            if isinstance(row, dict):
                seen.add(fingerprint(row.get("text", "")))
    return seen


def shard_paths(root: Path) -> list[Path]:
    return sorted((root / "shards").glob("*.json*"))


def load_done(root: Path) -> set[int]:
    """Source package ids already written — what makes a run resumable."""
    manifest = root / "manifest.json"
    if not manifest.exists():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {int(row.get("source_id") or 0) for row in data.get("packages", [])}


def load_manifest(root: Path) -> dict:
    manifest = root / "manifest.json"
    if manifest.exists():
        try:
            return json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"source": SITE, "id_offset": ID_OFFSET, "packages": [], "shards": []}


def write_shard(root: Path, name: str, rows: list[dict], compress: bool) -> str:
    (root / "shards").mkdir(parents=True, exist_ok=True)
    relative = f"shards/{name}" + (".json.gz" if compress else ".json")
    path = root / relative
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if compress:
        with gzip.open(path, "wb", compresslevel=9) as fh:
            fh.write(payload)
    else:
        path.write_bytes(payload)
    return relative


def flush_shard(root: Path, packages: list[dict], rows: list[dict], index: int,
                compress: bool) -> dict:
    ids = [int(row["question_id"]) for row in rows]
    name = f"turnirlar-{index:04d}"
    relative = write_shard(root, name, rows, compress)
    return {
        "file": relative,
        "packages": [int(row["id"]) for row in packages],
        "questions": len(rows),
        "min_question_id": min(ids) if ids else 0,
        "max_question_id": max(ids) if ids else 0,
        "bytes": (root / relative).stat().st_size,
    }


def save_manifest(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["package_count"] = len(manifest.get("packages", []))
    manifest["question_count"] = sum(int(row.get("questions") or 0)
                                     for row in manifest.get("packages", []))
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")


# ---------------------------------------------------------------------- run
def list_all_packages(limit_pages: int, start_page: int, workers: int,
                      max_packages: int) -> list[dict]:
    """Walk /paketlar until it stops producing new packages."""
    seen: dict[int, dict] = {}
    page = start_page
    empty_streak = 0
    while True:
        url = f"{SITE}/paketlar?" + urlencode({"page": page})
        try:
            status, markup = fetch(url)
        except RuntimeError as exc:
            print(f"[list] page {page}: {exc}", file=sys.stderr)
            break
        if status != 200:
            print(f"[list] page {page}: HTTP {status}, stopping", file=sys.stderr)
            break
        rows = parse_list(markup)
        fresh = [row for row in rows if row["source_id"] not in seen]
        for row in fresh:
            seen[row["source_id"]] = row
        empty_streak = empty_streak + 1 if not fresh else 0
        print(f"[list] page {page}: {len(rows)} cards, {len(fresh)} new, {len(seen)} total")
        if not rows or empty_streak >= 3:
            break
        if max_packages and len(seen) >= max_packages:
            break
        if limit_pages and page - start_page + 1 >= limit_pages:
            break
        page += 1
        time.sleep(0.2)
    ordered = sorted(seen.values(), key=lambda row: -int(row["source_id"]))
    return ordered[:max_packages] if max_packages else ordered


def import_packages(rows: list[dict], out_dir: Path, workers: int, compress: bool,
                    dedupe_against: list[Path], polite: float) -> dict:
    known = load_done(out_dir)
    todo = [row for row in rows if int(row["source_id"]) not in known]
    print(f"[import] {len(rows)} packages listed · {len(known)} already stored · "
          f"{len(todo)} to fetch")
    seen_texts = existing_fingerprints(dedupe_against) if dedupe_against else set()
    if seen_texts:
        print(f"[import] dedupe against {len(seen_texts)} known questions")

    manifest = load_manifest(out_dir)
    packages = list(manifest.get("packages", []))
    shards = list(manifest.get("shards", []))
    index = len(shards) + 1
    buffer_packages: list[dict] = []
    buffer_rows: list[dict] = []
    stats = {"packages": 0, "questions": 0, "duplicates": 0, "failed": 0, "empty": 0}

    def one(row: dict) -> tuple[dict, list[dict]]:
        package = {
            "id": ID_OFFSET + int(row["source_id"]),
            "source_id": int(row["source_id"]),
            "title": row.get("title") or "",
            "questions": 0,
            "rounds": int(row.get("rounds") or 0),
            "date": row.get("date") or "",
            "section": row.get("section") or "",
            "origin": SITE + f"/paket/{row['source_id']}",
        }
        status, markup = fetch(f"{SITE}/paket/{row['source_id']}")
        if status != 200:
            return package, []
        return package, parse_package(markup, package)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in todo:
            futures[pool.submit(one, row)] = row
            time.sleep(polite / max(1, workers))
        for future in as_completed(futures):
            row = futures[future]
            try:
                package, questions = future.result()
            except Exception as exc:                       # noqa: BLE001 - keep going
                stats["failed"] += 1
                print(f"[import] package {row['source_id']}: {exc}", file=sys.stderr)
                continue
            fresh: list[dict] = []
            for question in questions:
                mark = fingerprint(question["text"])
                if mark in seen_texts:
                    stats["duplicates"] += 1
                    continue
                seen_texts.add(mark)
                fresh.append(question)
            if not fresh:
                stats["empty"] += 1
                continue
            package["questions"] = len(fresh)
            package["rounds"] = max(int(item["round"]) for item in fresh)
            packages.append(package)
            buffer_packages.append(package)
            buffer_rows.extend(fresh)
            stats["packages"] += 1
            stats["questions"] += len(fresh)
            if len(buffer_packages) >= SHARD_PACKAGES:
                shards.append(flush_shard(out_dir, buffer_packages, buffer_rows, index, compress))
                index += 1
                buffer_packages, buffer_rows = [], []
                manifest["packages"], manifest["shards"] = packages, shards
                save_manifest(out_dir, manifest)
                print(f"[import] {stats['packages']} packages · {stats['questions']} questions "
                      f"· {len(shards)} shards")

    if buffer_packages:
        shards.append(flush_shard(out_dir, buffer_packages, buffer_rows, index, compress))
    manifest["packages"], manifest["shards"] = packages, shards
    save_manifest(out_dir, manifest)
    print(f"[import] done: {stats}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("probe", "import"), default="import")
    parser.add_argument("--out-dir", default="data/turnirlar")
    parser.add_argument("--probe-dir", default="/tmp/turnirlar-probe")
    parser.add_argument("--probe-package", type=int, default=7800)
    parser.add_argument("--probe-page", type=int, default=1)
    parser.add_argument("--max-packages", type=int, default=0, help="0 = the whole base")
    parser.add_argument("--pages", type=int, default=0, help="stop after N list pages")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--polite", type=float, default=0.4, help="seconds between requests")
    parser.add_argument("--no-gzip", action="store_true")
    parser.add_argument("--no-dedupe", action="store_true")
    args = parser.parse_args()

    if args.mode == "probe":
        return 1 if probe(Path(args.probe_dir), args.probe_package, args.probe_page) else 0

    out_dir = Path(args.out_dir)
    dedupe = [] if args.no_dedupe else sorted(Path("data/all/questions").glob("*.json"))
    rows = list_all_packages(args.pages, args.start_page, args.workers, args.max_packages)
    if not rows:
        print("[import] no packages found", file=sys.stderr)
        return 1
    import_packages(rows, out_dir, args.workers, not args.no_gzip, dedupe, args.polite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
