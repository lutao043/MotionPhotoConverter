# 技术说明

面向开发者的格式细节、实现说明与验证边界。使用者请看 [README.md](README.md)。

- [产物结构](#产物结构)
- [各家实现对照](#各家实现对照)
- [真机里的「多项目」文件](#真机里的多项目文件)
- [为什么 v1 会失败](#为什么-v1-会失败)
- [校验与诊断](#校验与诊断)
- [代码结构](#代码结构)
- [测试](#测试)
- [验证边界](#验证边界)

各档位字段的依据与出处见 [README.md](README.md) 的「资料来源与鸣谢」。

## 产物结构

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

`--inspect` 会打印 JPEG 段表、全部 XMP 属性、Directory 各项、两条定位规则算出的偏移、
Exif 的 IFD0 / ExifIFD 条目、MP4 顶层 box。排查「手机不认」时，拿手机自带相机拍的动态照片跑一遍
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
motionphoto/exif.py      厂商私有 Exif tag 写入（追加 IFD + 回指，不改动原字节）
motionphoto/convert.py   组装、厂商档位、MPF 段、自检与诊断
tests/                   标准库 unittest
```

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

测试样本全部由代码现造（含带 Exif / IFD1 缩略图的最小 JPEG、带 `mvhd` / `stsd` 的最小 MP4），
不依赖任何外部文件。覆盖的关键性质：

- 视频逐字节追加，主图图像数据（SOS→EOI）逐字节不变
- 两条定位规则与实际 `ftyp` 起点三者一致
- Exif 原有字节一个不动，只允许追加，方向 / MakerNote / 缩略图全部保留
- 旧版 v1 的产物必须校验失败（回归测试，锁住这个 bug）
- 重复转换幂等、重复 XMP 段被清理、坏 MP4 被拒绝

## 验证边界

各档位的字段组合有源码、反编译或真机文件级的依据，产物结构由 `--verify` 自检与单元测试保证，
校验器也用真机样本反向验证过。但产物本身没有在真机上验证过，手上没有可用于测试的机型。
真机识别请以你的手机实测为准，反馈时附上 `--inspect` 输出最有帮助。
