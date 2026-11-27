"""Tests for the two-source archive: local shards plus the fetched turnirlar base.

The remote half is exercised with synthetic manifests and a fake fetcher, so
nothing here touches the network — the point is exactly what happens when the
network is slow, empty or broken.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import random
import shutil
import tempfile
import unittest

from bot.archive import (REMOTE_PACKAGE_FLOOR, REMOTE_QUESTION_FLOOR, SHARDS_IN_MEMORY,
                         Archive, decode_questions, read_questions)

LOCAL_QUESTIONS = [
    {"id": 11, "question_id": 1, "package_id": 5, "package_title": "Mahalliy paket",
     "text": "Mahalliy savol", "answer": "Mahalliy javob", "comment": "", "attachments": []},
    {"id": 12, "question_id": 2, "package_id": 5, "package_title": "Mahalliy paket",
     "text": "Ikkinchi savol", "answer": "Ikkinchi javob", "comment": "", "attachments": []},
]


def build_local(root: pathlib.Path) -> None:
    (root / "questions").mkdir(parents=True, exist_ok=True)
    (root / "questions" / "questions-0001.json").write_text(
        json.dumps(LOCAL_QUESTIONS, ensure_ascii=False), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "source": "https://zakovatklubi.uz",
        "exported_at": "2026-11-26T12:15:54+00:00",
        "package_count": 1,
        "question_count": len(LOCAL_QUESTIONS),
        "shards": ["questions/questions-0001.json"],
        "packages": [{"id": 5, "title": "Mahalliy paket", "questions": len(LOCAL_QUESTIONS)}],
    }, ensure_ascii=False), encoding="utf-8")


def remote_row(source_id: int, shard: int, questions: int = 3, title: str = "") -> dict:
    return {
        "id": REMOTE_PACKAGE_FLOOR + source_id,
        "source_id": source_id,
        "title": title or f"Turnirlar to‘plami {source_id}",
        "questions": questions,
        "rounds": 2,
        "date": "16.11.2025",
        "section": "Professional",
        "origin": "Boshqa manba",
        "url": f"https://turnirlar.uz/savollar/paket/{source_id}",
        "images": 0,
        "shard": shard,
    }


def remote_question(package_source_id: int, question_id: int, index: int) -> dict:
    return {
        "id": REMOTE_QUESTION_FLOOR + question_id,
        "question_id": question_id,
        "package_id": REMOTE_PACKAGE_FLOOR + package_source_id,
        "index": index,
        "round": 1,
        "text": f"Uzoq savol {question_id}",
        "answer": f"Uzoq javob {question_id}",
        "created_at": 1763251200,
    }


def build_remote(root: pathlib.Path, shards: int = 2, write_files: bool = False,
                 compress: bool = True) -> dict:
    """A manifest with two packages per shard; the files only when asked.

    Returns the manifest and the rows of every shard, so a test can serve the
    very same bytes over its fake network. Package ids are 11, 12, 21, 22, …
    and question ids run from 101 upwards, one shard after another.
    """
    rows: list[tuple[int, dict]] = []
    packages = []
    shard_rows = []
    question_id = 100
    for shard in range(1, shards + 1):
        shard_rows.append({"file": f"shards/turnirlar-{shard:04d}.json.gz", "index": shard,
                           "packages": [], "questions": 0,
                           "min_question_id": 0, "max_question_id": 0, "bytes": 0})
        ids: list[int] = []
        for package in range(2):
            source_id = shard * 10 + package + 1
            shard_rows[-1]["packages"].append(source_id)
            for index in range(1, 4):
                question_id += 1
                ids.append(question_id)
                rows.append((shard, remote_question(source_id, question_id, index)))
            packages.append(remote_row(source_id, shard))
        shard_rows[-1]["questions"] = len(ids)
        shard_rows[-1]["min_question_id"] = min(ids)
        shard_rows[-1]["max_question_id"] = max(ids)
    manifest = {
        "source": "https://turnirlar.uz/savollar",
        "id_offset": REMOTE_PACKAGE_FLOOR,
        "base_url": "https://example.test/releases/download/savollar-bazasi/",
        "exported_at": "2026-11-27T18:00:00+00:00",
        "package_count": len(packages),
        "question_count": len(rows),
        "with_images": 0,
        "packages": packages,
        "shards": shard_rows,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    by_shard = {number: [row for index, row in rows if index == number]
                for number in range(1, shards + 1)}
    if write_files:
        (root / "shards").mkdir(parents=True, exist_ok=True)
        for number, shard_rows_list in by_shard.items():
            payload = json.dumps(shard_rows_list, ensure_ascii=False).encode("utf-8")
            name = f"turnirlar-{number:04d}.json" + (".gz" if compress else "")
            path = root / "shards" / name
            if compress:
                with gzip.open(path, "wb") as handle:
                    handle.write(payload)
            else:
                path.write_bytes(payload)
    return {"manifest": manifest, "shard_rows": by_shard}


def payloads_for(built: dict, only: int = 0) -> dict[str, bytes]:
    """Gzipped shard bytes keyed by file name, as the Release would serve them."""
    payloads = {}
    for shard in built["manifest"]["shards"]:
        number = int(shard["index"])
        if only and number > only:
            continue
        name = str(shard["file"]).rsplit("/", 1)[-1]
        payloads[name] = gzip.compress(
            json.dumps(built["shard_rows"][number], ensure_ascii=False).encode("utf-8"), 9)
    return payloads


class Fetcher:
    """Stands in for the network: counts calls, can fail, can serve shards."""

    def __init__(self, payloads: dict[str, bytes] | None = None, fail: bool = False):
        self.payloads = payloads or {}
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.fail:
            raise OSError("no route to the release")
        name = url.rsplit("/", 1)[-1]
        if name in self.payloads:
            return self.payloads[name]
        raise OSError(f"unknown shard {name}")


class TempArchiveCase(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="archive-test-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.local = self.base / "all"
        self.remote = self.base / "turnirlar"
        self.cache = self.base / "cache"
        build_local(self.local)

    def make(self, fetcher=None, **kwargs) -> Archive:
        kwargs.setdefault("cache_dir", self.cache)
        return Archive(self.local, self.base / "snapshot.json", self.remote,
                       fetcher=fetcher, **kwargs)


class LocalOnlyTest(TempArchiveCase):
    def test_without_a_remote_manifest_nothing_changes(self):
        archive = self.make()
        self.assertEqual(archive.counts(), (1, 2))
        self.assertEqual(archive.local_counts(), (1, 2))
        self.assertFalse(archive.remote_available())
        self.assertEqual([row["id"] for row in archive.questions(5)], [11, 12])
        self.assertEqual(archive.question(12)["answer"], "Ikkinchi javob")
        self.assertIsNone(archive.question(999))
        self.assertEqual(archive.random_package_id(), 5)
        self.assertEqual(list(archive.source_counts()), ["zakovatklubi"])

    def test_a_missing_local_manifest_falls_back_to_the_snapshot(self):
        snapshot = self.base / "snapshot.json"
        snapshot.write_text(json.dumps([LOCAL_QUESTIONS[0]]), encoding="utf-8")
        archive = Archive(self.base / "nothing", snapshot, None, cache_dir=self.cache)
        self.assertEqual(archive.random_package_id(), 959)
        self.assertEqual(len(archive.questions(959)), 1)


class MergedCatalogueTest(TempArchiveCase):
    def setUp(self):
        super().setUp()
        self.fetcher = Fetcher()
        build_remote(self.remote)
        self.archive = self.make(self.fetcher)

    def test_both_sources_share_one_catalogue(self):
        self.assertEqual(self.archive.counts(), (5, 14))
        self.assertEqual(self.archive.local_counts(), (1, 2))
        result = self.archive.list_packages("", page=1, per_page=12)
        self.assertEqual(result["count"], 5)
        self.assertTrue(result["items"][0]["id"] > REMOTE_PACKAGE_FLOOR)   # newest first

    def test_source_counts_report_each_base(self):
        sources = self.archive.source_counts()
        self.assertEqual(sources["zakovatklubi"],
                         {"packages": 1, "questions": 2, "stored": "local",
                          "exported_at": "2026-11-26T12:15:54+00:00"})
        self.assertEqual(sources["turnirlar"]["packages"], 4)
        self.assertEqual(sources["turnirlar"]["questions"], 12)
        self.assertEqual(sources["turnirlar"]["shards"], 2)
        self.assertEqual(sources["turnirlar"]["stored"], "release")

    def test_listing_and_searching_never_fetch_a_shard(self):
        self.archive.list_packages("turnirlar", page=1, per_page=5)
        self.archive.search("to‘plami 12", limit=3)
        self.archive.package_info(REMOTE_PACKAGE_FLOOR + 12)
        self.archive.package(REMOTE_PACKAGE_FLOOR + 12)
        self.assertEqual(self.fetcher.calls, [])

    def test_search_finds_a_remote_title_and_keeps_its_manifest_count(self):
        found = self.archive.search("to‘plami 12", limit=3)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], REMOTE_PACKAGE_FLOOR + 12)
        self.assertEqual(found[0]["questions"], 3)
        self.assertEqual(found[0]["origin"], "Boshqa manba")

    def test_a_local_package_still_counts_its_loaded_questions(self):
        self.assertEqual(self.archive.package(5)["questions"], 2)

    def test_is_remote_tells_the_two_sources_apart(self):
        self.assertTrue(self.archive.is_remote(REMOTE_PACKAGE_FLOOR + 12))
        self.assertFalse(self.archive.is_remote(5))


class RemoteShardTest(TempArchiveCase):
    def setUp(self):
        super().setUp()
        self.built = build_remote(self.remote)

    def payloads(self) -> dict[str, bytes]:
        return payloads_for(self.built)

    def test_questions_of_a_remote_package_are_fetched_once(self):
        fetcher = Fetcher(self.payloads())
        archive = self.make(fetcher)
        package_id = REMOTE_PACKAGE_FLOOR + 11
        rows = archive.questions(package_id)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["package_id"] == package_id for row in rows))
        self.assertEqual(len(fetcher.calls), 1)
        self.assertTrue(fetcher.calls[0].endswith("turnirlar-0001.json.gz"))
        self.assertTrue(fetcher.calls[0].startswith("https://example.test/"))
        archive.questions(package_id)                 # served from memory
        archive.questions(REMOTE_PACKAGE_FLOOR + 12)  # same shard
        self.assertEqual(len(fetcher.calls), 1)

    def test_a_fetched_shard_is_cached_on_disk_for_the_next_instance(self):
        fetcher = Fetcher(self.payloads())
        self.make(fetcher).questions(REMOTE_PACKAGE_FLOOR + 11)
        self.assertTrue((self.cache / "turnirlar-0001.json.gz").exists())
        second = self.make(Fetcher(fail=True))        # network is gone now
        self.assertEqual(len(second.questions(REMOTE_PACKAGE_FLOOR + 11)), 3)

    def test_a_shard_on_disk_needs_no_network(self):
        build_remote(self.remote, write_files=True)
        archive = self.make(Fetcher(fail=True))
        self.assertEqual(len(archive.questions(REMOTE_PACKAGE_FLOOR + 11)), 3)

    def test_a_plain_json_shard_is_read_too(self):
        build_remote(self.remote, write_files=True, compress=False)
        manifest_path = self.remote / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for shard in manifest["shards"]:
            shard["file"] = shard["file"].replace(".json.gz", ".json")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        archive = self.make(Fetcher(fail=True))
        self.assertEqual(len(archive.questions(REMOTE_PACKAGE_FLOOR + 11)), 3)

    def test_one_question_is_found_by_its_shard_range(self):
        fetcher = Fetcher(self.payloads())
        archive = self.make(fetcher)
        manifest = json.loads((self.remote / "manifest.json").read_text(encoding="utf-8"))
        wanted = REMOTE_QUESTION_FLOOR + manifest["shards"][1]["min_question_id"]
        row = archive.question(wanted)
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], wanted)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertTrue(fetcher.calls[0].endswith("turnirlar-0002.json.gz"))

    def test_an_unknown_remote_question_stays_unknown(self):
        fetcher = Fetcher(self.payloads())
        archive = self.make(fetcher)
        self.assertIsNone(archive.question(REMOTE_QUESTION_FLOOR + 999_999))
        self.assertIsNone(archive.question(7))
        # outside the recorded range nothing is even looked for
        self.assertEqual(fetcher.calls, [])

    def test_a_wild_id_never_reaches_the_network(self):
        fetcher = Fetcher(self.payloads())
        archive = self.make(fetcher)
        for question_id in (999_999_999, REMOTE_QUESTION_FLOOR, REMOTE_QUESTION_FLOOR + 1,
                            10 ** 12, -5):
            self.assertIsNone(archive.question(question_id))
        self.assertEqual(fetcher.calls, [])

    def test_only_a_few_shards_are_kept_in_memory(self):
        total = SHARDS_IN_MEMORY + 2
        built = build_remote(self.remote, shards=total)
        archive = self.make(Fetcher(payloads_for(built)))
        for shard in range(1, total + 1):
            archive.questions(REMOTE_PACKAGE_FLOOR + shard * 10 + 1)
        self.assertLessEqual(len(archive._shard_cache), SHARDS_IN_MEMORY)

    def test_a_broken_network_yields_no_questions_instead_of_an_error(self):
        archive = self.make(Fetcher(fail=True))
        self.assertEqual(archive.questions(REMOTE_PACKAGE_FLOOR + 11), [])
        self.assertIsNone(archive.question(REMOTE_QUESTION_FLOOR + 101))
        # the local archive keeps working
        self.assertEqual(len(archive.questions(5)), 2)

    def test_a_package_without_a_shard_number_yields_nothing(self):
        archive = self.make(Fetcher(fail=True))
        archive.remote_packages[REMOTE_PACKAGE_FLOOR + 77] = remote_row(77, 0)
        archive.packages[REMOTE_PACKAGE_FLOOR + 77] = archive.remote_packages[REMOTE_PACKAGE_FLOOR + 77]
        self.assertEqual(archive.questions(REMOTE_PACKAGE_FLOOR + 77), [])

    def test_garbage_from_the_network_is_ignored(self):
        fetcher = Fetcher({"turnirlar-0001.json.gz": b"not a shard at all"})
        archive = self.make(fetcher)
        self.assertEqual(archive.questions(REMOTE_PACKAGE_FLOOR + 11), [])

    def test_an_unknown_package_is_still_none(self):
        archive = self.make(Fetcher(fail=True))
        self.assertIsNone(archive.package_info(424242))
        self.assertIsNone(archive.package(424242))


class RandomPickTest(TempArchiveCase):
    def setUp(self):
        super().setUp()
        build_remote(self.remote)
        self.archive = self.make(Fetcher(fail=True))

    def test_a_random_game_stays_on_the_local_base_by_default(self):
        random.seed(7)
        picks = {self.archive.random_package_id() for _ in range(40)}
        self.assertEqual(picks, {5})

    def test_the_whole_base_can_be_opened_on_request(self):
        random.seed(7)
        picks = {self.archive.random_package_id(remote=True) for _ in range(200)}
        self.assertIn(5, picks)
        self.assertTrue(any(pid > REMOTE_PACKAGE_FLOOR for pid in picks))

    def test_without_local_packages_anything_usable_is_picked(self):
        self.archive.manifest["packages"] = []
        self.archive.packages = dict(self.archive.remote_packages)
        pick = self.archive.random_package_id()
        self.assertTrue(pick > REMOTE_PACKAGE_FLOOR)


class DecoderTest(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": 1, "text": "savol", "answer": "javob"}]
        self.payload = json.dumps(self.rows, ensure_ascii=False).encode("utf-8")

    def test_plain_and_gzipped_bytes_decode_the_same(self):
        self.assertEqual(decode_questions(self.payload), self.rows)
        self.assertEqual(decode_questions(gzip.compress(self.payload)), self.rows)

    def test_broken_payloads_decode_to_nothing(self):
        for payload in (b"", b"\x1f\x8bbroken", b"{}", b"[1,2]", "str".encode()):
            rows = decode_questions(payload)
            self.assertIsInstance(rows, list)
            self.assertTrue(all(isinstance(row, dict) for row in rows))

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(read_questions(pathlib.Path("/nonexistent/shard.json.gz")), [])

    def test_a_file_shard_round_trips(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="archive-read-"))
        self.addCleanup(shutil.rmtree, root, True)
        plain = root / "shard.json"
        plain.write_text(json.dumps(self.rows, ensure_ascii=False), encoding="utf-8")
        packed = root / "shard.json.gz"
        with gzip.open(packed, "wb") as handle:
            handle.write(self.payload)
        self.assertEqual(read_questions(plain), self.rows)
        self.assertEqual(read_questions(packed), self.rows)


class RealArchiveTest(unittest.TestCase):
    """The shipped archive must keep behaving the way the bot expects."""

    @classmethod
    def setUpClass(cls):
        cls.archive = Archive("data/all", turnirlar="data/turnirlar",
                              cache_dir=pathlib.Path(tempfile.mkdtemp(prefix="archive-real-")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.archive.cache_dir, True)

    def test_the_local_export_is_intact(self):
        packages, questions = self.archive.local_counts()
        self.assertGreater(packages, 1000)
        self.assertGreater(questions, 20000)
        self.assertEqual(len(self.archive.questions(959)), 54)
        self.assertEqual(self.archive.question(23330)["answer"].strip().lower(), "robot")

    def test_counts_include_whatever_remote_manifest_is_deployed(self):
        total_packages, total_questions = self.archive.counts()
        local_packages, local_questions = self.archive.local_counts()
        remote = self.archive.source_counts().get("turnirlar", {"packages": 0, "questions": 0})
        self.assertEqual(total_packages, local_packages + remote["packages"])
        self.assertEqual(total_questions, local_questions + remote["questions"])

    def test_a_random_pick_never_needs_the_network(self):
        fetcher = Fetcher(fail=True)
        self.archive._fetch = fetcher
        for _ in range(20):
            package_id = self.archive.random_package_id()
            self.assertFalse(self.archive.is_remote(package_id))
        self.assertEqual(fetcher.calls, [])


if __name__ == "__main__":
    unittest.main()
