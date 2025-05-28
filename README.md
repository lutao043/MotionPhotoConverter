---

# 📸 动态照片生成脚本

这是一个 Python 脚本，用于将静态 `.jpg` 图片与对应的 `.mp4` 视频文件合并成一个符合 [Android Motion Photo 格式](https://developer.android.com/media/platform/motion-photo-format) 的 JPEG 文件。该格式可在部分 Android 手机上被识别为“动态照片”，并支持播放嵌入的视频片段。

---

## 🧾 功能特性

- ✅ 支持递归遍历指定目录及其所有子目录
- ✅ 自动匹配同名的 `.jpg` 和 `.mp4` 文件（如 `photo.jpg` + `photo.mp4`）
- ✅ 生成标准格式的动态照片：`原文件名_livePhoto.jpg`
- ✅ 包含标准 XMP 元数据，兼容 Android 系统识别
- ✅ 用户可控制是否删除原始文件（通过参数控制）

---

## 📁 输入输出结构示例

```
输入目录/
├── dir1/
│   ├── photo1.jpg
│   └── photo1.mp4
├── dir2/
│   ├── photo2.jpg
│   └── photo2.mp4
└── dir3/
    ├── other.jpg
    └── unrelated.mp4
```

处理后：

```
输出目录/
├── dir1/
│   ├── photo1_livePhoto.jpg     <-- 合成后的动态照片
│   # 原始文件已被删除（如果启用 --delete-source）
├── dir2/
│   ├── photo2_livePhoto.jpg
│   # 原始文件已被删除（如果启用）
└── dir3/
    ├── other.jpg
    └── unrelated.mp4
```

---

## ⚙️ 使用方法

### 1. 安装依赖

> 本脚本使用纯 Python 原生库实现，无需额外安装第三方依赖！

```bash
pip install --upgrade pip
```

---

### 2. 运行脚本

```bash
python motion_photo.py <总目录路径> [--delete-source]
```

#### 参数说明：

| 参数 | 说明 |
|------|------|
| `<总目录路径>` | 必填，包含 `.jpg` 和 `.mp4` 文件的根目录 |
| `--delete-source` 或 `-d` | 可选，若指定则在合成后删除源文件 |

#### 示例：

```bash
# 不删除源文件
python motion_photo.py ./media/

# 删除源文件
python motion_photo.py ./media/ --delete-source
# 或者
python motion_photo.py ./media/ -d
```

---

## 📄 输出文件格式

- 文件名格式：`<原文件名>_livePhoto.jpg`
- 内容结构：
  - 静态图片（JPEG）
  - XMP 元数据（描述 Motion Photo 属性）
  - 视频数据（MP4）
- 兼容 Android 相册应用识别播放

---

## 🛠️ 开发者信息

- 使用 Python 原生模块（无第三方依赖）
- 构建了标准 XMP 元数据以确保兼容性
- 支持批量处理、递归查找、命名规范等企业级功能

---

## 📌 注意事项

- ✅ 脚本只会处理**同名**的 `.jpg` 和 `.mp4` 文件。
- ❗ 如果不加 `--delete-source`，原始文件不会被删除。
- ⚠️ 操作前建议备份重要文件，防止误删。

---


## 🤝 贡献与反馈
欢迎提交 issue 或 pull request 来帮助改进这个脚本！

---