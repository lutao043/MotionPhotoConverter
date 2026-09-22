#!/usr/bin/env python3
"""动态照片批量合成工具。

把同名的 ``.jpg`` 与 ``.mp4`` 合成为符合 Android / 小米相册规范的动态照片。
只依赖 Python 标准库。

    # 批量合成（默认小米档位）
    python3 main.py ./media/

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

from motionphoto import __version__, convert, jpeg, xmp

JPEG_SUFFIXES = (".jpg", ".jpeg")
VIDEO_SUFFIX = ".mp4"
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILED = 2


def _find_pairs(root):
    """递归找出同名 JPEG + MP4 组合。"""
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
            video = lower.get(stem.lower() + VIDEO_SUFFIX)
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
    same_as_source = os.path.abspath(out_path) == os.path.abspath(jpg_path)
    if os.path.exists(out_path) and not same_as_source and not force:
        return "skip", ["已存在同名产物，跳过（要覆盖请加 --force）：%s" % out_path]
    if dry_run:
        return "ok", ["[试运行] %s + %s -> %s" % (jpg_path, mp4_path, out_path)]

    with open(jpg_path, "rb") as handle:
        jpeg_bytes = handle.read()
    with open(mp4_path, "rb") as handle:
        mp4_bytes = handle.read()

    result = convert.build_motion_photo(jpeg_bytes, mp4_bytes, options)
    _atomic_write(out_path, result.data)

    messages = ["已生成：%s（视频 %d 字节，封面帧 %d µs）"
                % (out_path, result.video_length, result.timestamp_us)]
    messages.extend("提示：" + text for text in result.warnings)
    messages.append("自检通过：两条定位规则都指向 MP4 起点")
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

    pairs = _find_pairs(root)
    if not pairs:
        print("没有找到同名的 .jpg + .mp4 组合，未做任何处理")
        return EXIT_OK

    print("共找到 %d 组待处理文件，厂商档位：%s" % (len(pairs), options.vendor))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    deleted = []
    for jpg_path, mp4_path, stem in pairs:
        out_path = os.path.join(os.path.dirname(jpg_path), convert.output_name(stem, options.name_style))
        try:
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
                if os.path.abspath(source) == os.path.abspath(out_path):
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


def cmd_verify(args):
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
    print(convert.inspect_file(path))
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="把同名 .jpg + .mp4 合成为 Android/小米相册可识别的动态照片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 main.py ./media/\n"
               "  python3 main.py ./media/ --delete-source\n"
               "  python3 main.py --verify ./media/\n"
               "  python3 main.py --inspect ./media/photo_livePhoto.jpg\n",
    )
    parser.add_argument("directory", nargs="?", help="包含 .jpg 与 .mp4 的目录（递归处理）")
    parser.add_argument("-d", "--delete-source", action="store_true",
                        help="合成成功后删除源文件（默认保留）")
    parser.add_argument("--vendor", choices=convert.SUPPORTED_VENDORS, default=None,
                        help="厂商档位，转换时默认 xiaomi（标准元数据 + MicroVideo 旧字段 + Exif 0x8897）；"
                             "校验时不指定则跳过厂商相关的判断")
    parser.add_argument("--timestamp-us", type=int, default=None,
                        help="封面帧在视频时间轴上的位置（微秒），默认 0")
    parser.add_argument("--no-vendor-exif", action="store_true",
                        help="不写入厂商私有 Exif 标记（默认写入）")
    parser.add_argument("--xiaomi-extra-exif", action="store_true",
                        help="额外写入小米相关 tag 0x889f / 0x9a01（来源单一，默认不写）")
    parser.add_argument("--name-style", choices=convert.NAME_STYLES, default=convert.DEFAULT_NAME_STYLE,
                        help="输出命名：suffix=<名字>_livePhoto.jpg（默认）、keep=原名、mvimg=MVIMG_ 前缀")
    parser.add_argument("--force", action="store_true", help="产物已存在时覆盖（默认跳过）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要处理的文件")
    parser.add_argument("--verify", metavar="路径", help="只校验已有产物（文件或目录）")
    parser.add_argument("--inspect", metavar="文件", help="打印文件结构诊断")
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
