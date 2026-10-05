"""登入保護、CSRF、RBAC。"""
import secrets
from functools import wraps

from flask import abort, g, redirect, request, session, url_for, flash

from db import q


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    if request.method == "POST":
        tok = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not tok or not secrets.compare_digest(tok, session.get("csrf", "")):
            abort(400)


def user_perms(user):
    if not user:
        return set()
    return {r["perm"] for r in q("SELECT perm FROM role_permissions WHERE role_id=?", (user["role_id"],))}


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("front.login", next=request.full_path if request.method == "GET" else None))
        if g.user["must_change_pw"] and request.endpoint not in ("front.change_password", "front.logout"):
            return redirect(url_for("front.change_password"))
        return f(*a, **k)
    return w


def perm_required(*perms):
    """後台權限驗證（任一權限即可）。未登入導向登入，已登入但無權限回 403。"""
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user:
                return redirect(url_for("front.login", next=request.full_path if request.method == "GET" else None))
            if g.user["must_change_pw"]:
                return redirect(url_for("front.change_password"))
            if not (set(perms) & g.perms):
                abort(403)
            return f(*a, **k)
        return w
    return deco


def password_problem(pw):
    if len(pw) < 8:
        return "密碼至少需要 8 個字元。"
    if not (any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw)):
        return "密碼需同時包含英文字母與數字。"
    return None
