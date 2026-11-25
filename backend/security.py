"""安全：随机会话令牌（可过期）+ 登录限速 + 恒定时间比较 + 路径守卫。

相对旧版：不再用 HMAC(固定盐, 密码) 这类永不过期的静态令牌。
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from backend.config import settings

_sessions: dict[str, float] = {}          # token -> 过期时间戳
_lock = threading.Lock()
_login_attempts: dict[str, deque] = defaultdict(deque)

MAX_ATTEMPTS_PER_MINUTE = 8


def _purge() -> None:
    now = time.time()
    for tk in [k for k, v in _sessions.items() if v < now]:
        _sessions.pop(tk, None)


def issue_token() -> str:
    token = secrets.token_urlsafe(32)
    ttl = max(1, settings.SESSION_TTL_HOURS) * 3600
    with _lock:
        _purge()
        _sessions[token] = time.time() + ttl
    return token


def revoke_token(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


def check_session(token: str) -> bool:
    with _lock:
        _purge()
        exp = _sessions.get(token)
        if not exp:
            return False
        # 滑动续期
        _sessions[token] = time.time() + max(1, settings.SESSION_TTL_HOURS) * 3600
        return True


def verify_password(password: str) -> bool:
    if not settings.ACCESS_PASSWORD:
        return False
    return hmac.compare_digest((password or "").encode(), settings.ACCESS_PASSWORD.encode())


def rate_limit_ok(key: str) -> bool:
    """返回 True 表示允许继续；False 表示已触发限速。"""
    now = time.time()
    with _lock:
        q = _login_attempts[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= MAX_ATTEMPTS_PER_MINUTE:
            return False
        q.append(now)
        return True


def require_access(request: Request) -> None:
    """FastAPI 依赖：密码模式启用时校验 Bearer 会话令牌。"""
    if not settings.ACCESS_PASSWORD:
        return
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not check_session(token):
        raise HTTPException(status_code=401, detail="需要登录或会话已过期")