#!/usr/bin/env python3
"""动态照片批量合成工具。

把同名的 ``.jpg`` 与 ``.mp4`` 合成为符合 Android / 小米相册规范的动态照片。
只依赖 Python 标准库。

    # 批量合成（默认小米档位）
    python3 main.py ./media/

    # 合成苹果实况照片（一对同名文件）
    python3 main.py ./media/ --vendor apple

    # 合成后删除源文件
    python3 main.py ./media/ --delete-source

    # 只校验已有产物
    python3 main.py --verify ./media/

    # 结构诊断（贴 issue 用）
    python3 main.py --inspect ./media/photo_livePhoto.jpg
"""

import argparse
import os
import sys

from motionphoto import __version__, convert, jpeg, mov, xmp

JPEG_SUFFIXES = (".jpg", ".jpeg")
VIDEO_SUFFIX = ".mp4"
# 苹果档位两种视频扩展名都认（iPhone 导出的就是 .MOV）
APPLE_VIDEO_SUFFIXES = (".mp4", ".mov")
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILED = 2


def _find_pairs(root, video_suffixes=(VIDEO_SUFFIX,)):
    """递归找出同名 JPEG + 视频组合。"""
    pairs = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        lower = {}
        for name in files:
            if name.startswith("."):
                continue
            lower.setdefault(name.lower(), name)
        for name in sorted(files):
            stem, suffix = os.path.splitext(name)
            if suffix.lower() not in JPEG_SUFFIXES:
                continue
            video = None
            for video_suffix in video_suffixes:
                video = lower.get(stem.lower() + video_suffix)
                if video is not None:
                    break
            if video is None:
                continue
            pairs.append(
                (
                    os.path.join(current, name),
                    os.path.join(current, video),
                    stem,
                )
            )
    return pairs


def _same_path(one, other):
    """两个路径是否指同一个文件。

    先按真实文件比对（能正确处理大小写不敏感的文件系统），文件不存在时退回字符串比较。
    """
    try:
        return os.path.samefile(one, other)
    except OSError:
        return os.path.abspath(one).lower() == os.path.abspath(other).lower()


def _find_sibling(path, wanted_suffixes):
    """在同目录里找同主名、扩展名在 wanted_suffixes 里的另一个文件（不区分大小写）。"""
    directory = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in sorted(names):
        other_stem, other_suffix = os.path.splitext(name)
        if other_stem.lower() != stem.lower() or other_suffix.lower() not in wanted_suffixes:
            continue
        candidate = os.path.join(directory, name)
        if not _same_path(candidate, path):
            return candidate
    return None


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _atomic_write(path, data):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _convert_pair(jpg_path, mp4_path, out_path, options, force, dry_run):
    """返回 (状态, 消息列表)。状态为 'ok' / 'skip' / 'fail'。"""
    same_as_source = _same_path(out_path, jpg_path)
    if os.path.exists(out_path) and not same_as_source and not force:
        return "skip", ["已存在同名产物，跳过（要覆盖请加 --force）：%s" % out_path]
    if dry_run:
        return "ok", ["[试运行] %s + %s -> %s" % (jpg_path, mp4_path, out_path)]

    jpeg_bytes = _read(jpg_path)
    mp4_bytes = _read(mp4_path)

    result = convert.build_motion_photo(jpeg_bytes, mp4_bytes, options)
    _atomic_write(out_path, result.data)

    messages = ["已生成：%s（视频 %d 字节，封面帧 %d µs）"
                % (out_path, result.video_length, result.timestamp_us)]
    messages.extend("提示：" + text for text in result.warnings)
    messages.append("自检通过：两条定位规则都指向 MP4 起点")
    return "ok", messages


def _convert_live_photo(jpg_path, mp4_path, photo_path, video_path, options, force, dry_run):
    """苹果档位：写出一对同名文件。返回 (状态, 消息列表) 与输出路径列表。"""
    outputs = (photo_path, video_path)
    existing = [path for path in outputs if os.path.exists(path)]
    if existing and not force:
        return "skip", ["已存在同名产物，跳过（要覆盖请加 --force）：%s" % "、".join(existing)]
    if dry_run:
        return "ok", ["[试运行] %s + %s -> %s" % (jpg_path, mp4_path, " + ".join(outputs))]

    jpeg_bytes = _read(jpg_path)
    mp4_bytes = _read(mp4_path)

    result = convert.build_live_photo(jpeg_bytes, mp4_bytes, options)
    _atomic_write(photo_path, result.photo)
    try:
        _atomic_write(video_path, result.video)
    except OSError as exc:
        # 一对文件要一起成，不能留半成品
        if os.path.exists(photo_path):
            os.remove(photo_path)
        return "fail", ["写入视频失败，已撤回静止图：%s" % exc]

    messages = [
        "已生成：%s（静止图 %d 字节）" % (photo_path, len(result.photo)),
        "        %s（视频 %d 字节）" % (video_path, len(result.video)),
        "Apple 标识符：%s" % result.identifier,
    ]
    messages.extend("提示：" + text for text in result.warnings)
    messages.append("自检通过：两侧标识符一致，chunk 偏移都落在 mdat 内")
    return "ok", messages


def cmd_convert(args):
    root = args.directory
    if not os.path.isdir(root):
        print("错误：'%s' 不是一个有效目录" % root)
        return EXIT_USAGE

    options = convert.Options(
        vendor=args.vendor or xmp.DEFAULT_VENDOR,
        timestamp_us=args.timestamp_us,
        vendor_exif=not args.no_vendor_exif,
        extra_vendor_exif=args.xiaomi_extra_exif,
        name_style=args.name_style,
    )
    apple = options.vendor == convert.APPLE_VENDOR

    pairs = _find_pairs(root, APPLE_VIDEO_SUFFIXES if apple else (VIDEO_SUFFIX,))
    if not pairs:
        if apple:
            print("没有找到同名的 .jpg + .mp4/.mov 组合，未做任何处理")
        else:
            print("没有找到同名的 .jpg + .mp4 组合，未做任何处理")
        return EXIT_OK

    print("共找到 %d 组待处理文件，厂商档位：%s" % (len(pairs), options.vendor))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    deleted = []
    for jpg_path, mp4_path, stem in pairs:
        directory = os.path.dirname(jpg_path)
        output_paths = []
        try:
            if apple:
                photo_path, video_path = convert.live_photo_names(stem, options.name_style)
                photo_path = os.path.join(directory, photo_path)
                video_path = os.path.join(directory, video_path)
                output_paths = [photo_path, video_path]
                status, messages = _convert_live_photo(
                    jpg_path, mp4_path, photo_path, video_path, options, args.force, args.dry_run
                )
            else:
                out_path = os.path.join(directory, convert.output_name(stem, options.name_style))
                output_paths = [out_path]
                status, messages = _convert_pair(
                    jpg_path, mp4_path, out_path, options, args.force, args.dry_run
                )
        except convert.ConvertError as exc:
            status, messages = "fail", ["处理失败：%s" % exc]
        except (OSError, jpeg.JpegError) as exc:
            status, messages = "fail", ["读取失败：%s" % exc]

        counts[status] += 1
        prefix = {"ok": "✅", "skip": "⏭️", "fail": "❌"}[status]
        print("\n%s %s" % (prefix, jpg_path))
        for text in messages:
            print("   " + text)

        if status == "ok" and args.delete_source and not args.dry_run:
            for source in (jpg_path, mp4_path):
                if any(_same_path(source, out) for out in output_paths):
                    print("   ⚠️ 输出与源文件同名，为安全起见未删除 %s" % source)
                    continue
                os.remove(source)
                deleted.append(source)

    print("\n完成：成功 %d，跳过 %d，失败 %d" % (counts["ok"], counts["skip"], counts["fail"]))
    if deleted:
        print("已删除 %d 个源文件" % len(deleted))
    if counts["fail"]:
        print("\n建议：拿手机自带相机拍的动态照片，用 --inspect 与产物逐字段对照，"
              "可快速看出差异（也可把输出贴到 issue 里）。")
    elif apple and counts["ok"]:
        print("\n导入提示：把这一对同名文件放在同一个文件夹里一起导入（隔空投送整个文件夹，"
              "或在「照片」里一次选中两个）。只导入其中一个不会成为实况照片。")
    return EXIT_FAILED if counts["fail"] else EXIT_OK


def _looks_like_motion_photo(data):
    """目录模式下用来跳过普通照片：EOI 之后有追加数据，或 XMP 里带动态照片标记。"""
    try:
        if jpeg.has_trailing_data(data):
            return True
    except jpeg.JpegError:
        return True
    segment = jpeg.find_app_segment(data, jpeg.MARKER_APP1, jpeg.XMP_SIGNATURE)
    if segment is None:
        return False
    packet = jpeg.segment_body(data, segment)[len(jpeg.XMP_SIGNATURE):].decode("utf-8", "replace")
    return xmp.read_meta(packet).present


def _verify_live_photo(path):
    """校验一对苹果实况照片。返回 (状态, 输出行)。状态为 'ok' / 'skip' / 'fail'。"""
    lower = path.lower()
    if lower.endswith(JPEG_SUFFIXES):
        photo_path, video_path = path, _find_sibling(path, APPLE_VIDEO_SUFFIXES)
    else:
        video_path, photo_path = path, _find_sibling(path, JPEG_SUFFIXES)
    if photo_path is None or video_path is None:
        return "skip", ["⏭️  %s：没有同名的配对文件，跳过" % path]
    try:
        photo = _read(photo_path)
        video = _read(video_path)
    except OSError as exc:
        return "fail", ["❌ %s：读取失败（%s）" % (path, exc)]
    try:
        report = convert.verify_live_photo_pair(photo, video)
    except (jpeg.JpegError, mov.MovError) as exc:
        return "fail", ["❌ %s + %s：%s" % (photo_path, video_path, exc)]
    if not report.photo_identifier and not report.video_identifier:
        # 只是同名而已（例如源图与源视频），没有标识符就不是实况照片，跳过
        return "skip", [
            "⏭️  %s + %s：这一对没有 Live Photo 标识符，跳过"
            % (os.path.basename(photo_path), os.path.basename(video_path))
        ]
    lines = [
        ("✅" if report.ok else "❌")
        + " %s + %s（标识符 %s）"
        % (os.path.basename(photo_path), os.path.basename(video_path), report.identifier)
    ]
    for check in report.checks:
        if not check.ok:
            lines.append("   %s" % check.render())
    return ("ok" if report.ok else "fail"), lines


def _cmd_verify_live_photos(args):
    if os.path.isdir(args.verify):
        targets = []
        for current, dirs, files in os.walk(args.verify):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() in JPEG_SUFFIXES:
                    targets.append(os.path.join(current, name))
    else:
        targets = [args.verify]
    if not targets:
        print("没有找到可校验的 JPEG 文件")
        return EXIT_USAGE

    failed = 0
    skipped = 0
    for path in targets:
        status, lines = _verify_live_photo(path)
        for line in lines:
            print(line)
        if status == "fail":
            failed += 1
        elif status == "skip":
            skipped += 1
    print(
        "\n共 %d 个文件，跳过 %d 个（没有配对文件），%d 个未通过"
        % (len(targets), skipped, failed)
    )
    if failed:
        print("提示：带 ! 的是非致命项（例如缺 still-image-time 轨道），不影响结论")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_verify(args):
    if args.vendor == convert.APPLE_VENDOR:
        return _cmd_verify_live_photos(args)

    directory_mode = os.path.isdir(args.verify)
    targets = []
    if directory_mode:
        for current, dirs, files in os.walk(args.verify):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() in JPEG_SUFFIXES:
                    targets.append(os.path.join(current, name))
    else:
        targets.append(args.verify)

    if not targets:
        print("没有找到可校验的 JPEG 文件")
        return EXIT_USAGE

    failed = 0
    skipped = 0
    for path in targets:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            print("❌ %s：读取失败（%s）" % (path, exc))
            failed += 1
            continue
        if directory_mode and not _looks_like_motion_photo(data):
            skipped += 1
            continue
        try:
            report = convert.verify_bytes(data, vendor=args.vendor)
        except jpeg.JpegError as exc:
            print("❌ %s：%s" % (path, exc))
            failed += 1
            continue
        print(("✅" if report.ok else "❌") + " " + path)
        for check in report.checks:
            if not check.ok:
                print("   %s" % check.render())
        if not report.ok:
            failed += 1
    print(
        "\n共 %d 个文件，跳过 %d 个（不是动态照片），%d 个未通过"
        % (len(targets), skipped, failed)
    )
    if failed:
        print("提示：带 ! 的是非致命项（该档位未要求的扩展字段），不影响结论")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_inspect(args):
    path = args.inspect
    if not os.path.isfile(path):
        print("错误：'%s' 不是文件" % path)
        return EXIT_USAGE
    if args.vendor == convert.APPLE_VENDOR:
        if path.lower().endswith(JPEG_SUFFIXES):
            photo_path, video_path = path, _find_sibling(path, APPLE_VIDEO_SUFFIXES)
        else:
            video_path, photo_path = path, _find_sibling(path, JPEG_SUFFIXES)
        if photo_path is None or video_path is None:
            print("提示：没有找到同名的配对文件，只看找到的那一个\n")
        try:
            photo = _read(photo_path) if photo_path else b""
            video = _read(video_path) if video_path else b""
        except OSError as exc:
            print("读取失败：%s" % exc)
            return EXIT_FAILED
        print(
            convert.inspect_live_photo(
                photo,
                video,
                os.path.basename(photo_path) if photo_path else "",
                os.path.basename(video_path) if video_path else "",
            )
        )
        return EXIT_OK
    print(convert.inspect_file(path))
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="把同名 .jpg + 视频合成为相册可识别的动态照片"
                    "（安卓各档位产出单个 JPEG，--vendor apple 产出苹果实况照片的一对文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 main.py ./media/\n"
               "  python3 main.py ./media/ --vendor apple       # 苹果实况照片（JPG + MOV）\n"
               "  python3 main.py ./media/ --delete-source\n"
               "  python3 main.py --verify ./media/ --vendor apple\n"
               "  python3 main.py --inspect ./media/photo_livePhoto.jpg\n",
    )
    parser.add_argument("directory", nargs="?", help="包含 .jpg 与视频的目录（递归处理）")
    parser.add_argument("-d", "--delete-source", action="store_true",
                        help="合成成功后删除源文件（默认保留）")
    parser.add_argument("--vendor", choices=convert.SUPPORTED_VENDORS, default=None,
                        help="厂商档位，转换时默认 xiaomi（标准元数据 + MicroVideo 旧字段 + Exif 0x8897）；"
                             "apple 产出苹果实况照片的一对文件（<名字>.JPG + <名字>.MOV）；"
                             "校验时不指定则跳过厂商相关的判断")
    parser.add_argument("--timestamp-us", type=int, default=None,
                        help="封面帧在视频时间轴上的位置（微秒），默认 0")
    parser.add_argument("--no-vendor-exif", action="store_true",
                        help="不写入厂商私有 Exif 标记（默认写入）")
    parser.add_argument("--xiaomi-extra-exif", action="store_true",
                        help="额外写入小米相关 tag 0x889f / 0x9a01（来源单一，默认不写）")
    parser.add_argument("--name-style", choices=convert.NAME_STYLES, default=convert.DEFAULT_NAME_STYLE,
                        help="输出命名：suffix=<名字>_livePhoto.jpg（默认）、keep=原名、mvimg=MVIMG_ 前缀；"
                             "苹果档位下分别是 <名字>_livePhoto.JPG+.MOV、<名字>.JPG+.MOV、IMG_<名字>.JPG+.MOV")
    parser.add_argument("--force", action="store_true", help="产物已存在时覆盖（默认跳过）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要处理的文件")
    parser.add_argument("--verify", metavar="路径", help="只校验已有产物（文件或目录）")
    parser.add_argument("--inspect", metavar="文件",
                        help="打印文件结构诊断；苹果档位下给这一对文件中的任意一个即可")
    parser.add_argument("--version", action="version", version="motionphoto %s" % __version__)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.inspect:
        return cmd_inspect(args)
    if args.verify:
        return cmd_verify(args)
    if not args.directory:
        parser.print_help()
        return EXIT_USAGE
    return cmd_convert(args)


if __name__ == "__main__":
    sys.exit(main())
