# -*- coding: utf-8 -*-
r"""DocFlow 桌面版启动器：双击 DocFlow.exe 就是一个原生窗口应用。

行为
1. 自动挑一个空闲端口，在本进程内起 uvicorn（不弹黑框、不依赖系统 Python）。
2. 用 pywebview（系统 WebView2）打开原生窗口；缺依赖或创建失败时自动回退为浏览器模式。
3. 单实例：再次双击会直接打开已有实例的窗口地址，不重复起服务。
4. 数据目录：打包后在 %LOCALAPPDATA%\DocFlow（换 exe、升级都不丢数据）；源码运行在项目 data/。
5. 关闭窗口即停止服务并干净退出。

命令行：
    DocFlow.exe                 桌面窗口（默认）
    DocFlow.exe --serve         只起服务不建窗口（后台/开机自启，用 DOCFLOW_PORT 固定端口）
    DocFlow.exe --lan           监听 0.0.0.0（首次会自动生成访问密码并打印）
    DocFlow.exe --no-window     起服务并打开系统浏览器
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

IS_FROZEN = bool(getattr(sys, "frozen", False))
WINDOW_TITLE = "DocFlow 文流 · 智能文档整理"


# ---------------- 基础工具 ----------------
def _redirect_streams() -> None:
    """窗口版没有控制台，stdout/stderr 为 None，uvicorn 会炸；接回日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from backend.config import BASE_DIR

        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / "server.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = fh
        if sys.stderr is None:
            sys.stderr = fh
    except Exception:
        class _Null:
            def write(self, *a, **k): return 0
            def flush(self): pass
        sys.stdout = sys.stderr = _Null()


class Log:
    """桌面启动日志：既打屏也落盘，方便「配置自检」失败时排障。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def __call__(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except OSError:
            pass


def free_port(preferred: int = 0) -> int:
    raw = os.environ.get("DOCFLOW_PORT", "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    if preferred and not port_busy("127.0.0.1", preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
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


def pid_alive(pid: int) -> bool:
    """跨平台存活判断。Windows 上绝不能用 os.kill(pid, 0)（会终止目标进程）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class InstanceLock:
    """单实例锁：记录 pid + port，供二次启动复用已有服务。"""

    def __init__(self, path: Path, log: Log) -> None:
        self.path = path
        self.log = log

    def read(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def existing(self) -> dict | None:
        """有实例的判断以「进程是否存活」为准：端口可能还在启动中，不能据此误判为陈旧锁。"""
        info = self.read()
        if not info:
            return None
        if pid_alive(int(info.get("pid") or 0)):
            return info
        self.path.unlink(missing_ok=True)
        return None

    def acquire(self, port: int) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"pid": os.getpid(), "port": port,
                                             "started": int(time.time())}), encoding="utf-8")
        except OSError as e:
            self.log(f"锁文件不可写（不影响启动）：{e}")

    def release(self) -> None:
        try:
            if int(self.read().get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def wait_ready(url: str, timeout: float = 90.0, log: Log | None = None) -> bool:
    """等服务端口可用：/api/system/health 返回 <500 即认为就绪（404 也算端口通了）。"""
    deadline = time.time() + timeout
    told = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/system/health", timeout=1.5) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except Exception as e:
            if not told and log:
                log(f"等待服务就绪：{type(e).__name__}: {str(e)[:80]}")
                told = True
        time.sleep(0.25)
    return False


# ---------------- 后端服务（同进程线程） ----------------
class Server:
    def __init__(self, host: str, port: int, log: Log) -> None:
        self.host, self.port, self.log = host, port, log
        self.server = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        import uvicorn

        from backend.app import app

        config = uvicorn.Config(app, host=self.host, port=self.port,
                                log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        # 关掉 uvicorn 自己的信号处理（GUI 主线程接管）
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
        self.thread.start()
        self.log(f"服务线程已启动 http://{self.host}:{self.port}")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                self.log("服务线程未在 15 秒内退出，随进程结束")


# ---------------- 桌面桥（网页 → 本机能力） ----------------
class Bridge:
    """给前端用的原生能力：选目录、在资源管理器中显示、打开外部链接、退出。

    非桌面形态（浏览器访问）时这些接口不存在，前端会自动隐藏对应按钮。
    """

    def __init__(self, log: Log) -> None:
        self.log = log

    def _win(self):
        """窗口只在需要时从 pywebview 取，绝不存成属性。

        存成属性会被 pywebview 的 js_api 反射（dir()+getattr 递归）走到
        window.native 这条 WinForms/COM 属性链上，轻则刷
        “CoreWebView2 can only be accessed from the UI thread”，
        重则在递归里无限下钻（AccessibilityObject.Bounds.Empty.Empty…）把进程挂死，
        现象就是双击 exe 后窗口“无响应”。
        """
        import webview

        return webview.windows[0] if webview.windows else None

    def info(self) -> dict:
        from backend.config import runtime_info

        return {"ok": True, **runtime_info()}

    def pick_directory(self, title: str = "选择目录", start_path: str = "") -> dict:
        try:
            import inspect

            import webview

            win = self._win()
            if not win:
                return {"ok": False, "error": "窗口未就绪"}
            folder = getattr(webview, "FileDialog", None)
            kind = folder.FOLDER if folder is not None else webview.FOLDER_DIALOG
            params = inspect.signature(win.create_file_dialog).parameters
            kwargs: dict[str, Any] = {}
            if "title" in params:            # pywebview 5.x 有，6.x 已移除，按签名传
                kwargs["title"] = title
            initial = Path(start_path) if start_path else None
            if "directory" in params and initial is not None and initial.is_dir():
                kwargs["directory"] = str(initial)   # 直接开在当前配置的目录上
            res = win.create_file_dialog(kind, **kwargs)
            return {"ok": True, "path": str(res[0]) if res else ""}
        except Exception as e:  # noqa: BLE001
            self.log(f"选择目录失败：{e}")
            return {"ok": False, "error": str(e)[:160]}

    def reveal_document(self, doc_id: int) -> dict:
        """在资源管理器中定位归档后的文件（只接受数据库里的文档 id，路径经守卫校验）。"""
        try:
            from backend.database import SessionLocal
            from backend.models import Document
            from backend.services.preview import PreviewError, resolve_path

            with SessionLocal() as db:
                doc = db.get(Document, int(doc_id))
                if not doc:
                    return {"ok": False, "error": "文档不存在"}
                try:
                    real = resolve_path(doc.current_path or doc.original_path, db)
                except PreviewError as e:
                    return {"ok": False, "error": str(e)[:160]}
            if os.name == "nt":
                import subprocess

                subprocess.Popen(["explorer", "/select,", str(real)])
            else:
                os.startfile(str(real.parent))  # type: ignore[attr-defined]
            return {"ok": True, "path": str(real)}
        except Exception as e:  # noqa: BLE001
            self.log(f"定位文件失败：{e}")
            return {"ok": False, "error": str(e)[:160]}

    def open_external(self, url: str) -> dict:
        try:
            if str(url).startswith(("http://", "https://", "file://")):
                webbrowser.open(str(url))
                return {"ok": True}
            return {"ok": False, "error": "只允许 http/https/file 链接"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:160]}

    def quit(self) -> dict:
        threading.Thread(target=self._close, daemon=True).start()
        return {"ok": True}

    def _close(self) -> None:
        try:
            win = self._win()
            if win:
                win.destroy()
        except Exception:
            os._exit(0)


# ---------------- 主流程 ----------------
def resolve_startup(argv: list[str]) -> tuple[str, int, bool, bool]:
    """返回 (host, port, lan, no_window)。"""
    lan = "--lan" in argv
    no_window = ("--no-window" in argv or "--serve" in argv
                 or os.environ.get("DOCFLOW_HEADLESS") == "1")
    port = 0
    for i, a in enumerate(argv):
        if a in ("--port", "-p") and i + 1 < len(argv) and argv[i + 1].isdigit():
            port = int(argv[i + 1])
    host = os.environ.get("DOCFLOW_HOST", "").strip()
    if not host:
        host = "0.0.0.0" if lan else "127.0.0.1"
    return host, port, lan, no_window


def ensure_lan_password(lan: bool, log: Log) -> None:
    """局域网模式下强制有访问密码（源码版 run.py 的同款保护）。"""
    from backend.config import settings

    if not lan or settings.ACCESS_PASSWORD:
        return
    pw = secrets.token_urlsafe(9)
    settings.update(ACCESS_PASSWORD=pw)
    log(f"局域网模式已自动生成访问密码：{pw}")
    log("请在登录框输入该密码；可在「设置 → 安全与网络」修改")


def main(argv: list[str]) -> int:
    host, port, lan, no_window = resolve_startup(argv)
    from backend.config import BASE_DIR

    log = Log(BASE_DIR / "logs" / "desktop.log")
    log("=" * 52)
    log(f"DocFlow 桌面版启动（frozen={IS_FROZEN}）数据目录 {BASE_DIR}")

    lock = InstanceLock(BASE_DIR / ".desktop.lock", log)
    if existing := lock.existing():
        url = f"http://127.0.0.1:{existing['port']}"
        log(f"已有实例在运行（pid={existing.get('pid')}）：{url}，直接打开")
        wait_ready(url, 20, log)
        webbrowser.open(url)
        return 0

    port = free_port(port or (8756 if not lan else 8000))
    if port_busy(host, port):
        log(f"端口 {host}:{port} 被占用，改用随机端口")
        port = free_port()
    lock.acquire(port)
    url = f"http://127.0.0.1:{port}"
    try:
        ensure_lan_password(lan, log)
        server = Server(host, port, log)
        server.start()
        if not wait_ready(url, 90, log):
            log("服务 90 秒内未就绪，请把上面的日志贴出来排查")
            log(f"排障可手动执行：{sys.executable} --serve --port {port}")
            server.stop()
            return 1
        log(f"本机访问 {url} · 局域网 http://{local_ip()}:{port}")
        if no_window:
            webbrowser.open(url)
            log("浏览器模式：Ctrl+C 退出")
            while server.thread and server.thread.is_alive():
                time.sleep(1)
            return 0
        return run_window(url, log, server)
    except KeyboardInterrupt:
        log("已中断，正在退出")
        return 0
    except Exception:
        log("未预期异常：\n" + traceback.format_exc())
        return 1
    finally:
        lock.release()


# WebView2 默认会对「被其他窗口挡住」的窗口做节流：停止合成、推迟 rAF 与定时器，
# 结果就是桌面版窗口挂在后面时界面卡在占位符、图表画不出来。
# 这是我们自己要长期挂在后台的本地工具，必须关掉这套节流。
WEBVIEW_KEEP_ACTIVE = (
    "--disable-features=CalculateNativeWinOcclusion,BackgroundingOccludedWindowsForDisplay "
    "--disable-backgrounding-occluded-windows --disable-renderer-backgrounding "
    "--disable-background-timer-throttling"
)


def apply_webview_flags() -> str:
    """合并三处参数：调用方已设的 -> 用户追加的 DOCFLOW_WEBVIEW_FLAGS -> 我们的保活参数。

    DOCFLOW_WEBVIEW_FLAGS 是给「远程桌面 / 无显卡虚拟机上窗口画不出来」这类环境留的
    逃生口，例如设成 --disable-gpu --disable-software-rasterizer。
    """
    parts = [os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip(),
             os.environ.get("DOCFLOW_WEBVIEW_FLAGS", "").strip(),
             WEBVIEW_KEEP_ACTIVE]
    merged = " ".join(x for x in parts if x)
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = merged
    return merged


WINDOW_READY_TIMEOUT = 20      # 秒：原生窗口在该时间内没有 loaded 事件即视为卡死
WINDOW_STATE = ".window-state"


def _read_window_state(_log: Log) -> dict:
    try:
        from backend.config import BASE_DIR

        raw = json.loads((BASE_DIR / WINDOW_STATE).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_window_state(log: Log, **fields) -> None:
    """记录窗口健康状态；写失败不影响启动。"""
    try:
        from backend.config import BASE_DIR

        state = {**_read_window_state(log), **fields, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        (BASE_DIR / WINDOW_STATE).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log(f"窗口状态写入失败（不影响使用）：{e}")


def shell_mode(log: Log) -> str:
    """本次用原生窗口还是浏览器界面。

    DOCFLOW_SHELL=webview|browser 强制指定；否则只要上一次窗口卡死/从未加载完成，
    本次直接进浏览器界面，避免用户反复对着一个“无响应”的窗口。
    """
    forced = os.environ.get("DOCFLOW_SHELL", "").strip().lower()
    if forced in ("webview", "browser"):
        return forced
    state = _read_window_state(log)
    if state.get("blocked") or state.get("pending"):
        log(f"上次原生窗口异常（{state.get('reason') or '未加载完成就退出'}），本次直接使用浏览器界面")
        log(f"想重试原生窗口：删除数据目录下的 {WINDOW_STATE}，或设 DOCFLOW_SHELL=webview")
        return "browser"
    return "webview"


def browser_mode(url: str, log: Log, server: Server) -> int:
    webbrowser.open(url)
    log("浏览器模式：界面异常时推荐用这个；结束 DocFlow 进程即停止服务")
    while server.thread and server.thread.is_alive():
        time.sleep(1)
    return 0


def watch_window_ready(ready: threading.Event, closed: threading.Event, log: Log, url: str,
                       timeout: float = WINDOW_READY_TIMEOUT) -> bool:
    """窗口看门狗。返回 True 表示无需处理（已加载或用户已关窗）。

    说明：最恶劣的 COM 死锁会把整个进程（含本线程）一起冻住，这里只能尽力自救；
    根因由“不把 Window 放进桥接对象 + window.expose”消除。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready.wait(0.3) or closed.is_set():
            return True
    log(f"原生窗口 {int(timeout)} 秒内没有响应，已切换到浏览器界面：{url}")
    log("若窗口反复无响应：更新显卡驱动与「Microsoft Edge WebView2 运行时」，"
        f"或删除数据目录下的 {WINDOW_STATE} 后重试")
    _write_window_state(log, pending=False, blocked=True, reason=f"{int(timeout)} 秒未加载完成")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    return False


def run_window(url: str, log: Log, server: Server) -> int:
    if shell_mode(log) == "browser":
        return browser_mode(url, log, server)
    bridge = Bridge(log)
    os.environ["DOCFLOW_DESKTOP"] = "1"
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        log(f"未安装 pywebview，回退浏览器模式：{e}")
        return browser_mode(url, log, server)
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:  # noqa: BLE001
        pass
    apply_webview_flags()
    ready = threading.Event()
    closed = threading.Event()

    def _on_loaded() -> None:
        _write_window_state(log, pending=False, blocked=False)
        if not ready.is_set():
            log("原生窗口已就绪")
        ready.set()

    try:
        # 不传 js_api：改用 window.expose() 精确登记这几个方法。
        # js_api 会触发 pywebview 对桥接对象的递归反射（见 Bridge._win 注释），
        # 一旦桥接对象能碰到 Window/native，就会把窗口拖进 COM 死锁 —— 窗口“无响应”。
        window = webview.create_window(
            WINDOW_TITLE, url, width=1440, height=900, min_size=(1000, 700), text_select=True,
        )
        window.expose(bridge.info, bridge.pick_directory, bridge.reveal_document,
                      bridge.open_external, bridge.quit)
        window.events.loaded += _on_loaded
        _write_window_state(log, pending=True, blocked=False, reason="窗口启动中")
        log("正在打开原生窗口（WebView2）…")
    except Exception as e:  # noqa: BLE001
        log(f"窗口创建失败，回退浏览器模式：{e}")
        log(traceback.format_exc())
        _write_window_state(log, pending=False, blocked=True, reason="窗口创建失败")
        return browser_mode(url, log, server)
    threading.Thread(target=watch_window_ready, args=(ready, closed, log, url),
                     name="window-watch", daemon=True).start()
    try:
        webview.start(debug=os.environ.get("DOCFLOW_DEBUG") == "1")
    except Exception as e:  # noqa: BLE001
        log(f"原生窗口启动异常，回退浏览器模式：{e}")
        log(traceback.format_exc())
        closed.set()
        _write_window_state(log, pending=False, blocked=True, reason="窗口启动异常")
        return browser_mode(url, log, server)
    closed.set()
    if ready.is_set():
        _write_window_state(log, pending=False, blocked=False)
    else:
        # 窗口没加载就被正常关掉了（例如手快误关）：只清 pending，不判定为卡死。
        # 真正需要警惕的是 pending 残留 —— 那说明进程是在窗口卡死时被强杀的。
        _write_window_state(log, pending=False)
    log("窗口已关闭，正在停止服务…")
    server.stop()
    log("已退出")
    return 0


def cli() -> list[str]:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    return argv


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    _redirect_streams()
    sys.exit(main(cli()))