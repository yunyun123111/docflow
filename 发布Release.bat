@echo off
chcp 65001 >nul
cd /d %~dp0
echo 发布 DocFlow 桌面版到 GitHub Releases（构建前端 + PyInstaller + 发版）
echo 想跳过 GitHub 只做本地验证，请用：powershell -ExecutionPolicy Bypass -File packaging\release.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\release.ps1 %*
pause
