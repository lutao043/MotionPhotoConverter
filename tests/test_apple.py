"""苹果档位（--vendor apple）的测试：静止图 MakerNote、成对产出、校验与 CLI。"""

import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from motionphoto import convert, exif, jpeg, mov, xmp

from . import fixtures

UUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


def jpeg_with_makernote(maker_blob: bytes, base_tiff=None) -> bytes:
    """造一张带指定 MakerNote 的 JPEG（用现有 Exif 段或新建）。"""
    base = fixtures.build_jpeg(with_exif=base_tiff is not None)
    if base_tiff is not None:
        tiff = exif.write_tags(
            base_tiff, exif_ifd_tags=[exif.TagSpec(exif.TAG_MAKER_NOTE, exif.TYPE_UNDEFINED, maker_blob)]
        )
        segment = jpeg.find_app_segment(base, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
        base = base[:segment.offset] + base[segment.end:]
        base = jpeg.upsert_app_segment(
            base, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE, jpeg.EXIF_SIGNATURE + tiff
        )
    else:
        tiff = exif.build_tiff(
            exif_ifd_tags=[exif.TagSpec(exif.TAG_MAKER_NOTE, exif.TYPE_UNDEFINED, maker_blob)]
        )
        base = jpeg.upsert_app_segment(
            base, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE, jpeg.EXIF_SIGNATURE + tiff
        )
    return base


def jpeg_with_xmp(packet: str, with_exif: bool = True) -> bytes:
    """造一张带指定 XMP 包的 JPEG。"""
    base = fixtures.build_jpeg(with_exif=with_exif)
    return jpeg.upsert_app_segment(
        base, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE, jpeg.XMP_SIGNATURE + packet.encode("utf-8")
    )


def read_still_identifier(photo: bytes):
    segment = jpeg.find_app_segment(photo, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    if segment is None:
        return None
    return exif.read_apple_identifier(jpeg.segment_body(photo, segment)[len(jpeg.EXIF_SIGNATURE):])


def read_makernote_blob(photo: bytes) -> bytes:
    segment = jpeg.find_app_segment(photo, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    return exif.read_makernote(jpeg.segment_body(photo, segment)[len(jpeg.EXIF_SIGNATURE):])


class BuildLivePhotoTest(unittest.TestCase):
    def options(self, **kwargs):
        return convert.Options(vendor="apple", **kwargs)

    def test_pairs_matching_identifiers(self):
        result = convert.build_live_photo(fixtures.build_jpeg(), fixtures.build_mp4(), self.options())

        report = convert.verify_live_photo_pair(result.photo, result.video)
        self.assertTrue(report.ok, [check.render() for check in report.failures])
        self.assertEqual(report.photo_identifier, report.video_identifier)
        self.assertEqual(result.identifier, report.photo_identifier)
        self.assertEqual(report.identifier, report.identifier.upper())

    def test_image_data_and_media_data_untouched(self):
        jpeg_bytes = fixtures.build_jpeg()
        mp4_bytes = fixtures.build_mp4()

        result = convert.build_live_photo(jpeg_bytes, mp4_bytes, self.options())

        self.assertEqual(fixtures.jpeg_image_tail(result.photo), fixtures.jpeg_image_tail(jpeg_bytes))
        self.assertEqual(fixtures.mov_mdat_payload(result.video), fixtures.mov_mdat_payload(mp4_bytes))

    def test_photo_has_no_trailing_data(self):
        source = fixtures.build_jpeg(trailing=b"\x00" * 16 + fixtures.build_mp4())
        result = convert.build_live_photo(source, fixtures.build_mp4(), self.options())

        _, eoi = jpeg.parse_structure(result.photo)
        self.assertEqual(eoi + 2, len(result.photo))
        self.assertTrue(any("剥离" in text for text in result.warnings))

    def test_identifier_is_uppercase_uuid(self):
        result = convert.build_live_photo(fixtures.build_jpeg(), fixtures.build_mp4(), self.options())
        self.assertEqual(len(result.identifier), 36)
        self.assertEqual(result.identifier, result.identifier.upper())
        self.assertEqual(result.identifier.count("-"), 4)

    def test_strips_motion_photo_xmp_but_keeps_others(self):
        source = jpeg_with_xmp(fixtures.build_device_style_packet(1234))

        result = convert.build_live_photo(source, fixtures.build_mp4(), self.options())

        segment = jpeg.find_app_segment(result.photo, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        self.assertIsNotNone(segment, "还有无关元数据，XMP 段应保留")
        packet = jpeg.segment_body(result.photo, segment)[len(jpeg.XMP_SIGNATURE):].decode("utf-8")
        self.assertIn("保留我", packet)
        for key in xmp.parse_properties(packet):
            self.assertFalse(key.startswith(("GCamera:", "Container:", "Item:")), key)
        self.assertEqual(xmp.parse_items(packet), [])

    def test_drops_xmp_segment_when_only_motion_photo_left(self):
        source = fixtures.build_jpeg(with_xmp=True)
        result = convert.build_live_photo(source, fixtures.build_mp4(), self.options())
        self.assertIsNone(
            jpeg.find_app_segment(result.photo, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
        )

    def test_source_without_exif_gets_new_exif(self):
        result = convert.build_live_photo(
            fixtures.build_jpeg(with_exif=False), fixtures.build_mp4(), self.options()
        )
        self.assertEqual(read_still_identifier(result.photo), result.identifier)
        self.assertTrue(any("没有 Exif 段" in text for text in result.warnings))

    def test_patches_existing_apple_makernote_in_place(self):
        original = fixtures.build_apple_makernote(UUID)
        source = jpeg_with_makernote(original, base_tiff=fixtures.TIFF_EXIF)

        result = convert.build_live_photo(source, fixtures.build_mp4(), self.options())

        # 源图里已经有标识符，沿用；MakerNote 长度不变、其它条目原样
        self.assertEqual(result.identifier, UUID)
        patched = read_makernote_blob(result.photo)
        self.assertEqual(len(patched), len(original))
        self.assertEqual(patched, original)
        self.assertFalse(any("MakerNote" in text for text in result.warnings))

    def test_replaces_foreign_makernote_with_warning(self):
        source = jpeg_with_makernote(b"Panasonic\x00\x00\x00" * 4, base_tiff=fixtures.TIFF_EXIF)

        result = convert.build_live_photo(source, fixtures.build_mp4(), self.options())

        self.assertTrue(read_makernote_blob(result.photo).startswith(b"Apple iOS"))
        self.assertTrue(any("其它厂商格式" in text for text in result.warnings))

    def test_reuses_identifier_from_video(self):
        mp4 = mov.set_content_identifier(fixtures.build_mp4(), UUID)
        result = convert.build_live_photo(fixtures.build_jpeg(), mp4, self.options())
        self.assertEqual(result.identifier, UUID)
        self.assertEqual(read_still_identifier(result.photo), UUID)

    def test_conflicting_identifiers_warn_and_prefer_photo(self):
        source = jpeg_with_makernote(fixtures.build_apple_makernote(UUID), base_tiff=fixtures.TIFF_EXIF)
        mp4 = mov.set_content_identifier(fixtures.build_mp4(), "11111111-2222-3333-4444-555555555555")

        result = convert.build_live_photo(source, mp4, self.options())

        self.assertEqual(result.identifier, UUID)
        self.assertTrue(any("各带一个不同的标识符" in text for text in result.warnings))

    def test_conversion_is_idempotent(self):
        first = convert.build_live_photo(fixtures.build_jpeg(), fixtures.build_mp4(), self.options())
        second = convert.build_live_photo(first.photo, first.video, self.options())

        self.assertEqual(first.identifier, second.identifier)
        self.assertEqual(first.photo, second.photo)
        self.assertEqual(first.video, second.video)

    def test_rejects_video_without_moov(self):
        with self.assertRaises(convert.ConvertError):
            convert.build_live_photo(
                fixtures.build_jpeg(), fixtures.build_mp4_without_moov(), self.options()
            )

    def test_warns_about_ignored_options(self):
        result = convert.build_live_photo(
            fixtures.build_jpeg(), fixtures.build_mp4(), self.options(timestamp_us=1000, vendor_exif=False)
        )
        self.assertTrue(any("--timestamp-us 已忽略" in text for text in result.warnings))
        self.assertTrue(any("--no-vendor-exif 已忽略" in text for text in result.warnings))

    def test_quicktime_source_video_is_not_warned(self):
        result = convert.build_live_photo(
            fixtures.build_jpeg(), fixtures.build_mov(), self.options()
        )
        self.assertFalse(any("QuickTime" in text for text in result.warnings))


class VerifyPairTest(unittest.TestCase):
    def setUp(self):
        result = convert.build_live_photo(fixtures.build_jpeg(), fixtures.build_mp4(), convert.Options())
        self.photo = result.photo
        self.video = result.video

    def test_ok_for_own_output(self):
        report = convert.verify_live_photo_pair(self.photo, self.video)
        self.assertTrue(report.ok, [check.render() for check in report.checks])
        names = {check.name for check in report.checks}
        self.assertIn("两侧标识符一致", names)
        self.assertIn("媒体数据都能取到", names)

    def test_flags_missing_still_image_time_track_as_hint(self):
        report = convert.verify_live_photo_pair(self.photo, self.video)
        hint = [check for check in report.checks if "still-image-time" in check.name]
        self.assertEqual(len(hint), 1)
        self.assertFalse(hint[0].critical)
        self.assertIn("TECHNICAL.md", hint[0].detail)

    def test_flags_identifier_mismatch(self):
        tampered = mov.set_content_identifier(self.video, "11111111-2222-3333-4444-555555555555")
        report = convert.verify_live_photo_pair(self.photo, tampered)
        self.assertFalse(report.ok)
        self.assertIn("两侧标识符一致", [check.name for check in report.failures])

    def test_flags_photo_without_makernote(self):
        report = convert.verify_live_photo_pair(fixtures.build_jpeg(), self.video)
        self.assertFalse(report.ok)
        self.assertIn("静止图带 Apple MakerNote", [check.name for check in report.failures])

    def test_flags_video_without_identifier(self):
        report = convert.verify_live_photo_pair(self.photo, fixtures.build_mov())
        self.assertFalse(report.ok)
        self.assertIn("视频写入了 content.identifier", [check.name for check in report.failures])


class NameTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(
            convert.live_photo_names("001"), ("001_livePhoto.JPG", "001_livePhoto.MOV")
        )
        self.assertEqual(convert.live_photo_names("001", "keep"), ("001.JPG", "001.MOV"))
        self.assertEqual(convert.live_photo_names("001", "mvimg"), ("IMG_001.JPG", "IMG_001.MOV"))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mpc-apple-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write_sample(self, name="001", video_suffix=".mp4"):
        jpg = os.path.join(self.dir, name + ".jpg")
        video = os.path.join(self.dir, name + video_suffix)
        with open(jpg, "wb") as handle:
            handle.write(fixtures.build_jpeg())
        with open(video, "wb") as handle:
            handle.write(fixtures.build_mp4())
        return jpg, video

    def run_cli(self, argv):
        from main import main

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_convert_then_verify(self):
        self.write_sample()
        code, output = self.run_cli([self.dir, "--vendor", "apple"])
        self.assertEqual(code, 0, output)
        photo = os.path.join(self.dir, "001_livePhoto.JPG")
        video = os.path.join(self.dir, "001_livePhoto.MOV")
        self.assertTrue(os.path.exists(photo) and os.path.exists(video))
        self.assertIn("导入提示", output)

        code, output = self.run_cli(["--verify", self.dir, "--vendor", "apple"])
        self.assertEqual(code, 0, output)
        self.assertIn("001_livePhoto.JPG + 001_livePhoto.MOV", output)

    def test_inspect_accepts_either_file_of_the_pair(self):
        self.write_sample()
        self.run_cli([self.dir, "--vendor", "apple"])
        code, output = self.run_cli(
            ["--inspect", os.path.join(self.dir, "001_livePhoto.MOV"), "--vendor", "apple"]
        )
        self.assertEqual(code, 0, output)
        self.assertIn("Apple 标识符：", output)
        self.assertIn("com.apple.quicktime.content.identifier", output)

    def test_dry_run_writes_nothing(self):
        self.write_sample()
        code, output = self.run_cli([self.dir, "--vendor", "apple", "--dry-run"])
        self.assertEqual(code, 0, output)
        self.assertIn("[试运行]", output)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "001_livePhoto.JPG")))

    def test_delete_source_keeps_outputs(self):
        jpg, video = self.write_sample()
        code, output = self.run_cli([self.dir, "--vendor", "apple", "--delete-source"])
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.exists(jpg))
        self.assertFalse(os.path.exists(video))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "001_livePhoto.JPG")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "001_livePhoto.MOV")))

    def test_skip_existing_without_force(self):
        self.write_sample()
        self.run_cli([self.dir, "--vendor", "apple"])
        code, output = self.run_cli([self.dir, "--vendor", "apple"])
        self.assertEqual(code, 0, output)
        self.assertIn("跳过", output)

    def test_keep_style_does_not_delete_its_own_output(self):
        jpg, video = self.write_sample()
        code, output = self.run_cli(
            [self.dir, "--vendor", "apple", "--name-style", "keep", "--force", "--delete-source"]
        )
        self.assertEqual(code, 0, output)
        # 产物与源图同名（只差扩展名大小写），在大小写不敏感的文件系统上就是同一个文件，
        # 不能把它当源文件删掉
        self.assertTrue(os.path.exists(os.path.join(self.dir, "001.JPG")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "001.MOV")))
        self.assertFalse(os.path.exists(video))

    def test_mov_source_is_accepted(self):
        self.write_sample(video_suffix=".mov")
        code, output = self.run_cli([self.dir, "--vendor", "apple"])
        self.assertEqual(code, 0, output)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "001_livePhoto.MOV")))

    def test_verify_skips_pair_without_identifiers(self):
        self.write_sample()
        code, output = self.run_cli(["--verify", self.dir, "--vendor", "apple"])
        self.assertEqual(code, 0, output)
        self.assertIn("没有 Live Photo 标识符", output)


if __name__ == "__main__":
    unittest.main()
