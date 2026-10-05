"""後台：優惠（含審核、續約、版本）、公告、統計。"""
from datetime import date, timedelta

from flask import abort, flash, g, redirect, render_template, request, url_for

import lifecycle
from db import q, ex, now, today, iso, audit, diff, setting, setting_int
from lifecycle import BASE_SELECT, MGMT_LABEL, RENEWAL_STATES, MANUAL_RENEWAL_STATES
from security import perm_required
from views_admin import bp, paginate, region_list, url_ok
from views_admin_helpers import create_offer_version, log_review, one_year_after, save_image

OFFER_PERMS = ("offer.edit", "offer.review", "stats.view")
T = lambda: iso(today())

TABS = {
    "all": ("全部優惠", "v.mgmt_status!='archived'"),
    "draft": ("草稿", "v.mgmt_status='draft'"),
    "pending": ("待審核", "v.mgmt_status='pending'"),
    "valid": ("有效優惠", "v.mgmt_status='published' AND v.start_date<=:t AND v.end_date>=:t"),
    "expiring": ("即將到期", "v.mgmt_status='published' AND v.end_date BETWEEN :t AND :lim"),
    "expired": ("過期優惠", "v.mgmt_status='published' AND v.end_date<:t"),
    "renewal": ("待續約", "v.mgmt_status='published' AND v.end_date<=:lim AND v.end_date>=:ago "
                          "AND v.version_no=(SELECT MAX(version_no) FROM offer_versions x WHERE x.offer_id=v.offer_id) "
                          "AND v.renewal_status!='已續約'"),
    "archived": ("封存", "v.mgmt_status='archived'"),
}


@bp.route("/offers")
@perm_required(*OFFER_PERMS)
def offers():
    f = request.args
    tab = f.get("tab", "all") if f.get("tab", "all") in TABS else "all"
    t = today()
    p = {"t": iso(t), "lim": iso(t + timedelta(days=setting_int("expiring_days"))), "ago": iso(t - timedelta(days=365))}
    where = [TABS[tab][1]]
    if f.get("vendor_id", "").isdigit():
        where.append("o.vendor_id=:vid")
        p["vid"] = int(f["vendor_id"])
    if f.get("cat", "").isdigit():
        where.append("v.category_id=:cat")
        p["cat"] = int(f["cat"])
    if f.get("region", "").isdigit():
        where.append("EXISTS(SELECT 1 FROM version_regions vr WHERE vr.version_id=v.id AND vr.region_id=:reg)")
        p["reg"] = int(f["region"])
    if f.get("kw", "").strip():
        where.append("(v.name LIKE :kw OR ven.name LIKE :kw OR o.code LIKE :kw OR v.content LIKE :kw)")
        p["kw"] = f"%{f['kw'].strip()}%"
    if tab == "expired":
        if f.get("from"):
            where.append("v.end_date>=:from")
            p["from"] = f["from"]
        if f.get("to"):
            where.append("v.end_date<=:to")
            p["to"] = f["to"]
    if f.get("renewal") in RENEWAL_STATES:
        where.append("v.renewal_status=:rs")
        p["rs"] = f["renewal"]
    order = "v.end_date ASC" if tab in ("expiring", "renewal") else ("v.end_date DESC" if tab == "expired" else "v.updated_at DESC")
    rows, pg = paginate(BASE_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY " + order, p)
    counts = {k: q("SELECT COUNT(*) c FROM offer_versions v JOIN offers o ON o.id=v.offer_id JOIN vendors ven ON ven.id=o.vendor_id WHERE " + w,
                   p, one=True)["c"] for k, (_, w) in TABS.items() if k in ("pending", "expiring", "renewal")}
    return render_template("admin/offers.html", rows=rows, pg=pg, tab=tab, TABS=TABS, f=f, counts=counts,
                           vendors=q("SELECT id,code,name FROM vendors ORDER BY code"),
                           cats=q("SELECT * FROM categories ORDER BY sort"), regs=region_list(), RS=RENEWAL_STATES)


@bp.route("/history")
@perm_required(*OFFER_PERMS)
def history():
    kw = request.args.get("kw", "").strip()
    where, a = ["1=1"], []
    if kw:
        where.append("(ven.name LIKE ? OR o.code LIKE ? OR EXISTS(SELECT 1 FROM offer_versions x WHERE x.offer_id=o.id AND x.name LIKE ?))")
        a += [f"%{kw}%"] * 3
    rows, pg = paginate(
        "SELECT o.id, o.code, ven.name vendor_name, ven.code vendor_code, "
        "(SELECT COUNT(*) FROM offer_versions x WHERE x.offer_id=o.id) n_ver, "
        "(SELECT x.id FROM offer_versions x WHERE x.offer_id=o.id ORDER BY version_no DESC LIMIT 1) latest_id "
        "FROM offers o JOIN vendors ven ON ven.id=o.vendor_id WHERE " + " AND ".join(where) + " ORDER BY o.code", a)
    out = [(r, q(BASE_SELECT + " WHERE v.offer_id=? ORDER BY v.version_no DESC", (r["id"],))) for r in rows]
    return render_template("admin/history.html", out=out, pg=pg, kw=kw)


def parse_form(vendor_id):
    f = request.form
    errs = []
    d = dict(name=f.get("name", "").strip(), content=f.get("content", "").strip(), usage=f.get("usage", "").strip(),
             notes=f.get("notes", "").strip(), start_date=f.get("start_date", ""), end_date=f.get("end_date", ""),
             is_pinned=1 if f.get("is_pinned") else 0, is_recommended=1 if f.get("is_recommended") else 0)
    cat = q("SELECT * FROM categories WHERE id=? AND active=1", (f.get("category_id", "0"),), one=True)
    d["category_id"] = cat["id"] if cat else None
    if not d["name"]:
        errs.append("請填寫優惠名稱。")
    if not d["content"]:
        errs.append("請填寫優惠內容。")
    if not cat:
        errs.append("請選擇有效的福利分類。")
    try:
        s, e = date.fromisoformat(d["start_date"]), date.fromisoformat(d["end_date"])
        if s > e:
            errs.append("開始日期不得晚於結束日期。")
    except ValueError:
        errs.append("請填寫正確的開始與結束日期。")
    reg_ids = [int(x) for x in f.getlist("regions") if x.isdigit()]
    if reg_ids:
        reg_ids = [r["id"] for r in q(f"SELECT id FROM regions WHERE active=1 AND id IN ({','.join('?'*len(reg_ids))})", reg_ids)]
    if not reg_ids:
        errs.append("請至少選擇一個適用地區。")
    store_ids = [int(x) for x in f.getlist("stores") if x.isdigit()]
    if store_ids:  # 規則6：門市必須存在、屬於該廠商且啟用
        ok = {r["id"] for r in q(f"SELECT id FROM stores WHERE vendor_id=? AND status='active' AND id IN ({','.join('?'*len(store_ids))})",
                                [vendor_id, *store_ids])}
        if ok != set(store_ids):
            errs.append("適用門市含有不存在、已停用或不屬於此廠商的門市。")
        store_ids = list(ok)
    return d, reg_ids, store_ids, errs


def offer_form_ctx(vendor_id, v=None, **kw):
    return render_template("admin/offer_form.html", v=v, vendor=q("SELECT * FROM vendors WHERE id=?", (vendor_id,), one=True),
                           cats=q("SELECT * FROM categories WHERE active=1 ORDER BY sort"), regs=region_list(),
                           stores=q("SELECT * FROM stores WHERE vendor_id=? AND status='active' ORDER BY code", (vendor_id,)), **kw)


@bp.route("/offers/new", methods=["GET", "POST"])
@perm_required("offer.edit")
def offer_new():
    vid = request.values.get("vendor_id", "")
    vendor = q("SELECT * FROM vendors WHERE id=?", (vid,), one=True) if vid.isdigit() else None
    if not vendor:
        return render_template("admin/offer_pick_vendor.html", vendors=q("SELECT * FROM vendors WHERE status='active' ORDER BY code"))
    if vendor["status"] != "active":  # 規則7
        flash("此廠商已停用，不可建立新優惠。")
        return redirect(url_for("admin.vendor_form", vid=vendor["id"]))
    if request.method == "POST":
        d, regs, stores, errs = parse_form(vendor["id"])
        if not errs:
            nid = create_offer_version(None, vendor["id"], d, regs, stores, g.user)
            audit("新增優惠", "offer_version", nid, f'{vendor["name"]} {d["name"]}')
            flash("優惠草稿已建立。")
            return redirect(url_for("admin.offer_detail", vid=nid))
        v = {**d, "regions": regs, "stores": stores}
        return offer_form_ctx(vendor["id"], v, errs=errs)
    return offer_form_ctx(vendor["id"], {"start_date": T(), "end_date": iso(one_year_after(today())), "regions": [], "stores": []}, errs=[])


def get_version(vid):
    v = q(BASE_SELECT + " WHERE v.id=?", (vid,), one=True)
    return v or abort(404)


@bp.route("/offers/<int:vid>/edit", methods=["GET", "POST"])
@perm_required("offer.edit")
def offer_edit(vid):
    v = get_version(vid)
    if v["mgmt_status"] != "draft":  # 規則3、12：非草稿版本內容不可修改
        flash("只有「草稿」可以編輯。已送審或已發布的版本請以「續約」建立新版本，歷史版本不會被修改。")
        return redirect(url_for("admin.offer_detail", vid=vid))
    if request.method == "POST":
        d, regs, stores, errs = parse_form(v["vendor_id"])
        if not errs:
            old = dict(v)
            old["regions"] = ",".join(str(r["region_id"]) for r in q("SELECT region_id FROM version_regions WHERE version_id=? ORDER BY 1", (vid,)))
            ex("UPDATE offer_versions SET name=?,category_id=?,content=?,usage=?,notes=?,start_date=?,end_date=?,is_pinned=?,"
               "is_recommended=?,updated_by=?,updated_at=? WHERE id=?",
               (d["name"], d["category_id"], d["content"], d["usage"], d["notes"], d["start_date"], d["end_date"],
                d["is_pinned"], d["is_recommended"], g.user["id"], now(), vid))
            ex("DELETE FROM version_regions WHERE version_id=?", (vid,))
            ex("DELETE FROM version_stores WHERE version_id=?", (vid,))
            for r in regs:
                ex("INSERT INTO version_regions VALUES(?,?)", (vid, r))
            for s in stores:
                ex("INSERT INTO version_stores VALUES(?,?)", (vid, s))
            new = {**d, "regions": ",".join(str(r) for r in sorted(regs))}
            audit("修改優惠", "offer_version", vid, f'{v["vendor_name"]} {d["name"]} V{v["version_no"]}', changes=diff(old, new))
            flash("已儲存。")
            return redirect(url_for("admin.offer_detail", vid=vid))
        return offer_form_ctx(v["vendor_id"], {**dict(v), **d, "regions": regs, "stores": stores}, errs=errs)
    cur = dict(v)
    cur["regions"] = [r["region_id"] for r in q("SELECT region_id FROM version_regions WHERE version_id=?", (vid,))]
    cur["stores"] = [r["store_id"] for r in q("SELECT store_id FROM version_stores WHERE version_id=?", (vid,))]
    return offer_form_ctx(v["vendor_id"], cur, errs=[])


@bp.route("/offers/<int:vid>")
@perm_required(*OFFER_PERMS)
def offer_detail(vid):
    v = get_version(vid)
    regs = q("SELECT r.* FROM version_regions vr JOIN regions r ON r.id=vr.region_id WHERE vr.version_id=? ORDER BY r.sort", (vid,))
    stores = q("SELECT s.*, r.name region_name FROM version_stores vs JOIN stores s ON s.id=vs.store_id LEFT JOIN regions r ON r.id=s.region_id "
               "WHERE vs.version_id=? ORDER BY s.code", (vid,))
    versions = q(BASE_SELECT + " WHERE v.offer_id=? ORDER BY v.version_no DESC", (v["offer_id"],))
    logs = q("SELECT * FROM review_logs WHERE version_id=? ORDER BY id DESC", (vid,))
    users = {r["id"]: f'{r["name"]}（{r["emp_no"]}）' for r in q("SELECT id,name,emp_no FROM employees")}
    latest = max(x["version_no"] for x in versions)
    return render_template("admin/offer_detail.html", v=v, regs=regs, stores=stores, versions=versions, logs=logs,
                           users=users, is_latest=v["version_no"] == latest, RS=MANUAL_RENEWAL_STATES)


def overlap(v):
    return q("SELECT version_no FROM offer_versions WHERE offer_id=? AND id!=? AND mgmt_status='published' AND start_date<=? AND end_date>=?",
             (v["offer_id"], v["id"], v["end_date"], v["start_date"]), one=True)


def publish_check(v):
    if v["end_date"] < T():
        return "結束日期已過，無法送審或發布；請修改日期（草稿）或以續約建立新版本。"
    if v["vendor_status"] != "active":
        return "廠商已停用，不可發布優惠。"
    o = overlap(v)
    if o:
        return f"期間與同優惠的 V{o['version_no']} 重疊，請調整日期。"
    return None


@bp.route("/offers/<int:vid>/action", methods=["POST"])
@perm_required("offer.edit", "offer.review")
def offer_action(vid):
    v = get_version(vid)
    act = request.form.get("act")
    note = request.form.get("note", "").strip()[:500]
    st = v["mgmt_status"]
    label = f'{v["vendor_name"]} {v["name"]} V{v["version_no"]}'
    t = now()
    need = {"submit": "offer.edit", "approve": "offer.review", "reject": "offer.review", "disable": "offer.review",
            "enable": "offer.review", "archive": "offer.edit", "pin": "offer.edit", "recommend": "offer.edit"}.get(act)
    if not need:
        abort(400)
    if need not in g.perms:
        abort(403)

    def go(msg):
        flash(msg)
        return redirect(url_for("admin.offer_detail", vid=vid))
    if act == "submit" and st == "draft":
        p = publish_check(v)
        if p:
            return go(p)
        ex("UPDATE offer_versions SET mgmt_status='pending', updated_by=?, updated_at=? WHERE id=?", (g.user["id"], t, vid))
        log_review(vid, "提交審核", g.user, note)
        audit("優惠送審", "offer_version", vid, label, changes={"mgmt_status": ["草稿", "待審核"]})
        return go("已提交審核。")
    if act == "approve" and st == "pending":
        if setting("self_review_allowed") != "1" and v["created_by"] == g.user["id"]:
            return go("依系統設定，建立人不得自行審核，請由其他審核者處理。")
        p = publish_check(v)
        if p:
            return go(p)
        ex("UPDATE offer_versions SET mgmt_status='published', reviewed_by=?, reviewed_at=?, review_note=?, published_by=?, "
           "published_at=?, expired_processed_at=NULL, updated_at=? WHERE id=?", (g.user["id"], t, note, g.user["id"], t, t, vid))
        log_review(vid, "核准並發布", g.user, note)
        audit("優惠審核", "offer_version", vid, label, changes={"mgmt_status": ["待審核", "已發布"]})
        audit("優惠發布", "offer_version", vid, label)
        return go("已核准並發布，員工即可在前台查看（於開始日起生效）。")
    if act == "reject" and st == "pending":
        if not note:
            return go("退回時請填寫原因。")
        ex("UPDATE offer_versions SET mgmt_status='draft', review_note=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
           (note, g.user["id"], t, t, vid))
        log_review(vid, "退回修改", g.user, note)
        audit("優惠審核", "offer_version", vid, label, changes={"mgmt_status": ["待審核", "草稿"], "note": ["", note]})
        return go("已退回，優惠回到草稿。")
    if act == "disable" and st == "published":
        ex("UPDATE offer_versions SET mgmt_status='disabled', updated_at=?, updated_by=? WHERE id=?", (t, g.user["id"], vid))
        log_review(vid, "人工停用", g.user, note)
        audit("停用優惠", "offer_version", vid, label, changes={"mgmt_status": ["已發布", "人工停用"]})
        return go("已停用，員工前台不再顯示。")
    if act == "enable" and st == "disabled":
        p = publish_check(v)
        if p:
            return go(p)
        ex("UPDATE offer_versions SET mgmt_status='published', updated_at=?, updated_by=? WHERE id=?", (t, g.user["id"], vid))
        log_review(vid, "重新啟用", g.user, note)
        audit("啟用優惠", "offer_version", vid, label, changes={"mgmt_status": ["人工停用", "已發布"]})
        return go("已重新啟用。")
    if act == "archive":
        expired_pub = st == "published" and v["end_date"] < T()
        if st in ("draft", "disabled") or expired_pub:
            ex("UPDATE offer_versions SET mgmt_status='archived', updated_at=?, updated_by=? WHERE id=?", (t, g.user["id"], vid))
            log_review(vid, "封存", g.user, note)
            audit("封存優惠", "offer_version", vid, label, changes={"mgmt_status": [MGMT_LABEL[st], "封存"]})
            return go("已封存（資料保留，可在「封存」分頁查詢）。")
        return go("目前狀態不能封存；有效中的優惠請先停用。")
    if act in ("pin", "recommend") and st != "archived":
        col = "is_pinned" if act == "pin" else "is_recommended"
        new = 0 if v[col] else 1
        ex(f"UPDATE offer_versions SET {col}=?, updated_at=? WHERE id=?", (new, t, vid))
        audit("修改優惠", "offer_version", vid, label, changes={col: [v[col], new]})
        return go("已更新。")
    return go("目前狀態無法執行此操作。")


@bp.route("/offers/<int:vid>/renew", methods=["POST"])
@perm_required("offer.edit")
def offer_renew(vid):
    v = get_version(vid)
    latest = q("SELECT MAX(version_no) m FROM offer_versions WHERE offer_id=?", (v["offer_id"],), one=True)["m"]
    if v["version_no"] != latest:
        flash("此優惠已有更新的版本，請由最新版本續約。")
    elif v["mgmt_status"] not in ("published", "disabled"):
        flash("只有曾經發布的優惠可以續約。")
    elif v["vendor_status"] != "active":
        flash("廠商已停用，不可建立新優惠版本。")
    else:
        s = max(date.fromisoformat(v["end_date"]) + timedelta(days=1), today())
        fields = dict(name=v["name"], category_id=v["category_id"], content=v["content"], usage=v["usage"], notes=v["notes"],
                      start_date=iso(s), end_date=iso(one_year_after(s)), is_pinned=v["is_pinned"], is_recommended=v["is_recommended"])
        regs = [r["region_id"] for r in q("SELECT region_id FROM version_regions WHERE version_id=?", (vid,))]
        stores = [r["id"] for r in q("SELECT s.id FROM version_stores vs JOIN stores s ON s.id=vs.store_id WHERE vs.version_id=? AND s.status='active'", (vid,))]
        nid = create_offer_version(v["offer_id"], v["vendor_id"], fields, regs, stores, g.user)
        # 只標記舊版本的續約狀態與指向新版本，內容與日期完全不動（規則3、12）
        ex("UPDATE offer_versions SET renewal_status='已續約', renewed_to=? WHERE id=?", (nid, vid))
        log_review(vid, "已續約", g.user, f"建立 V{v['version_no']+1}")
        audit("優惠續約", "offer_version", vid, f'{v["vendor_name"]} {v["name"]}', changes={"renewal_status": [v["renewal_status"], "已續約"]})
        audit("建立新版本", "offer_version", nid, f'{v["vendor_name"]} {v["name"]} V{v["version_no"]+1}')
        flash(f"已建立新版本 V{v['version_no']+1}（草稿），請確認新期間與內容後送審。原版本內容未被更動。")
        return redirect(url_for("admin.offer_edit", vid=nid))
    return redirect(url_for("admin.offer_detail", vid=vid))


@bp.route("/offers/<int:vid>/renewal", methods=["POST"])
@perm_required("offer.edit")
def renewal_status(vid):
    v = get_version(vid)
    new = request.form.get("status")
    if new not in MANUAL_RENEWAL_STATES or v["renewal_status"] == "已續約":
        flash("無法變更續約狀態。")
    else:
        ex("UPDATE offer_versions SET renewal_status=? WHERE id=?", (new, vid))
        log_review(vid, f"續約狀態：{new}", g.user)
        audit("更新續約狀態", "offer_version", vid, f'{v["vendor_name"]} {v["name"]} V{v["version_no"]}',
              changes={"renewal_status": [v["renewal_status"], new]})
        flash("續約狀態已更新。")
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/admin") else url_for("admin.offer_detail", vid=vid))


# ---------------------------------------------------------------- 公告
KINDS_ANN = ["最新優惠公告", "新增廠商公告", "優惠續約公告", "優惠到期提醒", "職福活動公告"]


@bp.route("/announcements")
@perm_required("announce.manage")
def announcements():
    rows, pg = paginate("SELECT a.*, e.name creator FROM announcements a LEFT JOIN employees e ON e.id=a.created_by ORDER BY a.id DESC", [])
    return render_template("admin/announcements.html", rows=rows, pg=pg, today=T())


@bp.route("/announcements/new", methods=["GET", "POST"])
@bp.route("/announcements/<int:aid>", methods=["GET", "POST"])
@perm_required("announce.manage")
def announcement_form(aid=None):
    a = q("SELECT * FROM announcements WHERE id=?", (aid,), one=True) if aid else None
    if aid and not a:
        abort(404)
    errs = []
    if request.method == "POST":
        f = request.form
        d = dict(title=f.get("title", "").strip(), content=f.get("content", "").strip(), kind=f.get("kind", KINDS_ANN[0]),
                 publish_date=f.get("publish_date") or T(), end_date=f.get("end_date") or None,
                 is_pinned=1 if f.get("is_pinned") else 0, status=f.get("status", "draft"))
        if d["kind"] not in KINDS_ANN:
            d["kind"] = KINDS_ANN[0]
        if d["status"] not in ("draft", "published", "archived"):
            d["status"] = "draft"
        if not d["title"] or not d["content"]:
            errs.append("請填寫公告標題與內容。")
        if d["end_date"] and d["end_date"] < d["publish_date"]:
            errs.append("結束日期不得早於發布日期。")
        try:
            img = save_image(request.files.get("image"))
        except ValueError as e_:
            errs.append(str(e_))
            img = None
        if not errs:
            if img:
                d["image"] = img
            t = now()
            if a:
                ch = diff(dict(a), d)
                ex(f"UPDATE announcements SET {','.join(k+'=?' for k in d)}, updated_by=?, updated_at=? WHERE id=?",
                   (*d.values(), g.user["id"], t, aid))
                audit("公告發布" if ch.get("status", [0, ""])[1] == "published" else "修改公告", "announcement", aid, d["title"], changes=ch)
            else:
                cur = ex(f"INSERT INTO announcements({','.join(d)},created_by,created_at,updated_by,updated_at) "
                         f"VALUES({','.join('?'*len(d))},?,?,?,?)", (*d.values(), g.user["id"], t, g.user["id"], t))
                aid = cur.lastrowid
                audit("公告發布" if d["status"] == "published" else "新增公告", "announcement", aid, d["title"])
            flash("公告已儲存。")
            return redirect(url_for("admin.announcements"))
        a = {**(dict(a) if a else {}), **d}
    elif not a and request.args.get("from_version", "").isdigit():
        v = q(BASE_SELECT + " WHERE v.id=?", (int(request.args["from_version"]),), one=True)
        if v:
            y, m, d_ = v["end_date"].split("-")
            a = {"kind": "優惠續約公告", "title": f'{v["vendor_name"]}員工優惠續約囉！', "publish_date": T(), "status": "draft",
                 "content": f'「{v["name"]}」優惠已延續至 {int(y)} 年 {int(m)} 月 {int(d_)} 日。\n'
                            f'優惠期間：{v["start_date"].replace("-", "/")}～{v["end_date"].replace("-", "/")}\n'
                            f'優惠內容：{v["content"]}\n使用方式：{v["usage"] or "請洽職福單位"}'}
    return render_template("admin/announcement_form.html", a=a, errs=errs, KINDS=KINDS_ANN)


# ---------------------------------------------------------------- 統計
@bp.route("/stats")
@perm_required("stats.view")
def stats():
    rng = request.args.get("range", "month")
    t = today()
    start = {"month": iso(t.replace(day=1)), "30": iso(t - timedelta(days=30)), "year": iso(t.replace(month=1, day=1)), "all": "2000-01-01"}.get(rng)
    if not start:
        rng, start = "month", iso(t.replace(day=1))
    one = lambda sql: q(sql, (start,), one=True)["c"]
    tot = {"logins": one("SELECT COUNT(DISTINCT emp_id) c FROM usage_events WHERE kind='login' AND at>=?"),
           "login_n": one("SELECT COUNT(*) c FROM usage_events WHERE kind='login' AND at>=?"),
           "views": one("SELECT COUNT(*) c FROM usage_events WHERE kind='view' AND at>=?"),
           "searches": one("SELECT COUNT(*) c FROM usage_events WHERE kind='search' AND at>=?")}
    top_offers = q("SELECT v.id, v.name, ven.name vn, COUNT(*) n FROM usage_events u JOIN offer_versions v ON v.id=u.version_id JOIN offers o ON o.id=v.offer_id "
                   "JOIN vendors ven ON ven.id=o.vendor_id WHERE u.kind='view' AND u.at>=? GROUP BY v.id, ven.name ORDER BY n DESC LIMIT 10", (start,))
    top_vendors = q("SELECT ven.name, COUNT(*) n FROM usage_events u JOIN vendors ven ON ven.id=u.vendor_id WHERE u.kind='view' AND u.at>=? "
                    "GROUP BY ven.id ORDER BY n DESC LIMIT 10", (start,))
    top_cats = q("SELECT c.name, COUNT(*) n FROM usage_events u JOIN categories c ON c.id=u.category_id WHERE u.kind IN ('view','search') AND u.at>=? "
                 "GROUP BY c.id ORDER BY n DESC LIMIT 8", (start,))
    top_regs = q("SELECT r.name, COUNT(*) n FROM usage_events u JOIN regions r ON r.id=u.region_id WHERE u.kind IN ('view','search') AND u.at>=? "
                 "GROUP BY r.id ORDER BY n DESC LIMIT 8", (start,))
    combos = q("SELECT r.name rn, c.name cn, COUNT(*) n FROM usage_events u JOIN regions r ON r.id=u.region_id JOIN categories c ON c.id=u.category_id "
               "WHERE u.kind='search' AND u.at>=? GROUP BY r.name, c.name ORDER BY n DESC LIMIT 8", (start,))
    kws = q("SELECT keyword, COUNT(*) n FROM usage_events WHERE kind='search' AND keyword IS NOT NULL AND at>=? GROUP BY keyword ORDER BY n DESC LIMIT 10", (start,))
    return render_template("admin/stats.html", rng=rng, tot=tot, top_offers=top_offers, top_vendors=top_vendors, top_cats=top_cats,
                           top_regs=top_regs, combos=combos, kws=kws)
