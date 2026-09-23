"""QuickTime/MOV 盒子读写，用于写苹果 Live Photo 的元数据。

与 :mod:`motionphoto.mp4` 的分工：``mp4.py`` 只读，用于拼接前的预检；
这里负责写——在 ``moov`` 里维护 mdta 键值元数据（苹果的配对标识符
``com.apple.quicktime.content.identifier`` 就写在 ``moov/meta`` 的
``keys``/``ilst`` 里，真机文件结构见 TECHNICAL.md）。

三条硬约束：

1. **只动元数据**：``mdat`` 载荷逐字节保留，不重编码、不重封装；
2. **偏移必须自洽**：``moov`` 变大后再往后的字节整体位移，所有 ``stco``/``co64``
   里指向位移区之后的 chunk 偏移都要加上差值，否则媒体数据会指向错误位置；
3. **不确定就不写**：分片（``moof``）、分段索引（``sidx``）、辅助偏移（``saio``）
   这些含绝对偏移的结构一律拒绝，而不是冒险改写。
"""

import struct
from collections import namedtuple
from typing import List, Optional, Sequence, Tuple

CONTENT_IDENTIFIER_KEY = b"com.apple.quicktime.content.identifier"
STILL_IMAGE_TIME_KEY = b"com.apple.quicktime.still-image-time"
MDTA = b"mdta"

# meta 里认得的子盒类型，用来区分 QuickTime 风格（直接是子盒）与 ISO 风格（多 4 字节 version/flags）
_META_CHILDREN = {b"hdlr", b"keys", b"ilst", b"free", b"skip", b"ID32"}
# 含绝对 chunk 偏移的结构：出现就拒绝写入
_UNSAFE_BOXES = {b"sidx", b"saio", b"ssix", b"mfra", b"tfra"}


class MovError(ValueError):
    """MOV/MP4 结构不支持安全的元数据写入。"""


Box = namedtuple("Box", "type start body end size")


class KeyEntry(namedtuple("KeyEntry", "namespace value")):
    """``keys`` 表里的一项。下标从 1 开始，对应 ``ilst`` 里的子盒类型。"""


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def children(data: bytes, start: int, end: int) -> List[Box]:
    """列出 [start, end) 内的同级盒子。"""
    boxes: List[Box] = []
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise MovError("box %r 的 64 位长度字段被截断" % box_type)
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MovError("偏移 %d 处 box %r 长度 %d 越界" % (pos, box_type, size))
        boxes.append(Box(box_type, pos, pos + header, pos + size, size))
        pos += size
    return boxes


def find(data: bytes, start: int, end: int, wanted: bytes) -> Optional[Box]:
    for box in children(data, start, end):
        if box.type == wanted:
            return box
    return None


def _meta_children_start(data: bytes, meta: Box) -> int:
    """meta 的子盒起点：QuickTime 风格直接开始，ISO 风格前面多 4 字节 version/flags。"""
    kids = children(data, meta.body, meta.end)
    if kids and kids[0].type in _META_CHILDREN:
        return meta.body
    if data[meta.body:meta.body + 4] == b"\x00\x00\x00\x00":
        return meta.body + 4
    raise MovError("meta box 的子盒结构无法识别")


def _parse_keys(data: bytes, keys: Box) -> List[KeyEntry]:
    if keys.end - keys.body < 8:
        raise MovError("keys box 过短")
    count = struct.unpack(">I", data[keys.body + 4:keys.body + 8])[0]
    entries: List[KeyEntry] = []
    pos = keys.body + 8
    for _ in range(count):
        if pos + 8 > keys.end:
            raise MovError("keys 表项被截断")
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        if size < 8 or pos + size > keys.end:
            raise MovError("keys 表项长度 %d 越界" % size)
        entries.append(KeyEntry(data[pos + 4:pos + 8], data[pos + 8:pos + size]))
        pos += size
    return entries


def _find_meta(data: bytes, moov: Box) -> Optional[Box]:
    """读元数据时找 meta：优先 ``moov/meta``，其次 ``moov/udta/meta``（两种真机都有）。"""
    meta = find(data, moov.body, moov.end, b"meta")
    if meta is not None:
        return meta
    udta = find(data, moov.body, moov.end, b"udta")
    if udta is not None:
        return find(data, udta.body, udta.end, b"meta")
    return None


def _is_mdta_meta(data: bytes, meta: Box) -> bool:
    """meta 的 handler_type 是否为 mdta（苹果 Live Photo 用的就是这种 meta）。"""
    start = _meta_children_start(data, meta)
    hdlr = find(data, start, meta.end, b"hdlr")
    return hdlr is not None and data[hdlr.body + 8:hdlr.body + 12] == MDTA


def _writable_meta(data: bytes, moov: Box) -> Optional[Box]:
    """可写入的 meta：只在 ``moov/meta`` 找。

    苹果把 Live Photo 的标识符写在 ``moov/meta``，其它位置（``moov/udta/meta``，
    常见于安卓与 ffmpeg 写的文件）即使同为 mdta 键值也不动它，另建 ``moov/meta``。
    """
    meta = find(data, moov.body, moov.end, b"meta")
    if meta is not None and _is_mdta_meta(data, meta):
        return meta
    return None


def _read_meta_values(data: bytes, meta: Box) -> dict:
    """读出 meta 里 keys/ilst 的全部键值（值按 UTF-8 解码，失败的保留原始字节）。"""
    start = _meta_children_start(data, meta)
    kids = children(data, start, meta.end)
    keys_box = next((box for box in kids if box.type == b"keys"), None)
    ilst_box = next((box for box in kids if box.type == b"ilst"), None)
    values = {}
    if keys_box is None or ilst_box is None:
        return values
    entries = _parse_keys(data, keys_box)
    for item in children(data, ilst_box.body, ilst_box.end):
        index = struct.unpack(">I", item.type)[0]
        if not 1 <= index <= len(entries):
            continue
        for atom in children(data, item.body, item.end):
            if atom.type != b"data" or atom.end - atom.body < 8:
                continue
            payload = data[atom.body + 8:atom.end]
            try:
                values[entries[index - 1].value.decode("ascii")] = payload.decode("utf-8")
            except UnicodeDecodeError:
                values[entries[index - 1].value.decode("ascii", "replace")] = payload
    return values


def read_content_identifier(mov: bytes) -> Optional[str]:
    """读出 ``com.apple.quicktime.content.identifier``，没有则返回 None。"""
    top = children(mov, 0, len(mov))
    moov = next((box for box in top if box.type == b"moov"), None)
    if moov is None:
        return None
    meta = _find_meta(mov, moov)
    if meta is None:
        return None
    value = _read_meta_values(mov, meta).get(CONTENT_IDENTIFIER_KEY.decode("ascii"))
    return value if isinstance(value, str) else None


def has_still_image_time_track(mov: bytes) -> bool:
    """是否带 ``still-image-time`` 元数据轨道（真机 Live Photo 都有，本工具第一版不写）。"""
    top = children(mov, 0, len(mov))
    moov = next((box for box in top if box.type == b"moov"), None)
    return moov is not None and STILL_IMAGE_TIME_KEY in mov[moov.start:moov.end]


def mdat_payloads(data: bytes) -> List[Tuple[int, int]]:
    """所有 mdat 载荷的 (起点, 终点)。"""
    return [(box.body, box.end) for box in children(data, 0, len(data)) if box.type == b"mdat"]


def _offsets_of_chunk_box(data: bytes, box: Box) -> List[int]:
    """读出一个 stco/co64 里的全部 chunk 偏移。"""
    if box.end - box.body < 8:
        return []
    count = struct.unpack(">I", data[box.body + 4:box.body + 8])[0]
    width = 8 if box.type == b"co64" else 4
    offsets: List[int] = []
    pos = box.body + 8
    for _ in range(count):
        if pos + width > box.end:
            break
        offsets.append(struct.unpack(">Q" if width == 8 else ">I", data[pos:pos + width])[0])
        pos += width
    return offsets


def chunk_offsets(data: bytes) -> List[int]:
    """所有轨道 stco/co64 里的 chunk 偏移（自检与校验用）。"""
    top = children(data, 0, len(data))
    moov = next((box for box in top if box.type == b"moov"), None)
    if moov is None:
        return []
    offsets: List[int] = []
    for box in _iter_chunk_boxes(data, moov):
        offsets.extend(_offsets_of_chunk_box(data, box))
    return offsets


def describe(mov: bytes, name: str = "") -> List[str]:
    """给 --inspect 用的结构概览。"""
    lines = ["MOV：%s（%d 字节）" % (name or "<内存>", len(mov))]
    try:
        top = children(mov, 0, len(mov))
    except MovError as exc:
        return lines + ["  解析失败：%s" % exc]
    lines.append("  顶层 box：%s" % ", ".join("%s(%d)" % (box.type.decode("latin-1"), box.size) for box in top))
    moov = next((box for box in top if box.type == b"moov"), None)
    if moov is None:
        return lines + ["  没有 moov"]
    lines.append("  moov 子盒：%s" % ", ".join(
        "%s(%d)" % (box.type.decode("latin-1"), box.size)
        for box in children(mov, moov.body, moov.end)))
    try:
        info = _describe_tracks(mov, moov)
    except MovError as exc:
        info = ["  轨道解析失败：%s" % exc]
    lines.extend(info)
    meta = _find_meta(mov, moov)
    lines.append("  meta（mdta 键值）：%s" % ("无" if meta is None else ""))
    if meta is not None:
        for key, value in _read_meta_values(mov, meta).items():
            text = value if isinstance(value, str) else "<%d 字节>" % len(value)
            lines.append("    %s = %s" % (key, text))
    return lines


def _describe_tracks(data: bytes, moov: Box) -> List[str]:
    lines: List[str] = []
    for index, trak in enumerate(box for box in children(data, moov.body, moov.end) if box.type == b"trak"):
        mdia = find(data, trak.body, trak.end, b"mdia")
        if mdia is None:
            continue
        hdlr = find(data, mdia.body, mdia.end, b"hdlr")
        handler = data[hdlr.body + 8:hdlr.body + 12].decode("latin-1") if hdlr else "?"
        minf = find(data, mdia.body, mdia.end, b"minf")
        stbl = find(data, minf.body, minf.end, b"stbl") if minf else None
        stsd = find(data, stbl.body, stbl.end, b"stsd") if stbl else None
        codec = ""
        if stsd is not None and stsd.end - stsd.body >= 16:
            codec = data[stsd.body + 12:stsd.body + 16].decode("latin-1")
        stco = find(data, stbl.body, stbl.end, b"stco") if stbl else None
        chunk_note = ""
        if stco is not None:
            offsets = _offsets_of_chunk_box(data, stco)
            chunk_note = " chunk=%s%s" % (offsets[:3], "…" if len(offsets) > 3 else "")
        lines.append("  轨道 %d：handler=%s codec=%s%s" % (index + 1, handler, codec or "-", chunk_note))
    return lines


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #

def build_box(box_type: bytes, payload: bytes) -> bytes:
    """构造盒子；载荷超过 4GB 时用 64 位长度。"""
    if len(payload) + 8 > 0xFFFFFFFF:
        return struct.pack(">I", 1) + box_type + struct.pack(">Q", len(payload) + 16) + payload
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _build_meta_hdlr() -> bytes:
    """meta 的 mdta handler，字节与真机文件一致（34 字节）。"""
    payload = b"\x00" * 4 + b"\x00" * 4 + MDTA + b"\x00" * 12 + b"\x00\x00"
    return build_box(b"hdlr", payload)


def _build_keys(entries: Sequence[KeyEntry]) -> bytes:
    payload = b"\x00\x00\x00\x00" + struct.pack(">I", len(entries))
    for entry in entries:
        payload += struct.pack(">I", 8 + len(entry.value)) + entry.namespace + entry.value
    return build_box(b"keys", payload)


def _build_ilst_item(index: int, value: bytes) -> bytes:
    """ilst 里的一项：类型是 keys 表下标（1 基），内容是 data 原子。

    data 原子的 type_indicator=1 表示 UTF-8 字符串、locale=0，与真机一致。
    """
    return build_box(struct.pack(">I", index), build_box(b"data", struct.pack(">II", 1, 0) + value))


def _merge_meta(data: bytes, meta: Optional[Box], value: bytes) -> bytes:
    """在 meta 里写入（或更新）content.identifier，其余子盒与其它键原样保留。"""
    if meta is None:
        keys = _build_keys([KeyEntry(MDTA, CONTENT_IDENTIFIER_KEY)])
        ilst = build_box(b"ilst", _build_ilst_item(1, value))
        return build_box(b"meta", _build_meta_hdlr() + keys + ilst)

    start = _meta_children_start(data, meta)
    prefix = data[meta.body:start]
    kids = children(data, start, meta.end)
    hdlr = next((box for box in kids if box.type == b"hdlr"), None)
    keys_box = next((box for box in kids if box.type == b"keys"), None)
    ilst_box = next((box for box in kids if box.type == b"ilst"), None)

    if keys_box is None and ilst_box is not None:
        raise MovError("meta 里有 ilst 却没有 keys 表，结构异常")

    entries = _parse_keys(data, keys_box) if keys_box is not None else []
    index = None
    for position, entry in enumerate(entries, start=1):
        if entry.value == CONTENT_IDENTIFIER_KEY:
            index = position
            break

    if index is None:
        # 键表要新增一项：整体重写（其余表项的命名空间与键名逐字节照搬），新下标接在末尾
        index = len(entries) + 1
        new_keys = _build_keys(list(entries) + [KeyEntry(MDTA, CONTENT_IDENTIFIER_KEY)])
    else:
        new_keys = data[keys_box.start:keys_box.end]

    items = []
    replaced = False
    for item in children(data, ilst_box.body, ilst_box.end) if ilst_box is not None else []:
        if struct.unpack(">I", item.type)[0] == index:
            items.append(_build_ilst_item(index, value))
            replaced = True
        else:
            items.append(data[item.start:item.end])
    if not replaced:
        items.append(_build_ilst_item(index, value))
    new_ilst = build_box(b"ilst", b"".join(items))

    parts = []
    if hdlr is None:
        parts.append(_build_meta_hdlr())
    else:
        parts.append(data[hdlr.start:hdlr.end])
    parts.append(new_keys)
    parts.append(new_ilst)
    for box in kids:
        if box.type in (b"hdlr", b"keys", b"ilst"):
            continue
        parts.append(data[box.start:box.end])
    return build_box(b"meta", prefix + b"".join(parts))


def _iter_chunk_boxes(data: bytes, moov: Box):
    for trak in children(data, moov.body, moov.end):
        if trak.type != b"trak":
            continue
        mdia = find(data, trak.body, trak.end, b"mdia")
        if mdia is None:
            continue
        minf = find(data, mdia.body, mdia.end, b"minf")
        if minf is None:
            continue
        stbl = find(data, minf.body, minf.end, b"stbl")
        if stbl is None:
            continue
        for box in children(data, stbl.body, stbl.end):
            if box.type in (b"stco", b"co64"):
                yield box
            elif box.type in _UNSAFE_BOXES:
                raise MovError("轨道里有 %r（含绝对偏移的特殊结构），不支持改写"
                               % box.type.decode("latin-1"))


def _patch_chunk_offsets(moov_bytes: bytes, old: Box, delta: int) -> bytes:
    """moov 变大后修正 chunk 偏移：落在位移区之后的加差值。

    偏移指向 moov 之前的数据时不受影响；落在 moov 内部说明结构异常，直接报错。
    """
    result = bytearray(moov_bytes)
    # 新 moov 的起点与旧 moov 相同，长度已变，这里按新字节重新定位内部结构
    header = 16 if struct.unpack(">I", moov_bytes[:4])[0] == 1 else 8
    new_moov = Box(b"moov", 0, header, len(moov_bytes), len(moov_bytes))
    for box in _iter_chunk_boxes(moov_bytes, new_moov):
        name = box.type.decode("latin-1")
        if box.end - box.body < 8:
            raise MovError("%s box 过短" % name)
        count = struct.unpack(">I", result[box.body + 4:box.body + 8])[0]
        width = 8 if box.type == b"co64" else 4
        pos = box.body + 8
        for _ in range(count):
            if pos + width > box.end:
                raise MovError("%s 的偏移表被截断" % name)
            value = struct.unpack(">Q" if width == 8 else ">I", result[pos:pos + width])[0]
            if old.start <= value < old.end:
                raise MovError("chunk 偏移 %d 落在 moov 内部，结构异常" % value)
            if value >= old.end:
                result[pos:pos + width] = struct.pack(">Q" if width == 8 else ">I", value + delta)
            pos += width
    return bytes(result)


def set_content_identifier(mov: bytes, identifier: str) -> bytes:
    """写入 ``com.apple.quicktime.content.identifier``，返回新的 MOV 字节。

    只改 ``moov/meta``；``mdat`` 载荷逐字节保留。
    """
    try:
        value = identifier.encode("ascii")
    except UnicodeEncodeError:
        raise MovError("标识符必须是 ASCII 字符串：%r" % identifier)

    top = children(mov, 0, len(mov))
    if not top:
        raise MovError("文件里没有可解析的 box")
    if top[0].type != b"ftyp":
        raise MovError("首个 box 是 %r 而不是 ftyp" % top[0].type.decode("latin-1"))
    unsafe = [box.type.decode("latin-1") for box in top if box.type in _UNSAFE_BOXES]
    if b"moof" in [box.type for box in top]:
        raise MovError("这是分片（moof）文件，含相对分片偏移，不支持写入；请先转成普通 MP4/MOV")
    if unsafe:
        raise MovError("文件里有 %s（含绝对偏移的索引结构），不支持写入" % "/".join(unsafe))
    moovs = [box for box in top if box.type == b"moov"]
    if len(moovs) != 1:
        raise MovError("需要恰好一个 moov，实际找到 %d 个" % len(moovs))
    moov = moovs[0]

    meta = _writable_meta(mov, moov)
    if meta is None:
        # 苹果的位置是直接挂在 moov 下的 meta；已有的非 mdta meta 原样保留，我们插在它前面
        # （按顺序取第一个 meta 的读取器因此能读到我们的键），没有就在 moov 末尾追加
        new_meta = _merge_meta(mov, None, value)
        parts = []
        inserted = False
        for kid in children(mov, moov.body, moov.end):
            if kid.type == b"meta" and not inserted:
                parts.append(new_meta)
                inserted = True
            parts.append(mov[kid.start:kid.end])
        if not inserted:
            parts.append(new_meta)
        new_moov = build_box(b"moov", b"".join(parts))
    else:
        new_meta = _merge_meta(mov, meta, value)
        parts = [
            new_meta if kid.start == meta.start else mov[kid.start:kid.end]
            for kid in children(mov, moov.body, moov.end)
        ]
        new_moov = build_box(b"moov", b"".join(parts))

    delta = len(new_moov) - moov.size
    if delta:
        new_moov = _patch_chunk_offsets(new_moov, moov, delta)
    result = mov[:moov.start] + new_moov + mov[moov.end:]

    _self_check(mov, result, identifier)
    return result


def _self_check(before: bytes, after: bytes, identifier: str) -> None:
    """写出前自检：媒体载荷未变、标识符可读回、chunk 偏移仍落在 mdat 内。"""
    before_payload = b"".join(before[start:end] for start, end in mdat_payloads(before))
    after_payload = b"".join(after[start:end] for start, end in mdat_payloads(after))
    if before_payload != after_payload:
        raise MovError("自检未通过：mdat 载荷发生了变化")

    found = read_content_identifier(after)
    if found != identifier:
        raise MovError("自检未通过：写回读到的标识符是 %r" % found)

    ranges = mdat_payloads(after)
    for offset in chunk_offsets(after):
        if not any(start <= offset < end for start, end in ranges):
            raise MovError("自检未通过：chunk 偏移 %d 不在任何 mdat 内" % offset)
