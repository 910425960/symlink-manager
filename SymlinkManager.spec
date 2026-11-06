# -*- mode: python ; coding: utf-8 -*-
# 软链接管理器 PyInstaller 打包配置（小体积优先）
# 构建命令: pyinstaller SymlinkManager.spec --noconfirm
# 产物: dist/SymlinkManager.exe（单文件，无控制台窗口）

import os
import re
import sys

# ---------------------------------------------------------------------------
# 自动收集 tkinter / ctypes 的原生 DLL 依赖链
# ---------------------------------------------------------------------------
# 背景：Anaconda/Miniconda 的 Tk9 运行时把 tcl90.dll、tcl9tk90.dll、
# libtommath.dll、zlib1.dll 放在 Library/bin，把 libffi(ffi*.dll) 也放那里；
# PyInstaller 内置 hook 对这种非标准布局收集不全，打包后会报
# "DLL load failed while importing _tkinter / _ctypes"。
# 这里从 _tkinter.pyd、_ctypes.pyd 出发，递归解析它们引用的、且确实存在于
# 当前 Python 安装目录内的 DLL，全部收集进包。
# 对官方 python.org 构建：libffi 已静态链接、Tk DLL 就在 DLLs 目录，
# hook 本身能处理；本段只会额外补充遗漏项，不会产生副作用。

_SYSTEM_DLL = {
    'kernel32.dll', 'user32.dll', 'advapi32.dll', 'ws2_32.dll', 'msvcrt.dll',
    'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll', 'ucrtbase.dll',
    'gdi32.dll', 'comdlg32.dll', 'shell32.dll', 'ole32.dll', 'oleaut32.dll',
    'imm32.dll', 'comctl32.dll', 'winmm.dll', 'uxtheme.dll', 'dwmapi.dll',
    'shlwapi.dll', 'setupapi.dll', 'crypt32.dll', 'bcrypt.dll', 'normaliz.dll',
    'version.dll', 'netapi32.dll', 'ntdll.dll', 'kernelbase.dll', 'rpcrt4.dll',
    'sechost.dll', 'gdi32full.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
}


def _candidate_dirs():
    """当前 Python 安装中可能存放原生 DLL 的目录。"""
    dirs = []
    prefixes = []
    for p in (getattr(sys, 'base_prefix', None), sys.prefix, sys.exec_prefix):
        if p and p not in prefixes:
            prefixes.append(p)
    for pre in prefixes:
        dirs.append(os.path.join(pre, 'Library', 'bin'))  # conda 布局
        dirs.append(os.path.join(pre, 'DLLs'))
        dirs.append(pre)                                   # 官方布局 DLL 在根
    return [d for d in dirs if os.path.isdir(d)]


def _build_dll_index(dirs):
    idx = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            low = fn.lower()
            if low.endswith('.dll') or low.endswith('.pyd'):
                idx.setdefault(low, os.path.join(d, fn))
    return idx


def _scanned_deps(path):
    """从 PE 文件中提取其引用的 *.dll 名称（小写集合）。"""
    try:
        with open(path, 'rb') as f:
            blob = f.read()
    except OSError:
        return set()
    return set(m.decode().lower()
               for m in re.findall(rb'[A-Za-z0-9_\-.]+\.(?:dll|DLL)', blob))


def _collect_native_dlls():
    dirs = _candidate_dirs()
    idx = _build_dll_index(dirs)
    pyver = 'python%d%d.dll' % sys.version_info[:2]

    # 递归起点：GUI 与 ctypes 的扩展模块
    seeds = []
    for mod in ('_tkinter.pyd', '_ctypes.pyd'):
        if mod in idx:
            seeds.append(idx[mod])

    collected = {}
    seen = set()
    queue = list(seeds)
    while queue:
        cur = queue.pop()
        key = os.path.basename(cur).lower()
        if key in seen:
            continue
        seen.add(key)
        for dep in _scanned_deps(cur):
            if dep in _SYSTEM_DLL or dep.startswith('api-ms-win') or dep == pyver:
                continue
            if dep in idx and dep not in seen:
                path = idx[dep]
                collected[dep] = path
                queue.append(path)
    # ffi*.dll 一并收集（部分 _ctypes 变体运行时按需加载 ffi-8.dll）
    for name, path in idx.items():
        if name.startswith('ffi') and name.endswith('.dll'):
            collected[name] = path
    return [(p, '.') for p in sorted(collected.values())]


_binaries = _collect_native_dlls()

a = Analysis(
    ['symlink_manager.py'],
    pathex=[],
    binaries=_binaries,              # 自动收集的原生 DLL 依赖链
    # 图标需要随包分发一份运行时副本：EXE(icon=...) 只写 PE 资源（文件图标），
    # tkinter 窗口的标题栏/任务栏图标由 iconbitmap() 在运行时读取实际 .ico。
    datas=[(os.path.join(SPECPATH, 'app_icon.ico'), '.')],
    hiddenimports=['_winapi'],       # Junction 降级用到（惰性导入，需显式声明）
    hookspath=[],
    runtime_hooks=[],
    # 排除与本程序无关的大型标准库/第三方模块，显著减小体积
    excludes=[
        'numpy', 'pandas', 'PIL', 'scipy', 'matplotlib',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'unittest', 'doctest', 'pdb', 'pydoc', 'xmlrpc',
        'multiprocessing', 'concurrent.futures',
        'email', 'http', 'urllib.request', 'socketserver',
        'sqlite3', 'logging.config', 'lib2to3',
        'pygments', 'IPython', 'notebook', 'setuptools',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SymlinkManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # 不启用 UPX，避免杀软误报
    console=False,                  # GUI 程序，无控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # SPECPATH 由 PyInstaller 注入（spec 所在目录），从任意工作目录构建都能定位图标
    icon=os.path.join(SPECPATH, 'app_icon.ico'),  # 6 级尺寸透明程序图标
)
