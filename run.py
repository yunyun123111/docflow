"""DocFlow 一键启动：默认 127.0.0.1，自动开浏览器；LAN 模式需访问密码。

用法：python run.py            # 本机使用
      python run.py --lan      # 开放局域网（ACCESS_PASSWORD 为空时自动随机生成并打印）
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import threading
import time
import webbrowser


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> int:
    ap = argparse.ArgumentParser(description="DocFlow 启动器")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="监听 0.0.0.0（局域网可访问）")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    from backend.config import settings

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    if args.lan and not settings.ACCESS_PASSWORD:
        pw = secrets.token_urlsafe(9)
        settings.update(ACCESS_PASSWORD=pw)
        print(f"[DocFlow] 局域网模式自动生成访问密码: {pw}")
        print(f"[DocFlow] 打开 http://{local_ip()}:{args.port} 后在登录框输入该密码")

    if port_open("127.0.0.1", args.port):
        print(f"[DocFlow] 端口 {args.port} 已被占用，直接打开 http://127.0.0.1:{args.port}")
        webbrowser.open(f"http://127.0.0.1:{args.port}")
        return 0

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        def _open():
            for _ in range(120):
                if port_open("127.0.0.1", args.port):
                    webbrowser.open(url)
                    return
                time.sleep(0.5)
        threading.Thread(target=_open, daemon=True).start()

    print(f"[DocFlow] 启动服务: {url}  (LAN={args.lan})")
    import uvicorn

    uvicorn.run("backend.app:app", host=host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())