"""生成图标资源文件：native/assets/icon.png（256）与 icon.ico（多尺寸）。

运行时用的是 native/app.py 里 make_icon() 现画的 QIcon；
这里导出的文件是给「桌面快捷方式 / 安装包 / 文档」用的，两边同一套绘制代码。

用法：python tools/make_icon_assets.py
"""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
from native.app import _icon_pixmap          # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "native", "assets")
os.makedirs(OUT, exist_ok=True)


def png_bytes(size: int) -> bytes:
    img = _icon_pixmap(size).toImage()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path: str, sizes) -> None:
    """手写 ICO：头 + 目录 + 各尺寸的 PNG 数据（Vista 起支持 PNG 帧）。"""
    frames = [(s, png_bytes(s)) for s in sizes]
    n = len(frames)
    offset = 6 + 16 * n
    entries, blobs = b"", b""
    for size, png in frames:
        w = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, n) + entries + blobs)


png_path = os.path.join(OUT, "icon.png")
_icon_pixmap(256).save(png_path, "PNG")
print("写出", png_path)

ico_path = os.path.join(OUT, "icon.ico")
write_ico(ico_path, [16, 24, 32, 48, 64, 128, 256])
print("写出", ico_path, os.path.getsize(ico_path), "bytes")
