"""把静态 JPEG 与 MP4 合成为 Android/小米等机型可识别的动态照片。

模块划分：
    jpeg.py     JPEG 段级解析与 APP1/APP2 段读写
    xmp.py      XMP 元数据生成、合并与读取
    mp4.py      MP4 结构预检（ftyp/moov/mvhd/编码）
    exif.py     Exif 私有标记写入（追加 IFD 后回指，不改动原有字节）
    convert.py  组装、厂商档位、校验与结构诊断
"""

__version__ = "2.0.0"
