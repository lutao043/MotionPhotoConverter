# 技术说明

面向开发者的格式细节、实现说明与验证边界。使用者请看 [README.md](README.md)。

- [产物结构](#产物结构)
- [各家实现对照](#各家实现对照)
- [真机里的「多项目」文件](#真机里的多项目文件)
- [苹果实况照片](#苹果实况照片)
- [为什么 v1 会失败](#为什么-v1-会失败)
- [校验与诊断](#校验与诊断)
- [代码结构](#代码结构)
- [测试](#测试)
- [验证边界](#验证边界)

各档位字段的依据与出处见 [README.md](README.md) 的「资料来源与鸣谢」。

## 产物结构

（下面是安卓各档位的结构。苹果档位产出的是另一套：一对同名文件，见「苹果实况照片」一节。）

```
[JPEG：SOI · APP0 · APP1(Exif) · APP2(MPF，仅 OPPO) · APP1(XMP) · … · SOS 图像数据 … · EOI]
[MP4 原样追加]
```

读取器用两条规则找视频，产物要让两条规则都落在 MP4 的 `ftyp` 起点上：

| 规则 | 使用者 | 算式 |
|---|---|---|
| 自尾回推 | Google Photos、ExoPlayer | `视频起点 = 文件总长 − 视频项 Item:Length` |
| 自头累加 | 各家手机相册 | `主图 EOI + 各项 Length + Padding` |

写入的 XMP 用属性写法，与真机一致：

```xml
<rdf:Description rdf:about=""
  xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
  xmlns:Container="http://ns.google.com/photos/1.0/container/"
  xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
  GCamera:MotionPhoto="1"
  GCamera:MotionPhotoVersion="1"
  GCamera:MotionPhotoPresentationTimestampUs="0">
  <Container:Directory>
    <rdf:Seq>
      <rdf:li rdf:parseType="Resource">
        <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"
                        Item:Length="0" Item:Padding="0"/>
      </rdf:li>
      <rdf:li rdf:parseType="Resource">
        <Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto"
                        Item:Length="<MP4 字节数>" Item:Padding="0"/>
      </rdf:li>
    </rdf:Seq>
  </Container:Directory>
</rdf:Description>
```

`Item:Padding` 一律为 0，实际也不写填充字节。XMP 在 APP1 段内有自己的长度字段，不需要占位。

## 各家实现对照

同一套 Google 标准元数据是底座，各家另有差异：

| 厂商 | 额外需要 | 说明 |
|---|---|---|
| Google / Pixel | 仅标准元数据 | Ultra HDR 照片还会带 `hdrgm:Version` 与 GainMap 项 |
| 小米 / Redmi（默认档位） | 标准 + 旧字段 `GCamera:MicroVideo / MicroVideoVersion / MicroVideoOffset / MicroVideoPresentationTimestampUs` + Exif 私有 tag `0x8897`（十进制 34967）= 1 | 反编译小米相册确认它会读 `0x8897`；`MicroVideoOffset` = 自文件末尾回数的视频字节数，与 `Item:Length` 同义，兼容仍在读旧字段的读取器 |
| OPPO / 一加 / 真我 | 标准 + `OpCamera:{MotionPhotoOwner=oplus, OLivePhotoVersion=2, MotionPhotoFeatureFlag=1, VideoLength, MotionPhotoPrimaryPresentationTimestampUs}`（命名空间 `http://ns.oplus.com/photos/1.0/camera/`）+ Exif `UserComment=Oplus_8388608` + APP2 MPF 段 | MPF（CIPA DC-007）：`NumberOfImages=1`、`MPImageType=Baseline MP Primary` |
| vivo / iQOO | 标准 + `VCamera:{VMotionPhotoVersion=1, VMotionPhotoSource=1, VMediaKitVersion="1.0.0.5"}`（命名空间 `http://ns.vivo.com/photos/1.0/camera/`，三个字段必须同时出现） | 命名空间与取值来自一份 vivo X300 Pro 真机样本 |
| 三星 | 仅标准元数据 | SEF / SEFHDR 尾部不需要 |
| 苹果 iPhone（`apple` 档位） | 与上面都不同：一对同名文件 + 两边同一个 UUID | 静止图写 Exif MakerNote 的 tag 0x0011，视频写 `moov/meta` 的 `content.identifier`，见下一节 |
| 华为鸿蒙 / 部分 OriginOS | — | 这些系统常把实况照片存成「JPG + MP4 分离」两份文件，合成格式缺少权威资料，本工具未覆盖 |

小米相关的两个额外 tag（`0x889f`、`0x9a01`）来源单一且含义不明，默认不写，需要时用
`--xiaomi-extra-exif` 打开。

`Item:Semantic` 对视频项写 `MotionPhoto`。部分旧资料写作 `Secondary`，两者在真机文件中都出现过，
读取器基本按 `Item:Mime` 判断，因此这里跟随真机的常见取值。

### 真机里的「多项目」文件

真机（vivo、Pixel 的 Ultra HDR、三星等）文件里，视频前面往往还有别的东西：

```
[主图 JPEG][GainMap JPEG][厂商私有数据][MP4]     ← vivo X300 Pro 实测结构
```

而且厂商私有数据不会被写进 `Container:Directory`（实测有 282114 字节），所以「自头累加」这条规则
在真机上并不总是成立，真正可靠的是「文件总长 − 视频项 Length」。本工具的校验器据此区分两类情况：

- 累加超出视频起点：目录与实际数据矛盾，判失败（`✗`）；
- 累加不足视频起点：视频前有未列出的厂商数据，真机常见做法，只作提示（`!`）。

本工具自己只产出「主图 + 视频」两项。如果源图已经带了 GainMap 之类的附加项（例如把手机拍的
Ultra HDR 动态照片拿来重做），这些附加项会被丢弃，并在输出时给出明确告警。

## 苹果实况照片

苹果的做法与安卓各家完全不同：不是「一个文件里塞两段数据」，而是**一对同名文件**，
两边写同一个 UUID 来配对。`--vendor apple` 产出的是 `<名字>.JPG` 与 `<名字>.MOV`，
静止图里不再追加视频，也不需要 Google 那套 XMP。

```
<名字>.JPG   [SOI · APP1(Exif，含 Apple MakerNote) · 图像数据 · EOI]      ← 尾部干净
<名字>.MOV   [ftyp · … · moov（含 moov/meta 的 mdta 键值） · …]
```

两边的标识符（配对凭据）：

| 位置 | 键 | 值 |
|---|---|---|
| 静止图 | ExifIFD 的 MakerNote（0x927C）里 tag `0x0011` | ASCII，UUID 加结尾 NUL，共 37 字节 |
| 视频 | `moov/meta`（handler 为 `mdta`）的 `keys`/`ilst` 里 `com.apple.quicktime.content.identifier` | 同一个 UUID 字符串 |

以上两条是按真机文件（见 README 鸣谢里的样本）逐字节核对的，并用 macOS 的 ImageIO 与
AVFoundation（与 iOS 同一套解析代码）回读过：ImageIO 从静止图读出
`kCGImagePropertyMakerAppleDictionary` 的 key `17`，AVFoundation 从 MOV 读出
`com.apple.quicktime.content.identifier`，两者一致。

### Apple MakerNote 的字节布局

```
偏移 0    12 字节   签名 + 版本： "Apple iOS\0\0\1"
偏移 12   14 字节   内部 TIFF 头： "MM" 00 2a 00 01 00 09 00 00 00 01 00 00
偏移 26   IFD：      条目数(2) + 条目(12×n) + 下一个 IFD 偏移(4)
之后      值区        值的偏移**相对这份 MakerNote 的起点**，不是相对外层 Exif 的 TIFF
```

几个实测细节，实现时都得照做：

- 真机 MakerNote 是 **1568 字节**。macOS 的 ImageIO 对长度不足约 1KB 的 MakerNote
  直接不解析（我们逐字节试过：960 字节读不出，968 字节就能读出），所以生成的 MakerNote
  按真机尺寸补齐到 1568 字节，否则读回来是「没有 MakerNote」。
- IFD 后面那个 4 字节字段在真机里不是 0（实测都是 `0x00200002`），读的时候不能当成
  「还有下一个 IFD」而拒绝解析。
- 源图本来就带 Apple MakerNote 时**只在原地改写那 37 字节**，长度和其余字节一个都不动；
  真机 MakerNote 里还有 40 多个嵌套结构（运行时长 bplist、变换矩阵等），重排偏移风险太大。
- 源图带的是别的厂商 MakerNote 时会整条替换成 Apple 的，并在输出里告警说明丢弃了什么。

### MOV 侧：写 moov/meta

`moov/meta` 是 QuickTime 风格（没有 ISO 那 4 字节 version/flags），子盒依次是
`hdlr`（handler_type = `mdta`，34 字节）、`keys`、`ilst`：

```
keys   version_flags(4) + 条目数(4) + 每条[ 长度(4) + 'mdta'(4) + 键名 ]
ilst   每个子盒的类型 = keys 表下标（1 基，大端 u32），内容是一个 data 原子：
        长度(4) + 'data' + 类型(4, 1 表示 UTF-8 字符串) + locale(4) + 值
```

写标识符时：已有 `moov/meta`（且是 mdta 风格）就把键追加到 keys 表末尾、在 ilst 里补一项；
没有就在 `moov` 下新建一个。若 moov 下的 meta 是 iTunes 风格（handler 为 `mdir`、
ilst 里没有 keys 表，ffmpeg 与部分安卓机型都这么写），我们不动它，另建一个 mdta 的
`moov/meta` 放在它前面；`moov/udta/meta` 里的元数据同理不动。

### 撑大 moov 之后的偏移修正

写元数据会让 `moov` 变大。若媒体数据排在 moov 之后，这些字节整体后移，所有轨道的
`stco`/`co64` 里指向位移区之后的 chunk 偏移都要加上差值，否则播放器会取到错位的数据。
规则是逐条目判断：偏移 ≥ 旧 moov 末尾的加差值，指向 moov 之前的保持不变，落在 moov
内部的判为结构异常直接报错。含绝对偏移的索引结构（`moof` 分片、`sidx`、`saio`）一律
拒绝写入，不做冒险改写。写出前自检三条：`mdat` 载荷逐字节未变、标识符能读回、
所有 chunk 偏移都落在某个 `mdat` 里。

### 真机还有、本工具第一版没写的东西

真机 MOV 里有两条额外的元数据轨道，都是 `stsd` 为 `mebx` 的元数据轨：

- `com.apple.quicktime.still-image-time`：样本值恒为 `0xFF`（即 -1），
  静止图在视频时间轴上的位置由该样本的时间给出（真机里靠这条轨道上的空 edit
  把样本推到 0.667 s 处，而**不是**改样本值）。缺这条轨道时 `--verify` 会给出
  一条 `!` 提示。
- `com.apple.quicktime.live-photo-info`：每帧 240 字节的辅助数据。

待真机验证后如果相册不认，第二步就是补 `still-image-time` 这条轨道：新建一个
`trak`（`tkhd` / `mdhd` / `hdlr`(mdta) / `minf` / `stbl`，`stsd` 为内嵌该键的 `mebx`），
样本字节追加到文件末尾新增的 `mdat` 里，`stco` 指过去，同时把 `mvhd` 的
`next_track_ID` 加一。真机样本的这段结构（`trak` 共 801 字节、`mebx` 样本描述 293 字节、
样本 105 字节）已在开发时 dump 出来存档，照抄即可。

## 为什么 v1 会失败

v1 的产物有两个层面的问题，任何一条都足以让手机相册认不出来：

1. XMP 拼在 EOI 之后，不在 APP1 段里。走 `ExifInterface` / 相册元数据解析的读取器根本看不到这些标记。
2. 视频项的 `Item:Length` 为 0：按「文件总长 − Length」定位的读取器会得到文件末尾，等于没有视频；
   同时缺少旧读取器使用的 `MicroVideoOffset`。
3. 声明了 `Item:Padding=32` 并在 XMP 之后真的写了 32 个 NUL 字节。按项长度累加的读取器会落在
   MP4 内部第 32 字节，把 MP4 头破坏掉。
4. `Item:Mime/Semantic/Length/Padding` 用的是子元素写法（`<Item:Mime>`），真机是属性写法
   （`<Container:Item Item:Mime="…"/>`）。
5. `MotionPhotoPresentationTimestampUs` 硬编码 `123456789`（123 秒），远超视频时长。写错这个值会直接
   破坏小米播放。
6. `<?xpacket begin=""` 少了必须的 U+FEFF。

v2 的做法与之相反：XMP 写进 APP1 段，`Item:Length` 等于视频字节数，`MicroVideoOffset` 同值，
不写任何填充字节，时间戳默认 0（不知道真实封面帧位置时不凭空造值），并在写出前自检。

## 校验与诊断

`--verify` 的校验项：XMP 是否在 APP1 段内、视频项 `Item:Length` 是否与实际追加字节数一致、
两条定位规则是否都指向 `ftyp`、`MicroVideoOffset` 是否一致、封面帧时间戳是否落在视频时长内、
MP4 结构（`ftyp` / `moov`）是否完整。带 `!` 的是非致命提示项。

`--vendor apple` 下 `--verify` 改成校验一对文件（给这一对里的任意一个或整个目录都行）：
两侧是否都带标识符、标识符是否一致、静止图的 MakerNote 是不是 Apple 格式且带 tag 0x0011、
所有 chunk 偏移是否都落在 `mdat` 内、视频元数据能否解析。同名但没有标识符的一对
（例如源图与源视频）按「不是实况照片」跳过，不算失败。

`--inspect` 会打印 JPEG 段表、全部 XMP 属性、Directory 各项、两条定位规则算出的偏移、
Exif 的 IFD0 / ExifIFD 条目、MP4 顶层 box。苹果档位下会换成打印这一对文件：JPEG 段表、
Apple 标识符、MakerNote 的每条记录、MOV 的顶层 box 与各轨道、`moov/meta` 的全部键值，
最后附上与 `--verify` 相同的检查清单。排查「手机不认」时，拿手机自带相机拍的实况照片跑一遍
`--inspect`，与我们的产物逐字段对照，差异一目了然。

用真机样本自测（样本见 [README.md](README.md) 的「资料来源与鸣谢」）：

```bash
python3 main.py --verify /path/to/手机拍的动态照片.jpg --vendor vivo
python3 main.py --inspect /path/to/手机拍的动态照片.jpg
```

## 代码结构

```
main.py                  CLI 入口（批量转换 / --verify / --inspect）
motionphoto/jpeg.py      JPEG 段解析与 APP1/APP2 段读写（含段长修正）
motionphoto/xmp.py       XMP 生成、合并（保留其它元数据）、宽松读取
motionphoto/mp4.py       MP4 预检：ftyp/moov/mvhd/时长/编码/分片
motionphoto/mov.py       MOV 写侧：moov/meta 读写、chunk 偏移修正（苹果档位用）
motionphoto/exif.py      厂商私有 Exif tag 与 Apple MakerNote 写入（追加 IFD + 回指，不改动原字节）
motionphoto/convert.py   组装、厂商档位、MPF 段、自检与诊断
tools/apple_probe.swift  开发用只读探测脚本：用 ImageIO / AVFoundation 回读产物（需要 Xcode，不参与工具运行）
tests/                   标准库 unittest
```

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

测试样本全部由代码现造（含带 Exif / IFD1 缩略图的最小 JPEG、带 `mvhd` / `stsd` / `stco`
的最小 MP4 与 MOV、手写的 Apple MakerNote），不依赖任何外部文件。覆盖的关键性质：

- 视频逐字节追加，主图图像数据（SOS→EOI）逐字节不变
- 两条定位规则与实际 `ftyp` 起点三者一致
- Exif 原有字节一个不动，只允许追加，方向 / MakerNote / 缩略图全部保留
- 旧版 v1 的产物必须校验失败（回归测试，锁住这个 bug）
- 重复转换幂等、重复 XMP 段被清理、坏 MP4 被拒绝
- 苹果档位：两侧标识符一致且能读回、`mdat` 载荷逐字节不变、moov 在媒体数据之前时
  chunk 偏移按差值修正且指向的字节不变、已有 mdta 元数据的其它键保留、iTunes 风格
  `moov/udta/meta` 不被改动、分片文件与无 moov 的输入被拒绝
- 苹果档位：源图已有 Apple MakerNote 时只改标识符那 37 字节（长度与其它记录不变）、
  别的厂商 MakerNote 被替换并告警、重复转换时输出逐字节不变

## 验证边界

安卓各档位的字段组合有源码、反编译或真机文件级的依据，产物结构由 `--verify` 自检与单元测试
保证，校验器也用真机样本反向验证过。但安卓产物本身没有在真机上验证过，手上没有可用于测试的机型。

苹果档位的字段与字节布局来自真机实况照片（iPhone 13 / iOS 17.7.2，见 README 鸣谢），
并用 macOS 的 ImageIO 与 AVFoundation 回读验证过产物能被苹果自己的解析器读出标识符；
**但同样没有在 iPhone 上验证过**，相册是否把它当成实况照片要以真机实测为准。已知的差异是
真机 MOV 里还有 `still-image-time` 等元数据轨道（见上文），本工具第一版没写。
另外两件事不在本工具范围内：静止图只支持 JPEG（HEIC 需要 HEIF 解析器与 HEVC 编码器，
标准库做不到），视频沿用源文件的容器品牌与编码，不重新封装。
真机识别请以你的手机实测为准，反馈时附上 `--inspect` 输出最有帮助。
