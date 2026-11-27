#!/usr/bin/env python3
"""Export the public turnirlar.uz question base into the archive format.

turnirlar.uz/savollar is the big sibling of zakovatklubi.uz: 7 798 packages and
roughly 559 000 questions. There is no JSON API — the site downloads PDF/DOCX
only — so the server-rendered pages are read directly:

    GET https://turnirlar.uz/savollar/paketlar?page=N   — 30 package cards
    GET https://turnirlar.uz/savollar/paket/<id>        — one package, inline

Both are semantic enough to parse without a browser: cards are
``article.zk-catalog-card``, questions are ``article#savol-N`` inside
``section#tur-N``, and the answer block labels its parts
(``Javob: Qabul: Izoh: Manba(lar): Muallif(lar): Muharrir:``).

Two things make the raw pages usable here:

* the site writes Uzbek in a different orthography (``ö ç ş ğ ı`` instead of
  ``o‘ ch sh g‘ i``), so every string is normalised before it is stored — the
  answer checker and the players read standard Latin;
* part of the base overlaps with the export in ``data/all`` (the site itself
  counts 1 238 "Zakovat klubi" packages), so questions whose normalised text is
  already in the archive are dropped instead of duplicated.

Output: ``manifest.json`` (small, committed, deployed with the bot) plus
``shards/turnirlar-NNNN.json.gz`` (large, git-ignored, published to the GitHub
Release ``savollar-bazasi`` — see ``.vercelignore``). The bot fetches the shard
it needs over HTTPS and caches it, so the 500 MB never enter the deployment.

Examples:

    python3 scripts/export_turnirlar.py --mode probe --probe-dir /tmp/probe
    python3 scripts/export_turnirlar.py --mode import --max-packages 20 \
        --sample data/turnirlar/sample.json
    python3 scripts/export_turnirlar.py --mode import            # the whole base

The import is resumable: packages whose shard is already in the output directory
are skipped, so an interrupted run continues where it stopped. A manifest that
lists shards which are not on disk is pruned first, so a run can never claim
questions it does not have.
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
ORIGIN = "https://turnirlar.uz"
USER_AGENT = ("Mozilla/5.0 (compatible; zakovat-archive/1.0; "
              "+https://github.com/jasur-ai/zakovat)")
LIST_PER_PAGE = 30          # cards on one page of /paketlar
ID_OFFSET = 900_000         # turnirlar package 7800 becomes archive package 907800
QUESTION_OFFSET = 900_000_000
SHARD_PACKAGES = 150        # packages per shard file
RETRY_SLEEP = 1.5
RELEASE = "savollar-bazasi"
BASE_URL = f"https://github.com/jasur-ai/zakovat/releases/download/{RELEASE}/"
SOURCES = {"all": "", "zakovat-klubi": "zakovat-klubi", "boshqa": "boshqa"}

# ---------------------------------------------------------------- orthography
# The site transliterates Cyrillic with Turkish-flavoured letters. Uzbek Latin
# has one spelling, so every string is mapped back before it is stored.
# lowercase → standard Uzbek Latin
LOWER_MAP = {
    "ö": "o‘", "ç": "ch", "ş": "sh", "ğ": "g‘", "ı": "i",
    "ä": "a", "ü": "u", "ñ": "ng", "é": "e", "â": "a",
    "î": "i", "û": "u", "ô": "o",
}
# uppercase → (all-caps form, capitalised form): "IŞNI" is "ISHNI", but
# "Şaripov" is "Sharipov", so the case of the next letter decides.
UPPER_MAP = {
    "Ö": ("O‘", "O‘"), "Ç": ("CH", "Ch"), "Ş": ("SH", "Sh"), "Ğ": ("G‘", "G‘"),
    "İ": ("I", "I"), "Ä": ("A", "A"), "Ü": ("U", "U"), "Ñ": ("NG", "Ng"),
    "É": ("E", "E"), "Â": ("A", "A"), "Î": ("I", "I"), "Û": ("U", "U"),
    "Ô": ("O", "O"),
}
# the site's modifier letters for the Uzbek turned comma
APOSTROPHE_MAP = {"ʼ": "‘", "ʻ": "‘", "′": "‘"}


def normalize_script(text: str) -> str:
    """Turn the site's orthography into the standard Uzbek Latin one."""
    value = str(text or "")
    out: list[str] = []
    for position, char in enumerate(value):
        if char in LOWER_MAP:
            out.append(LOWER_MAP[char])
            continue
        if char in APOSTROPHE_MAP:
            out.append(APOSTROPHE_MAP[char])
            continue
        pair = UPPER_MAP.get(char)
        if not pair:
            out.append(char)
            continue
        shout, title = pair
        following = value[position + 1] if position + 1 < len(value) else ""
        previous = value[position - 1] if position else ""
        if following.isupper() or (not following.isalpha() and previous.isupper()):
            out.append(shout)
        else:
            out.append(title)
    return "".join(out)


def clean_text(value: object) -> str:
    """One string, however the page delivered it: no tags, no double blanks."""
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"(?s)<!--.*?-->", "", text)          # React text separators
    text = re.sub(r"(?is)<(svg|script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = normalize_script(text)
    text = text.replace("\u00a0", " ").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
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


def epoch_of(date: str) -> int | None:
    """``16.11.2025`` → seconds since the epoch (UTC), or None."""
    try:
        stamp = datetime.strptime(str(date or "").strip(), "%d.%m.%Y")
    except ValueError:
        return None
    return int(stamp.replace(tzinfo=timezone.utc).timestamp())


def absolute(url: str) -> str:
    """Media links are site-relative; keep them usable from anywhere."""
    value = str(url or "").strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return ORIGIN + value
    return value


# --------------------------------------------------------------------- fetch
def fetch(url: str, retries: int = 4, timeout: int = 45,
          headers: dict | None = None) -> tuple[int, str]:
    """GET a page; returns (status, body). Retries on network hiccups only."""
    merged = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "uz,en;q=0.7",
    }
    merged.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=merged), timeout=timeout) as response:
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


def strip_scripts(markup: str) -> str:
    """The rendered markup without the Next.js flight payload.

    The flight payload repeats the whole page as escaped JSON, which would make
    every marker count double and every naive split land in the wrong place.
    """
    return re.sub(r"(?is)<script\b.*?</script>", " ", markup)


def page_text(markup: str) -> str:
    """Visible text of a page: scripts and styles out, block tags become lines."""
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", markup)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</(p|div|li|h[1-6]|section|article|tr|button|span|dd|dt)>",
                  "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return clean_text(body)


# --------------------------------------------------------------------- probe
ENDPOINTS = (
    "/api/paketlar?page=1",
    "/savollar/api/paketlar?page=1",
    "/savollar/api/paket/7800",
    "/savollar/api/paketlar/7800",
    "/savollar/api/savol/559880",
)


def probe(out_dir: Path, package_ids: list[int], list_page: int,
          endpoints: bool = True) -> int:
    """Save raw pages and print what a parser can rely on.

    Run this in CI first: the sandbox that writes the parser has no route to the
    site, so the shape of the pages has to be reported from somewhere that does.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = {}
    for package_id in package_ids:
        urls[f"package-{package_id}.html"] = f"{SITE}/paket/{package_id}"
    urls[f"list-{list_page}.html"] = f"{SITE}/paketlar?" + urlencode({"page": list_page})
    urls["list-next.html"] = f"{SITE}/paketlar?" + urlencode({"page": list_page + 1})
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

    first = urls[f"list-{list_page}.html"]
    try:
        pages = list_page_count(Path(out_dir / Path(first).name).read_text(encoding="utf-8"))
    except OSError:
        pages = 0
    if pages:
        try:
            status, markup = fetch(f"{SITE}/paketlar?" + urlencode({"page": pages}))
            (out_dir / f"list-{pages}.html").write_text(markup, encoding="utf-8")
            print(f"[probe] last list page: HTTP {status} · page {pages} · "
                  f"{len(parse_list(markup))} cards")
        except RuntimeError as exc:
            print(f"[probe] last list page: {exc}")

    if endpoints:
        print("[probe] looking for a JSON endpoint behind the pages")
        for path in ENDPOINTS:
            url = ORIGIN + path
            for extra in ({}, {"RSC": "1"}):
                try:
                    status, body = fetch(url, retries=1, timeout=25, headers=extra)
                except RuntimeError as exc:
                    print(f"[probe]   {path} {extra or ''}: {exc}")
                    continue
                kind = "json" if body.lstrip()[:1] in "{[" else "other"
                print(f"[probe]   {path} {extra or ''}: HTTP {status} · {kind} · "
                      f"{len(body)} chars · {body[:160]!r}")
    return failures


def report(markup: str) -> None:
    """Everything a parser needs to know about one page, in a few lines."""
    body = strip_scripts(markup)
    markers = ("Javob", "Qabul", "Izoh", "Manba", "Muallif", "Muharrir", "-tur")
    found = {marker: body.count(marker) for marker in markers}
    question_ids = set(re.findall(r"/savollar/savol/(\d+)", body))
    package_ids = set(re.findall(r"/savollar/paket/(\d+)", body))
    print(f"[probe]   rendered markup: {len(body)} chars · markers: {found}")
    print(f"[probe]   question links: {len(question_ids)} · package links: {len(package_ids)}")
    articles = body.count("<article")
    tours = body.count('class="zk-tour"')
    details = body.count("<details")
    images = body.count("zk-material-image")
    print(f"[probe]   articles: {articles} · tours: {tours} · details: {details} · images: {images}")
    badges = sorted(set(re.sub(r"<[^>]+>", "", badge).strip() for badge in BADGE_RE.findall(body)))
    labels = collections.Counter(re.findall(r'zk-answer-label[^"]*">([^<]{2,40})<', body))
    print(f"[probe]   badges: {badges}")
    print(f"[probe]   answer labels: {dict(labels)}")
    letters = collections.Counter(char for char in body
                                  if ord(char) > 127 and char.isalpha())
    print("[probe]   non-ascii letters: "
          + ", ".join(f"{char}×{count}" for char, count in letters.most_common(14)))
    text = page_text(markup)
    print(f"[probe]   visible text: {len(text)} chars")
    print("[probe]   --- first 500 chars of visible text ---")
    print(text[:500])
    print("[probe]   --- end ---")


def check(probe_dir: Path, sample: Path | None = None) -> int:
    """Parse pages that were fetched earlier and report what came out.

    Offline by design: it is how the parsers get verified from a sandbox that
    has no route to the site, using only files the probe job left behind.
    """
    listed: list[dict] = []
    questions: list[dict] = []
    for path in sorted(probe_dir.glob("list-*.html")):
        markup = path.read_text(encoding="utf-8", errors="replace")
        rows = parse_list(markup)
        listed.extend(rows)
        print(f"[check] {path.name}: {len(rows)} cards · "
              f"{list_page_count(markup)} pages · {list_total(markup)} packages announced")
        for row in rows[:3]:
            print(f"[check]   {json.dumps(row, ensure_ascii=False)[:240]}")
    overlap = len(listed) - len({row["source_id"] for row in listed})
    print(f"[check] {len(listed)} cards listed · {overlap} repeated ids")

    for path in sorted(probe_dir.glob("package-*.html")):
        markup = path.read_text(encoding="utf-8", errors="replace")
        source_id = int(re.sub(r"\D", "", path.stem) or 0)
        package = {"id": ID_OFFSET + source_id, "source_id": source_id}
        meta, rows = parse_package(markup, package)
        questions.extend(rows)
        announced = meta.get("Savollar soni") or "?"
        print(f"[check] {path.name}: parsed {len(rows)} of {announced} questions · "
              f"meta {json.dumps({k: v for k, v in meta.items() if k != 'note'}, ensure_ascii=False)[:300]}")
        fields = collections.Counter(key for row in rows for key in row)
        print(f"[check]   fields: {dict(fields.most_common())}")
        raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        print(f"[check]   size: {len(raw)} bytes raw · "
              f"{len(gzip.compress(raw, 9))} bytes gzipped · "
              f"{len(raw) // max(1, len(rows))} bytes per question")
        empty = [row["question_id"] for row in rows if not row.get("answer")]
        if empty:
            print(f"[check]   no answer for: {empty[:10]}")
        for row in rows[:2]:
            print(f"[check]   {json.dumps(row, ensure_ascii=False)[:700]}")
    if sample is not None and (listed or questions):
        write_sample(sample, questions, listed)
    return 0 if (listed or questions) else 1


# --------------------------------------------------------------- list parsing
CARD_RE = re.compile(r'<article class="zk-catalog-card">(.*?)</article>', re.S)
CARD_LINK_RE = re.compile(
    r'<a class="zk-catalog-card-link" href="/savollar/paket/(\d+)">(.*?)</a>', re.S)
CARD_META_RE = re.compile(r'<p class="zk-catalog-card-meta">(.*?)</p>', re.S)
CARD_FOOT_RE = re.compile(r'<p class="zk-catalog-card-foot">(.*?)</p>', re.S)
PERSON_RE = re.compile(r'<a class="zk-person-link" href="/savollar/profile/[^"]*">(.*?)</a>', re.S)
PAGE_RE = re.compile(r'aria-label="(\d+)-sahifa"')
TOTAL_RE = re.compile(r"([\d\s\u00a0]{2,12})\s*ta to[‘'`]plam")


def parse_list(markup: str) -> list[dict]:
    """Package cards of one /paketlar page, in the order the page shows them."""
    rows: list[dict] = []
    for card in CARD_RE.findall(strip_scripts(markup)):
        link = CARD_LINK_RE.search(card)
        if not link:
            continue
        row: dict = {"source_id": int(link.group(1)), "title": clean_text(link.group(2))}
        metas = [clean_text(meta) for meta in CARD_META_RE.findall(card)]
        if metas:
            count = re.search(r"(\d+)\s*savol", metas[0])
            rounds = re.search(r"(\d+)\s*tur", metas[0])
            if count:
                row["questions"] = int(count.group(1))
            if rounds:
                row["rounds"] = int(rounds.group(1))
        if len(metas) > 1:
            parts = [part.strip() for part in metas[1].split("·")]
            if re.match(r"\d{2}\.\d{2}\.\d{4}$", parts[0]):
                row["date"] = parts[0]
                row["section"] = " · ".join(parts[1:])[:60]
            else:
                row["section"] = metas[1][:60]
        row["authors"] = [clean_text(name) for name in PERSON_RE.findall(card)][:6]
        foot = CARD_FOOT_RE.search(card)
        row["origin"] = clean_text(foot.group(1))[:60] if foot else ""
        rows.append(row)
    return rows


def list_page_count(markup: str) -> int:
    """Highest page number the pagination offers (0 when there is none)."""
    pages = [int(number) for number in PAGE_RE.findall(markup)]
    return max(pages) if pages else 0


def list_total(markup: str) -> int:
    """Package count the page announces ("7 798 ta to‘plam")."""
    match = TOTAL_RE.search(page_text(markup))
    if not match:
        return 0
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else 0


# ------------------------------------------------------------ package parsing
TOUR_RE = re.compile(r'<section id="tur-(\d+)"')
TOUR_TITLE_RE = re.compile(r'<h2 class="zk-tour-title">(.*?)</h2>\s*'
                           r'<span class="zk-tour-count">(.*?)</span>', re.S)
ARTICLE_START_RE = re.compile(r'<article id="savol-')
ART_OPEN_RE = re.compile(r'<article id="savol-(\d+)"([^>]*)>')
PANEL_REF_RE = re.compile(r'data-panel-ref="(\d+)"')
QNUMBER_RE = re.compile(r'<a class="zk-question-number"[^>]*href="/savollar/savol/(\d+)"', re.S)
# A package of more than 60 questions is rendered one tour at a time: the page
# ends with "Keyingi tur: 2-tur →" linking to ?tur=2, and that page links on.
TUR_LINK_RE = re.compile(r'href="[^"]*[?&]tur=(\d+)"')
QTEXT_RE = re.compile(r'<p class="zk-question-text">(.*?)</p>', re.S)
MATERIAL_RE = re.compile(r'<figure class="zk-material-block">(.*?)</figure>', re.S)
MATERIAL_TEXT_RE = re.compile(r'<p class="zk-material-text">(.*?)</p>', re.S)
IMAGE_RE = re.compile(r'<img[^>]*src="([^"]+)"[^>]*>', re.S)
BADGE_RE = re.compile(r'<span class="zk-badge[^"]*">(.*?)</span>', re.S)
LIKE_RE = re.compile(r'title="Yoqdi[^"]*?(\d+)')
# "Qabul:" sometimes carries a remark instead of another answer
GENERIC_ACCEPTED = re.compile(
    r"(?i)ma[’'`]nodosh|sinonim|har qanday|qabul qilin|ixtiyoriy|deya oladi|hisoblanadi")
ANSWER_BODY_RE = re.compile(
    r'<div class="zk-answer-body">(.*?)(?=<div class="zk-sources"|</details>|$)', re.S)
LABEL_RE = re.compile(r'<i class="zk-answer-label[^"]*">([^<]{2,30}):</i>(.*?)(?=</p>)', re.S)
EXPLAIN_RE = re.compile(r'<p class="zk-answer-explanation">(.*?)</p>', re.S)
SOURCES_RE = re.compile(r'<div class="zk-sources">(.*?)(?=<p class="zk-answer-credits"|</div></details>|$)', re.S)
CREDITS_RE = re.compile(r'<p class="zk-answer-credits">(.*?)</p>', re.S)
CREDIT_RE = re.compile(r'<span class="zk-question-credit[^"]*">')
CREDIT_LABEL_RE = re.compile(r'zk-question-credit-label">([^<]{2,30}):</i>', re.S)
FACTS_RE = re.compile(r'<dl class="zk-side-facts">(.*?)</dl>', re.S)
FACT_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
NOTE_RE = re.compile(r'<p class="zk-package-note">(.*?)</p>', re.S)
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)


def package_meta(markup: str) -> dict:
    """What the package page says about the package itself."""
    body = strip_scripts(markup)
    meta: dict = {}
    title = TITLE_RE.search(body)
    if title:
        meta["title"] = clean_text(title.group(1))
    facts = FACTS_RE.search(body)
    if facts:
        for key, value in FACT_RE.findall(facts.group(1)):
            meta[clean_text(key)] = clean_text(value)
    note = NOTE_RE.search(body)
    if note:
        meta["note"] = clean_text(note.group(1))[:1200]
    tours = [{"title": clean_text(name), "count": clean_text(count)}
             for name, count in TOUR_TITLE_RE.findall(body)]
    if tours:
        meta["tours"] = tours
    return meta


def _label_values(block: str) -> dict[str, list[str]]:
    """``Javob:``/``Qabul:``/… → their values, in document order."""
    found: dict[str, list[str]] = collections.defaultdict(list)
    for label, value in LABEL_RE.findall(block):
        text = clean_text(value)
        if text:
            found[clean_text(label).rstrip(":")].append(text)
    return dict(found)


def _credits(chunk: str) -> tuple[list[str], list[str]]:
    """Authors and editors out of the credits line."""
    authors: list[str] = []
    editors: list[str] = []
    credits = CREDITS_RE.search(chunk)
    if not credits:
        return authors, editors
    pieces = CREDIT_RE.split(credits.group(1))[1:]
    for piece in pieces:
        label = CREDIT_LABEL_RE.search(piece)
        names = [clean_text(name) for name in PERSON_RE.findall(piece)]
        names = [name for name in names if name]
        if not names:
            plain = clean_text(re.sub(r"<i[^>]*>.*?</i>", " ", piece, flags=re.S))
            names = [plain] if plain else []
        if label and label.group(1).strip().lower().startswith("muharrir"):
            editors.extend(names)
        else:
            authors.extend(names)
    return authors[:8], editors[:8]


def parse_question(chunk: str, package: dict, position: int, tour: int) -> dict | None:
    """One ``article#savol-N`` → one archive question."""
    opening = ART_OPEN_RE.match(chunk)
    attributes = opening.group(2) if opening else chunk[:400]
    index = int(opening.group(1)) if opening else position + 1

    question_id = 0
    ref = PANEL_REF_RE.search(attributes)
    if ref:
        question_id = int(ref.group(1))
    if not question_id:
        number = QNUMBER_RE.search(chunk)
        if number:
            question_id = int(number.group(1))
    if not question_id:
        return None

    text_match = QTEXT_RE.search(chunk)
    text = clean_text(text_match.group(1)) if text_match else ""

    material_texts: list[str] = []
    images: list[str] = []
    for material in MATERIAL_RE.findall(chunk):
        for piece in MATERIAL_TEXT_RE.findall(material):
            value = clean_text(piece)
            if value:
                material_texts.append(value)
        for source in IMAGE_RE.findall(material):
            url = absolute(source)
            if url and url not in images:
                images.append(url)
    if material_texts:
        text = "\n".join(material_texts + ([text] if text else []))
    if not text:
        return None

    answer_block = ANSWER_BODY_RE.search(chunk)
    labels = _label_values(answer_block.group(1)) if answer_block else {}
    answer = (labels.get("Javob") or [""])[0]
    accepted = [value for value in labels.get("Qabul", []) if value]
    explain = EXPLAIN_RE.search(answer_block.group(1)) if answer_block else None
    comment = clean_text(explain.group(1)) if explain else ""
    comment = re.sub(r"^Izoh\s*:\s*", "", comment).strip()

    sources: list[str] = []
    source_block = SOURCES_RE.search(chunk)
    if source_block:
        for item in re.findall(r"<div>(.*?)</div>", source_block.group(1), re.S):
            value = clean_text(item)
            value = re.sub(r"^\d+\s*\)\s*", "", value).strip()
            value = value[:200]
            if value and value not in sources:
                sources.append(absolute(value) if value.startswith("/") else value)
        for href in re.findall(r'<a href="([^"]+)"', source_block.group(1)):
            url = absolute(href)
            if url and url not in sources:
                sources.append(url)

    authors, editors = _credits(chunk)
    house_editor = clean_text(package.get("editor") or "")
    if house_editor:
        editors = [name for name in editors if name != house_editor]
    badges = []
    for badge in BADGE_RE.findall(chunk):
        value = clean_text(badge)
        if value and value not in badges:
            badges.append(value)
    like = LIKE_RE.search(chunk)

    # Real alternatives join the answer with the same "/" separator that
    # bot/answers.py already splits on; a note such as "Ma’nodosh javoblar."
    # says nothing about the answer itself, so it stays out of the way.
    alternatives = [value for value in accepted if not GENERIC_ACCEPTED.search(value)]
    full_answer = " / ".join([answer] + alternatives) if alternatives else answer
    # Per-package facts (title, date, section, origin) live in the manifest and
    # the question page is derivable from question_id, so neither is repeated
    # 559 000 times here. Empty values are dropped for the same reason.
    row = {
        "id": QUESTION_OFFSET + question_id,
        "question_id": question_id,
        "package_id": int(package.get("id") or 0),
        "index": index,
        "round": tour,
        "text": text,
        "answer": full_answer,
        "comment": comment,
        "accepted": accepted,
        "attachments": images,
        "sources": sources[:4],
        "authors": authors,
        "editors": editors,
        "badges": badges,
        "likes": int(like.group(1)) if like else 0,
        "created_at": epoch_of(str(package.get("date") or "")),
    }
    keep = ("id", "question_id", "package_id", "index", "round", "text", "answer")
    return {key: value for key, value in row.items()
            if key in keep or value not in ("", [], None, 0)}


def parse_package(markup: str, package: dict) -> tuple[dict, list[dict]]:
    """Every question of one package page, in order, with its round number."""
    body = strip_scripts(markup)
    meta = package_meta(body)
    if meta.get("Muharrir"):
        # one editor for the whole package: no need to repeat it per question
        package = dict(package, editor=meta["Muharrir"])
    tours = [(match.start(), int(match.group(1))) for match in TOUR_RE.finditer(body)]
    starts = [match.start() for match in ARTICLE_START_RE.finditer(body)]
    questions: list[dict] = []
    for position, start in enumerate(starts):
        end = body.find("</article>", start)
        chunk = body[start:end if end > start else len(body)]
        tour = 1
        for offset, number in tours:
            if offset < start:
                tour = number
        row = parse_question(chunk, package, position, tour)
        if row:
            questions.append(row)
    return meta, questions


def _digits(value: object) -> int:
    """``"36 savol"`` → 36 (0 when the page does not say)."""
    return int(re.sub(r"\D", "", str(value or "")) or 0)


def tour_links(markup: str) -> list[int]:
    """The tour numbers a package page links to, in order."""
    return sorted({int(number) for number in TUR_LINK_RE.findall(markup)})


def fetch_package(source_id: int, package: dict, max_tours: int = 80) -> tuple[dict, dict, list[dict]]:
    """Every question of one package, following the tour pages when needed.

    ``Savollar soni`` on the package page is the site's own count, so it decides
    when to stop: a package that fits on one page is read once, a bigger one is
    read tour by tour until the count is reached. Questions are kept in the
    site's own order (``article#savol-N`` numbers run across tours) and a
    question that shows up twice is kept once.
    """
    status, markup = fetch(f"{SITE}/paket/{source_id}")
    if status != 200:
        return package, {"http": status}, []
    meta, questions = parse_package(markup, package)
    body = strip_scripts(markup)
    announced = _digits(meta.get("Savollar soni"))
    seen = {row["question_id"] for row in questions}
    read = {1}                                   # the plain page is the first tour
    while announced and len(questions) < announced and len(read) < max_tours:
        pending = [tour for tour in tour_links(body) if tour not in read]
        if not pending:
            break
        tour = pending[0]
        read.add(tour)
        try:
            status, markup = fetch(f"{SITE}/paket/{source_id}?" + urlencode({"tur": tour}))
        except RuntimeError as exc:
            print(f"[import] package {source_id}: tour {tour}: {exc}", file=sys.stderr)
            meta["tour_failed"] = tour
            break
        if status != 200:
            print(f"[import] package {source_id}: tour {tour}: HTTP {status}", file=sys.stderr)
            meta["tour_failed"] = tour
            break
        body = strip_scripts(markup)
        _tour_meta, more = parse_package(markup, package)
        for row in more:
            if row["question_id"] in seen:
                continue
            seen.add(row["question_id"])
            questions.append(row)
    meta["announced"] = announced
    meta["tours"] = len(read)
    questions.sort(key=lambda row: (row["index"], row["question_id"]))
    return package, meta, questions


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


def read_shard(path: Path) -> list[dict]:
    """The questions of one shard, compressed or not, empty when unreadable."""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows = json.load(handle)
        else:
            with path.open(encoding="utf-8") as handle:
                rows = json.load(handle)
    except (OSError, json.JSONDecodeError, EOFError):
        return []
    return rows if isinstance(rows, list) else []


def load_state(root: Path) -> tuple[dict, set[int]]:
    """The stored manifest, pruned to the shards that are really on disk.

    A manifest may be committed while its shards live in a Release that has not
    been downloaded, so anything whose file is missing is dropped — and with it
    the packages that claim to be stored. That is what makes a run resumable
    without ever reporting questions it cannot show.
    """
    manifest_path = root / "manifest.json"
    manifest: dict = {"source": SITE, "id_offset": ID_OFFSET, "base_url": BASE_URL,
                      "packages": [], "shards": []}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, json.JSONDecodeError):
            pass
    kept: list[dict] = []
    stored: set[int] = set()
    for shard in manifest.get("shards", []):
        relative = shard.get("file") if isinstance(shard, dict) else str(shard)
        if relative and (root / relative).exists():
            kept.append(shard)
            stored.update(int(pid) for pid in shard.get("packages", []))
    manifest["shards"] = kept
    if kept:
        manifest["packages"] = [row for row in manifest.get("packages", [])
                                if int(row.get("source_id") or 0) in stored]
    else:
        manifest["packages"] = []
    return manifest, stored


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
    for row in packages:                      # the manifest points at the shard
        row["shard"] = index
    return {
        "file": relative,
        "index": index,
        "packages": [int(row["source_id"]) for row in packages],
        "questions": len(rows),
        "min_question_id": min(ids) if ids else 0,
        "max_question_id": max(ids) if ids else 0,
        "bytes": (root / relative).stat().st_size,
    }


def save_manifest(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    packages = manifest.get("packages", [])
    manifest["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["base_url"] = manifest.get("base_url") or BASE_URL
    manifest["package_count"] = len(packages)
    manifest["question_count"] = sum(int(row.get("questions") or 0) for row in packages)
    manifest["with_images"] = sum(1 for row in packages if row.get("images"))
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")


def write_sample(path: Path, rows: list[dict], listed: list[dict]) -> None:
    """A small, human-readable slice of the run — readable without the site."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "listed_packages": listed[:20],
        "questions": rows[:12],
        "fields": sorted({key for row in rows[:200] for key in row}),
        "average_bytes": (sum(len(json.dumps(row, ensure_ascii=False)) for row in rows[:500])
                          // max(1, min(len(rows), 500))),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[import] sample → {path} ({path.stat().st_size} bytes)")


# ---------------------------------------------------------------------- run
def list_all_packages(limit_pages: int, start_page: int, workers: int,
                      max_packages: int, source: str = "all",
                      polite: float = 0.2) -> list[dict]:
    """Walk /paketlar until the pagination runs out."""
    query: dict[str, object] = {"page": start_page}
    if SOURCES.get(source):
        query["manba"] = SOURCES[source]

    def url_of(page: int) -> str:
        params = dict(query, page=page)
        return f"{SITE}/paketlar?" + urlencode(params)

    seen: dict[int, dict] = {}
    last_page = 0
    try:
        status, markup = fetch(url_of(start_page))
    except RuntimeError as exc:
        print(f"[list] page {start_page}: {exc}", file=sys.stderr)
        return []
    if status != 200:
        print(f"[list] page {start_page}: HTTP {status}", file=sys.stderr)
        return []
    rows = parse_list(markup)
    for row in rows:
        seen[row["source_id"]] = row
    pages = list_page_count(markup)
    total = list_total(markup)
    print(f"[list] page {start_page}: {len(rows)} cards · {pages or '?'} pages · "
          f"{total or '?'} packages announced")
    if pages:
        last_page = min(pages, start_page + limit_pages - 1) if limit_pages else pages
    else:
        last_page = start_page

    todo = [page for page in range(start_page + 1, last_page + 1)]
    if max_packages:
        # 30 cards per page: never walk further than the request needs.
        need = max_packages - len(seen)
        todo = todo[:max(0, -(-need // LIST_PER_PAGE))]
    failures = 0

    def one(page: int) -> tuple[int, list[dict]]:
        status, markup = fetch(url_of(page))
        return status, parse_list(markup) if status == 200 else []

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {}
            for page in todo:
                futures[pool.submit(one, page)] = page
                time.sleep(polite / max(1, workers))
            done_pages = 0
            for future in as_completed(futures):
                page = futures[future]
                done_pages += 1
                try:
                    status, rows = future.result()
                except Exception as exc:                        # noqa: BLE001
                    failures += 1
                    print(f"[list] page {page}: {exc}", file=sys.stderr)
                    continue
                fresh = [row for row in rows if row["source_id"] not in seen]
                for row in fresh:
                    seen[row["source_id"]] = row
                if status != 200:
                    failures += 1
                if done_pages % 25 == 0 or done_pages == len(todo):
                    print(f"[list] {done_pages}/{len(todo)} pages · {len(seen)} packages")
    if failures:
        print(f"[list] {failures} pages failed", file=sys.stderr)
    ordered = sorted(seen.values(), key=lambda row: -int(row["source_id"]))
    return ordered[:max_packages] if max_packages else ordered


def import_packages(rows: list[dict], out_dir: Path, workers: int, compress: bool,
                    dedupe_against: list[Path], polite: float) -> tuple[dict, list[dict]]:
    manifest, known = load_state(out_dir)
    todo = [row for row in rows if int(row["source_id"]) not in known]
    print(f"[import] {len(rows)} packages listed · {len(known)} already stored · "
          f"{len(todo)} to fetch")
    seen_texts = existing_fingerprints(dedupe_against) if dedupe_against else set()
    if seen_texts:
        print(f"[import] dedupe against {len(seen_texts)} known questions")

    packages = list(manifest.get("packages", []))
    shards = list(manifest.get("shards", []))
    index = max([int(shard.get("index") or 0) for shard in shards], default=0) + 1
    buffer_packages: list[dict] = []
    buffer_rows: list[dict] = []
    sample: list[dict] = []
    stats = collections.Counter(packages=0, questions=0, duplicates=0, failed=0,
                                empty=0, images=0, missing=0)

    def one(row: dict) -> tuple[dict, dict, list[dict]]:
        source_id = int(row["source_id"])
        package = {
            "id": ID_OFFSET + source_id,
            "source_id": source_id,
            "title": row.get("title") or "",
            "questions": 0,
            "rounds": int(row.get("rounds") or 0),
            "date": row.get("date") or "",
            "section": row.get("section") or "",
            "origin": row.get("origin") or "",
            "url": f"{SITE}/paket/{source_id}",
        }
        return fetch_package(source_id, package)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in todo:
            futures[pool.submit(one, row)] = row
            time.sleep(polite / max(1, workers))
        for future in as_completed(futures):
            row = futures[future]
            try:
                package, meta, questions = future.result()
            except Exception as exc:                            # noqa: BLE001
                stats["failed"] += 1
                print(f"[import] package {row['source_id']}: {exc}", file=sys.stderr)
                continue
            if meta.get("http"):
                stats["failed"] += 1
                print(f"[import] package {row['source_id']}: HTTP {meta['http']}",
                      file=sys.stderr)
                continue
            if not questions:
                stats["missing"] += 1
                print(f"[import] package {row['source_id']}: no questions parsed "
                      f"(site says {meta.get('Savollar soni', '?')})", file=sys.stderr)
                continue
            if meta.get("title") and not package["title"]:
                package["title"] = meta["title"]
            if meta.get("Sana") and not package["date"]:
                package["date"] = meta["Sana"]
            if meta.get("Turlar"):
                try:
                    package["rounds"] = int(re.sub(r"\D", "", str(meta["Turlar"])) or 0)
                except ValueError:
                    pass
            if meta.get("note"):
                package["note"] = meta["note"][:600]

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
            package["images"] = sum(1 for item in fresh if item.get("attachments"))
            packages.append(package)
            buffer_packages.append(package)
            buffer_rows.extend(fresh)
            sample.extend(fresh[:1])
            stats["packages"] += 1
            stats["questions"] += len(fresh)
            stats["images"] += package["images"]
            if len(buffer_packages) >= SHARD_PACKAGES:
                shards.append(flush_shard(out_dir, buffer_packages, buffer_rows,
                                          index, compress))
                index += 1
                buffer_packages, buffer_rows = [], []
                manifest["packages"], manifest["shards"] = packages, shards
                save_manifest(out_dir, manifest)
                print(f"[import] {stats['packages']} packages · "
                      f"{stats['questions']} questions · {len(shards)} shards · "
                      f"{stats['duplicates']} duplicates skipped")

    if buffer_packages:
        shards.append(flush_shard(out_dir, buffer_packages, buffer_rows, index, compress))
    manifest["packages"], manifest["shards"] = packages, shards
    save_manifest(out_dir, manifest)
    print(f"[import] done: {dict(stats)}")
    return dict(stats), sample


def verify(sample_pages: int, per_page: int, source: str, workers: int,
           polite: float, start_page: int = 1) -> tuple[dict, list[dict]]:
    """Does the parser return every question the site says a package has?

    The catalogue header advertises a site-wide question total that is far
    bigger than the sum over its packages, so this walks a spread of list pages
    and compares three numbers for each package: the count on the catalogue
    card, the ``Savollar soni`` fact on the package page, and what
    ``parse_package`` actually returns. A package page that hid part of its
    questions behind a "load more" would show up here as ``short``.
    """
    query: dict[str, object] = {"page": start_page}
    if SOURCES.get(source):
        query["manba"] = SOURCES[source]

    def list_url(page: int) -> str:
        return f"{SITE}/paketlar?" + urlencode(dict(query, page=page))

    status, markup = fetch(list_url(start_page))
    if status != 200:
        print(f"[verify] list page {start_page}: HTTP {status}", file=sys.stderr)
        return {"checked": 0, "failed": 1}, []
    last_page = list_page_count(markup) or start_page
    total = list_total(markup) or 0
    print(f"[verify] catalogue: {last_page} pages · {total or '?'} packages announced "
          f"(manba={source})")

    sample_pages = max(1, int(sample_pages))
    if last_page > start_page:
        step = (last_page - start_page) / max(1, sample_pages - 1) if sample_pages > 1 else 0
        wanted = sorted({min(last_page, start_page + round(step * i))
                         for i in range(sample_pages)})
    else:
        wanted = [start_page]

    rows: list[dict] = []
    for page in wanted:
        try:
            status, markup = fetch(list_url(page))
        except RuntimeError as exc:
            print(f"[verify] list page {page}: {exc}", file=sys.stderr)
            continue
        if status != 200:
            print(f"[verify] list page {page}: HTTP {status}", file=sys.stderr)
            continue
        cards = parse_list(markup)[:max(1, per_page)]
        print(f"[verify] list page {page}: {len(cards)} of {per_page} cards")
        rows.extend(cards)

    stats = {"checked": 0, "failed": 0, "empty": 0, "short": 0, "multi_tour": 0,
             "announced": 0, "parsed": 0, "card_mismatch": 0}
    problems: list[dict] = []

    def one(row: dict) -> dict:
        source_id = int(row["source_id"])
        result = {"source_id": source_id, "card": _digits(row.get("questions")),
                  "page": 0, "parsed": 0, "tours": 1, "http": 200}
        _package, meta, questions = fetch_package(source_id, {"id": ID_OFFSET + source_id,
                                                              "source_id": source_id})
        result["http"] = int(meta.get("http") or 200)
        result["page"] = _digits(meta.get("Savollar soni"))
        result["parsed"] = len(questions)
        result["tours"] = int(meta.get("tours") or 1)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, row): row for row in rows}
        for row in rows:
            time.sleep(polite / max(1, workers))
        for future in as_completed(futures):
            row = futures[future]
            try:
                found = future.result()
            except Exception as exc:                            # noqa: BLE001
                stats["failed"] += 1
                print(f"[verify] package {row['source_id']}: {exc}", file=sys.stderr)
                continue
            if found.get("http") != 200:
                stats["failed"] += 1
                print(f"[verify] package {found['source_id']}: HTTP {found['http']}",
                      file=sys.stderr)
                continue
            stats["checked"] += 1
            announced = found["page"] or found["card"]
            stats["announced"] += announced
            stats["parsed"] += found["parsed"]
            if found["page"] and found["card"] and found["page"] != found["card"]:
                stats["card_mismatch"] += 1
            if found["tours"] > 1:
                stats["multi_tour"] += 1
            if not found["parsed"]:
                stats["empty"] += 1
            if found["parsed"] < announced:
                stats["short"] += 1
                problems.append(found)
                print(f"[verify] package {found['source_id']}: the site says {announced} "
                      f"questions, {found['tours']} tour page(s) returned {found['parsed']}",
                      file=sys.stderr)

    print(f"[verify] checked {stats['checked']} packages · announced {stats['announced']} "
          f"questions · parsed {stats['parsed']} · short {stats['short']} · "
          f"empty {stats['empty']} · failed {stats['failed']} · "
          f"card/page disagreement {stats['card_mismatch']} · "
          f"read over several tour pages {stats['multi_tour']}")
    if stats["checked"] and stats["announced"]:
        ratio = stats["parsed"] / stats["announced"]
        print(f"[verify] the parser keeps {ratio:.1%} of what those pages announce; "
              f"over the whole catalogue that is {int(total * stats['parsed'] / max(1, stats['checked']))} "
              f"questions in {total or '?'} packages")
    return stats, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("probe", "check", "verify", "import"),
                        default="import")
    parser.add_argument("--out-dir", default="data/turnirlar")
    parser.add_argument("--probe-dir", default="/tmp/turnirlar-probe")
    parser.add_argument("--probe-package", type=int, default=7800)
    parser.add_argument("--probe-package2", type=int, default=0,
                        help="a second package to probe (different shape)")
    parser.add_argument("--probe-page", type=int, default=1)
    parser.add_argument("--max-packages", type=int, default=0, help="0 = the whole base")
    parser.add_argument("--pages", type=int, default=0, help="stop after N list pages")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--source", choices=sorted(SOURCES), default="all",
                        help="manba filter of the catalogue")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--polite", type=float, default=0.4,
                        help="seconds of delay spread over the workers")
    parser.add_argument("--sample", default="", help="write a small readable sample here")
    parser.add_argument("--verify-pages", type=int, default=6,
                        help="how many catalogue pages the verify mode samples")
    parser.add_argument("--verify-per-page", type=int, default=8,
                        help="how many packages to re-check on each sampled page")
    parser.add_argument("--no-gzip", action="store_true")
    parser.add_argument("--no-dedupe", action="store_true")
    args = parser.parse_args()

    if args.mode == "probe":
        packages = [args.probe_package] + ([args.probe_package2] if args.probe_package2 else [])
        failures = probe(Path(args.probe_dir), packages, args.probe_page)
        return 1 if failures else 0

    if args.mode == "check":
        return check(Path(args.probe_dir), Path(args.sample) if args.sample else None)

    if args.mode == "verify":
        found, _problems = verify(args.verify_pages, args.verify_per_page, args.source,
                                  args.workers, args.polite, args.start_page)
        return 1 if found.get("short") or found.get("failed") else 0

    out_dir = Path(args.out_dir)
    dedupe = [] if args.no_dedupe else sorted(Path("data/all/questions").glob("*.json"))
    rows = list_all_packages(args.pages, args.start_page, args.workers,
                             args.max_packages, args.source)
    if not rows:
        print("[import] no packages found", file=sys.stderr)
        return 1
    stats, sample = import_packages(rows, out_dir, args.workers, not args.no_gzip,
                                    dedupe, args.polite)
    if args.sample:
        write_sample(Path(args.sample), sample, rows)
    return 0 if stats.get("packages") or stats.get("empty") else 1


if __name__ == "__main__":
    raise SystemExit(main())
