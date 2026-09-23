"""MP4 结构预检。

不做完整解复用，只读取拼接前需要知道的几件事：首个 box 是否为 ``ftyp``、
``moov`` 是否存在、时长（用于校验封面帧时间戳）、视频编码、以及是否为分片
（fragmented）文件——这些都会直接影响手机相册能否播放。
"""

import struct
from typing import Iterator, List, NamedTuple, Optional, Tuple


class Mp4Error(ValueError):
    """MP4 结构不合法，无法作为动态照片的视频部分。"""


class Mp4Info(NamedTuple):
    brand: str  # ftyp 主品牌
    compatible_brands: List[str]
    has_moov: bool
    has_mdat: bool
    fragmented: bool
    duration_us: Optional[int]
    video_codec: Optional[str]
    audio_codec: Optional[str]
    boxes: List[Tuple[str, int]]  # 顶层 box 摘要：(类型, 大小)
    trailing_garbage: int  # 顶层 box 之后多余的字节数

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.duration_us is None:
            return None
        return self.duration_us / 1_000_000.0


CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


def _iter_boxes(data: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int, int]]:
    """遍历 box，产出 (类型, 载荷起点, 载荷终点, box 起点)。"""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise Mp4Error("box %r 的 64 位长度字段被截断" % box_type)
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise Mp4Error(
                "偏移 %d 处 box %r 长度 %d 越界" % (pos, box_type, size)
            )
        yield box_type, pos + header, pos + size, pos
        pos += size


def _find_box(data: bytes, start: int, end: int, wanted: bytes):
    for box_type, payload_start, payload_end, box_start in _iter_boxes(data, start, end):
        if box_type == wanted:
            return payload_start, payload_end
    return None


def _parse_mvhd(data: bytes, start: int, end: int) -> Optional[int]:
    """返回时长（微秒）。"""
    if end - start < 20:
        return None
    version = data[start]
    if version == 1 and end - start < 32:
        return None
    if version == 1:
        # version/flags(4) + creation(8) + modification(8) + timescale(4) + duration(8)
        timescale = struct.unpack(">I", data[start + 20:start + 24])[0]
        duration = struct.unpack(">Q", data[start + 24:start + 32])[0]
    else:
        # version/flags(4) + creation(4) + modification(4) + timescale(4) + duration(4)
        timescale = struct.unpack(">I", data[start + 12:start + 16])[0]
        duration = struct.unpack(">I", data[start + 16:start + 20])[0]
    if not timescale or duration in (0, 0xFFFFFFFF):
        return None
    return int(duration * 1_000_000 / timescale)


def _track_codec(data: bytes, trak_start: int, trak_end: int) -> Tuple[Optional[str], Optional[str]]:
    """返回 (handler 类型, stsd 里第一个 entry 的格式码)。"""
    handler = None
    mdia = _find_box(data, trak_start, trak_end, b"mdia")
    if mdia is None:
        return None, None
    hdlr = _find_box(data, mdia[0], mdia[1], b"hdlr")
    if hdlr is not None and hdlr[1] - hdlr[0] >= 12:
        handler = data[hdlr[0] + 8:hdlr[0] + 12].decode("latin-1")

    minf = _find_box(data, mdia[0], mdia[1], b"minf")
    if minf is None:
        return handler, None
    stbl = _find_box(data, minf[0], minf[1], b"stbl")
    if stbl is None:
        return handler, None
    stsd = _find_box(data, stbl[0], stbl[1], b"stsd")
    if stsd is None or stsd[1] - stsd[0] < 16:
        return handler, None
    # stsd 载荷：version/flags(4) + entry_count(4) + 首个条目的 size(4) + format(4)
    entry_size = struct.unpack(">I", data[stsd[0] + 8:stsd[0] + 12])[0]
    if entry_size < 8:
        return handler, None
    fmt = data[stsd[0] + 12:stsd[0] + 16]
    return handler, fmt.decode("latin-1")


def parse(data: bytes) -> Mp4Info:
    """解析 MP4 的顶层结构与关键信息。"""
    if len(data) < 16:
        raise Mp4Error("文件太小，不是有效的 MP4")

    boxes: List[Tuple[str, int]] = []
    brand = ""
    compatible: List[str] = []
    has_moov = has_mdat = fragmented = False
    duration_us = None
    video_codec = audio_codec = None

    last_end = 0
    for box_type, payload_start, payload_end, box_start in _iter_boxes(data, 0, len(data)):
        boxes.append((box_type.decode("latin-1"), payload_end - box_start))
        last_end = payload_end
        if box_type == b"ftyp":
            if payload_end - payload_start >= 8:
                brand = data[payload_start:payload_start + 4].decode("latin-1").strip()
                rest = data[payload_start + 8:payload_end]
                compatible = [
                    rest[i:i + 4].decode("latin-1").strip()
                    for i in range(0, len(rest) - 3, 4)
                ]
        elif box_type == b"moov":
            has_moov = True
            mvhd = _find_box(data, payload_start, payload_end, b"mvhd")
            if mvhd is not None:
                duration_us = _parse_mvhd(data, mvhd[0], mvhd[1])
            for child_type, child_start, child_end, _ in _iter_boxes(data, payload_start, payload_end):
                if child_type != b"trak":
                    continue
                handler, codec = _track_codec(data, child_start, child_end)
                if handler == "vide" and video_codec is None:
                    video_codec = codec
                elif handler == "soun" and audio_codec is None:
                    audio_codec = codec
        elif box_type == b"mdat":
            has_mdat = True
        elif box_type == b"moof":
            fragmented = True

    return Mp4Info(
        brand=brand,
        compatible_brands=compatible,
        has_moov=has_moov,
        has_mdat=has_mdat,
        fragmented=fragmented,
        duration_us=duration_us,
        video_codec=video_codec,
        audio_codec=audio_codec,
        boxes=boxes,
        trailing_garbage=len(data) - last_end,
    )


# 手机相册一般能直接播放的编码；其余给出提示但不阻止
COMMON_CODECS = {"avc1", "avc3", "hvc1", "hev1", "mp4v", "avc2"}


def validate(data: bytes, allow_quicktime: bool = False) -> Tuple[Mp4Info, List[str], List[str]]:
    """返回 (信息, 错误列表, 警告列表)。错误非空表示不该继续拼接。

    allow_quicktime 用于苹果档位：产物本来就是 QuickTime(.mov)，不该再提示品牌问题。
    """
    errors: List[str] = []
    warnings: List[str] = []

    try:
        info = parse(data)
    except Mp4Error as exc:
        return (
            Mp4Info("", [], False, False, False, None, None, None, [], 0),
            ["MP4 结构异常：%s" % exc],
            [],
        )

    first_type = info.boxes[0][0] if info.boxes else ""
    if first_type != "ftyp":
        errors.append("MP4 首个 box 是 %r 而不是 ftyp，不是标准 MP4" % (first_type or "空"))
    if not info.has_moov:
        errors.append("MP4 缺少 moov box，无法播放")
    if not info.has_mdat:
        warnings.append("MP4 没有 mdat box（可能只有元数据）")
    if info.fragmented:
        warnings.append("MP4 是分片（moof）文件，部分相册无法播放，建议先转成普通 MP4")
    if info.trailing_garbage:
        warnings.append("MP4 末尾有 %d 字节多余数据" % info.trailing_garbage)
    if info.brand in ("qt", "qt  ") and not allow_quicktime:
        warnings.append("这是 QuickTime(.mov) 而非 MP4（ftyp 品牌为 qt），部分相册不识别")
    if info.video_codec and info.video_codec not in COMMON_CODECS:
        warnings.append(
            "视频编码 %s 不是常见编码（%s），部分手机相册可能无法播放"
            % (info.video_codec, "/".join(sorted(COMMON_CODECS)))
        )
    if not info.video_codec:
        warnings.append("未能在 moov 中找到视频轨道")
    if info.duration_us is None:
        warnings.append("未能从 mvhd 读出时长（封面帧时间戳将按 0 处理）")
    return info, errors, warnings
