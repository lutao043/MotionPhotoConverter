"""MP4 预检测试。"""

import struct
import unittest

from motionphoto import mp4
from tests import fixtures


class ParseTest(unittest.TestCase):
    def test_reads_core_fields(self):
        info = mp4.parse(fixtures.build_mp4(duration_ms=3000))
        self.assertEqual(info.brand, "isom")
        self.assertIn("mp42", info.compatible_brands)
        self.assertTrue(info.has_moov)
        self.assertTrue(info.has_mdat)
        self.assertFalse(info.fragmented)
        self.assertEqual(info.duration_us, 3_000_000)
        self.assertEqual(info.video_codec, "avc1")
        self.assertEqual(info.audio_codec, "mp4a")
        self.assertEqual(info.trailing_garbage, 0)
        self.assertEqual([name for name, _ in info.boxes], ["ftyp", "moov", "mdat"])

    def test_detects_fragmented(self):
        info = mp4.parse(fixtures.build_mp4(fragmented=True))
        self.assertTrue(info.fragmented)

    def test_parses_version1_mvhd(self):
        payload = b"\x01\x00\x00\x00"
        payload += struct.pack(">QQ", 0, 0)  # creation / modification
        payload += struct.pack(">IQ", 1000, 5000)  # timescale / duration
        payload += b"\x00" * 64
        mvhd = struct.pack(">I", len(payload) + 8) + b"mvhd" + payload
        moov = struct.pack(">I", len(mvhd) + 8) + b"moov" + mvhd
        data = struct.pack(">I", 16) + b"ftyp" + b"isom" + b"\x00" * 4 + moov
        info = mp4.parse(data)
        self.assertEqual(info.duration_us, 5_000_000)

    def test_reports_trailing_garbage(self):
        info = mp4.parse(fixtures.build_mp4() + b"\x01\x02\x03")
        self.assertEqual(info.trailing_garbage, 3)

    def test_rejects_truncated_box(self):
        data = fixtures.build_mp4()
        with self.assertRaises(mp4.Mp4Error):
            mp4.parse(data[:-40])

    def test_rejects_tiny_file(self):
        with self.assertRaises(mp4.Mp4Error):
            mp4.parse(b"\x00\x00\x00\x08ftyp")


class ValidateTest(unittest.TestCase):
    def test_good_file_has_no_complaints(self):
        info, errors, warnings = mp4.validate(fixtures.build_mp4())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertTrue(info.has_moov)

    def test_missing_moov_is_an_error(self):
        _, errors, _ = mp4.validate(fixtures.build_mp4_without_moov())
        self.assertTrue(any("moov" in text for text in errors))

    def test_missing_ftyp_is_an_error(self):
        data = struct.pack(">I", 16) + b"mdat" + b"\x00" * 8
        _, errors, _ = mp4.validate(data)
        self.assertTrue(any("ftyp" in text for text in errors))

    def test_uncommon_codec_warns(self):
        _, errors, warnings = mp4.validate(fixtures.build_mp4(codec=b"av01"))
        self.assertEqual(errors, [])
        self.assertTrue(any("av01" in text for text in warnings))

    def test_fragmented_warns(self):
        _, errors, warnings = mp4.validate(fixtures.build_mp4(fragmented=True))
        self.assertEqual(errors, [])
        self.assertTrue(any("分片" in text for text in warnings))

    def test_garbage_is_an_error(self):
        _, errors, _ = mp4.validate(b"this is not an mp4 file at all, really")
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
