# -*- coding: utf-8 -*-
"""
SQLite 持久化层（JSON/TXT 仅用于首次迁移）。

运行时数据全部存储在根目录 `turb.sqlite3`；旧 JSON/TXT/Codex 文件仅用于一次性迁移。
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import string
import threading
import time
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get("TURB_DATA_DIR") or _PROJECT_ROOT).expanduser().resolve()
_LEGACY_DATA_DIR = _DATA_DIR / "data"
_LOG_DIR = _DATA_DIR / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800

_OUTLOOK_JSON = _DATA_DIR / "用于注册的邮箱.json"
_OUTLOOK_TXT = _DATA_DIR / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _DATA_DIR / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _DATA_DIR / "用于注册的API邮箱.txt"
_ACCOUNTS_JSON = _DATA_DIR / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _DATA_DIR / "注册成功的邮箱.txt"
_TOKENS_TXT = _DATA_DIR / "注册成功的token.txt"
_JOBS_JSON = _DATA_DIR / "注册任务.json"
# 兼容旧测试/外部调用方；静态查看器已停用，不会再写入此路径。
_VIEWER_HTML = _DATA_DIR / "accounts_viewer.html"
_CODEX_DIR = _DATA_DIR / "codex_accounts"
_CODEX_AGENT_DIR = _DATA_DIR / "codex_agent_accounts"
# 仅供一次性迁移旧导出状态，运行期间不再读取该文件。
_LEGACY_CODEX_EXPORT_STATE = _DATA_DIR / "codex_导出状态.json"
# SQLite 是运行时唯一业务数据主存储；旧 JSON/TXT 仅用于一次性迁移。
_SQLITE_PATH = _DATA_DIR / "turb.sqlite3"
_SQLITE_LOCK = threading.RLock()
_SQLITE_READY = False
_TABLES = {
    "accounts": "accounts",
    "outlook": "email_pool",
    "generic_api": "email_pool",
    "imap": "email_pool",
    "jobs": "registration_jobs",
    "domain": "email_pool",
    "codex": "codex_accounts",
}
_EMAIL_SOURCES = {"outlook": "outlook", "generic_api": "generic_api", "imap": "imap", "domain": "cloudflare_domain"}
_LEGACY_TABLES = {"outlook": "outlook_pool", "generic_api": "generic_api_pool", "domain": "domain_email_pool"}

# ---------------------------------------------------------------
# 账号列表的筛选条件下推到 SQLite 索引列。
#
# 套餐/Codex/查活/2FA/分组/AT 状态都存在 accounts.payload 里，直接对 payload 做
# json_extract 会让每次分页都全表解析 JSON（5 万账号约数百毫秒）。这里用 SQLite
# VIRTUAL 生成列把同一批表达式固化下来并建索引：生成列由 SQLite 在写入时求值，
# 不存在“代码忘了同步”的失配问题，任何写路径都自动保持一致。
# ---------------------------------------------------------------

DEFAULT_ACCOUNT_GROUP = "默认分组"

# Python str.strip()/isspace() 认作空白的字符集合（SQLite trim 默认只去 ASCII 空格）。
_PY_WS_CHARS = (
    "\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000 "
)
_SQL_WS_CHARS = "'" + _PY_WS_CHARS.replace("'", "''") + "'"

_TOKEN_EXPIRED_EXPR = "lower(COALESCE(CAST(json_extract(payload, '$.token_expired') AS TEXT), ''))"
_PLAN_NORM_EXPR = (
    "lower(COALESCE(NULLIF(CAST(json_extract(payload, '$.current_plan_type') AS TEXT), ''), "
    "CAST(json_extract(payload, '$.plan_type') AS TEXT), ''))"
)
# “已开通 Plus”在 SQL 里只能写成 LIKE '%plus%'（前导通配符用不上索引），固化成 0/1 列后
# 可以直接等值索引。plan_norm 已经小写，GLOB 与 Python 的 `"plus" in plan and "free" not in plan` 等价。
_PLAN_PLUS_EXPR = (
    "CASE WHEN plan_norm GLOB '*plus*' AND plan_norm NOT GLOB '*free*' THEN 1 ELSE 0 END"
)
# julianday 口径的“AT 何时算过期”：
#   0.0 = 已标记过期（无论时间），5373484.5 = 9999-12-31（标记为未过期且无时间戳），
#   NULL = 无法判断。与 _account_at_state 的判定优先级完全一致。
_AT_NEVER_EXPIRES_JULIAN = 5373484.5
# CASE <值> WHEN ... 只求值一次 json_extract（原来的 IN (...) 形式要求值两次）。
_AT_FLAG_EXPR = (
    f"CASE {_TOKEN_EXPIRED_EXPR} "
    "WHEN '1' THEN 1 WHEN 'true' THEN 1 WHEN 'yes' THEN 1 WHEN 'on' THEN 1 "
    "WHEN '0' THEN 0 WHEN 'false' THEN 0 WHEN 'no' THEN 0 WHEN 'off' THEN 0 "
    "ELSE NULL END"
)
_AT_EXPIRES_TS_EXPR = "julianday(json_extract(payload, '$.token_expires_at'))"
# 直接引用前面两个生成列，避免同一行把 payload 反复解析。
_AT_STATE_EXP_EXPR = (
    f"CASE WHEN at_flag = 1 THEN 0.0 "
    "WHEN at_expires_ts IS NOT NULL THEN at_expires_ts "
    f"WHEN at_flag = 0 THEN {_AT_NEVER_EXPIRES_JULIAN} "
    "ELSE NULL END"
)
_GROUP_KEY_EXPR = (
    "COALESCE(NULLIF(trim(COALESCE(CAST(json_extract(payload, '$.group_name') AS TEXT), ''), "
    f"{_SQL_WS_CHARS}), ''), '{DEFAULT_ACCOUNT_GROUP}')"
)
_TOTP_FLAG_EXPR = (
    "CASE WHEN length(trim(lower(COALESCE(CAST(json_extract(payload, '$.totp_secret') AS TEXT), '')))) > 0 "
    "THEN 1 ELSE 0 END"
)


# 与 _extract_registration_password 对齐：extra_json 既可能是对象，也可能是 JSON 字符串；
# Python 用 `extra.get(k) or row.get(k) or ""` 取值，所以先按“真值”逐级回退，最后统一 strip。
# 多路径 json_extract 只解析一次 payload，返回 [extra.registration_password,
# registration_password, extra_json] 三个槽位，后续都只解析这个小数组。
_PASSWORD_SLOTS_EXPR = (
    "json_extract(payload, '$.extra_json.registration_password', "
    "'$.registration_password', '$.extra_json')"
)


def _password_slot_expr(slot: int) -> str:
    value = f"json_extract({_PASSWORD_SLOTS_EXPR}, '$[{slot}]')"
    # Python 里空对象/空数组是假值，SQL 取出来是 '{}' / '[]' 这样的真值文本，需要显式排除。
    return (
        f"CASE WHEN {value} IS NOT NULL AND {value} <> '' AND {value} <> 0 "
        f"AND json_type({_PASSWORD_SLOTS_EXPR}, '$[{slot}]') NOT IN ('object', 'array') "
        f"THEN CAST({value} AS TEXT) END"
    )


def _password_field_expr(base: str, path: str) -> str:
    value = f"json_extract({base}, '{path}')"
    return (
        f"CASE WHEN {value} IS NOT NULL AND {value} <> '' AND {value} <> 0 "
        f"AND json_type({base}, '{path}') NOT IN ('object', 'array') "
        f"THEN CAST({value} AS TEXT) END"
    )


_EXTRA_JSON_SLOT_EXPR = f"json_extract({_PASSWORD_SLOTS_EXPR}, '$[2]')"
_ROW_PASSWORD_EXPR = (
    "COALESCE("
    + _password_slot_expr(0)
    + f", CASE WHEN json_valid({_EXTRA_JSON_SLOT_EXPR}) THEN "
    + _password_field_expr(_EXTRA_JSON_SLOT_EXPR, "$.registration_password")
    + " END"
    + ", "
    + _password_slot_expr(1)
    + ")"
)
_HAS_PASSWORD_EXPR = (
    f"CASE WHEN length(trim({_ROW_PASSWORD_EXPR}, {_SQL_WS_CHARS})) > 0 THEN 1 ELSE 0 END"
)
# 账号是否有可兑换凭据：生成列只覆盖 SQL 能判定的部分，真正的兑换仍走 Python 复核。
_ACCOUNT_GENERATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plan_norm", _PLAN_NORM_EXPR),
    ("plan_plus", _PLAN_PLUS_EXPR),
    ("codex_norm", "lower(COALESCE(CAST(json_extract(payload, '$.codex_status') AS TEXT), ''))"),
    ("live_norm", "lower(COALESCE(CAST(json_extract(payload, '$.live_check_status') AS TEXT), ''))"),
    ("totp_flag", _TOTP_FLAG_EXPR),
    ("group_key", _GROUP_KEY_EXPR),
    ("email_key", f"lower(trim(email, {_SQL_WS_CHARS}))"),
    (
        "orig_email_key",
        f"lower(trim(COALESCE(CAST(json_extract(payload, '$.original_email') AS TEXT), ''), {_SQL_WS_CHARS}))",
    ),
    ("has_password", _HAS_PASSWORD_EXPR),
    ("at_flag", _AT_FLAG_EXPR),
    ("at_expires_ts", _AT_EXPIRES_TS_EXPR),
    ("at_state_exp", _AT_STATE_EXP_EXPR),
)
# (archived, <筛选列>, id DESC, updated_at) 同时服务分页（按 id 倒序取一页）与
# COUNT/MAX(updated_at) 聚合（覆盖索引，无需回表）。
_ACCOUNT_FILTER_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_accounts_archived_page", "(archived, id DESC, updated_at)"),
    ("idx_accounts_archived_plan", "(archived, plan_norm, id DESC, updated_at)"),
    ("idx_accounts_archived_plan_plus", "(archived, plan_plus, id DESC, updated_at)"),
    ("idx_accounts_archived_codex", "(archived, codex_norm, id DESC, updated_at)"),
    ("idx_accounts_archived_live", "(archived, live_norm, id DESC, updated_at)"),
    ("idx_accounts_archived_totp", "(archived, totp_flag, id DESC, updated_at)"),
    ("idx_accounts_archived_group", "(archived, group_key, id DESC, updated_at)"),
    ("idx_accounts_archived_at", "(archived, at_state_exp, id DESC, updated_at)"),
    # 注册成功时间独立于 created_at：后者是本地账号行首次落库时间，可能晚于
    # 远端注册完成（例如注册后还要跑 2FA/Codex）。
    ("idx_accounts_archived_registered", "(archived, registered_at, updated_at)"),
    # 按入库时间筛选：equality(archived) + range(created_at) + 覆盖 updated_at，
    # 否则 COUNT/MAX 要为每一行回表取 payload/updated_at（5 万行约 85ms）。
    ("idx_accounts_archived_created", "(archived, created_at, updated_at)"),
    ("idx_accounts_at_state_exp", "(at_state_exp)"),
    ("idx_accounts_email_key", "(email_key)"),
    ("idx_accounts_orig_email_key", "(orig_email_key)"),
    # 分组统计：GROUP BY group_key 走索引即有序，可兑换数则在部分覆盖索引上判定
    # （生成列的值已存进索引，扫描时不必再解析 payload）。
    ("idx_accounts_group_key", "(group_key)"),
    (
        "idx_accounts_redeemable",
        "(group_key, has_password, live_norm, email) WHERE archived=0",
    ),
)
# 一次性回填：早期入库的账号只有 access_token，没有 token_expires_at/token_expired。
# 补上这两项（与 insert_account 的约定一致）后，AT 状态无需再逐行回调 Python 解析 JWT。
_AT_BACKFILL_KEY = "account_at_expiry_backfill_v1"
# 生成列表达式集合的指纹，用于在升级后重建定义有变化的列。
_ACCOUNT_FILTER_SCHEMA_KEY = "account_filter_schema_v1"

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()
_DEFAULT_SQLITE_PATH = _SQLITE_PATH
_DEFAULT_ACCOUNTS_JSON = _ACCOUNTS_JSON
_DEFAULT_OUTLOOK_JSON = _OUTLOOK_JSON
_DEFAULT_JOBS_JSON = _JOBS_JSON
_SQLITE_READY_PATH: Path | None = None
_STORAGE_BOUND = False


def configure_storage(data_dir: str | Path) -> None:
    """Bind this process to one data directory before any database work starts."""
    global _STORAGE_BOUND
    root = Path(data_dir).expanduser().resolve()
    with _LOCK, _SQLITE_LOCK:
        if root == _DATA_DIR:
            _STORAGE_BOUND = True
            return
        if _SQLITE_READY or _STORAGE_BOUND:
            raise RuntimeError("数据库已绑定，不能在同一进程中切换数据目录")
        updates = {}
        for name, value in tuple(globals().items()):
            if name == "_PROJECT_ROOT" or not name.isupper() or not isinstance(value, Path):
                continue
            try:
                updates[name] = root / value.relative_to(_DATA_DIR)
            except ValueError:
                continue
        globals().update(updates)
        _STORAGE_BOUND = True


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_ascii(value: str) -> bool:
    """SQLite 的 lower()/NOCASE 只处理 ASCII；非 ASCII 需要回退到 Python 的 lower()/casefold()。"""
    try:
        str(value).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _jwt_expiry_seconds(token: object) -> float | None:
    """仅本地解析 JWT payload 的 exp（不校验签名）；非 JWT 或缺少 exp 时返回 None。"""
    text = str(token or "").strip().strip('"').strip("'")
    if not text:
        return None
    parts = text.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def _iso_utc_timestamp(value: object) -> float | None:
    """把 ISO 时间串转成 UTC 时间戳；无时区后缀按 UTC 解释，与 SQLite julianday 一致。"""
    parsed = _parse_iso_dt(str(value or ""))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _account_at_state(row: dict) -> str:
    """判断账号 AT 状态：expired（已过期）/ valid（未过期）/ unknown（信息不足）。

    与 `_account_at_state_sql` 保持同一口径：先看套餐查询写回的 token_expired，
    再看 token_expires_at 是否已到点（时间流逝会让旧结果失效），最后回退到直接解析
    当前 access_token 的 exp，避免从未查过套餐的账号无法筛选。
    """
    flag = row.get("token_expired")
    if isinstance(flag, str):
        normalized = flag.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            flag = True
        elif normalized in {"0", "false", "no", "off"}:
            flag = False
        else:
            flag = None
    if flag is True:
        return "expired"
    expires_at = _iso_utc_timestamp(row.get("token_expires_at"))
    if expires_at is not None:
        return "expired" if time.time() >= expires_at else "valid"
    if flag is False:
        return "valid"
    derived = _jwt_expiry_seconds(row.get("access_token"))
    if derived is None:
        return "unknown"
    return "expired" if time.time() >= derived else "valid"


def _account_at_expired_sql(token: object) -> int | None:
    """SQLite 回调（account_at_expired）：1=已过期，0=未过期，NULL=无法判断。"""
    exp = _jwt_expiry_seconds(token)
    if exp is None:
        return None
    return 1 if time.time() >= exp else 0


def _refresh_token_expiry(row: dict) -> bool:
    """按最新 access_token 刷新 token_expired/token_expires_at，返回是否写入。

    查活成功会换发新 AT，若不刷新，旧 AT 留下的“已过期”标记会让账号一直被筛选为过期。
    """
    exp = _jwt_expiry_seconds(row.get("access_token"))
    if exp is None:
        return False
    row["token_expires_at"] = (
        datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )
    row["token_expired"] = time.time() >= exp
    return True


def _sqlite_conn() -> sqlite3.Connection:
    """创建短生命周期连接；WAL 允许 WebUI 读与注册线程写并行。"""
    _ensure_storage()
    _active_sqlite_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_active_sqlite_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    # Use the same Unicode whitespace handling and exact identity as Python.
    conn.create_function("account_group_name", 1, lambda value: _account_group_name({"group_name": value}), deterministic=True)
    # AT 是否过期需要解码 JWT，SQLite 无法只用 SQL 完成；仅在没有已存过期信息时才回调。
    conn.create_function("account_at_expired", 1, _account_at_expired_sql, deterministic=True)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    """一次查询作用域内的连接：让筛选探测与分页共用同一个连接。"""
    _ensure_sqlite()
    conn = _sqlite_conn()
    try:
        yield conn
    finally:
        conn.close()


def _active_sqlite_path() -> Path:
    """测试替换旧 JSON 路径时使用同目录数据库，避免污染正式库。"""
    if (
        _ACCOUNTS_JSON != _DEFAULT_ACCOUNTS_JSON
        or _OUTLOOK_JSON != _DEFAULT_OUTLOOK_JSON
        or _JOBS_JSON != _DEFAULT_JOBS_JSON
    ):
        return _ACCOUNTS_JSON.parent / "turb.sqlite3"
    return _SQLITE_PATH


def _read_legacy_sqlite_collection(collection: str) -> list[dict] | None:
    """读取旧 data/registrations.db 的数据，仅在一次性迁移阶段调用。"""
    if not _LEGACY_SQLITE.exists():
        return None
    try:
        with closing(sqlite3.connect(str(_LEGACY_SQLITE))) as legacy_conn:
            legacy_conn.row_factory = sqlite3.Row
            table = "registered_accounts" if collection == "accounts" else "outlook_pool" if collection == "outlook" else ""
            if not table or not _table_exists(legacy_conn, table):
                return None
            return [dict(row) for row in legacy_conn.execute(f"SELECT * FROM {table}").fetchall()]
    except Exception:
        return None


def _existing_indexes(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _ensure_query_indexes(conn: sqlite3.Connection) -> None:
    """补齐其余列表页需要的覆盖索引（分页排序 + COUNT/MAX(updated_at) 聚合）。

    email_pool / registration_jobs 的列表接口同样会执行 COUNT(*) 与 MAX(updated_at)；
    updated_at 不进索引时这两项都是整表扫描（10 万行约 40ms）。
    """
    columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_xinfo(codex_accounts)")}
    if "exported_count" not in columns:
        conn.execute(
            "ALTER TABLE codex_accounts ADD COLUMN exported_count INTEGER GENERATED ALWAYS AS "
            "(COALESCE(CAST(json_extract(payload, '$._exported_count') AS INTEGER), 0)) VIRTUAL"
        )
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_codex_accounts_exported ON codex_accounts(archived, exported_count)",
        "CREATE INDEX IF NOT EXISTS idx_email_pool_source_updated ON email_pool(source, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_email_pool_updated ON email_pool(updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_email_pool_source_created ON email_pool(source, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_email_pool_created_id ON email_pool(created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_registration_jobs_updated ON registration_jobs(updated_at DESC)",
    )
    existing = _existing_indexes(conn)
    created = False
    for statement in statements:
        name = statement.split(" IF NOT EXISTS ", 1)[1].split(" ON ", 1)[0]
        if name in existing:
            continue
        conn.execute(statement)
        created = True
    if created:
        # 新索引会让优化器手里的基数失效（例如 email_pool 改走建表时的老索引），统一重算。
        for table in ("codex_accounts", "email_pool", "registration_jobs"):
            conn.execute(f"ANALYZE {table}")


def _ensure_account_registration_schema(conn: sqlite3.Connection) -> None:
    """为账号补齐注册完成时间，并兼容已有数据库/旧 payload。

    旧版本只有 ``created_at``，它表示账号首次写入本地存储的时间。历史记录
    没有可恢复的远端注册时间，因此用 ``created_at`` 作为一次性兼容回填；新
    注册流程会在拿到有效登录会话后显式写入 ``registered_at``。
    """
    columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_info(accounts)")}
    if "registered_at" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN registered_at TEXT NOT NULL DEFAULT ''")

    # 先读取旧 payload 中可能已经存在的字段，再退回表级 created_at。
    conn.execute(
        """UPDATE accounts
           SET registered_at = COALESCE(
               NULLIF(trim(registered_at), ''),
               CASE WHEN json_valid(payload)
                    THEN NULLIF(trim(CAST(json_extract(payload, '$.registered_at') AS TEXT)), '')
                    ELSE NULL END,
               NULLIF(trim(created_at), ''),
               ''
           )
         WHERE trim(COALESCE(registered_at, '')) = ''"""
    )
    # 让从 payload 读取的旧/外部账号也能在后续保存时保留该字段。
    conn.execute(
        """UPDATE accounts
           SET payload = json_set(payload, '$.registered_at', registered_at)
         WHERE registered_at <> '' AND json_valid(payload)
           AND COALESCE(CAST(json_extract(payload, '$.registered_at') AS TEXT), '') <> registered_at"""
    )


def _repair_email_pool_sources(conn: sqlite3.Connection) -> None:
    """一次性修复历史 email_pool 行的空 source（三条 UPDATE 都要全表扫描）。

    只针对旧版本写入时漏掉 source 列的行，按素材特征判定来源，避免误分类域名邮箱。
    """
    conn.execute(
        "UPDATE email_pool SET source=? "
        "WHERE (source IS NULL OR trim(source)='') AND ("
        "json_extract(payload, '$.code_url') IS NOT NULL OR "
        "json_extract(payload, '$.url') IS NOT NULL OR "
        "json_extract(payload, '$.source') IN ('generic_api', 'generic-api') OR "
        "json_extract(payload, '$.email_source') IN ('generic_api', 'generic-api')"
        ")",
        (_EMAIL_SOURCES["generic_api"],),
    )
    conn.execute(
        "UPDATE email_pool SET source=? "
        "WHERE (source IS NULL OR trim(source)='') AND ("
        "json_extract(payload, '$.client_id') IS NOT NULL OR "
        "json_extract(payload, '$.clientId') IS NOT NULL OR "
        "json_extract(payload, '$.refresh_token') IS NOT NULL OR "
        "json_extract(payload, '$.refreshToken') IS NOT NULL OR "
        "json_extract(payload, '$.source') IN ('outlook', 'outlook_pool') OR "
        "json_extract(payload, '$.email_source') = 'outlook'"
        ")",
        (_EMAIL_SOURCES["outlook"],),
    )
    # 域名邮箱的历史 payload 没有 client_id/code_url 等特征，剩余的空来源
    # 记录只能归入域名邮箱池。否则它们会在“全部邮箱池”中显示为未知来源，
    # 前端又会按 Outlook 处理，导致列表里能看到但删除/改状态找不到。
    conn.execute(
        "UPDATE email_pool SET source=? "
        "WHERE (source IS NULL OR trim(source)='') AND COALESCE(("
        "json_extract(payload, '$.code_url') IS NOT NULL OR "
        "json_extract(payload, '$.url') IS NOT NULL OR "
        "json_extract(payload, '$.source') IN ('generic_api', 'generic-api', 'outlook', 'outlook_pool') OR "
        "json_extract(payload, '$.email_source') IN ('generic_api', 'generic-api', 'outlook') OR "
        "json_extract(payload, '$.client_id') IS NOT NULL OR "
        "json_extract(payload, '$.clientId') IS NOT NULL OR "
        "json_extract(payload, '$.refresh_token') IS NOT NULL OR "
        "json_extract(payload, '$.refreshToken') IS NOT NULL"
        "), 0)=0",
        (_EMAIL_SOURCES["domain"],),
    )


def _ensure_account_filter_schema(conn: sqlite3.Connection) -> None:
    """把账号列表的筛选表达式固化成生成列 + 覆盖索引，并回填历史 AT 过期信息。

    生成列是 VIRTUAL 的：SQLite 负责在任何 INSERT/UPDATE 后保持一致，因此旧代码
    路径（runtime/scan_payment_store/plus_activation_store 等直接写 payload 的地方）
    也不需要改动。索引让分页排序与 COUNT/MAX(updated_at) 聚合都走覆盖索引。

    表达式集合的指纹存在 storage_meta：升级后表达式有变化时一次性重建生成列与索引，
    避免旧定义留在一个已经建了索引的库里造成筛选结果与代码不一致。
    """
    digest = hashlib.sha1(
        "\n".join(f"{name}={expression}" for name, expression in _ACCOUNT_GENERATED_COLUMNS).encode("utf-8")
    ).hexdigest()[:16]
    recorded = conn.execute(
        "SELECT value FROM storage_meta WHERE key=?", (_ACCOUNT_FILTER_SCHEMA_KEY,)
    ).fetchone()
    columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_xinfo(accounts)")}
    if recorded is not None and str(recorded[0]) != digest and any(name in columns for name, _ in _ACCOUNT_GENERATED_COLUMNS):
        _rebuild_account_filter_columns(conn)
        columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_xinfo(accounts)")}

    changed = recorded is None or str(recorded[0]) != digest
    for name, expression in _ACCOUNT_GENERATED_COLUMNS:
        if name in columns:
            continue
        _execute_schema_statement(
            conn, f"ALTER TABLE accounts ADD COLUMN {name} GENERATED ALWAYS AS ({expression}) VIRTUAL"
        )
        changed = True
    existing = _existing_indexes(conn)
    for name, definition in _ACCOUNT_FILTER_INDEXES:
        if name in existing:
            continue
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON accounts{definition}")
        changed = True
    if changed:
        # 新建索引后让统计信息跟上，避免优化器仍按旧基数选择全表扫描。
        conn.execute("ANALYZE accounts")
    conn.execute(
        "INSERT OR REPLACE INTO storage_meta(key, value) VALUES(?, ?)",
        (_ACCOUNT_FILTER_SCHEMA_KEY, digest),
    )
    if conn.execute("SELECT 1 FROM storage_meta WHERE key=? LIMIT 1", (_AT_BACKFILL_KEY,)).fetchone():
        return
    backfilled = _backfill_account_at_expiry(conn)
    conn.execute(
        "INSERT OR REPLACE INTO storage_meta(key, value) VALUES(?, ?)",
        (_AT_BACKFILL_KEY, f"{_now()} rows={backfilled}"),
    )


def _quote_sql_name(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _drop_indexes_referencing_generated_columns(conn: sqlite3.Connection) -> None:
    """删除 accounts 上所有引用生成列的索引，而不只是当前代码里的固定清单。

    表达式变化后的重建流程必须先把引用生成列的索引删掉，否则
    ``ALTER TABLE accounts DROP COLUMN`` 会直接失败：

        sqlite3.OperationalError: error in index <索引名> after drop column: no such column: <列名>

    历史版本留下的索引（改过名字、已经不在 ``_ACCOUNT_FILTER_INDEXES`` 里）正好属于
    这种情况。异常会从 ``_ensure_sqlite()`` 抛到每个接口，``storage_meta`` 里的指纹
    也写不进去，于是**所有**依赖数据库的接口每次都返回 500（Flask 返回 HTML 错误页，
    前端只能显示「服务响应异常（500）」，账号页就是「分组加载失败：服务响应异常（500）」）。
    所以这里按索引定义识别引用关系，把这类索引一并清理，而不是只认名字。
    """
    generated = {name.lower() for name, _ in _ACCOUNT_GENERATED_COLUMNS}
    if not generated:
        return
    rows = conn.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master WHERE type='index' AND tbl_name='accounts'"
    ).fetchall()
    for raw_name, raw_sql in rows:
        name = str(raw_name)
        if name.startswith("sqlite_autoindex"):
            continue
        tokens = {token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(raw_sql or ""))}
        if tokens & generated:
            conn.execute(f"DROP INDEX IF EXISTS {_quote_sql_name(name)}")


def _execute_schema_statement(conn: sqlite3.Connection, statement: str) -> None:
    """执行重建用的 DDL；若 SQLite 点名某个历史索引，删掉该索引后重试。

    ``ALTER TABLE ... ADD/DROP COLUMN`` 会重新解析整张表的 schema，只要表上还挂着
    一个引用缺失列的旧索引，SQLite 就会报 ``error in index <索引名>`` 让 DDL 失败，
    异常一路冒到接口，表现为所有请求 500。报错信息里带了索引名，按名字清理后重试
    即可自愈。
    """
    for _attempt in range(8):
        try:
            conn.execute(statement)
            return
        except sqlite3.OperationalError as exc:
            match = re.search(r"error in index\s+([^\s:]+)", str(exc))
            if not match:
                raise
            conn.execute(f"DROP INDEX IF EXISTS {_quote_sql_name(match.group(1))}")


def _rebuild_account_filter_columns(conn: sqlite3.Connection) -> None:
    """表达式变化时重建生成列：先删索引，再按依赖倒序删列，随后由调用方重新创建。"""
    for name, _definition in _ACCOUNT_FILTER_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    _drop_indexes_referencing_generated_columns(conn)
    columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_xinfo(accounts)")}
    for name, _expression in reversed(_ACCOUNT_GENERATED_COLUMNS):
        if name in columns:
            _execute_schema_statement(conn, f"ALTER TABLE accounts DROP COLUMN {_quote_sql_name(name)}")


def _backfill_account_at_expiry(conn: sqlite3.Connection) -> int:
    """为只有 access_token 的历史账号补写 token_expires_at/token_expired。

    这两项是 insert_account 一直以来的约定；补写后 AT 筛选不必再逐行回调 Python
    解析 JWT，也不会改变任何账号的 AT 状态（_account_at_state 优先级不变）。
    """
    updated = 0
    cursor = conn.execute(
        "SELECT id, payload FROM accounts WHERE at_state_exp IS NULL"
    )
    while True:
        batch = cursor.fetchmany(200)
        if not batch:
            break
        rows: list[tuple[str, int]] = []
        for raw in batch:
            try:
                row = json.loads(raw["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            if not _refresh_token_expiry(row):
                continue
            rows.append((json.dumps(row, ensure_ascii=False), int(raw["id"])))
        if rows:
            conn.executemany("UPDATE accounts SET payload=? WHERE id=?", rows)
            updated += len(rows)
    return updated


def _ensure_sqlite() -> None:
    """首次运行将现有 JSON 一次性导入 SQLite，之后 SQLite 为唯一读写源。"""
    global _SQLITE_READY, _SQLITE_READY_PATH
    active_path = _active_sqlite_path()
    if _SQLITE_READY and _SQLITE_READY_PATH == active_path:
        return
    with _SQLITE_LOCK:
        active_path = _active_sqlite_path()
        if _SQLITE_READY and _SQLITE_READY_PATH == active_path:
            return
        conn = _sqlite_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                registered_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                PRIMARY KEY (id)
            );
            CREATE TABLE IF NOT EXISTS email_pool (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registration_jobs AS SELECT * FROM accounts WHERE 0;
            CREATE TABLE IF NOT EXISTS codex_accounts (
                id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL UNIQUE, email TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codex_agent_accounts (
                account_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS storage_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS redeem_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                quantity INTEGER NOT NULL DEFAULT 1,
                redeemed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                note TEXT NOT NULL DEFAULT '',
                account_group TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS redeem_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL UNIQUE,
                email TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '',
                UNIQUE(code_id, account_id)
            );
            CREATE TABLE IF NOT EXISTS account_groups (
                group_name TEXT PRIMARY KEY,
                redeem_prefix TEXT NOT NULL DEFAULT '',
                public_stock INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS extract_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                provider_type TEXT NOT NULL DEFAULT 'extract',
                api_base TEXT NOT NULL DEFAULT '',
                default_link_type TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS extract_provider_cdks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                cdk TEXT NOT NULL,
                memo TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_id, cdk)
            );
            CREATE TABLE IF NOT EXISTS payment_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                provider_type TEXT NOT NULL,
                api_base TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS payment_cdks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                cdk TEXT NOT NULL,
                memo TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_id, cdk)
            );
        """)
        _ensure_account_registration_schema(conn)
        for table in {"accounts", "email_pool", "registration_jobs"}:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status, id DESC)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_archived ON {table}(archived, id DESC)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_email ON {table}(email COLLATE NOCASE)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_created ON {table}(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_registration_jobs_id ON registration_jobs(id)")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_registration_jobs_successful_retry
            ON registration_jobs(COALESCE(CAST(json_extract(payload, '$.root_job_id') AS INTEGER), 0), id DESC)
            WHERE status='success'""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_pool_source_status ON email_pool(source, status, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_archived ON codex_accounts(archived, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_email ON codex_accounts(email COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_created ON codex_accounts(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_agent_accounts_email ON codex_agent_accounts(email COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_agent_accounts_updated ON codex_agent_accounts(updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_redeem_codes_status ON redeem_codes(status, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_redeem_codes_created ON redeem_codes(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_redeem_claims_code ON redeem_claims(code_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_redeem_claims_account ON redeem_claims(account_id)")
        _ensure_account_filter_schema(conn)
        _ensure_query_indexes(conn)
        _group_columns = {r[1].lower() for r in conn.execute("PRAGMA table_info(account_groups)").fetchall()}
        for _column, _definition in (
            ("redeem_prefix", "TEXT NOT NULL DEFAULT ''"),
            ("public_stock", "INTEGER NOT NULL DEFAULT 0"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if _column not in _group_columns:
                conn.execute(f"ALTER TABLE account_groups ADD COLUMN {_column} {_definition}")
        _redeem_columns = {r[1].lower() for r in conn.execute("PRAGMA table_info(redeem_codes)").fetchall()}
        if "account_group" not in _redeem_columns:
            conn.execute("ALTER TABLE redeem_codes ADD COLUMN account_group TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO account_groups(group_name,redeem_prefix,public_stock,created_at,updated_at) VALUES(?,?,?,?,?)",
            (DEFAULT_ACCOUNT_GROUP, "", 0, _now(), _now()),
        )
        migration_done = conn.execute(
            "SELECT 1 FROM storage_meta WHERE key='legacy_import_completed' LIMIT 1"
        ).fetchone()
        # 迁移标记写入 SQLite，而不是依赖“表是否为空”。这样用户删除全部数据后，
        # 重启也不会再次从旧 JSON 恢复已删除的数据。
        if not migration_done:
            sources = {
                "accounts": (_ACCOUNTS_JSON, _LEGACY_ACCOUNTS_JSON),
                "outlook": (_OUTLOOK_JSON, _LEGACY_OUTLOOK_JSON),
                "generic_api": (_GENERIC_API_EMAIL_JSON,),
                "jobs": (_JOBS_JSON, _LEGACY_JOBS_JSON),
                "domain": (_DOMAIN_EMAIL_JSON,),
            }
            for collection, paths in sources.items():
                table = _TABLES[collection]
                exists = conn.execute(
                    f"SELECT 1 FROM {table}" + (" WHERE source=?" if table == "email_pool" else " LIMIT 1"),
                    ((_EMAIL_SOURCES[collection],) if table == "email_pool" else ()),
                ).fetchone()
                if exists:
                    continue
                rows = None
            # 兼容上一版“records 单表 + collection”实现。
                if _table_exists(conn, "records"):
                    legacy = conn.execute("SELECT payload FROM records WHERE collection=? ORDER BY id", (collection,)).fetchall()
                    if legacy:
                        rows = [json.loads(item["payload"]) for item in legacy]
            # 兼容上一版按邮箱来源拆分的三张表。
                if collection in _EMAIL_SOURCES and rows is None:
                    old_table = _LEGACY_TABLES[collection]
                    if _table_exists(conn, old_table):
                        legacy = conn.execute(f"SELECT payload FROM {old_table} ORDER BY id").fetchall()
                        if legacy:
                            rows = [json.loads(item["payload"]) for item in legacy]
                for path in paths:
                    if rows is None and path.exists():
                        candidate = _read_json(path, None)
                        if isinstance(candidate, list):
                            rows = candidate
                            break
                if rows is None:
                    rows = _read_legacy_sqlite_collection(collection)
                if not rows:
                    continue
                next_email_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM email_pool").fetchone()[0]) + 1 if table == "email_pool" else 0
                for pos, row in enumerate(rows, 1):
                    row = dict(row)
                    rid = next_email_id if table == "email_pool" else int(row.get("id") or pos)
                    if table == "email_pool":
                        next_email_id += 1
                    row["id"] = rid
                    if table == "accounts":
                        row["registered_at"] = str(row.get("registered_at") or row.get("created_at") or "")
                    conn.execute(
                        (
                            f"INSERT OR REPLACE INTO {table}(id,email,source,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?)"
                            if table == "email_pool" else
                            f"INSERT OR REPLACE INTO {table}(id,email,status,archived,created_at,registered_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?)"
                            if table == "accounts" else
                            f"INSERT OR REPLACE INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)"
                        ),
                        (
                            (rid, str(row.get("email") or ""), _EMAIL_SOURCES[collection], str(row.get("status") or ""),
                             int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                             str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False))
                            if table == "email_pool" else
                            (rid, str(row.get("email") or ""), str(row.get("status") or ""),
                             int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                             str(row.get("registered_at") or ""), str(row.get("updated_at") or ""),
                             json.dumps(row, ensure_ascii=False))
                            if table == "accounts" else
                            (rid, str(row.get("email") or ""), str(row.get("status") or ""),
                             int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                             str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False))
                        ),
                    )
        # 兼容早期 SQLite 版本的保存逻辑：旧版本写入 email_pool 时漏掉了 source 列，
        # 导致通用 API 邮箱在“全部邮箱池”里没有类型、按来源筛选也查不到。
        # 先用 source 索引判断是否真的存在空来源行：修复语句要全表扫描 email_pool
        # （10 万行约 200ms），而正常库里一行都没有，不该每次启动都付这个代价。
        if conn.execute(
            "SELECT 1 FROM email_pool WHERE source IS NULL OR source='' LIMIT 1"
        ).fetchone():
            _repair_email_pool_sources(conn)
        # CPA Codex 凭证首次导入数据库；后续列表查询不再扫描 codex_accounts/ 文件。
        if not migration_done and not conn.execute("SELECT 1 FROM codex_accounts LIMIT 1").fetchone() and _CODEX_DIR.exists():
            state = _read_json(_LEGACY_CODEX_EXPORT_STATE, {})
            state = state if isinstance(state, dict) else {}
            for pos, path in enumerate(sorted(_CODEX_DIR.glob("codex-*.json")), 1):
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                    stat = path.stat()
                except Exception:
                    continue
                filename = path.name
                meta = dict(content)
                meta["_filename"] = filename
                meta["_size"] = stat.st_size
                meta["_mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                es = state.get(filename) or {}
                meta["_exported_at"] = es.get("exported_at")
                meta["_exported_count"] = es.get("exported_count", 0)
                meta["_archived"] = bool(es.get("archived"))
                conn.execute(
                    "INSERT OR IGNORE INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                    (pos, filename, str(content.get("email") or ""), int(meta["_archived"]), meta["_mtime"], meta["_mtime"], json.dumps(meta, ensure_ascii=False)),
                )
        # Agent 凭证也只在首次迁移时读取；运行期间完整内容保存在 SQLite。
        if not migration_done and not conn.execute("SELECT 1 FROM codex_agent_accounts LIMIT 1").fetchone() and _CODEX_AGENT_DIR.exists():
            for path in sorted(_CODEX_AGENT_DIR.glob("codex-agent-*.json")):
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                    stat = path.stat()
                except Exception:
                    continue
                identity = content.get("agent_identity") if isinstance(content.get("agent_identity"), dict) else {}
                email = str(content.get("email") or identity.get("email") or "").strip()
                account = conn.execute("SELECT id, payload FROM accounts WHERE lower(email)=lower(?) LIMIT 1", (email,)).fetchone() if email else None
                if not account:
                    continue
                account_id = int(account["id"])
                stamp = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT OR IGNORE INTO codex_agent_accounts(account_id,email,filename,created_at,updated_at,payload) VALUES(?,?,?,?,?,?)",
                    (account_id, email or str(json.loads(account["payload"]).get("email") or ""), path.name, stamp, stamp, json.dumps(content, ensure_ascii=False)),
                )
                account_payload = json.loads(account["payload"])
                account_payload.setdefault("codex_agent_token", json.dumps(content, ensure_ascii=False))
                account_payload.pop("codex_agent_auth_path", None)
                conn.execute("UPDATE accounts SET payload=?, updated_at=? WHERE id=?", (json.dumps(account_payload, ensure_ascii=False), stamp, account_id))
        conn.commit()
        # 迁移完成后删除旧的通用表，避免运行时继续依赖它。
        for old_table in (*_LEGACY_TABLES.values(), "records"):
            if _table_exists(conn, old_table) and old_table not in _TABLES.values():
                conn.execute(f"DROP TABLE {old_table}")
        if not migration_done:
            conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_import_completed', ?)", (_now(),))
        from core import task_center_store
        task_center_store._initialize(conn)
        conn.commit()
        conn.close()
        _SQLITE_READY = True
        _SQLITE_READY_PATH = active_path


def _load_collection(collection: str) -> list[dict]:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn:
        with conn:
            sql = f"SELECT payload FROM {table}"
            params: tuple[str, ...] = ()
            if table == "email_pool":
                sql += " WHERE source=?"; params = (_EMAIL_SOURCES[collection],)
            sql += " ORDER BY id"
            return [json.loads(row["payload"]) for row in conn.execute(sql, params)]


def _save_collection(collection: str, rows: list[dict]) -> None:
    if collection == "accounts":
        # Batch callers supply the complete account snapshot. Only changed rows
        # are written, so each task hook sees the real pre-write state.
        from core import task_center_store
        with _row_write_transaction() as conn:
            existing = {int(item["id"]): item["payload"] for item in conn.execute("SELECT id,payload FROM accounts")}
            retained = set()
            for pos, raw in enumerate(rows, 1):
                row = dict(raw)
                row["id"] = int(row.get("id") or pos)
                row["group_name"] = _account_group_name(row)
                row["registered_at"] = _account_registered_at(row)
                row["copy_line"] = _account_line(row)
                retained.add(row["id"])
                previous = existing.get(row["id"])
                if previous is not None and json.loads(previous) == row:
                    continue
                _write_collection_row(conn, "accounts", row, insert=previous is None)
            for account_id in existing.keys() - retained:
                task_center_store.end_account_tasks(conn, account_id)
                conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return
    _ensure_sqlite()
    table = _TABLES[collection]
    source = _EMAIL_SOURCES[collection] if table == "email_pool" else None
    with closing(_sqlite_conn()) as conn:
        with conn:
            if table == "email_pool":
                conn.execute("DELETE FROM email_pool WHERE source=?", (source,))
            else:
                conn.execute(f"DELETE FROM {table}")
            for pos, raw in enumerate(rows, 1):
                row = dict(raw)
                if table == "accounts":
                    # 老账号 payload 里没有 group_name；落库时补默认分组，
                    # 保证 SQL 侧按分组过滤能命中这些行。
                    row["group_name"] = _account_group_name(row)
                rid = int(row.get("id") or pos)
                row["id"] = rid
                if table == "email_pool" and conn.execute("SELECT 1 FROM email_pool WHERE id=?", (rid,)).fetchone():
                    rid = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM email_pool").fetchone()[0])
                    row["id"] = rid
                if table == "email_pool":
                    conn.execute(
                        "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (rid, str(row.get("email") or ""), source, str(row.get("status") or ""),
                         int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                         str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)),
                    )
                else:
                    conn.execute(
                        f"INSERT INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                        (rid, str(row.get("email") or ""), str(row.get("status") or ""),
                         int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                         str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)),
                    )


@contextmanager
def _row_write_transaction():
    """Serialize read/modify/write across processes as well as local threads."""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


def _select_collection_row(conn: sqlite3.Connection, collection: str, *,
                           row_id: int | None = None, email: str | None = None) -> dict | None:
    """Read the earliest matching row, preserving Python's Unicode email matching."""
    table = _TABLES[collection]
    matches, params = [], []
    if row_id is not None:
        matches.append("id=?")
        params.append(int(row_id))
    if email is not None:
        conn.create_function("email_lower", 1, lambda value: (value or "").lower(), deterministic=True)
        matches.append("email_lower(email)=?")
        params.append((email or "").lower())
    if not matches:
        return None
    where = "(" + " OR ".join(matches) + ")"
    if table == "email_pool":
        where += " AND source=?"
        params.append(_EMAIL_SOURCES[collection])
    stored = conn.execute(f"SELECT payload FROM {table} WHERE {where} ORDER BY id LIMIT 1", params).fetchone()
    return json.loads(stored["payload"]) if stored else None


def _write_collection_row(conn: sqlite3.Connection, collection: str, row: dict, *, insert: bool = False) -> None:
    """Write one payload and its indexed metadata using the caller's transaction."""
    table = _TABLES[collection]
    before = _select_collection_row(conn, "accounts", row_id=row["id"]) if collection == "accounts" and not insert else None
    row["id"] = int(row["id"])
    if collection == "accounts":
        row["group_name"] = _account_group_name(row)
        row["registered_at"] = _account_registered_at(row)
        row["copy_line"] = _account_line(row)
    elif collection == "generic_api":
        row["code_url"] = _normalize_generic_api_code_url(row.get("code_url"))
        row["copy_line"] = _generic_api_email_line(row)
    elif collection == "imap":
        row["copy_line"] = _imap_email_line(row)
    columns = ["email", "status", "archived", "created_at", "updated_at", "payload"]
    values = [str(row.get("email") or ""), str(row.get("status") or ""),
              int(bool(row.get("archived"))),
              str(row.get("created_at") or (row.get("imported_at") if table == "email_pool" else "") or ""),
              str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)]
    if collection == "accounts":
        columns.insert(4, "registered_at")
        values.insert(4, str(row.get("registered_at") or ""))
    if table == "email_pool":
        columns.append("source")
        values.append(_EMAIL_SOURCES[collection])
    if insert:
        columns.append("id")
        conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                     [*values, row["id"]])
    else:
        conn.execute(f"UPDATE {table} SET {','.join(column + '=?' for column in columns)} WHERE id=?",
                     [*values, row["id"]])
    if collection == "accounts" and (insert or before is not None):
        from core import task_center_store
        task_center_store.sync_account(conn, before, row)


def _next_collection_id(conn: sqlite3.Connection, collection: str) -> int:
    # email_pool IDs are shared by all sources; never allocate per source.
    return int(conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {_TABLES[collection]}").fetchone()[0])


def _collection_where(collection: str, *, status: str | None = None, archived: str | bool | None = None,
                      q: str | None = None, date_from: str | None = None, date_to: str | None = None,
                      extra_where: list[str] | None = None,
                      extra_params: list[Any] | None = None) -> tuple[str, list[Any]]:
    """构造集合查询的 WHERE 子句，供分页、ID 列表等轻量查询复用。"""
    table = _TABLES[collection]
    where = ["1=1"]
    params: list[Any] = []
    if table == "email_pool":
        where.append("source=?"); params.append(_EMAIL_SOURCES[collection])
    if status:
        where.append("status=?"); params.append(status)
    if archived not in (None, "all", "include"):
        where.append("archived=?"); params.append(int(archived in (True, "1", "true", "yes", "only")))
    if q and str(q).strip():
        # 关键词已小写；SQLite 的 LIKE 本身对 ASCII 大小写不敏感，再套一层 lower()
        # 只会把每一行的 payload 复制一遍（搜索框每敲一次都要全表扫）。
        where.append("payload LIKE ?"); params.append("%" + str(q).strip().lower() + "%")
    if date_from:
        value = str(date_from)
        date_column = "registered_at" if collection == "accounts" else "created_at"
        where.append(f"{date_column} >= ?"); params.append(value + ("T00:00:00" if len(value) == 10 else ""))
    if date_to:
        value = str(date_to)
        date_column = "registered_at" if collection == "accounts" else "created_at"
        where.append(f"{date_column} <= ?"); params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    if extra_where:
        where.extend(extra_where)
        params.extend(extra_params or [])
    return " AND ".join(where), params


def _query_collection(collection: str, *, status: str | None = None, archived: str | bool | None = None,
                       q: str | None = None, date_from: str | None = None, date_to: str | None = None,
                       limit: int | None = None, offset: int = 0) -> list[dict]:
    """利用索引分页读取，避免 WebUI 为一个页面加载整个 JSON 文件。"""
    _ensure_sqlite()
    table = _TABLES[collection]
    clause, params = _collection_where(
        collection, status=status, archived=archived, q=q, date_from=date_from, date_to=date_to,
    )
    sql = f"SELECT payload FROM {table} WHERE " + clause + " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"; params = [*params, max(0, int(limit)), max(0, int(offset))]
    with closing(_sqlite_conn()) as conn:
        return [json.loads(row["payload"]) for row in conn.execute(sql, params)]


def _query_collection_page(collection: str, *, status: str | None = None,
                           archived: str | bool | None = None, q: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           extra_where: list[str] | None = None,
                           extra_params: list[Any] | None = None,
                           limit: int = 50, offset: int = 0,
                           conn: sqlite3.Connection | None = None) -> tuple[list[dict], int, str]:
    """执行真正的 SQL COUNT/LIMIT/OFFSET 分页，并返回最新更新时间。

    COUNT(*) 与 MAX(updated_at) 合成一次聚合扫描：两者都需要遍历同一批行，
    分成两条语句会让带筛选条件的页面查询成本翻倍。
    """
    _ensure_sqlite()
    table = _TABLES[collection]
    clause, params = _collection_where(
        collection, status=status, archived=archived, q=q, date_from=date_from, date_to=date_to,
        extra_where=extra_where, extra_params=extra_params,
    )
    own = conn is None
    active = _sqlite_conn() if own else conn
    try:
        total, latest = _collection_totals(active, table, clause, params)
        rows = [json.loads(row["payload"]) for row in active.execute(
            f"SELECT payload FROM {table} WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, max(1, int(limit)), max(0, int(offset))],
        )]
    finally:
        if own:
            active.close()
    return rows, total, latest


def _collection_totals(conn: sqlite3.Connection, table: str, clause: str, params: list[Any]) -> tuple[int, str]:
    """一次扫描同时取回总行数与最新更新时间。"""
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS latest FROM {table} WHERE {clause}",
        params,
    ).fetchone()
    if row is None:
        return 0, ""
    if isinstance(row, sqlite3.Row):
        return int(row["n"] or 0), str(row["latest"] or "")
    return int(row[0] or 0), str(row[1] or "")


def _query_collection_ids(collection: str, *, status: str | None = None,
                          archived: str | bool | None = None, q: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          extra_where: list[str] | None = None,
                          extra_params: list[Any] | None = None,
                          limit: int = 5000, offset: int = 0,
                          conn: sqlite3.Connection | None = None) -> tuple[list[int], int]:
    """只读 ID 的轻量分页：不反序列化 payload，供前端「全选」按页收集 ID。"""
    _ensure_sqlite()
    table = _TABLES[collection]
    clause, params = _collection_where(
        collection, status=status, archived=archived, q=q, date_from=date_from, date_to=date_to,
        extra_where=extra_where, extra_params=extra_params,
    )
    own = conn is None
    active = _sqlite_conn() if own else conn
    try:
        total = int(active.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", params).fetchone()[0])
        ids = [int(row["id"]) for row in active.execute(
            f"SELECT id FROM {table} WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, max(1, int(limit)), max(0, int(offset))],
        )]
    finally:
        if own:
            active.close()
    return ids, total


def _normalize_redemption_filter(value: object) -> bool | None:
    """把兑换状态筛选归一化为 True（已兑换）/ False（未兑换）/ None（全部）。"""
    raw = str(value or "").strip().lower()
    if raw in {"redeemed", "claimed", "1", "true", "yes", "on", "已兑换"}:
        return True
    if raw in {"unredeemed", "not_redeemed", "unclaimed", "0", "false", "no", "off", "未兑换"}:
        return False
    return None


# AT 状态筛选：expired（AT 已过期）/ valid（AT 未过期）/ unknown（无法判断）。
# 无法识别的取值返回 _FILTER_NONE，命中空集合，避免拼错参数时静默放大结果集。
_FILTER_NONE = "__none__"
_AT_FILTER_ALIASES = {
    "expired": "expired", "at_expired": "expired", "token_expired": "expired",
    "at_invalid": "expired", "dead": "expired", "过期": "expired", "已过期": "expired",
    "valid": "valid", "at_valid": "valid", "alive": "valid", "ok": "valid",
    "active": "valid", "normal": "valid", "未过期": "valid", "正常": "valid",
    "unknown": "unknown", "none": "unknown", "unchecked": "unknown",
    "unset": "unknown", "未知": "unknown",
}
_AT_STATE_SQL = (
    "CASE "
    "WHEN lower(COALESCE(CAST(json_extract(payload, '$.token_expired') AS TEXT), '')) "
    "IN ('1', 'true', 'yes', 'on') THEN 1 "
    "WHEN NULLIF(TRIM(COALESCE(CAST(json_extract(payload, '$.token_expires_at') AS TEXT), '')), '') IS NOT NULL "
    "AND julianday(json_extract(payload, '$.token_expires_at')) IS NOT NULL "
    "THEN (CASE WHEN julianday(json_extract(payload, '$.token_expires_at')) <= julianday('now') "
    "THEN 1 ELSE 0 END) "
    "WHEN lower(COALESCE(CAST(json_extract(payload, '$.token_expired') AS TEXT), '')) "
    "IN ('0', 'false', 'no', 'off') THEN 0 "
    "ELSE account_at_expired(json_extract(payload, '$.access_token')) END"
)
# 生成列版本：与 _AT_STATE_SQL 同一判定顺序，但可直接吃 (archived, at_state_exp) 索引。
_AT_STATE_FAST_SQL = {
    "expired": "(at_state_exp IS NOT NULL AND at_state_exp <= julianday('now'))",
    "valid": "(at_state_exp IS NOT NULL AND at_state_exp > julianday('now'))",
    "unknown": "(at_state_exp IS NULL)",
}
# 只有当“没有任何过期信息、但 access_token 能解析出 exp”的行存在时，生成列才会
# 与 Python 判定不一致；这类行用一次索引探测即可发现，发现后回退到精确表达式。
# INDEXED BY 让探测只按 at_state_exp IS NULL 定位（否则优化器会整表扫描，白花 40ms）。
_AT_STATE_PROBE_SQL = (
    "SELECT 1 FROM accounts INDEXED BY idx_accounts_at_state_exp WHERE at_state_exp IS NULL "
    "AND account_at_expired(json_extract(payload, '$.access_token')) IS NOT NULL LIMIT 1"
)


def _at_state_filter_sql(conn: sqlite3.Connection | None, state: str) -> str:
    """返回 AT 状态筛选片段；无法确认生成列覆盖全部行时使用精确表达式。"""
    exact = f"({_AT_STATE_SQL}) = 1" if state == "expired" else (
        f"({_AT_STATE_SQL}) = 0" if state == "valid" else f"({_AT_STATE_SQL}) IS NULL"
    )
    fast = _AT_STATE_FAST_SQL.get(state)
    if fast is None or conn is None:
        return exact
    try:
        if conn.execute(_AT_STATE_PROBE_SQL).fetchone() is not None:
            return exact
    except sqlite3.Error:
        return exact
    return fast


def _normalize_at_filter(value: object) -> str:
    """归一化 AT 状态筛选：expired / valid / unknown / 空字符串（不限制）。"""
    raw = str(value or "").strip().lower()
    if not raw or raw in {"all", "*", "any"}:
        return ""
    return _AT_FILTER_ALIASES.get(raw, _FILTER_NONE)


# 查活状态筛选：把别名映射到 live_check_status 的字面量集合。
_LIVE_FILTER_GROUPS: dict[str, tuple[str, ...]] = {
    "failed": ("failed", "fail", "error", "dead", "失败", "查活失败"),
    "success": ("success", "succeeded", "live", "ok", "alive", "active", "normal", "正常", "成功"),
    "deactivated": ("deactivated", "disabled", "banned", "废号", "已停用"),
    "checking": ("checking", "queued", "running", "pending", "查活中"),
    "cancelled": ("cancelled", "canceled", "已取消"),
    "never": ("never", "unchecked", "none", "empty", "未查活"),
}
_LIVE_FILTER_ALIASES: dict[str, tuple[str, ...]] = {}
for _group, _aliases in _LIVE_FILTER_GROUPS.items():
    _LIVE_FILTER_ALIASES[_group] = _LIVE_FILTER_GROUPS[_group]
    for _alias in _aliases:
        _LIVE_FILTER_ALIASES[_alias] = _LIVE_FILTER_GROUPS[_group]
# 「未查活」对应 status 字段缺失或为空。
_LIVE_FILTER_ALIASES["never"] = ("",)


def _normalize_live_filter(value: object) -> tuple[str, ...]:
    """归一化查活状态筛选，返回一组 live_check_status 字面量。

    空元组表示不限制；`(_FILTER_NONE,)` 表示无法识别的取值（命中空集合）。
    """
    raw = str(value or "").strip().lower()
    if not raw or raw in {"all", "*", "any"}:
        return ()
    return _LIVE_FILTER_ALIASES.get(raw, (_FILTER_NONE,))


# 套餐查询轻量状态快照返回的字段（id/email 单列，scan_request_* 单独处理）。
_PLAN_CHECK_FIELDS: tuple[str, ...] = (

        "id", "email", "archived", "group_name",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "eligible_promo_campaigns", "plus_trial_discount_percentage",
        "plan_check_status", "plan_check_ok", "plan_check_error",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "quota_check_status", "quota_check_ok", "quota_check_error", "quota_check_trigger",
        "quota_check_queued_at", "quota_check_started_at", "quota_check_completed_at",
        "quota_balance", "quota_balance_amount", "quota_currency", "quota_unlimited",
        "quota_has_credits", "quota_credits_balance", "quota_overage_limit_reached",
        "quota_balance_fallback",
        "quota_checked_at", "quota_error", "quota_http_status", "quota_last_success_at",
        "reset_credits_available", "reset_credits_applicable", "reset_credits_expires_at",
        "reset_credits_checked_at", "reset_credits_error", "reset_credits_http_status",
        "usage_plan_type", "usage_allowed", "usage_limit_reached", "usage_limit_reached_type",
        "usage_5h_percent", "usage_5h_window_seconds", "usage_5h_reset_at",
        "usage_5h_reset_after_seconds", "usage_5h_started",
        "usage_week_percent", "usage_week_window_seconds", "usage_week_reset_at",
        "usage_week_reset_after_seconds", "usage_week_started",
        "usage_checked_at", "usage_error", "usage_http_status",
        "live_check_status", "live_check_error", "live_checked_at",
        "live_check_proxy_used", "live_check_fingerprint_text",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id", "is_delinquent",
        "subscription_active_start", "subscription_active_until",
        "subscription_became_delinquent_at", "subscription_grace_period_end_at",
        "subscription_billing_currency", "subscription_billing_period", "subscription_plan_type",
        "subscription_checked_at", "subscription_http_status", "subscription_error",
        "extract_link_status", "extract_link_ok", "extract_link_type",
        "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_hosted_instructions_url", "extract_link_copy_paste",
        "extract_link_image_url_png", "extract_link_image_url_svg",
        "extract_link_expires_at", "extract_link_job_id", "extract_link_task_id",
        "extract_link_provider_id", "extract_link_provider_type", "extract_link_provider_name",
        "extract_link_cdk_id", "extract_link_cdk_suffix", "extract_link_progress", "extract_link_payment_status",
        "scan_request_provider", "scan_request_status", "scan_request_ok", "scan_request_task_id", "scan_request_message",
        "scan_request_error", "scan_request_error_code", "scan_request_retryable",
        "scan_request_duplicate", "scan_request_request_id", "scan_request_checked_at",
        "plus_activation_status", "plus_activation_message", "plus_activation_updated_at",
        "codex_status", "codex_error",
        "codex_agent_status", "codex_agent_message",
        "codex_agent_runtime_id", "codex_agent_sub2api_url",
        "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
        "totp_setup_status", "totp_setup_ok", "totp_setup_error",
        "totp_setup_message", "totp_setup_trigger", "totp_setup_queued_at",
        "totp_setup_started_at", "totp_setup_completed_at", "totp_setup_checked_at",
        "original_email", "email_source", "email_change_status", "email_change_ok",
        "email_change_error", "email_change_new_email", "email_change_started_at", "email_change_completed_at",
)
_PLAN_CHECK_SCAN_FIELDS: tuple[str, ...] = tuple(f for f in _PLAN_CHECK_FIELDS if f.startswith("scan_request_"))
_PLAN_CHECK_PLAIN_FIELDS: tuple[str, ...] = tuple(
    f for f in _PLAN_CHECK_FIELDS if not f.startswith("scan_request_") and f not in ("id", "email")
)


def _account_filter_sql(
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[str], list[Any]]:
    """把账号列表的套餐、Codex、2FA、分组、兑换状态、AT/查活状态条件下推到 SQLite。

    套餐、Codex、2FA、分组、AT/查活状态来自账号 payload；这里改写成查询
    ``_ACCOUNT_GENERATED_COLUMNS`` 维护的生成列，让分页与 COUNT 都走覆盖索引，
    不再为每次翻页解析整张表的 JSON。兑换状态来自 redeem_claims 表，用 EXISTS
    子查询过滤。``conn`` 传入时 AT 状态可以用生成列 + 索引探测的快速形式。
    """
    where: list[str] = []
    params: list[Any] = []
    plan = str(plan_filter or "").strip().lower()
    codex = str(codex_filter or "").strip().lower()
    totp = str(totp_filter or "").strip().lower()
    at_state = _normalize_at_filter(at_filter)
    live_states = _normalize_live_filter(live_filter)
    if at_state == _FILTER_NONE:
        where.append("0=1")
    elif at_state in _AT_STATE_FAST_SQL:
        where.append(_at_state_filter_sql(conn, at_state))
    if live_states == (_FILTER_NONE,):
        where.append("0=1")
    elif live_states:
        where.append(f"live_norm IN ({', '.join('?' for _ in live_states)})")
        params.extend(live_states)
    redeemed = _normalize_redemption_filter(redemption_filter)
    if redeemed is not None:
        claim_exists = "EXISTS (SELECT 1 FROM redeem_claims AS rc WHERE rc.account_id = accounts.id)"
        where.append(claim_exists if redeemed else f"NOT {claim_exists}")
    if group_filter is not None and group_filter != "":
        wanted = _validate_account_group_name(group_filter)
        where.append("group_key = ? COLLATE BINARY")
        params.append(wanted)

    plan_expr = "plan_norm"
    if plan and plan not in {"all", "any"}:
        if plan == "plus":
            # 与 _account_matches_plan_filter 保持一致：free(可试用)不算已开通 Plus。
            where.append("plan_plus = 1")
        elif plan in {"plus_trial", "plus_trial_eligible", "trial", "trial_eligible"}:
            # 只有当前套餐为 free 且套餐查询明确返回可试用资格时才命中。
            trial_expr = "lower(COALESCE(CAST(json_extract(payload, '$.plus_trial_eligible') AS TEXT), ''))"
            where.append(f"{plan_expr} = ?")
            where.append(f"{trial_expr} IN (?, ?, ?, ?)")
            params.extend(["free", "1", "true", "yes", "on"])
        elif plan in {"promo", "promotion", "plan_promo", "eligible_promo"} or plan.startswith("promo:"):
            # 当前套餐必须是 free，并且任意套餐存在至少一个可用优惠活动；
            # 不限定 Plus。eligible_promo_campaigns 是以套餐名为 key 的对象。
            promo_expr = "json_extract(payload, '$.eligible_promo_campaigns')"
            where.append(f"{plan_expr} = ?")
            where.append("json_type(payload, '$.eligible_promo_campaigns') = 'object'")
            where.append(f"EXISTS (SELECT 1 FROM json_each({promo_expr}))")
            params.append("free")
            promo_parts = plan.split(":") if plan.startswith("promo:") else []
            promo_type = promo_parts[1].strip()[:64] if len(promo_parts) > 1 else ""
            promo_discount = promo_parts[2].strip()[:16] if len(promo_parts) > 2 else ""
            conditions: list[str] = []
            condition_params: list[Any] = []
            if promo_type and promo_type not in {"*", "all", "any"}:
                canonical = promo_type.lower().replace("-", "").replace("_", "").replace(" ", "")
                if canonical.startswith("chatgpt"):
                    canonical = canonical[7:]
                if canonical.endswith("plan"):
                    canonical = canonical[:-4]
                normalized_key = "lower(replace(replace(replace(j.key, '-', ''), '_', ''), ' ', ''))"
                normalized_name = (
                    "lower(replace(replace(replace(COALESCE(CAST(json_extract(j.value, '$.metadata.plan_name') AS TEXT), ''), "
                    "'-', ''), '_', ''), ' ', ''))"
                )
                conditions.append(
                    f"({normalized_key} = ? OR {normalized_name} IN (?, ?, ?))"
                )
                condition_params.extend([canonical, canonical, f"{canonical}plan", f"chatgpt{canonical}plan"])
            if promo_discount:
                try:
                    discount_value = float(promo_discount.rstrip("%"))
                except ValueError:
                    discount_value = None
                if discount_value is not None:
                    conditions.append("CAST(json_extract(j.value, '$.metadata.discount.percentage') AS REAL) = ?")
                    condition_params.append(discount_value)
            if conditions:
                where.append(
                    f"EXISTS (SELECT 1 FROM json_each({promo_expr}) AS j WHERE "
                    + " AND ".join(conditions) + ")"
                )
                params.extend(condition_params)
        elif plan in {"free_no_trial", "free_without_trial", "free_not_trial"}:
            # 只匹配已成功查询且没有任何套餐优惠的 free 账号；字段缺失代表
            # 尚未得到完整优惠结果，不应归入“不可试用套餐”。
            promo_expr = "json_extract(payload, '$.eligible_promo_campaigns')"
            where.append(f"{plan_expr} = ?")
            where.append("json_type(payload, '$.eligible_promo_campaigns') = 'object'")
            where.append(f"NOT EXISTS (SELECT 1 FROM json_each({promo_expr}))")
            params.append("free")
        elif plan == "free":
            where.append(f"{plan_expr} = ?")
            params.append("free")
        else:
            where.append(f"{plan_expr} = ?")
            params.append(plan)

    status_expr = "codex_norm"
    live_status_expr = "live_norm"
    if codex and codex not in {"all", "*"}:
        if codex == "deactivated":
            where.append(f"{live_status_expr} = ?")
        else:
            where.append(f"{status_expr} = ?")
        params.append(codex)

    totp_secret_expr = "totp_flag"
    totp_setup_expr = "lower(COALESCE(CAST(json_extract(payload, '$.totp_setup_status') AS TEXT), ''))"
    if totp and totp not in {"all", "*"}:
        if totp in {"enabled", "on", "active"}:
            where.append(f"{totp_secret_expr} = 1")
        elif totp in {"disabled", "off", "not_enabled", "unset"}:
            where.append(f"{totp_secret_expr} = 0")
        elif totp in {"pending", "setup", "setting", "queued", "running"}:
            where.append(f"{totp_setup_expr} IN (?, ?)")
            params.extend(["queued", "running"])
        elif totp == "failed":
            where.append(f"{totp_setup_expr} = ?")
            params.append("failed")
        elif totp == "stopped":
            where.append(f"{totp_setup_expr} = ?")
            params.append("stopped")
        else:
            where.append(f"{totp_setup_expr} = ?")
            params.append(totp)
    return where, params


def _pool_summary_sql(collection: str) -> dict:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn:
        where = " WHERE source=?" if table == "email_pool" else ""
        params = (_EMAIL_SOURCES[collection],) if table == "email_pool" else ()
        counts = {str(r["status"] or "available"): int(r["n"]) for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM {table}{where} GROUP BY status", params
        )}
    out = {"available": counts.get("available", 0), "used": counts.get("used", 0), "failed": counts.get("failed", 0)}
    out.update({k: v for k, v in counts.items() if k not in out})
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        _normalize_generic_api_code_url(row.get("code_url")),
    ])


def _normalize_generic_api_code_url(value: object) -> str:
    """修复导入文本中误粘贴到 URL 前面的短横线。"""
    url = str(value or "").strip()
    if url.startswith("-"):
        candidate = url.lstrip("-")
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return url


def _imap_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("imap_password") or row.get("password") or "",
    ])


def _extract_registration_password(row: dict) -> str:
    extra_raw = row.get("extra_json")
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except Exception:
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    else:
        extra = {}
    return str(extra.get("registration_password") or row.get("registration_password") or "").strip()


def _looks_like_email_material_segment(segment: str) -> bool:
    seg = str(segment or "").strip()
    if not seg:
        return False
    if seg.startswith("M.") or seg.startswith("m."):
        return True
    if len(seg) >= 32 and "-" in seg and seg.count("-") >= 4:
        return True
    if any(ch in seg for ch in ("@", ":", "/", "\\")):
        return True
    return False


def _ensure_password_in_material_line(base: str, password: str) -> str:
    base = str(base or "").strip()
    password = str(password or "").strip()
    if not password:
        return base
    parts = [p for p in base.split("----") if p != ""] if base else []
    if not parts:
        return password
    if len(parts) == 1:
        if parts[0] == password:
            return base
        return "----".join([parts[0], password])
    if parts[1] == password:
        return base
    if _looks_like_email_material_segment(parts[1]):
        parts.insert(1, password)
        return "----".join(parts)
    return base


def _account_line(row: dict) -> str:
    base = row.get("original_email_line") or row.get("email") or ""
    email_password = str(row.get("password") or "").strip()
    base = _ensure_password_in_material_line(base, email_password)
    token = row.get("access_token") or ""
    gpt_password = _extract_registration_password(row) or "未设置"
    totp = row.get("totp_secret") or ""
    parts = [base, token, gpt_password]
    if totp:
        parts.append(totp)
    return "----".join(parts)


# “完整导出”里 2FA 段固定的站点占位（需求指定的固定文案）。
_TWOFA_EXPORT_URL = "https://2fa.run/"


def _account_full_export_line(row: dict) -> str:
    """生成“完整导出”单行，格式严格按需求：

        邮箱---邮箱接码API---密码---https://2fa.run/----2FA:密钥

    - 邮箱接码API：接码所用的“完整接码链接格式”。generic_api 账号直接输出邮箱池里
      存的 code_url（取码地址，如 http://127.0.0.1:5055/code?email=xxx@domain）；
      其它来源（gptmail/outlook/remail…）保留来源标识。
    - 密码：ChatGPT 账号自身登录密码（registration_password）。
    - 2FA：固定前缀 “2FA:” 拼接 TOTP 密钥。
    分隔符：前四段之间为 “---”，2FA 段之前为 “----”（与需求保持一致）。
    """
    email = str(row.get("email") or "").strip()
    email_api = _resolve_email_api_link(email, str(row.get("email_source") or "").strip())
    # 仅填 ChatGPT 注册密码；若该账号没有，则留空。
    password = _extract_registration_password(row)
    totp = str(row.get("totp_secret") or "").strip()
    line = "---".join([email, email_api, password, _TWOFA_EXPORT_URL])
    line = line + "----" + ("2FA:" + totp)
    return line


def _resolve_email_api_link(email: str, email_source: str) -> str:
    """把“邮箱接码API”字段解析为完整接码链接格式。

    - generic_api：优先取邮箱池里的 code_url（取码地址）作为完整链接；
      池里没有该邮箱时，按 OmniMail 取码接口约定拼出完整链接
      （{OMNIMAIL_BASE}/messages?mailbox=<邮箱>），仍然取不到才回退为原文。
    - 其它来源：原样返回来源标识。
    """
    if email_source == "generic_api" and email:
        try:
            pool_row = get_generic_api_email_by_email(email)
        except Exception:
            pool_row = None
        if pool_row:
            link = str(pool_row.get("code_url") or "").strip()
            if link:
                return link
        link = _build_generic_api_code_url(email)
        if link:
            return link
    return email_source


def _build_generic_api_code_url(email: str) -> str:
    """按 OmniMail 取码接口约定拼出完整取码链接；取不到基础地址时返回空串。"""
    try:
        # 延迟导入，避免 core.db 与 config 包产生循环依赖。
        from config.email import OMNIMAIL_BASE
        base = str(OMNIMAIL_BASE or "").strip().rstrip("/")
    except Exception:
        base = ""
    if not base or not email:
        return ""
    return f"{base}/messages?mailbox={email}"


def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _load_outlook() -> list[dict]:
    return _load_collection("outlook")


def _save_outlook(rows: list[dict]) -> None:
    _save_collection("outlook", rows)


def _load_generic_api_emails() -> list[dict]:
    rows = _load_collection("generic_api")
    changed = False
    for row in rows:
        original = row.get("code_url")
        normalized = _normalize_generic_api_code_url(original)
        if normalized != original:
            row["code_url"] = normalized
            changed = True
    if changed:
        _save_generic_api_emails(rows)
    return rows


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    _save_collection("generic_api", rows)


def _load_imap_emails() -> list[dict]:
    return _load_collection("imap")


def _save_imap_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _imap_email_line(row)
    _save_collection("imap", rows)


def _load_accounts() -> list[dict]:
    return _load_collection("accounts")


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    _save_collection("accounts", rows)


def _load_jobs() -> list[dict]:
    return _load_collection("jobs")


def _save_jobs(rows: list[dict]) -> None:
    _save_collection("jobs", rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _account_registered_at(row: dict) -> str:
    """返回账号注册时间；旧记录退回本地首次落库时间。"""
    return str(row.get("registered_at") or row.get("created_at") or "").strip()


def _account_group_name(row: dict) -> str:
    value = str(row.get("group_name") or "").strip()
    return value or DEFAULT_ACCOUNT_GROUP


def _validate_account_group_name(value: object) -> str:
    """New names are trimmed, case-sensitive Unicode identities, never aliases."""
    if not isinstance(value, str) or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in value):
        raise ValueError("分组名称必须是字符串，且不能包含控制字符")
    name = value.strip()
    if not name or len(name) > 60:
        raise ValueError("分组名称不能为空，且不能超过 60 个字符")
    return name


def _apply_plan_check_staleness(out: dict) -> None:
    """把超时的 queued/running 套餐查询标记为失败（列表展示与状态轮询共用）。"""
    plan_status = out.get("plan_check_status")
    if plan_status not in {"queued", "running"}:
        return
    try:
        stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
        stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
        started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
        if (datetime.now() - started_at).total_seconds() >= stale_after:
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
            out["plan_check_stale"] = True
    except (TypeError, ValueError):
        out["plan_check_status"] = "failed"
        out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
        out["plan_check_stale"] = True


def _apply_quota_check_staleness(out: dict) -> None:
    """把超时的 queued/running 额度查询标记为失败（与套餐查询同一套阈值）。"""
    status = out.get("quota_check_status")
    if status not in {"queued", "running"}:
        return
    try:
        stamp_key = "quota_check_queued_at" if status == "queued" else "quota_check_started_at"
        stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if status == "queued" else _PLAN_CHECK_STALE_SECONDS
        started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
        if (datetime.now() - started_at).total_seconds() >= stale_after:
            out["quota_check_status"] = "failed"
            out["quota_check_error"] = "上次额度查询状态已超时，可重新查询"
            out["quota_check_stale"] = True
    except (TypeError, ValueError):
        out["quota_check_status"] = "failed"
        out["quota_check_error"] = "上次额度查询状态异常，可重新查询"
        out["quota_check_stale"] = True


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    out["registered_at"] = _account_registered_at(out)
    out["group_name"] = _account_group_name(out)
    _apply_plan_check_staleness(out)
    _apply_quota_check_staleness(out)
    out["copy_line"] = _account_line(out)
    # 列表里的「AT 已过期」标记与 at_status 筛选共用同一判定，避免看到的状态和筛出来的集合不一致。
    out["at_expired"] = _account_at_state(out) == "expired"
    return out


def _decorate_account_status(row: dict) -> dict:
    """状态轮询专用轻量装饰：不生成 copy_line，避免为数千行白拼展示文本。"""
    out = dict(row)
    out["group_name"] = _account_group_name(out)
    _apply_plan_check_staleness(out)
    _apply_quota_check_staleness(out)
    out["at_expired"] = _account_at_state(out) == "expired"
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤：支持已开通 Plus、任意套餐优惠及 free 资格过滤。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if f == "plus":
        # “free(可Plus试用)”/plus_trial_eligible 只是可试用，不算已开通 Plus。
        # 只有套餐字段本身是 Plus/ChatGPT Plus/plus_* 且不含 free 时才命中。
        return "plus" in plan and "free" not in plan
    if f in {"plus_trial", "plus_trial_eligible", "trial", "trial_eligible"}:
        trial = row.get("plus_trial_eligible")
        if isinstance(trial, str):
            trial = trial.strip().lower() in {"1", "true", "yes", "on"}
        return plan == "free" and bool(trial)
    if f in {"promo", "promotion", "plan_promo", "eligible_promo"} or f.startswith("promo:"):
        campaigns = row.get("eligible_promo_campaigns")
        if plan != "free" or not isinstance(campaigns, dict) or not campaigns:
            return False
        promo_parts = f.split(":") if f.startswith("promo:") else []
        wanted = promo_parts[1].strip() if len(promo_parts) > 1 else ""
        discount_text = promo_parts[2].strip() if len(promo_parts) > 2 else ""
        try:
            wanted_discount = float(discount_text.rstrip("%")) if discount_text else None
        except ValueError:
            wanted_discount = None
        if not wanted and wanted_discount is None:
            return True

        def normalize_promo_type(value: Any) -> str:
            value = str(value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
            if value.startswith("chatgpt"):
                value = value[7:]
            if value.endswith("plan"):
                value = value[:-4]
            return value

        wanted = normalize_promo_type(wanted) if wanted not in {"*", "all", "any"} else ""
        for key, campaign in campaigns.items():
            metadata = campaign.get("metadata") if isinstance(campaign, dict) else {}
            plan_name = metadata.get("plan_name") if isinstance(metadata, dict) else ""
            type_matches = not wanted or wanted in {normalize_promo_type(key), normalize_promo_type(plan_name)}
            discount = metadata.get("discount") if isinstance(metadata, dict) else {}
            percentage = discount.get("percentage") if isinstance(discount, dict) else None
            try:
                discount_matches = wanted_discount is None or float(percentage) == wanted_discount
            except (TypeError, ValueError):
                discount_matches = False
            if type_matches and discount_matches:
                return True
        return False
    if f in {"free_no_trial", "free_without_trial", "free_not_trial"}:
        campaigns = row.get("eligible_promo_campaigns")
        return plan == "free" and isinstance(campaigns, dict) and not campaigns
    if f == "free":
        return plan == "free"
    return plan == f


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["code_url"] = _normalize_generic_api_code_url(out.get("code_url"))
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_imap_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _imap_email_line(out)
    account = account_by_email.get((out.get("email") or "").lower()) if account_by_email else None
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = ((account.get("access_token") or "")[:40] + "...") if account.get("access_token") else ""
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def list_email_pool_page(
    source: str = "all",
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """从统一邮箱库直接执行 COUNT + LIMIT/OFFSET。

    ``email_pool`` 是三个邮箱来源共用的表。source 为具体来源时按 id 倒序，
    source=all 时按入库时间合并倒序；两种情况都只从 SQLite 取当前页，
    不再先加载全部邮箱再由 WebUI 切片。
    """
    _ensure_sqlite()
    source = str(source or "outlook").strip().lower()
    if source not in {"all", "outlook", "generic_api", "imap", "cloudflare_domain"}:
        source = "outlook"
    collection = "domain" if source == "cloudflare_domain" else source
    db_source = None if source == "all" else _EMAIL_SOURCES[collection]
    limit = max(1, int(limit))
    offset = max(0, int(offset or 0))
    where = ["1=1"]
    params: list[Any] = []
    if db_source is not None:
        where.append("ep.source=?")
        params.append(db_source)
    if status:
        where.append("ep.status=?")
        params.append(status)
    if q and str(q).strip():
        like = "%" + str(q).strip().lower() + "%"
        # payload 覆盖邮箱池自身字段；source 和关联账号 payload 保持旧 WebUI
        # 的搜索能力（例如搜索 generic_api 或已注册账号 token）。
        where.append(
            "(ep.payload LIKE ? OR ep.source LIKE ? OR EXISTS ("
            "SELECT 1 FROM accounts AS a "
            "WHERE a.email = ep.email COLLATE NOCASE AND a.payload LIKE ?))"
        )
        params.extend([like, like, like])
    clause = " AND ".join(where)
    order_by = "ep.created_at DESC, ep.id DESC" if source == "all" else "ep.id DESC"
    with _LOCK, closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM email_pool AS ep WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(
            f"SELECT COALESCE(MAX(ep.updated_at), '') FROM email_pool AS ep WHERE {clause}",
            params,
        ).fetchone()[0] or "")
        rows = conn.execute(
            f"SELECT ep.payload, ep.source, "
            f"(SELECT a.payload FROM accounts AS a "
            f" WHERE a.email = ep.email COLLATE NOCASE ORDER BY a.id DESC LIMIT 1) AS account_payload "
            f"FROM email_pool AS ep WHERE {clause} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

    # ``domain`` 是内部 collection 名，API 对外统一使用 cloudflare_domain；
    # 直接反转 _EMAIL_SOURCES 会把域名邮箱错误地返回成 source=domain。
    source_names = {
        _EMAIL_SOURCES["outlook"]: "outlook",
        _EMAIL_SOURCES["generic_api"]: "generic_api",
        _EMAIL_SOURCES["imap"]: "imap",
        _EMAIL_SOURCES["domain"]: "cloudflare_domain",
    }
    items: list[dict] = []
    for row in rows:
        item = json.loads(row["payload"])
        account_payload = row["account_payload"]
        account = None
        if account_payload:
            try:
                account = json.loads(account_payload)
            except (TypeError, ValueError):
                account = None
        item_source = source_names.get(str(row["source"]), str(row["source"]))
        if item_source == "outlook":
            item = _decorate_outlook(item, {str(item.get("email") or "").lower(): account} if account else {})
        elif item_source == "generic_api":
            item = _decorate_generic_api_email(item, {str(item.get("email") or "").lower(): account} if account else {})
        elif item_source == "imap":
            item = _decorate_imap_email(item, {str(item.get("email") or "").lower(): account} if account else {})
        else:
            item = dict(item)
        item["source"] = item_source
        if not item.get("copy_line"):
            item["copy_line"] = item.get("email") or ""
        items.append(item)
    return {"items": items, "total": total, "offset": offset, "limit": limit, "latest": latest}


def _get_conn() -> sqlite3.Connection:
    """兼容旧入口：返回 SQLite 连接。"""
    return _sqlite_conn()


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# Plus 账号兑换
# ============================================================

class RedeemError(RuntimeError):
    """兑换失败，带有可直接返回给 API 的错误码和 HTTP 状态。"""

    def __init__(self, message: str, *, code: str = "redeem_failed", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


_REDEEM_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _normalize_redeem_code(value: object) -> str:
    return "".join(str(value or "").split()).strip().upper()


def _redeem_plan(row: dict) -> str:
    return str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()


def _redeem_credentials(row: dict) -> dict | None:
    """把有 ChatGPT 登录密码、未归档、未废号的账号放入兑换库存（不限制套餐）。"""
    email = str(row.get("email") or "").strip()
    password = _extract_registration_password(row)
    if not email or not password:
        return None
    if bool(row.get("archived")) or str(row.get("live_check_status") or "").lower() == "deactivated":
        return None
    plan = _redeem_plan(row)
    return {
        "account_id": int(row.get("id") or 0),
        "email": email,
        "password": password,
        "totp_secret": str(row.get("totp_secret") or "").strip(),
        "plan": str(row.get("current_plan_type") or row.get("plan_type") or "").strip(),
        "expires_at": row.get("plan_expires_at") or row.get("expires_at") or row.get("plan_renews_at") or "",
        "group_name": _account_group_name(row),
    }


def _redeem_claim_map(account_ids: Iterable[object]) -> dict[int, str]:
    """查询这些账号的兑换领取时间：{account_id: claimed_at}。

    账号列表用它标记「是否已被兑换」；只读取领取时间和账号 ID，
    不返回 CDK、密码或任何凭据。
    """
    ids: list[int] = []
    seen: set[int] = set()
    for value in account_ids or ():
        try:
            account_id = int(value)
        except (TypeError, ValueError):
            continue
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    if not ids:
        return {}
    _ensure_sqlite()
    claims: dict[int, str] = {}
    with closing(_sqlite_conn()) as conn:
        # SQLite 单条语句的参数上限（旧版本 999）低于 5000 个账号的批量查询，
        # 这里分片读取，避免“too many SQL variables”。
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT account_id, claimed_at FROM redeem_claims WHERE account_id IN ({placeholders})",
                chunk,
            ):
                claims[int(row["account_id"])] = str(row["claimed_at"] or "")
    return claims


def _attach_redeem_claims(items: list[dict]) -> list[dict]:
    """为账号列表补充兑换状态：redeemed（是否已被兑换）与 redeemed_at。"""
    if not items:
        return items
    claims = _redeem_claim_map([item.get("id") for item in items])
    for item in items:
        try:
            account_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        claimed_at = claims.get(account_id)
        item["redeemed"] = claimed_at is not None
        if claimed_at:
            item["redeemed_at"] = claimed_at
    return items


def _redeem_candidate_where(group_name: str | None = None, *, alias: str = "a") -> tuple[str, list[Any]]:
    """兑换候选行的 SQL 条件：与 _redeem_credentials 的硬性条件一一对应。

    这里只做“能确定不满足就直接排除”的粗筛（未归档、有登录密码、未废号、未被领取），
    真正可兑换与否仍由 Python 的 _redeem_credentials 复核，因此不会放宽语义。
    """
    wanted_group = _account_group_name({"group_name": group_name}) if group_name else None
    where = [
        f"{alias}.archived=0",
        f"{alias}.has_password=1",
        f"{alias}.live_norm<>'deactivated'",
        f"trim({alias}.email, {_SQL_WS_CHARS})<>''",
    ]
    if wanted_group:
        where.append(f"{alias}.group_key=?")
        params: list[Any] = [wanted_group]
    else:
        where.append(f"{alias}.plan_plus=1")
        params = []
    return " AND ".join(where), params


def _redeem_candidate_rows(conn: sqlite3.Connection, group_name: str | None = None) -> list[dict]:
    wanted_group = _account_group_name({"group_name": group_name}) if group_name else None
    clause, params = _redeem_candidate_where(group_name)
    rows = conn.execute(
        "SELECT a.id, a.email, a.archived, a.payload FROM accounts AS a "
        # INDEXED BY：优化器会改走 plan_plus 索引，并逐行回表判定密码/查活状态。
        "INDEXED BY idx_accounts_redeemable "
        f"WHERE {clause} "
        "AND NOT EXISTS (SELECT 1 FROM redeem_claims AS c WHERE c.account_id = a.id) "
        "ORDER BY a.id ASC",
        params,
    ).fetchall()

    result: list[dict] = []
    for raw in rows:
        try:
            payload = json.loads(raw["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("id", int(raw["id"]))
        payload.setdefault("email", str(raw["email"] or ""))
        payload.setdefault("archived", bool(raw["archived"]))
        item = _redeem_credentials(payload)
        if not item:
            continue
        if wanted_group and item["group_name"].casefold() != wanted_group.casefold():
            continue
        if not wanted_group:
            legacy_plan = str(item.get("plan") or "").strip().lower()
            if "plus" not in legacy_plan or "free" in legacy_plan:
                continue
        result.append(item)
    return result


def _redeem_expired(expires_at: object) -> bool:
    if not str(expires_at or "").strip():
        return False
    parsed = _parse_iso_dt(str(expires_at))
    if parsed is None:
        return False
    now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
    return now >= parsed


def _redeem_row_value(row: sqlite3.Row | dict, key: str) -> object:
    """sqlite3.Row 缺列会抛 IndexError，dict 缺键会抛 KeyError；统一按空值处理。"""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _redeem_public_code(row: sqlite3.Row | dict) -> dict:
    quantity = max(1, int(row["quantity"] or 1))
    redeemed = max(0, int(row["redeemed_count"] or 0))
    raw_status = str(row["status"] or "active").strip().lower()
    if raw_status == "active" and redeemed >= quantity:
        status = "exhausted"
    elif raw_status == "active" and _redeem_expired(row["expires_at"]):
        status = "expired"
    else:
        status = raw_status
    account_group = str(_redeem_row_value(row, "account_group") or "").strip()
    return {
        "id": int(row["id"]),
        "code": str(row["code"]),
        "quantity": quantity,
        "redeemed_count": redeemed,
        "remaining": max(0, quantity - redeemed),
        "status": status,
        "expires_at": row["expires_at"] or "",
        "note": row["note"] or "",
        "account_group": account_group,
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def create_redeem_code(*, quantity: int = 1, expires_at: str | None = None, note: str = "", account_group: str = "") -> dict:
    try:
        quantity = int(quantity)
    except (TypeError, ValueError) as exc:
        raise RedeemError("兑换数量必须是整数", code="invalid_quantity") from exc
    if quantity < 1 or quantity > 1000:
        raise RedeemError("兑换数量需在 1~1000 之间", code="invalid_quantity")

    raw_group = str(account_group or "")
    if not raw_group.strip():
        raise RedeemError("创建 CDK 时必须指定兑换分组", code="group_required")
    try:
        group = _validate_account_group_name(raw_group)
    except ValueError as exc:
        raise RedeemError(str(exc), code="invalid_group") from exc

    expiry = str(expires_at or "").strip()
    if expiry:
        parsed = _parse_iso_dt(expiry)
        if parsed is None:
            raise RedeemError("过期时间格式无效，请使用 ISO 时间", code="invalid_expiry")
        now_dt = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        if parsed <= now_dt:
            raise RedeemError("过期时间必须晚于当前时间", code="invalid_expiry")
        expiry = parsed.isoformat(timespec="seconds")
    note = str(note or "").strip()[:200]
    now = _now()
    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        prefix = ""
        try:
            meta = _group_meta_rows(conn).get(_normalize_group_key(group))
            if not meta:
                raise RedeemError(f"分组「{group}」不存在，请先创建分组", code="group_not_found")
            group = str(meta.get("group_name") or group)
            prefix = _normalize_redeem_prefix(meta.get("redeem_prefix") or "")
        except RedeemError:
            raise
        except sqlite3.Error:
            raise RedeemError("分组信息暂时不可用，请稍后重试", code="group_unavailable", status=503)
        for _ in range(30):
            if prefix:
                code = f"{prefix}-{''.join(secrets.choice(_REDEEM_CODE_ALPHABET) for _ in range(16))}"
            else:
                code = "CDK-" + "".join(secrets.choice(_REDEEM_CODE_ALPHABET) for _ in range(20))
            try:
                conn.execute(
                    "INSERT INTO redeem_codes(code,quantity,redeemed_count,status,expires_at,note,account_group,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (code, quantity, 0, "active", expiry or None, note, group, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
                return _redeem_public_code(row)
            except sqlite3.IntegrityError:
                conn.rollback()
        raise RedeemError("生成兑换码失败，请重试", code="code_generation_failed", status=503)


def list_redeem_codes(*, limit: int | None = 200) -> list[dict]:
    """列出 CDK，并附带每个 CDK 已领取的账号快照。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        if limit is None:
            rows = conn.execute("SELECT * FROM redeem_codes ORDER BY id DESC").fetchall()
        else:
            limit = max(1, min(1000, int(limit or 200)))
            rows = conn.execute("SELECT * FROM redeem_codes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return []

        code_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in code_ids)
        claim_rows = conn.execute(
            "SELECT id, code_id, account_id, email, claimed_at "
            f"FROM redeem_claims WHERE code_id IN ({placeholders}) ORDER BY code_id ASC, id ASC",
            code_ids,
        ).fetchall()

    claims_by_code: dict[int, list[dict]] = {code_id: [] for code_id in code_ids}
    for claim in claim_rows:
        claims_by_code[int(claim["code_id"])].append({
            "id": int(claim["id"]),
            "account_id": int(claim["account_id"]),
            "email": str(claim["email"] or ""),
            "claimed_at": str(claim["claimed_at"] or ""),
        })

    result = []
    for row in rows:
        item = _redeem_public_code(row)
        redeemed_accounts = claims_by_code.get(int(row["id"]), [])
        item["redeemed_accounts"] = redeemed_accounts
        item["is_redeemed"] = bool(redeemed_accounts)
        result.append(item)
    return result


def redeem_stock_summary(group_name: str | None = None) -> dict:
    """可兑换库存统计：全部在 SQL 里数，不再逐行解析 payload。

    ``available`` 与 _redeem_candidate_rows 使用同一组条件（SQL 粗筛条件即
    _redeem_credentials 的硬性条件），``known_plus`` 统计分组内已开通 Plus 的账号数。
    """
    _ensure_sqlite()
    clause, params = _redeem_candidate_where(group_name)
    wanted_group = _account_group_name({"group_name": group_name}) if group_name else None
    plus_where = ["a.archived=0", "a.plan_plus=1"]
    plus_params: list[Any] = []
    if wanted_group:
        plus_where.append("a.group_key=?")
        plus_params.append(wanted_group)
    with _LOCK, closing(_sqlite_conn()) as conn:
        available = int(conn.execute(
            "SELECT COUNT(*) FROM accounts AS a "
            "INDEXED BY idx_accounts_redeemable "
            f"WHERE {clause} "
            "AND NOT EXISTS (SELECT 1 FROM redeem_claims AS c WHERE c.account_id = a.id)",
            params,
        ).fetchone()[0])
        total_plus = int(conn.execute(
            f"SELECT COUNT(*) FROM accounts AS a WHERE {' AND '.join(plus_where)}",
            plus_params,
        ).fetchone()[0])
    return {"available": available, "known_plus": total_plus}


def revoke_redeem_code(code_id: int) -> dict | None:
    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        row = conn.execute("SELECT * FROM redeem_codes WHERE id=?", (int(code_id),)).fetchone()
        if not row:
            return None
        public = _redeem_public_code(row)
        if public["status"] != "revoked":
            now = _now()
            conn.execute("UPDATE redeem_codes SET status='revoked', updated_at=? WHERE id=?", (now, int(code_id)))
            conn.commit()
            row = conn.execute("SELECT * FROM redeem_codes WHERE id=?", (int(code_id),)).fetchone()
            return _redeem_public_code(row)
        return public


def redeem_plus_accounts(code: str, quantity: int | None = None, *, request_id: str | None = None) -> dict:
    from core import redeem_delivery

    request_id = redeem_delivery.validate_request_id(request_id)
    if quantity is not None and (type(quantity) is not int or not 1 <= quantity <= 1000):
        raise RedeemError("兑换数量必须是 1~1000 的整数", code="invalid_quantity")
    requested_quantity = quantity
    normalized = _normalize_redeem_code(code)
    if not normalized or len(normalized) > 80:
        raise RedeemError("请输入有效的 CDK", code="invalid_code")

    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (normalized,)).fetchone()
            if not row:
                raise RedeemError("CDK 不存在或输入错误", code="code_not_found", status=404)
            public = _redeem_public_code(row)
            if public["status"] == "revoked":
                raise RedeemError("该 CDK 已被停用", code="code_revoked", status=410)
            try:
                resumed = redeem_delivery.resume(conn, code_id=int(row["id"]), code=normalized,
                                                 request_id=request_id, quantity=requested_quantity)
            except RedeemError as exc:
                if exc.code == "delivery_expired":
                    exc.remaining = public["remaining"] if public["status"] == "active" else 0
                raise
            if resumed is not None:
                resumed["remaining"] = public["remaining"]
                conn.commit()
                return resumed
            if public["status"] == "expired":
                raise RedeemError("该 CDK 已过期", code="code_expired", status=410)
            if public["status"] == "exhausted":
                raise RedeemError("该 CDK 已兑换完毕", code="code_exhausted", status=410)

            quantity = public["remaining"] if quantity is None else quantity
            if quantity > public["remaining"]:
                raise RedeemError(
                    f"本次兑换数量超过 CDK 剩余名额：申请 {quantity} 个，剩余 {public['remaining']} 个",
                    code="insufficient_quota", status=409,
                )
            group_name = public.get("account_group") or ""
            candidates = _redeem_candidate_rows(conn, group_name=group_name or None)
            if len(candidates) < quantity:
                label = f"分组「{group_name}」" if group_name else "Plus"
                raise RedeemError(
                    f"{label} 库存不足：本次需要 {quantity} 个，当前只有 {len(candidates)} 个可兑换账号",
                    code="insufficient_stock",
                    status=409,
                )

            selected = candidates[:quantity]
            now = _now()
            for item in selected:
                conn.execute(
                    "INSERT INTO redeem_claims(code_id,account_id,email,claimed_at) VALUES(?,?,?,?)",
                    (int(row["id"]), int(item["account_id"]), item["email"], now),
                )
            new_count = int(row["redeemed_count"] or 0) + len(selected)
            new_status = "exhausted" if new_count >= int(row["quantity"] or 1) else "active"
            conn.execute(
                "UPDATE redeem_codes SET redeemed_count=?, status=?, updated_at=? WHERE id=?",
                (new_count, new_status, now, int(row["id"])),
            )
            lines = [
                f"{item['email']}---{item['password']}---{item['totp_secret']}"
                for item in selected
            ]
            result = {
                "requested_quantity": requested_quantity,
                "code_id": int(row["id"]),
                "count": len(selected),
                "remaining": max(0, int(row["quantity"] or 1) - new_count),
                "group_name": group_name,
                "lines": lines,
                "accounts": [
                    {
                        "account_id": int(item["account_id"]),
                        "email": item["email"],
                        "plan": item["plan"],
                        "expires_at": item["expires_at"],
                    }
                    for item in selected
                ],
            }
            result = redeem_delivery.save(conn, code=normalized, request_id=request_id, result=result)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    email_source: str | None = None,
    registered_at: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
        requested_registered_at = str(registered_at or "").strip()

        if existing is None:
            row_id = _next_id(accounts)
            created_at = _now()
            row = {
                "id": row_id,
                "email": email,
                "created_at": created_at,
                "registered_at": requested_registered_at or created_at,
                "group_name": DEFAULT_ACCOUNT_GROUP,
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])
            row.setdefault("group_name", DEFAULT_ACCOUNT_GROUP)
            # 已有账号的注册时间不可因刷新 Token/补跑 Codex 被覆盖；老记录缺少
            # 新字段时优先沿用 created_at，只有两者都没有才使用当前时间兜底。
            if not str(row.get("registered_at") or "").strip():
                row["registered_at"] = (
                    requested_registered_at
                    or str(row.get("created_at") or "").strip()
                    or _now()
                )

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        # 顺手记录 AT 过期时间，让「AT 已过期」筛选对新注册/导入的账号也直接可用。
        _refresh_token_expiry(row)
        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        return row_id


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", email=email or "")
        if row is None:
            return False
        row["codex_status"] = codex_status
        row["codex_error"] = codex_error
        if str(codex_status or "").strip().lower() == "deactivated":
            # Codex 授权阶段判定为 deactivated，按账号废号处理，便于账号列表统一筛选。
            row["live_check_status"] = "deactivated"
            row["live_check_ok"] = False
            row["live_check_error"] = codex_error or "Codex 授权判定账号已废号"
            row["live_checked_at"] = _now()
        row["updated_at"] = _now()
        _write_collection_row(conn, "accounts", row)
        return True


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            auth_json = result.get("auth_json")
            row["codex_agent_token"] = json.dumps(auth_json, ensure_ascii=False)
            agent_filename = f"codex-agent-{str(row.get('email') or acc_id)}.json"
            stamp = _now()
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.execute(
                    "INSERT INTO codex_agent_accounts(account_id,email,filename,created_at,updated_at,payload) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET email=excluded.email, filename=excluded.filename, updated_at=excluded.updated_at, payload=excluded.payload",
                    (int(acc_id), str(row.get("email") or ""), agent_filename, stamp, stamp, json.dumps(auth_json, ensure_ascii=False)),
                )
                conn.commit()
            row.pop("codex_agent_auth_path", None)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def get_codex_agent_credential(acc_id: int) -> tuple[str, str] | None:
    """从 SQLite 获取 Agent 凭证，返回 JSON 文本和下载文件名。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        row = conn.execute("SELECT filename, payload FROM codex_agent_accounts WHERE account_id=?", (int(acc_id),)).fetchone()
    if not row:
        return None
    return json.dumps(json.loads(row["payload"]), ensure_ascii=False, indent=2) + "\n", row["filename"]


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id, email=email or None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        _write_collection_row(conn, "accounts", row)
        return True


def mark_account_plan_check_running(acc_id: int) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = _now()
        row["plan_check_error"] = None
        row["updated_at"] = _now()
        _write_collection_row(conn, "accounts", row)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id, email=email or None)
        if row is None:
            return False

        # 任务中心的手动取消/暂停状态：不参与 ok/failed 归类，也不覆盖上一次成功
        # 得到的套餐信息，只释放占用并让账号可以重新查询。
        explicit = str(result.get("status") or "").strip().lower()
        if explicit in {"cancelled", "canceled", "paused", "stopped"}:
            row["plan_check_status"] = "cancelled" if explicit in {"cancelled", "canceled"} else explicit
            row["plan_check_ok"] = False
            row["plan_check_error"] = result.get("error")
            if explicit != "paused":
                row["plan_check_completed_at"] = _now()
            if result.get("message") is not None:
                row["plan_check_message"] = result.get("message")
            row["updated_at"] = _now()
            _write_collection_row(conn, "accounts", row)
            return True

        ok = bool(result.get("ok"))
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or _now()
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            # 订阅接口会用 null 表示当前没有对应时间；按 key 写入可以清掉
            # 上一次查询遗留的挽留期，避免账号恢复正常后仍显示旧日期。
            for _k in (
                "subscription_active_start",
                "subscription_active_until",
                "subscription_became_delinquent_at",
                "subscription_grace_period_end_at",
                "subscription_billing_currency",
                "subscription_billing_period",
                "subscription_plan_type",
                "subscription_checked_at",
                "subscription_http_status",
                "subscription_error",
            ):
                if _k in result:
                    row[_k] = result.get(_k)
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["eligible_promo_campaigns"] = result.get("eligible_promo_campaigns") or {}
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        # 查套餐会在同一条会话里顺带刷新额度与「银行重置」券：只写结果列，
        # 不动 quota_check_* 状态机，任务中心仍只记录用户实际发起的任务。
        _apply_quota_columns(row, result)
        row["token_expired"] = result.get("token_expired")
        row["token_expires_at"] = result.get("token_expires_at")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        _write_collection_row(conn, "accounts", row)
        return True


def claim_account_quota_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的额度查询；已有未超时查询时返回 False。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id, email=email or None)
        if row is None:
            return False

        current_status = row.get("quota_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "quota_check_queued_at" if current_status == "queued" else "quota_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["quota_check_status"] = "queued"
        row["quota_check_trigger"] = str(trigger or "manual")
        row["quota_check_queued_at"] = now
        row["quota_check_started_at"] = None
        row["quota_check_completed_at"] = None
        row["quota_check_error"] = None
        row["updated_at"] = now
        _write_collection_row(conn, "accounts", row)
        return True


def mark_account_quota_check_running(acc_id: int) -> bool:
    """把已排队的额度查询标记为执行中。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id)
        if row is None or row.get("quota_check_status") not in {"queued", "running"}:
            return False
        row["quota_check_status"] = "running"
        row["quota_check_started_at"] = _now()
        row["quota_check_error"] = None
        row["updated_at"] = _now()
        _write_collection_row(conn, "accounts", row)
        return True


def recover_interrupted_quota_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("quota_check_status") not in {"queued", "running"}:
                continue
            row["quota_check_status"] = "failed"
            row["quota_check_ok"] = False
            row["quota_check_error"] = "WebUI 重启导致额度查询中断，请重新查询"
            row["quota_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


# 额度查询结果里按端点区分归属的字段：某个端点失败时不覆盖它上次成功的数值。
_QUOTA_BALANCE_FIELDS = (
    "quota_balance",
    "quota_balance_amount",
    "quota_currency",
    "quota_http_status",
    "quota_error",
    "quota_checked_at",
    "quota_response_preview",
    # 余额来自 wham/usage 的 credits.balance 兜底时为真（前端提示数值来源）。
    "quota_balance_fallback",
)
_QUOTA_RESET_CREDIT_FIELDS = (
    "reset_credits_available",
    "reset_credits_applicable",
    "reset_credits_expires_at",
    "reset_credits_detail",
    "reset_credits_http_status",
    "reset_credits_error",
    "reset_credits_checked_at",
    "reset_credits_response_preview",
)
# 用量窗口（5 小时/周月）与 credits 权益面：来自 wham/usage。
_QUOTA_USAGE_FIELDS = (
    "usage_plan_type",
    "usage_allowed",
    "usage_limit_reached",
    "usage_limit_reached_type",
    "usage_5h_percent",
    "usage_5h_window_seconds",
    "usage_5h_reset_at",
    "usage_5h_reset_after_seconds",
    "usage_5h_started",
    "usage_week_percent",
    "usage_week_window_seconds",
    "usage_week_reset_at",
    "usage_week_reset_after_seconds",
    "usage_week_started",
    "usage_http_status",
    "usage_error",
    "usage_checked_at",
    "usage_response_preview",
)
# credits 权益标记可能来自 wham/usage（优先）或 remaining_balance；缺值时保留旧值。
_QUOTA_CREDITS_FIELDS = (
    "quota_has_credits",
    "quota_unlimited",
    "quota_overage_limit_reached",
    "quota_credits_balance",
    "quota_credits_balance_amount",
)
# 供 piggyback（查套餐顺带刷新）落盘时裁剪出只属于额度查询的字段。
_QUOTA_RESULT_FIELDS = (
    _QUOTA_BALANCE_FIELDS
    + _QUOTA_RESET_CREDIT_FIELDS
    + _QUOTA_USAGE_FIELDS
    + _QUOTA_CREDITS_FIELDS
)


def _apply_quota_columns(row: dict, result: dict) -> bool:
    """把额度/用量/重置券结果写进账号字段，返回本次是否真的带来了查询结果。

    三个端点各自独立：某一端失败时只更新该端的错误和时间戳，保留上次成功数值。
    """
    checked_quota = bool(result.get("quota_checked_at"))
    checked_credits = bool(result.get("reset_credits_checked_at"))
    checked_usage = bool(result.get("usage_checked_at"))
    if not (checked_quota or checked_credits or checked_usage):
        return False

    # 余额端点失败但 wham/usage 的 credits.balance 兜底成功时，仍按成功落盘。
    quota_ok = checked_quota and (not result.get("quota_error") or bool(result.get("quota_balance_fallback")))
    credits_ok = checked_credits and not result.get("reset_credits_error")
    usage_ok = checked_usage and not result.get("usage_error")

    if quota_ok:
        for key in _QUOTA_BALANCE_FIELDS:
            if key in result:
                row[key] = result.get(key)
    elif checked_quota:
        row["quota_error"] = result.get("quota_error") or row.get("quota_error")
        row["quota_http_status"] = result.get("quota_http_status")
        row["quota_checked_at"] = result.get("quota_checked_at") or row.get("quota_checked_at")

    if credits_ok:
        for key in _QUOTA_RESET_CREDIT_FIELDS:
            if key in result:
                row[key] = result.get(key)
    elif checked_credits:
        row["reset_credits_error"] = result.get("reset_credits_error") or row.get("reset_credits_error")
        row["reset_credits_http_status"] = result.get("reset_credits_http_status")
        row["reset_credits_checked_at"] = result.get("reset_credits_checked_at") or row.get("reset_credits_checked_at")

    if usage_ok:
        # 用量窗口按原值落盘：窗口消失时对应字段会被写成 None，避免展示过期百分比。
        for key in _QUOTA_USAGE_FIELDS:
            if key in result:
                row[key] = result.get(key)
        for key in _QUOTA_CREDITS_FIELDS:
            if result.get(key) is not None:
                row[key] = result.get(key)
    elif checked_usage:
        row["usage_error"] = result.get("usage_error") or row.get("usage_error")
        row["usage_http_status"] = result.get("usage_http_status")
        row["usage_checked_at"] = result.get("usage_checked_at") or row.get("usage_checked_at")

    row["quota_result_json"] = json.dumps(
        {key: result.get(key) for key in _QUOTA_RESULT_FIELDS if key in result},
        ensure_ascii=False,
    )
    return True


def update_account_quota(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号额度/「银行重置」券查询结果。"""
    result = result or {}
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id, email=email or None)
        if row is None:
            return False

        # 手动取消/暂停与套餐查询保持一致：只释放占用，不覆盖上次成功的额度。
        explicit = str(result.get("status") or "").strip().lower()
        if explicit in {"cancelled", "canceled", "paused", "stopped"}:
            row["quota_check_status"] = "cancelled" if explicit in {"cancelled", "canceled"} else explicit
            row["quota_check_ok"] = False
            row["quota_check_error"] = result.get("error")
            if explicit != "paused":
                row["quota_check_completed_at"] = _now()
            if result.get("message") is not None:
                row["quota_check_message"] = result.get("message")
            row["updated_at"] = _now()
            _write_collection_row(conn, "accounts", row)
            return True

        ok = bool(result.get("ok"))
        row["quota_check_status"] = "success" if ok else "failed"
        row["quota_check_ok"] = ok
        row["quota_check_completed_at"] = _now()
        row["quota_check_error"] = None if ok else result.get("error")
        # 无论整体成败都写一次：端点各自的错误与时间戳要能落到列表上，
        # 由 _apply_quota_columns 判断哪些端点在本次查询里真的拿到了结果。
        _apply_quota_columns(row, result)

        row["quota_check_network_route"] = result.get("network_route")
        row["quota_check_proxy_used"] = result.get("proxy_used")
        row["quota_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        if ok:
            row["quota_last_success_at"] = result.get("checked_at") or _now()
        row["updated_at"] = _now()
        _write_collection_row(conn, "accounts", row)
        return True


def claim_account_extract(acc_id: int, trigger: str = "manual", link_type: str = "pix", *,
                           provider_id: int | None = None, provider_type: str | None = None,
                           provider_name: str | None = None, cdk_id: int | None = None,
                           cdk_suffix: str | None = None) -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if trigger != "plus_activation" and row.get("plus_activation_status") in {"queued", "checking", "extracting", "paying", "verifying"}:
            return False
        current_status = row.get("extract_link_status")
        active_statuses = {"queued", "running", "awaiting_blik", "unknown", "interrupted"}
        if current_status in active_statuses:
            if str(row.get("extract_link_provider_type") or "").lower() == "upi_git5":
                # Upstream may still be running after a timeout/restart. Only a
                # confirmed terminal result makes a new charged submission safe.
                return False
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                is_lumen = str(row.get("extract_link_provider_type") or "").lower() == "lumen"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else (3600 if is_lumen else _PLAN_CHECK_STALE_SECONDS)
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        for key in ("job_id", "task_id", "long_url", "hosted_instructions_url", "copy_paste", "image_url_png", "image_url_svg",
                    "expires_at", "result_json", "payment_status", "payment_method", "payment_link_type",
                    "progress", "cdk_remaining"):
            row.pop("extract_link_" + key, None)
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        row["extract_link_awaiting_blik"] = False
        row["extract_link_task_id"] = None
        if provider_id is not None:
            row["extract_link_provider_id"] = int(provider_id)
        else:
            row.pop("extract_link_provider_id", None)
        if provider_type is not None:
            row["extract_link_provider_type"] = str(provider_type).lower()
        else:
            row.pop("extract_link_provider_type", None)
        if provider_name is not None:
            row["extract_link_provider_name"] = str(provider_name)[:120]
        else:
            row.pop("extract_link_provider_name", None)
        if cdk_id is not None:
            row["extract_link_cdk_id"] = int(cdk_id)
        else:
            row.pop("extract_link_cdk_id", None)
        if cdk_suffix is not None:
            row["extract_link_cdk_suffix"] = str(cdk_suffix)[:12]
        else:
            row.pop("extract_link_cdk_suffix", None)
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None, *,
                           expected_task_id: str | None = None, preserve_terminal: bool = False) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if expected_task_id is not None and row.get("extract_link_task_id") != expected_task_id:
            return False
        if preserve_terminal and row.get("extract_link_status") in {"success", "failed", "stopped"}:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped", "unknown", "interrupted"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status in {"running", "awaiting_blik", "queued"} else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("task_id") is not None:
            row["extract_link_task_id"] = result.get("task_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("provider_id") is not None:
            row["extract_link_provider_id"] = int(result.get("provider_id"))
        if result.get("provider_type") is not None:
            row["extract_link_provider_type"] = str(result.get("provider_type")).lower()
        if result.get("provider_name") is not None:
            row["extract_link_provider_name"] = str(result.get("provider_name"))[:120]
        if result.get("cdk_id") is not None:
            row["extract_link_cdk_id"] = int(result.get("cdk_id"))
        if result.get("cdk_suffix") is not None:
            row["extract_link_cdk_suffix"] = str(result.get("cdk_suffix"))[:12]
        if result.get("awaiting_blik") is not None:
            row["extract_link_awaiting_blik"] = bool(result.get("awaiting_blik"))
        if result.get("payment_status") is not None:
            row["extract_link_payment_status"] = str(result.get("payment_status"))[:80]
        if result.get("progress") is not None:
            try:
                row["extract_link_progress"] = max(0, min(100, int(result.get("progress"))))
            except (TypeError, ValueError):
                pass
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_hosted_instructions_url"] = payload.get("hosted_instructions_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("payment_status") is not None:
                row["extract_link_payment_status"] = str(payload.get("payment_status"))[:80]
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running", "awaiting_blik", "unknown", "interrupted"}:
                continue
            row["extract_link_status"] = "interrupted"
            row["extract_link_ok"] = False
            row["extract_link_awaiting_blik"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断；请先刷新/核对原任务，不要直接重提"
            row["extract_link_message"] = "任务中断，需核对受理状态"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def _account_matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _parse_iso_dt(value: str | None, end_of_day: bool = False) -> datetime | None:
    """宽松解析 ISO 日期/时间字符串；支持 YYYY-MM-DD 或完整 ISO；解析失败返回 None。

    end_of_day=True 时，纯日期（YYYY-MM-DD）按当天 23:59:59.999999 解析，
    用于 date_to 过滤（保证包含截止当天）；完整时间串原样返回。
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10 and text[4] == "-":
            if end_of_day:
                return datetime.fromisoformat(text + "T23:59:59.999999")
            return datetime.fromisoformat(text + "T00:00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _matches_codex_status_filter(row: dict, codex_filter: str | None) -> bool:
    codex_filter = str(codex_filter or "").strip().lower()
    if not codex_filter:
        return True
    status = str(row.get("codex_status") or "").strip().lower()
    live_status = str(row.get("live_check_status") or "").strip().lower()
    if codex_filter in {"all", "*"}:
        return True
    if codex_filter == "deactivated":
        return live_status == "deactivated"
    return status == codex_filter


def _matches_at_status_filter(row: dict, at_filter: str | None) -> bool:
    """按 AT 是否过期筛选账号，口径与 `_account_at_state_sql` 一致。"""
    wanted = _normalize_at_filter(at_filter)
    if not wanted:
        return True
    if wanted == _FILTER_NONE:
        return False
    return _account_at_state(row) == wanted


def _matches_live_status_filter(row: dict, live_filter: str | None) -> bool:
    """按查活状态筛选账号；failed 只匹配查活失败，不含已停用/已取消。"""
    wanted = _normalize_live_filter(live_filter)
    if not wanted:
        return True
    if wanted == (_FILTER_NONE,):
        return False
    status = str(row.get("live_check_status") or "").strip().lower()
    return status in wanted


def _matches_totp_status_filter(row: dict, totp_filter: str | None) -> bool:
    """按 2FA/TOTP 是否已配置及设置任务状态筛选账号。"""
    totp_filter = str(totp_filter or "").strip().lower()
    if not totp_filter or totp_filter in {"all", "*"}:
        return True

    enabled = bool(str(row.get("totp_secret") or "").strip())
    setup_status = str(row.get("totp_setup_status") or "").strip().lower()
    if totp_filter in {"enabled", "on", "active"}:
        return enabled
    if totp_filter in {"disabled", "off", "not_enabled", "unset"}:
        return not enabled
    if totp_filter in {"pending", "setup", "setting", "queued", "running"}:
        return setup_status in {"queued", "running"}
    if totp_filter in {"failed", "stopped"}:
        return setup_status == totp_filter
    return setup_status == totp_filter


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]
    decorated = [_decorate_account(r) for r in rows]
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    decorated = [r for r in decorated if _matches_codex_status_filter(r, codex_filter)]
    decorated = [r for r in decorated if _matches_totp_status_filter(r, totp_filter)]
    decorated = [r for r in decorated if _matches_at_status_filter(r, at_filter)]
    decorated = [r for r in decorated if _matches_live_status_filter(r, live_filter)]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    if group_filter:
        wanted = _account_group_name({"group_name": group_filter})
        decorated = [r for r in decorated if (r.get("group_name") or "").casefold() == wanted.casefold()]
    # 兑换状态保存在 redeem_claims 表，不在账号 payload 中，这里统一查一次再过滤。
    redeemed_filter = _normalize_redemption_filter(redemption_filter)
    if redeemed_filter is not None:
        claims = _redeem_claim_map(r.get("id") for r in decorated)
        decorated = [
            r for r in decorated
            if (int(r.get("id") or 0) in claims) is redeemed_filter
        ]
    # 按注册完成时间筛选（date_from/date_to 为 ISO 字符串或 YYYY-MM-DD）。
    if date_from or date_to:
        d_from = _parse_iso_dt(date_from)
        d_to = _parse_iso_dt(date_to, end_of_day=True)
        # date_from/date_to 是本地日期；外部导入的带时区值先转换到本地再比较。
        if d_from is not None and d_from.tzinfo is not None:
            d_from = d_from.astimezone().replace(tzinfo=None)
        if d_to is not None and d_to.tzinfo is not None:
            d_to = d_to.astimezone().replace(tzinfo=None)
        if d_from or d_to:
            filtered = []
            for r in decorated:
                ct = _parse_iso_dt(str(_account_registered_at(r) or ""))
                if ct is None:
                    continue
                if ct.tzinfo is not None:
                    ct = ct.astimezone().replace(tzinfo=None)
                if d_from and ct < d_from:
                    continue
                if d_to and ct > d_to:
                    continue
                filtered.append(r)
            decorated = filtered
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    with _LOCK, _connection() as conn:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        extra_where, extra_params = _account_filter_sql(
            plan_filter=plan_filter,
            codex_filter=codex_filter,
            totp_filter=totp_filter,
            group_filter=group_filter,
            redemption_filter=redemption_filter,
            at_filter=at_filter,
            live_filter=live_filter,
            conn=conn,
        )
        candidates, total, latest = _query_collection_page(
            "accounts",
            archived=archived,
            q=q,
            date_from=date_from,
            date_to=date_to,
            extra_where=extra_where,
            extra_params=extra_params,
            limit=limit,
            offset=offset,
            conn=conn,
        )
        rows = [_decorate_account_status(row) for row in candidates]
        items = []
        scan_fields = _PLAN_CHECK_SCAN_FIELDS
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in _PLAN_CHECK_PLAIN_FIELDS:
                value = row.get(key)
                if value is not None and value != "":
                    item[key] = value
            # scan_request_* 字段即使为空也要在“发起过扫码”时返回（前端按存在性判断）。
            if row.get("scan_request_status"):
                for key in scan_fields:
                    item[key] = row.get(key)
            item["totp_enabled"] = bool(str(row.get("totp_secret") or "").strip())
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["codex_agent_has_token"] = bool(str(row.get("codex_agent_token") or "").strip())
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            # 轻量轮询也要带上 AT 过期标记，否则列表徽标会在轮询后被旧值覆盖。
            item["at_expired"] = bool(row.get("at_expired"))
            items.append(item)
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            [
                {
                    "id": row.get("id"),
                    "updated_at": row.get("updated_at"),
                    "plan_check_status": row.get("plan_check_status"),
                    "plan_check_ok": row.get("plan_check_ok"),
                    "plan_check_error": row.get("plan_check_error"),
                    "current_plan_type": row.get("current_plan_type"),
                    "plan_type": row.get("plan_type"),
                    "plus_trial_eligible": row.get("plus_trial_eligible"),
                    "eligible_promo_campaigns": row.get("eligible_promo_campaigns"),
                    "is_delinquent": row.get("is_delinquent"),
                    "subscription_became_delinquent_at": row.get("subscription_became_delinquent_at"),
                    "subscription_grace_period_end_at": row.get("subscription_grace_period_end_at"),
                    "subscription_error": row.get("subscription_error"),
                    "extract_link_status": row.get("extract_link_status"),
                    "extract_link_progress": row.get("extract_link_progress"),
                    "extract_link_payment_status": row.get("extract_link_payment_status"),
                    "extract_link_job_id": row.get("extract_link_job_id"),
                    "extract_link_message": row.get("extract_link_message"),
                    "scan_request_status": row.get("scan_request_status"),
                    "scan_request_provider": row.get("scan_request_provider"),
                    "scan_request_task_id": row.get("scan_request_task_id"),
                    "scan_request_message": row.get("scan_request_message"),
                    "scan_request_error": row.get("scan_request_error"),
                    "plus_activation_status": row.get("plus_activation_status"),
                    "plus_activation_message": row.get("plus_activation_message"),
                    "plus_activation_updated_at": row.get("plus_activation_updated_at"),
                    "scan_request_checked_at": row.get("scan_request_checked_at"),
                    "codex_status": row.get("codex_status"),
                    "codex_agent_status": row.get("codex_agent_status"),
                    "totp_setup_status": row.get("totp_setup_status"),
                    "totp_setup_ok": row.get("totp_setup_ok"),
                    "totp_setup_error": row.get("totp_setup_error"),
                    "totp_setup_message": row.get("totp_setup_message"),
                    "totp_setup_checked_at": row.get("totp_setup_checked_at"),
                    "totp_setup_started_at": row.get("totp_setup_started_at"),
                    "totp_setup_completed_at": row.get("totp_setup_completed_at"),
                    "totp_enabled": bool(str(row.get("totp_secret") or "").strip()),
                    "email": row.get("email"),
                    "original_email": row.get("original_email"),
                    "email_source": row.get("email_source"),
                    "group_name": _account_group_name(row),
                    "email_change_status": row.get("email_change_status"),
                    "email_change_error": row.get("email_change_error"),
                }
                for row in rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(
    limit: int = 500,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
) -> list[dict]:
    # 非分页兼容接口也走同一条 SQL 分页路径，避免 limit=500 时先读取整张表。
    result = list_accounts_page(
        limit=limit,
        offset=offset,
        archived=archived,
        plan_filter=plan_filter,
        codex_filter=codex_filter,
        q=q,
        date_from=date_from,
        date_to=date_to,
        totp_filter=totp_filter,
        group_filter=group_filter,
        redemption_filter=redemption_filter,
        at_filter=at_filter,
        live_filter=live_filter,
    )
    return result["items"]


def find_accounts_by_emails(
    emails: list[str],
    *,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
) -> list[dict]:
    """按当前账号筛选条件精确匹配邮箱，返回已装饰的账号列表。

    命中集合就是请求里的那几个邮箱，因此直接在 SQL 里用 email_key/orig_email_key
    索引取行，不再为了找 100 个邮箱把整张账号表读进 Python。
    """
    targets = {
        str(email or "").strip().casefold()
        for email in (emails or [])
        if str(email or "").strip()
    }
    if not targets:
        return []

    if all(_is_ascii(target) for target in targets):
        ordered = sorted(targets)
        placeholders = ",".join("?" for _ in ordered)
        with _LOCK, _connection() as conn:
            extra_where, extra_params = _account_filter_sql(
                plan_filter=plan_filter,
                codex_filter=codex_filter,
                totp_filter=totp_filter,
                group_filter=group_filter,
                redemption_filter=redemption_filter,
                at_filter=at_filter,
                live_filter=live_filter,
                conn=conn,
            )
            # 两条 OR 分支各查一次：写成 email_key IN (...) OR orig_email_key IN (...)
            # 时 SQLite 会放弃两个索引改走全表扫描。
            found: dict[int, dict] = {}
            for column, index_name in (("email_key", "idx_accounts_email_key"), ("orig_email_key", "idx_accounts_orig_email_key")):
                clause, params = _collection_where(
                    "accounts", archived=archived, date_from=date_from, date_to=date_to,
                    extra_where=[*extra_where, f"{column} IN ({placeholders})"],
                    extra_params=[*extra_params, *ordered],
                )
                # INDEXED BY：带 archived 条件时优化器会放弃等值索引改扫 archived 索引。
                for row in conn.execute(
                    f"SELECT payload FROM accounts INDEXED BY {index_name} WHERE {clause}", params
                ):
                    payload = json.loads(row["payload"])
                    found[int(payload.get("id") or 0)] = payload
            rows = [found[key] for key in sorted(found, reverse=True)]
        return _attach_redeem_claims([_decorate_account(row) for row in rows])

    rows = _filtered_decorated_accounts(
        archived=archived,
        plan_filter=plan_filter,
        codex_filter=codex_filter,
        date_from=date_from,
        date_to=date_to,
        totp_filter=totp_filter,
        group_filter=group_filter,
        redemption_filter=redemption_filter,
        at_filter=at_filter,
        live_filter=live_filter,
    )
    matched = [
        row for row in rows
        if str(row.get("email") or "").strip().casefold() in targets
        or str(row.get("original_email") or "").strip().casefold() in targets
    ]
    return _attach_redeem_claims(matched)


def list_accounts_page(
    limit: int = 50,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
) -> dict:
    with _LOCK, _connection() as conn:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        extra_where, extra_params = _account_filter_sql(
            plan_filter=plan_filter,
            codex_filter=codex_filter,
            totp_filter=totp_filter,
            group_filter=group_filter,
            redemption_filter=redemption_filter,
            at_filter=at_filter,
            live_filter=live_filter,
            conn=conn,
        )
        candidates, total, latest = _query_collection_page(
            "accounts",
            archived=archived,
            q=q,
            date_from=date_from,
            date_to=date_to,
            extra_where=extra_where,
            extra_params=extra_params,
            limit=limit,
            offset=offset,
            conn=conn,
        )
        items = _attach_redeem_claims([_decorate_account(row) for row in candidates])
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def list_account_ids_page(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    group_filter: str | None = None,
    redemption_filter: str | None = None,
    at_filter: str | None = None,
    live_filter: str | None = None,
) -> dict:
    """账号「全选」用的轻量分页：只返回 ID，不解析 payload。"""
    with _LOCK, _connection() as conn:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        extra_where, extra_params = _account_filter_sql(
            plan_filter=plan_filter,
            codex_filter=codex_filter,
            totp_filter=totp_filter,
            group_filter=group_filter,
            redemption_filter=redemption_filter,
            at_filter=at_filter,
            live_filter=live_filter,
            conn=conn,
        )
        ids, total = _query_collection_ids(
            "accounts",
            archived=archived,
            q=q,
            date_from=date_from,
            date_to=date_to,
            extra_where=extra_where,
            extra_params=extra_params,
            limit=limit,
            offset=offset,
            conn=conn,
        )
        return {"ids": ids, "total": total, "offset": offset, "limit": limit}


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM accounts WHERE id=? LIMIT 1", (int(acc_id),)).fetchone()
        return _decorate_account(json.loads(row["payload"])) if row else None


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        _ensure_sqlite()
        wanted = (email or "").lower()
        with closing(_sqlite_conn()) as conn:
            # email_key 是 lower(trim(email)) 的生成列并有索引，ASCII 邮箱可以走索引；
            # 非 ASCII 邮箱仍需 Python 的 Unicode lower()，回退到逐行比较（少见）。
            if _is_ascii(wanted):
                row = conn.execute(
                    "SELECT payload FROM accounts WHERE email_key=? ORDER BY id LIMIT 1", (wanted,)
                ).fetchone()
            else:
                conn.create_function("email_lower", 1, lambda value: (value or "").lower(), deterministic=True)
                row = conn.execute(
                    "SELECT payload FROM accounts WHERE email_lower(email)=? ORDER BY id LIMIT 1",
                    (wanted,),
                ).fetchone()
        return _decorate_account(json.loads(row["payload"])) if row else None


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def claim_account_email_change(acc_id: int, source: str, trigger: str = "manual") -> bool:
    """原子占用账号邮箱换绑任务。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("email_change_status") in {"queued", "running"}:
            return False
        now = _now()
        row.update({
            "email_change_status": "queued", "email_change_ok": False,
            "email_change_source": str(source or ""), "email_change_trigger": str(trigger or "manual"),
            "email_change_queued_at": now, "email_change_started_at": None,
            "email_change_completed_at": None, "email_change_error": None, "updated_at": now,
        })
        _save_accounts(rows)
        return True


def mark_account_email_change_running(acc_id: int, new_email: str) -> bool:
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("email_change_status") not in {"queued", "running"}:
            return False
        row.update({"email_change_status": "running", "email_change_new_email": new_email,
                    "email_change_started_at": _now(), "email_change_error": None, "updated_at": _now()})
        _save_accounts(rows)
        return True


def finish_account_email_change(
    acc_id: int, *, ok: bool, new_email: str | None = None, source: str | None = None,
    material_line: str | None = None, error: str | None = None, status: str | None = None,
) -> bool:
    """写回换绑结果；成功时保留初始邮箱并将账号主邮箱切换为新邮箱。

    ``status`` 用于任务中心手动取消等非成功/失败终态（cancelled / stopped / paused）。
    """
    terminal = str(status or "").strip().lower()
    if terminal and terminal not in {"success", "failed"}:
        ok = False
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        if ok and new_email:
            old_email = str(row.get("email") or "").strip()
            row["original_email"] = str(row.get("original_email") or old_email)
            history = row.get("email_history") if isinstance(row.get("email_history"), list) else []
            if old_email and old_email.lower() not in {str(x).lower() for x in history}:
                history.append(old_email)
            row["email_history"] = history
            row["email"] = str(new_email).strip()
            row["email_source"] = str(source or row.get("email_source") or "")
            row["original_email_line"] = str(material_line or new_email)
            # 清理旧邮箱来源遗留的 Outlook 凭证；若新来源仍为 Outlook 则写入新素材。
            row["password"] = ""
            row["client_id"] = ""
            row["refresh_token"] = ""
            if str(source or "") == "outlook":
                mailbox = _find_by_email(_load_outlook(), str(new_email))
                if mailbox:
                    row["password"] = mailbox.get("password") or ""
                    row["client_id"] = mailbox.get("client_id") or ""
                    row["refresh_token"] = mailbox.get("refresh_token") or ""
            # 抓包表明 verify 成功后当前 OAuth token 会立即失效。
            row["access_token"] = ""
            row["token_expired"] = True
            row["live_check_status"] = ""
            row["email_change_new_email"] = str(new_email).strip()
        row["email_change_status"] = terminal or ("success" if ok else "failed")
        row["email_change_ok"] = bool(ok)
        row["email_change_error"] = None if ok else str(error or "换绑失败")[:1000]
        row["email_change_completed_at"] = now
        row["updated_at"] = now
        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def recover_interrupted_email_changes() -> int:
    """启动时将上次进程中断的邮箱换绑任务标记为失败。"""
    with _LOCK:
        rows = _load_accounts()
        count = 0
        for row in rows:
            if row.get("email_change_status") not in {"queued", "running"}:
                continue
            row.update({
                "email_change_status": "failed", "email_change_ok": False,
                "email_change_error": "WebUI 重启导致邮箱换绑中断，请重新操作",
                "email_change_completed_at": _now(), "updated_at": _now(),
            })
            count += 1
        if count:
            _save_accounts(rows)
        return count


def update_account_liveness(acc_id: int, result: dict | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
                # 查活换了新 AT，旧的「已过期」标记必须一起刷新，否则筛选结果会失真。
                _refresh_token_expiry(row)
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            if "proxy_used" in result:
                row["live_check_proxy_used"] = result.get("proxy_used")
            row["live_check_fingerprint_text"] = result.get("fingerprint_text") or row.get("live_check_fingerprint_text")
            if result.get("fingerprint"):
                row["live_check_fingerprint"] = result.get("fingerprint")
            row["live_check_error"] = None

        row["copy_line"] = _account_line(row)
        _write_collection_row(conn, "accounts", row)
        return True


def claim_account_totp_setup(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 2FA 设置任务；已有未超时任务时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("totp_setup_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "totp_setup_queued_at" if current_status == "queued" else "totp_setup_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["totp_setup_status"] = "queued"
        row["totp_setup_ok"] = False
        row["totp_setup_trigger"] = str(trigger or "manual")
        row["totp_setup_queued_at"] = now
        row["totp_setup_started_at"] = None
        row["totp_setup_completed_at"] = None
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def mark_account_totp_setup_running(acc_id: int) -> bool:
    """把 2FA 设置任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("totp_setup_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["totp_setup_status"] = "running"
        row["totp_setup_started_at"] = now
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_totp_secret(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 2FA/TOTP 设置结果。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["totp_setup_status"] = status
        row["totp_setup_ok"] = ok
        row["totp_setup_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["totp_setup_completed_at"] = _now()
        row["totp_setup_error"] = None if ok or status == "running" else result.get("error")
        secret = str(result.get("totp_secret") or "").strip()
        if ok and secret:
            row["totp_secret"] = secret
        if result.get("message") is not None:
            row["totp_setup_message"] = result.get("message")
        row["copy_line"] = _account_line(row)
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def recover_interrupted_totp_setups() -> int:
    """服务启动时恢复上次进程中断的 2FA 设置状态。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("totp_setup_status") not in {"queued", "running"}:
                continue
            row["totp_setup_status"] = "failed"
            row["totp_setup_ok"] = False
            row["totp_setup_error"] = "WebUI 重启导致 2FA 设置中断，请重新开启"
            row["totp_setup_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def claim_account_live_check(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id)
        if row is None:
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        _write_collection_row(conn, "accounts", row)
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int) -> bool:
    """把账号查活任务标记为运行中。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "accounts", row_id=acc_id)
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        _write_collection_row(conn, "accounts", row)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def update_accounts_group(account_ids: list[int] | None, group_name: str) -> tuple[list[dict], list[dict]]:
    """
    批量设置已注册账号的分组。
    返回 (updated, skipped)，updated 元素含 id/email/group_name。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    try:
        name = _validate_account_group_name(str(group_name or ""))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["group_name"] = name
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "group_name": name})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
            update_account_group_meta(name)
    return updated, skipped


def _group_meta_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """读取分组元数据（前缀、是否公开展示库存），key 为 casefold 后的分组名。"""
    try:
        rows = conn.execute("SELECT * FROM account_groups").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, dict] = {}
    for raw in rows:
        name = _account_group_name({"group_name": raw["group_name"]})
        out[_normalize_group_key(name)] = {
            "group_name": name,
            "redeem_prefix": str(raw["redeem_prefix"] or "").strip(),
            "public_stock": bool(raw["public_stock"]),
        }
    return out


def _upsert_group_meta(
    conn: sqlite3.Connection,
    group_name: str,
    *,
    redeem_prefix: str | None = None,
    public_stock: bool | None = None,
) -> dict:
    name = _validate_account_group_name(str(group_name or ""))
    now = _now()
    conn.execute(
        "INSERT INTO account_groups(group_name,redeem_prefix,public_stock,created_at,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(group_name) DO NOTHING",
        (name, "", 0, now, now),
    )
    if redeem_prefix is not None:
        conn.execute(
            "UPDATE account_groups SET redeem_prefix=?, updated_at=? WHERE group_name=?",
            (_normalize_redeem_prefix(redeem_prefix), now, name),
        )
    if public_stock is not None:
        conn.execute(
            "UPDATE account_groups SET public_stock=?, updated_at=? WHERE group_name=?",
            (1 if public_stock else 0, now, name),
        )
    row = conn.execute("SELECT * FROM account_groups WHERE group_name=?", (name,)).fetchone()
    return {
        "group_name": name,
        "redeem_prefix": str(row["redeem_prefix"] or "").strip() if row else "",
        "public_stock": bool(row["public_stock"]) if row else False,
    }


def _normalize_redeem_prefix(value: object) -> str:
    """CDK 前缀允许 ASCII 字母、数字、短横线，最长 16 位；空值表示使用默认 CDK 前缀。"""
    prefix = str(value or "").strip().upper()
    if len(prefix) > 16:
        raise ValueError("CDK 前缀最长 16 个字符")
    if any(ch not in (string.ascii_uppercase + string.digits + "-") for ch in prefix):
        raise ValueError("CDK 前缀只能包含字母、数字和短横线")
    return prefix


def update_account_group_meta(
    group_name: str,
    *,
    redeem_prefix: str | None = None,
    public_stock: bool | None = None,
) -> dict:
    """设置分组元数据：CDK 前缀、是否在公开页展示该分组库存。"""
    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        with conn:
            return _upsert_group_meta(conn, group_name, redeem_prefix=redeem_prefix, public_stock=public_stock)


def list_account_groups(*, public_only: bool = False) -> list[dict]:
    """按账号 group_name 聚合统计：账号数、可兑换数（有登录密码、未归档、未废号）。

    public_only=True 时只返回管理员标记为公开展示库存的分组。

    统计全部在 SQLite 里完成：分组、登录密码、查活状态都是生成列，未兑换用
    redeem_claims 的索引判断，因此不再需要把每个账号的 payload 解析成 Python 字典
    （旧实现在 5 万账号时要 500ms 以上，而 WebUI 每次页面加载都会调用）。
    """
    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        totals = {
            str(row["group_key"]): int(row["n"])
            for row in conn.execute("SELECT group_key, COUNT(*) AS n FROM accounts GROUP BY group_key")
        }
        # INDEXED BY 只是执行计划提示：索引不存在（迁移被中断、旧库只补了一半）时
        # 会直接报 "no such index" 让接口 500，所以确认存在才加，缺失就退回普通查询。
        redeemable_hint = (
            "INDEXED BY idx_accounts_redeemable "
            if "idx_accounts_redeemable" in _existing_indexes(conn)
            else ""
        )
        redeemable = {
            str(row["group_key"]): int(row["n"])
            for row in conn.execute(
                # INDEXED BY：优化器会误选 (archived) 索引并逐行回表解析 payload，
                # 这里强制走只覆盖判定列的索引（实测 30ms -> 0.3ms）。
                "SELECT a.group_key AS group_key, COUNT(*) AS n FROM accounts AS a "
                f"{redeemable_hint}"
                "WHERE a.archived=0 AND a.has_password=1 AND a.live_norm<>'deactivated' "
                f"AND trim(a.email, {_SQL_WS_CHARS})<>'' "
                "AND NOT EXISTS (SELECT 1 FROM redeem_claims AS rc WHERE rc.account_id=a.id) "
                "GROUP BY a.group_key"
            )
        }
        meta_map = _group_meta_rows(conn)
    counters: dict[str, dict] = {}
    for group, total in totals.items():
        counters[group] = {
            "group_name": group or DEFAULT_ACCOUNT_GROUP,
            "total": total,
            "redeemable": redeemable.get(group, 0),
        }

    out: list[dict] = []
    for entry in counters.values():
        meta = meta_map.get(_normalize_group_key(entry["group_name"])) or {}
        entry["redeem_prefix"] = str(meta.get("redeem_prefix") or "")
        entry["public_stock"] = bool(meta.get("public_stock"))
        out.append(entry)
    # 元数据里存在、但当前没有账号的分组也要展示，便于提前配置前缀。
    known = {_normalize_group_key(x["group_name"]) for x in out}
    for key, meta in meta_map.items():
        if key in known:
            continue
        out.append({
            "group_name": meta["group_name"],
            "total": 0,
            "redeemable": 0,
            "redeem_prefix": str(meta.get("redeem_prefix") or ""),
            "public_stock": bool(meta.get("public_stock")),
        })
    if public_only:
        out = [x for x in out if x["public_stock"]]
    return sorted(out, key=lambda x: (x["group_name"] != DEFAULT_ACCOUNT_GROUP, x["group_name"]))


def rename_account_group(old_name: str, new_name: str) -> tuple[int, str]:
    """原子重命名分组，并同步账号与未领取 CDK 的分组绑定。"""
    try:
        old = _validate_account_group_name(str(old_name or ""))
        new = _validate_account_group_name(str(new_name or ""))
    except ValueError as exc:
        raise RedeemError(str(exc), code="invalid_group") from exc
    if old == DEFAULT_ACCOUNT_GROUP:
        raise RedeemError("默认分组不能重命名", code="default_group_protected")
    if _normalize_group_key(old) == _normalize_group_key(new):
        return 0, old

    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            metas = conn.execute("SELECT * FROM account_groups").fetchall()
            source_meta = next((row for row in metas if _normalize_group_key(row["group_name"]) == _normalize_group_key(old)), None)
            target_exists = next((row for row in metas if _normalize_group_key(row["group_name"]) == _normalize_group_key(new)), None)
            account_rows = conn.execute("SELECT id, payload FROM accounts").fetchall()
            matching = []
            for row in account_rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                if isinstance(payload, dict) and _normalize_group_key(_account_group_name(payload)) == _normalize_group_key(old):
                    matching.append((row, payload))
            if source_meta is None and not matching:
                raise RedeemError(f"分组「{old}」不存在", code="group_not_found")
            if target_exists:
                raise RedeemError(f"分组「{new}」已存在", code="group_exists")

            now = _now()
            if source_meta is not None:
                conn.execute(
                    "UPDATE account_groups SET group_name=?, updated_at=? WHERE group_name=?",
                    (new, now, source_meta["group_name"]),
                )
            else:
                conn.execute(
                    "INSERT INTO account_groups(group_name,redeem_prefix,public_stock,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (new, "", 0, now, now),
                )
            for row, payload in matching:
                payload["group_name"] = new
                payload["updated_at"] = now
                conn.execute(
                    "UPDATE accounts SET payload=?, updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), now, int(row["id"])),
                )
            code_rows = conn.execute("SELECT id, account_group FROM redeem_codes WHERE account_group IS NOT NULL").fetchall()
            for code_row in code_rows:
                if _normalize_group_key(str(code_row["account_group"] or "")) == _normalize_group_key(old):
                    conn.execute("UPDATE redeem_codes SET account_group=?, updated_at=? WHERE id=?", (new, now, int(code_row["id"])))
            conn.commit()
            return len(matching), new
        except Exception:
            conn.rollback()
            raise


def delete_account_group(group_name: str, *, merge_to: str | None = None) -> tuple[int, str]:
    """删除非默认分组；有有效未耗尽 CDK 时拒绝，账号移入目标分组。"""
    try:
        target = _validate_account_group_name(str(group_name or ""))
        destination = _validate_account_group_name(str(merge_to or DEFAULT_ACCOUNT_GROUP))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if target == DEFAULT_ACCOUNT_GROUP:
        raise ValueError("默认分组不能删除")
    if _normalize_group_key(target) == _normalize_group_key(destination):
        raise ValueError("目标分组不能与要删除的分组相同")

    _ensure_sqlite()
    with _LOCK, closing(_sqlite_conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            metas = conn.execute("SELECT * FROM account_groups").fetchall()
            source_meta = next((row for row in metas if _normalize_group_key(row["group_name"]) == _normalize_group_key(target)), None)
            account_rows = conn.execute("SELECT id, payload FROM accounts").fetchall()
            matching = []
            for row in account_rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                if isinstance(payload, dict) and _normalize_group_key(_account_group_name(payload)) == _normalize_group_key(target):
                    matching.append((row, payload))
            if source_meta is None and not matching:
                raise ValueError(f"分组「{target}」不存在")

            for code_row in conn.execute("SELECT id, account_group, status, redeemed_count, quantity, expires_at FROM redeem_codes WHERE account_group IS NOT NULL").fetchall():
                if _normalize_group_key(str(code_row["account_group"] or "")) != _normalize_group_key(target):
                    continue
                status = str(code_row["status"] or "active").lower()
                if status == "active" and int(code_row["redeemed_count"] or 0) < int(code_row["quantity"] or 1) and not _redeem_expired(code_row["expires_at"]):
                    raise ValueError("该分组仍有有效 CDK，请先停用或耗尽后再删除")

            now = _now()
            destination_exists = next((row for row in metas if _normalize_group_key(row["group_name"]) == _normalize_group_key(destination)), None)
            if not destination_exists:
                conn.execute(
                    "INSERT INTO account_groups(group_name,redeem_prefix,public_stock,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (destination, "", 0, now, now),
                )
            for row, payload in matching:
                payload["group_name"] = destination
                payload["updated_at"] = now
                conn.execute("UPDATE accounts SET payload=?, updated_at=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), now, int(row["id"])))
            if source_meta is not None:
                conn.execute("DELETE FROM account_groups WHERE group_name=?", (source_meta["group_name"],))
            conn.commit()
            return len(matching), destination
        except Exception:
            conn.rollback()
            raise


def _normalize_group_key(value: str) -> str:
    return str(value or "").strip().casefold()


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """从 SQLite 删除一个已注册账号记录，并清理关联的 Agent 凭证。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted_ids = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                deleted_ids.append(int(row.get("id") or 0))
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows)
        if deleted_ids:
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.executemany("DELETE FROM codex_agent_accounts WHERE account_id=?", [(x,) for x in deleted_ids])
                conn.commit()
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows)
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.executemany("DELETE FROM codex_agent_accounts WHERE account_id=?", [(x["id"],) for x in deleted])
                conn.commit()
    return deleted, skipped


def import_existing_accounts(records: list[dict]) -> tuple[int, list[dict]]:
    """直接导入已有账号，不要求邮箱池素材。

    records 元素：{email, password, totp_secret, access_token[, user_name]}。
    password 是 ChatGPT 账号密码，存入 extra_json.registration_password；
    返回 (新增数, 跳过详情)。邮箱按大小写不敏感去重。
    """
    with _LOCK:
        accounts = _load_accounts()
        inserted = 0
        skipped: list[dict] = []
        seen_emails: set[str] = set()

        for raw in records:
            email = str(raw.get("email") or "").strip()
            password = str(raw.get("password") or "").strip()
            totp_secret = str(raw.get("totp_secret") or raw.get("totp") or "").strip()
            access_token = str(raw.get("access_token") or raw.get("token") or "").strip()
            email_key = email.casefold()
            public_item = {"email": email} if email else {}

            if not email or not password or not totp_secret:
                skipped.append({**public_item, "reason": "邮箱、密码和 2FA 都不能为空"})
                continue
            if email_key in seen_emails:
                skipped.append({"email": email, "reason": "本次内容中邮箱重复"})
                continue
            seen_emails.add(email_key)
            if _find_by_email(accounts, email):
                skipped.append({"email": email, "reason": "账号已存在"})
                continue

            now = _now()
            account = {
                "id": _next_id(accounts),
                "email": email,
                "created_at": now,
                "updated_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_name": str(raw.get("user_name") or "").strip() or "Imported Account",
                "email_source": "imported",
                "extra_json": json.dumps({
                    "imported_existing_account": True,
                    "registration_password": password,
                }, ensure_ascii=False),
                "codex_status": "",
                "original_email_line": email,
            }
            account["copy_line"] = _account_line(account)
            accounts.append(account)
            inserted += 1

        if inserted:
            _save_accounts(accounts)
        return inserted, skipped


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api: records 元素 {email,code_url[,access_token,totp_secret]}
      - imap: records 元素 {email,imap_password,imap_server,imap_port,imap_ssl}

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api", "imap"):
        raise ValueError("source 必须显式传入 outlook / generic_api / imap")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        imap_rows = _load_imap_emails()
        inserted = skipped = 0

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source == "imap":
                password = str(raw.get("imap_password") or raw.get("password") or "").strip()
                server = str(raw.get("imap_server") or raw.get("server") or "").strip()
                try:
                    port = int(raw.get("imap_port") or raw.get("port") or 993)
                except (TypeError, ValueError):
                    port = 0
                if not password or not server or not (1 <= port <= 65535):
                    skipped += 1
                    continue
                ssl_raw = raw.get("imap_ssl", raw.get("use_ssl", True))
                use_ssl = ssl_raw if isinstance(ssl_raw, bool) else str(ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
                pool_row = _find_by_email(imap_rows, email)
                values = {
                    "imap_password": password, "imap_server": server, "imap_port": port,
                    "imap_username": str(raw.get("imap_username") or raw.get("username") or "").strip(),
                    "imap_ssl": bool(use_ssl),
                }
                if pool_row is None:
                    pool_row = {"id": _next_id(imap_rows), "email": email, **values,
                                "status": "used", "used_at": now,
                                "note": "导入为已注册账号，用于 Codex 授权", "imported_at": now}
                    imap_rows.append(pool_row)
                else:
                    pool_row.update(values)
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _imap_email_line(pool_row)
                original_line = _imap_email_line(pool_row)
            elif source == "generic_api":
                code_url = _normalize_generic_api_code_url(raw.get("code_url") or raw.get("url"))
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_imap_emails(imap_rows)
        _save_accounts(accounts)
        return inserted, skipped


def _claim_available_email(collection: str) -> dict | None:
    with _row_write_transaction() as conn:
        stored = conn.execute(
            "SELECT payload FROM email_pool WHERE source=? AND status='available' ORDER BY id LIMIT 1",
            (_EMAIL_SOURCES[collection],),
        ).fetchone()
        if stored is None:
            return None
        row = json.loads(stored["payload"])
        row.update(status="used", used_at=_now(), note=None, updated_at=_now())
        _write_collection_row(conn, collection, row)
        return row


def _release_email_row(collection: str, email: str, status: str, note: str | None,
                       *, only_unconsumed: bool = False) -> bool:
    with _row_write_transaction() as conn:
        if only_unconsumed and _select_collection_row(conn, "accounts", email=email or "") is not None:
            return False
        row = _select_collection_row(conn, collection, email=email or "")
        if row is None or (only_unconsumed and row.get("status") != "used"):
            return False
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        row["updated_at"] = _now()
        _write_collection_row(conn, collection, row)
        return True


def claim_next_outlook() -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 used。"""
    row = _claim_available_email("outlook")
    return _decorate_outlook(row) if row is not None else None


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    _release_email_row("outlook", email, status, note)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。"""
    return _release_email_row("outlook", email, "available", note, only_unconsumed=True)


def delete_email_pool(email: str, source: str = "all") -> bool:
    """按邮箱及来源从统一邮箱池删除记录。

    ``source=all`` 必须真正查询三个来源，而不能退化成 Outlook；旧版 WebUI
    在“全部邮箱池”中删除通用 API/域名邮箱时就是因此一直返回未找到。直接
    删除 SQLite 行也避免了逐条批量删除时反复加载并重写整个邮箱池。
    """
    target = str(email or "").strip()
    source = str(source or "all").strip().lower()
    if not target:
        return False
    if source not in {"all", "outlook", "generic_api", "imap", "cloudflare_domain"}:
        raise ValueError(f"非法邮箱来源: {source}")

    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            if source == "all":
                cur = conn.execute(
                    "DELETE FROM email_pool WHERE email = ? COLLATE NOCASE",
                    (target,),
                )
            else:
                db_source = (
                    _EMAIL_SOURCES["domain"]
                    if source == "cloudflare_domain"
                    else _EMAIL_SOURCES[source]
                )
                cur = conn.execute(
                    "DELETE FROM email_pool "
                    "WHERE email = ? COLLATE NOCASE AND source = ?",
                    (target, db_source),
                )
            conn.commit()
            return cur.rowcount > 0


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    return delete_email_pool(email, source="outlook")


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="outlook", status=status, limit=limit, offset=0
    )["items"]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = _pool_summary_sql("outlook")
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = _normalize_generic_api_code_url(raw.get("code_url") or raw.get("url"))
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email() -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。"""
    row = _claim_available_email("generic_api")
    return _decorate_generic_api_email(row) if row is not None else None


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    _release_email_row("generic_api", email, status, note)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。"""
    return _release_email_row("generic_api", email, "available", note, only_unconsumed=True)


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    return delete_email_pool(email, source="generic_api")


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="generic_api", status=status, limit=limit, offset=0
    )["items"]


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("generic_api")


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# Generic IMAP email pool
# ============================================================

def import_imap_emails(records: list[dict]) -> tuple[int, int]:
    """导入 IMAP 邮箱。用户名留空时客户端使用邮箱地址登录。"""
    with _LOCK:
        rows = _load_imap_emails()
        inserted = skipped = 0
        for raw in records:
            email = str(raw.get("email") or "").strip()
            password = str(raw.get("imap_password") or raw.get("password") or "").strip()
            server = str(raw.get("imap_server") or raw.get("server") or "").strip()
            try:
                port = int(raw.get("imap_port") or raw.get("port") or 993)
            except (TypeError, ValueError):
                port = 0
            if not email or not password or not server or not (1 <= port <= 65535) or _find_by_email(rows, email):
                skipped += 1
                continue
            ssl_raw = raw.get("imap_ssl", raw.get("use_ssl", True))
            use_ssl = ssl_raw if isinstance(ssl_raw, bool) else str(ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
            row = {
                "id": _next_id(rows), "email": email,
                "imap_password": password, "imap_server": server, "imap_port": port,
                "imap_username": str(raw.get("imap_username") or raw.get("username") or "").strip(),
                "imap_ssl": bool(use_ssl), "status": "available", "used_at": None,
                "note": None, "imported_at": _now(),
            }
            row["copy_line"] = _imap_email_line(row)
            rows.append(row)
            inserted += 1
        _save_imap_emails(rows)
        return inserted, skipped


def claim_next_imap_email() -> dict | None:
    row = _claim_available_email("imap")
    return _decorate_imap_email(row) if row is not None else None


def release_imap_email(email: str, status: str = "available", note: str | None = None) -> None:
    _release_email_row("imap", email, status, note)


def release_unconsumed_imap_email(email: str, note: str | None = None) -> bool:
    return _release_email_row("imap", email, "available", note, only_unconsumed=True)


def delete_imap_email(email: str) -> bool:
    return delete_email_pool(email, source="imap")


def list_imap_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(source="imap", status=status, limit=limit, offset=0)["items"]


def imap_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("imap")


def get_imap_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_imap_emails(), email)
        return _decorate_imap_email(row) if row else None


# ============================================================
# Codex 授权账号（SQLite codex_accounts 表）
# ============================================================

def _codex_filter_sql(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if archived in (True, "1", "true", "yes", "only"):
        where.append("archived=1")
    elif archived not in ("all", "include"):
        where.append("archived=0")
    if date_from:
        value = str(date_from)
        where.append("created_at >= ?")
        params.append(value + ("T00:00:00" if len(value) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?")
        params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    if q and str(q).strip():
        # 同 _collection_where：关键词已小写，LIKE 自身对 ASCII 大小写不敏感。
        where.append("payload LIKE ?")
        params.append("%" + str(q).strip().lower() + "%")
    return where, params


def _codex_content_to_record(content: dict) -> dict:
    """把 SQLite 中的 Codex payload 转成列表展示对象。"""
    fname = content.get("_filename", "")
    without_prefix = fname[5:-5] if fname.startswith("codex-") and fname.endswith(".json") else fname
    email = content.get("email") or without_prefix
    plan = ""
    if "-" in without_prefix and without_prefix.rsplit("-", 1)[-1].lower() in ("free", "plus", "team", "pro", "enterprise"):
        plan = without_prefix.rsplit("-", 1)[-1].lower()
        if not content.get("email"):
            email = without_prefix.rsplit("-", 1)[0]
    return {
        "filename": fname, "path": f"sqlite://codex_accounts/{fname}", "email": email, "plan": plan,
        "account_id": content.get("account_id", ""), "type": content.get("type", "codex"),
        "last_refresh": content.get("last_refresh", ""), "expired": content.get("expired", ""),
        "access_token_preview": (content.get("access_token", "") or "")[:32],
        "size": content.get("_size", 0), "mtime": content.get("_mtime", ""),
        "exported_at": content.get("_exported_at"), "exported_count": content.get("_exported_count", 0),
        "archived": bool(content.get("_archived")), "archived_at": content.get("_archived_at"),
    }


def list_codex_accounts_page(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """直接在 codex_accounts 表执行分页查询，不读取 codex_accounts/ 文件。"""
    _ensure_sqlite()
    limit = max(1, int(limit))
    offset = max(0, int(offset or 0))
    where, params = _codex_filter_sql(archived, date_from, date_to, q)
    clause = " AND ".join(where) if where else "1=1"
    with closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM codex_accounts WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(
            f"SELECT COALESCE(MAX(updated_at), '') FROM codex_accounts WHERE {clause}", params
        ).fetchone()[0] or "")
        rows = conn.execute(
            f"SELECT payload FROM codex_accounts WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [_codex_content_to_record(json.loads(row["payload"])) for row in rows]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "revision": f"{total}:{latest}",
    }


def list_codex_accounts(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> list[dict]:
    """从 SQLite 读取 Codex 凭证元数据，不扫描 codex_accounts/ 文件。"""
    _ensure_sqlite()
    where, params = _codex_filter_sql(archived, date_from, date_to, q)
    clause = " AND ".join(where) if where else "1=1"
    with closing(_sqlite_conn()) as conn:
        rows = [json.loads(row["payload"]) for row in conn.execute(
            f"SELECT payload FROM codex_accounts WHERE {clause} ORDER BY created_at DESC, id DESC", params
        )]
    return [_codex_content_to_record(content) for content in rows]


def upsert_codex_credential(content: dict, filename: str) -> str:
    """把 Codex 凭证写入 SQLite，返回逻辑文件名（不创建本地文件）。"""
    if not isinstance(content, dict) or not filename:
        raise ValueError("Codex 凭证或文件名无效")
    _ensure_sqlite()
    now = _now()
    with _LOCK, closing(_sqlite_conn()) as conn:
        old = conn.execute("SELECT payload, created_at FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        meta = dict(content)
        if old:
            previous = json.loads(old["payload"])
            for key in ("_exported_at", "_exported_count", "_archived", "_archived_at"):
                if key not in meta:
                    meta[key] = previous.get(key)
            created_at = old["created_at"] or now
            account_id = conn.execute("SELECT id FROM codex_accounts WHERE filename=?", (filename,)).fetchone()[0]
        else:
            account_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM codex_accounts").fetchone()[0])
            created_at = now
        meta.update({"_filename": filename, "_size": len(json.dumps(content, ensure_ascii=False).encode("utf-8")), "_mtime": now})
        conn.execute(
            "INSERT INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(filename) DO UPDATE SET email=excluded.email, archived=excluded.archived, updated_at=excluded.updated_at, payload=excluded.payload",
            (account_id, filename, str(content.get("email") or ""), int(bool(meta.get("_archived"))), created_at, now, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
    return filename

def archive_codex(filename: str, archived: bool = True) -> dict | None:
    """归档/取消归档一条 Codex 授权凭证。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return None
            content = json.loads(row["payload"])
            rec = {"exported_at": content.get("_exported_at"), "exported_count": content.get("_exported_count", 0)}
        rec["archived"] = bool(archived)
        rec["archived_at"] = _now() if archived else None
        content.update({"_archived": rec["archived"], "_archived_at": rec["archived_at"]})
        with closing(_sqlite_conn()) as conn:
            conn.execute("UPDATE codex_accounts SET archived=?, updated_at=?, payload=? WHERE filename=?", (int(archived), _now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()
        return rec


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        if not row:
            raise ValueError(f"文件不存在: {filename}")
        content = json.loads(row["payload"])
        content = {k: v for k, v in content.items() if not k.startswith("_")}
        return json.dumps(content, ensure_ascii=False, indent=2), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return {"exported_count": 0}
            content = json.loads(row["payload"])
        rec = {"exported_count": int(content.get("_exported_count", 0) or 0)}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        content.update({"_exported_count": rec["exported_count"], "_exported_at": rec["exported_at"]})
        with closing(_sqlite_conn()) as conn:
            conn.execute("UPDATE codex_accounts SET updated_at=?, payload=? WHERE filename=?", (_now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return
            content = json.loads(row["payload"])
            content.update({"_exported_count": 0, "_exported_at": None})
            conn.execute("UPDATE codex_accounts SET updated_at=?, payload=? WHERE filename=?", (_now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()


def delete_codex_credential(filename: str) -> bool:
    """从 SQLite 删除一个 Codex 凭证。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            cur = conn.execute("DELETE FROM codex_accounts WHERE filename=?", (filename,))
            conn.commit()
            return cur.rowcount > 0


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute(
                # INDEXED BY：优化器会选整表扫描，逐行解析 payload 只为取 _exported_count。
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN exported_count > 0 THEN 1 ELSE 0 END) AS exported "
                "FROM codex_accounts INDEXED BY idx_codex_accounts_exported WHERE archived=0"
            ).fetchone()
        total = int(row["total"] or 0)
        exported = int(row["exported"] or 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    row_id: int | None = None,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": _next_id(rows) if row_id is None else int(row_id),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "email": email,
        "status": "pending",
        "error_message": None,
        "progress": 0,
        "stage": "等待执行",
        "progress_message": "任务已创建，等待执行",
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "network_traffic": None,
        "created_at": _now(),
    }


def create_job(email_source: str) -> dict:
    """创建一个首次执行的 pending 注册任务。"""
    with _row_write_transaction() as conn:
        row = _new_job_row([], row_id=_next_collection_id(conn, "jobs"), email_source=email_source)
        _write_collection_row(conn, "jobs", row, insert=True)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _row_write_transaction() as conn:
        source = _select_collection_row(conn, "jobs", row_id=source_job_id)
        if source is None:
            raise LookupError("任务不存在")
        if source.get("status") not in ("failed", "stopped", "cancelled"):
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        root_expr = "COALESCE(CAST(json_extract(payload, '$.root_job_id') AS INTEGER), 0)"
        active_row = conn.execute(
            f"SELECT payload FROM registration_jobs WHERE {root_expr}=? AND id!=? "
            "AND status IN ('pending','running','paused','stopping') ORDER BY id LIMIT 1",
            (root_id, int(source_job_id)),
        ).fetchone()
        if active_row is not None:
            active = json.loads(active_row["payload"])
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return active, False

        attempt = conn.execute(
            "SELECT COALESCE(MAX(COALESCE(CAST(json_extract(payload, '$.retry_attempt') AS INTEGER), 0)), 0) "
            f"FROM registration_jobs WHERE id=? OR {root_expr}=?", (root_id, root_id),
        ).fetchone()[0]
        row = _new_job_row(
            [],
            row_id=_next_collection_id(conn, "jobs"),
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=int(attempt) + 1,
            retry_action=("codex" if job_type == "codex_retry" else "registration"),
            email=email,
            account_id=account_id,
        )
        _write_collection_row(conn, "jobs", row, insert=True)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
    network_traffic: dict | None = None,
    progress: int | None = None,
    stage: str | None = None,
    progress_message: str | None = None,
) -> None:
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "jobs", row_id=job_id)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        if network_traffic is not None:
            row["network_traffic"] = dict(network_traffic)
        if progress is not None:
            row["progress"] = max(0, min(100, int(progress)))
        if stage is not None:
            row["stage"] = str(stage)
        if progress_message is not None:
            row["progress_message"] = str(progress_message)
        row["updated_at"] = _now()
        _write_collection_row(conn, "jobs", row)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        return [dict(r) for r in _query_collection("jobs", limit=limit)]


def list_active_jobs(limit: int = 1000) -> list[dict]:
    """按最新任务优先返回仍需用户关注的任务。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            rows = conn.execute(
                "SELECT payload FROM registration_jobs "
                "WHERE status IN ('pending', 'running', 'paused', 'stopping') "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(5000, int(limit))),),
            )
            return [json.loads(row["payload"]) for row in rows]


def list_history_jobs_page(limit: int = 20, offset: int = 0) -> dict:
    """按最新任务优先返回已结束任务，供任务中心历史区分页使用。"""
    with _LOCK:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset or 0))
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            where = "status NOT IN ('pending', 'running', 'paused', 'stopping')"
            total = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM registration_jobs WHERE {where}"
            ).fetchone()["n"])
            rows = conn.execute(
                f"SELECT payload FROM registration_jobs WHERE {where} "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return {
                "items": [json.loads(row["payload"]) for row in rows],
                "total": total,
            }


def list_jobs_page(limit: int = 50, offset: int = 0) -> dict:
    """直接使用 registration_jobs 的 SQL LIMIT/OFFSET 返回任务页。"""
    with _LOCK:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        rows, total, latest = _query_collection_page(
            "jobs", limit=limit, offset=offset
        )
        return {
            "items": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
            "revision": f"{total}:{latest}",
        }


def job_status_counts() -> dict:
    """在 SQLite 中聚合任务状态，避免为统计目的加载全部任务 payload。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        counts = {
            str(row["status"] or "unknown"): int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM registration_jobs GROUP BY status"
            )
        }
    counts["active"] = sum(int(counts.get(status, 0) or 0) for status in ("pending", "running", "paused", "stopping"))
    return counts


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute(
                "SELECT payload FROM registration_jobs WHERE id=? LIMIT 1", (int(job_id),)
            ).fetchone()
        return json.loads(row["payload"]) if row else None


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        source = get_job(job_id)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        with closing(_sqlite_conn()) as conn:
            row = conn.execute(
                """SELECT payload FROM registration_jobs
                   WHERE status='success'
                     AND COALESCE(CAST(json_extract(payload, '$.root_job_id') AS INTEGER), 0)=?
                     AND id != ? ORDER BY id DESC LIMIT 1""",
                (root_id, int(job_id)),
            ).fetchone()
        return json.loads(row["payload"]) if row else None


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "paused", "stopping"):
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                if item["status"] != "available":
                    release_outlook(item["email"], status=item["status"], note=item["note"])
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、旧邮箱 TXT/JSON 迁移到当前 SQLite 存储。
    多次调用是幂等的，不会生成或更新旧 JSON/TXT 文件。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _DATA_DIR / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_DATA_DIR / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _DATA_DIR / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """返回 SQLite 主数据库路径（保留函数名兼容旧调用方）。"""
    _ensure_sqlite()
    return _active_sqlite_path()


def storage_paths() -> dict:
    return {
        "sqlite": str(_SQLITE_PATH),
        "logs_dir": str(_LOG_DIR),
    }


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _DATA_DIR / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    return _load_collection("domain")


def _save_domain_pool(rows: list[dict]) -> None:
    _save_collection("domain", rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _row_write_transaction() as conn:
        row = _select_collection_row(conn, "domain", email=email or "")
        if row is not None:
            return row
        row = {
            "id": _next_collection_id(conn, "domain"),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        _write_collection_row(conn, "domain", row, insert=True)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    _release_email_row("domain", email, status, note)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。"""
    return _release_email_row("domain", email, "available", note, only_unconsumed=True)


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="cloudflare_domain", status=status, limit=limit, offset=0
    )["items"]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("domain")


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    return delete_email_pool(email, source="cloudflare_domain")
