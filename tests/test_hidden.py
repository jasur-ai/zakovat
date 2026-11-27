"""The invisible state codec: two densities, a sentinel, and a checksum.

Everything the bot remembers between serverless requests lives inside a
Telegram message as zero-width characters, so the codec is load-bearing:

* a short game state keeps the old v2 run (one bit per character) so states
  written before the change still decode after a deploy;
* a big tournament body needs v3 (two bits per character, zlib, a checksum);
* a run that is truncated, mixed or edited by hand must decode to ``None``,
  never to a half-read state;
* the encoded run must be counted in UTF-16 units, because that is what
  Telegram's 4096-character limit is measured in.
"""
import unittest

from bot import hidden

UNIT = '{"id":%d,"text":"Savol matni","answer":"Javob","comment":"Izoh"}'


def body(size: int) -> str:
    """About ``size`` characters of state-shaped JSON (compressible, as real
    tournament bodies are: the same keys over and over)."""
    out: list[str] = []
    total = 0
    index = 0
    while total < size:
        piece = UNIT % index
        out.append(piece)
        total += len(piece)
        index += 1
    return "".join(out)[:size]


class DensityTests(unittest.TestCase):
    def test_a_short_body_stays_in_v2(self):
        run = hidden.encode(body(40))
        self.assertEqual(hidden.density(run), "v2")
        self.assertTrue(all(char in hidden.LEGACY_INVISIBLE for char in run))

    def test_a_big_body_moves_to_v3(self):
        run = hidden.encode(body(2000))
        self.assertEqual(hidden.density(run), "v3")
        self.assertLess(len(run), 8 * 2000)          # v2 would need eight per byte

    def test_the_density_can_be_forced(self):
        text = body(2000)
        self.assertEqual(hidden.density(hidden.encode(text, dense=True)), "v3")
        self.assertEqual(hidden.density(hidden.encode(text, dense=False)), "v2")
        self.assertEqual(hidden.encode(text, dense=True), hidden.encode_dense(text))
        self.assertEqual(hidden.encode(text, dense=False), hidden.encode_legacy(text))

    def test_the_threshold_decides(self):
        text = body(200)
        self.assertEqual(hidden.density(hidden.encode(text, threshold=300)), "v2")
        self.assertEqual(hidden.density(hidden.encode(text, threshold=100)), "v3")

    def test_v3_is_smaller_for_a_tournament_sized_body(self):
        text = body(2000)
        self.assertLess(len(hidden.encode_dense(text)), len(hidden.encode_legacy(text)))

    def test_both_densities_carry_every_byte(self):
        for size in (1, 7, 40, 300, 4096):
            text = body(size)
            for dense in (False, True):
                run = hidden.encode(text, dense=dense)
                self.assertEqual(hidden.decode(run), text, f"{size} bytes, dense={dense}")


class RoundTripTests(unittest.TestCase):
    def test_unicode_survives(self):
        text = "O‘zbekcha: so‘roq, «tirnoq», — chiziqli, 😀 emoji, 1234"
        for dense in (False, True):
            self.assertEqual(hidden.decode(hidden.encode(text, dense=dense)), text)

    def test_an_empty_body_carries_no_state(self):
        # v2 packs the bytes themselves, so an empty body has nothing to read
        # back; v3 keeps zlib's empty stream and yields an empty string. Either
        # way no caller ever writes an empty state.
        for empty in ("", None):
            self.assertIsNone(hidden.decode(hidden.encode(empty, dense=False)))
            self.assertEqual(hidden.decode(hidden.encode(empty, dense=True)), "")

    def test_the_run_is_invisible(self):
        for dense in (False, True):
            run = hidden.encode(body(500), dense=dense)
            self.assertTrue(all(char in hidden.INVISIBLE for char in run))
            self.assertEqual("".join(char for char in run if char not in hidden.INVISIBLE), "")

    def test_the_run_hides_inside_a_message(self):
        text = "SAVOL 3 / 30\n" + hidden.encode(body(500)) + "\nJavob: 60 sekund"
        self.assertEqual(hidden.decode(text), body(500))
        self.assertIn("SAVOL 3 / 30", text)

    def test_the_longest_valid_run_wins(self):
        """A message carries one state; if two runs slip in, the longer is read.

        Compared at one density: a compressed v3 run can be shorter in
        characters than the v2 run of a smaller body.
        """
        small, big = body(120), body(400)
        text = (f"{hidden.encode(small, dense=False)} o‘rtada "
                f"{hidden.encode(big, dense=False)}")
        self.assertEqual(hidden.decode(text), big)

    def test_a_single_run_is_read_wherever_it_sits(self):
        text = body(300)
        for prefix, suffix in (("", ""), ("Sarlavha\n", ""), ("", "\nDavomi"),
                               ("A\n", "\nB")):
            self.assertEqual(hidden.decode(prefix + hidden.encode(text) + suffix), text)

    def test_a_tournament_body_round_trips(self):
        text = ('{"i":959,"q":3,"d":1735000000,"teams":[{"n":"Bilimdonlar","c":101,'
                '"m":[{"i":101,"n":"Kapitan","r":1}]}],"include":[1,2,3]}')
        self.assertEqual(hidden.decode(hidden.encode(text)), text)


class TamperTests(unittest.TestCase):
    def test_a_truncated_run_is_refused(self):
        for dense in (False, True):
            run = hidden.encode(body(500), dense=dense)
            self.assertIsNone(hidden.decode(run[:8]))
            self.assertIsNone(hidden.decode(run[:-len(run) // 2]))

    def test_an_edited_run_is_refused(self):
        for dense in (False, True):
            run = list(hidden.encode(body(500), dense=dense))
            middle = len(run) // 2
            run[middle] = hidden.ZW0 if run[middle] != hidden.ZW0 else hidden.ZW1
            self.assertIsNone(hidden.decode("".join(run)))

    def test_a_mixed_alphabet_is_refused(self):
        v2 = hidden.encode_legacy(body(500))
        self.assertEqual(hidden.density(v2), "v2")
        self.assertEqual(hidden.density(hidden.encode_dense(body(500))), "v3")
        # one v3-only character inside a v2 run: it is read as v3 and fails
        self.assertIsNone(hidden.decode_run(list(v2) + [hidden.ZWA]))

    def test_a_run_that_is_too_short_is_refused(self):
        self.assertIsNone(hidden.decode(hidden.ZW0 * 8))
        self.assertIsNone(hidden.decode(hidden.ZWA * 4))
        self.assertIsNone(hidden.decode(""))

    def test_plain_text_is_not_a_state(self):
        self.assertIsNone(hidden.decode("Savol 3 / 30 · QOLGAN VAQT 00:45"))
        self.assertIsNone(hidden.decode(hidden.ZW0 + hidden.ZW1 + hidden.ZW0 + hidden.ZW1))

    def test_a_wrong_checksum_is_refused(self):
        run = list(hidden.encode_dense(body(900)))
        run[-4] = hidden.ZWB if run[-4] != hidden.ZWB else hidden.ZWA
        self.assertIsNone(hidden.decode("".join(run)))

    def test_a_wrong_sentinel_is_refused(self):
        run = list(hidden.encode_legacy(body(900)))
        run[0] = hidden.ZW1 if run[0] != hidden.ZW1 else hidden.ZW0
        self.assertIsNone(hidden.decode("".join(run)))


class LengthTests(unittest.TestCase):
    def test_utf16_length_counts_pairs(self):
        self.assertEqual(hidden.utf16_length("abc"), 3)
        self.assertEqual(hidden.utf16_length("😀"), 2)          # one surrogate pair
        self.assertEqual(hidden.utf16_length(""), 0)
        self.assertEqual(hidden.utf16_length(None), 0)

    def test_every_invisible_character_is_one_unit(self):
        for char in hidden.INVISIBLE:
            self.assertEqual(hidden.utf16_length(char), 1)

    def test_a_message_keeps_room_for_the_text(self):
        text = body(1200)
        run = hidden.encode_dense(text)
        self.assertEqual(hidden.utf16_length(run), len(run))
        self.assertLess(hidden.utf16_length(run) + hidden.utf16_length(text), 4096)


class ConstantsTests(unittest.TestCase):
    def test_the_sentinels_differ(self):
        self.assertNotEqual(hidden.SENTINEL_V2, hidden.SENTINEL_V3)

    def test_the_alphabets_are_known(self):
        self.assertEqual(len(set(hidden.INVISIBLE)), 4)
        self.assertEqual(set(hidden.LEGACY_INVISIBLE), {hidden.ZW0, hidden.ZW1})
        self.assertEqual(set(hidden.V3_SYMBOLS), set(hidden.INVISIBLE))

    def test_minimum_runs_line_up_with_the_packing(self):
        self.assertEqual(hidden.MIN_RUN_V2 % 8, 0)      # v2 packs eight chars a byte
        self.assertEqual(hidden.MIN_RUN_V3 % 4, 0)      # v3 packs four
        self.assertGreater(hidden.MIN_RUN_V2, 0)
        self.assertGreater(hidden.MIN_RUN_V3, 0)


if __name__ == "__main__":
    unittest.main()
