"""JPEG 段级读写测试。"""

import unittest

from motionphoto import jpeg
from tests import fixtures


class ParseStructureTest(unittest.TestCase):
    def test_finds_eoi_and_segments(self):
        data = fixtures.build_jpeg()
        segments, eoi = jpeg.parse_structure(data)
        self.assertEqual(eoi, len(data) - 2)
        self.assertEqual(segments[0].marker, jpeg.MARKER_APP0)
        self.assertEqual(segments[1].marker, jpeg.MARKER_APP1)
        self.assertEqual(data[segments[1].body_offset:segments[1].body_offset + 6], b"Exif\x00\x00")

    def test_scan_skips_stuffing_and_restart_markers(self):
        # 样本的熵编码数据里含 0xFF00 与 0xFFD0，EOI 必须落在真正的末尾
        data = fixtures.build_jpeg()
        _, eoi = jpeg.parse_structure(data)
        self.assertEqual(data[eoi:eoi + 2], jpeg.EOI)

    def test_rejects_non_jpeg(self):
        with self.assertRaises(jpeg.JpegError):
            jpeg.parse_structure(b"not a jpeg at all")

    def test_rejects_truncated_segment(self):
        data = fixtures.build_jpeg()[:20]
        with self.assertRaises(jpeg.JpegError):
            jpeg.parse_structure(data)

    def test_missing_eoi(self):
        data = fixtures.build_jpeg()
        with self.assertRaises(jpeg.JpegError):
            jpeg.parse_structure(data[:-4])


class TrailingDataTest(unittest.TestCase):
    def test_detects_and_strips_trailing(self):
        base = fixtures.build_jpeg()
        with_trailing = base + b"\x00" * 32 + b"junk"
        self.assertTrue(jpeg.has_trailing_data(with_trailing))
        self.assertEqual(jpeg.trailing_data(with_trailing), b"\x00" * 32 + b"junk")
        self.assertEqual(jpeg.strip_trailing_data(with_trailing), base)

    def test_no_trailing(self):
        base = fixtures.build_jpeg()
        self.assertFalse(jpeg.has_trailing_data(base))
        self.assertEqual(jpeg.strip_trailing_data(base), base)


class AppSegmentTest(unittest.TestCase):
    def test_finds_exif_but_not_xmp_when_absent(self):
        data = fixtures.build_jpeg(with_exif=True, with_xmp=False)
        self.assertIsNotNone(jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE))
        self.assertIsNone(jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE))

    def test_insert_xmp_goes_after_exif_and_before_image_data(self):
        data = fixtures.build_jpeg(with_exif=True)
        packet = b'<?xpacket begin="\xef\xbb\xbf"?><x:xmpmeta/>'
        updated = jpeg.upsert_app_segment(
            data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE, jpeg.XMP_SIGNATURE + packet
        )
        segments, _ = jpeg.parse_structure(updated)
        order = []
        for seg in segments:
            body = updated[seg.body_offset:seg.end]
            if body.startswith(jpeg.EXIF_SIGNATURE):
                order.append("exif")
            elif body.startswith(jpeg.XMP_SIGNATURE):
                order.append("xmp")
        self.assertEqual(order, ["exif", "xmp"])
        xmp_segment = jpeg.find_app_segment(updated, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        sos = [seg for seg in segments if seg.marker == jpeg.MARKER_SOS][0]
        self.assertLess(xmp_segment.offset, sos.offset)

    def test_upsert_replaces_in_place(self):
        data = fixtures.build_jpeg(with_xmp=True)
        before = len(jpeg.inspect_segments(data))
        packet = b'<?xpacket begin="\xef\xbb\xbf"?><x:xmpmeta id="new"/>'
        updated = jpeg.upsert_app_segment(
            data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE, jpeg.XMP_SIGNATURE + packet
        )
        self.assertEqual(len(jpeg.inspect_segments(updated)), before)
        segment = jpeg.find_app_segment(updated, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        self.assertIn(b'id="new"', jpeg.segment_body(updated, segment))

    def test_remove_all_app_segments_clears_duplicates(self):
        data = fixtures.build_jpeg(with_exif=True)
        body = jpeg.XMP_SIGNATURE + b"<x:xmpmeta/>"
        data = jpeg.upsert_app_segment(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE, body)
        data = data[:2] + jpeg.build_app_segment(jpeg.MARKER_APP1, body) + data[2:]
        self.assertEqual(len(_xmp_segments(data)), 2)
        cleaned = jpeg.remove_all_app_segments(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        self.assertEqual(len(_xmp_segments(cleaned)), 0)

    def test_oversized_segment_rejected(self):
        with self.assertRaises(jpeg.JpegError):
            jpeg.build_app_segment(jpeg.MARKER_APP1, b"x" * (jpeg.MAX_SEGMENT_BODY + 1))


def _xmp_segments(data):
    result = []
    for seg in jpeg.inspect_segments(data):
        if seg.marker == jpeg.MARKER_APP1 and data[
            seg.body_offset:seg.body_offset + len(jpeg.XMP_SIGNATURE)
        ] == jpeg.XMP_SIGNATURE:
            result.append(seg)
    return result


if __name__ == "__main__":
    unittest.main()
