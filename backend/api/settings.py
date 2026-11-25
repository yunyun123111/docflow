"""系统设置 API：读取（密钥掩码）与更新（写回 .env 并联动后台服务）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from backend.config import settings
from backend.services import selfcheck
from backend.schemas import SettingsIn

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _out() -> dict:
    return {
        "document_root": str(settings.document_root), "inbox_root": str(settings.inbox_root),
        "recycle_root": str(settings.recycle_root), "backup_root": str(settings.backup_root),
        "ledger_root": str(settings.ledger_root), "sample_root": str(settings.sample_root),
        "archive_template": settings.ARCHIVE_TEMPLATE, "rename_template": settings.RENAME_TEMPLATE,
        "auto_archive_threshold": settings.AUTO_ARCHIVE_THRESHOLD,
        "review_threshold": settings.REVIEW_THRESHOLD,
        "weights": settings.confidence_weights(),
        "allow_overwrite": settings.ALLOW_OVERWRITE,
        "recycle_retention_days": settings.RECYCLE_RETENTION_DAYS,
        "max_workers": settings.MAX_WORKERS,
        "host": settings.HOST,
        "access_password_set": bool(settings.ACCESS_PASSWORD),
        "max_upload_mb": settings.MAX_UPLOAD_MB,
        "ocr_enabled": settings.OCR_ENABLED,
        "ai": {"enabled": settings.AI_ENABLED, "base_url": settings.AI_BASE_URL,
               "model": settings.AI_MODEL, "timeout": settings.AI_TIMEOUT,
               "api_key_set": bool(settings.AI_API_KEY)},
        "watch": {"enabled": settings.WATCH_ENABLED, "dirs": [str(d) for d in settings.watch_dirs],
                  "interval": settings.WATCH_INTERVAL},
        "seal_detect_enabled": settings.SEAL_DETECT_ENABLED,
        "email": {"enabled": settings.EMAIL_ENABLED, "host": settings.EMAIL_IMAP_HOST,
                  "port": settings.EMAIL_IMAP_PORT, "ssl": settings.EMAIL_SSL,
                  "user": settings.EMAIL_USER, "password_set": bool(settings.EMAIL_PASSWORD),
                  "poll_interval": settings.EMAIL_POLL_INTERVAL,
                  "sender_whitelist": settings.EMAIL_SENDER_WHITELIST,
                  "subject_prefix": settings.EMAIL_SUBJECT_PREFIX},
        "recon": {"enabled": settings.RECON_ENABLED, "interval_minutes": settings.RECON_INTERVAL_MINUTES},
        "bundle": {"merge_suggest_enabled": settings.BUNDLE_MERGE_SUGGEST_ENABLED,
                   "amount_tolerance": settings.BUNDLE_MERGE_AMOUNT_TOLERANCE,
                   "date_window_days": settings.BUNDLE_MERGE_DATE_WINDOW_DAYS},
        "auto_backup": {"enabled": settings.AUTO_BACKUP_ENABLED, "keep": settings.AUTO_BACKUP_KEEP},
        "lan_hint": "host=0.0.0.0 时请务必设置访问密码",
    }


@router.get("")
def read():
    return _out()


@router.put("")
def update(req: SettingsIn):
    data = req.model_dump(exclude_none=True)
    kwargs: dict = {}
    simple_map = {
        "document_root": "DOCUMENT_ROOT", "inbox_root": "INBOX_ROOT",
        "archive_template": "ARCHIVE_TEMPLATE", "rename_template": "RENAME_TEMPLATE",
        "auto_archive_threshold": "AUTO_ARCHIVE_THRESHOLD", "review_threshold": "REVIEW_THRESHOLD",
        "w_rule": "W_RULE", "w_field": "W_FIELD", "w_keyword": "W_KEYWORD", "w_ai": "W_AI",
        "allow_overwrite": "ALLOW_OVERWRITE", "recycle_retention_days": "RECYCLE_RETENTION_DAYS",
        "max_workers": "MAX_WORKERS", "host": "HOST", "access_password": "ACCESS_PASSWORD",
        "ocr_enabled": "OCR_ENABLED", "ocr_max_pages": "OCR_MAX_PAGES",
        "ai_enabled": "AI_ENABLED", "ai_base_url": "AI_BASE_URL", "ai_api_key": "AI_API_KEY",
        "ai_model": "AI_MODEL", "ai_timeout": "AI_TIMEOUT",
        "watch_enabled": "WATCH_ENABLED", "watch_interval": "WATCH_INTERVAL",
        "seal_detect_enabled": "SEAL_DETECT_ENABLED",
        "email_enabled": "EMAIL_ENABLED", "email_imap_host": "EMAIL_IMAP_HOST",
        "email_imap_port": "EMAIL_IMAP_PORT", "email_ssl": "EMAIL_SSL",
        "email_user": "EMAIL_USER", "email_password": "EMAIL_PASSWORD",
        "email_poll_interval": "EMAIL_POLL_INTERVAL",
        "email_sender_whitelist": "EMAIL_SENDER_WHITELIST",
        "email_subject_prefix": "EMAIL_SUBJECT_PREFIX",
        "recon_enabled": "RECON_ENABLED", "recon_interval_minutes": "RECON_INTERVAL_MINUTES",
        "bundle_merge_suggest_enabled": "BUNDLE_MERGE_SUGGEST_ENABLED",
        "bundle_merge_amount_tolerance": "BUNDLE_MERGE_AMOUNT_TOLERANCE",
        "bundle_merge_date_window_days": "BUNDLE_MERGE_DATE_WINDOW_DAYS",
        "auto_backup_enabled": "AUTO_BACKUP_ENABLED", "auto_backup_keep": "AUTO_BACKUP_KEEP",
    }
    for k, field in simple_map.items():
        if k in data:
            kwargs[field] = data[k]
    if "watch_dirs" in data:
        kwargs["WATCH_DIRS"] = json.dumps(data["watch_dirs"], ensure_ascii=False)
    settings.update(**kwargs)

    if settings.HOST not in ("127.0.0.1", "localhost", "::1") and not settings.ACCESS_PASSWORD:
        import logging

        logging.getLogger("settings").warning("已开放局域网监听但未设置访问密码，存在未授权访问风险")

    # 联动后台服务
    from backend.services import email_ingest, reconciliation, watcher

    watcher.sync_from_settings()
    email_ingest.sync_from_settings()
    reconciliation.sync_scheduler()
    selfcheck.invalidate()
    return _out()


@router.post("/test/ai")
def test_ai():
    from backend.services.ai import ai_enabled, _chat

    if not ai_enabled():
        return {"ok": False, "detail": "AI 未启用或未配置"}
    try:
        reply = _chat("回复 OK 两个字母即可")
        return {"ok": True, "echo": reply[:40]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200]}


@router.post("/test/email")
def test_email():
    from backend.services.email_ingest import poll_once

    if not (settings.EMAIL_IMAP_HOST and settings.EMAIL_USER):
        return {"ok": False, "detail": "邮箱未配置"}
    try:
        saved = poll_once()
        return {"ok": True, "new_attachments": len(saved)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200]}