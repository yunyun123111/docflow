"""整理流水线：注册 → 去重 → 解析/OCR → 字段/规则/样例/AI → 置信度 → 自动归档或送审。

改进点（相对旧版）：
- 递归扫描 + 文件稳定性门槛（避免抓正在写入的文件）；
- 全文落库不截断（document_texts），旧版正文截 5000 字；
- 任务内存注册 + init_db 崩溃恢复（running → interrupted）；
- 失败原因分类记录，可按类别重试。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from backend.database import SessionLocal

from backend.config import settings
from backend.models import (
    Document, DocumentField, DocumentText, OperationLog, ProcessingJob,
    STATUS_ARCHIVED, STATUS_DUPLICATE, STATUS_FAILED, STATUS_NEED_REVIEW,
    STATUS_PENDING, STATUS_PROCESSING, STATUS_RECYCLED,
)
from backend.services import fields as fields_service
from backend.services.archive import log_operation, move_file, sha256_file
from backend.services.classifier import classify
from backend.services.fingerprint import fingerprint as make_fp
from backend.services.naming import render_name, render_path, template_for
from backend.services.parser import get_file_type, parse_file
from backend.services.seal import check as seal_check
from backend.utils.fs import safe_filename, unique_filename
from backend.utils.logger import get_logger

logger = get_logger("pipeline")

SKIP_SCAN_PREFIXES = (".", "~$", "._")
TRANSIENT_SUFFIXES = (".part", ".crdownload", ".tmp", ".download")


def _iter_candidates(root: Path, recursive: bool) -> list[Path]:
    it = root.rglob("*") if recursive else root.iterdir()
    out = []
    for p in it:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        name = p.name
        if name.startswith(SKIP_SCAN_PREFIXES) or name.lower().endswith(TRANSIENT_SUFFIXES):
            continue
        if get_file_type(p) == "unknown":
            continue
        out.append(p)
    return out


def _is_stable(p: Path) -> bool:
    try:
        st = p.stat()
    except OSError:
        return False
    return (time.time() - max(st.st_mtime, st.st_ctime)) >= settings.MIN_FILE_AGE_SECONDS


class Pipeline:
    def __init__(self) -> None:
        self._cancel: dict[int, threading.Event] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    # ---------- 任务入口 ----------
    def has_running(self) -> bool:
        with self._lock:
            return any(t.is_alive() for t in self._threads.values())

    def start_dir_job(self, db: Session, source_dir: str | None = None, recursive: bool = True,
                      kind: str = "batch") -> ProcessingJob:
        if self.has_running():
            raise RuntimeError("已有整理任务在运行，请等待完成或取消")
        src = Path(source_dir).resolve() if source_dir else settings.inbox_root
        recycle = settings.recycle_root.resolve()
        if src == recycle or recycle in src.parents:
            raise ValueError("回收站目录不能作为整理源")
        if not src.is_dir():
            raise FileNotFoundError(f"目录不存在: {src}")
        files = [p for p in _iter_candidates(src, recursive) if _is_stable(p)]
        files = self._drop_known(db, files)
        return self._launch(db, files, str(src), kind)

    def start_files_job(self, db: Session, files: list[Path], kind: str = "upload",
                        source: str = "", hints: dict[str, str] | None = None,
                        in_place: bool = False) -> ProcessingJob:
        """显式文件列表（上传/监控/邮箱共用）；hints=路径→分类提示。

        in_place=True 是「重新识别」：文件早就入库了，路径和内容都没变，必须原地
        重跑并保住原来那条记录。若照常走 register，它会用自己的 sha256 把自己判成
        重复——旧记录永远停在 pending，还凭空多一条 duplicate，包里的条目也接不上。
        """
        return self._launch(db, files, source or "upload", kind, hints=hints, in_place=in_place)

    def _drop_known(self, db: Session, files: list[Path]) -> list[Path]:
        """跳过已定性（归档/审核/重复）且内容相同的路径；同名不同内容仍处理。"""
        known = {
            (r[0], r[1])
            for r in db.query(Document.original_path, Document.sha256).filter(
                Document.status.in_((STATUS_ARCHIVED, STATUS_NEED_REVIEW, STATUS_DUPLICATE))).all()
        }
        kept: list[Path] = []
        for p in files:
            ps = str(p.resolve())
            hashes = {h for (path, h) in known if path == ps}
            if hashes:
                try:
                    if sha256_file(p) in hashes:
                        continue
                except OSError:
                    continue
            kept.append(p)
        return kept

    def _launch(self, db: Session, files: list[Path], source: str, kind: str,
                hints: dict[str, str] | None = None,
                in_place: bool = False) -> ProcessingJob:
        job = ProcessingJob(source=source, kind=kind, total=len(files), status="running")
        db.add(job)
        db.commit()
        db.refresh(job)
        cancel = threading.Event()
        with self._lock:
            self._cancel[job.id] = cancel
        t = threading.Thread(target=self._run,
                             args=(job.id, files, cancel, hints or {}, in_place),
                             name=f"pipeline-{job.id}", daemon=True)
        with self._lock:
            self._threads[job.id] = t
        t.start()
        return job

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            ev = self._cancel.get(job_id)
        if not ev:
            return False
        ev.set()
        return True

    # ---------- worker ----------
    def _run(self, job_id: int, files: list[Path], cancel: threading.Event,
             hints: dict[str, str], in_place: bool = False) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        workers = max(1, min(settings.MAX_WORKERS, len(files) or 1))
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self._one, job_id, p, cancel, hints.get(str(p), ""), in_place): p
                        for p in files}
                for fut in as_completed(futs):
                    if cancel.is_set():
                        break
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001
                        logger.exception("worker 异常: %s", e)
        finally:
            with SessionLocal() as db:
                job = db.get(ProcessingJob, job_id)
                if job:
                    job.status = "cancelled" if cancel.is_set() else "completed"
                    job.ended_at = datetime.now()
                    db.commit()
            with self._lock:
                self._cancel.pop(job_id, None)
                self._threads.pop(job_id, None)

    def _one(self, job_id: int, path: Path, cancel: threading.Event, hint: str = "",
             in_place: bool = False) -> None:
        if cancel.is_set():
            return
        with SessionLocal() as db:
            try:
                self.process_file(db, path, job_id, category_hint=hint, in_place=in_place)
            except Exception as e:  # noqa: BLE001
                logger.exception("处理失败 %s: %s", path, e)
                db.rollback()
                self._bump(db, job_id, "failed")

    # ---------- 单文件核心（上传/批量/监控/邮箱共用） ----------
    def process_file(self, db: Session, path: Path, job_id: int | None = None,
                     category_hint: str = "", in_place: bool = False) -> Document | None:
        """单文件全流程；重复内容只记 duplicate 并计数。category_hint 来自邮箱主题路由。"""
        doc = self.register(db, path, job_id, in_place=in_place)
        if doc is None:
            self._bump(db, job_id, "failed")
            return None
        if doc.status == STATUS_DUPLICATE:
            if job_id:
                self._bump(db, job_id, "duplicate")
            return doc
        return self.recognize(db, doc, job_id, category_hint=category_hint)

    def register(self, db: Session, path: Path, job_id: int | None = None,
                 in_place: bool = False) -> Document | None:
        """入库登记 + 去重。返回新 Document；重复内容返回 duplicate 记录。

        in_place 时按路径认领已存在的记录（含刚进回收站的，重识别等于把它捞回来）。
        """
        if not path.is_file():
            return None
        if in_place:
            target = str(path)
            mine = (db.query(Document)
                    .filter(or_(Document.current_path == target, Document.original_path == target))
                    .order_by(Document.status == STATUS_RECYCLED, Document.id.desc()).first())
            if mine is not None:
                mine.status = STATUS_PENDING
                mine.error = ""
                mine.job_id = job_id
                db.commit()
                return mine
        ftype = get_file_type(path)
        if ftype == "unknown":
            return None
        try:
            digest = sha256_file(path)
        except OSError as e:
            logger.warning("读取失败 %s: %s", path, e)
            return None
        twin = (db.query(Document).filter(Document.sha256 == digest)
                .filter(Document.status.notin_([STATUS_RECYCLED]))
                .order_by(Document.status.in_([STATUS_ARCHIVED, STATUS_NEED_REVIEW]).desc(),
                          Document.id.desc()).first())
        if twin:
            dup = Document(original_filename=path.name, current_filename=path.name,
                           original_path=str(path), current_path=str(path),
                           file_type=ftype, file_size=path.stat().st_size, sha256=digest,
                           status=STATUS_DUPLICATE, job_id=job_id,
                           error=f"与 #{twin.id} {twin.current_filename or twin.original_filename} 内容相同")
            db.add(dup)
            db.commit()
            return dup
        safe_name = safe_filename(path.stem, path.suffix)
        doc = Document(
            original_filename=path.name, current_filename=safe_name,
            original_path=str(path), current_path=str(path),
            file_type=ftype, mime_type=_mime(ftype),
            file_size=path.stat().st_size, sha256=digest,
            status=STATUS_PENDING, job_id=job_id,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        log_operation(db, "import", document_id=doc.id, old_path=str(path), new_path=str(path))
        db.commit()
        return doc

    def recognize(self, db: Session, doc: Document, job_id: int | None = None,
                  category_hint: str = "") -> Document:
        doc.status = STATUS_PROCESSING
        db.commit()
        path = Path(doc.current_path or doc.original_path)
        try:
            parsed = parse_file(path)
        except Exception as e:  # noqa: BLE001
            doc.status = STATUS_FAILED
            doc.error = f"parse: {e}"
            db.commit()
            self._bump(db, job_id, "failed")
            return doc

        from backend.models import space_cjk

        db.merge(DocumentText(document_id=doc.id, text=parsed.text,
                              text_spaced=space_cjk(parsed.text)))
        # 证据只用用户上传时的原始名：current_filename 是上一次识别自己拼出来的，
        # 拿它再喂回去，被判成「发票_20260512_…pdf」的合同就永远翻不回来。
        probe_name = doc.original_filename or doc.current_filename or ""
        extracted = fields_service.extract(probe_name, parsed.text)
        cls = classify(db, probe_name, parsed.text, extracted.values)
        doc.document_type = cls.document_type
        doc.suggested_category_id = cls.category_id
        doc.title = (doc.current_filename or doc.original_filename).rsplit(".", 1)[0]
        doc.confidence = cls.confidence
        doc.confidence_detail = cls.detail
        doc.fingerprint = make_fp(parsed.text)
        doc.has_seal = seal_check(path, doc.file_type)
        for name, (value, conf) in extracted.values.items():
            self._set_field(db, doc.id, name, str(value), "rule", conf)
        # 上一轮误判写错的字段必须清掉：重新识别只覆盖同名项，不删就会一直挂着假的「公司名称/金额」
        for stale in [x for x in doc.fields
                      if x.source != "manual" and x.name not in extracted.values]:
            db.delete(stale)
        doc.processed_at = datetime.now()

        if category_hint:
            hint_cat = self._find_category(db, category_hint)
            if hint_cat:
                doc.suggested_category_id = hint_cat.id
                cls.category_id = hint_cat.id
                cls.category_path = hint_cat.path
        if cls.confidence >= settings.AUTO_ARCHIVE_THRESHOLD or (category_hint and doc.suggested_category_id
                                                                 and cls.confidence >= settings.REVIEW_THRESHOLD):
            self.archive_auto(db, doc, cls)
            self._bump(db, job_id, "archived")
        else:
            doc.status = STATUS_NEED_REVIEW
            db.commit()
            self._bump(db, job_id, "need_review")
        return doc

    def archive_auto(self, db: Session, doc: Document, cls) -> None:
        db.flush()
        ext = Path(doc.current_path).suffix
        tpl = template_for(db, doc.document_type)
        original = Path(doc.original_filename).stem
        new_name = render_name(tpl.pattern, self._fields_map(db, doc), ext,
                               doc.document_type, cls.category_path, datetime.now(), original)
        segments = render_path(settings.ARCHIVE_TEMPLATE, self._fields_map(db, doc),
                               doc.document_type, cls.category_path, datetime.now(), original)
        self.apply_archive(db, doc, segments, new_name)

    # ---------- 供审核确认复用 ----------
    def apply_archive(self, db: Session, doc: Document, segments: list[str], new_name: str) -> Path:
        from backend.utils.fs import unique_filename as _u

        target_dir = Path(settings.document_root, *segments)
        suffix = Path(doc.current_path).suffix
        if suffix and new_name.lower().endswith(suffix.lower()):
            new_name = new_name[: -len(suffix)]   # 模板已经拼过扩展名，别再叠一层 .txt.txt
        target = _u(target_dir, safe_filename(new_name, suffix))
        src = Path(doc.current_path)
        old = str(src)
        move_file(src, target)
        doc.current_path = str(target)
        doc.current_filename = target.name
        doc.archived_category_id = doc.suggested_category_id
        doc.status = STATUS_ARCHIVED
        doc.archived_at = datetime.now()
        log_operation(db, "archive", document_id=doc.id, old_path=old, new_path=str(target),
                      detail="/".join(segments))
        db.commit()
        try:
            from backend.services import bundles as bundle_service

            bundle_service.attach_document(db, doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("归档后自动入包失败 doc=%s: %s", doc.id, exc)
        return target

    def rename(self, db: Session, doc: Document, base: str) -> str:
        """手动重命名（保持目录，只改名）。"""
        src = Path(doc.current_path)
        new_name = safe_filename(base, src.suffix)
        if new_name == src.name:
            return new_name
        target = unique_filename(src.parent, new_name)
        old = str(src)
        move_file(src, target)
        doc.current_path = str(target)
        doc.current_filename = target.name
        log_operation(db, "rename", document_id=doc.id, old_path=old, new_path=str(target))
        db.commit()
        return target.name

    @staticmethod
    def _find_category(db: Session, name: str):
        from backend.models import Category

        return (db.query(Category).filter(Category.name == name).first()
                or db.query(Category).filter(Category.path.like(f"%{name}%")).first())

    def _set_field(self, db: Session, doc_id: int, name: str, value: str, source: str, conf: float) -> None:
        f = db.query(DocumentField).filter(DocumentField.document_id == doc_id,
                                           DocumentField.name == name).first()
        if f:
            if f.source == "manual" and source != "manual":
                return   # 人工填过的值优先：重新识别不该把用户手工修正冲回规则值
            f.value, f.source, f.confidence = value[:500], source, conf
        else:
            db.add(DocumentField(document_id=doc_id, name=name, value=value[:500], source=source, confidence=conf))

    def _fields_map(self, db: Session, doc: Document) -> dict:
        """直接查库：同一 session 里刚写入的字段不会自动进 doc.fields 关系缓存，
        渲染命名时读到旧集合就会漏掉合同号，文件名里留下 .pdf-1 这种上传残留。
        """
        rows = db.query(DocumentField).filter(DocumentField.document_id == doc.id).all()
        return {f.name: (f.value, f.confidence) for f in rows}

    COUNTERS = ("archived", "need_review", "duplicate", "failed")

    def _bump(self, db: Session | None, job_id: int | None, counter: str) -> None:
        if not job_id or counter not in self.COUNTERS:
            return
        d = db or SessionLocal()
        try:
            # 4 条线程各自读一遍再 +1 会丢增量（进度条停在 16/19）：交给 SQL 原子累加
            d.execute(update(ProcessingJob).where(ProcessingJob.id == job_id)
                      .values(**{counter: getattr(ProcessingJob, counter) + 1}))
            d.execute(update(ProcessingJob).where(ProcessingJob.id == job_id).values(
                processed=(ProcessingJob.archived + ProcessingJob.need_review
                         + ProcessingJob.duplicate + ProcessingJob.failed)))
            d.commit()
        finally:
            if db is None:
                d.close()

    def retry_failed(self, db: Session, only: str | None = None) -> ProcessingJob | None:
        """重试 failed 文档；only=parse|ocr|ai|read 可按失败类别过滤。"""
        q = db.query(Document).filter(Document.status == STATUS_FAILED)
        files = []
        for doc in q.all():
            if only and not (doc.error or "").startswith(only):
                continue
            p = Path(doc.current_path or doc.original_path)
            if p.is_file():
                doc.status = STATUS_PENDING
                doc.error = ""
                files.append(p)
        db.commit()
        if not files:
            return None
        return self._launch(db, files, "retry", "retry")


def _mime(ftype: str) -> str:
    from backend.services.parser import MIME

    return MIME.get(ftype, "application/octet-stream")


pipeline = Pipeline()