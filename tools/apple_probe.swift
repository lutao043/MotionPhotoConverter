// 开发用只读探测脚本（不属于工具运行时的一部分，需要 Xcode 的命令行工具）。
//
// 用途：拿苹果自己的解析器回读产物，验证「苹果认不认我们写的字节」。
//   - 静止图走 ImageIO，读 kCGImagePropertyMakerAppleDictionary（Live Photo 标识符在 key 17）
//   - 视频走 AVFoundation，读 QuickTime metadata（com.apple.quicktime.content.identifier）
//
// 用法：
//   swift tools/apple_probe.swift 产物.JPG 产物.MOV
//   swift tools/apple_probe.swift /path/to/真机样本.HEIC /path/to/真机样本.MOV

import AVFoundation
import Foundation
import ImageIO

func probeStill(_ path: String) {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        print("\(path): ImageIO 打不开这个文件")
        return
    }
    guard let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any] else {
        print("\(path): 读不到图像属性")
        return
    }
    let makerKey = kCGImagePropertyMakerAppleDictionary as String
    let tiff = props[kCGImagePropertyTIFFDictionary as String] as? [String: Any] ?? [:]
    print("\(path): TIFF 字典 Make=\(tiff[kCGImagePropertyTIFFMake as String] ?? "-")"
          + " Model=\(tiff[kCGImagePropertyTIFFModel as String] ?? "-")"
          + " Software=\(tiff[kCGImagePropertyTIFFSoftware as String] ?? "-")")
    print("    有 Exif 字典：\(props[kCGImagePropertyExifDictionary as String] != nil)")
    if let maker = props[makerKey] as? [String: Any] {
        print("\(path): ImageIO 读到的 MakerApple 字典（\(maker.count) 项）")
        for (key, value) in maker.sorted(by: { $0.key < $1.key }) {
            print("    \(key) = \(value)")
        }
        if let identifier = maker["17"] {
            print("    → 标识符（key 17）= \(identifier)")
        } else {
            print("    → 没有 key 17：这不是 ImageIO 眼中的实况照片静止图")
        }
    } else {
        print("\(path): ImageIO 没读到 MakerApple 字典")
    }
}

func probeMovie(_ path: String) {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    print("\(path): 时长 \(CMTimeGetSeconds(asset.duration)) 秒")
    let items = asset.metadata(forFormat: .quickTimeMetadata)
    print("    QuickTime metadata（mdta 键）共 \(items.count) 项")
    for item in items {
        let name = item.identifier?.rawValue ?? "?"
        let value: String
        if let text = item.stringValue {
            value = text
        } else if let data = item.value as? Data {
            value = String(data: data, encoding: .utf8) ?? "<\(data.count) 字节>"
        } else {
            value = "<非文本>"
        }
        print("    \(name) = \(value)")
    }
    let contentIdentifier = AVMetadataItem.metadataItems(
        from: items,
        filteredByIdentifier: .quickTimeMetadataContentIdentifier
    )
    print("    → 标识符 = \(contentIdentifier.first?.stringValue ?? "（没有）")")
    let metadataTracks = asset.tracks(withMediaType: .metadata)
    print("    metadata 轨道 \(metadataTracks.count) 条")
    for track in metadataTracks {
        let formats = track.formatDescriptions.compactMap { ($0 as! CMFormatDescription).mediaSubType }
        print("        track \(track.trackID) 子类型 \(formats)")
    }
}

let paths = Array(CommandLine.arguments.dropFirst())
if paths.isEmpty {
    print("用法：swift tools/apple_probe.swift <静止图或视频>...")
    exit(2)
}
for path in paths {
    let lower = path.lowercased()
    if lower.hasSuffix(".mov") || lower.hasSuffix(".mp4") {
        probeMovie(path)
    } else {
        probeStill(path)
    }
}
