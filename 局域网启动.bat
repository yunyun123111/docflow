@echo off
chcp 65001 >nul
cd /d %~dp0
echo 首次运行会在 .env 里生成随机访问密码并打印，请记住它
python run.py --lan
