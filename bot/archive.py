"""Question archive access layer.

Reads the full export written by scripts/export_all.py (data/all/) and falls
back to the small 959-package snapshot if the full archive is absent.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SNAPSHOT_PACKAGE_ID = 959


class Archive:
    def __init__(self, root: str | Path = "data/all", snapshot: str | Path = "data/zakovat-959.flat.json"):
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
        self.packages = {int(p["id"]): p for p in self.manifest.get("packages", [])}
        self._by_package: dict[int, list[dict]] | None = None
        self._question_index: dict[int, dict] | None = None

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

    def questions(self, package_id: int) -> list[dict]:
        return list(self._load().get(int(package_id), []))

    def question(self, question_id: int) -> dict | None:
        """Look up one question by id (used by stateless webhook sessions)."""
        if self._question_index is None:
            self._question_index = {
                int(q["id"]): q
                for questions in self._load().values()
                for q in questions
            }
        return self._question_index.get(int(question_id))

    def package(self, package_id: int) -> dict | None:
        package = self.packages.get(int(package_id))
        if not package:
            return None
        result = dict(package)
        result["questions"] = len(self.questions(package_id))
        return result

    def search(self, query: str, limit: int = 12) -> list[dict]:
        """Case-insensitive title search; most recent packages first."""
        needle = (query or "").strip().casefold()
        ids = sorted(self.packages, reverse=True)
        if needle:
            ids = [pid for pid in ids if needle in (self.packages[pid].get("title") or "").casefold()]
        result = []
        for pid in ids[:limit]:
            package = self.package(pid)
            if package and package["questions"]:
                result.append(package)
        return result

    def package_info(self, package_id: int) -> dict | None:
        """Manifest entry only — no shard loading, so it is fast on serverless."""
        info = self.packages.get(int(package_id))
        return dict(info) if info else None

    def list_packages(self, query: str = "", page: int = 1, per_page: int = 12) -> dict:
        """Every package (newest first), one page at a time.

        Uses the manifest counts, so it never has to load the question shards —
        important on serverless where the archive is 27 MB.
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

    def random_package_id(self) -> int:
        """Pick a package that actually has questions."""
        available = [pid for pid, info in self.packages.items() if info.get("questions", 0)]
        if not available:
            available = list(self._load())
        return random.choice(available) if available else SNAPSHOT_PACKAGE_ID

    def counts(self) -> tuple[int, int]:
        return self.manifest.get("package_count", 0), self.manifest.get("question_count", 0)
