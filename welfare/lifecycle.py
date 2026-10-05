"""優惠生命週期：顯示狀態由日期自動判斷、每日自動下架、查詢。"""
from datetime import timedelta

from db import q, ex, today, iso, now, audit, setting_int

MGMT_LABEL = {"draft": "草稿", "pending": "待審核", "published": "已發布", "disabled": "人工停用", "archived": "封存"}
RENEWAL_STATES = ["待聯繫", "洽談中", "待確認", "已續約", "不續約"]
MANUAL_RENEWAL_STATES = ["待聯繫", "洽談中", "待確認", "不續約"]  # 「已續約」只能由續約動作產生

BASE_SELECT = """
SELECT v.*, o.code AS offer_code, o.vendor_id, ven.name AS vendor_name, ven.brand AS vendor_brand,
       ven.logo AS vendor_logo, ven.status AS vendor_status, ven.code AS vendor_code, c.name AS cat_name,
       (SELECT group_concat(r.name, '、') FROM version_regions vr JOIN regions r ON r.id=vr.region_id
         WHERE vr.version_id=v.id) AS region_names
FROM offer_versions v JOIN offers o ON o.id=v.offer_id JOIN vendors ven ON ven.id=o.vendor_id
JOIN categories c ON c.id=v.category_id
"""


def display_status(row, ref=None):
    """有效／即將到期／已過期／未開始；非已發布者回傳管理狀態標籤。"""
    ref = ref or today()
    if row["mgmt_status"] != "published":
        return MGMT_LABEL[row["mgmt_status"]]
    t = iso(ref)
    if row["end_date"] < t:
        return "已過期"
    if row["start_date"] > t:
        return "未開始"
    if row["end_date"] <= iso(ref + timedelta(days=setting_int("expiring_days"))):
        return "即將到期"
    return "有效"


def days_left(row):
    from datetime import date
    return (date.fromisoformat(row["end_date"]) - today()).days


def visible_where():
    """員工前台可見條件：已發布、期間內、廠商未停用。已過期者絕不出現（規則1）。"""
    return ("v.mgmt_status='published' AND v.start_date<=:today AND v.end_date>=:today "
            "AND ven.status='active'")


def expire_job():
    """每日自動下架：把已過期且尚未處理的已發布優惠標記並寫入操作紀錄。可重複執行（冪等）。"""
    rows = q("SELECT v.id, v.name, v.end_date, o.code FROM offer_versions v JOIN offers o ON o.id=v.offer_id "
             "WHERE v.mgmt_status='published' AND v.end_date<? AND v.expired_processed_at IS NULL", (iso(today()),))
    for r in rows:
        ex("UPDATE offer_versions SET expired_processed_at=? WHERE id=?", (now(), r["id"]))
        ex("INSERT INTO review_logs(version_id,action,actor_name,note,at) VALUES(?,?,?,?,?)",
           (r["id"], "自動下架", "系統", f'已超過結束日 {r["end_date"]}', now()))
        audit("優惠到期自動下架", "offer_version", r["id"], f'{r["code"]} {r["name"]}',
              actor_name="系統", changes={"display_status": ["有效", "已過期"]})
    return len(rows)


def reminders():
    """後台 Dashboard 的自動提醒（第一階段：僅站內顯示）。"""
    t = today()
    out = []
    for r in q(BASE_SELECT + " WHERE v.mgmt_status='published' AND v.end_date BETWEEN ? AND ? ORDER BY v.end_date",
               (iso(t), iso(t + timedelta(days=30)))):
        d = days_left(r)
        if d <= 7:
            msg = "今天到期" if d == 0 else f"將於 {d} 天後到期"
            out.append(("danger", f'{r["vendor_name"]}「{r["name"]}」{msg}', r["id"]))
        else:
            out.append(("warn", f'{r["vendor_name"]}「{r["name"]}」優惠即將到期（{r["end_date"]}，剩 {d} 天）', r["id"]))
    for r in q(BASE_SELECT + " WHERE v.mgmt_status='published' AND v.end_date<? AND v.end_date>=? "
               "ORDER BY v.end_date DESC", (iso(t), iso(t - timedelta(days=7)))):
        out.append(("info", f'{r["vendor_name"]}「{r["name"]}」優惠已於 {r["end_date"]} 後自動下架', r["id"]))
    for r in q("SELECT v.id, v.name, ven.name AS vn, v.published_at FROM offer_versions v JOIN offers o ON o.id=v.offer_id "
               "JOIN vendors ven ON ven.id=o.vendor_id WHERE v.version_no>1 AND v.mgmt_status='published' "
               "AND v.published_at>=? ORDER BY v.published_at DESC", (iso(t - timedelta(days=7)),)):
        out.append(("ok", f'{r["vn"]}「{r["name"]}」優惠已重新上架', r["id"]))
    return out
