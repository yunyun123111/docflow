@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/3] 构建前端（含 pdf.js 离线中文资源）
where npm >nul 2>&1 || (echo [错误] 需要 Node.js 才能重新构建前端 & pause & exit /b 1)
pushd frontend
call npm ci || (popd & pause & exit /b 1)
call npm run build || (popd & pause & exit /b 1)
popd

echo [2/3] 安装打包依赖
python -m pip install --quiet pyinstaller pywebview

echo [3/3] PyInstaller 打包 -> dist\DocFlow\DocFlow.exe
python -m PyInstaller packaging\docflow.spec --noconfirm || (pause & exit /b 1)
echo 完成：dist\DocFlow\DocFlow.exe（整个 DocFlow 目录一起拷走即可）
pause
