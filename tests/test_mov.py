"""MOV 写侧（motionphoto/mov.py）的测试：标识符写入、偏移修正、拒绝危险结构。"""

import unittest

from motionphoto import mov

from . import fixtures

UUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
OTHER = "11111111-2222-3333-4444-555555555555"


class WriteIdentifierTest(unittest.TestCase):
    def test_adds_meta_when_absent(self):
        source = fixtures.build_mov()
        self.assertIsNone(mov.read_content_identifier(source))

        result = mov.set_content_identifier(source, UUID)

        self.assertEqual(mov.read_content_identifier(result), UUID)
        self.assertGreater(len(result), len(source))
        self.assertEqual(fixtures.mov_mdat_payload(result), fixtures.mov_mdat_payload(source))

    def test_creates_quicktime_style_meta(self):
        """新建的 meta 要与真机同形：hdlr(mdta) + keys + ilst，且没有 version/flags。"""
        result = mov.set_content_identifier(fixtures.build_mov(), UUID)
        moov = mov.find(result, 0, len(result), b"moov")
        meta = mov.find(result, moov.body, moov.end, b"meta")
        self.assertIsNotNone(meta)
        kids = mov.children(result, meta.body, meta.end)
        self.assertEqual([kid.type for kid in kids], [b"hdlr", b"keys", b"ilst"])
        hdlr = kids[0]
        self.assertEqual(result[hdlr.body + 8:hdlr.body + 12], b"mdta")
        self.assertEqual(result[hdlr.body:meta.end][:4], b"\x00\x00\x00\x00")

    def test_keeps_other_keys_in_existing_meta(self):
        source = fixtures.build_mov(meta_keys=[b"com.apple.quicktime.creationdate"])
        self.assertIsNone(mov.read_content_identifier(source))

        result = mov.set_content_identifier(source, UUID)

        self.assertEqual(mov.read_content_identifier(result), UUID)
        moov = mov.find(result, 0, len(result), b"moov")
        meta = mov.find(result, moov.body, moov.end, b"meta")
        values = mov._read_meta_values(result, meta)
        self.assertIn("com.apple.quicktime.creationdate", values)
        self.assertEqual(values["com.apple.quicktime.content.identifier"], UUID)

    def test_updates_existing_identifier_without_growing_keys(self):
        source = fixtures.build_mov(meta_keys=[b"com.apple.quicktime.content.identifier"])
        first = mov.set_content_identifier(source, UUID)
        second = mov.set_content_identifier(first, OTHER)

        self.assertEqual(mov.read_content_identifier(second), OTHER)
        moov = mov.find(second, 0, len(second), b"moov")
        meta = mov.find(second, moov.body, moov.end, b"meta")
        keys = mov.find(second, meta.body, meta.end, b"keys")
        self.assertEqual(len(mov._parse_keys(second, keys)), 1)

    def test_is_idempotent(self):
        source = fixtures.build_mov()
        once = mov.set_content_identifier(source, UUID)
        twice = mov.set_content_identifier(once, UUID)
        self.assertEqual(once, twice)

    def test_leaves_itunes_meta_alone(self):
        source = fixtures.build_mov(with_itunes_meta=True)
        udta = source[source.index(b"udta"):][:98]
        result = mov.set_content_identifier(source, UUID)
        self.assertEqual(mov.read_content_identifier(result), UUID)
        self.assertIn(udta, result)


class ChunkOffsetTest(unittest.TestCase):
    def test_offsets_follow_growth_when_moov_is_first(self):
        source = fixtures.build_mov(moov_first=True)
        before = mov.chunk_offsets(source)
        self.assertTrue(before)

        result = mov.set_content_identifier(source, UUID)

        delta = len(result) - len(source)
        self.assertGreater(delta, 0)
        self.assertEqual(mov.chunk_offsets(result), [offset + delta for offset in before])
        # 偏移指向的字节必须还是原来那份媒体数据
        for old, new in zip(before, mov.chunk_offsets(result)):
            self.assertEqual(result[new:new + 8], source[old:old + 8])

    def test_offsets_unchanged_when_moov_is_last(self):
        source = fixtures.build_mov(moov_first=False)
        before = mov.chunk_offsets(source)

        result = mov.set_content_identifier(source, UUID)

        self.assertEqual(mov.chunk_offsets(result), before)
        self.assertEqual(fixtures.mov_mdat_payload(result), fixtures.mov_mdat_payload(source))

    def test_all_offsets_stay_inside_mdat(self):
        for moov_first in (True, False):
            result = mov.set_content_identifier(fixtures.build_mov(moov_first=moov_first), UUID)
            ranges = mov.mdat_payloads(result)
            for offset in mov.chunk_offsets(result):
                self.assertTrue(
                    any(start <= offset < end for start, end in ranges),
                    "chunk 偏移 %d 不在 mdat 里（moov_first=%s）" % (offset, moov_first),
                )


class RejectTest(unittest.TestCase):
    def test_rejects_fragmented(self):
        with self.assertRaises(mov.MovError):
            mov.set_content_identifier(fixtures.build_mov(fragmented=True), UUID)

    def test_rejects_without_moov(self):
        with self.assertRaises(mov.MovError):
            mov.set_content_identifier(fixtures.build_mp4_without_moov(), UUID)

    def test_rejects_garbage(self):
        with self.assertRaises(mov.MovError):
            mov.set_content_identifier(b"\x01\x02\x03\x04" * 4, UUID)

    def test_rejects_non_ascii_identifier(self):
        with self.assertRaises(mov.MovError):
            mov.set_content_identifier(fixtures.build_mov(), "标识符")


class ReadTest(unittest.TestCase):
    def test_reads_missing_identifier_as_none(self):
        self.assertIsNone(mov.read_content_identifier(fixtures.build_mov()))
        self.assertIsNone(mov.read_content_identifier(fixtures.build_mp4()))

    def test_has_still_image_time_track(self):
        self.assertFalse(mov.has_still_image_time_track(fixtures.build_mov()))
        source = fixtures.build_mov(meta_keys=[mov.STILL_IMAGE_TIME_KEY])
        self.assertTrue(mov.has_still_image_time_track(source))


if __name__ == "__main__":
    unittest.main()
