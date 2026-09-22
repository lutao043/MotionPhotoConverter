# 动态照片合成工具

把一张静态照片（`.jpg` / `.jpeg`）和一段视频（`.mp4`）合成一张动态照片：在手机相册里它还是那张照片，
长按或点开「动态」就播放视频。小米管这个叫动态照片，OPPO 叫实况照片，Google 叫 Motion Photo。

适合这些情况：

- 相机或第三方 App 导出的照片和视频是分开的两个文件，想让它们变回相册里能播放动效的一张照片；
- 从 iPhone 导出实况照片之后只剩下静态图和一段视频。

不需要联网，也不用装第三方库，系统里有 Python 3.7 或更高版本就能跑。

## 快速开始

装 Python 3.7 或更高版本。macOS 和 Linux 一般自带；Windows 到 python.org 下载安装，安装时勾上
「Add Python to PATH」。

拿到本工具：

```bash
git clone https://github.com/lutao043/MotionPhotoConverter.git
cd MotionPhotoConverter
```

不熟悉 git 的话，也可以在 GitHub 页面点 Code → Download ZIP，解压后在终端进入该目录。

把要合成的照片和视频放进同一个文件夹，改成相同的主文件名。扩展名大小写不敏感，`.jpeg` 和 `.jpg` 都认：

```
我的照片/
├── 001.jpg
├── 001.mp4      ← 与 001.jpg 同名，会被合成
├── 002.jpeg
├── 002.mp4      ← .jpeg 一样认
├── 003.jpg      ← 没有同名的 003.mp4，保持原样不动
└── 假期/         ← 子文件夹里的文件也会一起处理
    ├── 004.jpg
    └── 004.mp4
```

运行：

```bash
python3 main.py /path/to/我的照片
```

照片旁边会出现 `001_livePhoto.jpg`，这就是合成好的动态照片。原始的照片和视频默认保留，不会被删也不会被改。

## 常用命令

```bash
python3 main.py ./media/                       # 批量合成（默认按小米的规范写）
python3 main.py ./media/ --vendor vivo         # 按 vivo / iQOO 手机的要求写
python3 main.py ./media/ --dry-run             # 先看看会处理哪些文件，不实际生成
python3 main.py ./media/ --delete-source       # 合成成功后删掉原来的 jpg 和 mp4
python3 main.py --verify ./media/              # 检查已经合成好的文件是否规范
python3 main.py --inspect ./001_livePhoto.jpg  # 打印文件内部结构（反馈问题时附上它）
```

## 全部选项

| 选项 | 作用 |
|------|------|
| `--vendor 档位` | 按哪家手机的规范来写，见下一节。默认 `xiaomi` |
| `--name-style 风格` | 输出文件名：`suffix`＝`<原名>_livePhoto.jpg`（默认）、`keep`＝沿用原名（会覆盖原图）、`mvimg`＝`MVIMG_<原名>.jpg` |
| `-d` / `--delete-source` | 合成成功后删除源文件（默认保留）。输出与源文件同名时出于安全不会删除 |
| `--force` | 目标文件已存在时覆盖，默认跳过已存在的文件 |
| `--dry-run` | 只列出将要处理的文件，不生成、不删除 |
| `--timestamp-us 微秒数` | 指定封面帧在视频时间轴上的位置，默认 0（视频开头）。不知道填什么就用默认值 |
| `--no-vendor-exif` | 不写入厂商私有的 Exif 标记（默认写入，建议保持默认） |
| `--xiaomi-extra-exif` | 额外写入两个小米相关标记 `0x889f` / `0x9a01`（来源单一、含义不明，默认不写） |
| `--verify 路径` | 只检查已有文件是否规范，可以是单个文件或整个目录 |
| `--inspect 文件` | 打印 JPEG 段、元数据、视频偏移等结构信息，排查问题用 |
| `--version` | 显示版本号 |

## 我该选哪个档位

各档位写的都是同一套 Google 标准元数据，区别只在各家手机额外要求的字段，按你的手机品牌选就行：

| 你的手机 | `--vendor` | 说明 |
|---|---|---|
| 小米 / Redmi | `xiaomi`（默认） | 额外写小米相册要看的老字段与一个 Exif 标记 |
| Google Pixel | `google` | 只写标准字段 |
| 三星 | `samsung` | 只写标准字段 |
| OPPO / 一加 / 真我 | `oppo` | 额外写 OPPO 的字段与图片索引段 |
| vivo / iQOO | `vivo` | 额外写 vivo 的字段 |
| 华为鸿蒙 / 部分 OriginOS | — | 暂不支持：这些系统常把实况照片存成独立的 JPG + MP4 两个文件，缺少可靠的合成格式资料 |

拿不准就先用默认值试一次。手机认不出来，再换成对应品牌的档位重做。

## 合成结果是什么样的

- 输出放在源文件同一个文件夹里，默认叫 `<原名>_livePhoto.jpg`。
- 源文件默认全部保留，只有加了 `--delete-source` 才会删。注意 `--name-style keep` 会让输出直接覆盖原来的那张 jpg，不想动原图就用默认的 `suffix`。
- 照片的像素数据一个字节都不改，视频原样追加，原有的拍摄时间、方向、GPS、缩略图这些 Exif 信息也全部保留，画质和拍摄信息不会因为合成而丢失。
- 每张图都是先写到临时文件、确认无误后再替换。合成失败会直接报错，不会留下半成品。
- 已经合成过的文件再跑一次会跳过，加 `--force` 才会覆盖重做。
- 源图本身已经是动态照片，会先剥掉尾部原有的视频再重新拼接；源图里带 GainMap 这类附加画面（比如手机拍的 Ultra HDR 照片）会被丢弃。这两种情况都会给出提示。

## 怎么确认合成成功

工具写出文件之前会自检一遍：视频项长度与实际追加的字节数对不对、两条定位视频的算法是不是都落在视频起点上。
自检不通过就直接报错、不产出文件，所以能生成出来的文件结构都是对的。

想再确认已经生成的文件：

```bash
python3 main.py --verify ./media/001_livePhoto.jpg     # 单个文件
python3 main.py --verify ./media/                      # 整个目录（不是动态照片的文件自动跳过）
python3 main.py --verify ./media/ --vendor xiaomi      # 顺带检查小米档位要求的字段
```

校验会检查元数据位置、视频长度与实际字节数是否一致、封面帧时间戳是否落在视频时长内、视频文件本身是否完整。
带 `!` 的是非致命提示，不影响结论。

想看清楚文件内部到底写了什么：

```bash
python3 main.py --inspect ./media/001_livePhoto.jpg
```

## 手机认不出来怎么办

按下面的顺序排查，绝大多数情况是前两条：

1. 别用微信或 QQ 传。它们会重新压缩图片，挂在尾部的视频被整段丢掉，动效必然消失。要用微信发就得勾「原图」或以「文件」形式发送，即便如此，对方手机上的微信也未必认动态照片。传文件请走数据线、互传、AirDrop 这类无损方式。
2. 相册有缓存。同一个路径或文件名以前显示的是静态照片的话，系统可能还在用旧结果。换个文件名，或者在相册里让它重新扫描、清一下缓存再试。
3. 档位和手机品牌不匹配，用 `--vendor` 换成对应品牌重做一次。
4. 部分机型或 ROM 对「第三方动态照片」有限制（小米相册里对应 `motionPhotoThirdParty` / `isPlayableMotionPhoto`），结构完全正确的文件也可能被这个开关挡住。
5. 源视频要完整。缺 `moov` 的 MP4 没法合成，工具会直接报错。
6. 拿手机自带相机拍的动态照片对照一下：两个文件各跑一次 `--inspect`，字段差在哪看得出来。
7. 不同 App 的判定规则不一样。Google Photos 和各家相册找视频的方式不同，同一个文件在一个 App 里能播、在另一个里不播是可能的。工具保证两种规则都成立，App 自身的兼容行为不在控制范围内。

上面都试过还是不认，用[「手机认不出来」模板](https://github.com/lutao043/MotionPhotoConverter/issues/new?template=1-phone-not-playing.yml)
提个 issue，表单会告诉你要附哪些信息。

## 反馈问题

反馈走 [issues](https://github.com/lutao043/MotionPhotoConverter/issues/new/choose)，点「新建 issue」会让你挑模板，
照着表单填就行，里面问的就是定位问题需要的信息：

| 模板 | 什么时候用 |
|---|---|
| [手机认不出来 / 不动](https://github.com/lutao043/MotionPhotoConverter/issues/new?template=1-phone-not-playing.yml) | 合成好了，但相册里看不到动效 |
| [转换失败 / 报错](https://github.com/lutao043/MotionPhotoConverter/issues/new?template=2-convert-failed.yml) | 运行时报错、文件没生成、`--verify` 不通过 |
| [使用提问](https://github.com/lutao043/MotionPhotoConverter/issues/new?template=3-question.yml) | 不知道怎么用、想确认某个行为、想要新功能 |

最关键的一项是 `--inspect` 的输出（把文件名换成你的产物）：

```bash
python3 main.py --inspect 你的产物.jpg     # 整段复制进表单
```

Windows 上同样是这条命令。另外请附上手机型号与系统版本，例如「小米 15 / HyperOS 3」。

## 资料来源与鸣谢

各家的字段与布局结论来自下面这些公开项目与真机文件分析，在此致谢。本仓库不包含这些项目的代码，
只参考了它们的结论，并用其中一份样本做过验证：

- [lucky0523/lossless-motionphoto-mux](https://github.com/lucky0523/lossless-motionphoto-mux)：
  提供 vivo / OPPO / 小米 / 三星 / Pixel 真机文件的字节级分析与「两条定位规则」判据，
  vivo 档位的命名空间与取值依据的是该项目公开的 vivo X300 Pro 真机样本
  （`samples/vivo_sample.jpg`，由本仓库作者取得用于本地验证，未随本仓库分发）。
- [Young-Spark/oppo-live-photo-maker](https://github.com/Young-Spark/oppo-live-photo-maker)：
  OPPO 的字段要求，以及「元数据必须写在 JPEG 的 APP1 段内」这一结论。
- [ZhiQiu-Kinsey/AppleLivePhotoConvert](https://github.com/ZhiQiu-Kinsey/AppleLivePhotoConvert)：
  反编译小米相册确认它读取哪个 Exif 标记，以及封面帧时间戳写错会破坏小米播放。
- [flashlab/motion-live-photo](https://github.com/flashlab/motion-live-photo)：多厂商读取逻辑。
- [AssassinJY/live_motion_photos_convert](https://github.com/AssassinJY/live_motion_photos_convert)：
  小米 HyperOS 3 上实测可用的字段组合。
- Android 官方说明与 ExifTool 的 `XMP-GCamera` / `XMP-GContainer` 标签定义：
  标准元数据各字段的语义。

## 面向开发者

字节级结构、各厂商字段对照表、v1 版本失败的原因复盘、代码结构与测试情况，都整理在
[TECHNICAL.md](TECHNICAL.md)。

本仓库以 Apache-2.0 许可发布，详见 [LICENSE](LICENSE)。
