"""全部 ORM 模型（单模块，干净的分层数据视图）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                         UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.now()


# 状态机：pending → processing → archived | need_review | failed | duplicate；recycled（回收站中）
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_ARCHIVED = "archived"
STATUS_NEED_REVIEW = "need_review"
STATUS_FAILED = "failed"
STATUS_DUPLICATE = "duplicate"
STATUS_RECYCLED = "recycled"
FINAL_STATUSES = (STATUS_ARCHIVED, STATUS_NEED_REVIEW, STATUS_DUPLICATE)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    path: Mapped[str] = mapped_column(String(200), default="")
    color: Mapped[str] = mapped_column(String(20), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    keywords: Mapped[list["CategoryKeyword"]] = relationship(cascade="all, delete-orphan")


class CategoryKeyword(Base):
    """分类关键词：分类器打分的数据源之一。"""
    __tablename__ = "category_keywords"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(100))


class ClassifyRule(Base):
    """识别规则：在文件名/正文上匹配关键词或正则，直接给出分类与置信度。"""
    __tablename__ = "classify_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    pattern: Mapped[str] = mapped_column(String(500))
    match_type: Mapped[str] = mapped_column(String(16), default="keyword")   # keyword | regex
    field: Mapped[str] = mapped_column(String(16), default="any")            # filename | text | any
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    document_type: Mapped[str] = mapped_column(String(80), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.9)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RenameTemplate(Base):
    __tablename__ = "rename_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    document_type: Mapped[str] = mapped_column(String(80), default="")       # 空=通用
    pattern: Mapped[str] = mapped_column(String(300))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class FilenameRule(Base):
    """文件名清洗规则：入库/重命名前对原名做 regex 替换。"""
    __tablename__ = "filename_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    regex: Mapped[str] = mapped_column(String(300))
    replacement: Mapped[str] = mapped_column(String(300), default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    current_filename: Mapped[str] = mapped_column(String(512), default="")
    original_path: Mapped[str] = mapped_column(String(1024))
    current_path: Mapped[str] = mapped_column(String(1024), default="")
    file_type: Mapped[str] = mapped_column(String(20), default="", index=True)
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    document_type: Mapped[str] = mapped_column(String(200), default="", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    suggested_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    archived_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_detail: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING, index=True)
    has_seal: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[int | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)

    fields: Mapped[list["DocumentField"]] = relationship(cascade="all, delete-orphan", back_populates="document")
    text: Mapped["DocumentText | None"] = relationship(cascade="all, delete-orphan", back_populates="document")


class DocumentField(Base):
    __tablename__ = "document_fields"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(String(512), default="", index=True)
    source: Mapped[str] = mapped_column(String(16), default="rule")          # rule | ai | manual | sample
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    document: Mapped[Document] = relationship(back_populates="fields")
    __table_args__ = (UniqueConstraint("document_id", "name"),)


class DocumentText(Base):
    """正文全文单独表（不截断）。

    text_spaced 为“单字空格化”镜像列：unicode61 会把连续中文切成一个 token，
    空格化后每个汉字独立成 token，FTS5 短语查询即可命中任意中文子串。
    """
    __tablename__ = "document_texts"
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    text_spaced: Mapped[str] = mapped_column(Text, default="")
    document: Mapped[Document] = relationship(back_populates="text")


def space_cjk(text: str) -> str:
    return " ".join(text or "")


def unspace_cjk(text: str) -> str:
    """去掉与汉字相邻的空格（还原 space_cjk 的镜像），保留英文正常空格。"""
    import re

    return re.sub(r"(?<=[\u4e00-\u9fff]) (?=\S)|(?<=\S) (?=[\u4e00-\u9fff])", "", text or "")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(512), default="")
    kind: Mapped[str] = mapped_column(String(20), default="batch")           # batch | upload | watch | email
    status: Mapped[str] = mapped_column(String(16), default="running")       # running | completed | cancelled | failed | interrupted
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[int] = mapped_column(Integer, default=0)
    need_review: Mapped[int] = mapped_column(Integer, default=0)
    duplicate: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)


class OperationLog(Base):
    __tablename__ = "operation_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    op: Mapped[str] = mapped_column(String(32), index=True)   # import|rename|archive|move|delete|restore|purge|undo|approve|reject
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    old_path: Mapped[str] = mapped_column(String(1024), default="")
    new_path: Mapped[str] = mapped_column(String(1024), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(String(16), default="ok")            # ok | error
    error: Mapped[str] = mapped_column(Text, default="")
    undone_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class DocumentSample(Base):
    """人工样例：审核纠正时“存为样例”，分类器按文本指纹匹配（零 token）。"""
    __tablename__ = "document_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(1024), default="")
    document_type: Mapped[str] = mapped_column(String(200), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    text_norm: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LearnCorrection(Base):
    """纠错流水：审核中改变分类/类型的记录，用于生成候选规则建议。"""
    __tablename__ = "learn_corrections"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int | None] = mapped_column(Integer)
    from_category_id: Mapped[int | None] = mapped_column(Integer)
    to_category_id: Mapped[int | None] = mapped_column(Integer)
    document_type: Mapped[str] = mapped_column(String(200), default="")
    keyword_hint: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RecycleItem(Base):
    __tablename__ = "recycle_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(512), default="")
    original_path: Mapped[str] = mapped_column(String(1024), default="")
    recycled_path: Mapped[str] = mapped_column(String(1024), default="")
    reason: Mapped[str] = mapped_column(String(200), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Ledger(Base):
    """业务台账（Excel），对账数据源。"""
    __tablename__ = "ledgers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(1024))
    sheet_map: Mapped[dict | None] = mapped_column(JSON)     # {"contract":"合同台账",...} 自动探测可覆盖
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    rows: Mapped[list["LedgerRow"]] = relationship(cascade="all, delete-orphan")


class LedgerRow(Base):
    """台账业务行：合同主数据 + 其下结算/回款汇总。"""
    __tablename__ = "ledger_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id", ondelete="CASCADE"), index=True)
    contract_no: Mapped[str] = mapped_column(String(120), default="", index=True)
    customer: Mapped[str] = mapped_column(String(200), default="")
    our_entity: Mapped[str] = mapped_column(String(200), default="")
    direction: Mapped[str] = mapped_column(String(16), default="")    # sale | purchase | ""
    amount: Mapped[float | None] = mapped_column(Float)
    sign_date: Mapped[str] = mapped_column(String(32), default="")
    settlement_total: Mapped[float | None] = mapped_column(Float)
    received_total: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict | None] = mapped_column(JSON)


class ReconRun(Base):
    __tablename__ = "recon_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ledger_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(12), default="ledger")   # ledger | archive
    status: Mapped[str] = mapped_column(String(16), default="running")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    total_docs: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict | None] = mapped_column(JSON)        # diff type -> count
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    diffs: Mapped[list["ReconDiff"]] = relationship(cascade="all, delete-orphan")


class ReconDiff(Base):
    __tablename__ = "recon_diffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("recon_runs.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    diff_type: Mapped[str] = mapped_column(String(40), index=True)   # missing_doc|missing_required|missing_payment|amount_mismatch|extra_doc|date_suspect|seal_missing
    # 新增状态一律进 detail JSON：{"docs": [id], "required_role": "发票", "bundle_id": 1, "closed_by": [id]}
    severity: Mapped[str] = mapped_column(String(10), default="warn")  # info | warn | error
    contract_no: Mapped[str] = mapped_column(String(120), default="")
    customer: Mapped[str] = mapped_column(String(200), default="")
    document_id: Mapped[int | None] = mapped_column(Integer)
    expected: Mapped[str] = mapped_column(String(200), default="")
    actual: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict | None] = mapped_column(JSON)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Bundle(Base):
    """业务包：一份业务的采购/销售合同及其全部往来单据。"""
    __tablename__ = "bundles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    contract_key: Mapped[str] = mapped_column(String(120), default="")
    customer: Mapped[str] = mapped_column(String(200), default="")
    our_entity: Mapped[str] = mapped_column(String(200), default="")
    direction: Mapped[str] = mapped_column(String(12), default="", index=True)  # sale|purchase|mixed
    amount: Mapped[float | None] = mapped_column(Float)
    sign_date: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|settled
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(12), default="auto")              # auto|manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    items: Mapped[list["BundleItem"]] = relationship(cascade="all, delete-orphan",
                                                     back_populates="bundle")
    __table_args__ = (
        # 一个合同号只该有一个业务包：并发归档时两条线程都"先查再插"会各建一个，
        # 把一个业务劈成两半（齐备度、关联差异都被对半砍）。空号是手工包，不参与唯一。
        Index("uq_bundles_contract_key", "contract_key", unique=True,
              sqlite_where=text("contract_key <> ''")),
    )


class BundleItem(Base):
    """包内一条文档及其角色；一个文档只属于一个包。"""
    __tablename__ = "bundle_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    bundle_id: Mapped[int] = mapped_column(ForeignKey("bundles.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="其他")
    contract_no: Mapped[str] = mapped_column(String(120), default="")
    added_by: Mapped[str] = mapped_column(String(12), default="auto")            # auto|manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    bundle: Mapped[Bundle] = relationship(back_populates="items")
    __table_args__ = (UniqueConstraint("bundle_id", "document_id"),)


class DocRequirement(Base):
    """必备单据清单：决定“缺发票/缺提货单”这类可上传补齐的差异。"""
    __tablename__ = "doc_requirements"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_role: Mapped[str] = mapped_column(String(40), default="", index=True)
    required_role: Mapped[str] = mapped_column(String(40), default="")
    min_count: Mapped[int] = mapped_column(Integer, default=1)
    amount_check: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("contract_role", "required_role"),)
