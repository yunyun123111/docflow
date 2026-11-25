"""数据库引擎/会话/初始化：WAL、外键、FTS5 全文索引、崩溃恢复。"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings
from backend.models import Base

logger = logging.getLogger("db")

_connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=8000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    text_spaced, content='document_texts', content_rowid='document_id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON document_texts BEGIN
    INSERT INTO documents_fts(rowid, text_spaced) VALUES (new.document_id, new.text_spaced);
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON document_texts BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, text_spaced) VALUES ('delete', old.document_id, old.text_spaced);
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE ON document_texts BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, text_spaced) VALUES ('delete', old.document_id, old.text_spaced);
    INSERT INTO documents_fts(rowid, text_spaced) VALUES (new.document_id, new.text_spaced);
END;
"""


# create_all 只建新表、不会给已存在的表加列；用户库已在用，所以加列必须显式补。
# 只加不删，逐列 try/except，失败也只记日志——绝不因为一次补列失败而让程序起不来。
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("recon_runs", "source", "VARCHAR(12) NOT NULL DEFAULT 'ledger'"),
]


def _ensure_columns() -> None:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    raw = engine.raw_connection()
    try:
        for table, column, ddl in ADDED_COLUMNS:
            try:
                if not insp.has_table(table) or column in {c["name"] for c in insp.get_columns(table)}:
                    continue
                # raw_connection 是 DBAPI 层的连接，只吃字符串 SQL：传 TextClause 会抛
                # "argument 1 must be str"，于是老库的列永远补不上，读它的接口直接 500。
                cur = raw.cursor()
                try:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                finally:
                    cur.close()
                raw.commit()
                logger.warning("已为 %s 补列 %s", table, column)
            except Exception as exc:  # noqa: BLE001
                logger.error("补列失败 %s.%s：%s", table, column, exc)
    finally:
        raw.close()


def _ensure_indexes() -> None:
    """给已经存在的表补新增索引：create_all 只建新表，不会动旧表。"""
    for table in Base.metadata.sorted_tables:
        for ix in table.indexes:
            try:
                ix.create(bind=engine, checkfirst=True)
            except Exception as exc:  # noqa: BLE001
                logger.error("建索引失败 %s：%s", ix.name, exc)


def _heal_bundles() -> None:
    """历史竞态把一个合同号劈成过多个包，先并掉，否则唯一索引建不上。"""
    from backend.services.bundles import repair_duplicate_bundles

    try:
        with SessionLocal() as db:
            n = repair_duplicate_bundles(db)
        if n:
            logger.warning("已合并 %d 个重复业务包", n)
    except Exception as exc:  # noqa: BLE001
        logger.error("重复业务包自愈失败：%s", exc)


def init_db() -> None:
    """建表 + 补列 + 自愈 + 索引 + FTS + 默认数据 + 崩溃恢复。"""
    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _heal_bundles()
    _ensure_indexes()
    raw = engine.raw_connection()
    try:
        raw.executescript(FTS_DDL)   # 触发器含 BEGIN/END，必须整段执行
        raw.commit()
    finally:
        raw.close()
    from backend.services.defaults import seed_defaults, seed_missing
    with SessionLocal() as db:
        seed_defaults(db)
        seed_missing(db)   # 老库也要拿到新增的分类/规则/必备单据清单
        stale = db.execute(
            text("UPDATE processing_jobs SET status='interrupted', message='服务重启时中断', ended_at=CURRENT_TIMESTAMP "
                 "WHERE status='running'")
        )
        if stale.rowcount:
            db.execute(text("UPDATE documents SET status='pending' WHERE status='processing'"))
            logger.warning("恢复 %s 个被中断的整理任务", stale.rowcount)
        db.commit()