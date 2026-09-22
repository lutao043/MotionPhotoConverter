"""组装、校验与 CLI 的集成测试。"""

import contextlib
import io
import os
import shutil
import struct
import tempfile
import unittest

from motionphoto import convert, exif, jpeg, xmp
from tests import fixtures


def convert_sample(jpeg_bytes=None, mp4_bytes=None, **options):
    jpeg_bytes = fixtures.build_jpeg() if jpeg_bytes is None else jpeg_bytes
    mp4_bytes = fixtures.build_mp4() if mp4_bytes is None else mp4_bytes
    opts = convert.Options(**options)
    return convert.build_motion_photo(jpeg_bytes, mp4_bytes, opts), jpeg_bytes, mp4_bytes


class MultiItemFileTest(unittest.TestCase):
    """真机（vivo）会在视频前再放 GainMap 与未列出的厂商数据，校验必须照得住。"""

    def setUp(self):
        self.data, self.mp4, self.gainmap = fixtures.build_vivo_style_output()
        self.report = convert.verify_bytes(self.data, vendor="vivo")

    def test_real_device_layout_passes(self):
        self.assertTrue(self.report.ok, [check.render() for check in self.report.checks])
        # 自尾回推这条（真机读取器用的）必须精确命中视频
        self.assertEqual(self.report.video_start_backward, self.report.ftyp_offset)
        self.assertEqual(
            self.report.ftyp_offset, self.report.jpeg_length + len(self.gainmap) + 64
        )

    def test_unlisted_vendor_data_only_produces_notes(self):
        details = " ".join(check.detail for check in self.report.checks if not check.ok)
        self.assertIn("未列出", details)
        self.assertTrue(all(not check.critical for check in self.report.checks if not check.ok))

    def test_gainmap_item_is_recognised(self):
        semantics = [item.semantic for item in self.report.meta.items]
        self.assertEqual(semantics, ["Primary", "GainMap", "MotionPhoto"])
        self.assertIsNone(self.report.meta.items[0].length)

    def test_directory_overrun_is_a_hard_failure(self):
        # GainMap 的 Length 声明得比实际大 → 累加超过视频起点，属于真损坏
        data, mp4, _ = fixtures.build_vivo_style_output(gainmap_extra=200)
        report = convert.verify_bytes(data, vendor="vivo")
        self.assertFalse(report.ok)
        self.assertIn("自头累加 == MP4 起点", [check.name for check in report.failures])

    def test_wrong_video_length_breaks_backward_rule(self):
        # 视频项 Length 声明得比实际短 → 自尾回推会落在视频中间，属于硬错误
        data, _, _ = fixtures.build_vivo_style_output(video_length_delta=-500)
        report = convert.verify_bytes(data, vendor="vivo")
        self.assertFalse(report.ok)
        self.assertIn("自尾回推 == MP4 起点", [check.name for check in report.failures])

    def test_dropping_gainmap_is_reported(self):
        result = convert.build_motion_photo(self.data, self.mp4, convert.Options(vendor="vivo"))
        self.assertTrue(any("GainMap" in text and "丢弃" in text for text in result.warnings))
        self.assertTrue(convert.verify_bytes(result.data, vendor="vivo").ok)

    def test_vivo_style_source_under_other_profile_drops_vendor_fields(self):
        result = convert.build_motion_photo(self.data, self.mp4, convert.Options(vendor="xiaomi"))
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        props = report.meta.properties
        self.assertNotIn("VCamera:VMediaKitVersion", props)
        self.assertEqual(props["GCamera:MicroVideoOffset"], str(len(self.mp4)))
        self.assertEqual(props["GCamera:MicroVideoPresentationTimestampUs"], "0")


class BuildMotionPhotoTest(unittest.TestCase):
    def test_video_is_appended_byte_exact(self):
        result, _, mp4 = convert_sample()
        self.assertEqual(result.data[-len(mp4):], mp4)

    def test_image_data_is_untouched(self):
        result, jpeg_bytes, _ = convert_sample()
        self.assertEqual(
            fixtures.jpeg_image_tail(result.data), fixtures.jpeg_image_tail(jpeg_bytes)
        )

    def test_no_padding_bytes_between_image_and_video(self):
        result, _, _ = convert_sample()
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        # 追加数据必须紧跟主图 EOI，中间不能有 NUL 填充（旧版在这里塞了 32 个 0）
        self.assertEqual(result.data[report.jpeg_length - 2:report.jpeg_length], jpeg.EOI)
        self.assertEqual(result.data[report.jpeg_length:report.jpeg_length + 8][4:8], b"ftyp")
        self.assertEqual(report.video_start_forward, report.jpeg_length)

    def test_xmp_sits_in_app1_before_image_data(self):
        result, _, _ = convert_sample()
        segment = jpeg.find_app_segment(result.data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        self.assertIsNotNone(segment)
        _, eoi = jpeg.parse_structure(result.data)
        self.assertLess(segment.offset, eoi)
        body = jpeg.segment_body(result.data, segment)
        self.assertTrue(body.startswith(jpeg.XMP_SIGNATURE))
        self.assertIn("GCamera:MotionPhoto=\"1\"".encode("utf-8"), body)

    def test_metadata_matches_video_length(self):
        result, _, mp4 = convert_sample()
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        meta = report.meta
        self.assertTrue(meta.present)
        self.assertEqual(meta.micro_video_offset, len(mp4))
        self.assertEqual(meta.timestamp_us, 0)
        self.assertEqual(meta.video_item.mime, "video/mp4")
        self.assertEqual(meta.video_item.semantic, "MotionPhoto")
        self.assertEqual(meta.video_item.length, len(mp4))
        self.assertEqual(meta.items[0].semantic, "Primary")
        self.assertEqual(meta.items[0].length, 0)

    def test_both_locating_rules_agree_on_ftyp(self):
        result, _, mp4 = convert_sample()
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        self.assertEqual(report.video_start_backward, report.ftyp_offset)
        self.assertEqual(report.video_start_forward, report.ftyp_offset)
        self.assertEqual(report.ftyp_offset, len(result.data) - len(mp4))

    def test_xiaomi_exif_tag_written(self):
        result, _, _ = convert_sample()
        segment = jpeg.find_app_segment(result.data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
        info = exif.describe(jpeg.segment_body(result.data, segment)[6:])
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO][2], 1)
        self.assertEqual(info["ifd0"][0x0112][2], 6)  # 原 Orientation 保留

    def test_vendor_exif_can_be_disabled(self):
        result, _, _ = convert_sample(vendor_exif=False)
        segment = jpeg.find_app_segment(result.data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
        info = exif.describe(jpeg.segment_body(result.data, segment)[6:])
        self.assertNotIn(exif.TAG_XIAOMI_MICRO_VIDEO, info["ifd0"])
        # 关掉私有标记后依然能通过校验（这些标记不是致命项）
        self.assertTrue(convert.verify_bytes(result.data).ok)

    def test_google_profile_has_no_legacy_fields(self):
        result, _, mp4 = convert_sample(vendor="google")
        meta = convert.verify_bytes(result.data).meta
        self.assertIsNone(meta.micro_video_offset)
        self.assertEqual(meta.video_item.length, len(mp4))
        self.assertTrue(convert.verify_bytes(result.data).ok)

    def test_vivo_profile_writes_the_three_vendor_fields(self):
        result, _, mp4 = convert_sample(vendor="vivo")
        props = convert.verify_bytes(result.data, vendor="vivo").meta.properties
        self.assertEqual(props["VCamera:VMotionPhotoVersion"], "1")
        self.assertEqual(props["VCamera:VMotionPhotoSource"], "1")
        self.assertEqual(props["VCamera:VMediaKitVersion"], "1.0.0.5")
        self.assertIn(xmp.NS_VCAMERA, xmp.build_packet("vivo", 1, 0))
        self.assertTrue(convert.verify_bytes(result.data, vendor="vivo").ok)
        # vivo 档位不写 MicroVideo 旧字段
        self.assertNotIn("GCamera:MicroVideoOffset", props)

    def test_oppo_profile_writes_mpf_and_user_comment(self):
        result, _, mp4 = convert_sample(vendor="oppo")
        data = result.data
        mpf = jpeg.find_app_segment(data, jpeg.MARKER_APP2, jpeg.MPF_SIGNATURE)
        self.assertIsNotNone(mpf, "OPPO 档位应写入 MPF APP2 段")
        body = jpeg.segment_body(data, mpf)[len(jpeg.MPF_SIGNATURE):]
        size = _mpf_primary_size(body)
        report = convert.verify_bytes(data)
        self.assertEqual(size, report.jpeg_length)

        meta = report.meta
        self.assertEqual(meta.properties["OpCamera:MotionPhotoOwner"], "oplus")
        self.assertEqual(meta.properties["OpCamera:VideoLength"], str(len(mp4)))
        self.assertEqual(meta.video_item.length, len(mp4))

        segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
        info = exif.describe(jpeg.segment_body(data, segment)[6:])
        self.assertEqual(info["exif_ifd"][0x9286][2], b"ASCII\x00\x00\x00Oplus_8388608")

    def test_timestamp_out_of_range_falls_back(self):
        result, _, _ = convert_sample(timestamp_us=10_000_000)
        self.assertEqual(result.timestamp_us, 0)
        self.assertTrue(any("超出视频时长" in text for text in result.warnings))

    def test_timestamp_within_range_is_kept(self):
        result, _, _ = convert_sample(timestamp_us=1_500_000)
        self.assertEqual(result.timestamp_us, 1_500_000)
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        self.assertEqual(report.meta.timestamp_us, 1_500_000)
        # MicroVideoOffset 是「自文件末尾回数」的视频字节数
        self.assertEqual(report.meta.micro_video_offset, result.video_length)

    def test_exif_segment_precedes_xmp(self):
        for jpeg_bytes in (fixtures.build_jpeg(with_exif=True), fixtures.build_jpeg(with_exif=False)):
            result, _, _ = convert_sample(jpeg_bytes=jpeg_bytes)
            order = [
                "exif" if result.data[seg.body_offset:seg.body_offset + 6] == b"Exif\x00\x00" else "xmp"
                for seg in jpeg.inspect_segments(result.data)
                if seg.marker == jpeg.MARKER_APP1
            ]
            self.assertEqual(order, ["exif", "xmp"])

    def test_source_without_exif_gets_one(self):
        result, _, _ = convert_sample(jpeg_bytes=fixtures.build_jpeg(with_exif=False))
        self.assertTrue(any("没有 Exif 段" in text for text in result.warnings))
        segment = jpeg.find_app_segment(result.data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
        info = exif.describe(jpeg.segment_body(result.data, segment)[6:])
        self.assertEqual(info["ifd0"][exif.TAG_XIAOMI_MICRO_VIDEO][2], 1)

    def test_corrupt_exif_is_skipped_with_warning(self):
        bad = bytearray(fixtures.build_jpeg())
        marker = bad.index(b"Exif\x00\x00")
        bad[marker + 6 + 2:marker + 6 + 4] = struct.pack("<H", 99)  # 破坏 TIFF 魔数
        result, _, _ = convert_sample(jpeg_bytes=bytes(bad))
        self.assertTrue(any("Exif 结构异常" in text for text in result.warnings))
        self.assertTrue(convert.verify_bytes(result.data).ok)

    def test_existing_trailing_data_is_stripped_with_warning(self):
        jpeg_bytes = fixtures.build_jpeg() + b"\x00" * 24
        result, _, _ = convert_sample(jpeg_bytes=jpeg_bytes)
        self.assertTrue(any("EOI 之后已有" in text for text in result.warnings))
        self.assertTrue(convert.verify_bytes(result.data, vendor="xiaomi").ok)

    def test_existing_device_style_xmp_is_merged_not_duplicated(self):
        packet = fixtures.build_device_style_packet(500)
        jpeg_bytes = fixtures.build_jpeg(with_xmp=True)
        jpeg_bytes = jpeg.upsert_app_segment(
            jpeg_bytes, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE,
            jpeg.XMP_SIGNATURE + packet.encode("utf-8"),
        )
        result, _, mp4 = convert_sample(jpeg_bytes=jpeg_bytes)
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        self.assertEqual(report.meta.micro_video_offset, len(mp4))
        data = result.data
        # 无关元数据必须保留（dc:description 是容器元素，按原文比对）
        segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        self.assertIn("保留我", jpeg.segment_body(data, segment).decode("utf-8"))
        self.assertEqual(_count_xmp_segments(data), 1)
        self.assertEqual(data.count(b"<Container:Directory>"), 1)

    def test_duplicate_xmp_segments_are_cleaned_up(self):
        # 真机文件里出现过重复的 XMP 段，产物必须只剩一段
        jpeg_bytes = fixtures.build_jpeg(with_xmp=True)
        stale = jpeg.XMP_SIGNATURE + fixtures.build_device_style_packet(500).encode("utf-8")
        jpeg_bytes = jpeg.upsert_app_segment(
            jpeg_bytes, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE, stale
        )
        jpeg_bytes = jpeg_bytes[:2] + jpeg.build_app_segment(jpeg.MARKER_APP1, stale) + jpeg_bytes[2:]
        self.assertEqual(_count_xmp_segments(jpeg_bytes), 2)

        result, _, mp4 = convert_sample(jpeg_bytes=jpeg_bytes)
        self.assertEqual(_count_xmp_segments(result.data), 1)
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        self.assertEqual(report.meta.micro_video_offset, len(mp4))

    def test_reconversion_is_idempotent(self):
        first, _, mp4 = convert_sample()
        second = convert.build_motion_photo(first.data, mp4, convert.Options())
        report = convert.verify_bytes(second.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        self.assertEqual(_count_xmp_segments(second.data), 1)
        self.assertEqual(second.data[-len(mp4):], mp4)
        self.assertTrue(any("EOI 之后已有" in text for text in second.warnings))


class RejectionTest(unittest.TestCase):
    def test_mp4_without_moov_rejected(self):
        with self.assertRaises(convert.ConvertError) as ctx:
            convert_sample(mp4_bytes=fixtures.build_mp4_without_moov())
        self.assertIn("moov", str(ctx.exception))

    def test_non_jpeg_rejected(self):
        with self.assertRaises(convert.ConvertError):
            convert_sample(jpeg_bytes=b"not a jpeg")

    def test_empty_mp4_rejected(self):
        with self.assertRaises(convert.ConvertError):
            convert_sample(mp4_bytes=b"")


class LegacyRegressionTest(unittest.TestCase):
    """旧版脚本的产物必须被判定为不合格——这正是 issue 里「小米不支持」的原因。"""

    def setUp(self):
        self.jpeg = fixtures.build_jpeg()
        self.mp4 = fixtures.build_mp4()
        self.legacy = fixtures.build_legacy_output(self.jpeg, self.mp4)

    def test_legacy_output_fails_verification(self):
        report = convert.verify_bytes(self.legacy)
        self.assertFalse(report.ok)
        names = [check.name for check in report.failures]
        self.assertIn("APP1 段内存在 XMP", names)

    def test_legacy_xmp_is_not_in_any_app1_segment(self):
        self.assertIsNone(jpeg.find_app_segment(self.legacy, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE))

    def test_legacy_metadata_has_no_usable_length(self):
        packet = fixtures.legacy_xmp_packet().decode("utf-8")
        meta = xmp.read_meta(packet)
        self.assertIsNone(meta.video_item.length)
        self.assertEqual(meta.timestamp_us, 123456789)

    def test_new_code_can_repair_a_legacy_file(self):
        result = convert.build_motion_photo(self.legacy, self.mp4, convert.Options())
        report = convert.verify_bytes(result.data, vendor="xiaomi")
        self.assertTrue(report.ok, [check.render() for check in report.checks])


class OutputNameTest(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(convert.output_name("photo"), "photo_livePhoto.jpg")
        self.assertEqual(convert.output_name("photo", "keep"), "photo.jpg")
        self.assertEqual(convert.output_name("photo", "mvimg"), "MVIMG_photo.jpg")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="motionphoto-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write_sample(self, name="photo"):
        with open(os.path.join(self.root, name + ".jpg"), "wb") as handle:
            handle.write(fixtures.build_jpeg())
        with open(os.path.join(self.root, name + ".mp4"), "wb") as handle:
            handle.write(fixtures.build_mp4())

    def _run(self, argv):
        import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(argv)
        return code, buffer.getvalue()

    def test_batch_conversion_and_delete_source(self):
        self._write_sample()
        code, output = self._run([self.root, "--delete-source"])
        self.assertEqual(code, 0, output)
        out_path = os.path.join(self.root, "photo_livePhoto.jpg")
        self.assertTrue(os.path.exists(out_path))
        self.assertFalse(os.path.exists(os.path.join(self.root, "photo.jpg")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "photo.mp4")))
        self.assertTrue(convert.verify_file(out_path, vendor="xiaomi").ok)

    def test_second_run_skips_existing_output(self):
        self._write_sample()
        self._run([self.root])
        code, output = self._run([self.root])
        self.assertEqual(code, 0, output)
        self.assertIn("已存在同名产物", output)

    def test_force_overwrites(self):
        self._write_sample()
        self._run([self.root])
        code, output = self._run([self.root, "--force"])
        self.assertEqual(code, 0, output)
        self.assertIn("已生成", output)

    def test_dry_run_writes_nothing(self):
        self._write_sample()
        code, output = self._run([self.root, "--dry-run"])
        self.assertEqual(code, 0, output)
        self.assertIn("试运行", output)
        self.assertFalse(os.path.exists(os.path.join(self.root, "photo_livePhoto.jpg")))

    def test_verify_reports_legacy_file(self):
        with open(os.path.join(self.root, "legacy.jpg"), "wb") as handle:
            handle.write(fixtures.build_legacy_output(fixtures.build_jpeg(), fixtures.build_mp4()))
        code, output = self._run(["--verify", self.root])
        self.assertEqual(code, 2)
        self.assertIn("未通过", output)

    def test_verify_accepts_fresh_output(self):
        self._write_sample()
        self._run([self.root])
        code, output = self._run(["--verify", self.root])
        self.assertEqual(code, 0, output)

    def test_inspect_prints_diagnostics(self):
        self._write_sample()
        self._run([self.root])
        code, output = self._run(["--inspect", os.path.join(self.root, "photo_livePhoto.jpg")])
        self.assertEqual(code, 0, output)
        self.assertIn("定位结果", output)
        self.assertIn("App1".lower(), output.lower())
        self.assertIn("0x8897", output)

    def test_failure_reports_nonzero(self):
        self._write_sample()
        # 把视频破坏掉，必须报失败而不是静默产出坏文件
        with open(os.path.join(self.root, "photo.mp4"), "wb") as handle:
            handle.write(fixtures.build_mp4_without_moov())
        code, output = self._run([self.root])
        self.assertEqual(code, 2)
        self.assertIn("moov", output)

    def test_usage_error_without_arguments(self):
        code, _ = self._run([])
        self.assertEqual(code, 1)

    def test_missing_directory(self):
        code, output = self._run([os.path.join(self.root, "nope")])
        self.assertEqual(code, 1)
        self.assertIn("不是一个有效目录", output)


def _count_xmp_segments(data):
    count = 0
    for seg in jpeg.inspect_segments(data):
        if seg.marker == jpeg.MARKER_APP1 and data[
            seg.body_offset:seg.body_offset + len(jpeg.XMP_SIGNATURE)
        ] == jpeg.XMP_SIGNATURE:
            count += 1
    return count


def _mpf_primary_size(tiff):
    """从 MPF 的 TIFF 结构里读出首个 MPEntry 的「主图字节数」（大端）。"""
    endian = "<" if tiff[:2] == b"II" else ">"
    ifd_offset = struct.unpack(endian + "I", tiff[4:8])[0]
    count = struct.unpack(endian + "H", tiff[ifd_offset:ifd_offset + 2])[0]
    entry_value_offset = None
    for index in range(count):
        base = ifd_offset + 2 + 12 * index
        tag = struct.unpack(endian + "H", tiff[base:base + 2])[0]
        if tag == 0xB002:
            entry_value_offset = struct.unpack(endian + "I", tiff[base + 8:base + 12])[0]
    if entry_value_offset is None:
        raise AssertionError("MPF 里没有 MPEntry 条目")
    return struct.unpack(">I", tiff[entry_value_offset + 4:entry_value_offset + 8])[0]


if __name__ == "__main__":
    unittest.main()
