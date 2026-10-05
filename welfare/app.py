"""員工職福好康站 — 應用程式入口。"""
import os
import secrets
import threading
import time
from datetime import datetime

from flask import Flask, g, render_template, request, session, redirect, url_for

import db
from db import q, ex, now, today
from security import csrf_token, check_csrf, user_perms
import lifecycle


def _secret():
    if os.environ.get("WELFARE_SECRET"):
        return os.environ["WELFARE_SECRET"]
    p = os.path.join(db.BASE_DIR, "data", "secret.key")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(p, 0o600)
    return open(p).read().strip()


def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("WELFARE_HTTPS", "0") == "1",  # 上線時請放在 HTTPS 後並設為 1
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    )
    db.init_db()
    app.teardown_appcontext(db.close_db)

    from views_front import bp as front
    from views_admin import bp as admin
    app.register_blueprint(front)
    app.register_blueprint(admin, url_prefix="/admin")

    @app.before_request
    def load_user():
        g.user = None
        g.perms = set()
        if request.endpoint == "static":
            return
        check_csrf()
        uid = session.get("uid")
        if uid:
            u = q("SELECT * FROM employees WHERE id=?", (uid,), one=True)
            idle = db.setting_int("session_idle_minutes") * 60
            ok = u is not None and u["active"] and time.time() - session.get("last", 0) <= idle
            if ok:
                from views_front import can_login_status
                ok = can_login_status(u) is None
            if ok:
                g.user = u
                g.perms = user_perms(u)
                session["last"] = time.time()
            else:
                expired = u is not None and time.time() - session.get("last", 0) > idle
                session.clear()
                if expired:
                    session["flash_msg"] = "閒置時間過長，已自動登出，請重新登入。"

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        if g.get("user"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        return dict(csrf=csrf_token, perms=g.get("perms", set()), me=g.get("user"),
                    site_name=db.setting("site_name"), now_str=db.now(), today_str=db.iso(db.today()), today=db.iso(db.today()), ds=lifecycle.display_status,
                    days_left=lifecycle.days_left, MGMT_LABEL=lifecycle.MGMT_LABEL,
                    is_url=lambda s: bool(s) and s.lower().startswith(("http://", "https://")))

    def friendly(code, title, msg):
        def h(_e):
            return render_template("error.html", code=code, title=title, msg=msg), code
        return h
    app.register_error_handler(400, friendly(400, "請求無法處理", "頁面已逾時或資料不完整，請重新整理頁面後再試一次。"))
    app.register_error_handler(403, friendly(403, "權限不足", "您沒有權限使用此功能。如有需要，請洽 HR 或系統管理員。"))
    app.register_error_handler(404, friendly(404, "找不到頁面", "您要查看的內容不存在，或已不再開放。"))
    app.register_error_handler(413, friendly(413, "檔案太大", "上傳的檔案過大，請縮小後重新上傳。"))
    app.register_error_handler(500, friendly(500, "系統暫時無法使用", "系統發生問題，已通知管理人員。請稍後再試。"))

    @app.errorhandler(Exception)
    def unhandled(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        app.logger.exception("unhandled error")
        try:
            db.get_db().rollback()
        except Exception:
            pass
        return render_template("error.html", code=500, title="系統暫時無法使用",
                               msg="系統發生問題，請稍後再試；若持續發生請聯絡 HR。"), 500

    # 每日自動下架：啟動時先跑一次，之後每小時檢查（冪等）
    def run_expire():
        with app.app_context():
            lifecycle.expire_job()
            db.get_db().commit()
            from views_admin_helpers import make_backup, list_backups
            stamp = db.datetime.now(db.TZ).strftime("%Y%m%d")
            if not any(b["name"].startswith(f"welfare-{stamp}") and b["name"].endswith("-auto.db") for b in list_backups()):
                make_backup("auto")

    run_expire()

    def loop():
        while True:
            time.sleep(3600)
            try:
                run_expire()
            except Exception:
                app.logger.exception("expire job failed")
    if os.environ.get("WELFARE_NO_THREAD") != "1":
        threading.Thread(target=loop, daemon=True).start()

    @app.teardown_request
    def commit(exc):
        conn = g.get("db")
        if conn is not None and exc is None:
            conn.commit()

    return app


app = create_app() if __name__ != "__main__" else None

if __name__ == "__main__":
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
