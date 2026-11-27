"""Tests for the turnirlar.uz exporter — offline, against fixture markup.

The fixtures mirror the real pages byte for byte where the parser looks: the
card classes of /paketlar, the article/tour structure of /paket/<id>, the label
italics of the answer block, and the Next.js flight payload that repeats the
whole page inside <script> (which a naive parser would read twice).
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import export_turnirlar as ex  # noqa: E402

LIST_PAGE = """<!doctype html><html><body>
<p class="zk-results-summary">7 798 ta töplam bazada mavjud</p>
<nav aria-label="Sahifalar" class="zk-pagination"><ul>
<li><a class="zk-pagination-page" aria-current="page" aria-label="1-sahifa" href="/savollar/paketlar">1</a></li>
<li><a class="zk-pagination-page" aria-label="2-sahifa" href="/savollar/paketlar?page=2">2</a></li>
<li><a class="zk-pagination-page" aria-label="260-sahifa" href="/savollar/paketlar?page=260">260</a></li>
</ul></nav>
<ul class="zk-catalog-list"><li>
<article class="zk-catalog-card"><h2 class="zk-catalog-card-title">
<a class="zk-catalog-card-link" href="/savollar/paket/7794">Professional va Birinçi liga kuzgi mavsumi 10-tur</a></h2>
<p class="zk-catalog-card-meta"><strong>36</strong> savol<!-- --> · 1 tur</p>
<p class="zk-catalog-card-meta">14.11.2026 · Professional</p>
<p class="zk-catalog-card-editors"><a class="zk-person-link" href="/savollar/profile/50896">Oyatillo Töxtaraliyev Ubaydullo öğli</a>, <a class="zk-person-link" href="/savollar/profile/112978">Dilşod Şaripov Şaxidjonoviç</a></p>
<p class="zk-catalog-card-foot">Zakovat klubi</p></article>
</li><li>
<article class="zk-catalog-card"><h2 class="zk-catalog-card-title">
<a class="zk-catalog-card-link" href="/savollar/paket/6001">Blits keça, Muallif: Boshqa</a></h2>
<p class="zk-catalog-card-meta"><strong>12</strong> savol<!-- --> · 3 tur</p>
<p class="zk-catalog-card-meta">02.03.2024 · Maktab · Toshkent</p>
<p class="zk-catalog-card-editors"><a class="zk-person-link" href="/savollar/profile/s9926">Hakim Batiraliyev</a></p>
<p class="zk-catalog-card-foot">Boshqa manba</p></article>
</li></ul>
<script>self.__next_f.push([1,"<article class=\\"zk-catalog-card\\"><a class=\\"zk-catalog-card-link\\" href=\\"/savollar/paket/9999\\">flight payload card</a>"])</script>
</body></html>"""

PACKAGE_PAGE = """<!doctype html><html><body>
<h1 class="zk-page-title">Özbekiston çempionati 2025 savollari</h1>
<dl class="zk-side-facts">
<div><dt>Savollar soni</dt><dd>3</dd></div>
<div><dt>Turlar</dt><dd>2</dd></div>
<div><dt>Sana</dt><dd>16.11.2025</dd></div>
<div><dt>Muharrir</dt><dd><a class="zk-person-link" href="/savollar/profile/111709">Saidakrom Rasulev Farxadoviç</a></dd></div>
</dl>
<p class="zk-package-note">Jamoalarga eslatma. Boşlovçi savol tartib raqamini eʼlon qiladi.</p>
<section class="zk-side-card zk-side-tours"><h2>Turlar</h2></section>
<div class="zk-package-tours">
<section id="tur-1" class="zk-tour">
<div class="zk-tour-heading"><h2 class="zk-tour-title">1-tur</h2><span class="zk-tour-count">2 savol</span></div>
<article id="savol-1" data-question-id="81aac2ce-2567-4f3a-9eb3-920f24ef2c93" class="zk-record zk-card zk-qcard" data-context="package" data-panel-ref="559880">
<div class="zk-question zk-question--package"><div class="zk-question-body">
<div class="zk-question-meta"><a class="zk-question-number" aria-label="1-savolni oçiş" title="Savolni oçiş" href="/savollar/savol/559880">Savol <!-- -->1</a></div>
<p class="zk-question-text">Mexanik turk oq figuralarda öynaydi va BU IŞNI QILADI.
<b class="zk-question-label">Savol:</b> Bu işni qiliş bilan almaştirilgan sözlarni yozib bering.</p>
</div></div>
<div class="zk-qcard-foot"><div class="zk-question-actions">
<button type="button" aria-label="Yoqdi" aria-pressed="false" title="Yoqdi · 7 kişi yoqtirgan"><span class="zk-like-count">7</span></button>
</div>
<details class="zk-answer"><summary><span class="zk-answer-show">Javobni oçiş</span></summary>
<div class="zk-answer-body">
<p><i class="zk-answer-label">Javob:</i> <!-- -->Birinçi yurişni amalga oşiradi.</p>
<p><i class="zk-answer-label">Qabul:</i> <!-- -->Ma’nodoş javoblar.</p>
<p class="zk-answer-explanation"><i class="zk-answer-label">Izoh:</i> <!-- -->Mexanik turk 1770-yilda yaratilgan.</p>
<div class="zk-sources"><i class="zk-answer-label">Manbalar:</i>
<div>1<!-- -->)<!-- --> <a href="https://en.wikipedia.org/wiki/Mechanical_Turk" target="_blank">https://en.wikipedia.org/wiki/Mechanical_Turk</a></div>
<div>2<!-- -->)<!-- --> Kitob, 42-bet</div></div>
<p class="zk-answer-credits"><span class="zk-question-credits">
<span class="zk-question-credit"><i class="zk-answer-label zk-question-credit-label">Muallif:</i> <span class="zk-author"><a class="zk-person-link" href="/savollar/profile/112978">Dilşod Şaripov Şaxidjonoviç</a></span></span>
<span class="zk-question-credit zk-editors"><i class="zk-answer-label zk-question-credit-label">Muharrir:</i> <span class="zk-author"><a class="zk-person-link" href="/savollar/profile/111709">Saidakrom Rasulev Farxadoviç</a></span></span>
</span></p>
</div></details></div></article>
<article id="savol-2" data-question-id="e74b4a32-ae0b-4d95-a8aa-dd933a5d6591" class="zk-record zk-card zk-qcard" data-context="package" data-panel-ref="559905">
<div class="zk-question zk-question--package"><div class="zk-question-body">
<div class="zk-question-meta"><a class="zk-question-number" href="/savollar/savol/559905">Savol <!-- -->2</a>
<span class="zk-question-badges zk-question-badges--inline"><span class="zk-badge zk-badge--brass"><svg aria-hidden="true"><rect width="18"></rect></svg>Rasmli</span><span class="zk-badge zk-badge--green">Duplet</span></span></div>
<figure class="zk-material-block"><figcaption class="zk-material-label"><span>Körgazmali material</span></figcaption>
<button type="button" class="zk-material-image-button" aria-label="Rasmni kattalaştiriş: Körgazmali material">
<img src="/savollar/api/media/pub/e74b4a32-ae0b-4d95-a8aa-dd933a5d6591/8dcf1f28.webp" alt="Körgazmali material" loading="lazy" class="zk-material-image" width="117" height="143"/></button>
<p class="zk-material-text">Back to the future</p></figure>
<p class="zk-question-text">Körgazmali Qarşingizda XV asr illyustratsiyasi.<br/>
<b class="zk-question-label">Savol:</b> Unda nima tasvirlangan?</p>
</div></div>
<div class="zk-qcard-foot"><div class="zk-question-actions">
<button type="button" title="Yoqdi · 0 kişi yoqtirgan"><span class="zk-like-count">0</span></button></div>
<details class="zk-answer"><summary><span class="zk-answer-show">Javobni oçiş</span></summary>
<div class="zk-answer-body">
<p><i class="zk-answer-label">Javob:</i> <!-- -->Plantagenetlarning şajarasi.</p>
<p><i class="zk-answer-label">Qabul:</i> <!-- -->Plantagenetlarning genealogik daraxti.</p>
<p class="zk-answer-credits"><span class="zk-question-credits">
<span class="zk-question-credit"><i class="zk-answer-label zk-question-credit-label">Mualliflar:</i> <span class="zk-author"><a class="zk-person-link" href="/savollar/profile/s9926">Hakim Batiraliyev</a>, <a class="zk-person-link" href="/savollar/profile/1291">Yan Sadkovskiy Igoreviç</a></span></span>
<span class="zk-question-credit zk-editors"><i class="zk-answer-label zk-question-credit-label">Muharrir:</i> <span class="zk-author"><a class="zk-person-link" href="/savollar/profile/111709">Saidakrom Rasulev Farxadoviç</a></span></span>
</span></p>
</div></details></div></article>
</section>
<section id="tur-2" class="zk-tour">
<div class="zk-tour-heading"><h2 class="zk-tour-title">2-tur</h2><span class="zk-tour-count">1 savol</span></div>
<article id="savol-3" data-question-id="11111111-2222-3333-4444-555555555555" class="zk-record zk-card zk-qcard" data-context="package" data-panel-ref="559920">
<div class="zk-question zk-question--package"><div class="zk-question-body">
<div class="zk-question-meta"><a class="zk-question-number" href="/savollar/savol/559920">Savol <!-- -->3</a></div>
<p class="zk-question-text">Uçinçi savol matni.</p>
</div></div>
<div class="zk-qcard-foot"><details class="zk-answer"><summary>Javobni oçiş</summary>
<div class="zk-answer-body"><p><i class="zk-answer-label">Javob:</i> <!-- -->Uçinçi javob.</p></div>
</details></div></article>
</section>
</div>
<script>self.__next_f.push([1,"{\\"children\\":\\"<article id=\\\\\\"savol-99\\\\\\" data-panel-ref=\\\\\\"1\\\\\\">Javob: flight payload decoy</article>\\"}"])</script>
</body></html>"""

PACKAGE = {"id": ex.ID_OFFSET + 7800, "source_id": 7800,
           "title": "O‘zbekiston chempionati 2025 savollari", "date": "16.11.2025",
           "section": "Professional", "origin": "Zakovat klubi"}


class OrthographyTest(unittest.TestCase):
    def test_lowercase_letters_become_standard_latin(self):
        self.assertEqual(ex.normalize_script("Birinçi yurişni amalga oşiradi."),
                         "Birinchi yurishni amalga oshiradi.")
        self.assertEqual(ex.normalize_script("öynaydi va ğalaba qozondi"),
                         "o‘ynaydi va g‘alaba qozondi")

    def test_capitalised_words_keep_their_capital(self):
        self.assertEqual(ex.normalize_script("Şaripov Şaxidjonoviç"),
                         "Sharipov Shaxidjonovich")
        self.assertEqual(ex.normalize_script("Özbekiston çempionati"),
                         "O‘zbekiston chempionati")
        self.assertEqual(ex.normalize_script("Ubaydullo öğli"), "Ubaydullo o‘g‘li")

    def test_all_caps_words_stay_all_caps(self):
        self.assertEqual(ex.normalize_script("BU IŞNI QILIDI"), "BU ISHNI QILIDI")
        self.assertEqual(ex.normalize_script("ALMAŞTIRILIŞ."), "ALMASHTIRILISH.")
        self.assertEqual(ex.normalize_script("O‘ZBEK TILINING LOTIN ALIFBOSI"),
                         "O‘ZBEK TILINING LOTIN ALIFBOSI")

    def test_mixed_text_is_decided_letter_by_letter(self):
        self.assertEqual(ex.normalize_script("Ikkinçi İKSga Çexov"),
                         "Ikkinchi IKSga Chexov")

    def test_apostrophes_are_left_as_the_site_writes_them(self):
        # "Ma’nodosh" keeps its tutuq belgisi; only modifier letters are mapped.
        self.assertEqual(ex.normalize_script("Ma’nodoş javoblar"),
                         "Ma’nodosh javoblar")
        self.assertEqual(ex.normalize_script("eʼlon qiladi"), "e‘lon qiladi")

    def test_cyrillic_and_other_scripts_survive(self):
        self.assertEqual(ex.normalize_script("Ықтимол ва Москва"),
                         "Ықтимол ва Москва")

    def test_clean_text_drops_tags_comments_and_extra_blanks(self):
        self.assertEqual(
            ex.clean_text('<b>Savol:</b> <!-- -->Birinçi<!-- --> yuriş.\n\n\n  Keyingi'),
            "Savol: Birinchi yurish.\n\nKeyingi")
        self.assertEqual(ex.clean_text("1&nbsp;&lt;2&gt; &amp; 3"), "1 <2> & 3")
        self.assertEqual(ex.clean_text('a<br/>b<br />c'), "a\nb\nc")

    def test_fingerprint_ignores_orthography_case_and_punctuation(self):
        pairs = (
            ("Birinçi yurişni amalga oşiradi.", "birinchi yurishni amalga oshiradi"),
            ("Özbekiston çempionati", "O‘zbekiston chempionati!"),
            ("Ne’mat Aminov", "Ne'mat   Aminov"),
            ("MA’NODOSH JAVOBLAR", "Ma’nodoş javoblar"),
        )
        for left, right in pairs:
            self.assertEqual(ex.fingerprint(left), ex.fingerprint(right), (left, right))

    def test_fingerprint_keeps_different_questions_apart(self):
        self.assertNotEqual(ex.fingerprint("Birinçi yuriş"), ex.fingerprint("Ikkinçi yuriş"))
        self.assertNotEqual(ex.fingerprint(""), ex.fingerprint("savol"))

    def test_epoch_of_reads_the_sites_date_format(self):
        self.assertEqual(ex.epoch_of("01.01.1970"), 0)
        self.assertEqual(ex.epoch_of("16.11.2025"), 1763251200)
        self.assertIsNone(ex.epoch_of(""))
        self.assertIsNone(ex.epoch_of("kecha"))

    def test_absolute_resolves_site_relative_media(self):
        self.assertEqual(ex.absolute("/savollar/api/media/pub/x.webp"),
                         "https://turnirlar.uz/savollar/api/media/pub/x.webp")
        self.assertEqual(ex.absolute("//cdn.example/x.webp"), "https://cdn.example/x.webp")
        self.assertEqual(ex.absolute("https://a.example/x"), "https://a.example/x")


class ListParsingTest(unittest.TestCase):
    def setUp(self):
        self.rows = ex.parse_list(LIST_PAGE)

    def test_cards_are_read_in_page_order(self):
        self.assertEqual([row["source_id"] for row in self.rows], [7794, 6001])

    def test_the_flight_payload_is_not_parsed_twice(self):
        self.assertNotIn(9999, [row["source_id"] for row in self.rows])

    def test_every_card_field(self):
        self.assertEqual(self.rows[0], {
            "source_id": 7794,
            "title": "Professional va Birinchi liga kuzgi mavsumi 10-tur",
            "questions": 36,
            "rounds": 1,
            "date": "14.11.2026",
            "section": "Professional",
            "authors": ["Oyatillo To‘xtaraliyev Ubaydullo o‘g‘li",
                        "Dilshod Sharipov Shaxidjonovich"],
            "origin": "Zakovat klubi",
        })

    def test_multi_part_sections_and_plural_rounds(self):
        row = self.rows[1]
        self.assertEqual(row["section"], "Maktab · Toshkent")
        self.assertEqual(row["rounds"], 3)
        self.assertEqual(row["origin"], "Boshqa manba")
        self.assertEqual(row["authors"], ["Hakim Batiraliyev"])

    def test_pagination_and_total(self):
        self.assertEqual(ex.list_page_count(LIST_PAGE), 260)
        self.assertEqual(ex.list_total(LIST_PAGE), 7798)
        self.assertEqual(ex.list_page_count("<html></html>"), 0)
        self.assertEqual(ex.list_total("<html></html>"), 0)

    def test_a_card_without_a_link_is_skipped(self):
        self.assertEqual(ex.parse_list('<article class="zk-catalog-card"></article>'), [])


class PackageParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.meta, cls.questions = ex.parse_package(PACKAGE_PAGE, PACKAGE)
        cls.by_id = {row["question_id"]: row for row in cls.questions}

    def test_package_meta(self):
        self.assertEqual(self.meta["title"], "O‘zbekiston chempionati 2025 savollari")
        self.assertEqual(self.meta["Savollar soni"], "3")
        self.assertEqual(self.meta["Turlar"], "2")
        self.assertEqual(self.meta["Sana"], "16.11.2025")
        self.assertEqual(self.meta["Muharrir"], "Saidakrom Rasulev Farxadovich")
        self.assertTrue(self.meta["note"].startswith("Jamoalarga eslatma."))
        self.assertEqual([tour["title"] for tour in self.meta["tours"]], ["1-tur", "2-tur"])

    def test_every_question_is_parsed_once(self):
        self.assertEqual([row["question_id"] for row in self.questions],
                         [559880, 559905, 559920])

    def test_flight_payload_decoy_is_ignored(self):
        self.assertNotIn("flight payload decoy", PACKAGE_PAGE and
                         json.dumps(self.questions, ensure_ascii=False))

    def test_ids_are_offset_so_both_archives_coexist(self):
        self.assertEqual(self.questions[0]["id"], 900_000_000 + 559880)
        self.assertEqual(self.questions[0]["package_id"], 900_000 + 7800)

    def test_rounds_follow_the_tour_sections(self):
        self.assertEqual([row["round"] for row in self.questions], [1, 1, 2])
        self.assertEqual([row["index"] for row in self.questions], [1, 2, 3])

    def test_question_text_is_normalised_and_keeps_the_savol_line(self):
        text = self.by_id[559880]["text"]
        self.assertIn("BU ISHNI QILADI", text)
        self.assertIn("Savol: Bu ishni qilish bilan almashtirilgan so‘zlarni", text)

    def test_answer_comment_and_credits(self):
        row = self.by_id[559880]
        self.assertEqual(row["answer"], "Birinchi yurishni amalga oshiradi.")
        self.assertEqual(row["comment"], "Mexanik turk 1770-yilda yaratilgan.")
        self.assertEqual(row["authors"], ["Dilshod Sharipov Shaxidjonovich"])
        self.assertEqual(row["sources"],
                         ["https://en.wikipedia.org/wiki/Mechanical_Turk", "Kitob, 42-bet"])
        self.assertEqual(row["likes"], 7)
        self.assertEqual(row["created_at"], 1763251200)

    def test_the_package_editor_is_not_repeated_on_every_question(self):
        self.assertNotIn("editors", self.by_id[559880])

    def test_a_remark_is_not_folded_into_the_answer(self):
        row = self.by_id[559880]
        self.assertEqual(row["accepted"], ["Ma’nodosh javoblar."])
        self.assertNotIn("Ma’nodosh", row["answer"])

    def test_a_real_alternative_joins_the_answer_with_a_slash(self):
        # bot/answers.py splits expected answers on "/", so both forms match.
        self.assertEqual(self.by_id[559905]["answer"],
                         "Plantagenetlarning shajarasi. / Plantagenetlarning genealogik daraxti.")

    def test_images_become_absolute_attachments(self):
        row = self.by_id[559905]
        self.assertEqual(row["attachments"], [
            "https://turnirlar.uz/savollar/api/media/pub/"
            "e74b4a32-ae0b-4d95-a8aa-dd933a5d6591/8dcf1f28.webp"])
        self.assertEqual(row["badges"], ["Rasmli", "Duplet"])

    def test_material_text_precedes_the_question(self):
        self.assertTrue(self.by_id[559905]["text"].startswith("Back to the future\n"))

    def test_several_authors_are_kept_in_order(self):
        self.assertEqual(self.by_id[559905]["authors"],
                         ["Hakim Batiraliyev", "Yan Sadkovskiy Igorevich"])

    def test_empty_optional_fields_are_dropped(self):
        row = self.by_id[559920]
        self.assertEqual(row["text"], "Uchinchi savol matni.")
        self.assertEqual(row["answer"], "Uchinchi javob.")
        for key in ("comment", "accepted", "attachments", "sources", "badges", "likes"):
            self.assertNotIn(key, row)

    def test_a_question_without_an_answer_still_counts(self):
        markup = PACKAGE_PAGE.replace("<!-- -->Uçinçi javob.", "")
        _, rows = ex.parse_package(markup, PACKAGE)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["answer"], "")

    def test_a_page_without_articles_yields_nothing(self):
        meta, rows = ex.parse_package("<html><body>bo‘sh</body></html>", PACKAGE)
        self.assertEqual(rows, [])
        self.assertEqual(meta, {})


class OutputTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="turnirlar-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_shards_round_trip_through_gzip(self):
        rows = [{"id": 1, "question_id": 11, "package_id": 900001, "text": "savol",
                 "answer": "javob"}]
        packages = [{"id": 900001, "source_id": 1, "title": "t", "questions": 1}]
        shard = ex.flush_shard(self.root, packages, rows, 1, compress=True)
        self.assertEqual(shard["file"], "shards/turnirlar-0001.json.gz")
        self.assertEqual(shard["questions"], 1)
        self.assertEqual(shard["packages"], [1])
        self.assertEqual(shard["min_question_id"], 11)
        self.assertEqual(packages[0]["shard"], 1)
        stored = self.root / shard["file"]
        with gzip.open(stored, "rt", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), rows)
        self.assertEqual(ex.read_shard(stored), rows)

    def test_manifest_counts_and_base_url(self):
        packages = [{"id": 900001, "source_id": 1, "questions": 5, "images": 2},
                    {"id": 900002, "source_id": 2, "questions": 7, "images": 0}]
        ex.save_manifest(self.root, {"source": ex.SITE, "packages": packages, "shards": []})
        saved = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["package_count"], 2)
        self.assertEqual(saved["question_count"], 12)
        self.assertEqual(saved["with_images"], 1)
        self.assertEqual(saved["base_url"], ex.BASE_URL)
        self.assertTrue(saved["base_url"].endswith("savollar-bazasi/"))

    def test_load_state_prunes_shards_that_are_not_on_disk(self):
        manifest = {
            "source": ex.SITE,
            "packages": [{"id": 900001, "source_id": 1, "questions": 3},
                         {"id": 900002, "source_id": 2, "questions": 4}],
            "shards": [{"file": "shards/turnirlar-0001.json.gz", "index": 1, "packages": [1]},
                       {"file": "shards/turnirlar-0002.json.gz", "index": 2, "packages": [2]}],
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "shards").mkdir(parents=True)
        (self.root / "shards" / "turnirlar-0001.json.gz").write_bytes(b"x")

        pruned, done = ex.load_state(self.root)
        self.assertEqual(done, {1})
        self.assertEqual([row["source_id"] for row in pruned["packages"]], [1])
        self.assertEqual([shard["index"] for shard in pruned["shards"]], [1])

    def test_load_state_without_a_manifest_starts_clean(self):
        manifest, done = ex.load_state(self.root)
        self.assertEqual(done, set())
        self.assertEqual(manifest["packages"], [])
        self.assertEqual(manifest["id_offset"], ex.ID_OFFSET)

    def test_existing_fingerprints_reads_the_other_archive(self):
        path = self.root / "questions-0001.json"
        path.write_text(json.dumps([{"text": "Birinçi yuriş"}, {"text": "Ikkinçi yuriş"}],
                                   ensure_ascii=False), encoding="utf-8")
        seen = ex.existing_fingerprints([path, self.root / "missing.json"])
        self.assertIn(ex.fingerprint("birinchi yurish"), seen)
        self.assertEqual(len(seen), 2)

    def test_write_sample_is_readable(self):
        rows = [{"id": 1, "text": "savol", "answer": "javob"}]
        target = self.root / "sample.json"
        ex.write_sample(target, rows, [{"source_id": 7, "title": "To‘plam"}])
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["questions"], rows)
        self.assertEqual(saved["listed_packages"][0]["source_id"], 7)
        self.assertIn("text", saved["fields"])
        self.assertGreater(saved["average_bytes"], 0)


class CheckModeTest(unittest.TestCase):
    def test_check_parses_files_left_by_the_probe(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="turnirlar-check-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "list-1.html").write_text(LIST_PAGE, encoding="utf-8")
        (root / "package-7800.html").write_text(PACKAGE_PAGE, encoding="utf-8")
        sample = root / "sample.json"
        with contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(ex.check(root, sample), 0)
        self.assertIn("parsed 3 of 3 questions", log.getvalue())
        saved = json.loads(sample.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["listed_packages"]), 2)
        self.assertEqual([row["question_id"] for row in saved["questions"]],
                         [559880, 559905, 559920])

    def test_check_on_an_empty_directory_fails_loudly(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="turnirlar-check-"))
        self.addCleanup(shutil.rmtree, root, True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ex.check(root), 1)


if __name__ == "__main__":
    unittest.main()


class VerifyModeTest(unittest.TestCase):
    """``--mode verify`` compares the site's own counts with the parsed ones.

    The catalogue header advertises far more questions than its packages hold,
    so this is the check that a package page is not hiding part of its list.
    """

    def _fetcher(self, announced: int | None = None):
        calls: list[str] = []

        def fetch(url: str, *args, **kwargs):
            calls.append(url)
            if "/paketlar" in url:
                return 200, LIST_PAGE
            markup = PACKAGE_PAGE
            if announced is not None:
                markup = markup.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                        f"<dt>Savollar soni</dt><dd>{announced}</dd>", 1)
            return 200, markup

        return fetch, calls

    def test_verify_accepts_a_package_the_parser_reads_completely(self):
        fetch, calls = self._fetcher()
        with mock.patch.object(ex, "fetch", fetch):
            stats, problems = ex.verify(1, 8, "all", 2, 0.0)
        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["announced"], 6)          # two packages, 3 questions each
        self.assertEqual(stats["parsed"], 6)
        self.assertEqual(stats["short"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(problems, [])
        self.assertTrue(any("/paket/7794" in url for url in calls))

    def test_verify_flags_a_package_page_that_hides_questions(self):
        fetch, _calls = self._fetcher(announced=5)
        with mock.patch.object(ex, "fetch", fetch):
            stats, problems = ex.verify(1, 8, "all", 2, 0.0)
        self.assertEqual(stats["short"], 2)
        self.assertEqual(sorted(row["source_id"] for row in problems), [6001, 7794])
        self.assertEqual(problems[0]["parsed"], 3)
        self.assertEqual(problems[0]["page"], 5)

    def test_verify_spreads_its_sample_over_the_whole_catalogue(self):
        fetch, calls = self._fetcher()
        with mock.patch.object(ex, "fetch", fetch):
            stats, _problems = ex.verify(4, 1, "all", 2, 0.0)
        listed = [url for url in calls if "/paketlar" in url]
        self.assertEqual(len(listed), 5)                 # the probe page + 4 samples
        self.assertIn("page=260", listed[-1])            # the last page is always sampled
        self.assertEqual(len([url for url in calls if "/paket/" in url]), 4)
        self.assertEqual(stats["checked"], 4)

    def test_verify_counts_a_card_that_disagrees_with_the_package_page(self):
        def fetch(url: str, *args, **kwargs):
            if "/paketlar" in url:
                return 200, LIST_PAGE
            return 200, PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                             "<dt>Savollar soni</dt><dd>36</dd>", 1)
        with mock.patch.object(ex, "fetch", fetch):
            stats, _problems = ex.verify(1, 8, "all", 2, 0.0)
        self.assertEqual(stats["card_mismatch"], 1)      # 6001's card says 12, the page 36
        self.assertEqual(stats["short"], 2)              # and the parser is short of both

    def test_verify_names_the_packages_whose_card_disagrees_with_the_page(self):
        def fetch(url: str, *args, **kwargs):
            if "/paketlar" in url:
                return 200, LIST_PAGE
            return 200, PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                             "<dt>Savollar soni</dt><dd>36</dd>", 1)
        with mock.patch.object(ex, "fetch", fetch), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            stats, _problems = ex.verify(1, 8, "all", 2, 0.0)
        self.assertEqual(stats["card_mismatch"], 1)      # 6001's card says 12
        self.assertIn("package 6001: the card promises 12 questions, the package page 36",
                      printed.getvalue())

    def test_verify_survives_a_package_that_refuses(self):
        def fetch(url: str, *args, **kwargs):
            if "/paketlar" in url:
                return 200, LIST_PAGE
            return 500, ""
        with mock.patch.object(ex, "fetch", fetch):
            stats, problems = ex.verify(1, 8, "all", 2, 0.0)
        self.assertEqual(stats["checked"], 0)
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(problems, [])


def _tour_page(number: int, first_index: int, first_ref: int) -> str:
    """A package page as the site renders one tour of a big package."""
    page = PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                f"<dt>Savollar soni</dt><dd>6</dd>", 1)
    page = page.replace('<section id="tur-1" class="zk-tour">',
                        f'<section id="tur-{number}" class="zk-tour">', 1)
    page = page.replace('<h2 class="zk-tour-title">1-tur</h2>',
                        f'<h2 class="zk-tour-title">{number}-tur</h2>', 1)
    for offset in range(3, 0, -1):
        page = page.replace(f'id="savol-{offset}"', f'id="savol-{first_index + offset - 1}"')
        page = page.replace(f'data-panel-ref="{559880 + (offset - 1) * 25 if offset < 3 else 559920}"',
                            f'data-panel-ref="{first_ref + offset - 1}"')
        page = page.replace(f'href="/savollar/savol/{559880 + (offset - 1) * 25 if offset < 3 else 559920}"',
                            f'href="/savollar/savol/{first_ref + offset - 1}"')
    return page


class TourPaginationTest(unittest.TestCase):
    """A package over 60 questions is rendered one tour per page."""

    LINK = ('<a class="zk-tour-next" href="/savollar/paket/7800?tur=2">'
            'Keyingi tur: 2-tur →</a>')

    def test_tour_links_are_read_from_the_page(self):
        self.assertEqual(ex.tour_links(PACKAGE_PAGE + self.LINK), [2])
        self.assertEqual(ex.tour_links(PACKAGE_PAGE), [])
        self.assertEqual(ex.tour_links('<a href="https://turnirlar.uz/savollar/paket/9?tur=7">7</a>'), [7])
        # the flight payload repeats the page inside <script>: it must not count
        scripted = '<script>self.__next_f.push([1,"href=\\\"?tur=5\\\""])</script>'
        self.assertEqual(ex.tour_links(scripted), [])

    def test_a_package_that_fits_on_one_page_is_read_once(self):
        calls: list[str] = []

        def fetch(url: str, *args, **kwargs):
            calls.append(url)
            return 200, PACKAGE_PAGE

        with mock.patch.object(ex, "fetch", fetch):
            _package, meta, questions = ex.fetch_package(7800, {"source_id": 7800})
        self.assertEqual(len(calls), 1)                    # the site says 3, we have 3
        self.assertEqual(len(questions), 3)
        self.assertEqual(meta["announced"], 3)
        self.assertEqual(meta["tours"], 1)

    def test_the_tour_pages_are_followed_until_the_count_is_reached(self):
        calls: list[str] = []

        def fetch(url: str, *args, **kwargs):
            calls.append(url)
            if "tur=2" in url:
                return 200, _tour_page(2, 4, 559930)
            return 200, PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                             "<dt>Savollar soni</dt><dd>6</dd>", 1) + self.LINK

        with mock.patch.object(ex, "fetch", fetch):
            _package, meta, questions = ex.fetch_package(7800, {"source_id": 7800})
        self.assertEqual(calls, [f"{ex.SITE}/paket/7800", f"{ex.SITE}/paket/7800?tur=2"])
        self.assertEqual(len(questions), 6)
        self.assertEqual([row["index"] for row in questions], [1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted({row["round"] for row in questions}), [1, 2])
        self.assertEqual(len({row["question_id"] for row in questions}), 6)
        self.assertEqual(meta["tours"], 2)
        self.assertEqual(meta["announced"], 6)

    def test_a_tour_that_repeats_a_question_is_not_counted_twice(self):
        def fetch(url: str, *args, **kwargs):
            if "tur=2" in url:
                return 200, PACKAGE_PAGE          # the same three questions again
            return 200, PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                             "<dt>Savollar soni</dt><dd>9</dd>", 1) + self.LINK

        with mock.patch.object(ex, "fetch", fetch):
            _package, meta, questions = ex.fetch_package(7800, {"source_id": 7800})
        self.assertEqual(len(questions), 3)
        self.assertEqual(meta["tours"], 2)        # it tried, and stopped there

    def test_a_tour_page_that_refuses_leaves_the_rest_intact(self):
        def fetch(url: str, *args, **kwargs):
            if "tur=2" in url:
                return 500, ""
            return 200, PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                             "<dt>Savollar soni</dt><dd>6</dd>", 1) + self.LINK

        with mock.patch.object(ex, "fetch", fetch):
            _package, meta, questions = ex.fetch_package(7800, {"source_id": 7800})
        self.assertEqual(len(questions), 3)
        self.assertEqual(meta["tour_failed"], 2)

    def test_the_walk_stops_when_the_page_offers_no_further_tour(self):
        calls: list[str] = []

        def fetch(url: str, *args, **kwargs):
            calls.append(url)
            return 200, PACKAGE_PAGE.replace("<dt>Savollar soni</dt><dd>3</dd>",
                                             "<dt>Savollar soni</dt><dd>60</dd>", 1)

        with mock.patch.object(ex, "fetch", fetch):
            _package, meta, questions = ex.fetch_package(7800, {"source_id": 7800})
        self.assertEqual(len(calls), 1)           # nothing to follow
        self.assertEqual(len(questions), 3)
        self.assertEqual(meta["announced"], 60)


class LastRunTest(unittest.TestCase):
    """What the workflow learns from a run: did the base change, and did it work?"""

    def test_the_run_file_says_whether_the_archive_changed(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="turnirlar-lastrun-"))
        self.addCleanup(shutil.rmtree, root, True)
        path = ex.save_last_run(root, {"packages": 3, "questions": 40, "failed": 0}, True)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, "last-run.json")
        self.assertTrue(data["changed"])
        self.assertEqual(data["stats"], {"packages": 3, "questions": 40, "failed": 0})
        self.assertIn("exported_at", data)

    def test_an_unchanged_run_is_written_as_unchanged(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="turnirlar-lastrun-"))
        self.addCleanup(shutil.rmtree, root, True)
        ex.save_last_run(root, {"packages": 0, "questions": 0, "failed": 0}, False)
        data = json.loads((root / "last-run.json").read_text(encoding="utf-8"))
        self.assertFalse(data["changed"])

    def test_finding_nothing_new_is_a_success(self):
        # the weekly refresh usually walks pages that are already stored
        self.assertEqual(ex.exit_code({"packages": 0, "empty": 0, "failed": 0}), 0)
        self.assertEqual(ex.exit_code({"packages": 0, "empty": 62, "failed": 0}), 0)
        self.assertEqual(ex.exit_code({"packages": 12, "empty": 0, "failed": 2}), 0)

    def test_a_run_that_only_collected_errors_is_a_failure(self):
        self.assertEqual(ex.exit_code({"packages": 0, "empty": 0, "failed": 5}), 1)

