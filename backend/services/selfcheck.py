"""配置状态自检 + 一键修复。

每项检查返回 {key,label,level(ok|warn|error|info),message,detail,fix}；
fix 为可执行修复动作名，前端“一键修复”调用 /api/system/repair/{fix}。
"""
from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text as sql_text

from backend.config import BASE_DIR, RES_DIR, runtime_info, settings
from backend.database import engine
from backend.utils.logger import get_logger

logger = get_logger("selfcheck")

OK, WARN, ERR, INFO = "ok", "warn", "error", "info"


def _writable(path: Path) -> tuple[bool, str]:
    try:
        probe = path / ".write_probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, ""
    except OSError as e:
        return False, str(e)[:120]


def _check_dirs() -> list[dict]:
    out = []
    for label, p in (("归档目录", settings.document_root), ("待整理目录", settings.inbox_root),
                     ("回收站", settings.recycle_root), ("备份目录", settings.backup_root),
                     ("日志目录", settings.log_dir)):
        if not p.is_dir():
            out.append({"key": str(p), "label": label, "level": WARN,
                        "message": "目录不存在", "detail": str(p), "fix": "dirs"})
            continue
        okw, err = _writable(p)
        out.append({"key": str(p), "label": label, "level": OK if okw else ERR,
                    "message": "正常" if okw else f"不可写：{err}", "detail": str(p),
                    "fix": None if okw else "dirs"})
    return out


def _check_disk() -> dict:
    try:
        free = shutil.disk_usage(str(settings.document_root)).free
        gb = free / 2**30
        level = OK if gb > 2 else (WARN if gb > 0.5 else ERR)
        return {"key": "disk", "label": "磁盘剩余空间", "level": level,
                "message": f"归档盘剩余 {gb:.1f} GB", "detail": str(settings.document_root), "fix": None}
    except OSError as e:
        return {"key": "disk", "label": "磁盘剩余空间", "level": INFO, "message": str(e)[:100], "detail": "", "fix": None}


def _check_database() -> list[dict]:
    out = []
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
            wal = conn.execute(sql_text("PRAGMA journal_mode")).scalar()
        out.append({"key": "db", "label": "数据库", "level": OK, "message": f"可连接（{wal}）", "detail": "", "fix": None})
    except Exception as e:  # noqa: BLE001
        out.append({"key": "db", "label": "数据库", "level": ERR, "message": str(e)[:120], "detail": "", "fix": None})
    out.extend(_check_schema())
    return out


def _check_schema() -> list[dict]:
    """表结构漂移：老库少一列，用到它的接口就只会甩个 500，这里提前点名。"""
    from sqlalchemy import inspect as sa_inspect

    from backend.models import Base

    try:
        insp = sa_inspect(engine)
        gaps: list[str] = []
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                gaps.append("缺表 " + table.name)
                continue
            cols = {c["name"] for c in insp.get_columns(table.name)}
            missing = [c.name for c in table.columns if c.name not in cols]
            if missing:
                gaps.append(table.name + "." + ",".join(missing))
    except Exception as e:  # noqa: BLE001
        return [{"key": "schema", "label": "表结构", "level": WARN, "message": str(e)[:120],
                "detail": "", "fix": None}]
    if gaps:
        return [{"key": "schema", "label": "表结构", "level": ERR,
                "message": f"{len(gaps)} 处缺表/缺列",
                "detail": "；".join(gaps[:5]) + "（重启程序会自动补齐）", "fix": None}]
    return [{"key": "schema", "label": "表结构", "level": OK,
            "message": f"{len(Base.metadata.tables)} 张表结构齐备", "detail": "", "fix": None}]


def _check_fts() -> dict:
    try:
        raw = engine.raw_connection()
        try:
            raw.execute("INSERT INTO documents_fts(documents_fts) VALUES('integrity-check')")
            n = raw.execute("SELECT count(*) FROM documents_fts").fetchone()[0]
        finally:
            raw.close()
        return {"key": "fts", "label": "全文索引", "level": OK, "message": f"完整性通过（{n} 条）", "detail": "", "fix": None}
    except Exception as e:  # noqa: BLE001
        return {"key": "fts", "label": "全文索引", "level": WARN, "message": str(e)[:120],
                "detail": "可能影响正文搜索", "fix": "fts"}


def _check_ocr() -> dict:
    if not settings.OCR_ENABLED:
        return {"key": "ocr", "label": "OCR", "level": INFO, "message": "已关闭，扫描件/图片不识别", "detail": "", "fix": None}
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return {"key": "ocr", "label": "OCR", "level": OK, "message": "RapidOCR 可用（模型首次使用时加载）", "detail": "", "fix": None}
    except Exception as e:  # noqa: BLE001
        return {"key": "ocr", "label": "OCR", "level": WARN, "message": f"依赖缺失：{str(e)[:80]}", "detail": "", "fix": None}


def _check_seal() -> dict:
    try:
        import cv2  # noqa: F401

        return {"key": "seal", "label": "盖章检测", "level": OK if settings.SEAL_DETECT_ENABLED else INFO,
                "message": "可用" if settings.SEAL_DETECT_ENABLED else "已关闭", "detail": "", "fix": None}
    except Exception as e:  # noqa: BLE001
        return {"key": "seal", "label": "盖章检测", "level": WARN, "message": str(e)[:80], "detail": "", "fix": None}


def _check_ai() -> dict:
    if not settings.AI_ENABLED:
        return {"key": "ai", "label": "AI 辅助", "level": INFO, "message": "未启用（纯规则模式，按剩余权重归一化）", "detail": "", "fix": None}
    missing = [n for n, v in (("AI_BASE_URL", settings.AI_BASE_URL), ("AI_MODEL", settings.AI_MODEL)) if not v]
    if missing:
        return {"key": "ai", "label": "AI 辅助", "level": WARN, "message": f"缺配置：{'、'.join(missing)}", "detail": "", "fix": None}
    try:
        import httpx

        r = httpx.get(settings.AI_BASE_URL.rstrip("/") + "/models", timeout=3.0,
                      headers={"Authorization": f"Bearer {settings.AI_API_KEY}"} if settings.AI_API_KEY else {})
        ok = r.status_code < 500
        return {"key": "ai", "label": "AI 辅助", "level": OK if ok else WARN,
                "message": f"端点可达（HTTP {r.status_code}）" if ok else f"端点异常（HTTP {r.status_code}）",
                "detail": settings.AI_BASE_URL, "fix": None}
    except Exception as e:  # noqa: BLE001
        return {"key": "ai", "label": "AI 辅助", "level": WARN, "message": f"无法连接：{str(e)[:90]}", "detail": settings.AI_BASE_URL, "fix": None}


def _check_watch() -> dict:
    dirs = settings.watch_dirs
    if not settings.WATCH_ENABLED:
        return {"key": "watch", "label": "目录监控", "level": INFO, "message": "未启用", "detail": "", "fix": None}
    if not dirs:
        return {"key": "watch", "label": "目录监控", "level": WARN, "message": "已启用但未配置目录", "detail": "", "fix": None}
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        return {"key": "watch", "label": "目录监控", "level": WARN, "message": f"目录不存在：{missing[0]}", "detail": "、".join(missing[:3]), "fix": None}
    return {"key": "watch", "label": "目录监控", "level": OK, "message": f"{len(dirs)} 个目录正常", "detail": "", "fix": None}


def _check_email() -> dict:
    if not settings.EMAIL_ENABLED:
        return {"key": "email", "label": "邮箱接收", "level": INFO, "message": "未启用", "detail": "", "fix": None}
    missing = [n for n, v in (("主机", settings.EMAIL_IMAP_HOST), ("账号", settings.EMAIL_USER),
                              ("密码/授权码", settings.EMAIL_PASSWORD)) if not v]
    if missing:
        return {"key": "email", "label": "邮箱接收", "level": WARN, "message": f"缺配置：{'、'.join(missing)}", "detail": "", "fix": None}
    return {"key": "email", "label": "邮箱接收", "level": OK, "message": f"{settings.EMAIL_USER} 配置完整", "detail": "", "fix": None}


def _check_security() -> dict:
    lan = settings.HOST not in ("127.0.0.1", "localhost", "::1")
    if lan and not settings.ACCESS_PASSWORD:
        return {"key": "security", "label": "网络与安全", "level": ERR,
                "message": f"监听 {settings.HOST}（局域网）但未设访问密码", "detail": "请到 设置→安全 设置密码", "fix": None}
    if lan:
        return {"key": "security", "label": "网络与安全", "level": OK, "message": "局域网模式 + 已设密码", "detail": settings.HOST, "fix": None}
    return {"key": "security", "label": "网络与安全", "level": OK, "message": "仅本机访问", "detail": "127.0.0.1", "fix": None}


def _check_backup() -> dict:
    if not settings.AUTO_BACKUP_ENABLED:
        return {"key": "backup", "label": "自动备份", "level": INFO, "message": "已关闭", "detail": "", "fix": None}
    backups = sorted(settings.backup_root.glob("backup_*.zip"), reverse=True) if settings.backup_root.is_dir() else []
    if not backups:
        return {"key": "backup", "label": "自动备份", "level": WARN, "message": "尚无任何备份", "detail": "", "fix": "backup_now"}
    name = backups[0].name
    try:
        stamp = datetime.strptime(name[len("backup_"):][:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return {"key": "backup", "label": "自动备份", "level": WARN, "message": "无法解析备份时间", "detail": name, "fix": None}
    age = datetime.now() - stamp
    level = OK if age < timedelta(days=2) else WARN
    return {"key": "backup", "label": "自动备份", "level": level,
            "message": f"最近备份 {name}（{int(age.total_seconds() // 3600)} 小时前）", "detail": "", "fix": None if level == OK else "backup_now"}


def _check_runtime() -> list[dict]:
    """运行形态与环境：桌面版 exe / 源码、数据目录、配置文件、前端资源。"""
    info = runtime_info()
    out = [{
        "key": "runtime", "label": "运行形态", "level": OK,
        "message": "桌面版（DocFlow.exe）" if info["frozen"] else "源码运行（python run.py）",
        "detail": f"v{info['version']} · Python {info['python']} · 数据目录 {info['data_dir']}",
        "fix": None,
    }]
    env = BASE_DIR / ".env"
    writable, err = _writable(BASE_DIR)
    out.append({
        "key": "config", "label": "配置文件", "level": OK if writable else ERR,
        "message": "可写（设置能保存并生效）" if writable else f"数据目录不可写：{err}",
        "detail": str(env) if env.exists() else f"{env}（尚未生成，首次保存设置时创建）",
        "fix": None if writable else "dirs",
    })
    index_html = RES_DIR / "frontend" / "dist" / "index.html"
    if index_html.is_file():
        assets = (RES_DIR / "frontend" / "dist" / "assets")
        n = len(list(assets.glob("*"))) if assets.is_dir() else 0
        out.append({"key": "frontend", "label": "界面资源", "level": OK,
                    "message": f"已内置（{n} 个静态文件）", "detail": str(index_html.parent), "fix": None})
    else:
        out.append({"key": "frontend", "label": "界面资源", "level": ERR,
                    "message": "找不到前端构建产物，页面无法打开",
                    "detail": f"{RES_DIR / 'frontend' / 'dist'}；桌面版请重装，源码版请执行 cd frontend && npm run build",
                    "fix": None})
    return out


def _check_ocr_models() -> dict:
    if not settings.OCR_ENABLED:
        return {"key": "ocr_model", "label": "OCR 模型", "level": INFO, "message": "OCR 已关闭", "detail": "", "fix": None}
    try:
        import rapidocr_onnxruntime as _ro

        root = Path(_ro.__file__).parent
        models = list(root.rglob("*.onnx"))
        if models:
            mb = sum(m.stat().st_size for m in models) / 2**20
            return {"key": "ocr_model", "label": "OCR 模型", "level": OK,
                    "message": f"{len(models)} 个模型文件（{mb:.0f} MB）", "detail": str(root), "fix": None}
        return {"key": "ocr_model", "label": "OCR 模型", "level": WARN, "message": "未找到 .onnx 模型文件，扫描件将无法识别",
                "detail": str(root), "fix": None}
    except Exception as exc:  # noqa: BLE001
        return {"key": "ocr_model", "label": "OCR 模型", "level": WARN, "message": f"OCR 依赖不完整：{str(exc)[:80]}",
                "detail": "桌面版可先关闭 OCR，或用纯规则模式继续", "fix": None}


def _check_cache() -> dict:
    d = settings.temp_dir / "thumbs"
    if not d.is_dir():
        return {"key": "cache", "label": "预览缓存", "level": OK, "message": "暂无缓存", "detail": str(d), "fix": None}
    files = list(d.glob("*.png"))
    mb = sum(f.stat().st_size for f in files) / 2**20
    level = OK if mb < 500 else WARN
    return {"key": "cache", "label": "预览缓存", "level": level,
            "message": f"{len(files)} 个文件 / {mb:.0f} MB", "detail": str(d),
            "fix": "preview_cache" if mb >= 500 else None}


def _check_missing_files() -> dict:
    """库与磁盘的一致性：文件被外部删掉后留下的「幽灵记录」（预览/打开都会失败）。"""
    from backend.database import SessionLocal
    from backend.models import Document, STATUS_RECYCLED
    from backend.services.recycle import find_file

    try:
        with SessionLocal() as db:
            docs = db.query(Document).filter(Document.status != STATUS_RECYCLED).all()
            missing = [d.id for d in docs if find_file(d) is None]
    except Exception as exc:  # noqa: BLE001
        return {"key": "ghosts", "label": "记录与磁盘一致性", "level": INFO,
                "message": str(exc)[:100], "detail": "", "fix": None}
    if not missing:
        return {"key": "ghosts", "label": "记录与磁盘一致性", "level": OK,
                "message": f"{len(docs)} 条记录的文件都在磁盘上", "detail": "", "fix": None}
    names = "、".join(str(i) for i in missing[:5])
    return {"key": "ghosts", "label": "记录与磁盘一致性", "level": WARN if len(missing) < 100 else ERR,
            "message": f"{len(missing)} 条记录的文件已不在磁盘上（无法预览/打开）",
            "detail": f"文档 id：{names}{' …' if len(missing) > 5 else ''}；一键清理会把它们移入回收站",
            "fix": "prune_missing"}


def _check_interrupted() -> dict:
    """任务与队列一致性：崩溃遗留、待审核积压、回收站到期未清。"""
    from backend.database import SessionLocal
    from backend.models import Document, RecycleItem

    try:
        with SessionLocal() as db:
            stuck = db.query(Document).filter(Document.status == "processing").count()
            review = db.query(Document).filter(Document.status == "need_review").count()
            overdue = db.query(RecycleItem).filter(RecycleItem.expires_at is not None,
                                                   RecycleItem.expires_at < datetime.now()).count()
    except Exception as exc:  # noqa: BLE001
        return {"key": "queue", "label": "处理队列", "level": INFO,
                "message": f"数据库尚未初始化（{str(exc)[:60]}）", "detail": "", "fix": None}
    if stuck:
        return {"key": "queue", "label": "处理队列", "level": WARN,
                "message": f"{stuck} 个文档停留在「识别中」（上次异常退出留下）",
                "detail": f"另有待审核 {review} 个", "fix": "requeue"}
    msg = f"待审核 {review} 个" if review else "队列空闲"
    if overdue and settings.RECYCLE_RETENTION_DAYS:
        msg += f" · 回收站 {overdue} 项已到期待清理"
    return {"key": "queue", "label": "处理队列", "level": INFO if review or overdue else OK,
            "message": msg, "detail": "", "fix": "purge_recycle" if overdue else None}


def _check_bundles() -> dict:
    """归集一致性：一个合同号只该有一个业务包，带编号的归档文档都该在包里。"""
    from backend.database import SessionLocal
    from backend.models import Bundle, BundleItem, Document, STATUS_ARCHIVED
    from backend.services import bundles as bundle_service
    from backend.services.fields import FIELD_CONTRACT_NO

    try:
        with SessionLocal() as db:
            groups = bundle_service.duplicate_key_groups(db)
            total = db.query(Bundle).count()
            docs = db.query(Document).filter(Document.status == STATUS_ARCHIVED).all()
            have = {i.document_id for i in db.query(BundleItem).all()}
            fmap = bundle_service.field_map(db, [d.id for d in docs])
            loose = [d.id for d in docs
                     if fmap.get(d.id, {}).get(FIELD_CONTRACT_NO, "").strip() and d.id not in have]
    except Exception as exc:  # noqa: BLE001
        return {"key": "bundles", "label": "文档归集", "level": INFO,
                "message": str(exc)[:100], "detail": "", "fix": None}
    if groups or loose:
        bits = []
        if groups:
            bits.append(f"{len(groups)} 个合同号被劈成了多个包")
        if loose:
            names = "、".join(str(i) for i in loose[:5])
            more = " …" if len(loose) > 5 else ""
            bits.append(f"{len(loose)} 份带合同号的归档文档没入包（{names}{more}）")
        return {"key": "bundles", "label": "文档归集", "level": WARN,
                "message": "；".join(bits),
                "detail": "点「重建归集」会按归档文档重算全部业务包", "fix": "rebuild_bundles"}
    return {"key": "bundles", "label": "文档归集", "level": OK if total else INFO,
            "message": f"{total} 个业务包，同合同号无重复", "detail": "", "fix": "rebuild_bundles"}


def _check_company() -> dict:
    """本公司名称：不填就分不清我方与对方，业务包与合并建议的口径都是虚的。"""
    from backend.config import settings
    from backend.database import SessionLocal
    from backend.models import Bundle

    ours = (settings.OUR_COMPANY or "").strip()
    if ours:
        return {"key": "company", "label": "本公司名称", "level": OK, "message": ours, "detail": "", "fix": None}
    try:
        with SessionLocal() as db:
            same = db.query(Bundle).filter(Bundle.customer != "", Bundle.our_entity != "",
                                            Bundle.customer == Bundle.our_entity).count()
    except Exception as exc:  # noqa: BLE001
        return {"key": "company", "label": "本公司名称", "level": INFO,
                "message": str(exc)[:100], "detail": "", "fix": None}
    if same:
        return {"key": "company", "label": "本公司名称", "level": WARN,
                "message": f"未设置，{same} 个业务包的客户与我方主体同值",
                "detail": "设置 → 无人值守 → 本公司名称 填上，再点「重建归集」即可分开；"
                        "合并建议也靠它判断是不是同一家公司",
                "fix": "rebuild_bundles"}
    return {"key": "company", "label": "本公司名称", "level": INFO, "message": "未设置：分不清我方与对方",
            "detail": "设置 → 无人值守 → 文档归集卡片", "fix": None}


def run_checks(quick: bool = False) -> dict:
    """quick=True 只跑本地文件/数据库类检查（不碰网络、不加载 OCR 模型），供顶栏徽标使用。"""
    slow = ("_check_ocr", "_check_seal", "_check_ai", "_check_email", "_check_ocr_models",
            "_check_missing_files", "_check_bundles")
    items: list[dict] = []
    all_fns = (_check_runtime, _check_dirs, _check_disk, _check_database, _check_fts, _check_cache,
               _check_interrupted, _check_missing_files, _check_bundles, _check_ocr, _check_ocr_models,
               _check_seal, _check_ai, _check_watch, _check_email, _check_security, _check_backup,
               _check_company)
    for fn in [f for f in all_fns if not (quick and f.__name__ in slow)]:
        try:
            r = fn()
            items.extend(r if isinstance(r, list) else [r])
        except Exception as e:  # noqa: BLE001
            logger.exception("自检项异常: %s", fn)
            items.append({"key": fn.__name__, "label": "检查异常", "level": WARN, "message": str(e)[:100], "detail": "", "fix": None})
    counts = {"ok": 0, "warn": 0, "error": 0, "info": 0}
    for it in items:
        counts[it["level"]] = counts.get(it["level"], 0) + 1
    return {"checked_at": datetime.now().isoformat(timespec="seconds"),
            "summary": counts, "items": items, "quick": quick}


FIXES = ("dirs", "fts", "backup_now", "preview_cache", "requeue", "purge_recycle", "prune_missing",
         "rebuild_bundles")


def repair(action: str) -> dict:
    result = _repair_inner(action)
    invalidate()
    return result


def _repair_inner(action: str) -> dict:
    if action == "dirs":
        settings.ensure_dirs()
        return {"ok": True, "detail": f"目录已创建/修复：{BASE_DIR}"}
    if action == "preview_cache":
        from backend.services.preview import clear_cache

        return {"ok": True, "detail": f"已清理 {clear_cache()} 个预览缓存文件"}
    if action == "requeue":
        from backend.database import SessionLocal
        from backend.models import Document

        with SessionLocal() as db:
            rows = db.query(Document).filter(Document.status == "processing").all()
            for d in rows:
                d.status = "pending"
            db.commit()
            return {"ok": True, "detail": f"已把 {len(rows)} 个卡住的文档退回待整理"}
    if action == "prune_missing":
        from backend.database import SessionLocal
        from backend.models import Document, STATUS_RECYCLED
        from backend.services.recycle import find_file, to_recycle

        n = 0
        with SessionLocal() as db:
            for d in db.query(Document).filter(Document.status != STATUS_RECYCLED).all():
                if find_file(d) is not None:
                    continue
                try:
                    to_recycle(db, d, reason="清理失效记录")
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("清理失效记录失败 id=%s: %s", d.id, exc)
        return {"ok": True, "detail": f"已把 {n} 条文件已丢失的记录移入回收站（可在回收站彻底删除）"}
    if action == "rebuild_bundles":
        from backend.database import SessionLocal

        with SessionLocal() as db:
            from backend.services import bundles as bundle_service

            out = bundle_service.rebuild_all(db)
        return {"ok": True, "detail": (f"已按归档文档重建业务包：{out['bundles']} 个包，"
                                       f"入包 {out['attached']} 份，{out['without_contract_no']} 份无合同编号跳过")}
    if action == "purge_recycle":
        from backend.database import SessionLocal
        from backend.services.recycle import purge_expired

        with SessionLocal() as db:
            n = purge_expired(db)
        return {"ok": True, "detail": f"已清理 {n} 个到期回收项"}
    if action == "fts":
        raw = engine.raw_connection()
        try:
            raw.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
            raw.commit()
        finally:
            raw.close()
        return {"ok": True, "detail": "全文索引已重建"}
    if action == "backup_now":
        from backend.services.backup import create_backup

        info = create_backup(include_documents=False)
        return {"ok": True, "detail": f"已生成 {info['filename']}"}
    return {"ok": False, "detail": f"未知修复动作: {action}"}


# ---- 快速状态缓存：顶栏徽标每 60s 拉一次，避免频繁做重检查 ----
_status_cache: dict = {"ts": 0.0, "data": None}
_status_lock = threading.Lock()
STATUS_TTL = 60.0


def invalidate() -> None:
    with _status_lock:
        _status_cache["data"] = None


def quick_status() -> dict:
    """轻量自检：只回计数与最紧要问题，供全局顶栏显示。"""
    now = time.time()
    with _status_lock:
        cached = _status_cache["data"]
        if cached and now - _status_cache["ts"] < STATUS_TTL:
            return cached
    data = _summarize(run_checks(quick=True))
    with _status_lock:
        _status_cache.update(ts=time.time(), data=data)
    return data


def _summarize(result: dict) -> dict:
    items = result["items"]
    worst = "ok"
    order = {"ok": 0, "info": 1, "warn": 2, "error": 3}
    for it in items:
        if order.get(it["level"], 0) > order[worst]:
            worst = it["level"]
    issues = [{"key": i["key"], "label": i["label"], "level": i["level"], "message": i["message"],
               "fix": i.get("fix")} for i in items if i["level"] in ("warn", "error")]
    return {"level": worst, "summary": result["summary"], "issues": issues[:6],
            "checked_at": result["checked_at"]}