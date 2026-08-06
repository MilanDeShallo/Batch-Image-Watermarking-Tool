@echo off
chcp 65001 >nul
echo ========================================
echo   批量图片水印工具 - 打包脚本
echo ========================================
echo.

cd /d "%~dp0"

echo [1/4] 检查依赖...
python -c "import customtkinter, tkinterdnd2, PIL" 2>nul
if errorlevel 1 (
    echo [错误] 缺少依赖，正在安装...
    pip install customtkinter tkinterdnd2 pillow pyinstaller
)

echo [2/4] 清理旧构建...
if exist "dist\WatermarkTool" rmdir /s /q "dist\WatermarkTool"
if exist "build" rmdir /s /q "build"

echo [3/4] 开始打包（可能需要几分钟）...
python -m PyInstaller build.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [错误] 打包失败！请检查上方错误信息。
    pause
    exit /b 1
)

echo [4/4] 验证打包结果...
if not exist "dist\WatermarkTool\批量图片水印工具.exe" (
    echo [错误] 未找到输出 exe 文件！
    pause
    exit /b 1
)

echo.
echo ========================================
echo   打包成功！
echo   输出目录: dist\WatermarkTool\
echo   exe 文件: dist\WatermarkTool\批量图片水印工具.exe
echo ========================================
echo.
echo   将整个 WatermarkTool 文件夹分发给用户即可。
echo.
pause
