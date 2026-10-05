"""建立示範資料：python seed_demo.py（僅供試用，正式環境請勿執行）。"""
import os
from datetime import timedelta

from werkzeug.security import generate_password_hash

import db
from db import now, today, iso

db.init_db()
c = db.connect()
t = now()
d = today()
if c.execute("SELECT 1 FROM vendors").fetchone():
    print("已有廠商資料，略過。")
    raise SystemExit
reg = {r["name"]: r["id"] for r in c.execute("SELECT * FROM regions")}
cat = {r["name"]: r["id"] for r in c.execute("SELECT * FROM categories")}
pw = generate_password_hash("Demo12345")
for no, name, role, rg in (("H0001", "林職福", 2, "台北地區"), ("E0001", "王小明", 1, "桃竹苗"), ("E0002", "陳大華", 1, "高雄")):
    c.execute("INSERT INTO employees(emp_no,name,dept,title,region_id,hire_date,status,active,role_id,password_hash,must_change_pw,created_at,updated_at)"
              " VALUES(?,?,?,?,?,?,?,1,?,?,0,?,?)", (no, name, "營運部", "專員", reg[rg], "2023-01-01", "在職", role, pw, t, t))
vendors = [("V0001", "好食光餐廳", "好食光", "桃竹苗", "桃園市中壢區中正路100號"), ("V0002", "城市旅宿", "城市旅宿", "台北地區", "台北市信義區松仁路8號"),
           ("V0003", "活力健身房", "活力", "台中", "台中市西區公益路50號"), ("V0004", "線上書城", "書城", "線上優惠", "")]
for code, n, b, r, a in vendors:
    c.execute("INSERT INTO vendors(code,name,brand,region_id,address,phone,website,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
              (code, n, b, reg[r], a, "03-1234567", "https://example.com", t, t))
c.execute("INSERT INTO stores(code,vendor_id,name,region_id,address,phone,status,created_at,updated_at) VALUES('S0001',1,'中壢店',?,'桃園市中壢區中正路100號','03-1234567','active',?,?)", (reg["桃竹苗"], t, t))
c.execute("INSERT INTO stores(code,vendor_id,name,region_id,address,phone,status,created_at,updated_at) VALUES('S0002',1,'桃園店',?,'桃園市桃園區復興路20號','03-7654321','active',?,?)", (reg["桃竹苗"], t, t))
c.execute("INSERT INTO stores(code,vendor_id,name,region_id,address,status,created_at,updated_at) VALUES('S0003',2,'信義館',?,'台北市信義區松仁路8號','active',?,?)", (reg["台北地區"], t, t))
c.execute("INSERT INTO offers(code,vendor_id,created_by,created_at) VALUES('O0001',1,1,?)", (t,))
c.execute("INSERT INTO offers(code,vendor_id,created_by,created_at) VALUES('O0002',2,1,?)", (t,))
c.execute("INSERT INTO offers(code,vendor_id,created_by,created_at) VALUES('O0003',3,1,?)", (t,))
c.execute("INSERT INTO offers(code,vendor_id,created_by,created_at) VALUES('O0004',4,1,?)", (t,))
c.execute("INSERT INTO offers(code,vendor_id,created_by,created_at) VALUES('O0005',1,1,?)", (t,))


def ver(oid, no, name, cname, content, start, end, status, regions, stores=(), usage="消費時出示公司員工識別證。", notes="不得與其他優惠併用。", renewal="待聯繫"):
    cur = c.execute("INSERT INTO offer_versions(offer_id,version_no,name,category_id,content,usage,notes,start_date,end_date,mgmt_status,renewal_status,"
                    "created_by,created_at,updated_by,updated_at,published_by,published_at,reviewed_by,reviewed_at,views) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,1,?,1,?,1,?,?)",
                    (oid, no, name, cat[cname], content, usage, notes, iso(start), iso(end), status, renewal, t, t, t, t, no * 3))
    vid = cur.lastrowid
    for r in regions:
        c.execute("INSERT INTO version_regions VALUES(?,?)", (vid, reg[r]))
    for s in stores:
        c.execute("INSERT INTO version_stores VALUES(?,?)", (vid, s))
    c.execute("INSERT INTO review_logs(version_id,action,actor_name,at) VALUES(?,?,?,?)", (vid, "示範資料", "系統", t))


y = timedelta(days=365)
ver(1, 1, "平日消費享9折", "食", "平日內用享9折，每桌限一次。", d - 2 * y, d - y - timedelta(days=1), "published", ["桃竹苗"], [1, 2], renewal="已續約")
ver(1, 2, "平日消費享9折", "食", "平日內用享9折，每桌限一次。", d - y, d - timedelta(days=100), "published", ["桃竹苗"], [1, 2], renewal="已續約")
ver(1, 3, "平日消費享85折", "食", "平日內用享85折，每桌限一次。", d - timedelta(days=99), d + timedelta(days=265), "published", ["桃竹苗"], [1, 2])
ver(2, 1, "住宿每晚折 300 元", "住", "官網訂房輸入員工代碼，每晚折抵 300 元。", d - timedelta(days=60), d + timedelta(days=20), "published", ["台北地區", "全區"], [3], renewal="洽談中")
ver(3, 1, "月費 8 折", "健康", "加入一年會員享月費 8 折，免入會費。", d - timedelta(days=200), d - timedelta(days=5), "published", ["台中"], renewal="待確認")
ver(4, 1, "電子書 85 折", "育", "全站電子書 85 折，結帳輸入員工序號。", d - timedelta(days=10), d + timedelta(days=355), "published", ["線上優惠", "全區"])
ver(5, 1, "週末套餐加贈甜點", "食", "週末套餐加贈招牌甜點一份。", d, d + timedelta(days=364), "pending", ["桃竹苗"], [1])
c.execute("INSERT INTO announcements(title,content,kind,publish_date,is_pinned,status,created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?,1,'published',1,?,1,?)",
          ("歡迎使用員工職福好康站", "專屬員工的生活優惠，一站查詢。有任何建議歡迎聯絡職福單位。", "職福活動公告", iso(d), t, t))
c.commit()
print("示範資料完成。帳號：admin／Admin1234（首次登入須改密碼）、H0001／Demo12345（HR）、E0001／Demo12345（員工）")
