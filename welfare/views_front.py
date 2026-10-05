"""員工前台。"""
import os
import secrets
from datetime import timedelta

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db
from db import q, ex, now, today, iso, audit, setting, setting_int
from lifecycle import BASE_SELECT, visible_where
from security import login_required, password_problem

bp = Blueprint("front", __name__)


def can_login_status(u):
    """回傳不可登入的原因，可登入則回傳 None。"""
    if not u["active"]:
        return "此帳號已停用，請洽 HR。"
    if u["status"] == "離職":
        return "此帳號已停用，請洽 HR。"
    if u["status"] == "留停" and setting("allow_leave_login") != "1":
        return "此帳號目前無法登入，請洽 HR。"
    return None


def safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user and not g.user["must_change_pw"]:
        return redirect(url_for("front.home"))
    msg = session.pop("flash_msg", None)
    if request.method == "POST":
        emp_no = request.form.get("emp_no", "").strip()
        pw = request.form.get("password", "")
        u = q("SELECT * FROM employees WHERE emp_no=?", (emp_no,), one=True)
        generic = "員工編號或密碼錯誤，請再確認一次。"
        if u is None:  # 1. 員工編號是否存在
            audit("登入失敗", "employee", None, emp_no, "失敗", actor_name=f"未知（{emp_no[:20]}）")
            msg = generic
        elif u["locked_until"] and u["locked_until"] > now():  # 4. 是否遭鎖定
            audit("登入失敗", "employee", u["id"], emp_no, "失敗：帳號鎖定中", actor=u)
            msg = f"登入錯誤次數過多，帳號暫時鎖定至 {u['locked_until'][11:16]}，請稍後再試。"
        elif can_login_status(u):  # 2/3. 在職、啟用
            audit("登入失敗", "employee", u["id"], emp_no, "失敗：帳號不可登入", actor=u)
            msg = can_login_status(u)
        elif not check_password_hash(u["password_hash"], pw):  # 5. 密碼
            n = u["failed_count"] + 1
            lock = None
            if n >= setting_int("max_failed_logins"):
                lock = (db.datetime.now(db.TZ) + timedelta(minutes=setting_int("lock_minutes"))).strftime("%Y-%m-%d %H:%M:%S")
                n = 0
            ex("UPDATE employees SET failed_count=?, locked_until=? WHERE id=?", (n, lock, u["id"]))
            audit("登入失敗", "employee", u["id"], emp_no, "失敗：密碼錯誤" + ("，帳號已鎖定" if lock else ""), actor=u)
            msg = generic
        else:
            ex("UPDATE employees SET failed_count=0, locked_until=NULL, last_login=? WHERE id=?", (now(), u["id"]))
            session.clear()  # 防止 session fixation
            session["uid"] = u["id"]
            session["last"] = __import__("time").time()
            g.user = u
            ex("INSERT INTO usage_events(at,kind,emp_id) VALUES(?,?,?)", (now(), "login", u["id"]))
            audit("登入", "employee", u["id"], emp_no, actor=u)
            if u["must_change_pw"]:
                return redirect(url_for("front.change_password"))
            return redirect(safe_next(request.args.get("next")) or url_for("front.home"))
    return render_template("front/login.html", msg=msg)


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit("登出", "employee", g.user["id"], g.user["emp_no"])
    session.clear()
    return redirect(url_for("front.login"))


@bp.route("/forgot")
def forgot():
    return render_template("front/forgot.html")


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    msg = None
    if request.method == "POST":
        old, new, new2 = (request.form.get(k, "") for k in ("old", "new", "new2"))
        if not check_password_hash(g.user["password_hash"], old):
            msg = "目前的密碼不正確。"
        elif new != new2:
            msg = "兩次輸入的新密碼不一致。"
        elif new == old:
            msg = "新密碼不可與目前密碼相同。"
        elif password_problem(new):
            msg = password_problem(new)
        else:
            ex("UPDATE employees SET password_hash=?, must_change_pw=0, updated_at=? WHERE id=?",
               (generate_password_hash(new), now(), g.user["id"]))
            audit("修改密碼", "employee", g.user["id"], g.user["emp_no"])
            flash("密碼已更新。")
            return redirect(url_for("front.home"))
    return render_template("front/password.html", msg=msg, forced=bool(g.user["must_change_pw"]))


def cards(where="", params=None, order="v.published_at DESC", limit=None, offset=0):
    p = {"today": iso(today())}
    p.update(params or {})
    sql = BASE_SELECT + " WHERE " + visible_where() + (" AND " + where if where else "") + " ORDER BY " + order
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    return q(sql, p)


def filters():
    cats = q("SELECT * FROM categories WHERE active=1 ORDER BY sort,id")
    regs = q("SELECT * FROM regions WHERE active=1 ORDER BY sort,id")
    return cats, regs


@bp.route("/")
@login_required
def home():
    t = today()
    cats, regs = filters()
    latest = cards(limit=6)
    popular = cards(order="v.views DESC, v.published_at DESC", limit=6)
    expiring = cards("v.end_date<=:lim", {"lim": iso(t + timedelta(days=30))}, "v.end_date ASC", 6)
    anns = announcements_query(limit=3)
    return render_template("front/home.html", cats=cats, regs=regs, latest=latest, popular=popular,
                           expiring=expiring, anns=anns)


def announcements_query(limit=None):
    t = iso(today())
    sql = ("SELECT * FROM announcements WHERE status='published' AND publish_date<=? "
           "AND (end_date IS NULL OR end_date='' OR end_date>=?) ORDER BY is_pinned DESC, publish_date DESC, id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return q(sql, (t, t))


@bp.route("/search")
@login_required
def search():
    cats, regs = filters()
    kw = request.args.get("q", "").strip()[:60]
    cat_ids = [int(x) for x in request.args.getlist("cat") if x.isdigit()]
    reg_ids = [int(x) for x in request.args.getlist("region") if x.isdigit()]
    sort = request.args.get("sort", "latest")
    where, params = [], {}
    if kw:
        # 以「空白分隔的每個詞」都必須命中（廠商、品牌、優惠名稱、內容、使用方式、地區、門市）
        for i, term in enumerate(kw.split()):
            k = f"kw{i}"
            params[k] = "%" + term.replace("%", r"\%").replace("_", r"\_") + "%"
            where.append(
                f"(ven.name LIKE :{k} ESCAPE '\\' OR ven.brand LIKE :{k} ESCAPE '\\' OR v.name LIKE :{k} ESCAPE '\\' "
                f"OR v.content LIKE :{k} ESCAPE '\\' OR v.usage LIKE :{k} ESCAPE '\\' OR c.name LIKE :{k} ESCAPE '\\' "
                f"OR EXISTS(SELECT 1 FROM version_regions vr JOIN regions r ON r.id=vr.region_id "
                f"WHERE vr.version_id=v.id AND r.name LIKE :{k} ESCAPE '\\') "
                f"OR EXISTS(SELECT 1 FROM version_stores vs JOIN stores s ON s.id=vs.store_id "
                f"WHERE vs.version_id=v.id AND (s.name LIKE :{k} ESCAPE '\\' OR s.address LIKE :{k} ESCAPE '\\')))")
    if cat_ids:
        ph = ",".join(f":c{i}" for i in range(len(cat_ids)))
        params.update({f"c{i}": c for i, c in enumerate(cat_ids)})
        where.append(f"v.category_id IN ({ph})")
    if reg_ids:
        ph = ",".join(f":r{i}" for i in range(len(reg_ids)))
        params.update({f"r{i}": c for i, c in enumerate(reg_ids)})
        where.append(f"EXISTS(SELECT 1 FROM version_regions vr JOIN regions r ON r.id=vr.region_id "
                     f"WHERE vr.version_id=v.id AND (vr.region_id IN ({ph}) OR r.is_all=1))")
    if sort == "expiring":
        where.append("v.end_date<=:lim")
        params["lim"] = iso(today() + timedelta(days=setting_int("expiring_days")))
    order = {"latest": "v.is_pinned DESC, v.published_at DESC", "expiring": "v.end_date ASC",
             "popular": "v.views DESC", "vendor": "ven.name COLLATE NOCASE ASC"}.get(sort, "v.published_at DESC")
    w = " AND ".join(where)
    results = cards(w, params, order)
    # 統計（不記錄個人；單一類別/地區才記，避免複選難以歸因）
    if kw or cat_ids or reg_ids:
        ex("INSERT INTO usage_events(at,kind,category_id,region_id,keyword) VALUES(?,?,?,?,?)",
           (now(), "search", cat_ids[0] if len(cat_ids) == 1 else None,
            reg_ids[0] if len(reg_ids) == 1 else None, kw or None))
    return render_template("front/search.html", cats=cats, regs=regs, results=results, kw=kw,
                           cat_ids=cat_ids, reg_ids=reg_ids, sort=sort)


@bp.route("/offers/<int:vid>")
@login_required
def offer_detail(vid):
    # 只有「目前可見」的優惠員工才查得到（已過期／草稿／停用一律 404）
    rows = cards("v.id=:vid", {"vid": vid})
    if not rows:
        abort(404)
    v = rows[0]
    ex("UPDATE offer_versions SET views=views+1 WHERE id=?", (vid,))
    regs = q("SELECT r.* FROM version_regions vr JOIN regions r ON r.id=vr.region_id WHERE vr.version_id=? ORDER BY r.sort", (vid,))
    ex("INSERT INTO usage_events(at,kind,version_id,vendor_id,category_id,region_id) VALUES(?,?,?,?,?,?)",
       (now(), "view", vid, v["vendor_id"], v["category_id"], regs[0]["id"] if len(regs) == 1 else None))
    stores = q("SELECT s.*, r.name AS region_name FROM version_stores vs JOIN stores s ON s.id=vs.store_id "
               "LEFT JOIN regions r ON r.id=s.region_id WHERE vs.version_id=? AND s.status='active' "
               "ORDER BY r.sort, s.name", (vid,))
    vendor = q("SELECT * FROM vendors WHERE id=?", (v["vendor_id"],), one=True)
    return render_template("front/offer.html", v=v, regs=regs, stores=stores, vendor=vendor)


@bp.route("/announcements")
@login_required
def announcements():
    return render_template("front/announcements.html", anns=announcements_query())


@bp.route("/announcements/<int:aid>")
@login_required
def announcement(aid):
    t = iso(today())
    a = q("SELECT * FROM announcements WHERE id=? AND status='published' AND publish_date<=? "
          "AND (end_date IS NULL OR end_date='' OR end_date>=?)", (aid, t, t), one=True)
    if not a:
        abort(404)
    return render_template("front/announcement.html", a=a)


@bp.route("/me")
@login_required
def me():
    emp = q("SELECT e.*, r.name AS region_name FROM employees e LEFT JOIN regions r ON r.id=e.region_id "
            "WHERE e.id=?", (g.user["id"],), one=True)  # 只讀取自己的資料，不接受任何 id 參數
    return render_template("front/me.html", emp=emp)


@bp.route("/uploads/<name>")
@login_required
def uploads(name):
    return send_from_directory(db.UPLOAD_DIR, name)
