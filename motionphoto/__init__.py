"""把静态 JPEG 与视频合成为相册可识别的动态照片。

安卓各档位产出单个 JPEG（XMP 写在 APP1、视频追加在 EOI 之后）；
苹果档位（``--vendor apple``）产出一对同名文件（JPG + MOV，两边写同一个标识符）。

模块划分：
    jpeg.py     JPEG 段级解析与 APP1/APP2 段读写
    xmp.py      XMP 元数据生成、合并与读取
    mp4.py      MP4 结构预检（ftyp/moov/mvhd/编码）
    mov.py      MOV 写侧（moov/meta 读写与 chunk 偏移修正）
    exif.py     Exif 私有标记与 Apple MakerNote 写入（追加 IFD 后回指，不改动原有字节）
    convert.py  组装、厂商档位、校验与结构诊断
"""

__version__ = "2.1.0"
