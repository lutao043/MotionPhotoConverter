"""JPEG 段级解析与读写。

这里只做结构处理，不解码图像数据，因此合成后的主图字节可以保持原样——这是
Google/小米等厂商实现共同采用的做法：XMP 写在 APP1 段内（图像数据之前），
MP4 紧跟 EOI 之后原样追加。
"""

import struct
from typing import List, NamedTuple, Optional

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# APP1 段内的标识字符串，用于区分同名的 XMP / Exif 段
XMP_SIGNATURE = b"http://ns.adobe.com/xap/1.0/\x00"
EXIF_SIGNATURE = b"Exif\x00\x00"
# APP2 段内的 MPF（CIPA DC-007 Multi-Picture Format）标识
MPF_SIGNATURE = b"MPF\x00"

MARKER_APP0 = 0xFFE0
MARKER_APP1 = 0xFFE1
MARKER_APP2 = 0xFFE2
MARKER_SOS = 0xFFDA
MARKER_EOI = 0xFFD9

# 段长字段只有 2 字节且包含它自己，所以单个段的载荷上限是 65535 - 2
MAX_SEGMENT_BODY = 65533


class JpegError(ValueError):
    """JPEG 结构不合法。"""


class Segment(NamedTuple):
    marker: int  # 完整标记值，如 0xFFE1
    offset: int  # 标记中第一个 0xFF 的位置
    body_offset: int  # 载荷（段长字段之后）的起始位置
    body_length: int  # 载荷长度

    @property
    def end(self) -> int:
        """该段结束（下一个段开始）的位置。"""
        return self.body_offset + self.body_length

    @property
    def name(self) -> str:
        if MARKER_APP0 <= self.marker <= 0xFFEF:
            return "APP%d" % (self.marker - 0xFFE0)
        return "0xFF%02X" % (self.marker & 0xFF)


def _skip_entropy_data(data: bytes, pos: int) -> int:
    """跳过 SOS 之后的熵编码数据，返回下一个真实标记的起始位置。

    需要正确处理 0xFF00 字节填充和 0xFFD0-0xFFD7 重启标记。
    """
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        start = pos
        j = pos + 1
        while j < n and data[j] == 0xFF:
            j += 1
        if j >= n:
            break
        value = data[j]
        if value == 0x00 or 0xD0 <= value <= 0xD7:
            pos = j + 1
            continue
        return start
    raise JpegError("JPEG 损坏：熵编码数据之后未找到标记（缺少 EOI？）")


def parse_structure(data: bytes):
    """解析 JPEG 结构。

    返回 ``(segments, eoi_offset)``：segments 是所有带长度字段的段（按出现顺序，
    含 SOS 之后多扫描线的渐进式 JPEG 段），eoi_offset 是 EOI 标记的位置。
    """
    if not data.startswith(SOI):
        raise JpegError("不是 JPEG 文件：缺少 SOI 标记")
    segments: List[Segment] = []
    pos = 2
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            raise JpegError("JPEG 结构异常：偏移 %d 处缺少标记前缀 0xFF" % pos)
        marker_start = pos
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            return segments, marker_start
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # 独立标记，无长度字段
            continue
        if pos + 2 > n:
            raise JpegError("JPEG 结构异常：段长字段被截断")
        seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
        if seg_len < 2 or pos + seg_len > n:
            raise JpegError(
                "JPEG 结构异常：偏移 %d 处的段长度 %d 越界" % (marker_start, seg_len)
            )
        segments.append(Segment(0xFF00 | marker, marker_start, pos + 2, seg_len - 2))
        pos += seg_len
        if marker == 0xDA:  # SOS：其后是熵编码数据
            pos = _skip_entropy_data(data, pos)
    raise JpegError("JPEG 损坏：未找到 EOI 标记")


def find_eoi(data: bytes) -> int:
    """返回 EOI 标记位置。"""
    return parse_structure(data)[1]


def strip_trailing_data(data: bytes) -> bytes:
    """去掉 EOI 之后的所有字节。

    源图如果已经是动态照片、或被别的工具追加过数据，直接拼接会得到嵌套的坏文件，
    所以先在 EOI 处截断。
    """
    _, eoi = parse_structure(data)
    return data[:eoi + 2]


def has_trailing_data(data: bytes) -> bool:
    """EOI 之后是否还有字节。"""
    _, eoi = parse_structure(data)
    return eoi + 2 < len(data)


def trailing_data(data: bytes) -> bytes:
    """返回 EOI 之后的字节。"""
    _, eoi = parse_structure(data)
    return data[eoi + 2:]


def find_app_segment(data: bytes, marker: int, signature: bytes) -> Optional[Segment]:
    """按标记和载荷标识串查找段（如 APP1 + XMP 签名）。"""
    segments, _ = parse_structure(data)
    for seg in segments:
        if seg.marker != marker:
            continue
        if data[seg.body_offset:seg.body_offset + len(signature)] == signature:
            return seg
    return None


def segment_body(data: bytes, seg: Segment) -> bytes:
    return data[seg.body_offset:seg.end]


def _insertion_point(data: bytes, segments: List[Segment]) -> int:
    """返回插入新 APPn 段的位置：开头连续 APPn 段之后。

    这样既不会插到图像数据之后，也能让 Exif 排在 XMP 之前（先写 Exif 再写 XMP）。
    """
    point = 2
    for seg in segments:
        if MARKER_APP0 <= seg.marker <= 0xFFEF:
            point = seg.end
        else:
            break
    return point


def build_app_segment(marker: int, body: bytes) -> bytes:
    """构造一个带长度字段的 JPEG 段（含 0xFF 前缀）。"""
    if len(body) > MAX_SEGMENT_BODY:
        raise JpegError("段载荷过大：%d 字节（上限 %d）" % (len(body), MAX_SEGMENT_BODY))
    return struct.pack(">BBH", 0xFF, marker & 0xFF, len(body) + 2) + body


def upsert_app_segment(data: bytes, marker: int, signature: bytes, body: bytes,
                       anchor=None) -> bytes:
    """写入一个 APPn 段：同名段存在则原位置替换，否则插入。

    body 需自带 signature（例如 XMP 段体以 ``b"http://ns.adobe.com/xap/1.0/\\0"`` 开头）。
    anchor 给出 ``(标记, 标识串)`` 时优先紧跟该段之后插入——XMP 因此能紧挨 Exif，
    而不是被厂商那十几个 64KB 的大段挤到几百 KB 之后。返回新的 JPEG 字节串。
    """
    blob = build_app_segment(marker, body)
    segments, _ = parse_structure(data)
    for seg in segments:
        if seg.marker != marker:
            continue
        if data[seg.body_offset:seg.body_offset + len(signature)] == signature:
            return data[:seg.offset] + blob + data[seg.end:]
    if anchor is not None:
        target = find_app_segment(data, anchor[0], anchor[1])
        if target is not None:
            return data[:target.end] + blob + data[target.end:]
    point = _insertion_point(data, segments)
    return data[:point] + blob + data[point:]


def remove_app_segment(data: bytes, marker: int, signature: bytes) -> bytes:
    """删除匹配的 APPn 段（不存在时原样返回）。"""
    seg = find_app_segment(data, marker, signature)
    if seg is None:
        return data
    return data[:seg.offset] + data[seg.end:]


def remove_all_app_segments(data: bytes, marker: int, signature: bytes) -> bytes:
    """删除所有匹配的 APPn 段。

    真机文件里出现过重复的 XMP 段，只替换第一段会留下过期偏移，所以整段清掉。
    """
    while True:
        seg = find_app_segment(data, marker, signature)
        if seg is None:
            return data
        data = data[:seg.offset] + data[seg.end:]


def inspect_segments(data: bytes) -> List[Segment]:
    """返回全部段，供 --inspect 打印。"""
    return parse_structure(data)[0]
