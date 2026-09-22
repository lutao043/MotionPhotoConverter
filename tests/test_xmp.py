"""XMP 生成、合并与读取测试。"""

import unittest

from motionphoto import xmp
from tests import fixtures


class BuildPacketTest(unittest.TestCase):
    def test_xiaomi_profile_has_standard_and_legacy_fields(self):
        packet = xmp.build_packet("xiaomi", 4096, 0)
        props = xmp.parse_properties(packet)
        self.assertEqual(props["GCamera:MotionPhoto"], "1")
        self.assertEqual(props["GCamera:MotionPhotoVersion"], "1")
        self.assertEqual(props["GCamera:MotionPhotoPresentationTimestampUs"], "0")
        self.assertEqual(props["GCamera:MicroVideo"], "1")
        self.assertEqual(props["GCamera:MicroVideoOffset"], "4096")
        self.assertEqual(props["GCamera:MicroVideoPresentationTimestampUs"], "0")

    def test_items_use_attribute_form_with_correct_length(self):
        packet = xmp.build_packet("xiaomi", 4096, 0)
        # 真机用属性写法，不是 <Item:Mime>…</Item:Mime>
        self.assertIn('<Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"', packet)
        self.assertIn('Item:Length="4096"', packet)
        self.assertNotIn("<Item:Mime>", packet)

        items = xmp.parse_items(packet)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].mime, "image/jpeg")
        self.assertEqual(items[0].semantic, "Primary")
        self.assertEqual(items[0].length, 0)
        self.assertEqual(items[0].padding, 0)
        self.assertEqual(items[1].mime, "video/mp4")
        self.assertEqual(items[1].semantic, "MotionPhoto")
        self.assertEqual(items[1].length, 4096)

    def test_xpacket_header_contains_bom(self):
        packet = xmp.build_packet("google", 10, 0)
        self.assertTrue(packet.startswith('<?xpacket begin="\ufeff"'), packet[:40])
        self.assertIn('<?xpacket end="w"?>', packet)

    def test_google_and_samsung_profiles_skip_legacy_fields(self):
        for vendor in ("google", "samsung"):
            packet = xmp.build_packet(vendor, 2048, 0)
            props = xmp.parse_properties(packet)
            self.assertNotIn("GCamera:MicroVideoOffset", props, vendor)
            self.assertEqual(props["GCamera:MotionPhoto"], "1", vendor)

    def test_oppo_profile_adds_opcamera_namespace(self):
        packet = xmp.build_packet("oppo", 2048, 0)
        props = xmp.parse_properties(packet)
        self.assertEqual(props["OpCamera:MotionPhotoOwner"], "oplus")
        self.assertEqual(props["OpCamera:OLivePhotoVersion"], "2")
        self.assertEqual(props["OpCamera:MotionPhotoFeatureFlag"], "1")
        self.assertEqual(props["OpCamera:VideoLength"], "2048")
        self.assertIn(xmp.NS_OPCAMERA, packet)
        self.assertNotIn("GCamera:MicroVideoOffset", props)


class MergePacketTest(unittest.TestCase):
    def setUp(self):
        self.existing = fixtures.build_device_style_packet(500)

    def test_keeps_unrelated_metadata(self):
        merged = xmp.merge_packet(self.existing, "xiaomi", 900, 0)
        self.assertIn("保留我", merged)

    def test_replaces_stale_values(self):
        merged = xmp.merge_packet(self.existing, "xiaomi", 900, 0)
        props = xmp.parse_properties(merged)
        self.assertEqual(props["GCamera:MicroVideoOffset"], "900")
        self.assertEqual(props["GCamera:MotionPhotoPresentationTimestampUs"], "0")
        self.assertNotIn("999999", merged)

    def test_does_not_accumulate_directories(self):
        merged = xmp.merge_packet(self.existing, "xiaomi", 900, 0)
        self.assertEqual(merged.count("<Container:Directory>"), 1)
        self.assertEqual(merged.count('Item:Mime="video/mp4"'), 1)
        items = xmp.parse_items(merged)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1].length, 900)

    def test_google_profile_strips_legacy_fields(self):
        merged = xmp.merge_packet(self.existing, "google", 900, 0)
        props = xmp.parse_properties(merged)
        self.assertNotIn("GCamera:MicroVideoOffset", props)
        self.assertNotIn("GCamera:MicroVideo", props)

    def test_merging_twice_is_stable(self):
        once = xmp.merge_packet(self.existing, "xiaomi", 900, 0)
        twice = xmp.merge_packet(once, "xiaomi", 900, 0)
        self.assertEqual(once.count("<Container:Directory>"), twice.count("<Container:Directory>"))
        self.assertEqual(xmp.parse_properties(once), xmp.parse_properties(twice))

    def test_broken_packet_raises(self):
        with self.assertRaises(xmp.XmpError):
            xmp.merge_packet("<x:xmpmeta></x:xmpmeta>", "xiaomi", 100, 0)


class ReadMetaTest(unittest.TestCase):
    def test_legacy_child_element_form_is_readable(self):
        packet = fixtures.legacy_xmp_packet().decode("utf-8")
        meta = xmp.read_meta(packet)
        self.assertTrue(meta.present)
        self.assertEqual(meta.timestamp_us, 123456789)
        items = meta.items
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].mime, "image/jpeg")
        self.assertEqual(items[0].length, 0)
        self.assertEqual(items[0].padding, 32)
        # 视频项没有 Length —— 这正是旧版产物的致命缺陷
        self.assertEqual(items[1].mime, "video/mp4")
        self.assertIsNone(items[1].length)
        self.assertIsNone(meta.video_item.length)

    def test_alias_prefix_is_normalised(self):
        packet = fixtures.legacy_xmp_packet().decode("utf-8")
        meta = xmp.read_meta(packet)
        self.assertEqual(meta.properties["GCamera:MotionPhoto"], "1")

    def test_new_packet_round_trip(self):
        meta = xmp.read_meta(xmp.build_packet("xiaomi", 777, 2500))
        self.assertTrue(meta.present)
        self.assertEqual(meta.micro_video_offset, 777)
        self.assertEqual(meta.timestamp_us, 2500)
        self.assertEqual(meta.video_item.length, 777)
        self.assertEqual(meta.video_item.mime, "video/mp4")


if __name__ == "__main__":
    unittest.main()
