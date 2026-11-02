"""栅格金字塔（overview / .ovr）检测与生成。

打开大栅格前先检查有没有 overview。没有就提示用户生成；生成时通过 GDAL
的 C API 拿到真实进度，放到 QThread 里跑，主界面不会被栅格 I/O 卡住。

GDAL 来自 rasterio 自带的 DLL（rasterio.libs/gdal-*.dll），不依赖系统
安装的 GDAL，也不要求用户装 gdaladdo。
"""
from __future__ import annotations

import ctypes
import glob
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

_DLL_HANDLE = None
_GDAL_LIB = None


def has_overviews(path: str | Path) -> bool:
    """有没有内部 overview 或外部 .ovr。"""
    p = Path(path)
    if Path(str(p) + ".ovr").is_file():
        return True
    try:
        import rasterio
        with rasterio.open(p) as ds:
            return any(ds.overviews(i) for i in range(1, ds.count + 1))
    except Exception:
        return False


def overview_factors(width: int, height: int) -> list[int]:
    """给 GDAL 的 overview 层级：从 2 开始，直到缩略图小于 256 像素。"""
    out: list[int] = []
    f = 2
    max_side = max(int(width), int(height))
    while max_side // f >= 256 and f <= 64:
        out.append(f)
        f *= 2
    if not out and max_side >= 512:
        out.append(2)
    return out


def _gdal():
    """加载 rasterio 的 GDAL DLL，并返回 C API 函数。"""
    global _DLL_HANDLE, _GDAL_LIB
    if _GDAL_LIB is not None:
        return _GDAL_LIB
    if os.name != "nt":
        raise RuntimeError("当前只支持 Windows 的 rasterio GDAL DLL")
    import rasterio
    base = Path(rasterio.__file__).resolve().parent.parent / "rasterio.libs"
    dlls = sorted(glob.glob(str(base / "gdal-*.dll")))
    if not dlls:
        raise RuntimeError("找不到 rasterio 自带的 GDAL DLL")
    if hasattr(os, "add_dll_directory"):
        # 句柄必须存活到进程结束，不能让它被 GC 掉
        _DLL_HANDLE = os.add_dll_directory(str(base))
    lib = ctypes.WinDLL(dlls[0])
    lib.GDALAllRegister()
    lib.GDALOpen.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.GDALOpen.restype = ctypes.c_void_p
    lib.GDALBuildOverviews.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_void_p]
    lib.GDALBuildOverviews.restype = ctypes.c_int
    lib.GDALClose.argtypes = [ctypes.c_void_p]
    lib.GDALClose.restype = None
    _GDAL_LIB = lib
    return lib


def build_overviews(path: str | Path, factors: list[int],
                    progress: Callable[[int], None] | None = None,
                    cancel: Callable[[], bool] | None = None) -> tuple[bool, str]:
    """生成外部 .ovr（只读打开源文件，不修改原 TIFF）。

    返回 (ok, message)。取消时 GDAL 返回非 0，调用方按“已取消”处理即可。
    """
    p = Path(path)
    if not factors:
        return True, ""
    lib = _gdal()
    ds = lib.GDALOpen(str(p).encode("utf-8"), 0)  # GA_ReadOnly -> 外部 .ovr
    if not ds:
        return False, "GDAL 无法打开栅格文件"
    cb_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_double,
                               ctypes.c_char_p, ctypes.c_void_p)

    def _progress(done, _msg, _arg):
        if progress is not None:
            try:
                progress(max(0, min(100, int(round(float(done) * 100.0)))))
            except Exception:
                pass
        if cancel is not None:
            try:
                if cancel():
                    return 0
            except Exception:
                pass
        return 1

    cb = cb_type(_progress)
    arr = (ctypes.c_int * len(factors))(*[int(f) for f in factors])
    try:
        rc = lib.GDALBuildOverviews(
            ds, b"AVERAGE", len(factors), arr, 0, None, cb, None)
    finally:
        lib.GDALClose(ds)
    if rc != 0:
        # 取消/失败留下半成品，删掉，避免下次误判已有金字塔
        try:
            Path(str(p) + ".ovr").unlink(missing_ok=True)
        except Exception:
            pass
        return False, "已取消" if (cancel is not None and cancel()) else "GDAL 生成金字塔失败"
    return True, ""


class PyramidWorker(QThread):
    """后台生成 .ovr，进度回主线程。"""

    progress = Signal(int)
    done = Signal(bool, str)

    def __init__(self, path: str | Path, factors: list[int], parent=None):
        super().__init__(parent)
        self._path = str(path)
        self._factors = list(factors)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            ok, msg = build_overviews(self._path, self._factors,
                                      progress=self.progress.emit,
                                      cancel=lambda: self._cancel)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        self.done.emit(ok, msg)
