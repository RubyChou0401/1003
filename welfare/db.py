"""資料庫、時間與稽核工具。所有日期時間一律使用 Asia/Taipei（UTC+8）。"""
import json
import os
import sqlite3
from datetime import datetime, date, timedelta, timezone

from flask import g, request, session
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WELFARE_DB", os.path.join(BASE_DIR, "data", "welfare.db"))
BACKUP_DIR = os.environ.get("WELFARE_BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TZ = timezone(timedelta(hours=8))  # Asia/Taipei 無日光節約時間


def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now(TZ).date()


def iso(d):
    return d.strftime("%Y-%m-%d")


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    return get_db().execute(sql, args)


PERMISSIONS = [
    ("staff.manage", "員工管理"),
    ("vendor.manage", "廠商／門市管理"),
    ("basic.manage", "基礎資料（分類、地區）"),
    ("offer.edit", "優惠建立與編輯、續約"),
    ("offer.review", "優惠審核與發布"),
    ("announce.manage", "公告管理"),
    ("import.run", "Excel 匯入"),
    ("stats.view", "Dashboard 與統計分析"),
    ("audit.view", "查看操作紀錄"),
    ("admin.manage", "管理者、角色、權限、系統設定、備份"),
]
HR_PERMS = [p for p, _ in PERMISSIONS if p != "admin.manage"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS roles(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, is_admin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS role_permissions(
  role_id INTEGER NOT NULL REFERENCES roles(id), perm TEXT NOT NULL, PRIMARY KEY(role_id, perm));
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY, emp_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  dept TEXT DEFAULT '', title TEXT DEFAULT '', region_id INTEGER REFERENCES regions(id),
  hire_date TEXT, status TEXT NOT NULL DEFAULT '在職',      -- 在職/留停/離職
  active INTEGER NOT NULL DEFAULT 1, role_id INTEGER NOT NULL REFERENCES roles(id),
  password_hash TEXT NOT NULL, must_change_pw INTEGER NOT NULL DEFAULT 1,
  failed_count INTEGER NOT NULL DEFAULT 0, locked_until TEXT, last_login TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS regions(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  is_all INTEGER DEFAULT 0,   -- 1 = 全區（任何地區查詢都會包含）
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vendors(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, brand TEXT DEFAULT '',
  logo TEXT DEFAULT '', contact TEXT DEFAULT '', phone TEXT DEFAULT '', email TEXT DEFAULT '',
  address TEXT DEFAULT '', region_id INTEGER REFERENCES regions(id), website TEXT DEFAULT '',
  map_url TEXT DEFAULT '', note TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stores(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, vendor_id INTEGER NOT NULL REFERENCES vendors(id),
  name TEXT NOT NULL, region_id INTEGER REFERENCES regions(id), address TEXT DEFAULT '',
  phone TEXT DEFAULT '', map_url TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, vendor_id INTEGER NOT NULL REFERENCES vendors(id),
  created_by INTEGER REFERENCES employees(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer_versions(
  id INTEGER PRIMARY KEY, offer_id INTEGER NOT NULL REFERENCES offers(id), version_no INTEGER NOT NULL,
  name TEXT NOT NULL, category_id INTEGER NOT NULL REFERENCES categories(id),
  content TEXT NOT NULL, usage TEXT DEFAULT '', notes TEXT DEFAULT '',
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  is_pinned INTEGER DEFAULT 0, is_recommended INTEGER DEFAULT 0,
  mgmt_status TEXT NOT NULL DEFAULT 'draft',  -- draft/pending/published/disabled/archived
  renewal_status TEXT NOT NULL DEFAULT '待聯繫', -- 待聯繫/洽談中/待確認/已續約/不續約
  renewed_to INTEGER REFERENCES offer_versions(id),
  expired_processed_at TEXT,
  created_by INTEGER, created_at TEXT NOT NULL, updated_by INTEGER, updated_at TEXT NOT NULL,
  reviewed_by INTEGER, reviewed_at TEXT, review_note TEXT DEFAULT '',
  published_by INTEGER, published_at TEXT, views INTEGER DEFAULT 0,
  UNIQUE(offer_id, version_no), CHECK(start_date <= end_date));
CREATE TABLE IF NOT EXISTS version_regions(
  version_id INTEGER NOT NULL REFERENCES offer_versions(id), region_id INTEGER NOT NULL REFERENCES regions(id),
  PRIMARY KEY(version_id, region_id));
CREATE TABLE IF NOT EXISTS version_stores(
  version_id INTEGER NOT NULL REFERENCES offer_versions(id), store_id INTEGER NOT NULL REFERENCES stores(id),
  PRIMARY KEY(version_id, store_id));
CREATE TABLE IF NOT EXISTS review_logs(
  id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES offer_versions(id),
  action TEXT NOT NULL, actor_id INTEGER, actor_name TEXT, note TEXT DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS announcements(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, image TEXT DEFAULT '',
  kind TEXT NOT NULL DEFAULT '最新優惠', publish_date TEXT NOT NULL, end_date TEXT,
  is_pinned INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft', -- draft/published/archived
  created_by INTEGER, created_at TEXT NOT NULL, updated_by INTEGER, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor_id INTEGER, actor_name TEXT,
  action TEXT NOT NULL, target_type TEXT, target_id TEXT, target_label TEXT,
  result TEXT NOT NULL DEFAULT '成功', diff TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_logs(at);
CREATE TABLE IF NOT EXISTS import_batches(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, filename TEXT, actor_id INTEGER, actor_name TEXT,
  created_at TEXT NOT NULL, total INTEGER DEFAULT 0, n_add INTEGER DEFAULT 0, n_update INTEGER DEFAULT 0,
  n_error INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'previewed', -- previewed/confirmed/cancelled/failed
  payload TEXT, errors TEXT, confirmed_at TEXT, backup_file TEXT);
CREATE TABLE IF NOT EXISTS usage_events(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,  -- login/view/search
  emp_id INTEGER,            -- 僅 login 會記錄，用於計算登入人數；view/search 不記錄個人
  version_id INTEGER, vendor_id INTEGER, category_id INTEGER, region_id INTEGER, keyword TEXT);
CREATE INDEX IF NOT EXISTS ix_usage ON usage_events(kind, at);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

DEFAULT_SETTINGS = {
    "allow_leave_login": "1",     # 留停員工是否可登入
    "session_idle_minutes": "30",
    "max_failed_logins": "5",
    "lock_minutes": "15",
    "self_review_allowed": "1",   # 建立人是否可自行審核
    "expiring_days": "30",
    "backup_keep": "14",
    "site_name": "員工職福好康站",
}

DEFAULT_CATEGORIES = ["食", "衣", "住", "行", "育", "樂", "健康", "其他"]
DEFAULT_REGIONS = ["台北地區", "桃竹苗", "台中", "高雄", "金門", "澎湖", "全區", "線上優惠"]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    t = now()
    if not conn.execute("SELECT 1 FROM roles").fetchone():
        conn.execute("INSERT INTO roles(code,name,is_admin) VALUES('employee','一般員工',0)")
        conn.execute("INSERT INTO roles(code,name,is_admin) VALUES('hr','HR／職福管理者',1)")
        conn.execute("INSERT INTO roles(code,name,is_admin) VALUES('sysadmin','系統管理員',1)")
        for p, _ in PERMISSIONS:
            conn.execute("INSERT INTO role_permissions VALUES(3,?)", (p,))
        for p in HR_PERMS:
            conn.execute("INSERT INTO role_permissions VALUES(2,?)", (p,))
    for i, n in enumerate(DEFAULT_CATEGORIES, 1):
        conn.execute("INSERT OR IGNORE INTO categories(name,sort,created_at,updated_at) VALUES(?,?,?,?)", (n, i, t, t))
    for i, n in enumerate(DEFAULT_REGIONS, 1):
        conn.execute("INSERT OR IGNORE INTO regions(name,sort,is_all,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (n, i, 1 if n == "全區" else 0, t, t))
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    if not conn.execute("SELECT 1 FROM employees WHERE role_id=3").fetchone():
        pw = os.environ.get("WELFARE_ADMIN_PASSWORD", "Admin1234")
        conn.execute(
            "INSERT INTO employees(emp_no,name,dept,role_id,password_hash,must_change_pw,created_at,updated_at)"
            " VALUES('admin','系統管理員','資訊部',3,?,1,?,?)", (generate_password_hash(pw), t, t))
    conn.commit()
    conn.close()


def setting(key):
    row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def setting_int(key):
    try:
        return int(setting(key))
    except ValueError:
        return int(DEFAULT_SETTINGS[key])


def diff(old, new, fields=None):
    """回傳 {欄位: [舊, 新]}，只列出有變動的欄位。"""
    out = {}
    for k in (fields or new.keys()):
        a, b = (old or {}).get(k), new.get(k)
        if str(a if a is not None else "") != str(b if b is not None else ""):
            out[k] = [a, b]
    return out


def audit(action, target_type=None, target_id=None, label=None, result="成功", changes=None,
          actor=None, actor_name=None):
    """寫入操作紀錄（與呼叫端同一交易，commit 由呼叫端或 request 結束時處理）。"""
    user = actor if actor is not None else getattr(g, "user", None)
    aid = user["id"] if user else None
    aname = actor_name or (f'{user["name"]}（{user["emp_no"]}）' if user else "系統")
    ip = request.remote_addr if request else None
    try:
        ex("INSERT INTO audit_logs(at,actor_id,actor_name,action,target_type,target_id,target_label,result,diff,ip)"
           " VALUES(?,?,?,?,?,?,?,?,?,?)",
           (now(), aid, aname, action, target_type, None if target_id is None else str(target_id), label, result,
            json.dumps(changes, ensure_ascii=False) if changes else None, ip))
    except RuntimeError:
        pass
