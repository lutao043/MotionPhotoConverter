"""测试用的最小样本构造器（不依赖任何外部文件）。"""

import struct

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


# --------------------------------------------------------------------------- #
# JPEG
# --------------------------------------------------------------------------- #

def _segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def _app0_jfif() -> bytes:
    return _segment(0xE0, b"JFIF\x00\x01\x02\x01\x00H\x00H\x00\x00")


def _dqt() -> bytes:
    return _segment(0xDB, b"\x00" + bytes([16] * 64))


def _sof0(width: int, height: int) -> bytes:
    payload = b"\x08" + struct.pack(">HH", height, width) + b"\x03"
    payload += b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return _segment(0xC0, payload)


def _dht() -> bytes:
    counts = bytes([1] + [0] * 15)
    return _segment(0xC4, b"\x00" + counts + b"\x00")


def _sos() -> bytes:
    payload = b"\x03" + b"\x01\x00\x02\x11\x03\x11" + b"\x00\x3f\x00"
    return _segment(0xDA, payload)


def build_tiff_with_exif() -> bytes:
    """手写一份含 IFD0 / ExifIFD / IFD1 缩略图的小端 TIFF。

    布局是显式算好的，测试会据此断言「原有字节一个都没变」。
    """
    ifd0_offset = 8
    entries = [
        (0x0112, 3, 1, struct.pack("<H", 6) + b"\x00\x00"),  # Orientation = 6（旋转）
        (0x010F, 2, 8, None),  # Make，数据区
        (0x0132, 2, 20, None),  # DateTime，数据区
        (0x8769, 4, 1, None),  # ExifIFD 指针
    ]
    ifd0_size = 2 + 12 * len(entries) + 4
    data_offset = ifd0_offset + ifd0_size  # 62
    make = b"TestCam\x00"  # 8
    make_offset = data_offset
    date = b"2026:01:01 00:00:00\x00"  # 20
    date_offset = make_offset + len(make)

    exif_ifd_offset = date_offset + len(date)  # 90
    exif_entries = [
        (0x9000, 7, 4, b"0232"),  # ExifVersion，内联
        (0x9286, 7, 19, None),  # UserComment，数据区
    ]
    exif_ifd_size = 2 + 12 * len(exif_entries) + 4
    comment = b"ASCII\x00\x00\x00Old comment"  # 19
    comment_offset = exif_ifd_offset + exif_ifd_size  # 120

    ifd1_offset = comment_offset + len(comment)  # 139 -> 补到偶数
    if ifd1_offset % 2:
        ifd1_offset += 1
    ifd1_entries = [
        (0x0201, 4, 1, None),  # 缩略图数据偏移
        (0x0202, 4, 1, struct.pack("<I", 4)),  # 缩略图长度
    ]
    ifd1_size = 2 + 12 * len(ifd1_entries) + 4
    thumbnail_offset = ifd1_offset + ifd1_size  # 实测会断言
    thumbnail = b"\xff\xd8\xff\xd9"

    entries[1] = (0x010F, 2, len(make), struct.pack("<I", make_offset))
    entries[2] = (0x0132, 2, len(date), struct.pack("<I", date_offset))
    entries[3] = (0x8769, 4, 1, struct.pack("<I", exif_ifd_offset))
    exif_entries[1] = (0x9286, 7, len(comment), struct.pack("<I", comment_offset))
    ifd1_entries[0] = (0x0201, 4, 1, struct.pack("<I", thumbnail_offset))

    out = bytearray()
    out += b"II" + struct.pack("<H", 42) + struct.pack("<I", ifd0_offset)
    while len(out) < ifd0_offset:
        out.append(0)
    out += struct.pack("<H", len(entries))
    for tag, type_, count, raw in entries:
        out += struct.pack("<HHI", tag, type_, count) + raw
    out += struct.pack("<I", ifd1_offset)
    assert len(out) == data_offset, (len(out), data_offset)
    out += make
    out += date
    assert len(out) == exif_ifd_offset
    out += struct.pack("<H", len(exif_entries))
    for tag, type_, count, raw in exif_entries:
        out += struct.pack("<HHI", tag, type_, count) + raw
    out += struct.pack("<I", 0)
    assert len(out) == comment_offset
    out += comment
    while len(out) < ifd1_offset:
        out.append(0)
    out += struct.pack("<H", len(ifd1_entries))
    for tag, type_, count, raw in ifd1_entries:
        out += struct.pack("<HHI", tag, type_, count) + raw
    out += struct.pack("<I", 0)
    assert len(out) == thumbnail_offset, (len(out), thumbnail_offset)
    out += thumbnail
    return bytes(out)


TIFF_EXIF = build_tiff_with_exif()


def fixture_tiff_offsets():
    """测试要用的关键偏移（避免在测试里写魔数）。"""
    ifd0_offset = struct.unpack("<I", TIFF_EXIF[4:8])[0]
    return {
        "ifd0": ifd0_offset,
        "make": struct.unpack("<I", TIFF_EXIF[ifd0_offset + 2 + 12 + 8:ifd0_offset + 2 + 12 + 12])[0],
        "date": struct.unpack("<I", TIFF_EXIF[ifd0_offset + 2 + 24 + 8:ifd0_offset + 2 + 24 + 12])[0],
        "exif_ifd": struct.unpack("<I", TIFF_EXIF[ifd0_offset + 2 + 36 + 8:ifd0_offset + 2 + 36 + 12])[0],
        "ifd1": struct.unpack("<I", TIFF_EXIF[ifd0_offset + 2 + 48:ifd0_offset + 2 + 52])[0],
    }


def build_jpeg(with_exif: bool = True, with_xmp: bool = False, trailing: bytes = b"") -> bytes:
    """构造一个结构完整的 JPEG（可解码性不在测试范围，只要求结构合法）。"""
    out = bytearray(SOI)
    out += _app0_jfif()
    if with_exif:
        out += _segment(0xE1, b"Exif\x00\x00" + TIFF_EXIF)
    if with_xmp:
        from motionphoto import xmp as xmp_module

        packet = xmp_module.build_packet("google", 1234, 0)
        out += _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + packet.encode("utf-8"))
    out += _dqt()
    out += _sof0(8, 8)
    out += _dht()
    out += _sos()
    # 熵编码数据：含 0xFF00 字节填充与 0xFFD0 重启标记，用来验证扫描逻辑
    out += b"\x01\x02\xff\x00\x03\xff\xd0\x04\x05\x06"
    out += EOI
    out += trailing
    return bytes(out)


def jpeg_image_tail(data: bytes) -> bytes:
    """返回从 SOS 到 EOI 的字节，用于断言主图数据未被改动。"""
    from motionphoto import jpeg as jpeg_module

    segments, eoi = jpeg_module.parse_structure(data)
    sos = [seg for seg in segments if seg.marker == jpeg_module.MARKER_SOS]
    assert sos, "样本里没有 SOS"
    return data[sos[-1].offset:eoi + 2]


# --------------------------------------------------------------------------- #
# MP4
# --------------------------------------------------------------------------- #

def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _mvhd(duration_ms: int) -> bytes:
    payload = b"\x00\x00\x00\x00"  # version 0 + flags
    payload += struct.pack(">II", 0, 0)  # creation / modification
    payload += struct.pack(">II", 1000, duration_ms)  # timescale / duration
    payload += struct.pack(">IHH", 65536, 0x0100, 0)  # rate / volume / reserved
    payload += b"\x00" * 8 + struct.pack(">9I", *([65536, 0, 0] * 3))
    payload += b"\x00" * 24 + struct.pack(">I", 2)
    return _box(b"mvhd", payload)


def _trak(handler: str, codec: bytes) -> bytes:
    hdlr_payload = b"\x00\x00\x00\x00" + struct.pack(">I", 0) + handler.encode("ascii") + b"\x00" * 12
    stsd_entry = struct.pack(">I", 16) + codec + b"\x00" * 8
    stsd_payload = b"\x00\x00\x00\x00" + struct.pack(">I", 1) + stsd_entry
    stbl = _box(b"stbl", _box(b"stsd", stsd_payload))
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", _box(b"hdlr", hdlr_payload) + minf)
    return _box(b"trak", mdia)


def build_mp4(duration_ms: int = 3000, codec: bytes = b"avc1", audio: bool = True,
              fragmented: bool = False, payload: bytes = b"\x00" * 64) -> bytes:
    """构造结构合法的 MP4（不追求可播放，只要求 box 结构正确）。"""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isommp42")
    moov_payload = _mvhd(duration_ms) + _trak("vide", codec)
    if audio:
        moov_payload += _trak("soun", b"mp4a")
    moov = _box(b"moov", moov_payload)
    mdat = _box(b"mdat", payload)
    if fragmented:
        moof = _box(b"moof", _box(b"mfhd", b"\x00\x00\x00\x00" + struct.pack(">I", 1)))
        return ftyp + moov + moof + mdat
    return ftyp + moov + mdat


def build_mp4_without_moov() -> bytes:
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isom")
    return ftyp + _box(b"mdat", b"\x00" * 32)


# --------------------------------------------------------------------------- #
# MOV（苹果实况照片的视频侧）：带 stco 的最小结构，可放在 mdat 之前或之后
# --------------------------------------------------------------------------- #

def _mov_trak(chunk_offsets, sample_sizes) -> bytes:
    """最小视频轨：hdlr=vide + stsd(avc1) + stts/stsc/stsz/stco。"""
    hdlr = _box(b"hdlr", b"\x00\x00\x00\x00" + struct.pack(">I", 0) + b"vide" + b"\x00" * 12 + b"\x00\x00")
    mdhd = _box(b"mdhd", b"\x00\x00\x00\x00" + struct.pack(">II", 0, 0)
                + struct.pack(">II", 600, 600) + b"\x00\x00\x00\x00")
    stsd_entry = struct.pack(">I", 16) + b"avc1" + b"\x00" * 8
    stsd = _box(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + stsd_entry)
    stts = _box(b"stts", b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                + struct.pack(">II", len(sample_sizes), 500))
    stsc = _box(b"stsc", b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                + struct.pack(">III", 1, len(sample_sizes), 1))
    stsz = _box(b"stsz", b"\x00\x00\x00\x00" + struct.pack(">II", 0, len(sample_sizes))
                + b"".join(struct.pack(">I", size) for size in sample_sizes))
    stco = _box(b"stco", b"\x00\x00\x00\x00" + struct.pack(">I", len(chunk_offsets))
                + b"".join(struct.pack(">I", offset) for offset in chunk_offsets))
    stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
    minf = _box(b"minf", _box(b"vmhd", b"\x00\x00\x00\x01" + b"\x00" * 8) + stbl)
    return _box(b"trak", _box(b"mdia", mdhd + hdlr + minf))


def mdta_meta(entries) -> bytes:
    """QuickTime 风格的 mdta meta（与真机一样：无 version/flags，hdlr+keys+ilst）。"""
    keys = b"".join(
        struct.pack(">I", 8 + len(key)) + b"mdta" + key for key in entries
    )
    keys_box = _box(b"keys", b"\x00\x00\x00\x00" + struct.pack(">I", len(entries)) + keys)
    items = b""
    for index, key in enumerate(entries, start=1):
        items += _box(struct.pack(">I", index),
                      _box(b"data", struct.pack(">II", 1, 0) + b"value-%d" % index))
    hdlr = _box(b"hdlr", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + b"mdta" + b"\x00" * 12 + b"\x00\x00")
    return _box(b"meta", hdlr + keys_box + _box(b"ilst", items))


def itunes_udta_meta(software: bytes = b"Lavf57") -> bytes:
    """ISO 风格的 iTunes 元数据（hdlr 是 mdir、ilst 无 keys 表），真机与 ffmpeg 都这么写。"""
    hdlr = _box(b"hdlr", b"\x00\x00\x00\x00" + struct.pack(">I", 0) + b"mdir" + b"appl" + b"\x00" * 9)
    ilst = _box(b"ilst", _box(b"\xa9too", _box(b"data", struct.pack(">II", 1, 0) + software)))
    return _box(b"udta", _box(b"meta", b"\x00\x00\x00\x00" + hdlr + ilst))


def build_mov(moov_first: bool = False, media: bytes = b"\xaa" * 64,
              meta_keys=(), with_itunes_meta: bool = False, fragmented: bool = False) -> bytes:
    """构造一个 ftyp 品牌为 qt 的最小 MOV。

    moov_first=True 时 moov 在 mdat 之前，chunk 偏移指到 mdat 载荷里——
    这正是「写元数据撑大 moov 后必须修正偏移」的场景。
    """
    ftyp = _box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    sizes = [len(media) // 2 or 1, len(media) - (len(media) // 2 or 1)]

    def assemble(offsets) -> bytes:
        payload = _mvhd(3000) + _mov_trak(offsets, sizes)
        if meta_keys:
            payload += mdta_meta(list(meta_keys))
        if with_itunes_meta:
            payload += itunes_udta_meta()
        return payload

    probe = ftyp + _box(b"moov", assemble([0, 0])) + _box(b"mdat", media)
    moov_size = len(probe) - len(ftyp) - len(_box(b"mdat", media))
    payload_start = len(ftyp) + moov_size + 8 if moov_first else len(ftyp) + 8
    offsets = [payload_start, payload_start + sizes[0]]
    moov = _box(b"moov", assemble(offsets))
    if fragmented:
        moof = _box(b"moof", _box(b"mfhd", b"\x00\x00\x00\x00" + struct.pack(">I", 1)))
        return ftyp + moov + moof + _box(b"mdat", media)
    if moov_first:
        return ftyp + moov + _box(b"mdat", media)
    return ftyp + _box(b"mdat", media) + moov


def mov_mdat_payload(data: bytes) -> bytes:
    """取出 MOV 里 mdat 的载荷（断言媒体数据逐字节未变）。"""
    from motionphoto import mov as mov_module

    return b"".join(data[start:end] for start, end in mov_module.mdat_payloads(data))


# --------------------------------------------------------------------------- #
# Apple MakerNote（独立手写，用来验证原地改值不动其它条目）
# --------------------------------------------------------------------------- #

def build_apple_makernote(identifier: str, with_extra_entries: bool = True) -> bytes:
    """手写一份 Apple MakerNote：12 字节签名 + 14 字节内部 TIFF 头 + IFD。

    故意不调用 motionphoto.exif 的实现，这样测试验证的是「别人写的 MakerNote
    我们也能认、也能原地改值」，而不是自证。
    """
    signature = b"Apple iOS\x00\x00\x01"
    head = b"MM\x00\x2a\x00\x01\x00\x09\x00\x00\x00\x01\x00\x00"
    ifd_offset = len(signature) + len(head)
    value = identifier.encode("ascii") + b"\x00"
    entries = []
    if with_extra_entries:
        entries.append((0x0003, 7, 12, b"bplist00\x00\x00\x00"))  # 12 字节，走值区
        entries.append((0x0004, 9, 1, struct.pack(">i", 1)))       # 内联
    entries.append((0x0011, 2, len(value), value))
    entries.sort(key=lambda item: item[0])

    ifd_size = 2 + 12 * len(entries) + 4
    cursor = ifd_offset + ifd_size
    ifd = struct.pack(">H", len(entries))
    pending = []
    for tag, type_, count, raw in entries:
        if len(raw) <= 4:
            ifd += struct.pack(">HHI", tag, type_, count) + raw.ljust(4, b"\x00")
            continue
        ifd += struct.pack(">HHI", tag, type_, count) + struct.pack(">I", cursor)
        pending.append((cursor, raw))
        cursor += len(raw)
    ifd += struct.pack(">I", 0x00200002)  # 真机的这段不是 0，照抄

    out = bytearray(signature + head + ifd)
    for offset, raw in pending:
        while len(out) < offset:
            out.append(0)
        out += raw
    return bytes(out)


# --------------------------------------------------------------------------- #
# 旧版脚本的产物（用于回归：这种文件必须校验失败）
# --------------------------------------------------------------------------- #

def vivo_style_packet(gainmap_length: int, video_length: int, include_vendor_fields: bool = True) -> str:
    """仿 vivo 真机的 XMP：主图项不带 Length、含 GainMap 项、视频项带 Length。

    注意真机文件里就是这样：主图项只有 Mime/Semantic，Length/Padding 直接不写。
    """
    vendor = (
        '      VCamera:VMotionPhotoVersion="1"\n'
        '      VCamera:VMotionPhotoSource="1"\n'
        '      VCamera:VMediaKitVersion="1.0.0.5">\n'
        if include_vendor_fields
        else ">\n"
    )
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1.0-jc003">\n'
        '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '    <rdf:Description rdf:about=""\n'
        '        xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"\n'
        '        xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
        '        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"\n'
        '        xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"\n'
        '        xmlns:VCamera="http://ns.vivo.com/photos/1.0/camera/"\n'
        '      hdrgm:Version="1.0"\n'
        '      GCamera:MotionPhoto="1"\n'
        '      GCamera:MotionPhotoVersion="1"\n'
        '      GCamera:MotionPhotoPresentationTimestampUs="1483705"\n'
        + vendor
        + "      <Container:Directory>\n"
        "        <rdf:Seq>\n"
        '          <rdf:li rdf:parseType="Resource">\n'
        "            <Container:Item\n"
        '              Item:Semantic="Primary"\n'
        '              Item:Mime="image/jpeg"/>\n'
        "          </rdf:li>\n"
        '          <rdf:li rdf:parseType="Resource">\n'
        "            <Container:Item\n"
        '              Item:Semantic="GainMap"\n'
        '              Item:Mime="image/jpeg"\n'
        '              Item:Length="%d"/>\n' % gainmap_length
        + "          </rdf:li>\n"
        '          <rdf:li rdf:parseType="Resource">\n'
        "            <Container:Item\n"
        '              Item:Mime="video/mp4"\n'
        '              Item:Semantic="MotionPhoto"\n'
        '              Item:Length="%d"\n' % video_length
        + '              Item:Padding="0"/>\n'
        "          </rdf:li>\n"
        "        </rdf:Seq>\n"
        "      </Container:Directory>\n"
        "    </rdf:Description>\n"
        "  </rdf:RDF>\n"
        "</x:xmpmeta>"
    )


def build_vivo_style_output(vendor_gap: bytes = b"\x00" * 64, gainmap_extra: int = 0,
                            video_length_delta: int = 0):
    """仿 vivo 真机结构：主图 | GainMap | Directory 未列出的厂商数据 | 视频。

    gainmap_extra / video_length_delta 用来故意把声明的 Length 写错，构造越界或错位样本。
    """
    from motionphoto import jpeg as jpeg_module

    jpeg_bytes = build_jpeg(with_exif=True)
    gainmap = build_jpeg(with_exif=False)
    mp4 = build_mp4()
    packet = vivo_style_packet(len(gainmap) + gainmap_extra, len(mp4) + video_length_delta)
    jpeg_with_xmp = jpeg_module.upsert_app_segment(
        jpeg_bytes,
        jpeg_module.MARKER_APP1,
        jpeg_module.XMP_SIGNATURE,
        jpeg_module.XMP_SIGNATURE + packet.encode("utf-8"),
    )
    data = jpeg_with_xmp + gainmap + vendor_gap + mp4
    return data, mp4, gainmap


def legacy_xmp_packet() -> bytes:
    """旧版脚本写出的 XMP：拼在 EOI 之后、子元素写法、Length 全为 0、时间戳是魔数。"""
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkcK"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        '             xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '             xmlns:Camera="http://ns.google.com/photos/1.0/camera/"\n'
        '             xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
        '             xmlns:Item="http://ns.google.com/photos/1.0/container/item/">\n'
        "        <rdf:Description>\n"
        "            <Camera:MotionPhoto>1</Camera:MotionPhoto>\n"
        "            <Camera:MotionPhotoVersion>1</Camera:MotionPhotoVersion>\n"
        '            <Camera:MotionPhotoPresentationTimestampUs>123456789'
        "</Camera:MotionPhotoPresentationTimestampUs>\n"
        "            <Container:Directory>\n"
        "                <rdf:Seq>\n"
        '                    <rdf:li rdf:parseType="Resource">\n'
        "                        <Item:Mime>image/jpeg</Item:Mime>\n"
        "                        <Item:Semantic>Primary</Item:Semantic>\n"
        "                        <Item:Length>0</Item:Length>\n"
        "                        <Item:Padding>32</Item:Padding>\n"
        "                    </rdf:li>\n"
        '                    <rdf:li rdf:parseType="Resource">\n'
        "                        <Item:Mime>video/mp4</Item:Mime>\n"
        "                        <Item:Semantic>Secondary</Item:Semantic>\n"
        "                    </rdf:li>\n"
        "                </rdf:Seq>\n"
        "            </Container:Directory>\n"
        "        </rdf:Description>\n"
        "    </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n'
    ).encode("utf-8")


def build_legacy_output(jpeg: bytes, mp4: bytes) -> bytes:
    """复刻旧版 main.py 的输出：XMP 裸拼在 EOI 之后、Length 全为 0、尾部 32 个 NUL。"""
    return jpeg + legacy_xmp_packet() + b"\x00" * 32 + mp4


# --------------------------------------------------------------------------- #
# 真机风格样本（属性写法 + 已有旧字段），用于合并测试
# --------------------------------------------------------------------------- #

def build_device_style_packet(video_length: int) -> str:
    """仿真机文件：属性写法、视频项 Semantic=MotionPhoto，另含无关元数据。"""
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkcK"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1.2">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "   <dc:description><rdf:Alt><rdf:li xml:lang=\"x-default\">保留我</rdf:li></rdf:Alt>"
        "</dc:description>\n"
        "  </rdf:Description>\n"
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"\n'
        '    xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
        '    xmlns:Item="http://ns.google.com/photos/1.0/container/item/"\n'
        '    GCamera:MotionPhoto="1"\n'
        '    GCamera:MotionPhotoVersion="1"\n'
        '    GCamera:MotionPhotoPresentationTimestampUs="999999"\n'
        '    GCamera:MicroVideo="1"\n'
        '    GCamera:MicroVideoVersion="1"\n'
        '    GCamera:MicroVideoOffset="%d">\n' % video_length
        + "   <Container:Directory>\n"
        "    <rdf:Seq>\n"
        '     <rdf:li rdf:parseType="Resource">\n'
        '      <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" '
        'Item:Length="0" Item:Padding="0"/>\n'
        "     </rdf:li>\n"
        '     <rdf:li rdf:parseType="Resource">\n'
        '      <Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" '
        'Item:Length="%d" Item:Padding="0"/>\n' % video_length
        + "     </rdf:li>\n"
        "    </rdf:Seq>\n"
        "   </Container:Directory>\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n'
    )
