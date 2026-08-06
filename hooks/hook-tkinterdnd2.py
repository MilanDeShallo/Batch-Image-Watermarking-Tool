"""Hook for tkinterdnd2 — 确保 tkdnd Tcl 库被打包"""
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files('tkinterdnd2')
