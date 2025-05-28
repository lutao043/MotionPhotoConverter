import os
import sys


# 构建标准 XMP 元数据
def build_xmp_metadata():
    xmp_data = '''<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkcK"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.6-c015 79.160557, 2020/01/01-00:00:00">
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns:dc="http://purl.org/dc/elements/1.1/"
             xmlns:Camera="http://ns.google.com/photos/1.0/camera/"
             xmlns:Container="http://ns.google.com/photos/1.0/container/"
             xmlns:Item="http://ns.google.com/photos/1.0/container/item/">
        <rdf:Description>
            <Camera:MotionPhoto>1</Camera:MotionPhoto>
            <Camera:MotionPhotoVersion>1</Camera:MotionPhotoVersion>
            <Camera:MotionPhotoPresentationTimestampUs>123456789</Camera:MotionPhotoPresentationTimestampUs>
            <Container:Directory>
                <rdf:Seq>
                    <rdf:li rdf:parseType="Resource">
                        <Item:Mime>image/jpeg</Item:Mime>
                        <Item:Semantic>Primary</Item:Semantic>
                        <Item:Length>0</Item:Length>
                        <Item:Padding>32</Item:Padding>
                    </rdf:li>
                    <rdf:li rdf:parseType="Resource">
                        <Item:Mime>video/mp4</Item:Mime>
                        <Item:Semantic>Secondary</Item:Semantic>
                    </rdf:li>
                </rdf:Seq>
            </Container:Directory>
        </rdf:Description>
    </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
'''
    return xmp_data.encode('utf-8')


# 创建单个动态照片
def create_motion_photo(jpeg_path, mp4_path, output_path, delete_source):
    try:
        with open(jpeg_path, 'rb') as f:
            jpeg_data = f.read()

        with open(mp4_path, 'rb') as f:
            mp4_data = f.read()

        xmp_data = build_xmp_metadata()
        padding = b'\x00' * 32  # 推荐值

        with open(output_path, 'wb') as out:
            out.write(jpeg_data)
            out.write(xmp_data)
            out.write(padding)
            out.write(mp4_data)

        print(f"✅ 已生成动态照片: {output_path}")

        if delete_source:
            os.remove(jpeg_path)
            os.remove(mp4_path)
            print(f"🗑️ 已删除源文件: {jpeg_path}, {mp4_path}")

    except Exception as e:
        print(f"❌ 处理失败 {jpeg_path} + {mp4_path}: {e}")


# 遍历目录，批量处理
def batch_process(root_dir, delete_source):
    for root, dirs, files in os.walk(root_dir):
        jpg_files = [f for f in files if f.lower().endswith('.jpg')]
        mp4_files = set(f for f in files if f.lower().endswith('.mp4'))

        for jpg in jpg_files:
            base_name = os.path.splitext(jpg)[0]
            mp4_candidate = base_name + '.mp4'

            if mp4_candidate in mp4_files:
                jpg_path = os.path.join(root, jpg)
                mp4_path = os.path.join(root, mp4_candidate)
                output_name = f"{base_name}_livePhoto.jpg"
                output_path = os.path.join(root, output_name)

                create_motion_photo(jpg_path, mp4_path, output_path, delete_source)


# 主函数
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python motion_photo.py <总目录路径> [--delete-source]")
        sys.exit(1)

    root_directory = sys.argv[1]
    delete_source = '--delete-source' in sys.argv or '-d' in sys.argv

    if not os.path.isdir(root_directory):
        print(f"❌ 错误：'{root_directory}' 不是一个有效目录")
        sys.exit(1)

    batch_process(root_directory, delete_source)
