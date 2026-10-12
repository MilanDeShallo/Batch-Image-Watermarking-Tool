@echo off
rem Launch the freshly built executable (kept ASCII-only on purpose:
rem cmd.exe parses batch files using the console code page, so non-ASCII
rem bytes inside a .bat can be misread as command separators).
cd /d "%~dp0dist\WatermarkTool"
if not exist "*.exe" (
    echo [ERROR] No executable found in dist\WatermarkTool
    echo Run build_exe.bat first.
    pause
    exit /b 1
)
for %%f in (*.exe) do start "" "%%~ff"
