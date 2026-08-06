# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 批量图片水印工具"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# 收集 CustomTkinter 数据文件
ctk_datas = collect_data_files('customtkinter')

# 收集 tkinterdnd2 数据文件（含 tkdnd Tcl 库）
dnd_datas = collect_data_files('tkinterdnd2')

# 合并所有数据文件
all_datas = ctk_datas + dnd_datas

# 确保 presets 目录存在
all_datas.append(('presets', 'presets'))

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=all_datas,
    hiddenimports=[
        'customtkinter',
        'tkinterdnd2',
        'tkinterdnd2.TkinterDnD',
        'PIL',
        'PIL._tkinter_finder',
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'unittest', 'test'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='批量图片水印工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # 可后续添加 icon.ico
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WatermarkTool',
)
