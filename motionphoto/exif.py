"""Exif 私有标记写入。

小米相册会读私有 tag ``0x8897``（十进制 34967）来判断「这是动态照片」，
OPPO 还会读 ``UserComment``（``Oplus_…``）。这些字段都在 Exif 的 IFD 结构里，
直接改 IFD 会牵动整条偏移链（值区、ExifIFD、MakerNote、IFD1 缩略图），风险很高。

这里的做法是把改动压在最小范围：

1. 解析 TIFF 头拿到原 IFD0 与 ExifIFD 的条目；
2. 在 TIFF 数据区**末尾**追加一份新的 ExifIFD / IFD0（原条目原样复制，
   值指针不动，所以 MakerNote 之类仍指向原位置）；
3. 只把 TIFF 头里的「第一个 IFD 偏移」改成指向新 IFD0。

原有字节一个都不改、一个都不移，任何结构异常直接抛错让调用方跳过。
"""

import struct
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

TYPE_BYTE = 1
TYPE_ASCII = 2
TYPE_SHORT = 3
TYPE_LONG = 4
TYPE_RATIONAL = 5
TYPE_UNDEFINED = 7

TAG_EXIF_IFD = 0x8769
TAG_XIAOMI_MICRO_VIDEO = 0x8897  # 小米相册读取的私有标记，值 1
TAG_XIAOMI_MICRO_VIDEO_EXTRA = 0x889F  # 单一来源、含义不明，默认不写
TAG_EMBEDDED_VIDEO = 0x9A01  # 同上
TAG_USER_COMMENT = 0x9286
TAG_MAKER_NOTE = 0x927C

# Apple MakerNote：Live Photo 标识符写在里面的 tag 0x0011（真机 HEIC/JPG 都是如此）。
# 结构是 12 字节签名（"Apple iOS\0" + 版本）+ 14 字节内部 TIFF 头 + IFD，
# 值区偏移相对这份 MakerNote 的起点，而不是相对外层 Exif 的 TIFF。
APPLE_MAKERNOTE_SIGNATURE = b"Apple iOS\x00\x00\x01"
APPLE_MAKERNOTE_TIFF_HEAD = b"MM\x00\x2a\x00\x01\x00\x09\x00\x00\x00\x01\x00\x00"
APPLE_MAKERNOTE_IFD_OFFSET = len(APPLE_MAKERNOTE_SIGNATURE) + len(APPLE_MAKERNOTE_TIFF_HEAD)
APPLE_TAG_CONTENT_IDENTIFIER = 0x0011  # ASCII，UUID 加结尾 NUL 共 37 字节
# 真机 MakerNote 是 1568 字节。实测 macOS 的 ImageIO（与 iOS 同一套 Exif 解析）
# 对长度不足 1KB 的 MakerNote 干脆不解析（960 字节读不出、968 字节可以），
# 所以生成的 MakerNote 按真机尺寸补齐到 1568 字节。
APPLE_MAKERNOTE_MIN_SIZE = 1568

# APP1 段体上限 65533 字节，减去 b"Exif\0\0" 的 6 字节
MAX_TIFF_BODY = 65527

_TYPE_SIZES = {
    TYPE_BYTE: 1,
    TYPE_ASCII: 1,
    TYPE_SHORT: 2,
    TYPE_LONG: 4,
    TYPE_RATIONAL: 8,
    TYPE_UNDEFINED: 1,
}


class ExifError(ValueError):
    """Exif/TIFF 结构不可用，调用方应跳过写入而不是冒险修改。"""


class TagSpec(NamedTuple):
    """要写入的一个 tag。value 超过 4 字节时会被放进数据区。"""

    tag: int
    type: int
    value: bytes

    @property
    def count(self) -> int:
        return len(self.value) // _TYPE_SIZES.get(self.type, 1)


def micro_video_tag() -> TagSpec:
    """小米私有标记 0x8897 = 1（BYTE）。"""
    return TagSpec(TAG_XIAOMI_MICRO_VIDEO, TYPE_BYTE, b"\x01")


def extra_vendor_tags() -> List[TagSpec]:
    """另外两个单一来源的小米相关 tag，需显式开启才写。"""
    return [
        TagSpec(TAG_XIAOMI_MICRO_VIDEO_EXTRA, TYPE_BYTE, b"\x01"),
        TagSpec(TAG_EMBEDDED_VIDEO, TYPE_BYTE, b"\x01"),
    ]


def user_comment_tag(text: str) -> TagSpec:
    """UserComment（0x9286），按 Exif 规范带 8 字节字符集前缀。"""
    return TagSpec(TAG_USER_COMMENT, TYPE_UNDEFINED, b"ASCII\x00\x00\x00" + text.encode("ascii"))


def _identifier_bytes(identifier: str) -> bytes:
    """标识符按 ASCII 编码并补结尾 NUL（真机是 37 字节）。"""
    try:
        payload = identifier.encode("ascii")
    except UnicodeEncodeError:
        raise ExifError("Live Photo 标识符必须是 ASCII：%r" % identifier)
    if not payload:
        raise ExifError("Live Photo 标识符不能为空")
    return payload + b"\x00"


class MakerNoteEntry(NamedTuple):
    """Apple MakerNote 里的一条记录。offset 是值在 MakerNote 内的偏移（内联值为 None）。"""

    tag: int
    type: int
    count: int
    value: bytes
    offset: Optional[int]


def apple_makernote(identifier: str) -> bytes:
    """构造一份最小 Apple MakerNote，内部只有一个 Live Photo 标识符 tag 0x0011。

    末尾补齐到真机尺寸：见 :data:`APPLE_MAKERNOTE_MIN_SIZE` 的说明。
    """
    value = _identifier_bytes(identifier)
    value_offset = APPLE_MAKERNOTE_IFD_OFFSET + 2 + 12 + 4
    ifd = struct.pack(">H", 1)
    ifd += struct.pack(">HHI", APPLE_TAG_CONTENT_IDENTIFIER, TYPE_ASCII, len(value))
    ifd += struct.pack(">I", value_offset)
    ifd += struct.pack(">I", 0)  # 没有下一个 IFD
    blob = APPLE_MAKERNOTE_SIGNATURE + APPLE_MAKERNOTE_TIFF_HEAD + ifd + value
    return blob.ljust(APPLE_MAKERNOTE_MIN_SIZE, b"\x00")


def apple_identifier_tag(identifier: str) -> TagSpec:
    """写进 ExifIFD 的 MakerNote（0x927C），Live Photo 的标识符就藏在它里面。"""
    return TagSpec(TAG_MAKER_NOTE, TYPE_UNDEFINED, apple_makernote(identifier))


def makernote_entries(blob: bytes):
    """解析 Apple MakerNote 的 IFD，返回 ``(endian, 条目列表)``。

    值按真机约定解析：偏移相对这份 MakerNote 的起点。结构不认识就抛 :class:`ExifError`。
    """
    if not blob.startswith(b"Apple iOS"):
        raise ExifError("不是 Apple MakerNote（缺少 Apple iOS 签名）")
    if len(blob) < APPLE_MAKERNOTE_IFD_OFFSET + 2:
        raise ExifError("Apple MakerNote 过短")
    head = blob[12:APPLE_MAKERNOTE_IFD_OFFSET]
    if head[:2] == b"MM":
        endian = ">"
    elif head[:2] == b"II":
        endian = "<"
    else:
        raise ExifError("Apple MakerNote 内部字节序不是 II/MM")
    ifd = APPLE_MAKERNOTE_IFD_OFFSET
    count = struct.unpack(endian + "H", blob[ifd:ifd + 2])[0]
    if count > 512 or ifd + 2 + 12 * count + 4 > len(blob):
        raise ExifError("Apple MakerNote 的 IFD 条目数 %d 不合理" % count)
    entries: List[MakerNoteEntry] = []
    for index in range(count):
        base = ifd + 2 + 12 * index
        tag, type_, item_count = struct.unpack(endian + "HHI", blob[base:base + 8])
        raw = blob[base + 8:base + 12]
        size = _TYPE_SIZES.get(type_, 1) * item_count
        if size <= 4:
            entries.append(MakerNoteEntry(tag, type_, item_count, raw[:size], None))
            continue
        value_offset = struct.unpack(endian + "I", raw)[0]
        if value_offset + size > len(blob):
            raise ExifError("Apple MakerNote 的 tag 0x%04X 值（偏移 %d）越界" % (tag, value_offset))
        entries.append(MakerNoteEntry(tag, type_, item_count, blob[value_offset:value_offset + size], value_offset))
    return endian, entries


def read_makernote(tiff: bytes) -> Optional[bytes]:
    """取出 ExifIFD 里的 MakerNote（0x927C）原始字节，没有则返回 None。"""
    try:
        endian, ifd0_offset = _parse_header(tiff)
        ifd0, _ = _read_ifd(tiff, ifd0_offset, endian)
        entry = ifd0.get(TAG_EXIF_IFD)
        if entry is None:
            return None
        pointer = _decode_value(tiff, endian, entry[0], entry[1], entry[2])
        if not isinstance(pointer, int) or not pointer:
            return None
        exif_entries, _ = _read_ifd(tiff, pointer, endian)
    except (ExifError, struct.error):
        return None
    maker = exif_entries.get(TAG_MAKER_NOTE)
    if maker is None:
        return None
    blob = _decode_value(tiff, endian, maker[0], maker[1], maker[2])
    if not isinstance(blob, (bytes, bytearray)):
        return None
    return bytes(blob)


def read_apple_identifier(tiff: bytes) -> Optional[str]:
    """从 TIFF 里读出 Apple MakerNote 的 Live Photo 标识符，没有则返回 None。"""
    blob = read_makernote(tiff)
    if blob is None:
        return None
    try:
        _, entries = makernote_entries(blob)
    except (ExifError, struct.error):
        return None
    for entry in entries:
        if entry.tag == APPLE_TAG_CONTENT_IDENTIFIER and entry.type == TYPE_ASCII and entry.value:
            return entry.value.split(b"\x00")[0].decode("ascii", "replace")
    return None


def patch_apple_makernote(existing: bytes, identifier: str) -> Optional[bytes]:
    """在已有 Apple MakerNote 里原地改写标识符；做不到（没有该 tag 或长度不同）返回 None。

    真机 MakerNote 本来就带 tag 0x0011（ASCII、37 字节，值在 tag 里记着偏移），
    原地覆盖那 37 字节既不用重排偏移，也不会碰到苹果另写的嵌套结构，是最安全的改法。
    """
    try:
        _, entries = makernote_entries(existing)
    except (ExifError, struct.error):
        return None
    value = _identifier_bytes(identifier)
    for entry in entries:
        if entry.tag != APPLE_TAG_CONTENT_IDENTIFIER:
            continue
        if entry.type != TYPE_ASCII or entry.offset is None or entry.count != len(value):
            return None
        return existing[:entry.offset] + value + existing[entry.offset + len(value):]
    return None


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def _parse_header(tiff: bytes):
    if len(tiff) < 8:
        raise ExifError("TIFF 数据过短")
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        raise ExifError("TIFF 字节序标记不是 II/MM")
    magic = struct.unpack(endian + "H", tiff[2:4])[0]
    if magic != 42:
        raise ExifError("TIFF 魔数不是 42（实际 %d）" % magic)
    ifd0_offset = struct.unpack(endian + "I", tiff[4:8])[0]
    return endian, ifd0_offset


def _read_ifd(tiff: bytes, offset: int, endian: str):
    """返回 ({tag: (type, count, 4 字节值域)}, 下一个 IFD 偏移)。"""
    if offset + 2 > len(tiff):
        raise ExifError("IFD 偏移 %d 越界" % offset)
    count = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
    if count > 4096:
        raise ExifError("IFD 条目数 %d 不合理" % count)
    end = offset + 2 + 12 * count + 4
    if end > len(tiff):
        raise ExifError("IFD 条目区越界")
    entries: Dict[int, Tuple[int, int, bytes]] = {}
    for index in range(count):
        base = offset + 2 + 12 * index
        tag, type_, item_count = struct.unpack(endian + "HHI", tiff[base:base + 8])
        entries[tag] = (type_, item_count, tiff[base + 8:base + 12])
    next_offset = struct.unpack(endian + "I", tiff[end - 4:end])[0]
    return entries, next_offset


def _decode_value(tiff: bytes, endian: str, type_: int, count: int, raw: bytes):
    """把条目值域解析成可读形式（够用即可，只覆盖常见类型）。"""
    size = _TYPE_SIZES.get(type_, 1) * count
    if size <= 4:
        data = raw[:size]
    else:
        offset = struct.unpack(endian + "I", raw)[0]
        if offset + size > len(tiff):
            return None
        data = tiff[offset:offset + size]
    if type_ in (TYPE_BYTE, TYPE_UNDEFINED):
        if type_ == TYPE_BYTE and count == 1:
            return data[0]
        return data
    if type_ == TYPE_ASCII:
        return data.split(b"\x00")[0].decode("ascii", "replace")
    if type_ == TYPE_SHORT:
        values = struct.unpack(endian + "%dH" % count, data)
        return values[0] if count == 1 else values
    if type_ == TYPE_LONG:
        values = struct.unpack(endian + "%dI" % count, data)
        return values[0] if count == 1 else values
    return data


def describe(tiff: bytes) -> Dict[str, object]:
    """给 --inspect 用的概览。"""
    endian, ifd0_offset = _parse_header(tiff)
    ifd0, _ = _read_ifd(tiff, ifd0_offset, endian)
    result: Dict[str, object] = {
        "byte_order": "little" if endian == "<" else "big",
        "ifd0_offset": ifd0_offset,
        "ifd0": {
            tag: (type_, count, _decode_value(tiff, endian, type_, count, raw))
            for tag, (type_, count, raw) in sorted(ifd0.items())
        },
        "exif_ifd": {},
        "ifd1": {},
    }
    if TAG_EXIF_IFD in ifd0:
        type_, count, raw = ifd0[TAG_EXIF_IFD]
        value = _decode_value(tiff, endian, type_, count, raw)
        if isinstance(value, int):
            try:
                exif_entries, _ = _read_ifd(tiff, value, endian)
            except ExifError:
                exif_entries = {}
            result["exif_ifd"] = {
                tag: (type_, count, _decode_value(tiff, endian, type_, count, raw_value))
                for tag, (type_, count, raw_value) in sorted(exif_entries.items())
            }
    next_ifd = _read_ifd(tiff, ifd0_offset, endian)[1]
    if next_ifd:
        try:
            ifd1_entries, _ = _read_ifd(tiff, next_ifd, endian)
        except ExifError:
            ifd1_entries = {}
        result["ifd1"] = {
            tag: (type_, count, _decode_value(tiff, endian, type_, count, raw_value))
            for tag, (type_, count, raw_value) in sorted(ifd1_entries.items())
        }
    return result


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #

def _inline_or_offset(spec: TagSpec, endian: str) -> Tuple[bytes, Optional[bytes]]:
    """返回 (4 字节值域, 需要放进数据区的载荷)。"""
    if len(spec.value) <= 4:
        return spec.value.ljust(4, b"\x00"), None
    return None, spec.value


def _serialise_ifd(entries: Dict[int, Tuple[int, int, bytes]], next_offset: int, endian: str) -> bytes:
    out = bytearray()
    out += struct.pack(endian + "H", len(entries))
    for tag in sorted(entries):
        type_, count, raw = entries[tag]
        out += struct.pack(endian + "HHI", tag, type_, count)
        out += raw[:4].ljust(4, b"\x00")
    out += struct.pack(endian + "I", next_offset)
    return bytes(out)


def write_tags(tiff: bytes, ifd0_tags: Sequence[TagSpec] = (), exif_ifd_tags: Sequence[TagSpec] = ()) -> bytes:
    """写入 tag 并返回新的 TIFF 数据（原字节不动，只做追加与回指）。"""
    if not ifd0_tags and not exif_ifd_tags:
        return tiff

    endian, ifd0_offset = _parse_header(tiff)
    ifd0, next_ifd0 = _read_ifd(tiff, ifd0_offset, endian)
    ifd0 = dict(ifd0)

    exif_entries: Dict[int, Tuple[int, int, bytes]] = {}
    exif_next = 0
    if exif_ifd_tags and TAG_EXIF_IFD in ifd0:
        type_, count, raw = ifd0[TAG_EXIF_IFD]
        pointer = _decode_value(tiff, endian, type_, count, raw)
        if isinstance(pointer, int) and pointer:
            exif_entries, exif_next = _read_ifd(tiff, pointer, endian)
            exif_entries = dict(exif_entries)

    base = len(tiff) + (len(tiff) % 2)
    region = bytearray()
    while len(tiff) + len(region) < base:
        region.append(0)

    def align() -> None:
        if (base + len(region)) % 2:
            region.append(0)

    def place(payload: bytes) -> int:
        align()
        offset = base + len(region)
        region.extend(payload)
        return offset

    # 1) ExifIFD 里超过 4 字节的值（如 UserComment）
    exif_value_offsets: Dict[int, int] = {}
    for spec in exif_ifd_tags:
        if len(spec.value) > 4:
            exif_value_offsets[spec.tag] = place(spec.value)

    # 2) 新的 ExifIFD
    new_exif_offset = 0
    if exif_ifd_tags:
        align()
        new_exif_offset = base + len(region)
        merged = dict(exif_entries)
        for spec in exif_ifd_tags:
            raw, large = _inline_or_offset(spec, endian)
            if large is not None:
                raw = struct.pack(endian + "I", exif_value_offsets[spec.tag])
            merged[spec.tag] = (spec.type, spec.count, raw)
        region.extend(_serialise_ifd(merged, exif_next, endian))

    # 3) 新的 IFD0
    merged_ifd0: Dict[int, Tuple[int, int, bytes]] = dict(ifd0)
    for spec in ifd0_tags:
        merged_ifd0[spec.tag] = (spec.type, spec.count, b"")
    if exif_ifd_tags:
        merged_ifd0[TAG_EXIF_IFD] = (TYPE_LONG, 1, b"")

    align()
    new_ifd0_offset = base + len(region)
    values_start = new_ifd0_offset + 2 + 12 * len(merged_ifd0) + 4
    cursor = values_start
    pending: List[Tuple[int, bytes]] = []
    final_ifd0: Dict[int, Tuple[int, int, bytes]] = {}
    specs_by_tag = {spec.tag: spec for spec in ifd0_tags}

    for tag in sorted(merged_ifd0):
        type_, count, raw = merged_ifd0[tag]
        if tag == TAG_EXIF_IFD and exif_ifd_tags:
            final_ifd0[tag] = (TYPE_LONG, 1, struct.pack(endian + "I", new_exif_offset))
            continue
        spec = specs_by_tag.get(tag)
        if spec is None:
            final_ifd0[tag] = (type_, count, raw)
            continue
        if len(spec.value) <= 4:
            final_ifd0[tag] = (spec.type, spec.count, spec.value.ljust(4, b"\x00"))
        else:
            if cursor % 2:
                cursor += 1
            final_ifd0[tag] = (spec.type, spec.count, struct.pack(endian + "I", cursor))
            pending.append((cursor, spec.value))
            cursor += len(spec.value)

    region.extend(_serialise_ifd(final_ifd0, next_ifd0, endian))
    for offset, payload in pending:
        while base + len(region) < offset:
            region.append(0)
        region.extend(payload)

    result = bytearray(tiff)
    if len(tiff) % 2:
        result.append(0)
    result.extend(region)
    # 只改 TIFF 头里的「第一个 IFD 偏移」，其余原样
    result[4:8] = struct.pack(endian + "I", new_ifd0_offset)

    if len(result) > MAX_TIFF_BODY:
        raise ExifError(
            "写入后 Exif 段 %d 字节，超过 APP1 段上限 %d" % (len(result), MAX_TIFF_BODY)
        )
    return bytes(result)


def build_tiff(ifd0_tags: Sequence[TagSpec] = (), exif_ifd_tags: Sequence[TagSpec] = ()) -> bytes:
    """源图没有 Exif 时，构造一份最小 TIFF（小端，文件头 8 字节）。"""
    endian = "<"
    base = 8
    region = bytearray()

    exif_value_offsets: Dict[int, int] = {}
    for spec in exif_ifd_tags:
        if len(spec.value) > 4:
            if (base + len(region)) % 2:
                region.append(0)
            exif_value_offsets[spec.tag] = base + len(region)
            region.extend(spec.value)

    new_exif_offset = 0
    if exif_ifd_tags:
        if (base + len(region)) % 2:
            region.append(0)
        new_exif_offset = base + len(region)
        merged: Dict[int, Tuple[int, int, bytes]] = {}
        for spec in exif_ifd_tags:
            raw, large = _inline_or_offset(spec, endian)
            if large is not None:
                raw = struct.pack(endian + "I", exif_value_offsets[spec.tag])
            merged[spec.tag] = (spec.type, spec.count, raw)
        region.extend(_serialise_ifd(merged, 0, endian))

    if (base + len(region)) % 2:
        region.append(0)
    new_ifd0_offset = base + len(region)
    entries: Dict[int, Tuple[int, int, bytes]] = {}
    cursor = new_ifd0_offset + 2 + 12 * (len(ifd0_tags) + (1 if exif_ifd_tags else 0)) + 4
    pending: List[Tuple[int, bytes]] = []
    for spec in ifd0_tags:
        if len(spec.value) <= 4:
            entries[spec.tag] = (spec.type, spec.count, spec.value.ljust(4, b"\x00"))
        else:
            if cursor % 2:
                cursor += 1
            entries[spec.tag] = (spec.type, spec.count, struct.pack(endian + "I", cursor))
            pending.append((cursor, spec.value))
            cursor += len(spec.value)
    if exif_ifd_tags:
        entries[TAG_EXIF_IFD] = (TYPE_LONG, 1, struct.pack(endian + "I", new_exif_offset))
    region.extend(_serialise_ifd(entries, 0, endian))
    for offset, payload in pending:
        while base + len(region) < offset:
            region.append(0)
        region.extend(payload)

    tiff = b"II" + struct.pack(endian + "H", 42) + struct.pack(endian + "I", new_ifd0_offset) \
        + bytes(region)
    if len(tiff) > MAX_TIFF_BODY:
        raise ExifError("生成的 Exif 段过大（%d 字节）" % len(tiff))
    return tiff
