@echo off
REM Pure ASCII launcher only.
REM cmd.exe parses .bat with the active console code page; when the file
REM encoding does not match it, Chinese bytes are read as command separators.
REM All logic and messages live in build_exe.py (UTF-8 stdout there).
cd /d "%~dp0"
python build_exe.py %*
if not defined NOPAUSE pause
