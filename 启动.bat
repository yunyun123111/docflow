@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>&1 || (echo [错误] 需要 Python 3.10+，安装时勾选 Add to PATH & pause & exit /b 1)
python run.py %*