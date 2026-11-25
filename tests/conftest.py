"""pytest 公共夹具：全部路径/数据库指向临时目录，绝不污染真实数据。"""
from __future__ import annotations

import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp(prefix="docflow_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/app.db"
os.environ["DOCUMENT_ROOT"] = f"{_tmp}/archive"
os.environ["INBOX_ROOT"] = f"{_tmp}/inbox"
os.environ["RECYCLE_ROOT"] = f"{_tmp}/recycle"
os.environ["TEMP_DIR"] = f"{_tmp}/temp"
os.environ["BACKUP_ROOT"] = f"{_tmp}/backup"
os.environ["LEDGER_ROOT"] = f"{_tmp}/ledgers"
os.environ["SAMPLE_ROOT"] = f"{_tmp}/samples"
os.environ["LOG_DIR"] = f"{_tmp}/logs"
os.environ["AI_ENABLED"] = "False"
os.environ["AI_BASE_URL"] = ""
os.environ["OCR_ENABLED"] = "False"
os.environ["ACCESS_PASSWORD"] = ""
os.environ["AUTO_BACKUP_ENABLED"] = "False"
os.environ["RECON_ENABLED"] = "False"
os.environ["WATCH_ENABLED"] = "False"
os.environ["EMAIL_ENABLED"] = "False"
os.environ["ALLOW_OVERWRITE"] = "False"
os.environ["ARCHIVE_TEMPLATE"] = "{category}/{year}/{month}"
os.environ["LOG_LEVEL"] = "ERROR"
os.environ["MIN_FILE_AGE_SECONDS"] = "0"  # 测试即时处理，不等文件稳定窗口

import pytest  # noqa: E402

from backend.config import settings  # noqa: E402

settings.ensure_dirs()


@pytest.fixture(scope="session")
def initialized_db():
    """建表/FTS/种子数据：不依赖 client 的用例（纯 db 单测）也要有分类与规则。"""
    from backend.database import init_db

    init_db()
    yield


@pytest.fixture(scope="session")
def tmp_base():
    yield _tmp


@pytest.fixture()
def db(initialized_db):
    from backend.database import SessionLocal

    d = SessionLocal()
    try:
        yield d
    finally:
        d.close()


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from backend.app import app
    from backend.database import init_db

    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_docs(db, initialized_db):
    """用例间隔离：清空文档/日志/对账数据，保留分类与规则。"""
    from backend.database import Base, engine

    Base.metadata.create_all(bind=engine)
    from backend.models import (
        Bundle, BundleItem, DocRequirement, Document, DocumentField, DocumentSample,
        DocumentText, Ledger, LedgerRow, LearnCorrection, OperationLog, ProcessingJob,
        RecycleItem, ReconDiff, ReconRun,
    )

    for m in (BundleItem, Bundle, ReconDiff, ReconRun, DocumentField, DocumentText, Document,
              OperationLog, ProcessingJob, RecycleItem, LedgerRow, Ledger, LearnCorrection,
              DocumentSample, DocRequirement):
        db.query(m).delete()
    db.commit()
    # 必备清单是用例的输入，改过就要还原：删掉后由 seed_missing 幂等补回默认值
    from backend.services.defaults import seed_missing

    seed_missing(db)
    yield