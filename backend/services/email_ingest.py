"""邮箱接收：IMAP 轮询 → 附件流式落盘（防重名/去重）→ 触发整理。

主题路由：`整理[分类名]: 说明` → 附件按该分类提示归档（发件人白名单内的可信来源）。
"""
from __future__ import annotations

import email
import imaplib
import threading
from datetime import datetime
from email.header import decode_header
from pathlib import Path

from backend.config import settings
from backend.services.parser import get_file_type
from backend.utils.fs import safe_filename, unique_filename
from backend.utils.logger import get_logger

logger = get_logger("email")

_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict = {"running": False, "last_poll": None, "last_new": 0, "error": ""}


def _decoded(value: str | None) -> str:
    if not value:
        return ""
    out = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                out += text.decode(enc or "utf-8", "ignore")
            except LookupError:
                out += text.decode("utf-8", "ignore")
        else:
            out += text
    return out.strip()


def _seen_file() -> Path:
    return settings.log_dir.parent / "email_seen.txt"


def _load_seen() -> set[str]:
    f = _seen_file()
    return set(f.read_text(encoding="utf-8").split()) if f.exists() else set()


def _save_seen(ids: set[str]) -> None:
    ids = set(list(ids)[-5000:])
    _seen_file().write_text("\n".join(sorted(ids)), encoding="utf-8")


def _connect() -> imaplib.IMAP4:
    m: imaplib.IMAP4 = (imaplib.IMAP4_SSL(settings.EMAIL_IMAP_HOST, settings.EMAIL_IMAP_PORT)
                       if settings.EMAIL_SSL else imaplib.IMAP4(settings.EMAIL_IMAP_HOST, settings.EMAIL_IMAP_PORT))
    m.login(settings.EMAIL_USER, settings.EMAIL_PASSWORD)
    m.select("INBOX")
    return m


def _category_hint(subject: str) -> str:
    prefix = settings.EMAIL_SUBJECT_PREFIX or "整理"
    if prefix and subject.startswith(prefix):
        rest = subject[len(prefix):]
        if rest.startswith("["):
            name, _, _ = rest[1:].partition("]")
            return name.strip()
    return ""


def _whitelisted(sender: str) -> bool:
    wl = [x.strip().lower() for x in (settings.EMAIL_SENDER_WHITELIST or "").split(",") if x.strip()]
    return (not wl) or any(w in sender.lower() for w in wl)


def _extract_attachments(msg, hint: str, saved: list[Path], hints: dict[str, str]) -> None:
    from backend.database import SessionLocal
    from backend.services.pipeline import pipeline

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fname = _decoded(part.get_filename(" "))
        if not fname:
            continue
        if get_file_type(Path(fname)) == "unknown":
            logger.info("跳过不支持的附件类型: %s", fname)
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        target = unique_filename(settings.inbox_root, safe_filename(Path(fname).stem, Path(fname).suffix))
        target.write_bytes(payload)
        saved.append(target)
        if hint:
            hints[str(target)] = hint


def poll_once() -> list[Path]:
    """拉一轮新邮件附件。异常吞掉并记录状态，线程安全。"""
    if not (settings.EMAIL_ENABLED and settings.EMAIL_IMAP_HOST and settings.EMAIL_USER):
        return []
    from backend.database import SessionLocal
    from backend.services.pipeline import pipeline

    saved: list[Path] = []
    hints: dict[str, str] = {}
    try:
        m = _connect()
        try:
            typ, data = m.search(None, "UNSEEN")
            ids = data[0].split() if typ == "OK" else []
            for msg_id in ids:
                key = f"{settings.EMAIL_USER}:{msg_id.decode()}"
                typ, msgdata = m.fetch(msg_id, "(RFC822 BODY.PEEK[])")
                if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                sender = _decoded(msg.get("From", ""))
                subject = _decoded(msg.get("Subject", ""))
                if not _whitelisted(sender):
                    logger.info("忽略白名单外发件人: %s", sender)
                    continue
                hint = _category_hint(subject)
                before = len(saved)
                _extract_attachments(msg, hint, saved, hints)
                if len(saved) == before:
                    _mark_seen(key)
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        error = ""
    except Exception as e:  # noqa: BLE001
        logger.warning("邮箱轮询失败: %s", e)
        saved, error = [], str(e)

    if saved:
        with SessionLocal() as db:
            pipeline.start_files_job(db, saved, kind="email", source="email", hints=hints)
    _status.update(last_poll=datetime.now().isoformat(timespec="seconds"),
                   last_new=len(saved), error=error)
    return saved


_seen_cache: set[str] | None = None


def _mark_seen(key: str) -> None:
    global _seen_cache
    if _seen_cache is None:
        _seen_cache = _load_seen()
    _seen_cache.add(key)
    _save_seen(_seen_cache)


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        poll_once()
        stop.wait(max(30, settings.EMAIL_POLL_INTERVAL))
    _status["running"] = False


def start() -> bool:
    global _thread, _stop
    if _thread and _thread.is_alive():
        return True
    _stop = threading.Event()
    _thread = threading.Thread(target=_loop, args=(_stop,), name="email-ingest", daemon=True)
    _status["running"] = True
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def sync_from_settings() -> bool:
    if settings.EMAIL_ENABLED and settings.EMAIL_IMAP_HOST:
        return start()
    stop()
    return False


def get_status() -> dict:
    return dict(_status)