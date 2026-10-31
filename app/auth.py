# -*- coding: utf-8 -*-
"""Front door of the web portal: Register / Sign in with mobile OTP, or Continue with Google.

Everything on the website sits behind this page (see OPEN). The gate does not touch the assistant channel:
/oauth, /.well-known, /internal, /assist and the MCP endpoints keep their own authentication.
A signed-in person already has a verified mobile, so "Apply for Pension" no longer asks for a second OTP.
Google needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and the redirect URI  <PUBLIC_BASE_URL>/auth/google/callback
registered in Google Cloud Console. Google proves who the person is; their mobile is still verified once by OTP
(a pension application is tied to a mobile number) and remembered for next time.
"""
import os, secrets
from datetime import datetime
from urllib.parse import urlencode
import requests
from flask import Blueprint, render_template, request, session, redirect, url_for, flash

import app as portal

auth = Blueprint("auth", __name__)
OPEN = ("/welcome", "/auth/", "/static/", "/oauth/", "/.well-known/", "/internal/", "/assist/", "/healthz",
        "/eval-dashboard", "/admin", "/__gateway", "/status", "/favicon", "/citizen/", "/officer/")
GID, GSECRET = os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", "")
PUBLIC = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def gate():
    p = request.path
    if p.startswith(OPEN) or session.get("user") or session.get("logged_in"):
        return None
    return redirect(url_for("auth.welcome"))


def _login(mobile, name="", email=None, sub=None, via="otp"):
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO web_users (mobile, name, email, google_sub, created_at, last_login) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (mobile) DO UPDATE SET name = COALESCE(NULLIF(EXCLUDED.name,''), web_users.name),
                   email = COALESCE(EXCLUDED.email, web_users.email), google_sub = COALESCE(EXCLUDED.google_sub, web_users.google_sub),
                   last_login = EXCLUDED.last_login RETURNING name""",
                (mobile, name, email, sub, datetime.now(), datetime.now()))
    shown = cur.fetchone()[0] or mobile
    conn.commit(); cur.close(); conn.close()
    for k in ("auth_mobile", "auth_name", "auth_google"):
        session.pop(k, None)
    session.permanent = True
    session["user"], session["user_mobile"], session["user_via"] = shown, mobile, via
    session["verified_mobile"] = mobile


@auth.route("/welcome")
def welcome():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("welcome.html", google=bool(GID and GSECRET), captcha_q=portal.make_captcha())


@auth.route("/auth/otp", methods=["POST"])
def send_otp():
    mobile, name = request.form.get("mobile", "").strip(), " ".join(request.form.get("name", "").split())[:100]
    try:
        captcha_ok = int(request.form.get("captcha", "")) == session.get("captcha_answer")
    except ValueError:
        captcha_ok = False
    if not captcha_ok:
        flash("Security check answer is incorrect. Please try again."); return redirect(url_for("auth.welcome"))
    if len(mobile) != 10 or not mobile.isdigit():
        flash("Please enter a valid 10-digit mobile number."); return redirect(url_for("auth.welcome"))
    ok, msg = portal.issue_otp(mobile)
    if not ok:
        flash(msg); return redirect(url_for("auth.welcome"))
    session["auth_mobile"], session["auth_name"] = mobile, name
    return redirect(url_for("auth.verify"))


@auth.route("/auth/mobile", methods=["GET"])
def link_mobile():      # after Google: ask for the mobile number once
    if not session.get("auth_google"):
        return redirect(url_for("auth.welcome"))
    return render_template("welcome.html", google=False, captcha_q=portal.make_captcha(), link=session["auth_google"])


@auth.route("/auth/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("auth_mobile")
    if not mobile:
        return redirect(url_for("auth.welcome"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = portal.get_db(); cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile=%s ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone(); cur.close(); conn.close()
        if row and row[0] == code:
            if (datetime.now() - row[1]).total_seconds() > portal.OTP_VALIDITY_SECONDS:
                flash("OTP expired. Please request a new OTP."); return redirect(url_for("auth.welcome"))
            g = session.get("auth_google") or {}
            _login(mobile, session.get("auth_name") or g.get("name", ""), g.get("email"), g.get("sub"), "google" if g else "otp")
            return redirect(url_for("index"))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile, auth=True, demo=__import__("sms").demo_mode())


@auth.route("/auth/google")
def google():
    if not (GID and GSECRET):
        flash("Google sign-in is not set up on this server yet. Please register with your mobile number."); return redirect(url_for("auth.welcome"))
    session["g_state"] = secrets.token_urlsafe(16)
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": GID, "redirect_uri": PUBLIC + "/auth/google/callback", "response_type": "code", "scope": "openid email profile",
        "state": session["g_state"], "prompt": "select_account"}))


@auth.route("/auth/google/callback")
def google_callback():
    if request.args.get("error") or not request.args.get("state") or request.args.get("state") != session.pop("g_state", None):
        flash("Google sign-in was cancelled or could not be verified. Please try again."); return redirect(url_for("auth.welcome"))
    try:
        t = requests.post("https://oauth2.googleapis.com/token", data={"code": request.args.get("code"), "client_id": GID, "client_secret": GSECRET,
                          "redirect_uri": PUBLIC + "/auth/google/callback", "grant_type": "authorization_code"}, timeout=10).json()
        u = requests.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": "Bearer " + t["access_token"]}, timeout=10).json()
        assert u.get("email_verified") and u.get("sub")
    except Exception:
        flash("Google sign-in failed. Please try again or register with your mobile number."); return redirect(url_for("auth.welcome"))
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT mobile, name FROM web_users WHERE google_sub=%s AND mobile IS NOT NULL", (u["sub"],))
    row = cur.fetchone(); cur.close(); conn.close()
    if row:                                    # this Google account already has a verified mobile
        _login(row[0], u.get("name", ""), u.get("email"), u["sub"], "google"); return redirect(url_for("index"))
    session["auth_google"] = {"sub": u["sub"], "email": u.get("email"), "name": u.get("name", "")}
    return redirect(url_for("auth.link_mobile"))


@auth.route("/auth/logout")
def logout():
    session.clear()
    flash("You have been signed out.")
    return redirect(url_for("auth.welcome"))