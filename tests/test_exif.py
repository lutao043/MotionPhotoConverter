"""Exif 私有标记写入测试。

核心安全性质：原有 Exif 字节一个都不能被改动或搬移，只允许追加，
并且只改 TIFF 头里那 4 个字节的「第一个 IFD 偏移」。
"""

import struct
import unittest

from motionphoto import exif
from tests import fixtures


def raw_ifd0_tags(tiff: bytes):
    """独立于 exif.py 的手写解析：数一数 IFD0 里每个 tag 出现几次。"""
    endian = "<" if tiff[:2] == b"II" else ">"
    offset = struct.unpack(endian + "I", tiff[4:8])[0]
    count = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
    tags = []
    for index in range(count):
        base = offset + 2 + 12 * index
        tags.append(struct.unpack(endian + "H", tiff[base:base + 2])[0])
    return tags


class WriteTagsTest(unittest.TestCase):
    def test_original_bytes_are_preserved(self):
        original = fixtures.TIFF_EXIF
        updated = exif.write_tags(original, [exif.micro_video_tag()])
        # 只有 TIFF 头里的 4 字节 IFD 偏移被改写，其余原样
        self.assertEqual(updated[:4], original[:4])
        self.assertEqual(updated[8:len(original)], original[8:])
        self.assertNotEqual(updated[4:8], original[4:8])

    def test_xiaomi_tag_is_written_and_only_once(self):
        updated = exif.write_tags(fixtures.TIFF_EXIF, [exif.micro_video_tag()])
        info = exif.describe(updated)
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO], (exif.TYPE_BYTE, 1, 1))
        self.assertEqual(raw_ifd0_tags(updated).count(exif.TAG_XIAOMI_MICRO_VIDEO), 1)

        # 再写一次不会出现重复条目
        again = exif.write_tags(updated, [exif.micro_video_tag()])
        self.assertEqual(raw_ifd0_tags(again).count(exif.TAG_XIAOMI_MICRO_VIDEO), 1)
        self.assertEqual(exif.describe(again)["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO][2], 1)

    def test_existing_tags_survive(self):
        updated = exif.write_tags(fixtures.TIFF_EXIF, [exif.micro_video_tag()])
        info = exif.describe(updated)
        self.assertEqual(info["ifd0"][0x0112][2], 6)  # Orientation
        self.assertEqual(info["ifd0"][0x010F][2], "TestCam")
        self.assertEqual(info["ifd0"][0x0132][2], "2026:01:01 00:00:00")
        self.assertEqual(info["exif_ifd"][0x9000][2], b"0232")
        self.assertEqual(info["exif_ifd"][0x9286][2], b"ASCII\x00\x00\x00Old comment")
        self.assertIn(0x0201, info["ifd1"])
        self.assertIn(0x0202, info["ifd1"])

    def test_thumbnail_data_still_reachable(self):
        offsets = fixtures.fixture_tiff_offsets()
        info = exif.describe(fixtures.TIFF_EXIF)
        thumbnail_offset = info["ifd1"][0x0201][2]
        thumbnail_length = info["ifd1"][0x0202][2]
        original_thumbnail = fixtures.TIFF_EXIF[thumbnail_offset:thumbnail_offset + thumbnail_length]

        updated = exif.write_tags(fixtures.TIFF_EXIF, [exif.micro_video_tag()])
        updated_info = exif.describe(updated)
        self.assertEqual(updated_info["ifd1"][0x0201][2], thumbnail_offset)
        self.assertEqual(
            updated[thumbnail_offset:thumbnail_offset + thumbnail_length], original_thumbnail
        )
        self.assertGreater(offsets["ifd1"], 0)

    def test_user_comment_goes_into_exif_ifd(self):
        updated = exif.write_tags(
            fixtures.TIFF_EXIF, [], [exif.user_comment_tag("Oplus_8388608")]
        )
        info = exif.describe(updated)
        self.assertEqual(info["exif_ifd"][0x9286][2], b"ASCII\x00\x00\x00Oplus_8388608")
        self.assertEqual(info["exif_ifd"][0x9000][2], b"0232")  # 原条目保留
        self.assertNotIn(0x9286, info["ifd0"])

    def test_both_ifds_in_one_pass(self):
        updated = exif.write_tags(
            fixtures.TIFF_EXIF,
            [exif.micro_video_tag()],
            [exif.user_comment_tag("Oplus_1")],
        )
        info = exif.describe(updated)
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO][2], 1)
        self.assertEqual(info["exif_ifd"][0x9286][2], b"ASCII\x00\x00\x00Oplus_1")
        self.assertEqual(info["ifd0"][0x0112][2], 6)

    def test_noop_without_tags(self):
        self.assertEqual(exif.write_tags(fixtures.TIFF_EXIF), fixtures.TIFF_EXIF)

    def test_oversized_is_rejected(self):
        padded = fixtures.TIFF_EXIF + b"\x00" * (exif.MAX_TIFF_BODY - len(fixtures.TIFF_EXIF) + 10)
        with self.assertRaises(exif.ExifError):
            exif.write_tags(padded, [exif.micro_video_tag()])

    def test_extra_vendor_tags(self):
        updated = exif.write_tags(fixtures.TIFF_EXIF, exif.extra_vendor_tags())
        info = exif.describe(updated)
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO_EXTRA][2], 1)
        self.assertEqual(info["ifd0"][exif.TAG_EMBEDDED_VIDEO][2], 1)


    def test_opaque_makernote_blob_is_not_moved(self):
        """MakerNote 一类的黑盒数据必须停留在原偏移，且逐字节不变。"""
        makernote = b"MAKER\x00" + bytes(range(48))
        tiff = exif.write_tags(
            fixtures.TIFF_EXIF, [], [exif.TagSpec(0x927C, exif.TYPE_UNDEFINED, makernote)]
        )
        offset = _makernote_offset(tiff)
        self.assertEqual(tiff[offset:offset + len(makernote)], makernote)

        updated = exif.write_tags(tiff, [exif.micro_video_tag()])
        self.assertEqual(_makernote_offset(updated), offset)
        self.assertEqual(updated[offset:offset + len(makernote)], makernote)
        info = exif.describe(updated)
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO][2], 1)
        self.assertEqual(info["exif_ifd"][0x927C][2], makernote)
        self.assertEqual(info["exif_ifd"][0x9286][2], b"ASCII\x00\x00\x00Old comment")


def _makernote_offset(tiff):
    endian = "<" if tiff[:2] == b"II" else ">"
    ifd0_offset = struct.unpack(endian + "I", tiff[4:8])[0]
    count = struct.unpack(endian + "H", tiff[ifd0_offset:ifd0_offset + 2])[0]
    exif_ifd_offset = None
    for index in range(count):
        base = ifd0_offset + 2 + 12 * index
        if struct.unpack(endian + "H", tiff[base:base + 2])[0] == 0x8769:
            exif_ifd_offset = struct.unpack(endian + "I", tiff[base + 8:base + 12])[0]
    if exif_ifd_offset is None:
        raise AssertionError("样例里没有 ExifIFD")
    exif_count = struct.unpack(endian + "H", tiff[exif_ifd_offset:exif_ifd_offset + 2])[0]
    for index in range(exif_count):
        base = exif_ifd_offset + 2 + 12 * index
        if struct.unpack(endian + "H", tiff[base:base + 2])[0] == 0x927C:
            return struct.unpack(endian + "I", tiff[base + 8:base + 12])[0]
    raise AssertionError("样例里没有 MakerNote")


class UnusableInputTest(unittest.TestCase):
    def test_rejects_short_data(self):
        with self.assertRaises(exif.ExifError):
            exif.write_tags(b"II*\x00", [exif.micro_video_tag()])

    def test_rejects_bad_magic(self):
        bad = bytearray(fixtures.TIFF_EXIF)
        bad[2:4] = struct.pack("<H", 43)
        with self.assertRaises(exif.ExifError):
            exif.write_tags(bytes(bad), [exif.micro_video_tag()])

    def test_rejects_bad_byte_order(self):
        bad = b"XX" + fixtures.TIFF_EXIF[2:]
        with self.assertRaises(exif.ExifError):
            exif.write_tags(bad, [exif.micro_video_tag()])

    def test_rejects_out_of_range_ifd(self):
        bad = bytearray(fixtures.TIFF_EXIF)
        bad[4:8] = struct.pack("<I", 10 ** 6)
        with self.assertRaises(exif.ExifError):
            exif.write_tags(bytes(bad), [exif.micro_video_tag()])


class BuildTiffTest(unittest.TestCase):
    def test_minimal_tiff_is_readable(self):
        tiff = exif.build_tiff([exif.micro_video_tag()], [exif.user_comment_tag("Oplus_1")])
        info = exif.describe(tiff)
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO][2], 1)
        self.assertEqual(info["exif_ifd"][0x9286][2], b"ASCII\x00\x00\x00Oplus_1")
        self.assertEqual(info["ifd1"], {})

    def test_minimal_tiff_can_be_written_again(self):
        tiff = exif.build_tiff([exif.micro_video_tag()])
        again = exif.write_tags(tiff, [exif.extra_vendor_tags()[0]])
        info = exif.describe(again)
        self.assertEqual(raw_ifd0_tags(again).count(exif.TAG_XIAOMI_MICRO_VIDEO), 1)
        self.assertIn(exif.TAG_XIAOMI_MICRO_VIDEO_EXTRA, info["ifd0"])


if __name__ == "__main__":
    unittest.main()
