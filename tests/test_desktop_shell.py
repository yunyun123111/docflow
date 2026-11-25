"""桌面壳：桥接对象必须可被 pywebview 安全反射；窗口卡死要能自动退到浏览器界面。"""
from __future__ import annotations

import sys
import threading
import types

import pytest


# ---------- pywebview 反射的等价复刻（webview/util.py: get_functions） ----------
def crawl(obj, prefix="", depth=0, seen=None, out=None):
    seen = set() if seen is None else seen
    out = [] if out is None else out
    if id(obj) in seen or depth > 6:
        return out
    seen.add(id(obj))
    for name in dir(obj):
        if name.startswith("_"):
            continue
        full = f"{prefix}.{name}" if prefix else name
        try:
            attr = getattr(obj, name)
        except Exception:      # noqa: BLE001
            continue
        if callable(attr) or isinstance(attr, (str, int, float, bool, type(None))):
            continue
        out.append(full)
        crawl(attr, full, depth + 1, seen, out)
    return out


def test_bridge_is_reflection_safe():
    import desktop_app

    bridge = desktop_app.Bridge(desktop_app.Log(None))
    assert crawl(bridge) == []


def test_crawler_detects_the_original_bug():
    """守卫本身要有效：把 Window 挂到桥上就该被爬出来（这正是“无响应”的成因）。"""

    class Native:
        @property
        def CoreWebView2(self):  # noqa: N802
            raise ValueError("can only be accessed from the UI thread")

    class Window:
        native = Native()

    class BadBridge:
        def __init__(self):
            self.window = Window()

    names = crawl(BadBridge())
    assert any("window" in n for n in names)


# ---------- run_window 走 expose，不走 js_api ----------
class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in self.handlers:
            fn()


class _Events:
    def __init__(self):
        self.loaded = _Event()
        self.closed = _Event()


class _FakeWindow:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.events = _Events()
        self.exposed: list[str] = []

    def expose(self, *fns):
        self.exposed.extend(f.__name__ for f in fns)


class _FakeServer:
    thread = None

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture()
def fake_webview(monkeypatch, tmp_path):
    created: list[_FakeWindow] = []
    mod = types.ModuleType("webview")
    mod.settings = {}
    mod.windows = []

    def create_window(*args, **kwargs):
        win = _FakeWindow(**kwargs)
        created.append(win)
        mod.windows.append(win)
        return win

    def start(*args, **kwargs):
        # 模拟真实时序：导航完成后 pywebview 注入 JS 并触发 loaded，然后窗口被关闭、start 返回
        created[0].events.loaded.fire()

    mod.create_window = create_window
    mod.start = start
    monkeypatch.setitem(sys.modules, "webview", mod)
    monkeypatch.setattr("desktop_app.apply_webview_flags", lambda: "")
    monkeypatch.setattr("backend.config.BASE_DIR", tmp_path, raising=False)
    monkeypatch.delenv("DOCFLOW_SHELL", raising=False)
    return created


def test_run_window_exposes_methods_without_js_api(fake_webview, monkeypatch):
    import desktop_app

    server = _FakeServer()
    opened = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", lambda u: opened.append(u))
    assert desktop_app.run_window("http://127.0.0.1:9", desktop_app.Log(None), server) == 0
    win = fake_webview[0]
    assert "js_api" not in win.kwargs
    assert sorted(win.exposed) == sorted(
        ["info", "pick_directory", "quit", "open_external", "reveal_document"])
    assert opened == []            # 窗口正常，不该去开浏览器
    assert server.stopped is True
    assert desktop_app._read_window_state(desktop_app.Log(None))["pending"] is False


def test_watchdog_falls_back_and_blocks_next_launch(monkeypatch, tmp_path):
    import desktop_app

    monkeypatch.setattr("backend.config.BASE_DIR", tmp_path, raising=False)
    log_lines = []
    log = desktop_app.Log(None)
    monkeypatch.setattr(log, "__call__", lambda msg: log_lines.append(msg))
    opened = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", lambda u: opened.append(u))

    ok = desktop_app.watch_window_ready(threading.Event(), threading.Event(), log,
                                        "http://127.0.0.1:9", timeout=0.2)
    assert ok is False
    assert opened == ["http://127.0.0.1:9"]
    assert desktop_app.shell_mode(log) == "browser"          # 下次启动不再赌原生窗口
    monkeypatch.setenv("DOCFLOW_SHELL", "webview")
    assert desktop_app.shell_mode(log) == "webview"          # 但保留手动重试的逃生口


def test_watchdog_stays_quiet_when_window_loads(monkeypatch, tmp_path):
    import desktop_app

    monkeypatch.setattr("backend.config.BASE_DIR", tmp_path, raising=False)
    opened = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", lambda u: opened.append(u))
    ready = threading.Event()
    ready.set()
    assert desktop_app.watch_window_ready(ready, threading.Event(), desktop_app.Log(None),
                                          "http://127.0.0.1:9", timeout=0.3) is True
    assert opened == []


def test_pending_attempt_from_previous_hang_forces_browser(monkeypatch, tmp_path):
    """上次进程被强杀（窗口卡死典型结局）→ 本次直接浏览器界面。"""
    import desktop_app

    monkeypatch.setattr("backend.config.BASE_DIR", tmp_path, raising=False)
    monkeypatch.delenv("DOCFLOW_SHELL", raising=False)
    log = desktop_app.Log(None)
    desktop_app._write_window_state(log, pending=True, blocked=False, reason="窗口启动中")
    assert desktop_app.shell_mode(log) == "browser"


# ---------- pick_directory：按已装 pywebview 的真实签名传参 ----------
class _Win6:
    """pywebview 6.x：create_file_dialog 已经没有 title 形参，传了就是 TypeError。"""

    def __init__(self, chosen):
        self.chosen = chosen
        self.calls: dict = {}

    def create_file_dialog(self, dialog_type=10, directory="", allow_multiple=False,
                           save_filename="", file_types=()):
        self.calls = {"dialog_type": dialog_type, "directory": directory}
        return self.chosen


class _Win5(_Win6):
    """pywebview 5.x：还有 title，得继续带上。"""

    def create_file_dialog(self, dialog_type=10, directory="", allow_multiple=False,
                           save_filename="", file_types=(), title=""):
        super().create_file_dialog(dialog_type, directory, allow_multiple, save_filename, file_types)
        self.calls["title"] = title
        return self.chosen


def _fake_webview(monkeypatch):
    import enum

    class FileDialog(enum.IntEnum):
        OPEN = 10
        FOLDER = 20
        SAVE = 30

    mod = types.ModuleType("webview")
    mod.FileDialog = FileDialog
    monkeypatch.setitem(sys.modules, "webview", mod)
    return FileDialog


def test_pick_directory_matches_installed_signature(monkeypatch, tmp_path):
    import desktop_app

    folder = _fake_webview(monkeypatch).FOLDER
    target = tmp_path / "归档"
    target.mkdir()

    win = _Win6([str(target)])
    bridge = desktop_app.Bridge(desktop_app.Log(None))
    monkeypatch.setattr(bridge, "_win", lambda: win)
    assert bridge.pick_directory("选择归档根目录", str(tmp_path)) == {"ok": True, "path": str(target)}
    assert win.calls["dialog_type"] == folder
    assert win.calls["directory"] == str(tmp_path)     # 直接开在当前配置的目录

    win5 = _Win5([str(target)])
    monkeypatch.setattr(bridge, "_win", lambda: win5)
    assert bridge.pick_directory("选择待整理目录", str(tmp_path))["ok"] is True
    assert win5.calls["title"] == "选择待整理目录"


def test_pick_directory_without_window_or_on_cancel(monkeypatch):
    import desktop_app

    _fake_webview(monkeypatch)
    bridge = desktop_app.Bridge(desktop_app.Log(None))
    monkeypatch.setattr(bridge, "_win", lambda: None)
    assert bridge.pick_directory()["ok"] is False

    monkeypatch.setattr(bridge, "_win", lambda: _Win6(None))   # 用户按了取消
    r = bridge.pick_directory("选择目录", "")
    assert r == {"ok": True, "path": ""}
