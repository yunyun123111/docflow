"""DocFlow 配置：环境变量 / .env 驱动，默认只监听 127.0.0.1。

安全默认值（相对旧版的关键改进）：
- HOST 默认 127.0.0.1；改 0.0.0.0 时若未设置访问密码会在日志中告警。
- 上传流式写盘 + 单文件大小上限；zip 解包防穿越。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 运行形态：源码（python run.py）或打包桌面版（DocFlow.exe / PyInstaller）
IS_FROZEN = bool(getattr(sys, 'frozen', False))
# RES_DIR：只读资源（frontend/dist、内置依赖），打包后为 _internal 目录
RES_DIR = Path(str(getattr(sys, '_MEIPASS', str(Path(__file__).resolve().parent.parent))))
# BASE_DIR：可写数据根。打包后落在 %LOCALAPPDATA%\DocFlow，换 exe 不丢数据、不需要管理员权限
_HOME = RES_DIR if not IS_FROZEN else Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or str(Path.home())) / 'DocFlow'
BASE_DIR = Path(os.environ.get('DOCFLOW_HOME') or str(_HOME))
VERSION = '2.3.3'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "DocFlow"
    DEBUG: bool = False

    # 网络与安全
    HOST: str = "127.0.0.1"
    ACCESS_PASSWORD: str = ""
    SESSION_TTL_HOURS: int = 168
    MAX_UPLOAD_MB: int = 200

    # 数据库与目录
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"
    DOCUMENT_ROOT: str = str(BASE_DIR / "data" / "archive")      # 归档区
    INBOX_ROOT: str = str(BASE_DIR / "data" / "inbox")           # 待整理
    RECYCLE_ROOT: str = str(BASE_DIR / "data" / "recycle")       # 回收站
    TEMP_DIR: str = str(BASE_DIR / "data" / "temp")
    BACKUP_ROOT: str = str(BASE_DIR / "data" / "backup")
    LEDGER_ROOT: str = str(BASE_DIR / "data" / "ledgers")        # 台账/业务档案
    SAMPLE_ROOT: str = str(BASE_DIR / "data" / "samples")
    LOG_DIR: str = str(BASE_DIR / "data" / "logs")
    LOG_LEVEL: str = "INFO"

    # 归档结构与重命名
    ARCHIVE_TEMPLATE: str = "{category}/{year}/{month}"           # 段缺失自动跳过；支持 {field:公司名称} 等
    RENAME_TEMPLATE: str = "{date}{underscore}{contract_no}{underscore}{title}"
    ALLOW_OVERWRITE: bool = False
    RECYCLE_RETENTION_DAYS: int = 30                              # 0 = 永不自动清理

    # 置信度
    AUTO_ARCHIVE_THRESHOLD: float = 0.85
    REVIEW_THRESHOLD: float = 0.60
    W_RULE: float = 0.40
    W_FIELD: float = 0.20
    W_KEYWORD: float = 0.15
    W_AI: float = 0.25

    # 处理
    MAX_WORKERS: int = 4
    MIN_FILE_AGE_SECONDS: int = 3                                 # 文件稳定性门槛（监控/上传共用）
    WATCH_ENABLED: bool = False
    WATCH_DIRS: str = "[]"                                        # JSON 数组
    WATCH_INTERVAL: int = 30
    SEAL_DETECT_ENABLED: bool = True

    # OCR
    OCR_ENABLED: bool = True
    OCR_MAX_PAGES: int = 20

    # AI（OpenAI 兼容）
    AI_ENABLED: bool = False
    AI_BASE_URL: str = ""
    AI_API_KEY: str = ""
    AI_MODEL: str = ""
    AI_TIMEOUT: int = 60
    AI_MAX_RETRIES: int = 2
    AI_MAX_TEXT_LENGTH: int = 1500

    # 邮箱接收
    EMAIL_ENABLED: bool = False
    EMAIL_IMAP_HOST: str = ""
    EMAIL_IMAP_PORT: int = 993
    EMAIL_SSL: bool = True
    EMAIL_USER: str = ""
    EMAIL_PASSWORD: str = ""
    EMAIL_POLL_INTERVAL: int = 120
    EMAIL_SENDER_WHITELIST: str = ""      # 逗号分隔；空=不限
    EMAIL_SUBJECT_PREFIX: str = ""        # 仅处理该前缀主题（可选），前缀后第一段冒号内容用作分类提示

    # 对账巡检
    RECON_ENABLED: bool = False
    RECON_INTERVAL_MINUTES: int = 360

    # 文档归集：采购/销售合并建议的阈值
    # 本公司名称：区分「我方 / 对方」的唯一可靠依据。留空时只能按文中出现顺序猜，
    # 业务包的客户与我方主体可能同值，合并建议也比不出是不是同一家公司。
    OUR_COMPANY: str = ""
    BUNDLE_MERGE_SUGGEST_ENABLED: bool = True
    BUNDLE_MERGE_AMOUNT_TOLERANCE: float = 0.05     # 相对差 ≤ 5%
    BUNDLE_MERGE_DATE_WINDOW_DAYS: int = 30         # 签订日期相距 ≤ 30 天

    # 自动备份
    AUTO_BACKUP_ENABLED: bool = True
    AUTO_BACKUP_KEEP: int = 7

    @property
    def document_root(self) -> Path: return Path(self.DOCUMENT_ROOT)
    @property
    def inbox_root(self) -> Path: return Path(self.INBOX_ROOT)
    @property
    def recycle_root(self) -> Path: return Path(self.RECYCLE_ROOT)
    @property
    def temp_dir(self) -> Path: return Path(self.TEMP_DIR)
    @property
    def backup_root(self) -> Path: return Path(self.BACKUP_ROOT)
    @property
    def ledger_root(self) -> Path: return Path(self.LEDGER_ROOT)
    @property
    def sample_root(self) -> Path: return Path(self.SAMPLE_ROOT)
    @property
    def log_dir(self) -> Path: return Path(self.LOG_DIR)
    @property
    def watch_dirs(self) -> list[Path]:
        import json
        try:
            raw = json.loads(self.WATCH_DIRS or "[]")
        except (ValueError, TypeError):
            raw = []
        return [Path(str(x).strip()) for x in raw if isinstance(x, str) and str(x).strip()]

    def ensure_dirs(self) -> None:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        for p in (self.document_root, self.inbox_root, self.recycle_root, self.temp_dir,
                  self.backup_root, self.ledger_root, self.sample_root, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)

    def confidence_weights(self) -> dict[str, float]:
        return {"rule": self.W_RULE, "field": self.W_FIELD, "keyword": self.W_KEYWORD, "ai": self.W_AI}

    def update(self, **kwargs) -> None:
        """更新内存配置并持久化到 .env（仅写入显式传入的键，保留注释）。"""
        data = {k.upper(): v for k, v in kwargs.items() if v is not None and hasattr(self, k.upper())}
        if not data:
            return
        for k, v in data.items():
            object.__setattr__(self, k, v)
        self.ensure_dirs()
        env = BASE_DIR / ".env"
        lines: list[str] = []
        written: set[str] = set()
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    key = line.split("=", 1)[0].strip()
                    if key in data:
                        lines.append(f"{key}={data[key]}")
                        written.add(key)
                        continue
                lines.append(line)
        for k, v in data.items():
            if k not in written:
                lines.append(f"{k}={v}")
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")


settings = Settings()


def frontend_dist() -> Path:
    """前端构建产物目录（打包后在 _internal/frontend/dist）。"""
    return RES_DIR / "frontend" / "dist"


def runtime_info() -> dict:
    """运行形态自检所需的基础信息。"""
    return {
        "version": VERSION,
        "frozen": IS_FROZEN,
        "mode": "desktop_exe" if IS_FROZEN else "source",
        "executable": sys.executable if IS_FROZEN else str(RES_DIR / "run.py"),
        "data_dir": str(BASE_DIR),
        "resource_dir": str(RES_DIR),
        "env_file": str(BASE_DIR / ".env"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "frontend_bundled": frontend_dist().is_dir(),
    }