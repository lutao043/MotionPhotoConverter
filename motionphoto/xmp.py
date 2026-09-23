"""XMP 元数据生成、合并与读取。

写入的属性对齐真机文件（见 TECHNICAL.md「各家实现对照」）：
  * 通用：GCamera:MotionPhoto / MotionPhotoVersion / MotionPhotoPresentationTimestampUs
           + Container:Directory（主图项 + 视频项，视频项 Length = MP4 字节数）
  * 小米：额外补旧字段 GCamera:MicroVideo / MicroVideoVersion / MicroVideoOffset /
           MicroVideoPresentationTimestampUs，兼容仍在读旧字段的读取器
  * OPPO：额外补 OpCamera:*（命名空间 http://ns.oplus.com/photos/1.0/camera/）
  * vivo：额外补 VCamera:VMotionPhotoVersion / VMotionPhotoSource / VMediaKitVersion
           （命名空间 http://ns.vivo.com/photos/1.0/camera/，三个字段必须同时出现）

Container:Item 使用属性写法（``<Container:Item Item:Mime="…"/>``），与真机文件一致。
"""

import re
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import __version__

NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_CAMERA = "http://ns.google.com/photos/1.0/camera/"
NS_CONTAINER = "http://ns.google.com/photos/1.0/container/"
NS_ITEM = "http://ns.google.com/photos/1.0/container/item/"
NS_OPCAMERA = "http://ns.oplus.com/photos/1.0/camera/"
NS_VCAMERA = "http://ns.vivo.com/photos/1.0/camera/"
NS_HDRGM = "http://ns.adobe.com/hdr-gain-map/1.0/"

XMP_HEADER = '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkcK"?>'
XMP_TRAILER = '<?xpacket end="w"?>'

VIDEO_MIME = "video/mp4"
IMAGE_MIME = "image/jpeg"
SEMANTIC_PRIMARY = "Primary"
SEMANTIC_MOTION_PHOTO = "MotionPhoto"
SEMANTIC_GAIN_MAP = "GainMap"

# 由本工具负责写入的属性：合并前会先清掉源图里的同名旧值，
# 否则文件里会同时留着指向旧视频位置的过期偏移。
OWNED_PROPERTIES: Tuple[Tuple[str, str], ...] = (
    ("GCamera", "MotionPhoto"),
    ("GCamera", "MotionPhotoVersion"),
    ("GCamera", "MotionPhotoPresentationTimestampUs"),
    ("GCamera", "MicroVideo"),
    ("GCamera", "MicroVideoVersion"),
    ("GCamera", "MicroVideoOffset"),
    ("GCamera", "MicroVideoPresentationTimestampUs"),
    ("OpCamera", "MotionPhotoOwner"),
    ("OpCamera", "OLivePhotoVersion"),
    ("OpCamera", "MotionPhotoFeatureFlag"),
    ("OpCamera", "VideoLength"),
    ("OpCamera", "MotionPhotoPrimaryPresentationTimestampUs"),
    ("VCamera", "VMotionPhotoVersion"),
    ("VCamera", "VMotionPhotoSource"),
    ("VCamera", "VMediaKitVersion"),
)
# 结构型元素，同样整体重建
OWNED_ELEMENTS: Tuple[Tuple[str, str], ...] = (("Container", "Directory"),)

# 同一命名空间的常见别名前缀（Camera: 与 GCamera: 语义等价）
PREFIX_ALIASES = {
    "Camera": "GCamera",
    "GContainer": "Container",
    "GItem": "Item",
}

VENDORS = ("xiaomi", "google", "samsung", "oppo", "vivo")
DEFAULT_VENDOR = "xiaomi"


class XmpError(ValueError):
    """XMP 结构不可用。"""


class Item(NamedTuple):
    """Container:Directory 里的一项。"""

    mime: Optional[str] = None
    semantic: Optional[str] = None
    length: Optional[int] = None
    padding: Optional[int] = None


class MotionPhotoMeta(NamedTuple):
    """从 XMP 里读出的动态照片信息。"""

    present: bool
    parse_ok: bool
    timestamp_us: Optional[int]
    micro_video_offset: Optional[int]
    items: List[Item]
    properties: Dict[str, str]

    @property
    def video_item(self) -> Optional[Item]:
        for item in self.items:
            if (item.mime or "").lower() == VIDEO_MIME:
                return item
        for item in self.items:
            if (item.semantic or "").lower() == SEMANTIC_MOTION_PHOTO.lower():
                return item
        return None


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def vendor_properties(vendor: str, video_length: int, timestamp_us: int):
    """返回该厂商档位需要写入的 (前缀, 属性名, 值) 列表。"""
    props = [
        ("GCamera", "MotionPhoto", "1"),
        ("GCamera", "MotionPhotoVersion", "1"),
        ("GCamera", "MotionPhotoPresentationTimestampUs", str(timestamp_us)),
    ]
    if vendor == "xiaomi":
        # 旧一代读取器（小米等）仍按 MicroVideo 体系找视频，
        # 语义与 Container 项一致：偏移自文件末尾起算，即视频字节数。
        props += [
            ("GCamera", "MicroVideo", "1"),
            ("GCamera", "MicroVideoVersion", "1"),
            ("GCamera", "MicroVideoOffset", str(video_length)),
            ("GCamera", "MicroVideoPresentationTimestampUs", str(timestamp_us)),
        ]
    elif vendor == "oppo":
        props += [
            ("OpCamera", "MotionPhotoOwner", "oplus"),
            ("OpCamera", "OLivePhotoVersion", "2"),
            ("OpCamera", "MotionPhotoFeatureFlag", "1"),
            ("OpCamera", "VideoLength", str(video_length)),
            ("OpCamera", "MotionPhotoPrimaryPresentationTimestampUs", str(timestamp_us)),
        ]
    elif vendor == "vivo":
        # 三者必须同时出现（真机 vivo 文件即如此）
        props += [
            ("VCamera", "VMotionPhotoVersion", "1"),
            ("VCamera", "VMotionPhotoSource", "1"),
            ("VCamera", "VMediaKitVersion", "1.0.0.5"),
        ]
    return props


def build_description(vendor: str, video_length: int, timestamp_us: int, indent: str = "  ") -> str:
    """构造 rdf:Description（属性写法 + Container:Directory 子元素）。"""
    namespaces = [("GCamera", NS_CAMERA)]
    if vendor == "oppo":
        namespaces.append(("OpCamera", NS_OPCAMERA))
    elif vendor == "vivo":
        namespaces.append(("VCamera", NS_VCAMERA))
    namespaces += [("Container", NS_CONTAINER), ("Item", NS_ITEM)]

    attrs = ['rdf:about=""']
    attrs += ['xmlns:%s="%s"' % (prefix, uri) for prefix, uri in namespaces]
    attrs += [
        '%s:%s="%s"' % (prefix, name, value)
        for prefix, name, value in vendor_properties(vendor, video_length, timestamp_us)
    ]

    return "\n".join(
        [
            '%s<rdf:Description %s>' % (indent, " ".join(attrs)),
            "%s <Container:Directory>" % indent,
            "%s  <rdf:Seq>" % indent,
            '%s   <rdf:li rdf:parseType="Resource">' % indent,
            '%s    <Container:Item Item:Mime="%s" Item:Semantic="%s" '
            'Item:Length="0" Item:Padding="0"/>' % (indent, IMAGE_MIME, SEMANTIC_PRIMARY),
            "%s   </rdf:li>" % indent,
            '%s   <rdf:li rdf:parseType="Resource">' % indent,
            '%s    <Container:Item Item:Mime="%s" Item:Semantic="%s" '
            'Item:Length="%d" Item:Padding="0"/>'
            % (indent, VIDEO_MIME, SEMANTIC_MOTION_PHOTO, video_length),
            "%s   </rdf:li>" % indent,
            "%s  </rdf:Seq>" % indent,
            "%s </Container:Directory>" % indent,
            "%s</rdf:Description>" % indent,
        ]
    )


def build_packet(vendor: str, video_length: int, timestamp_us: int) -> str:
    """构造完整的 XMP 包（含 xpacket 头尾）。"""
    return "\n".join(
        [
            XMP_HEADER,
            '<x:xmpmeta xmlns:x="%s" x:xmptk="MotionPhotoConverter %s">' % (NS_X, __version__),
            ' <rdf:RDF xmlns:rdf="%s">' % NS_RDF,
            build_description(vendor, video_length, timestamp_us, indent="  "),
            " </rdf:RDF>",
            "</x:xmpmeta>",
            XMP_TRAILER,
            "",
        ]
    )


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #

def _prefix_variants(prefix: str) -> List[str]:
    """某个前缀本身，以及语义等价的别名（如 ``Camera:`` 等价于 ``GCamera:``）。"""
    aliases = [alias for alias, target in PREFIX_ALIASES.items() if target == prefix]
    return sorted(set([prefix] + aliases))


def _strip_owned(text: str) -> str:
    """移除文本里由本工具负责的属性/元素（属性写法与元素写法都处理）。"""
    specs = set()
    for prefix, name in OWNED_PROPERTIES + OWNED_ELEMENTS:
        for variant in _prefix_variants(prefix):
            specs.add((variant, name))

    for prefix, name in sorted(specs):
        quoted = re.escape("%s:%s" % (prefix, name))
        # 属性写法
        text = re.sub(r'\s+' + quoted + r'\s*=\s*"[^"]*"', "", text)
        # 自闭合元素
        text = re.sub(r"<" + quoted + r"\s*/>", "", text)
        # 带内容的元素：必须连子元素一起删（Container:Directory 里是一整棵子树）
        text = re.sub(r"<" + quoted + r"\b[^>]*>.*?</" + quoted + r"\s*>", "", text, flags=re.S)
    return text


def merge_packet(existing: str, vendor: str, video_length: int, timestamp_us: int) -> str:
    """把动态照片属性合并进已有 XMP 包，保留其它元数据。"""
    close = existing.rfind("</rdf:RDF>")
    if close < 0 or "<rdf:RDF" not in existing:
        raise XmpError("已有 XMP 缺少 rdf:RDF 结构，无法合并")
    head = _strip_owned(existing[:close])
    tail = existing[close:]
    description = build_description(vendor, video_length, timestamp_us, indent="  ")
    return "%s%s\n%s" % (head, description, tail)


def strip_motion_photo(packet: str) -> str:
    """去掉动态照片属性（GCamera/Container/MicroVideo 那一套），保留其它元数据。

    苹果的实况照片不用这套字段，静止图里留着 ``GCamera:MotionPhoto="1"`` 是自相矛盾的声明。
    """
    return _strip_owned(packet)


# 只剩这些时说明包里已经没有真内容（x:xmptk 是工具标记，rdf:about 是结构属性）
_BOILERPLATE_PROPERTIES = ("x:xmptk", "rdf:about")


def has_meaningful_properties(packet: str) -> bool:
    """包里除样板属性外是否还有内容——没有的话整段 XMP 都可以不要。"""
    return any(
        key not in _BOILERPLATE_PROPERTIES for key in parse_properties(packet)
    )


# --------------------------------------------------------------------------- #
# 读取（宽松解析：同时支持属性写法与元素写法，前缀按惯例识别）
# --------------------------------------------------------------------------- #

_ATTR_RE = re.compile(r'([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')
_ELEM_RE = re.compile(r"<([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)\s*>(.*?)</\1:\2\s*>", re.S)


def _normalise(key: str) -> str:
    if ":" not in key:
        return key
    prefix, local = key.split(":", 1)
    return "%s:%s" % (PREFIX_ALIASES.get(prefix, prefix), local)


def _collect_element_properties(text: str, props: Dict[str, str]) -> None:
    """收集元素写法的属性值。

    容器元素（如 rdf:Description、rdf:li）要往里递归，否则它会把子属性的
    匹配整个吃掉——旧版脚本写出的 XMP 正是这种形态。
    """
    for match in _ELEM_RE.finditer(text):
        prefix, local, content = match.group(1), match.group(2), match.group(3)
        if _ELEM_RE.search(content):
            _collect_element_properties(content, props)
        else:
            props.setdefault(_normalise("%s:%s" % (prefix, local)), content.strip())


def parse_properties(text: str) -> Dict[str, str]:
    """提取全部 ``前缀:名称="值"`` 属性，以及元素写法的同名属性。"""
    props: Dict[str, str] = {}
    for prefix, local, value in _ATTR_RE.findall(text):
        if prefix == "xmlns":
            continue
        props.setdefault(_normalise("%s:%s" % (prefix, local)), value.strip())
    _collect_element_properties(text, props)
    return props


def _iter_element_chunks(text: str, pattern: str):
    """按 pattern 找出元素片段（支持自闭合、带子元素、无闭合三种写法）。"""
    for match in re.finditer(pattern, text):
        tag = match.group(1)
        start = match.start()
        gt = text.find(">", match.end())
        if gt < 0:
            continue
        if text[gt - 1] == "/":
            yield text[start:gt + 1]
            continue
        closing = re.search(r"</%s\s*>" % re.escape(tag), text[gt:])
        if closing:
            yield text[start:gt + 1 + closing.end()]
        else:
            yield text[start:]


def parse_items(text: str) -> List[Item]:
    """解析 Container:Directory 的各项。"""
    directory = re.search(
        r"<(?:[A-Za-z_][\w.-]*:)?Directory\b.*?</(?:[A-Za-z_][\w.-]*:)?Directory\s*>", text, re.S
    )
    block = directory.group(0) if directory else text

    chunks = list(_iter_element_chunks(block, r"<(rdf:li)\b"))
    if not chunks:
        chunks = list(_iter_element_chunks(block, r"<(?:[A-Za-z_][\w.-]*:)?(Item)\b"))
    if not chunks and directory is None:
        return []

    items: List[Item] = []
    for chunk in chunks:
        props = parse_properties(chunk)
        if not any(key.startswith("Item:") for key in props):
            continue

        def _int(name: str) -> Optional[int]:
            raw = props.get(name)
            if raw is None:
                return None
            try:
                return int(raw)
            except ValueError:
                return None

        items.append(
            Item(
                mime=props.get("Item:Mime"),
                semantic=props.get("Item:Semantic"),
                length=_int("Item:Length"),
                padding=_int("Item:Padding"),
            )
        )
    return items


def read_meta(packet: str) -> MotionPhotoMeta:
    """读取 XMP 里的动态照片信息。"""
    props = parse_properties(packet)

    def _int(key: str) -> Optional[int]:
        raw = props.get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    items = parse_items(packet)
    present = (
        props.get("GCamera:MotionPhoto") == "1"
        or props.get("GCamera:MicroVideo") == "1"
        or bool(items)
    )
    return MotionPhotoMeta(
        present=present,
        parse_ok=True,
        timestamp_us=_int("GCamera:MotionPhotoPresentationTimestampUs"),
        micro_video_offset=_int("GCamera:MicroVideoOffset"),
        items=items,
        properties=props,
    )
