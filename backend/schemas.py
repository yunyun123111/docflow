"""Pydantic 请求模型（响应统一返回结构化 dict，形状见各端点注释）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    password: str


class RenameRequest(BaseModel):
    filename: str


class DocumentPatchRequest(BaseModel):
    document_type: str | None = None
    title: str | None = None
    suggested_category_id: int | None = None
    fields: dict[str, str] | None = None
    filename: str | None = None


class ReviewApproveRequest(BaseModel):
    category_id: int | None = None
    document_type: str | None = None
    filename: str | None = None
    save_as_sample: bool = False
    rename_template_id: int | None = None


class BatchIds(BaseModel):
    ids: list[int]


class BatchApproveRequest(BaseModel):
    ids: list[int]
    category_id: int | None = None


class ClusterConfirmRequest(BaseModel):
    fingerprint: str
    category_id: int | None = None
    document_type: str | None = None
    base_filename: str | None = None
    ids: list[int] | None = None


class BatchDeleteRequest(BaseModel):
    ids: list[int]
    reason: str = "批量删除"


class ExportZipRequest(BaseModel):
    ids: list[int]
    zip_name: str = "导出文档"


class ProcessRequest(BaseModel):
    source_dir: str | None = None
    recursive: bool = True


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: int | None = None
    color: str = ""


class KeywordsIn(BaseModel):
    keywords: list[str]


class RuleIn(BaseModel):
    name: str
    pattern: str
    match_type: str = "keyword"
    field: str = "any"
    category_id: int | None = None
    document_type: str = ""
    confidence: float = 0.9
    priority: int = 100
    enabled: bool = True


class FilenameRuleIn(BaseModel):
    name: str
    regex: str
    replacement: str = ""
    priority: int = 100
    enabled: bool = True


class RenameTemplateIn(BaseModel):
    name: str
    pattern: str
    document_type: str = ""
    is_default: bool = False


class SimulateRequest(BaseModel):
    filename: str = ""
    text: str = ""


class SettingsIn(BaseModel):
    document_root: str | None = None
    inbox_root: str | None = None
    archive_template: str | None = None
    rename_template: str | None = None
    auto_archive_threshold: float | None = None
    review_threshold: float | None = None
    w_rule: float | None = None
    w_field: float | None = None
    w_keyword: float | None = None
    w_ai: float | None = None
    allow_overwrite: bool | None = None
    recycle_retention_days: int | None = None
    max_workers: int | None = None
    ocr_enabled: bool | None = None
    ocr_max_pages: int | None = None
    ai_enabled: bool | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None
    ai_timeout: int | None = None
    access_password: str | None = None
    host: str | None = None
    watch_enabled: bool | None = None
    watch_dirs: list[str] | None = None
    watch_interval: int | None = None
    seal_detect_enabled: bool | None = None
    email_enabled: bool | None = None
    email_imap_host: str | None = None
    email_imap_port: int | None = None
    email_ssl: bool | None = None
    bundle_merge_suggest_enabled: bool | None = None
    bundle_merge_amount_tolerance: float | None = None
    bundle_merge_date_window_days: int | None = None
    email_user: str | None = None
    email_password: str | None = None
    email_poll_interval: int | None = None
    email_sender_whitelist: str | None = None
    email_subject_prefix: str | None = None
    recon_enabled: bool | None = None
    recon_interval_minutes: int | None = None
    auto_backup_enabled: bool | None = None
    auto_backup_keep: int | None = None


class BackupCreateRequest(BaseModel):
    include_documents: bool = False


class LedgerCreateRequest(BaseModel):
    name: str
    sheet_map: dict[str, str] | None = None


class ReconRunRequest(BaseModel):
    ledger_id: int | None = None
    source: str = "auto"          # auto | ledger | archive


class BundleCreateRequest(BaseModel):
    name: str = ""
    note: str = ""


class BundlePatchRequest(BaseModel):
    name: str | None = None
    note: str | None = None
    status: str | None = None


class BundleItemsRequest(BaseModel):
    document_ids: list[int]
    role: str | None = None


class BundleItemPatchRequest(BaseModel):
    role: str


class BundleMergeRequest(BaseModel):
    target_id: int
    note: str = ""


class DocRequirementIn(BaseModel):
    contract_role: str
    required_role: str
    min_count: int = 1
    amount_check: bool = True
    enabled: bool = True


class ResolveRequest(BaseModel):
    note: str = ""