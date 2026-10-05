"""後台：Dashboard、員工、匯入、廠商／門市、基礎資料、系統管理。"""
import json
import re
from datetime import timedelta

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_file, send_from_directory,
                   url_for)
from werkzeug.security import generate_password_hash

import db
import excel
import lifecycle
from db import q, ex, now, today, iso, audit, diff, setting, PERMISSIONS
from security import perm_required, password_problem
from views_admin_helpers import next_code, save_image, make_backup, list_backups, restore_backup

bp = Blueprint("admin", __name__)


def page_args(per=20):
    p = max(1, int(request.args.get("page", 1)) if request.args.get("page", "1").isdigit() else 1)
    return p, per, (p - 1) * per


def paginate(sql, args, per=20):
    p, per, off = page_args(per)
    total = q(f"SELECT COUNT(*) c FROM ({sql})", args, one=True)["c"]
    rows = q(sql + f" LIMIT {per} OFFSET {off}", args)
    return rows, {"page": p, "pages": max(1, -(-total // per)), "total": total}


def url_ok(s):
    return not s or s.lower().startswith(("http://", "https://"))


# ---------------------------------------------------------------- Dashboard
@bp.route("/")
@perm_required("stats.view", "staff.manage", "vendor.manage", "offer.edit", "offer.review", "announce.manage",
               "audit.view", "admin.manage", "import.run", "basic.manage")
def dashboard():
    if not (g.perms & {"stats.view"}):
        return redirect(url_for("admin.offers"))
    t = today()
    lifecycle.expire_job()
    one = lambda sql, a=(): q(sql, a, one=True)["c"]
    month = iso(t.replace(day=1))
    emp = {"total": one("SELECT COUNT(*) c FROM employees"),
           "active": one("SELECT COUNT(*) c FROM employees WHERE active=1 AND status!='離職'"),
           "inactive": one("SELECT COUNT(*) c FROM employees WHERE active=0 OR status='離職'")}
    ven = {"total": one("SELECT COUNT(*) c FROM vendors"),
           "active": one("SELECT COUNT(*) c FROM vendors WHERE status='active'"),
           "inactive": one("SELECT COUNT(*) c FROM vendors WHERE status!='active'")}
    d30, d7 = iso(t + timedelta(days=30)), iso(t + timedelta(days=7))
    pub = "mgmt_status='published'"
    off = {"valid": one(f"SELECT COUNT(*) c FROM offer_versions WHERE {pub} AND start_date<=? AND end_date>=?", (iso(t), iso(t))),
           "expiring": one(f"SELECT COUNT(*) c FROM offer_versions WHERE {pub} AND end_date BETWEEN ? AND ?", (iso(t), d30)),
           "expiring7": one(f"SELECT COUNT(*) c FROM offer_versions WHERE {pub} AND end_date BETWEEN ? AND ?", (iso(t), d7)),
           "expired": one(f"SELECT COUNT(*) c FROM offer_versions v WHERE {pub} AND end_date<? AND version_no=(SELECT MAX(version_no) FROM offer_versions x WHERE x.offer_id=v.offer_id)", (iso(t),)),
           "pending": one("SELECT COUNT(*) c FROM offer_versions WHERE mgmt_status='pending'")}
    # 續約追蹤集合：已發布且（30 天內到期或已過期，近一年內）
    ren = {s: one(f"SELECT COUNT(*) c FROM offer_versions WHERE {pub} AND end_date<=? AND end_date>=? AND renewal_status=?",
                  (d30, iso(t - timedelta(days=365)), s)) for s in lifecycle.RENEWAL_STATES}
    use = {"logins": one("SELECT COUNT(DISTINCT emp_id) c FROM usage_events WHERE kind='login' AND at>=?", (month,)),
           "views": one("SELECT COUNT(*) c FROM usage_events WHERE kind='view' AND at>=?", (month,))}
    top = q("SELECT v.id, v.name, ven.name vn, COUNT(*) c FROM usage_events u JOIN offer_versions v ON v.id=u.version_id "
            "JOIN offers o ON o.id=v.offer_id JOIN vendors ven ON ven.id=o.vendor_id WHERE u.kind='view' AND u.at>=? "
            "GROUP BY v.id ORDER BY c DESC LIMIT 5", (month,))
    topcat = q("SELECT c.name, COUNT(*) n FROM usage_events u JOIN categories c ON c.id=u.category_id WHERE u.kind IN ('view','search') "
               "AND u.at>=? GROUP BY c.id ORDER BY n DESC LIMIT 5", (month,))
    topreg = q("SELECT r.name, COUNT(*) n FROM usage_events u JOIN regions r ON r.id=u.region_id WHERE u.kind IN ('view','search') "
               "AND u.at>=? GROUP BY r.id ORDER BY n DESC LIMIT 5", (month,))
    return render_template("admin/dashboard.html", emp=emp, ven=ven, off=off, ren=ren, use=use, top=top, topcat=topcat,
                           topreg=topreg, reminders=lifecycle.reminders())


# ---------------------------------------------------------------- 員工
def region_list():
    return q("SELECT * FROM regions WHERE active=1 ORDER BY sort,id")


@bp.route("/staff")
@perm_required("staff.manage")
def staff():
    kw = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    where, a = ["1=1"], []
    if kw:
        where.append("(e.emp_no LIKE ? OR e.name LIKE ? OR e.dept LIKE ?)")
        a += [f"%{kw}%"] * 3
    if status == "inactive":
        where.append("(e.active=0 OR e.status='離職')")
    elif status in ("在職", "留停", "離職"):
        where.append("e.status=?")
        a.append(status)
    elif status == "locked":
        where.append("e.locked_until>?")
        a.append(now())
    rows, pg = paginate("SELECT e.*, r.name region_name, ro.name role_name FROM employees e LEFT JOIN regions r ON r.id=e.region_id "
                        "JOIN roles ro ON ro.id=e.role_id WHERE " + " AND ".join(where) + " ORDER BY e.emp_no", a)
    return render_template("admin/staff_list.html", rows=rows, pg=pg, kw=kw, status=status)


@bp.route("/staff/new", methods=["GET", "POST"])
@bp.route("/staff/<int:eid>", methods=["GET", "POST"])
@perm_required("staff.manage")
def staff_form(eid=None):
    e = q("SELECT * FROM employees WHERE id=?", (eid,), one=True) if eid else None
    if eid and not e:
        abort(404)
    errs = []
    if request.method == "POST":
        f = request.form
        d = dict(name=f.get("name", "").strip(), dept=f.get("dept", "").strip(), title=f.get("title", "").strip(),
                 region_id=int(f["region_id"]) if f.get("region_id", "").isdigit() else None,
                 hire_date=f.get("hire_date") or None, status=f.get("status", "在職"))
        d["active"] = 0 if d["status"] == "離職" else (1 if f.get("active") else 0)
        if not d["name"]:
            errs.append("請填寫姓名。")
        if d["status"] not in ("在職", "留停", "離職"):
            errs.append("員工狀態不正確。")
        if e and e["id"] == g.user["id"] and not d["active"]:
            errs.append("不能停用自己的帳號。")
        if not e:
            emp_no = f.get("emp_no", "").strip()
            pw = f.get("password", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", emp_no):
                errs.append("員工編號格式不正確（英數字、底線、連字號，20 字內）。")
            elif q("SELECT 1 FROM employees WHERE emp_no=?", (emp_no,), one=True):
                errs.append("員工編號已存在。")
            if password_problem(pw):
                errs.append("初始密碼：" + password_problem(pw))
        if not errs:
            t = now()
            if e:
                ch = diff(dict(e), d)
                ex("UPDATE employees SET name=?,dept=?,title=?,region_id=?,hire_date=?,status=?,active=?,updated_at=? WHERE id=?",
                   (*d.values(), t, eid))
                audit("修改員工", "employee", eid, f'{d["name"]}（{e["emp_no"]}）', changes=ch)
            else:
                cur = ex("INSERT INTO employees(emp_no,name,dept,title,region_id,hire_date,status,active,role_id,password_hash,"
                         "must_change_pw,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,1,?,?)",
                         (emp_no, d["name"], d["dept"], d["title"], d["region_id"], d["hire_date"], d["status"], d["active"],
                          generate_password_hash(pw), t, t))
                eid = cur.lastrowid
                audit("新增員工", "employee", eid, f'{d["name"]}（{emp_no}）')
            flash("員工資料已儲存。")
            return redirect(url_for("admin.staff_form", eid=eid))
        e = {**(dict(e) if e else {}), **d, "emp_no": e["emp_no"] if e else request.form.get("emp_no", "")}
    return render_template("admin/staff_form.html", e=e, errs=errs, regions=region_list())


@bp.route("/staff/<int:eid>/reset", methods=["POST"])
@perm_required("staff.manage")
def staff_reset(eid):
    e = q("SELECT * FROM employees WHERE id=?", (eid,), one=True) or abort(404)
    pw = request.form.get("password", "")
    if password_problem(pw):
        flash("重設失敗：" + password_problem(pw))
    else:
        ex("UPDATE employees SET password_hash=?, must_change_pw=1, failed_count=0, locked_until=NULL, updated_at=? WHERE id=?",
           (generate_password_hash(pw), now(), eid))
        audit("重設員工密碼", "employee", eid, f'{e["name"]}（{e["emp_no"]}）')
        flash("密碼已重設，員工下次登入須自行更改。")
    return redirect(url_for("admin.staff_form", eid=eid))


@bp.route("/staff/<int:eid>/unlock", methods=["POST"])
@perm_required("staff.manage")
def staff_unlock(eid):
    e = q("SELECT * FROM employees WHERE id=?", (eid,), one=True) or abort(404)
    ex("UPDATE employees SET failed_count=0, locked_until=NULL WHERE id=?", (eid,))
    audit("解除帳號鎖定", "employee", eid, f'{e["name"]}（{e["emp_no"]}）')
    flash("已解除鎖定。")
    return redirect(url_for("admin.staff_form", eid=eid))


# ---------------------------------------------------------------- Excel 匯入
KINDS = {
    "employees": dict(label="員工資料", cols=excel.EMP_COLS, req=excel.EMP_REQUIRED, perm="staff.manage",
                      validate=excel.validate_employees,
                      sample=["E1001", "王小明", "營運部", "台北地區", "專員", "2024/03/01", "在職", "Welcome123"]),
    "offers": dict(label="優惠資料", cols=excel.OFFER_COLS, req=excel.OFFER_REQUIRED, perm="offer.edit",
                   validate=excel.validate_offers,
                   sample=["V0001", "食", "台北地區、桃竹苗", "平日消費9折", "平日內用享9折", "出示員工證",
                           "不得與其他優惠併用", "2026/01/01", "2026/12/31", "信義店", "https://example.com",
                           "https://maps.google.com", "草稿"]),
}


def kind_or_404(kind):
    k = KINDS.get(kind) or abort(404)
    if "import.run" not in g.perms or k["perm"] not in g.perms:
        abort(403)
    return k


@bp.route("/import/<kind>", methods=["GET", "POST"])
@perm_required("import.run")
def import_upload(kind):
    k = kind_or_404(kind)
    err = None
    if request.method == "POST":
        fs = request.files.get("file")
        if not fs or not fs.filename.lower().endswith(".xlsx"):
            err = "請選擇 .xlsx 格式的 Excel 檔案。"
        else:
            try:
                rows = excel.read_sheet(fs.stream, k["cols"], k["req"])
                items = k["validate"](rows)
            except excel.ImportFormatError as ex_:
                err = str(ex_)
                audit("Excel匯入", "import", None, f'{k["label"]}：{fs.filename}', f"失敗：{err}")
            if not err:
                errors = [i for i in items if i["action"] == "error"]
                cnt = lambda a: sum(1 for i in items if i["action"] == a)
                # 密碼不長期留存：確認或取消後 payload 會被清除
                cur = ex("INSERT INTO import_batches(kind,filename,actor_id,actor_name,created_at,total,n_add,n_update,n_error,payload,errors)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (kind, fs.filename[:120], g.user["id"], g.user["name"], now(), len(items), cnt("add"),
                          cnt("update"), len(errors), json.dumps(items, ensure_ascii=False),
                          json.dumps(errors, ensure_ascii=False)))
                audit("Excel匯入預覽", "import", cur.lastrowid, f'{k["label"]}：{fs.filename}')
                return redirect(url_for("admin.import_detail", bid=cur.lastrowid))
    return render_template("admin/import_upload.html", kind=kind, k=k, err=err)


@bp.route("/import/<kind>/template")
@perm_required("import.run")
def import_template(kind):
    k = kind_or_404(kind)
    import io
    return send_file(io.BytesIO(excel.template_bytes(k["cols"], k["sample"])), as_attachment=True,
                     download_name=f"{k['label']}匯入範本.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/imports")
@perm_required("import.run")
def import_logs():
    rows, pg = paginate("SELECT id,kind,filename,actor_name,created_at,total,n_add,n_update,n_error,status FROM import_batches "
                        "ORDER BY id DESC", [])
    return render_template("admin/import_logs.html", rows=rows, pg=pg, KINDS=KINDS)


def batch_or_404(bid):
    b = q("SELECT * FROM import_batches WHERE id=?", (bid,), one=True) or abort(404)
    if b["kind"] not in KINDS or KINDS[b["kind"]]["perm"] not in g.perms:
        abort(403)
    return b


@bp.route("/imports/<int:bid>")
@perm_required("import.run")
def import_detail(bid):
    b = batch_or_404(bid)
    errors = json.loads(b["errors"] or "[]")
    return render_template("admin/import_detail.html", b=b, errors=errors, k=KINDS[b["kind"]],
                           show_all=request.args.get("errors") == "1")


@bp.route("/imports/<int:bid>/cancel", methods=["POST"])
@perm_required("import.run")
def import_cancel(bid):
    b = batch_or_404(bid)
    if b["status"] == "previewed":
        ex("UPDATE import_batches SET status='cancelled', payload=NULL WHERE id=?", (bid,))
        audit("Excel匯入取消", "import", bid, b["filename"])
        flash("已取消匯入，沒有任何資料被寫入。")
    return redirect(url_for("admin.import_logs"))


@bp.route("/imports/<int:bid>/confirm", methods=["POST"])
@perm_required("import.run")
def import_confirm(bid):
    b = batch_or_404(bid)
    if b["status"] != "previewed" or not b["payload"]:
        flash("此匯入已處理過，無法重複匯入。")
        return redirect(url_for("admin.import_detail", bid=bid))
    k = KINDS[b["kind"]]
    items = json.loads(b["payload"])
    # 匯入前資料保護：先備份，失敗則不匯入
    g.db.commit()
    try:
        backup = make_backup("pre-import")
    except Exception:
        flash("匯入前備份失敗，為保護資料，本次未匯入。請聯絡系統管理員。")
        return redirect(url_for("admin.import_detail", bid=bid))
    # 確認當下重新檢核（資料可能在預覽後變動），只寫入仍然有效的列
    raw = [(i["row"], {c: i["data"].get(c, "") for c in k["cols"]}) for i in items]
    fresh = k["validate"](raw)
    good = [i for i in fresh if i["action"] != "error"]
    errors = [i for i in fresh if i["action"] == "error"]
    try:
        if b["kind"] == "employees":
            added, updated = excel.apply_employees(good)
        else:
            added, updated = excel.apply_offers(good, g.user)
    except Exception:
        g.db.rollback()
        ex("UPDATE import_batches SET status='failed', payload=NULL WHERE id=?", (bid,))
        audit("Excel匯入", "import", bid, b["filename"], "失敗：寫入時發生錯誤，已還原")
        g.db.commit()
        flash("資料匯入失敗，請確認 Excel 格式後重新上傳。既有資料未受影響。")
        return redirect(url_for("admin.import_detail", bid=bid))
    ex("UPDATE import_batches SET status='confirmed', confirmed_at=?, payload=NULL, n_add=?, n_update=?, n_error=?, errors=?, backup_file=? WHERE id=?",
       (now(), added, updated, len(errors), json.dumps(errors, ensure_ascii=False), backup, bid))
    audit("Excel匯入", "import", bid, f'{k["label"]}：{b["filename"]}',
          changes={"新增": [0, added], "更新": [0, updated], "錯誤略過": [0, len(errors)]})
    flash(f"匯入完成：新增 {added} 筆、更新 {updated} 筆，略過錯誤 {len(errors)} 筆。")
    return redirect(url_for("admin.import_detail", bid=bid))


@bp.route("/imports/<int:bid>/errors.xlsx")
@perm_required("import.run")
def import_errors(bid):
    b = batch_or_404(bid)
    errors = json.loads(b["errors"] or "[]")
    bio = excel.errors_workbook(errors, KINDS[b["kind"]]["cols"])
    if KINDS[b["kind"]]["cols"] is excel.EMP_COLS:  # 錯誤清單不輸出初始密碼
        pass
    return send_file(bio, as_attachment=True, download_name=f"匯入錯誤清單_{bid}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- 廠商
@bp.route("/vendors")
@perm_required("vendor.manage", "offer.edit", "offer.review")
def vendors():
    kw = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    where, a = ["1=1"], []
    if kw:
        where.append("(v.code LIKE ? OR v.name LIKE ? OR v.brand LIKE ?)")
        a += [f"%{kw}%"] * 3
    if status in ("active", "inactive"):
        where.append("v.status=?")
        a.append(status)
    rows, pg = paginate("SELECT v.*, r.name region_name, (SELECT COUNT(*) FROM offers o WHERE o.vendor_id=v.id) n_offers, "
                        "(SELECT COUNT(*) FROM stores s WHERE s.vendor_id=v.id) n_stores FROM vendors v "
                        "LEFT JOIN regions r ON r.id=v.region_id WHERE " + " AND ".join(where) + " ORDER BY v.code", a)
    return render_template("admin/vendors.html", rows=rows, pg=pg, kw=kw, status=status)


@bp.route("/vendors/new", methods=["GET", "POST"])
@bp.route("/vendors/<int:vid>", methods=["GET", "POST"])
@perm_required("vendor.manage")
def vendor_form(vid=None):
    v = q("SELECT * FROM vendors WHERE id=?", (vid,), one=True) if vid else None
    if vid and not v:
        abort(404)
    errs = []
    if request.method == "POST":
        f = request.form
        d = {k: f.get(k, "").strip() for k in ("name", "brand", "contact", "phone", "email", "address", "website", "map_url", "note")}
        d["region_id"] = int(f["region_id"]) if f.get("region_id", "").isdigit() else None
        d["status"] = "active" if f.get("status", "active") == "active" else "inactive"
        if not d["name"]:
            errs.append("請填寫廠商名稱。")
        if not url_ok(d["website"]) or not url_ok(d["map_url"]):
            errs.append("網址須以 http:// 或 https:// 開頭。")
        try:
            logo = save_image(request.files.get("logo"))
        except ValueError as e_:
            errs.append(str(e_))
            logo = None
        if not errs:
            if logo:
                d["logo"] = logo
            t = now()
            if v:
                ch = diff(dict(v), d)
                sets = ",".join(f"{k}=?" for k in d)
                ex(f"UPDATE vendors SET {sets}, updated_at=? WHERE id=?", (*d.values(), t, vid))
                audit("停用廠商" if ch.get("status", [0, ""])[1] == "inactive" else "修改廠商", "vendor", vid,
                      f'{v["code"]} {d["name"]}', changes=ch)
            else:
                code = f.get("code", "").strip() or next_code("vendors", "V")
                if q("SELECT 1 FROM vendors WHERE code=?", (code,), one=True):
                    errs.append("廠商編號已存在。")
                else:
                    cols = ",".join(d)
                    cur = ex(f"INSERT INTO vendors(code,{cols},created_at,updated_at) VALUES(?,{','.join('?'*len(d))},?,?)",
                             (code, *d.values(), t, t))
                    vid = cur.lastrowid
                    audit("新增廠商", "vendor", vid, f'{code} {d["name"]}')
            if not errs:
                flash("廠商資料已儲存。")
                return redirect(url_for("admin.vendor_form", vid=vid))
        v = {**(dict(v) if v else {}), **d, "code": (v["code"] if v else request.form.get("code", ""))}
    stores = q("SELECT s.*, r.name region_name FROM stores s LEFT JOIN regions r ON r.id=s.region_id WHERE s.vendor_id=? ORDER BY s.code",
               (vid,)) if vid else []
    offers = q(lifecycle.BASE_SELECT + " WHERE o.vendor_id=? ORDER BY o.code, v.version_no DESC", (vid,)) if vid else []
    return render_template("admin/vendor_form.html", v=v, errs=errs, regions=region_list(), stores=stores, offers=offers)


@bp.route("/stores")
@perm_required("vendor.manage")
def stores():
    kw = request.args.get("q", "").strip()
    vid = request.args.get("vendor_id", "")
    where, a = ["1=1"], []
    if kw:
        where.append("(s.code LIKE ? OR s.name LIKE ? OR s.address LIKE ?)")
        a += [f"%{kw}%"] * 3
    if vid.isdigit():
        where.append("s.vendor_id=?")
        a.append(int(vid))
    rows, pg = paginate("SELECT s.*, v.name vendor_name, v.code vendor_code, r.name region_name FROM stores s JOIN vendors v ON v.id=s.vendor_id "
                        "LEFT JOIN regions r ON r.id=s.region_id WHERE " + " AND ".join(where) + " ORDER BY v.code, s.code", a)
    return render_template("admin/stores.html", rows=rows, pg=pg, kw=kw, vendor_id=vid,
                           vendors=q("SELECT id,code,name FROM vendors ORDER BY code"))


@bp.route("/stores/new", methods=["GET", "POST"])
@bp.route("/stores/<int:sid>", methods=["GET", "POST"])
@perm_required("vendor.manage")
def store_form(sid=None):
    s = q("SELECT * FROM stores WHERE id=?", (sid,), one=True) if sid else None
    if sid and not s:
        abort(404)
    errs = []
    if request.method == "POST":
        f = request.form
        d = {k: f.get(k, "").strip() for k in ("name", "address", "phone", "map_url")}
        d["region_id"] = int(f["region_id"]) if f.get("region_id", "").isdigit() else None
        d["status"] = "active" if f.get("status", "active") == "active" else "inactive"
        vendor_id = s["vendor_id"] if s else (int(f["vendor_id"]) if f.get("vendor_id", "").isdigit() else None)
        ven = q("SELECT * FROM vendors WHERE id=?", (vendor_id,), one=True)
        if not ven:
            errs.append("請選擇廠商。")
        if not d["name"]:
            errs.append("請填寫門市名稱。")
        if not url_ok(d["map_url"]):
            errs.append("網址須以 http:// 或 https:// 開頭。")
        if not errs:
            t = now()
            if s:
                ch = diff(dict(s), d)
                ex("UPDATE stores SET name=?,address=?,phone=?,map_url=?,region_id=?,status=?,updated_at=? WHERE id=?",
                   (d["name"], d["address"], d["phone"], d["map_url"], d["region_id"], d["status"], t, sid))
                audit("修改門市", "store", sid, f'{s["code"]} {d["name"]}', changes=ch)
            else:
                code = next_code("stores", "S")
                cur = ex("INSERT INTO stores(code,vendor_id,name,region_id,address,phone,map_url,status,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)", (code, vendor_id, d["name"], d["region_id"], d["address"], d["phone"],
                                                        d["map_url"], d["status"], t, t))
                sid = cur.lastrowid
                audit("新增門市", "store", sid, f'{code} {d["name"]}')
            flash("門市資料已儲存。")
            return redirect(url_for("admin.vendor_form", vid=vendor_id))
        s = {**(dict(s) if s else {}), **d, "vendor_id": vendor_id}
    else:
        if not s and request.args.get("vendor_id", "").isdigit():
            s = {"vendor_id": int(request.args["vendor_id"]), "status": "active"}
    return render_template("admin/store_form.html", s=s, errs=errs, regions=region_list(),
                           vendors=q("SELECT id,code,name FROM vendors WHERE status='active' ORDER BY code"))


# ---------------------------------------------------------------- 基礎資料（分類、地區）
def dict_page(table, label, extra_all=False):
    if request.method == "POST":
        f = request.form
        act = f.get("act")
        t = now()
        if act == "add":
            name = f.get("name", "").strip()
            if not name:
                flash("請輸入名稱。")
            elif q(f"SELECT 1 FROM {table} WHERE name=?", (name,), one=True):
                flash("名稱已存在。")
            else:
                n = q(f"SELECT COALESCE(MAX(sort),0)+1 n FROM {table}", one=True)["n"]
                cur = ex(f"INSERT INTO {table}(name,sort,created_at,updated_at) VALUES(?,?,?,?)", (name, n, t, t))
                audit(f"新增{label}", table, cur.lastrowid, name)
                flash(f"已新增{label}。")
        elif f.get("id", "").isdigit():
            row = q(f"SELECT * FROM {table} WHERE id=?", (int(f["id"]),), one=True) or abort(404)
            if act == "toggle":
                new = 0 if row["active"] else 1
                ex(f"UPDATE {table} SET active=?, updated_at=? WHERE id=?", (new, t, row["id"]))
                audit(f"{'啟用' if new else '停用'}{label}", table, row["id"], row["name"], changes={"active": [row["active"], new]})
                flash("已更新。")
            elif act == "save":
                name = f.get("name", "").strip()
                sort = int(f["sort"]) if f.get("sort", "").lstrip("-").isdigit() else row["sort"]
                dup = q(f"SELECT 1 FROM {table} WHERE name=? AND id!=?", (name, row["id"]), one=True)
                if not name or dup:
                    flash("名稱不可空白或重複。")
                else:
                    ch = diff(dict(row), {"name": name, "sort": sort})
                    ex(f"UPDATE {table} SET name=?, sort=?, updated_at=? WHERE id=?", (name, sort, t, row["id"]))
                    audit(f"修改{label}", table, row["id"], name, changes=ch)
                    flash("已儲存。")
        return redirect(request.url)
    rows = q(f"SELECT t.*, (SELECT COUNT(*) FROM {'offer_versions' if table=='categories' else 'version_regions'} x "
             f"WHERE x.{'category_id' if table=='categories' else 'region_id'}=t.id) used FROM {table} t ORDER BY sort,id")
    return render_template("admin/dict.html", rows=rows, label=label, table=table)


@bp.route("/categories", methods=["GET", "POST"])
@perm_required("basic.manage")
def categories():
    return dict_page("categories", "福利分類")


@bp.route("/regions", methods=["GET", "POST"])
@perm_required("basic.manage")
def regions():
    return dict_page("regions", "地區")


# ---------------------------------------------------------------- 系統：管理者、角色、操作紀錄、設定
@bp.route("/admins", methods=["GET", "POST"])
@perm_required("admin.manage")
def admins():
    if request.method == "POST":
        emp = q("SELECT * FROM employees WHERE emp_no=?", (request.form.get("emp_no", "").strip(),), one=True)
        role = q("SELECT * FROM roles WHERE id=?", (request.form.get("role_id", "0"),), one=True)
        if not emp or not role:
            flash("找不到該員工或角色。")
        elif emp["id"] == g.user["id"]:
            flash("不能變更自己的角色，避免失去管理權限。")
        elif emp["role_id"] == 3 and role["id"] != 3 and q("SELECT COUNT(*) c FROM employees WHERE role_id=3 AND active=1", one=True)["c"] <= 1:
            flash("至少需保留一位系統管理員。")
        else:
            ex("UPDATE employees SET role_id=?, updated_at=? WHERE id=?", (role["id"], now(), emp["id"]))
            old = q("SELECT name FROM roles WHERE id=?", (emp["role_id"],), one=True)["name"]
            audit("權限異動", "employee", emp["id"], f'{emp["name"]}（{emp["emp_no"]}）', changes={"role": [old, role["name"]]})
            flash("角色已更新。")
        return redirect(url_for("admin.admins"))
    rows = q("SELECT e.*, r.name role_name FROM employees e JOIN roles r ON r.id=e.role_id WHERE e.role_id>1 ORDER BY e.role_id DESC, e.emp_no")
    return render_template("admin/admins.html", rows=rows, roles=q("SELECT * FROM roles ORDER BY id"))


@bp.route("/roles", methods=["GET", "POST"])
@perm_required("admin.manage")
def roles():
    roles_ = q("SELECT * FROM roles ORDER BY id")
    grants = {(r["role_id"], r["perm"]) for r in q("SELECT * FROM role_permissions")}
    if request.method == "POST":
        changes = {}
        for r in roles_:
            if r["id"] == 1:
                continue  # 一般員工不可被授予後台權限
            for p, label in PERMISSIONS:
                want = bool(request.form.get(f"{r['id']}:{p}"))
                if r["id"] == 3 and p == "admin.manage":
                    want = True  # 系統管理員永遠保有管理權限
                have = (r["id"], p) in grants
                if want != have:
                    if want:
                        ex("INSERT INTO role_permissions VALUES(?,?)", (r["id"], p))
                    else:
                        ex("DELETE FROM role_permissions WHERE role_id=? AND perm=?", (r["id"], p))
                    changes[f'{r["name"]}／{label}'] = ["有" if have else "無", "有" if want else "無"]
        if changes:
            audit("權限異動", "role", None, "角色權限矩陣", changes=changes)
            flash("權限設定已更新。")
        return redirect(url_for("admin.roles"))
    return render_template("admin/roles.html", roles=roles_, grants=grants, PERMS=PERMISSIONS)


@bp.route("/audit")
@perm_required("audit.view")
def audit_log():
    f = request.args
    where, a = ["1=1"], []
    if f.get("action"):
        where.append("action LIKE ?")
        a.append(f"%{f['action']}%")
    if f.get("actor"):
        where.append("actor_name LIKE ?")
        a.append(f"%{f['actor']}%")
    if f.get("target"):
        where.append("(target_label LIKE ? OR target_id=?)")
        a += [f"%{f['target']}%", f["target"]]
    if f.get("result") == "fail":
        where.append("result!='成功'")
    if f.get("from"):
        where.append("at>=?")
        a.append(f["from"])
    if f.get("to"):
        where.append("at<?")
        a.append(f["to"] + " 99")
    rows, pg = paginate("SELECT * FROM audit_logs WHERE " + " AND ".join(where) + " ORDER BY id DESC", a, per=30)
    return render_template("admin/audit.html", rows=rows, pg=pg, f=f, json=json)


@bp.route("/settings", methods=["GET", "POST"])
@perm_required("admin.manage")
def settings():
    keys = {"allow_leave_login": "bool", "session_idle_minutes": "int", "max_failed_logins": "int", "lock_minutes": "int",
            "self_review_allowed": "bool", "expiring_days": "int", "backup_keep": "int", "site_name": "text"}
    if request.method == "POST":
        act = request.form.get("act")
        if act == "backup":
            name = make_backup("manual")
            audit("手動備份", "backup", name, name)
            flash(f"已建立備份：{name}")
        elif act == "restore":
            try:
                restore_backup(request.form.get("name", ""))
                audit("還原備份", "backup", request.form.get("name"), request.form.get("name"))
                flash("已還原備份（還原前已自動另存目前資料）。")
            except Exception:
                flash("還原失敗，請確認備份檔案。")
        else:
            changes = {}
            for k, typ in keys.items():
                v = request.form.get(k, "").strip()
                if typ == "bool":
                    v = "1" if request.form.get(k) else "0"
                elif typ == "int":
                    v = v if v.isdigit() and int(v) > 0 else setting(k)
                elif not v:
                    v = setting(k)
                if v != setting(k):
                    changes[k] = [setting(k), v]
                    ex("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
            if changes:
                audit("修改系統設定", "settings", None, "系統設定", changes=changes)
            flash("設定已儲存。")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", s={k: setting(k) for k in keys}, backups=list_backups())


@bp.route("/backups/<name>")
@perm_required("admin.manage")
def backup_download(name):
    if not re.fullmatch(r"welfare-[0-9\-]+-[a-z\-]+\.db", name):
        abort(404)
    audit("下載備份", "backup", name, name)
    return send_from_directory(db.BACKUP_DIR, name, as_attachment=True)


import views_admin_offers  # noqa: E402,F401  (註冊優惠／公告／統計路由)
