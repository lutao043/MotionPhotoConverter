"""组装动态照片：写元数据、拼视频、校验、诊断。

产物结构（与真机文件一致）::

    [JPEG: SOI · APP1(Exif) · APP2(MPF，仅 OPPO) · APP1(XMP) · … · EOI][MP4 原样追加]

两条定位规则都必须落到 MP4 的 ftyp 起点上，这是所有读取器认账的前提：

* 自尾回推：``视频起点 = 文件总长 − 视频项 Item:Length``（Google Photos / ExoPlayer）
* 自头累加：``主图 EOI + 各项 Length + Padding``
"""

import os
import uuid
from typing import List, NamedTuple, Optional, Tuple

from . import exif as exif_mod
from . import jpeg, mov, mp4, xmp

# 苹果档位是「一对文件」而不是单个 JPEG，因此不进 xmp 的属性表，
# 由 build_live_photo 单独处理（见本文件末尾的「苹果 Live Photo」一节）。
APPLE_VENDOR = "apple"
SUPPORTED_VENDORS = xmp.VENDORS + (APPLE_VENDOR,)
NAME_STYLES = ("suffix", "keep", "mvimg")
DEFAULT_NAME_STYLE = "suffix"

APPLE_PHOTO_SUFFIX = ".JPG"
APPLE_VIDEO_SUFFIX = ".MOV"

OPPO_USER_COMMENT = "Oplus_8388608"


class ConvertError(Exception):
    """无法生成有效的动态照片。"""


class Options(NamedTuple):
    vendor: str = xmp.DEFAULT_VENDOR
    timestamp_us: Optional[int] = None
    vendor_exif: bool = True
    extra_vendor_exif: bool = False
    name_style: str = DEFAULT_NAME_STYLE


class ConversionResult(NamedTuple):
    data: bytes
    warnings: List[str]
    video_length: int
    timestamp_us: int


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #

def build_motion_photo(jpeg_bytes: bytes, mp4_bytes: bytes, options: Options) -> ConversionResult:
    """把 JPEG 与 MP4 合成为动态照片，返回完整文件字节。"""
    warnings: List[str] = []

    try:
        _, eoi = jpeg.parse_structure(jpeg_bytes)
    except jpeg.JpegError as exc:
        raise ConvertError("源图不是有效的 JPEG：%s" % exc)

    if eoi + 2 < len(jpeg_bytes):
        warnings.append(
            "源图 EOI 之后已有 %d 字节数据（可能已经是动态照片），已剥离后重新拼接"
            % (len(jpeg_bytes) - eoi - 2)
        )

    video_length = len(mp4_bytes)
    if video_length == 0:
        raise ConvertError("MP4 文件为空")

    mp4_info, mp4_errors, mp4_warnings = mp4.validate(mp4_bytes)
    if mp4_errors:
        raise ConvertError("；".join(mp4_errors))
    warnings.extend(mp4_warnings)

    timestamp_us = _resolve_timestamp(options.timestamp_us, mp4_info, warnings)

    data = jpeg.strip_trailing_data(jpeg_bytes)
    existing_packet = _read_xmp_packet(data)
    _warn_about_dropped_items(existing_packet, warnings)

    # 1) 先写厂商 Exif 标记：Exif 段应排在 XMP 之前（常规布局）
    #    写入失败只告警，不影响主流程
    data = _write_vendor_exif(data, options, warnings)

    # 2) 写 XMP：先清掉已有的同名属性与重复的 XMP 段，再合并最新值
    if existing_packet is None:
        packet = xmp.build_packet(options.vendor, video_length, timestamp_us)
    else:
        try:
            packet = xmp.merge_packet(existing_packet, options.vendor, video_length, timestamp_us)
        except xmp.XmpError as exc:
            warnings.append("源图 XMP 无法合并（%s），已整体替换为新的 XMP 段" % exc)
            packet = xmp.build_packet(options.vendor, video_length, timestamp_us)

    data = jpeg.remove_all_app_segments(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
    data = jpeg.upsert_app_segment(
        data,
        jpeg.MARKER_APP1,
        jpeg.XMP_SIGNATURE,
        jpeg.XMP_SIGNATURE + packet.encode("utf-8"),
        anchor=(jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE),
    )

    # 3) OPPO 还需要 MPF（APP2）段
    if options.vendor == "oppo":
        data = _insert_mpf(data, warnings)

    output = data + mp4_bytes

    report = verify_bytes(output, vendor=options.vendor)
    if not report.ok:
        failed = "；".join(check.detail or check.name for check in report.failures)
        raise ConvertError("自检未通过，已放弃写出该文件：%s" % failed)

    return ConversionResult(
        data=output, warnings=warnings, video_length=video_length, timestamp_us=timestamp_us
    )


def _resolve_timestamp(requested: Optional[int], mp4_info: mp4.Mp4Info, warnings: List[str]) -> int:
    """确定封面帧时间戳。

    必须是视频时间轴上的真实位置；不知道就用 0（资料里缺失元数据时的标准回退值），
    绝不凭空造值——写错值会直接让小米相册无法播放。
    """
    timestamp_us = 0 if requested is None else int(requested)
    if requested is not None and requested < 0:
        warnings.append("封面帧时间戳不能为负，已改为 0")
        timestamp_us = 0
    if timestamp_us and mp4_info.duration_us is None:
        warnings.append("无法从 MP4 读出时长，无法校验封面帧时间戳 %d 是否越界" % timestamp_us)
    elif mp4_info.duration_us is not None and timestamp_us > mp4_info.duration_us:
        warnings.append(
            "封面帧时间戳 %d µs 超出视频时长 %.2f s，已回退为 0"
            % (timestamp_us, mp4_info.duration_seconds or 0.0)
        )
        timestamp_us = 0
    return timestamp_us


def _read_xmp_packet(data: bytes) -> Optional[str]:
    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
    if segment is None:
        return None
    body = jpeg.segment_body(data, segment)[len(jpeg.XMP_SIGNATURE):]
    return body.decode("utf-8", "replace")


def _warn_about_dropped_items(packet: Optional[str], warnings: List[str]) -> None:
    """源图若已带 GainMap 之类的附加项（Ultra HDR），本次合成会把它们丢掉，需明确告知。"""
    if packet is None:
        return
    extras = []
    for item in xmp.parse_items(packet):
        semantic = (item.semantic or "").lower()
        if semantic in ("primary", xmp.SEMANTIC_MOTION_PHOTO.lower()):
            continue
        if (item.mime or "").lower() == xmp.VIDEO_MIME:
            continue
        extras.append(item.semantic or item.mime or "未命名项")
    if extras:
        warnings.append(
            "源图带有附加项（%s），本次合成只保留主图与视频，这些附加项会被丢弃"
            % "、".join(extras)
        )


def _write_vendor_exif(data: bytes, options: Options, warnings: List[str]) -> bytes:
    ifd0_tags: List[exif_mod.TagSpec] = []
    exif_ifd_tags: List[exif_mod.TagSpec] = []

    if options.vendor == "xiaomi" and options.vendor_exif:
        ifd0_tags.append(exif_mod.micro_video_tag())
        if options.extra_vendor_exif:
            ifd0_tags.extend(exif_mod.extra_vendor_tags())
    if options.vendor == "oppo":
        exif_ifd_tags.append(exif_mod.user_comment_tag(OPPO_USER_COMMENT))
    if not ifd0_tags and not exif_ifd_tags:
        return data

    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    try:
        if segment is None:
            tiff = exif_mod.build_tiff(ifd0_tags, exif_ifd_tags)
            warnings.append("源图没有 Exif 段，已新建一个（仅含写入的标记）")
        else:
            body = jpeg.segment_body(data, segment)
            tiff = exif_mod.write_tags(body[len(jpeg.EXIF_SIGNATURE):], ifd0_tags, exif_ifd_tags)
        return jpeg.upsert_app_segment(
            data,
            jpeg.MARKER_APP1,
            jpeg.EXIF_SIGNATURE,
            jpeg.EXIF_SIGNATURE + tiff,
            anchor=(jpeg.MARKER_APP0, b"JFIF\x00"),
        )
    except exif_mod.ExifError as exc:
        warnings.append("Exif 结构异常，已跳过厂商标记写入（其余元数据不受影响）：%s" % exc)
        return data


# --------------------------------------------------------------------------- #
# OPPO 的 MPF 段（CIPA DC-007）
# --------------------------------------------------------------------------- #

MPF_TAG_VERSION = 0xB000
MPF_TAG_NUMBER_OF_IMAGES = 0xB001
MPF_TAG_MP_ENTRY = 0xB002
MP_ENTRY_SIZE = 16
# MPType = 0x030000 表示 Baseline MP Primary Image
MP_TYPE_BASELINE_PRIMARY = 0x030000


def _build_mpf_body(primary_size: int) -> bytes:
    entry = (
        _pack_u32_be(MP_TYPE_BASELINE_PRIMARY)  # 属性：无标志位，类型为基础主图
        + _pack_u32_be(primary_size)  # 主图字节数
        + _pack_u32_be(0)  # 主图偏移：自 MP 字节序字段起算，主图就在文件开头
        + _pack_u16_be(0)
        + _pack_u16_be(0)
    )
    entries = [
        (MPF_TAG_VERSION, 7, 4, b"0100"),
        (MPF_TAG_NUMBER_OF_IMAGES, 4, 1, _pack_u32_le(1)),
        (MPF_TAG_MP_ENTRY, 7, len(entry), b""),  # 值域稍后回填偏移
    ]
    ifd_offset = 8
    header = b"II" + _pack_u16_le(42) + _pack_u32_le(ifd_offset)
    body = bytearray()
    body += _pack_u16_le(len(entries))
    value_offset = ifd_offset + 2 + 12 * len(entries) + 4
    entry_offset = value_offset
    for tag, type_, count, raw in entries:
        body += _pack_u16_le(tag) + _pack_u16_le(type_) + _pack_u32_le(count)
        if tag == MPF_TAG_MP_ENTRY:
            body += _pack_u32_le(entry_offset)
        else:
            body += raw[:4].ljust(4, b"\x00")
    body += _pack_u32_le(0)  # 无下一个 IFD
    while len(body) + len(header) < value_offset:
        body.append(0)
    body += entry
    return header + bytes(body)


def _pack_u16_le(value: int) -> bytes:
    return value.to_bytes(2, "little")


def _pack_u32_le(value: int) -> bytes:
    return value.to_bytes(4, "little")


def _pack_u16_be(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _pack_u32_be(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _insert_mpf(data: bytes, warnings: List[str]) -> bytes:
    """写入 MPF 段：长度固定，主图字节数先占位、拼好后回填。"""
    try:
        placeholder = jpeg.upsert_app_segment(
            data, jpeg.MARKER_APP2, jpeg.MPF_SIGNATURE, jpeg.MPF_SIGNATURE + _build_mpf_body(0)
        )
        primary_size = jpeg.find_eoi(placeholder) + 2
        body = jpeg.MPF_SIGNATURE + _build_mpf_body(primary_size)
        if len(body) != len(jpeg.MPF_SIGNATURE + _build_mpf_body(0)):
            warnings.append("MPF 段长度异常，已跳过")
            return jpeg.upsert_app_segment(
                placeholder, jpeg.MARKER_APP2, jpeg.MPF_SIGNATURE,
                jpeg.MPF_SIGNATURE + _build_mpf_body(0),
            )
        return jpeg.upsert_app_segment(placeholder, jpeg.MARKER_APP2, jpeg.MPF_SIGNATURE, body)
    except jpeg.JpegError as exc:
        warnings.append("MPF 段写入失败，已跳过：%s" % exc)
        return data


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

class Check(NamedTuple):
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True

    def render(self) -> str:
        mark = "✓" if self.ok else ("✗" if self.critical else "!")
        text = self.name
        if self.detail:
            text += "：%s" % self.detail
        return "%s %s" % (mark, text)


def _locate_mp4(trailer: bytes, jpeg_length: int):
    """在追加数据里定位真正的 MP4。

    真机文件（例如 vivo）会在视频之前再放 GainMap、厂商私有数据，所以不能假设
    MP4 就从 EOI 之后开始；这里扫描 ``ftyp`` 候选位置，取第一个能解析出 ``moov`` 的。
    """
    search = 0
    while True:
        index = trailer.find(b"ftyp", search)
        if index < 0:
            return None, None
        candidate = index - 4
        if candidate >= 0:
            try:
                info = mp4.parse(trailer[candidate:])
            except mp4.Mp4Error:
                info = None
            if info is not None and info.has_moov:
                return jpeg_length + candidate, info
        search = index + 1


class Report(NamedTuple):
    checks: List[Check]
    meta: Optional[xmp.MotionPhotoMeta]
    mp4_info: Optional[mp4.Mp4Info]
    jpeg_length: int = 0
    file_length: int = 0
    video_start_backward: Optional[int] = None
    video_start_forward: Optional[int] = None
    ftyp_offset: Optional[int] = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.critical)

    @property
    def failures(self) -> List[Check]:
        return [check for check in self.checks if not check.ok]


def verify_bytes(data: bytes, vendor: Optional[str] = None) -> Report:
    """按读取器的规则校验一个动态照片文件。"""
    checks: List[Check] = []
    meta = None
    mp4_info = None

    try:
        _, eoi = jpeg.parse_structure(data)
    except jpeg.JpegError as exc:
        checks.append(Check("JPEG 结构", False, str(exc)))
        return Report(checks, None, None)

    jpeg_length = eoi + 2
    trailer = data[jpeg_length:]
    checks.append(
        Check("主图后存在追加数据", bool(trailer), "%d 字节" % len(trailer))
    )
    if not trailer:
        return Report(checks, None, None, jpeg_length, len(data))

    packet = _read_xmp_packet(data)
    if packet is None:
        checks.append(Check("APP1 段内存在 XMP", False, "未找到 XMP 段"))
        return Report(checks, None, None, jpeg_length, len(data))

    checks.append(Check("APP1 段内存在 XMP", True, "已在图像数据之前找到"))
    meta = xmp.read_meta(packet)

    if not meta.present:
        checks.append(Check("声明为动态照片", False, "XMP 里没有 MotionPhoto/MicroVideo 标记"))
    else:
        checks.append(Check("声明为动态照片", True, "GCamera:MotionPhoto / MicroVideo"))

    video_item = meta.video_item
    if video_item is None or not video_item.length:
        checks.append(Check("视频项带 Item:Length", False, "缺少视频项或其 Length 为 0"))
        video_start_backward = None
    else:
        checks.append(Check("视频项带 Item:Length", True, "Length=%d" % video_item.length))
        video_start_backward = len(data) - video_item.length

    if video_item is not None:
        semantic = (video_item.semantic or "")
        checks.append(
            Check(
                "视频项 Item:Semantic",
                semantic == xmp.SEMANTIC_MOTION_PHOTO,
                "当前 %r（真机通常为 %s）" % (semantic, xmp.SEMANTIC_MOTION_PHOTO),
                critical=False,
            )
        )

    # 自头累加：主图之后按各项 Length + Padding 推进
    video_start_forward = None
    position = jpeg_length
    for index, item in enumerate(meta.items):
        if index == 0:
            position += item.padding or 0
            continue
        if video_start_forward is None and (item.mime or "").lower() == xmp.VIDEO_MIME:
            video_start_forward = position
        position += (item.length or 0) + (item.padding or 0)
    if meta.items:
        covered = position == len(data)
        checks.append(
            Check(
                "目录项铺满追加数据",
                covered,
                "%d / %d%s"
                % (
                    position,
                    len(data),
                    "" if covered else "（差额为 Directory 未列出的厂商数据）",
                ),
                critical=position > len(data),
            )
        )

    # 实际的 MP4 起点：真机文件可能在其前面放 GainMap / 厂商私有数据，
    # 所以不能假设 MP4 就紧跟在 EOI 之后，这里按结构扫描确认
    ftyp_offset, mp4_info = _locate_mp4(trailer, jpeg_length)
    if ftyp_offset is None:
        checks.append(Check("追加数据里能找到 MP4", False, "未找到有效的 ftyp + moov"))
    else:
        extra = ftyp_offset - jpeg_length
        checks.append(
            Check(
                "追加数据里能找到 MP4",
                True,
                "偏移 %d%s" % (ftyp_offset, "" if extra == 0 else "（视频之前还有 %d 字节）" % extra),
            )
        )

    if video_start_backward is not None:
        checks.append(
            Check(
                "自尾回推 == MP4 起点",
                video_start_backward == ftyp_offset,
                "%s vs %s" % (video_start_backward, ftyp_offset),
            )
        )
    if video_start_forward is not None:
        if ftyp_offset is None:
            checks.append(Check("自头累加 == MP4 起点", False, "未找到 MP4 起点"))
        else:
            agrees = video_start_forward == ftyp_offset
            overrun = video_start_forward > ftyp_offset
            detail = "%d vs %d" % (video_start_forward, ftyp_offset)
            if not agrees and not overrun:
                detail += "（视频前有 %d 字节未列出的厂商数据，真机常见）" % (
                    ftyp_offset - video_start_forward
                )
            checks.append(Check("自头累加 == MP4 起点", agrees, detail, critical=overrun))

    if meta.micro_video_offset is not None:
        declared = video_item.length if video_item else None
        checks.append(
            Check(
                "MicroVideoOffset 与视频长度一致",
                meta.micro_video_offset == declared,
                "MicroVideoOffset=%d，视频项 Length=%s" % (meta.micro_video_offset, declared),
            )
        )
    elif vendor == "xiaomi":
        checks.append(
            Check(
                "小米档位写入 MicroVideoOffset",
                False,
                "缺少旧字段（旧读取器可能不认）",
                critical=False,
            )
        )

    # 封面帧时间戳必须落在视频时长内
    if mp4_info is None:
        checks.append(Check("MP4 结构", False, "未能定位 / 解析出 MP4"))
    elif not mp4_info.has_moov:
        checks.append(Check("MP4 结构", False, "缺少 moov box"))
    else:
        checks.append(
            Check(
                "MP4 结构",
                True,
                "%s / %s / 时长 %s s"
                % (mp4_info.brand, mp4_info.video_codec, mp4_info.duration_seconds),
            )
        )

    timestamp = meta.timestamp_us
    if timestamp is None:
        checks.append(Check("封面帧时间戳", False, "XMP 里没有时间戳", critical=False))
    elif mp4_info is not None and mp4_info.duration_us:
        checks.append(
            Check(
                "封面帧时间戳在时长内",
                0 <= timestamp <= mp4_info.duration_us,
                "%d µs / 时长 %d µs" % (timestamp, mp4_info.duration_us),
            )
        )

    if vendor == "xiaomi":
        checks.append(_check_xiaomi_exif(data))

    return Report(
        checks=checks,
        meta=meta,
        mp4_info=mp4_info,
        jpeg_length=jpeg_length,
        file_length=len(data),
        video_start_backward=video_start_backward,
        video_start_forward=video_start_forward,
        ftyp_offset=ftyp_offset,
    )


def _check_xiaomi_exif(data: bytes) -> Check:
    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    if segment is None:
        return Check("小米 Exif 标记 0x8897", False, "源图没有 Exif 段", critical=False)
    try:
        info = exif_mod.describe(jpeg.segment_body(data, segment)[len(jpeg.EXIF_SIGNATURE):])
    except exif_mod.ExifError as exc:
        return Check("小米 Exif 标记 0x8897", False, str(exc), critical=False)
    entry = info["ifd0"].get(exif_mod.TAG_XIAOMI_MICRO_VIDEO)
    ok = bool(entry) and entry[2] == 1
    return Check(
        "小米 Exif 标记 0x8897",
        ok,
        "值 %r" % (entry[2] if entry else None),
        critical=False,
    )


def verify_file(path: str, vendor: Optional[str] = None) -> Report:
    with open(path, "rb") as handle:
        return verify_bytes(handle.read(), vendor=vendor)


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #

def inspect_bytes(data: bytes, name: str = "") -> str:
    """打印式结构诊断，输出文本（便于贴到 issue 里对照）。"""
    lines: List[str] = []
    lines.append("文件：%s（%d 字节）" % (name or "<内存>", len(data)))

    try:
        segments, eoi = jpeg.parse_structure(data)
    except jpeg.JpegError as exc:
        lines.append("✗ JPEG 结构与解析失败：%s" % exc)
        return "\n".join(lines)

    lines.append("")
    lines.append("JPEG 段：")
    for seg in segments:
        extra = ""
        body = data[seg.body_offset:seg.end]
        if seg.marker == jpeg.MARKER_APP1 and body.startswith(jpeg.EXIF_SIGNATURE):
            extra = "Exif"
        elif seg.marker == jpeg.MARKER_APP1 and body.startswith(jpeg.XMP_SIGNATURE):
            extra = "XMP"
        elif seg.marker == jpeg.MARKER_APP2 and body.startswith(jpeg.MPF_SIGNATURE):
            extra = "MPF"
        lines.append(
            "  %-5s 偏移 %-8d 长度 %-6d %s" % (seg.name, seg.offset, seg.body_length, extra)
        )

    jpeg_length = eoi + 2
    trailer = data[jpeg_length:]
    lines.append("")
    lines.append("EOI 偏移 %d，主图共 %d 字节，追加数据 %d 字节" % (eoi, jpeg_length, len(trailer)))

    packet = _read_xmp_packet(data)
    if packet is None:
        lines.append("")
        lines.append("✗ 没有 APP1 XMP 段（真机读取器多半看不到动态照片标记）")
    else:
        meta = xmp.read_meta(packet)
        lines.append("")
        lines.append("XMP 属性：")
        for key in sorted(meta.properties):
            # Item:* 属于 Directory 各项，下面单独列表，避免与多项混淆
            if key.startswith(("GCamera:", "OpCamera:", "VCamera:", "Container:", "hdrgm:")):
                lines.append("  %s = %s" % (key, meta.properties[key]))
        lines.append("Container:Directory 项：")
        if not meta.items:
            lines.append("  （无）")
        for index, item in enumerate(meta.items):
            lines.append(
                "  [%d] mime=%s semantic=%s length=%s padding=%s"
                % (index, item.mime, item.semantic, item.length, item.padding)
            )

        lines.append("")
        lines.append("定位结果：")
        report = verify_bytes(data)
        lines.append("  自尾回推（文件长 − 视频项 Length）=%s" % report.video_start_backward)
        lines.append("  自头累加（主图 EOI + 各项 Length/Padding）=%s" % report.video_start_forward)
        lines.append("  实际 ftyp 起点=%s" % report.ftyp_offset)

        lines.append("")
        lines.append("检查：")
        for check in report.checks:
            lines.append("  %s" % check.render())

    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    lines.append("")
    if segment is None:
        lines.append("Exif：无 Exif 段")
    else:
        try:
            info = exif_mod.describe(jpeg.segment_body(data, segment)[len(jpeg.EXIF_SIGNATURE):])
        except exif_mod.ExifError as exc:
            lines.append("Exif：解析失败（%s）" % exc)
        else:
            lines.append("Exif（IFD0）：")
            for tag, entry in info["ifd0"].items():
                lines.append("  0x%04X type=%s count=%s value=%r" % (tag, entry[0], entry[1], entry[2]))
            if info["exif_ifd"]:
                lines.append("Exif（ExifIFD）：")
                for tag, entry in info["exif_ifd"].items():
                    lines.append(
                        "  0x%04X type=%s count=%s value=%r" % (tag, entry[0], entry[1], entry[2])
                    )

    if trailer:
        lines.append("")
        try:
            info = mp4.parse(trailer)
        except mp4.Mp4Error as exc:
            lines.append("MP4：解析失败（%s）" % exc)
        else:
            lines.append(
                "MP4：brand=%s 时长=%s s 视频=%s 音频=%s 分片=%s"
                % (
                    info.brand,
                    info.duration_seconds,
                    info.video_codec,
                    info.audio_codec,
                    info.fragmented,
                )
            )
            lines.append("  顶层 box：%s" % ", ".join("%s(%d)" % box for box in info.boxes))
    return "\n".join(lines)


def inspect_file(path: str) -> str:
    with open(path, "rb") as handle:
        return inspect_bytes(handle.read(), os.path.basename(path))


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #

def output_name(base_name: str, style: str = DEFAULT_NAME_STYLE) -> str:
    """按命名风格生成输出文件名。"""
    if style == "keep":
        return base_name + ".jpg"
    if style == "mvimg":
        return "MVIMG_" + base_name + ".jpg"
    return base_name + "_livePhoto.jpg"


def live_photo_names(base_name: str, style: str = DEFAULT_NAME_STYLE) -> Tuple[str, str]:
    """苹果档位的成对文件名。

    两个文件必须同主名（苹果按主名 + 标识符配对），所以只算一次 stem 再套两种扩展名。
    ``mvimg`` 在苹果档位下对应 ``IMG_`` 前缀，和苹果自己拍的实况照片命名一致。
    """
    if style == "keep":
        stem = base_name
    elif style == "mvimg":
        stem = "IMG_" + base_name
    else:
        stem = base_name + "_livePhoto"
    return stem + APPLE_PHOTO_SUFFIX, stem + APPLE_VIDEO_SUFFIX


# --------------------------------------------------------------------------- #
# 苹果 Live Photo（一对同名文件）
# --------------------------------------------------------------------------- #

class LivePhotoResult(NamedTuple):
    photo: bytes
    video: bytes
    identifier: str
    warnings: List[str]


class LivePhotoReport(NamedTuple):
    checks: List[Check]
    photo_identifier: Optional[str] = None
    video_identifier: Optional[str] = None
    mp4_info: Optional[mp4.Mp4Info] = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.critical)

    @property
    def failures(self) -> List[Check]:
        return [check for check in self.checks if not check.ok]

    @property
    def identifier(self) -> Optional[str]:
        return self.photo_identifier or self.video_identifier


def build_live_photo(jpeg_bytes: bytes, mp4_bytes: bytes, options: Options) -> LivePhotoResult:
    """把 JPEG 与视频组装成苹果实况照片的一对文件（静止图 + MOV）。

    与安卓档位不同，这里不往静止图后面追加视频：苹果靠的是两边写同一个 UUID。
    """
    warnings: List[str] = []

    try:
        _, eoi = jpeg.parse_structure(jpeg_bytes)
    except jpeg.JpegError as exc:
        raise ConvertError("源图不是有效的 JPEG：%s" % exc)
    if eoi + 2 < len(jpeg_bytes):
        warnings.append(
            "源图 EOI 之后已有 %d 字节数据（可能已经是动态照片），已剥离"
            % (len(jpeg_bytes) - eoi - 2)
        )
    if not mp4_bytes:
        raise ConvertError("视频文件为空")

    mp4_info, mp4_errors, mp4_warnings = mp4.validate(mp4_bytes, allow_quicktime=True)
    if mp4_errors:
        raise ConvertError("；".join(mp4_errors))
    warnings.extend(mp4_warnings)
    if options.timestamp_us:
        warnings.append("苹果档位不写封面帧时间，--timestamp-us 已忽略")
    if not options.vendor_exif:
        warnings.append("苹果实况照片靠 MakerNote 里的标识符配对，--no-vendor-exif 已忽略")

    identifier = _resolve_identifier(jpeg_bytes, mp4_bytes, warnings)
    photo = _write_apple_still(jpeg_bytes, identifier, warnings)
    try:
        video = mov.set_content_identifier(mp4_bytes, identifier)
    except mov.MovError as exc:
        raise ConvertError("无法写入视频标识符：%s" % exc)

    report = verify_live_photo_pair(photo, video)
    if not report.ok:
        failed = "；".join(check.detail or check.name for check in report.failures)
        raise ConvertError("自检未通过，已放弃写出这对文件：%s" % failed)

    return LivePhotoResult(photo=photo, video=video, identifier=identifier, warnings=warnings)


def _resolve_identifier(jpeg_bytes: bytes, video_bytes: bytes, warnings: List[str]) -> str:
    """决定这对文件用哪个标识符。

    源图或源视频已经带了就沿用（重复转换因此是幂等的），都没有才新生成一个。
    """
    from_still = None
    segment = jpeg.find_app_segment(jpeg_bytes, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    if segment is not None:
        try:
            from_still = exif_mod.read_apple_identifier(
                jpeg.segment_body(jpeg_bytes, segment)[len(jpeg.EXIF_SIGNATURE):]
            )
        except exif_mod.ExifError:
            from_still = None
    try:
        from_video = mov.read_content_identifier(video_bytes)
    except mov.MovError:
        from_video = None

    if from_still and from_video and from_still.upper() != from_video.upper():
        warnings.append(
            "源图与源视频各带一个不同的标识符（%s / %s），已沿用源图的"
            % (from_still, from_video)
        )
    return (from_still or from_video or str(uuid.uuid4())).upper()


def _write_apple_still(jpeg_bytes: bytes, identifier: str, warnings: List[str]) -> bytes:
    """写静止图：剥掉尾部数据、清掉不适用于苹果的动态照片 XMP，再写 Apple MakerNote。"""
    data = jpeg.strip_trailing_data(jpeg_bytes)
    data = _strip_motion_photo_xmp(data, warnings)

    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    tag = exif_mod.apple_identifier_tag(identifier)
    if segment is None:
        warnings.append("源图没有 Exif 段，已新建一个（只含 Live Photo 标识符）")
    try:
        if segment is None:
            tiff = exif_mod.build_tiff(exif_ifd_tags=[tag])
        else:
            tiff = jpeg.segment_body(data, segment)[len(jpeg.EXIF_SIGNATURE):]
            existing = exif_mod.read_makernote(tiff)
            if existing is None:
                tiff = exif_mod.write_tags(tiff, exif_ifd_tags=[tag])
            elif existing.startswith(exif_mod.APPLE_MAKERNOTE_SIGNATURE):
                patched = exif_mod.patch_apple_makernote(existing, identifier)
                if patched == existing:
                    # 标识符已经就是它，Exif 一个字节都不用动
                    return data
                if patched is None:
                    warnings.append(
                        "源图的 Apple MakerNote 里没有可原地改写的标识符，已整条替换"
                        "（原 MakerNote 内容会丢失）"
                    )
                tiff = exif_mod.write_tags(
                    tiff,
                    exif_ifd_tags=[
                        exif_mod.TagSpec(exif_mod.TAG_MAKER_NOTE, exif_mod.TYPE_UNDEFINED, patched)
                    ] if patched is not None else [tag],
                )
            else:
                warnings.append(
                    "源图的 MakerNote 是其它厂商格式（%r），已替换为 Apple MakerNote，"
                    "原 MakerNote 内容会丢失" % existing[:16]
                )
                tiff = exif_mod.write_tags(tiff, exif_ifd_tags=[tag])
    except exif_mod.ExifError as exc:
        # 苹果档位靠标识符配对，写不进去就没有意义，这里不降级
        raise ConvertError("写入 Live Photo 标识符失败：%s" % exc)

    return jpeg.upsert_app_segment(
        data,
        jpeg.MARKER_APP1,
        jpeg.EXIF_SIGNATURE,
        jpeg.EXIF_SIGNATURE + tiff,
        anchor=(jpeg.MARKER_APP0, b"JFIF\x00"),
    )


def _strip_motion_photo_xmp(data: bytes, warnings: List[str]) -> bytes:
    """清掉源图里 Google 那套动态照片 XMP。

    苹果的实况照片不用这些字段，留着 ``GCamera:MotionPhoto="1"`` 会让静止图自称是
    安卓动态照片却又没有视频；其它无关的 XMP 属性原样保留，清空后整段移除。
    """
    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
    if segment is None:
        return data
    packet = jpeg.segment_body(data, segment)[len(jpeg.XMP_SIGNATURE):].decode("utf-8", "replace")
    stripped = xmp.strip_motion_photo(packet)
    if xmp.has_meaningful_properties(stripped):
        return jpeg.upsert_app_segment(
            data,
            jpeg.MARKER_APP1,
            jpeg.XMP_SIGNATURE,
            jpeg.XMP_SIGNATURE + stripped.encode("utf-8"),
        )
    warnings.append("源图的动态照片 XMP 已清理（苹果实况照片不用这套字段），清理后整段移除")
    return jpeg.remove_all_app_segments(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)


def verify_live_photo_pair(photo: bytes, video: bytes) -> LivePhotoReport:
    """校验一对苹果实况照片文件：两侧标识符一致、结构完整、媒体数据可达。"""
    checks: List[Check] = []

    try:
        _, eoi = jpeg.parse_structure(photo)
    except jpeg.JpegError as exc:
        checks.append(Check("静止图是有效的 JPEG", False, str(exc)))
        return LivePhotoReport(checks)
    checks.append(Check("静止图是有效的 JPEG", True, "%d 字节" % len(photo)))
    if eoi + 2 < len(photo):
        checks.append(
            Check(
                "静止图尾部没有多余数据",
                False,
                "EOI 之后还有 %d 字节（苹果的静止图不该有）" % (len(photo) - eoi - 2),
                critical=False,
            )
        )

    photo_identifier = None
    segment = jpeg.find_app_segment(photo, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    if segment is None:
        checks.append(Check("静止图带 Apple MakerNote", False, "没有 Exif 段"))
    else:
        tiff = jpeg.segment_body(photo, segment)[len(jpeg.EXIF_SIGNATURE):]
        blob = None
        try:
            blob = exif_mod.read_makernote(tiff)
        except exif_mod.ExifError as exc:
            checks.append(Check("静止图带 Apple MakerNote", False, str(exc)))
        if blob is None:
            checks.append(Check("静止图带 Apple MakerNote", False, "Exif 里没有 MakerNote（0x927C）"))
        elif not blob.startswith(exif_mod.APPLE_MAKERNOTE_SIGNATURE):
            checks.append(
                Check("静止图带 Apple MakerNote", False, "MakerNote 不是 Apple 格式（%r）" % blob[:12])
            )
        else:
            photo_identifier = exif_mod.read_apple_identifier(tiff)
            checks.append(
                Check(
                    "静止图带 Apple MakerNote",
                    bool(photo_identifier),
                    "标识符 %s" % photo_identifier if photo_identifier else "MakerNote 里没有 tag 0x0011",
                )
            )

    mp4_info = None
    video_identifier = None
    try:
        top = mov.children(video, 0, len(video))
        moov = next((box for box in top if box.type == b"moov"), None)
        if moov is None:
            checks.append(Check("视频是有效的 MOV/MP4", False, "没有 moov"))
        elif any(box.type == b"moof" for box in top):
            checks.append(Check("视频是有效的 MOV/MP4", False, "分片（moof）文件，相册多半不认"))
        else:
            video_identifier = mov.read_content_identifier(video)
            checks.append(
                Check(
                    "视频写入了 content.identifier",
                    bool(video_identifier),
                    "标识符 %s" % video_identifier if video_identifier else "moov/meta 里没有该键",
                )
            )
    except mov.MovError as exc:
        checks.append(Check("视频是有效的 MOV/MP4", False, str(exc)))

    if photo_identifier and video_identifier:
        same = photo_identifier.upper() == video_identifier.upper()
        checks.append(
            Check(
                "两侧标识符一致",
                same,
                "%s vs %s" % (photo_identifier, video_identifier),
            )
        )

    try:
        ranges = mov.mdat_payloads(video)
        offsets = mov.chunk_offsets(video)
        outside = [offset for offset in offsets
                   if not any(start <= offset < end for start, end in ranges)]
        checks.append(
            Check(
                "媒体数据都能取到",
                not outside,
                "%d 个 chunk 偏移全部落在 mdat 内" % len(offsets) if not outside
                else "有 %d 个 chunk 偏移不在 mdat 内（%s）" % (len(outside), outside[:3]),
            )
        )
    except mov.MovError as exc:
        checks.append(Check("媒体数据都能取到", False, str(exc)))

    try:
        mp4_info = mp4.parse(video)
    except mp4.Mp4Error as exc:
        checks.append(Check("视频元数据", False, str(exc)))
    else:
        checks.append(
            Check(
                "视频元数据",
                True,
                "品牌 %s / %s / 时长 %s s"
                % (mp4_info.brand, mp4_info.video_codec, mp4_info.duration_seconds),
            )
        )

    try:
        if not mov.has_still_image_time_track(video):
            checks.append(
                Check(
                    "带 still-image-time 元数据轨道",
                    False,
                    "真机实况照片都有这条轨道（记录静止图在视频时间轴上的位置），"
                    "本工具第一版没写；相册不认时优先补它，见 TECHNICAL.md",
                    critical=False,
                )
            )
    except mov.MovError:
        pass

    return LivePhotoReport(checks, photo_identifier, video_identifier, mp4_info)


def inspect_live_photo(photo: bytes, video: bytes, photo_name: str = "", video_name: str = "") -> str:
    """一对实况照片的结构诊断（贴 issue 用）。"""
    lines = ["静止图：%s（%d 字节）" % (photo_name or "<内存>", len(photo))]
    try:
        segments, eoi = jpeg.parse_structure(photo)
    except jpeg.JpegError as exc:
        lines.append("  ✗ JPEG 解析失败：%s" % exc)
    else:
        for seg in segments:
            body = photo[seg.body_offset:seg.end]
            extra = ""
            if seg.marker == jpeg.MARKER_APP1 and body.startswith(jpeg.EXIF_SIGNATURE):
                extra = "Exif"
            elif seg.marker == jpeg.MARKER_APP1 and body.startswith(jpeg.XMP_SIGNATURE):
                extra = "XMP"
            lines.append("  %-5s 偏移 %-8d 长度 %-6d %s" % (seg.name, seg.offset, seg.body_length, extra))
        lines.append("  EOI 偏移 %d，主图共 %d 字节" % (eoi, eoi + 2))

    segment = jpeg.find_app_segment(photo, jpeg.MARKER_APP1, jpeg.EXIF_SIGNATURE)
    lines.append("")
    if segment is None:
        lines.append("Exif：无")
    else:
        tiff = jpeg.segment_body(photo, segment)[len(jpeg.EXIF_SIGNATURE):]
        lines.append("Apple 标识符：%s" % (exif_mod.read_apple_identifier(tiff) or "（没有）"))
        blob = exif_mod.read_makernote(tiff)
        if blob is None:
            lines.append("MakerNote：无")
        else:
            lines.append("MakerNote：%d 字节，头 %r" % (len(blob), blob[:12]))
            try:
                _, entries = exif_mod.makernote_entries(blob)
            except exif_mod.ExifError as exc:
                lines.append("  条目解析失败：%s" % exc)
            else:
                for entry in entries:
                    lines.append(
                        "  tag=0x%04X type=%s count=%s value=%r"
                        % (entry.tag, entry.type, entry.count, entry.value[:48])
                    )

    lines.append("")
    try:
        lines.extend(mov.describe(video, video_name or "<内存>"))
    except mov.MovError as exc:
        lines.append("MOV：解析失败（%s）" % exc)

    lines.append("")
    lines.append("检查：")
    for check in verify_live_photo_pair(photo, video).checks:
        lines.append("  %s" % check.render())
    return "\n".join(lines)
