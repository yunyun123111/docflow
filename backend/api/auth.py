"""认证：状态查询 / 登录（限速 + 随机会话令牌）/ 登出。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.config import settings
from backend.schemas import LoginRequest
from backend.security import check_session, issue_token, rate_limit_ok, revoke_token, verify_password
from backend.utils.logger import get_logger

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger("api.auth")


@router.get("/status")
def auth_status():
    return {"password_required": bool(settings.ACCESS_PASSWORD)}


@router.post("/login")
def login(req: LoginRequest, request: Request):
    client = request.client.host if request.client else "unknown"
    if not settings.ACCESS_PASSWORD:
        return {"ok": True, "token": None}
    if not rate_limit_ok(f"login:{client}"):
        raise HTTPException(status_code=429, detail="尝试过于频繁，请 1 分钟后再试")
    if not verify_password(req.password):
        logger.warning("登录失败 from %s", client)
        raise HTTPException(status_code=401, detail="密码错误")
    logger.info("登录成功 from %s", client)
    return {"ok": True, "token": issue_token()}


@router.post("/logout")
def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        revoke_token(auth[7:])
    return {"ok": True}