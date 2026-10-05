"""後台共用函式：編號、優惠版本建立、備份、檔案上傳。"""
import glob
import json
import os
import re
import secrets
import sqlite3
from datetime import date, timedelta

from werkzeug.utils import secure_filename

import db
from db import q, ex, now, today, iso


def next_code(table, prefix, width=4):
    row = q(f"SELECT code FROM {table} WHERE code LIKE ? ORDER BY length(code) DESC, code DESC LIMIT 1", (prefix + "%",), one=True)
    n = int(re.sub(r"\D", "", row["code"]) or 0) + 1 if row else 1
    return f"{prefix}{n:0{width}d}"


def create_offer_version(offer_id, vendor_id, f, reg_ids, store_ids, actor, renewal_from=None):
    """建立新優惠（V1）或既有優惠的新版本。歷史版本永不被修改。"""
    t = now()
    if offer_id is None:
        cur = ex("INSERT INTO offers(code,vendor_id,created_by,created_at) VALUES(?,?,?,?)",
                 (next_code("offers", "O"), vendor_id, actor["id"], t))
        offer_id = cur.lastrowid
        vno = 1
    else:
        vno = q("SELECT MAX(version_no) m FROM offer_versions WHERE offer_id=?", (offer_id,), one=True)["m"] + 1
    cur = ex("INSERT INTO offer_versions(offer_id,version_no,name,category_id,content,usage,notes,start_date,end_date,"
             "is_pinned,is_recommended,mgmt_status,created_by,created_at,updated_by,updated_at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (offer_id, vno, f["name"], f["category_id"], f["content"], f.get("usage", ""), f.get("notes", ""),
              f["start_date"], f["end_date"], f.get("is_pinned", 0), f.get("is_recommended", 0),
              f.get("mgmt_status", "draft"), actor["id"], t, actor["id"], t))
    vid = cur.lastrowid
    for r in reg_ids:
        ex("INSERT INTO version_regions VALUES(?,?)", (vid, r))
    for s in store_ids:
        ex("INSERT INTO version_stores VALUES(?,?)", (vid, s))
    log_review(vid, "建立" if vno == 1 else f"建立 V{vno}", actor)
    return vid


def log_review(vid, action, actor, note=""):
    ex("INSERT INTO review_logs(version_id,action,actor_id,actor_name,note,at) VALUES(?,?,?,?,?,?)",
       (vid, action, actor["id"] if actor else None, actor["name"] if actor else "系統", note, now()))


def one_year_after(d):
    try:
        e = d.replace(year=d.year + 1)
    except ValueError:  # 2/29
        e = d.replace(year=d.year + 1, day=28)
    return e - timedelta(days=1)


# ---------------------------------------------------------------- 備份
# SQLite：複製整個資料庫檔。PostgreSQL（Supabase）：匯出所有資料表為 JSON（另有 Supabase 平台層級備份）。
DUMP_TABLES = ["roles", "role_permissions", "categories", "regions", "employees", "vendors", "stores", "offers",
               "offer_versions", "version_regions", "version_stores", "review_logs", "announcements", "audit_logs",
               "import_batches", "usage_events", "settings"]
SEQ_TABLES = ["roles", "categories", "regions", "employees", "vendors", "stores", "offers", "offer_versions",
              "review_logs", "announcements", "audit_logs", "import_batches", "usage_events"]
BACKUP_NAME = r"welfare-[0-9\-]+-[a-z\-]+\.(db|json)"


def make_backup(reason="manual"):
    os.makedirs(db.BACKUP_DIR, exist_ok=True)
    stamp = db.datetime.now(db.TZ).strftime("%Y%m%d-%H%M%S")
    name = f"welfare-{stamp}-{reason}.{'json' if db.PG else 'db'}"
    path = os.path.join(db.BACKUP_DIR, name)
    if db.PG:
        conn = db.connect()
        try:
            data = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()] for t in DUMP_TABLES}
        finally:
            conn.close()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(path, 0o600)
    else:
        src = db.connect()
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    if reason == "auto":
        prune_backups()
    return name


def list_backups():
    out = []
    for p in sorted(glob.glob(os.path.join(db.BACKUP_DIR, "welfare-*")), reverse=True):
        if re.fullmatch(BACKUP_NAME, os.path.basename(p)):
            out.append({"name": os.path.basename(p), "size": os.path.getsize(p)})
    return out


def prune_backups():
    keep = db.setting_int("backup_keep")
    autos = [b["name"] for b in list_backups() if "-auto." in b["name"]]
    for n in autos[keep:]:
        os.remove(os.path.join(db.BACKUP_DIR, n))


def restore_backup(name):
    if not re.fullmatch(BACKUP_NAME, name):
        raise ValueError("bad name")
    path = os.path.join(db.BACKUP_DIR, name)
    if not os.path.exists(path):
        raise ValueError("missing")
    make_backup("pre-restore")
    if db.PG:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        conn = db.connect()
        try:
            for t in reversed(DUMP_TABLES):
                conn.execute(f"DELETE FROM {t}")
            for t in DUMP_TABLES:
                for r in data.get(t, []):
                    cols = list(r)
                    conn.execute(f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?'*len(cols))})" +
                                 (" ON CONFLICT DO NOTHING" if t in SEQ_TABLES else ""), [r[c] for c in cols])
            for t in SEQ_TABLES:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),0)+1, false)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return
    src = sqlite3.connect(path)
    live = db.connect()
    try:
        src.backup(live)
    finally:
        src.close()
        live.close()


# ---------------------------------------------------------------- 上傳
ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")


def save_image(fs):
    """儲存上傳圖片（限制副檔名與檔頭，不接受 SVG），回傳檔名；無檔案回傳 None；格式不符丟 ValueError。"""
    if not fs or not fs.filename:
        return None
    ext = os.path.splitext(secure_filename(fs.filename) or "x")[1].lower()
    head = fs.stream.read(12)
    fs.stream.seek(0)
    if ext not in ALLOWED_IMG or not head.startswith(MAGIC):
        raise ValueError("圖片格式不支援，請上傳 PNG、JPG、GIF 或 WebP 檔案。")
    name = secrets.token_hex(12) + ext
    fs.save(os.path.join(db.UPLOAD_DIR, name))
    return name
