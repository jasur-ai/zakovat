"""Question archive access layer.

Two sources, one interface:

* ``data/all`` — the zakovatklubi.uz export written by scripts/export_all.py.
  Its shards ship with the deployment, so the whole base is read from disk;
* ``data/turnirlar`` — the turnirlar.uz export written by
  scripts/export_turnirlar.py: ~7 800 packages, ~500 000 questions. Only its
  manifest (~2 MB) is deployed; the gzipped shards live in the GitHub Release
  ``savollar-bazasi`` and are fetched when a package from there is actually
  played. Fetched shards are cached in /tmp and two of them are kept in memory,
  so a serverless instance never holds the whole base.

A shard that cannot be fetched yields no questions instead of raising: the bot
keeps working on the local archive. When neither export is present, the small
959-package snapshot is used.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import random
import sys
from collections import OrderedDict
from pathlib import Path
from urllib.request import Request, urlopen

SNAPSHOT_PACKAGE_ID = 959
LOCAL_ROOT = "data/all"
REMOTE_ROOT = "data/turnirlar"

# scripts/export_turnirlar.py offsets turnirlar.uz ids so that both archives can
# live in one id space: package 7800 → 907800, question 559880 → 900559880.
REMOTE_PACKAGE_FLOOR = 900_000
REMOTE_QUESTION_FLOOR = 900_000_000

SHARDS_IN_MEMORY = 2        # ~11 000 questions each; the rest stays on disk
FETCH_TIMEOUT = 12          # seconds; a slow shard must not eat a whole request
USER_AGENT = "zakovat-bot/1.0 (+https://github.com/jasur-ai/zakovat)"
CACHE_DIR_ENV = "ZAKOVAT_TURNIRLAR_CACHE"
BASE_URL_ENV = "ZAKOVAT_TURNIRLAR_BASE"
DEFAULT_CACHE_DIR = "/tmp/zakovat-turnirlar"


def read_questions(path: Path) -> list[dict]:
    """The rows of one shard file, gzipped or not, empty when unreadable."""
    if not path.exists():
        return []
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows = json.load(handle)
        else:
            with path.open(encoding="utf-8") as handle:
                rows = json.load(handle)
    except (OSError, ValueError, EOFError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def decode_questions(payload: bytes) -> list[dict]:
    """Rows out of downloaded bytes, compressed or not."""
    if not payload:
        return []
    try:
        if payload[:2] == b"\x1f\x8b":
            payload = gzip.GzipFile(fileobj=io.BytesIO(payload)).read()
        rows = json.loads(payload.decode("utf-8", errors="replace"))
    except (OSError, ValueError, EOFError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def download(url: str, timeout: int = FETCH_TIMEOUT) -> bytes:
    """GET a shard. Redirects (GitHub Release → object storage) are followed."""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(request, timeout=timeout) as response:      # noqa: S310 - fixed https host
        return response.read()


class Archive:
    def __init__(self, root: str | Path = LOCAL_ROOT,
                 snapshot: str | Path = "data/zakovat-959.flat.json",
                 turnirlar: str | Path | None = REMOTE_ROOT,
                 base_url: str = "", fetcher=None, cache_dir: str | Path = ""):
        self.root = Path(root)
        self.snapshot = Path(snapshot)
        manifest_path = self.root / "manifest.json"
        if manifest_path.exists():
            with manifest_path.open(encoding="utf-8") as fh:
                self.manifest = json.load(fh)
        else:
            self.manifest = {
                "package_count": 1,
                "question_count": 0,
                "shards": [],
                "packages": [{"id": SNAPSHOT_PACKAGE_ID, "title": "Zakovat 959", "questions": 0}],
                "source": str(self.snapshot),
            }
        # The local manifest stays the one callers introspect (stats sections,
        # export date); the remote one is merged in only where it is useful.
        self.remote_root = Path(turnirlar) if turnirlar else None
        self.remote_manifest = self._read_remote_manifest()
        self.remote_packages = {int(row["id"]): row
                                for row in self.remote_manifest.get("packages", [])
                                if isinstance(row, dict) and row.get("id")}
        self.remote_shards = {int(row["index"]): row
                              for row in self.remote_manifest.get("shards", [])
                              if isinstance(row, dict) and row.get("index")}
        self.remote_base = (base_url or os.environ.get(BASE_URL_ENV, "")
                            or str(self.remote_manifest.get("base_url") or ""))
        self.cache_dir = Path(cache_dir or os.environ.get(CACHE_DIR_ENV, DEFAULT_CACHE_DIR))
        self._fetch = fetcher or download
        # Recorded question-id range of the whole remote base: an id outside it
        # cannot be there, so it must never trigger a shard download.
        ranges = [(int(row.get("min_question_id") or 0), int(row.get("max_question_id") or 0))
                  for row in self.remote_shards.values()]
        self.remote_question_range = ((min(low for low, _ in ranges),
                                       max(high for _, high in ranges))
                                      if ranges else (0, 0))

        self.packages = {int(p["id"]): p for p in self.manifest.get("packages", [])}
        self.packages.update(self.remote_packages)
        self._by_package: dict[int, list[dict]] | None = None
        self._question_index: dict[int, dict] | None = None
        self._shard_cache: OrderedDict[str, dict] = OrderedDict()

    # ------------------------------------------------------------ local source
    def _read_remote_manifest(self) -> dict:
        if not self.remote_root:
            return {}
        path = self.remote_root / "manifest.json"
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _load(self) -> dict[int, list[dict]]:
        if self._by_package is None:
            by_package: dict[int, list[dict]] = {}
            for shard in self.manifest.get("shards", []):
                path = self.root / shard
                with path.open(encoding="utf-8") as fh:
                    for question in json.load(fh):
                        by_package.setdefault(int(question["package_id"]), []).append(question)
            if not by_package and self.snapshot.exists():
                with self.snapshot.open(encoding="utf-8") as fh:
                    by_package[SNAPSHOT_PACKAGE_ID] = json.load(fh)
            for package_id, questions in by_package.items():
                if package_id in self.packages:
                    self.packages[package_id]["questions"] = len(questions)
            self._by_package = by_package
        return self._by_package

    # ----------------------------------------------------------- remote source
    def is_remote(self, package_id: int) -> bool:
        """True when the package comes from turnirlar.uz (shard fetched on use)."""
        return int(package_id) in self.remote_packages

    def remote_available(self) -> bool:
        return bool(self.remote_packages and self.remote_shards)

    def _shard_entry(self, index: int) -> dict:
        """One remote shard, indexed by package and by question id, LRU-cached."""
        empty = {"rows": [], "by_package": {}, "by_id": {}}
        shard = self.remote_shards.get(int(index))
        if not shard:
            return empty
        name = str(shard.get("file") or "").rsplit("/", 1)[-1] or f"turnirlar-{int(index):04d}.json.gz"
        cached = self._shard_cache.get(name)
        if cached is not None:
            self._shard_cache.move_to_end(name)
            return cached
        rows = self._shard_rows(shard, name)
        entry = {"rows": rows, "by_package": {}, "by_id": {}}
        for row in rows:
            try:
                entry["by_package"].setdefault(int(row.get("package_id") or 0), []).append(row)
                entry["by_id"][int(row.get("id") or 0)] = row
            except (TypeError, ValueError):
                continue
        self._shard_cache[name] = entry
        while len(self._shard_cache) > SHARDS_IN_MEMORY:
            self._shard_cache.popitem(last=False)
        return entry

    def _shard_rows(self, shard: dict, name: str) -> list[dict]:
        """Disk copy first (development, CI), then /tmp, then the network."""
        relative = str(shard.get("file") or f"shards/{name}")
        if self.remote_root:
            rows = read_questions(self.remote_root / relative)
            if rows:
                return rows
        local = self.cache_dir / name
        rows = read_questions(local)
        if rows:
            return rows
        if not self.remote_base:
            return []
        try:
            payload = self._fetch(self.remote_base + name)
        except Exception as exc:                                   # noqa: BLE001
            print(f"[archive] shard {name}: {exc}", file=sys.stderr)
            return []
        rows = decode_questions(payload)
        if rows:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                local.write_bytes(payload)
            except OSError:
                pass                                               # /tmp is a bonus
        return rows

    def _shard_for_package(self, package_id: int) -> int:
        row = self.remote_packages.get(int(package_id)) or {}
        try:
            return int(row.get("shard") or 0)
        except (TypeError, ValueError):
            return 0

    def _shards_for_question(self, question_id: int) -> list[int]:
        """Which shard can hold this question, from the recorded id ranges."""
        source_id = int(question_id) - REMOTE_QUESTION_FLOOR
        low, high = self.remote_question_range
        if not self.remote_shards or source_id < low or source_id > high:
            return []
        inside = []
        for index, shard in self.remote_shards.items():
            low = int(shard.get("min_question_id") or 0)
            high = int(shard.get("max_question_id") or 0)
            if low <= source_id <= high:
                inside.append(index)
        if inside:
            return sorted(inside)
        # Ranges only cover stored questions, so a near miss is worth one try.
        nearest = min(self.remote_shards, key=lambda index: min(
            abs(source_id - int(self.remote_shards[index].get("min_question_id") or 0)),
            abs(source_id - int(self.remote_shards[index].get("max_question_id") or 0))))
        return [nearest]

    # ------------------------------------------------------------------- reads
    def questions(self, package_id: int) -> list[dict]:
        package_id = int(package_id)
        if self.is_remote(package_id):
            index = self._shard_for_package(package_id)
            if not index:
                return []
            return list(self._shard_entry(index)["by_package"].get(package_id, []))
        return list(self._load().get(package_id, []))

    def question(self, question_id: int) -> dict | None:
        """Look up one question by id (used by stateless webhook sessions)."""
        question_id = int(question_id)
        if self._question_index is None:
            self._question_index = {
                int(q["id"]): q
                for questions in self._load().values()
                for q in questions
            }
        local = self._question_index.get(question_id)
        if local:
            return local
        if question_id >= REMOTE_QUESTION_FLOOR:
            for index in self._shards_for_question(question_id):
                row = self._shard_entry(index)["by_id"].get(question_id)
                if row:
                    return row
        return None

    def package(self, package_id: int) -> dict | None:
        package = self.packages.get(int(package_id))
        if not package:
            return None
        result = dict(package)
        if not self.is_remote(package_id):
            # counting a remote package would mean downloading its shard
            result["questions"] = len(self.questions(package_id))
        return result

    def search(self, query: str, limit: int = 12) -> list[dict]:
        """Case-insensitive title search over both sources, newest first."""
        needle = (query or "").strip().casefold()
        ids = sorted(self.packages, reverse=True)
        if needle:
            ids = [pid for pid in ids if needle in (self.packages[pid].get("title") or "").casefold()]
        result = []
        for pid in ids[:limit]:
            package = self.package(pid)
            if package and package.get("questions"):
                result.append(package)
        return result

    def package_info(self, package_id: int) -> dict | None:
        """Manifest entry only — no shard loading, so it is fast on serverless."""
        info = self.packages.get(int(package_id))
        return dict(info) if info else None

    def list_packages(self, query: str = "", page: int = 1, per_page: int = 12) -> dict:
        """Every package of both sources (newest first), one page at a time.

        Uses the manifest counts, so it never has to load a shard — important on
        serverless, where the two archives together are half a gigabyte.
        """
        needle = (query or "").strip().casefold()
        ids = sorted(self.packages, reverse=True)
        if needle:
            ids = [pid for pid in ids if needle in (self.packages[pid].get("title") or "").casefold()]
        per_page = max(1, int(per_page))
        pages = max(1, -(-len(ids) // per_page))
        page = min(max(1, int(page)), pages)
        start = (page - 1) * per_page
        items = [self.packages[pid] for pid in ids[start:start + per_page]]
        return {"page": page, "pages": pages, "count": len(ids), "items": items, "start": start}

    def random_package_id(self, remote: bool = False) -> int:
        """Pick a package that has questions.

        Local packages only by default: a random game starts instantly, while a
        remote one would first have to fetch its shard. ``remote=True`` opens the
        whole base (that is what the turnirlar.uz half is for).
        """
        usable = [pid for pid, info in self.packages.items() if info.get("questions", 0)]
        if not remote:
            local = [pid for pid in usable if not self.is_remote(pid)]
            if local:
                usable = local
        if not usable:
            usable = list(self._load())
        return random.choice(usable) if usable else SNAPSHOT_PACKAGE_ID

    # ------------------------------------------------------------------ totals
    def counts(self) -> tuple[int, int]:
        """Packages and questions of both sources together."""
        local_packages, local_questions = self.local_counts()
        remote = self.remote_manifest
        return (local_packages + int(remote.get("package_count") or len(self.remote_packages)),
                local_questions + int(remote.get("question_count") or 0))

    def local_counts(self) -> tuple[int, int]:
        return (self.manifest.get("package_count", 0), self.manifest.get("question_count", 0))

    def welcome_sources(self) -> list[tuple[str, int, int]]:
        """(name, packages, questions) per source — only when there is more than one."""
        counts = self.source_counts()
        rows = [(label, int(counts[key]["packages"]), int(counts[key]["questions"]))
                for key, label in (("zakovatklubi", "zakovatklubi.uz"),
                                   ("turnirlar", "turnirlar.uz"))
                if counts.get(key, {}).get("packages")]
        return rows if len(rows) > 1 else []

    def source_counts(self) -> dict[str, dict]:
        """Per-source totals, for the status page and the welcome message."""
        local_packages, local_questions = self.local_counts()
        result = {
            "zakovatklubi": {"packages": int(local_packages), "questions": int(local_questions),
                             "stored": "local"},
        }
        if self.remote_manifest or self.remote_packages:
            remote = self.remote_manifest
            result["turnirlar"] = {
                "packages": int(remote.get("package_count") or len(self.remote_packages)),
                "questions": int(remote.get("question_count") or 0),
                "images": int(remote.get("with_images") or 0),
                "shards": len(self.remote_shards),
                "exported_at": remote.get("exported_at") or "",
                "stored": "release",
            }
        return result
