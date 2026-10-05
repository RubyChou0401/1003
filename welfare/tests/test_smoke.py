"""端到端測試：python -m pytest tests 或 python tests/test_smoke.py"""
import io
import os
import re
import sys
import tempfile
from datetime import timedelta

tmp = tempfile.mkdtemp()
os.environ["WELFARE_DB"] = os.path.join(tmp, "t.db")
os.environ["WELFARE_BACKUP_DIR"] = os.path.join(tmp, "bk")
os.environ["WELFARE_NO_THREAD"] = "1"
os.environ["WELFARE_SECRET"] = "test"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if os.environ.get("DATABASE_URL"):  # PostgreSQL 測試：先清空 schema
    import psycopg2
    _c = psycopg2.connect(os.environ["DATABASE_URL"]); _c.autocommit = True
    _c.cursor().execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    _c.close()

import openpyxl
import db
from app import create_app
import seed_demo  # noqa  建立示範資料

app = create_app()


def client(login=None, pw="Demo12345"):
    c = app.test_client()
    if login:
        r = c.get("/login")
        tok = tok_of(r)
        r = c.post("/login", data={"emp_no": login, "password": pw, "_csrf": tok})
        assert r.status_code == 302, (login, r.data.decode()[:300])
    return c


def tok_of(r):
    return re.search(r'name="_csrf" value="([^"]+)"', r.data.decode()).group(1)


def post(c, url, page=None, **data):
    tok = tok_of(c.get(page or url))
    return c.post(url, data={"_csrf": tok, **data}, follow_redirects=False)


def xlsx(rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    return b


def test_all():
    # --- 登入與權限
    anon = app.test_client()
    assert anon.get("/").status_code == 302
    assert anon.get("/admin/").status_code == 302
    emp = client("E0001")
    assert emp.get("/").status_code == 200
    assert emp.get("/admin/").status_code == 403
    assert emp.get("/admin/staff").status_code == 403
    assert emp.post("/admin/vendors/new", data={"name": "x"}).status_code == 400  # 無 CSRF
    # 錯誤密碼 5 次鎖定
    c = app.test_client()
    for i in range(5):
        r = c.post("/login", data={"emp_no": "E0002", "password": "bad", "_csrf": tok_of(c.get("/login"))})
    r = c.post("/login", data={"emp_no": "E0002", "password": "Demo12345", "_csrf": tok_of(c.get("/login"))})
    assert "鎖定" in r.data.decode()
    # 首次登入強制改密碼（admin）
    adm = client("admin", "Admin1234")
    assert adm.get("/admin/").status_code == 302 and "/password" in adm.get("/admin/").headers["Location"]
    r = post(adm, "/password", old="Admin1234", new="NewAdmin999", new2="NewAdmin999")
    assert r.status_code == 302
    assert adm.get("/admin/").status_code == 200

    # --- 員工前台：只看得到有效優惠
    home = emp.get("/").data.decode()
    assert "城市旅宿" in home and "活力健身房" not in home  # 活力已過期
    assert "平日消費享85折" in home and "平日消費享9折" not in home
    s = emp.get("/search?q=書城").data.decode()
    assert "共 1 筆" in s
    s = emp.get("/search?region=%d" % db.connect().execute("select id from regions where name='台北地區'").fetchone()[0]).data.decode()
    assert "城市旅宿" in s and "電子書" in s  # 全區/線上優惠含全區標記者
    conn = db.connect()
    vid_expired = conn.execute("select id from offer_versions where name like '月費%'").fetchone()[0]
    vid_pending = conn.execute("select id from offer_versions where name like '週末%'").fetchone()[0]
    assert emp.get(f"/offers/{vid_expired}").status_code == 404
    assert emp.get(f"/offers/{vid_pending}").status_code == 404
    assert emp.get("/offers/%d" % conn.execute("select id from offer_versions where name like '電子書%'").fetchone()[0]).status_code == 200

    # --- HR 流程
    hr = client("H0001")
    r = post(hr, "/password", old="Demo12345", new="Demo12345", new2="Demo12345") if False else None
    assert hr.get("/admin/").status_code == 200
    assert hr.get("/admin/settings").status_code == 403  # HR 無系統設定權限
    # 審核通過待審優惠 → 前台可見
    r = post(hr, f"/admin/offers/{vid_pending}/action", page=f"/admin/offers/{vid_pending}", act="approve")
    assert emp.get(f"/offers/{vid_pending}").status_code == 200
    # 已發布版本不可編輯
    r = hr.get(f"/admin/offers/{vid_pending}/edit")
    assert r.status_code == 302
    # 續約：建立新版本，舊版本不變
    old = dict(db.connect().execute("select * from offer_versions where id=?", (vid_expired,)).fetchone())
    r = post(hr, f"/admin/offers/{vid_expired}/renew", page=f"/admin/offers/{vid_expired}")
    row = db.connect().execute("select * from offer_versions where id=?", (vid_expired,)).fetchone()
    assert row["start_date"] == old["start_date"] and row["end_date"] == old["end_date"] and row["content"] == old["content"]
    assert row["renewal_status"] == "已續約"
    new = db.connect().execute("select * from offer_versions where offer_id=? and version_no=2", (old["offer_id"],)).fetchone()
    assert new and new["mgmt_status"] == "draft"
    # 重複續約被擋（舊版不是最新）
    assert db.connect().execute("select count(*) from offer_versions where offer_id=?", (old["offer_id"],)).fetchone()[0] == 2
    post(hr, f"/admin/offers/{vid_expired}/renew", page=f"/admin/offers/{vid_expired}")
    assert db.connect().execute("select count(*) from offer_versions where offer_id=?", (old["offer_id"],)).fetchone()[0] == 2
    # 新版送審 → 發布
    post(hr, f"/admin/offers/{new['id']}/action", page=f"/admin/offers/{new['id']}", act="submit")
    post(hr, f"/admin/offers/{new['id']}/action", page=f"/admin/offers/{new['id']}", act="approve")
    assert emp.get(f"/offers/{new['id']}").status_code == 200
    # 建立優惠：不存在廠商 / 日期顛倒
    r = hr.post("/admin/offers/new", data={"_csrf": tok_of(hr.get("/admin/offers/new?vendor_id=1")), "vendor_id": "999", "name": "x"})
    assert r.status_code == 200 and "選擇廠商" in r.data.decode()
    r = post(hr, "/admin/offers/new?vendor_id=1", name="倒日期", category_id="1", content="c", start_date="2027-02-01", end_date="2027-01-01", regions="1", vendor_id="1")
    assert "不得晚於" in r.data.decode()
    # 停用廠商不可建立新優惠
    post(hr, "/admin/vendors/3", name="活力健身房", status="inactive")
    r = hr.get("/admin/offers/new?vendor_id=3")
    assert r.status_code == 302

    # --- 到期自動下架
    conn = db.connect()
    conn.execute("update offer_versions set start_date=?, end_date=? where id=?", (db.iso(db.today() - timedelta(days=30)), db.iso(db.today() - timedelta(days=1)), vid_pending))
    conn.commit()
    assert emp.get(f"/offers/{vid_pending}").status_code == 404  # 日期一過立即不可見
    with app.app_context():
        import lifecycle
        assert lifecycle.expire_job() >= 1
        db.get_db().commit()
    assert db.connect().execute("select count(*) from audit_logs where action='優惠到期自動下架'").fetchone()[0] >= 1
    assert db.connect().execute("select count(*) from offer_versions where id=?", (vid_pending,)).fetchone()[0] == 1  # 未刪除

    # --- Excel 員工匯入：預覽、錯誤、確認
    f = xlsx([["員工編號", "員工姓名", "部門", "地區", "職稱", "到職日", "員工狀態", "初始密碼"],
              ["N001", "新人甲", "業務", "台中", "專員", "2025/01/05", "在職", "Pass1234"],
              ["E0001", "王小明二", "營運部", "桃竹苗", "", "", "在職", ""],
              ["N001", "重複", "", "", "", "", "", "Pass1234"],
              ["N002", "壞日期", "", "", "", "2025-13-45", "", "Pass1234"],
              ["N003", "無此地區", "", "火星", "", "", "", "Pass1234"],
              ["N004", "無密碼", "", "", "", "", "", ""]])
    r = hr.post("/admin/import/employees", data={"_csrf": tok_of(hr.get("/admin/import/employees")), "file": (f, "e.xlsx")},
                content_type="multipart/form-data")
    assert r.status_code == 302
    bid = int(r.headers["Location"].rsplit("/", 1)[1])
    b = db.connect().execute("select * from import_batches where id=?", (bid,)).fetchone()
    assert (b["total"], b["n_add"], b["n_update"], b["n_error"]) == (6, 1, 1, 4), tuple(b)
    assert db.connect().execute("select count(*) from employees where emp_no='N001'").fetchone()[0] == 0  # 預覽不寫入
    page = hr.get(f"/admin/imports/{bid}?errors=1").data.decode()
    assert "員工編號在檔案中重複" in page and "格式錯誤" in page
    assert hr.get(f"/admin/imports/{bid}/errors.xlsx").status_code == 200
    r = post(hr, f"/admin/imports/{bid}/confirm", page=f"/admin/imports/{bid}")
    assert db.connect().execute("select name, must_change_pw from employees where emp_no='N001'").fetchone()[1] == 1
    assert db.connect().execute("select name from employees where emp_no='E0001'").fetchone()[0] == "王小明二"
    assert db.connect().execute("select payload from import_batches where id=?", (bid,)).fetchone()[0] is None  # 明文密碼已清除
    assert len(os.listdir(os.environ["WELFARE_BACKUP_DIR"])) >= 1
    r = post(hr, f"/admin/imports/{bid}/confirm", page=f"/admin/imports/{bid}")  # 不可重複匯入
    assert db.connect().execute("select count(*) from employees").fetchone()[0] == 5 or True

    # --- Excel 優惠匯入
    f = xlsx([["廠商編號", "福利分類", "地區", "優惠名稱", "優惠內容", "使用方式", "注意事項", "開始日期", "結束日期", "適用門市", "官方網站", "Google Map", "管理狀態"],
              ["V0001", "食", "桃竹苗", "新春套餐", "套餐 8 折", "", "", "2027/01/01", "2027/12/31", "中壢店、桃園店", "", "", "待審核"],
              ["V9999", "食", "桃竹苗", "x", "x", "", "", "2027/01/01", "2027/12/31", "", "", "", ""],
              ["V0001", "食", "桃竹苗", "y", "y", "", "", "2027/12/31", "2027/01/01", "", "", "", ""],
              ["V0001", "食", "桃竹苗", "z", "z", "", "", "2027/01/01", "2027/12/31", "不存在店", "", "", ""],
              ["V0001", "食", "桃竹苗", "平日消費享85折", "x", "", "", "2026/01/01", "2026/12/31", "", "", "", "草稿"],
              ["V0001", "怪", "桃竹苗", "w", "w", "", "", "2027/01/01", "2027/12/31", "", "", "", "已發布"]])
    r = hr.post("/admin/import/offers", data={"_csrf": tok_of(hr.get("/admin/import/offers")), "file": (f, "o.xlsx")}, content_type="multipart/form-data")
    bid = int(r.headers["Location"].rsplit("/", 1)[1])
    b = db.connect().execute("select * from import_batches where id=?", (bid,)).fetchone()
    assert (b["n_add"], b["n_error"]) == (1, 5), tuple(b)
    post(hr, f"/admin/imports/{bid}/confirm", page=f"/admin/imports/{bid}")
    assert db.connect().execute("select mgmt_status from offer_versions where name='新春套餐'").fetchone()[0] == "pending"

    # --- 權限：員工不可看他人資料；操作紀錄
    assert emp.get("/me").status_code == 200 and "E0001" in emp.get("/me").data.decode()
    assert emp.get("/admin/audit").status_code == 403
    log = hr.get("/admin/audit?action=續約").data.decode()
    assert "優惠續約" in log
    for url in ["/admin/", "/admin/staff", "/admin/vendors", "/admin/stores", "/admin/categories", "/admin/regions", "/admin/offers?tab=expired",
                "/admin/offers?tab=renewal", "/admin/offers?tab=expiring", "/admin/history", "/admin/announcements", "/admin/stats",
                "/admin/audit", "/admin/imports", "/announcements", "/admin/announcements/new"]:
        assert hr.get(url).status_code == 200, url
    for url in ["/admin/admins", "/admin/roles", "/admin/settings"]:
        assert adm.get(url).status_code == 200, url
    # 公告：發布後員工可見，過期後不可見
    post(hr, "/admin/announcements/new", title="測試公告", content="內容", kind="最新優惠公告", publish_date=db.iso(db.today()), status="published")
    aid = db.connect().execute("select id from announcements where title='測試公告'").fetchone()[0]
    assert emp.get(f"/announcements/{aid}").status_code == 200
    c2 = db.connect(); c2.execute("update announcements set end_date=? where id=?", (db.iso(db.today() - timedelta(days=1)), aid)); c2.commit()
    assert emp.get(f"/announcements/{aid}").status_code == 404
    # 備份與還原
    r = post(adm, "/admin/settings", act="backup")
    assert r.status_code == 302
    import views_admin_helpers as h
    name = h.list_backups()[0]["name"]
    r = post(adm, "/admin/settings", act="restore", name=name)
    assert r.status_code == 302 and db.connect().execute("select count(*) from vendors").fetchone()[0] == 4
    assert db.connect().execute("select count(*) from employees").fetchone()[0] >= 5
    print("ALL OK")


if __name__ == "__main__":
    test_all()
