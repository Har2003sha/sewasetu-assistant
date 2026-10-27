# -*- coding: utf-8 -*-
"""
Assistant channel for Sewa Setu.

The two MCP servers (mcp/) never touch the database. They verify the OAuth token, then call
this internal API with a service key and the person the token belongs to. Everything that
decides *what is allowed* lives here, next to the portal's own rules:

  * applications are created only through portal.submit_application() (same validation,
    same one-active-application rule, same table as the web form);
  * officer decisions use the same UPDATE ... WHERE status='PENDING' and the same audit
    format as the web dashboard, with the officer's own name as the actor.

/internal/* is for the docker network only (the reverse proxy blocks it).
"""
import os, re, json, hmac, secrets
from datetime import datetime, timedelta
from difflib import get_close_matches
from functools import wraps

from flask import Blueprint, request, jsonify, abort, render_template_string

import app as portal

api = Blueprint("api", __name__)
INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "")
PUBLIC = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
GENDERS, MARITAL = ["Male", "Female", "Other"], ["Married", "Unmarried", "Widowed"]
FIELDS = ["applicant_name", "dob", "gender", "marital_status", "husband_name", "village",
          "block", "bank_account", "ifsc"]
DRAFT_TTL_H, UPLOAD_TTL_MIN = 24, 30
DECLARATION = ("I hereby declare that the information furnished above is true to the best of my "
               "knowledge and belief. I understand that furnishing false information is punishable "
               "under applicable law and will result in cancellation of pension.")
HARD_FLAGS = ("underage_at_submission", "duplicate_active_application")


# ---------------------------------------------------------------- schema
def ensure_schema():
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(7001)")          # 4 gunicorn workers start together
    cur.execute("""
    CREATE TABLE IF NOT EXISTS assistant_drafts (
        mobile VARCHAR(15) PRIMARY KEY, data JSONB DEFAULT '{}', doc_path VARCHAR(200) DEFAULT '',
        upload_token VARCHAR(64), upload_expires TIMESTAMP, updated_at TIMESTAMP);
    CREATE TABLE IF NOT EXISTS eval_results (
        id SERIAL PRIMARY KEY, run_id VARCHAR(40), kind VARCHAR(12), persona JSONB, transcript JSONB,
        checks JSONB, passed BOOLEAN, model VARCHAR(60), turns INTEGER, created_at TIMESTAMP);
    CREATE TABLE IF NOT EXISTS eval_notes (
        id SERIAL PRIMARY KEY, run_id VARCHAR(40), found TEXT, changed TEXT, created_at TIMESTAMP);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_app_no ON applications(application_no);
    CREATE INDEX IF NOT EXISTS idx_app_name ON applications(lower(applicant_name) text_pattern_ops);
    """)
    cur.execute("SELECT pg_advisory_unlock(7001)")
    conn.commit(); cur.close(); conn.close()


def internal(f):
    @wraps(f)
    def w(*a, **k):
        got = request.headers.get("X-Internal-Key", "")
        if not INTERNAL_KEY or not hmac.compare_digest(got, INTERNAL_KEY):
            abort(403)
        return f(*a, **k)
    return w


def _body():
    return request.get_json(silent=True) or {}


def _mobile():
    m = str(_body().get("mobile", ""))
    if not (len(m) == 10 and m.isdigit()):
        abort(400)
    return m


def _mask(acct):
    return ("*" * max(0, len(acct) - 4) + acct[-4:]) if acct else ""


def _sla(submitted_at, status):
    """Days left before the nightly job deems a PENDING application approved."""
    if status != "PENDING" or not submitted_at:
        return {}
    due = submitted_at + timedelta(days=portal.SLA_DAYS)
    return {"days_waiting": (datetime.now() - submitted_at).days,
            "auto_approves_on": due.date().isoformat(),
            "days_left_before_auto_approval": max(0, (due - datetime.now()).days)}


# ---------------------------------------------------------------- input normalising
def _pick(val, options):
    v = (val or "").strip()
    for o in options:
        if v.lower() == o.lower():
            return o
    m = get_close_matches(v.lower(), [o.lower() for o in options], n=1, cutoff=0.75)
    return next((o for o in options if m and o.lower() == m[0]), v)


def _dob(raw):
    raw = (raw or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y",
                "%B %d %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return raw          # validate_application() will say it must be DD/MM/YYYY


def _normalise(f):
    out = {}
    for k in FIELDS:
        if f.get(k) is None:
            continue
        v = str(f[k]).strip()
        if k == "dob": v = _dob(v)
        elif k == "gender": v = _pick(v, GENDERS)
        elif k == "marital_status": v = _pick(v, MARITAL)
        elif k == "block": v = _pick(v, portal.BLOCKS)
        elif k == "ifsc": v = re.sub(r"[\s-]", "", v).upper()
        elif k == "bank_account": v = re.sub(r"[\s-]", "", v)
        out[k] = v
    return out


# ---------------------------------------------------------------- scheme facts
@api.get("/internal/v1/scheme")
@internal
def scheme():
    left = portal.deadline_remaining()
    return jsonify(scheme="Old Age Pension, Government of Purvanchal (Dept. of Social Welfare)",
                   amount_rupees_per_month=250, minimum_age=portal.MIN_AGE,
                   window_open=left is not None, hours_left=left,
                   window_closes=portal.SCHEME_DEADLINE.strftime("%d %B %Y, %I:%M %p IST"),
                   blocks=portal.BLOCKS, decision_days=portal.SLA_DAYS,
                   rules=["Applicant is %d or older" % portal.MIN_AGE,
                          "An age-proof document (JPG, PNG or PDF, up to 5 MB) is uploaded",
                          "Only one live application per mobile number",
                          "Not decided in %d days = approved automatically by law" % portal.SLA_DAYS],
                   declaration=DECLARATION)


# ---------------------------------------------------------------- citizen
def _load_draft(cur, mobile):
    cur.execute("SELECT data, doc_path, updated_at FROM assistant_drafts WHERE mobile=%s", (mobile,))
    r = cur.fetchone()
    if r and r[2] and r[2] > datetime.now() - timedelta(hours=DRAFT_TTL_H):
        return dict(r[0] or {}), r[1] or ""
    return {}, ""


def _draft_view(cur, mobile, data, doc):
    errors, _ = portal.validate_application(data, doc)
    needs_doc = any("age proof" in e for e in errors)
    problems = [e for e in errors if "age proof" not in e]
    need = [k for k in FIELDS if not data.get(k) and k != "husband_name"]   # late husband's name is optional on the web form too
    # fields the portal reports as problems are not "missing", they are wrong
    shown = dict(data)
    if shown.get("bank_account"): shown["bank_account"] = _mask(shown["bank_account"])
    act = portal.active_application(cur, mobile)
    return {"saved": shown, "still_needed": need, "problems": problems, "document_uploaded": bool(doc),
            "needs_document": needs_doc and not doc, "ready_to_submit": not errors,
            "already_has_application": ({"application_no": act[1], "status": act[2]} if act else None)}


@api.post("/internal/v1/citizen/draft")
@internal
def citizen_draft():
    mobile, fields = _mobile(), _normalise(_body().get("fields") or {})
    conn = portal.get_db(); cur = conn.cursor()
    data, doc = _load_draft(cur, mobile)
    data.update({k: v for k, v in fields.items()})
    cur.execute("""INSERT INTO assistant_drafts (mobile, data, doc_path, updated_at) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (mobile) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at""",
                (mobile, json.dumps(data), doc, datetime.now()))
    conn.commit()
    out = _draft_view(cur, mobile, data, doc)
    cur.close(); conn.close()
    return jsonify(out)


@api.post("/internal/v1/citizen/upload-link")
@internal
def citizen_upload_link():
    mobile = _mobile()
    tok = secrets.token_urlsafe(24)
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO assistant_drafts (mobile, upload_token, upload_expires, updated_at)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (mobile) DO UPDATE SET upload_token=EXCLUDED.upload_token,
                   upload_expires=EXCLUDED.upload_expires, updated_at=EXCLUDED.updated_at""",
                (mobile, tok, datetime.now() + timedelta(minutes=UPLOAD_TTL_MIN), datetime.now()))
    conn.commit(); cur.close(); conn.close()
    return jsonify(url="%s/assist/upload/%s" % (PUBLIC, tok), valid_minutes=UPLOAD_TTL_MIN)


UPLOAD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Upload age proof - Sewa Setu</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:24px;background:#f4f6fb;color:#12233f;font-size:20px}
main{max-width:520px;margin:auto;background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px #0002}
h1{font-size:26px;margin-top:0}input[type=file]{width:100%;font-size:18px;margin:18px 0}
button{width:100%;font-size:22px;padding:18px;border:0;border-radius:12px;background:#1a3c6e;color:#fff}
.m{padding:12px;border-radius:8px;margin:12px 0}.e{background:#fde8e8;color:#8a1c1c}.o{background:#e3f6e8;color:#14532d}</style></head>
<body><main><h1>Upload your age proof</h1>
{% if done %}<div class="m o"><b>Done.</b> Your document was received. Go back to the chat and tell the assistant you have uploaded it.</div>
{% else %}{% if err %}<div class="m e">{{ err }}</div>{% endif %}
<p>Take a clear photo of the document (for example Aadhaar, school certificate or birth certificate) showing your date of birth.
JPG, PNG or PDF, up to 5 MB.</p>
<form method="post" enctype="multipart/form-data"><input type="file" name="document" accept="image/*,application/pdf" required>
<button type="submit">Send document</button></form>{% endif %}
</main></body></html>"""


def _sniff_ok(ext, b):
    return (ext == "pdf" and b[:4] == b"%PDF") or (ext == "png" and b[:8] == b"\x89PNG\r\n\x1a\n") or \
           (ext in ("jpg", "jpeg") and b[:3] == b"\xff\xd8\xff")


@api.route("/assist/upload/<token>", methods=["GET", "POST"])
def assist_upload(token):
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT mobile FROM assistant_drafts WHERE upload_token=%s AND upload_expires>%s",
                (token, datetime.now()))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return render_template_string(UPLOAD_PAGE, err="This link has expired or was already used. "
                                      "Ask the assistant for a new upload link.", done=False), 410
    mobile = row[0]
    if request.method == "GET":
        cur.close(); conn.close()
        return render_template_string(UPLOAD_PAGE, err=None, done=False)
    f = request.files.get("document")
    name = (f.filename if f else "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    content = f.read() if f else b""
    err = None
    if ext not in ("pdf", "jpg", "jpeg", "png") or not _sniff_ok(ext, content):
        err = "Please choose a JPG, PNG or PDF file."
    elif len(content) > 5 * 1024 * 1024:
        err = "File is larger than 5 MB. Please choose a smaller photo."
    if err:
        cur.close(); conn.close()
        return render_template_string(UPLOAD_PAGE, err=err, done=False), 400
    os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(portal.UPLOAD_DIR, "%s_%s.%s" % (mobile, secrets.token_hex(4), ext))
    with open(path, "wb") as out:
        out.write(content)
    cur.execute("UPDATE assistant_drafts SET doc_path=%s, upload_token=NULL, updated_at=%s WHERE mobile=%s",
                (path, datetime.now(), mobile))
    conn.commit(); cur.close(); conn.close()
    return render_template_string(UPLOAD_PAGE, err=None, done=True)


@api.post("/internal/v1/citizen/submit")
@internal
def citizen_submit():
    mobile = _mobile()
    if _body().get("declaration_accepted") is not True:
        return jsonify(ok=False, errors=["the citizen has not accepted the declaration"], declaration=DECLARATION), 400
    conn = portal.get_db(); cur = conn.cursor()
    data, doc = _load_draft(cur, mobile)
    cur.close(); conn.close()
    try:
        new_id, app_no = portal.submit_application(mobile, data, doc, "AI assistant")
    except portal.SubmitRefused as e:
        return jsonify(ok=False, errors=e.errors,
                       existing=({"application_no": e.existing[1], "status": e.existing[2]} if e.existing else None)), 409
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM assistant_drafts WHERE mobile=%s", (mobile,))
    conn.commit(); cur.close(); conn.close()
    portal.send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal with "
                            "mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return jsonify(ok=True, application_no=app_no, status="PENDING",
                   decision_within_days=portal.SLA_DAYS,
                   track="SMS sent to the registered mobile. Status portal: %s/status (password is the date of birth as DDMMYYYY)." % PUBLIC)


@api.post("/internal/v1/citizen/status")
@internal
def citizen_status():
    mobile = _mobile()
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT application_no, status, submitted_at, decided_at, decided_by, block, village FROM applications "
                "WHERE mobile=%s ORDER BY submitted_at DESC LIMIT 5", (mobile,))
    apps = []
    for no, st, sub, dec, by, block, vil in cur.fetchall():
        a = {"application_no": no, "status": st, "submitted_on": sub.date().isoformat() if sub else None,
             "block": block, "village": vil}
        if dec: a["decided_on"] = dec.date().isoformat(); a["decided_by"] = "system (15-day rule)" if by == "RTPS-AUTO" else ("applicant" if by == "APPLICANT" else "block officer")
        a.update(_sla(sub, st)); apps.append(a)
    data, doc = _load_draft(cur, mobile)
    cur.close(); conn.close()
    return jsonify(applications=apps, draft_in_progress=bool(data),
                   draft_fields_saved=[k for k in data if data[k]], document_uploaded=bool(doc))


@api.post("/internal/v1/citizen/withdraw")
@internal
def citizen_withdraw():
    mobile, no = _mobile(), str(_body().get("application_no", "")).strip().upper()
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("UPDATE applications SET status='WITHDRAWN', decided_at=%s, decided_by='APPLICANT' "
                "WHERE application_no=%s AND mobile=%s AND status='PENDING' RETURNING id",
                (datetime.now(), no, mobile))
    row = cur.fetchone()
    if row:
        portal.write_audit(cur, row[0], "WITHDRAW", "applicant:%s" % mobile, "withdrawn via AI assistant")
        conn.commit()
    else:
        cur.execute("SELECT status FROM applications WHERE application_no=%s AND mobile=%s", (no, mobile))
        r = cur.fetchone()
    cur.close(); conn.close()
    if row:
        return jsonify(ok=True, application_no=no, status="WITHDRAWN", note="You can apply again with the same mobile number.")
    return jsonify(ok=False, error=("No application %s found on this mobile number." % no) if not r else
                   "Only a PENDING application can be withdrawn; this one is %s." % r[0]), 409


@api.post("/internal/v1/citizen/signout")
@internal
def citizen_signout():
    mobile = _mobile()
    import oauth
    n = oauth._revoke_grants("citizen:" + mobile)
    cleared = False
    if _body().get("clear_draft", True):
        conn = portal.get_db(); cur = conn.cursor()
        cur.execute("DELETE FROM assistant_drafts WHERE mobile=%s", (mobile,))
        conn.commit(); cur.close(); conn.close(); cleared = True
    return jsonify(ok=True, sign_ins_ended=n, draft_cleared=cleared)


# ---------------------------------------------------------------- officer
def _flags(cur, row):
    """Things a careful counter clerk would notice. Hard flags block approval unless acknowledged."""
    (app_id, no, name, mobile, dob, status, sub, bank, ifsc, doc) = row
    fl = []
    if dob and sub and portal.age_on(dob, sub.date()) < portal.MIN_AGE:
        fl.append({"flag": "underage_at_submission", "detail": "aged %d when submitted; minimum is %d" % (portal.age_on(dob, sub.date()), portal.MIN_AGE)})
    cur.execute("SELECT application_no, status FROM applications WHERE mobile=%s AND id<>%s AND status IN %s",
                (mobile, app_id, portal.ACTIVE_STATUSES))
    o = cur.fetchall()
    if o: fl.append({"flag": "duplicate_active_application", "detail": "same mobile also has " + ", ".join("%s (%s)" % x for x in o)})
    if bank:
        cur.execute("SELECT application_no, applicant_name FROM applications WHERE bank_account=%s AND id<>%s AND status IN %s LIMIT 5",
                    (bank, app_id, portal.ACTIVE_STATUSES))
        o = cur.fetchall()
        if o: fl.append({"flag": "bank_account_shared", "detail": "same account on " + ", ".join("%s (%s)" % (a, n) for a, n in o)})
    if not doc: fl.append({"flag": "no_age_proof_on_file", "detail": "no document stored (older records have none)"})
    if not ifsc or not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc): fl.append({"flag": "ifsc_missing_or_invalid", "detail": ifsc or "blank"})
    if sub and sub > portal.SCHEME_DEADLINE: fl.append({"flag": "submitted_after_deadline", "detail": sub.isoformat()})
    return fl


@api.post("/internal/v1/officer/list")
@internal
def officer_list():
    b = _body()
    status = (b.get("status") or "PENDING").upper()
    q = (b.get("query") or "").strip()
    block = _pick(b.get("block"), portal.BLOCKS) if b.get("block") else None
    limit = max(1, min(int(b.get("limit") or 15), 25))
    where, args = [], []
    if status != "ALL": where.append("status=%s"); args.append(status)
    if block: where.append("block=%s"); args.append(block)
    if q:
        if re.fullmatch(r"\d{10}", q): where.append("mobile=%s"); args.append(q)
        elif re.fullmatch(r"SSP\d+", q.upper()): where.append("application_no=%s"); args.append(q.upper())
        else: where.append("lower(applicant_name) LIKE %s"); args.append(q.lower() + "%")
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, village, block, status, submitted_at, dob FROM applications"
                + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY submitted_at ASC LIMIT %s", args + [limit])
    rows = [dict(application_no=r[0], name=r[1], village=r[2], block=r[3], status=r[4],
                 submitted_on=r[5].date().isoformat() if r[5] else None,
                 age=portal.age_on(r[6], datetime.now().date()) if r[6] else None, **_sla(r[5], r[4]))
            for r in cur.fetchall()]
    cur.execute("SELECT count(*), count(*) FILTER (WHERE submitted_at < %s) FROM applications WHERE status='PENDING'"
                + (" AND block=%s" if block else ""),
                [datetime.now() - timedelta(days=portal.SLA_DAYS - 3)] + ([block] if block else []))
    tot, soon = cur.fetchone()
    cur.close(); conn.close()
    return jsonify(pending_total=tot, auto_approving_within_3_days=soon, shown=len(rows), applications=rows)


@api.post("/internal/v1/officer/application")
@internal
def officer_application():
    no = str(_body().get("application_no", "")).strip().upper()
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, dob, status, submitted_at, bank_account, ifsc, doc_path, "
                "gender, marital_status, husband_name, village, block, decided_at, decided_by FROM applications WHERE application_no=%s", (no,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close(); return jsonify(error="No application %s." % no), 404
    flags = _flags(cur, r[:10])
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id=%s ORDER BY at", (r[0],))
    audit = [{"at": a.strftime("%Y-%m-%d %H:%M"), "action": x, "actor": y, "note": z} for a, x, y, z in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(application_no=r[1], name=r[2], mobile=r[3], dob=r[4].isoformat() if r[4] else None,
                   age_now=portal.age_on(r[4], datetime.now().date()) if r[4] else None, gender=r[10],
                   marital_status=r[11], late_husband_name=r[12] or None, village=r[13], block=r[14],
                   bank_account_masked=_mask(r[7]), ifsc=r[8], age_proof_on_file=bool(r[9]), status=r[5],
                   submitted_on=r[6].date().isoformat() if r[6] else None,
                   decided_on=r[15].date().isoformat() if r[15] else None, decided_by=r[16], flags=flags,
                   audit_trail=audit, **_sla(r[6], r[5]))


@api.post("/internal/v1/officer/decide")
@internal
def officer_decide():
    b = _body()
    officer = re.sub(r"[^A-Za-z0-9_.-]", "", str(b.get("officer", "")))[:50]
    no = str(b.get("application_no", "")).strip().upper()
    decision = str(b.get("decision", "")).lower()
    reason = " ".join(str(b.get("reason", "")).split())[:400]
    if not officer: return jsonify(ok=False, error="no officer identity on the token"), 403
    if decision not in ("approve", "reject"): return jsonify(ok=False, error="decision must be 'approve' or 'reject'"), 400
    if len(reason) < 8: return jsonify(ok=False, error="A reason of at least a short sentence is required; it is recorded against the officer's name."), 400
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, dob, status, submitted_at, bank_account, ifsc, doc_path "
                "FROM applications WHERE application_no=%s", (no,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close(); return jsonify(ok=False, error="No application %s." % no), 404
    if decision == "approve" and not b.get("acknowledge_flags"):
        hard = [f for f in _flags(cur, r) if f["flag"] in HARD_FLAGS]
        if hard:
            cur.close(); conn.close()
            return jsonify(ok=False, blocked_by=hard, error="Approval blocked: " + "; ".join(f["detail"] for f in hard) +
                           ". If the officer has checked and still wants to approve, repeat with acknowledge_flags=true and say why in the reason."), 409
    new = "APPROVED" if decision == "approve" else "REJECTED"
    cur.execute("UPDATE applications SET status=%s, decided_at=%s, decided_by=%s WHERE id=%s AND status='PENDING' RETURNING application_no",
                (new, datetime.now(), officer, r[0]))
    ok = cur.fetchone()
    if ok:
        portal.write_audit(cur, r[0], new, "admin:%s" % officer, reason + " [via AI assistant]")
        conn.commit()
    cur.close(); conn.close()
    if not ok:
        return jsonify(ok=False, error="Application %s is %s, not PENDING; it cannot be decided now." % (no, r[5])), 409
    return jsonify(ok=True, application_no=no, status=new, decided_by=officer, reason_recorded=reason)


# ---------------------------------------------------------------- health + eval dashboard
@api.get("/healthz")
def healthz():
    conn = portal.get_db(); cur = conn.cursor(); cur.execute("SELECT 1"); cur.close(); conn.close()
    return "ok"


DASH = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sewa Setu assistant - evals</title><style>
:root{--ink:#14213d;--mut:#5b6678;--ok:#14532d;--okb:#e3f6e8;--bad:#8a1c1c;--badb:#fde8e8;--line:#d9deea}
body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f7fb;color:var(--ink)}main{max-width:980px;margin:auto;padding:24px}
h1{margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px}.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 18px}.stat b{font-size:28px;display:block}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0}
.card h3{margin:0}.meta{color:var(--mut);font-size:14px}.pill{padding:2px 10px;border-radius:99px;font-size:13px;font-weight:600}
.p{background:var(--okb);color:var(--ok)}.f{background:var(--badb);color:var(--bad)}ul{margin:8px 0;padding-left:20px}
details{margin-top:8px}summary{cursor:pointer;color:#1a3c6e}.t{font-size:14px;background:#f3f5fa;border-radius:8px;padding:8px 12px;margin:4px 0;white-space:pre-wrap}
.u{border-left:4px solid #9fb3d9}.a{border-left:4px solid #6bbf8a}.k{border-left:4px solid #e0a84f;font-family:ui-monospace,monospace;font-size:13px}
.note{background:#fffbea;border:1px solid #f0e2a1;border-radius:12px;padding:12px 16px;margin:10px 0}</style></head><body><main>
<h1>Sewa Setu assistant: evals</h1>
<p class="sub">Invented citizens and officers, played against the two MCP servers. After each conversation the portal database is read to check what is actually there.</p>
{% if not runs %}<div class="card">No eval runs recorded yet.</div>{% endif %}
{% for kind, s in summary.items() %}<div class="stats"><div class="stat"><b>{{ s.pct }}%</b>{{ kind_label[kind] }} pass rate</div>
<div class="stat"><b>{{ s.passed }}/{{ s.total }}</b>conversations passed</div><div class="stat"><b>{{ s.checks }}</b>database checks</div></div>{% endfor %}
{% if notes %}<h2>What building the evals turned up, and what changed</h2>{% for n in notes %}<div class="note"><b>Found:</b> {{ n.found }}<br><b>Changed:</b> {{ n.changed }}</div>{% endfor %}{% endif %}
<h2>Conversations</h2>
{% for r in rows %}<div class="card"><h3>{{ r.persona.name }}, {{ r.persona.age }}, {{ r.persona.village }}
 <span class="pill {{ 'p' if r.passed else 'f' }}">{{ 'passed' if r.passed else 'failed' }}</span></h3>
<div class="meta">{{ kind_label[r.kind] }} · {{ r.persona.role }} · {{ r.model or 'scripted client' }} · {{ r.turns }} tool calls · run {{ r.run_id }}</div>
<p><b>Who:</b> {{ r.persona.who }}<br><b>Wants:</b> {{ r.persona.goal }}<br><b>Why this person:</b> {{ r.persona.why }}</p>
<b>Checked in the database afterwards</b><ul>{% for c in r.checks %}<li>{{ '✓' if c.ok else '✗' }} {{ c.what }}{% if not c.ok %}, got {{ c.got }}{% endif %}</li>{% endfor %}</ul>
<details><summary>Conversation</summary>{% for m in r.transcript %}<div class="t {{ m.who }}">{{ m.text }}</div>{% endfor %}</details></div>{% endfor %}
</main></body></html>"""


@api.get("/eval-dashboard")
def eval_dashboard():
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("SELECT run_id, kind, persona, transcript, checks, passed, model, turns FROM eval_results e WHERE run_id = "
                "(SELECT run_id FROM eval_results WHERE kind = e.kind ORDER BY created_at DESC LIMIT 1) ORDER BY kind DESC, id")
    rows = [dict(zip(("run_id", "kind", "persona", "transcript", "checks", "passed", "model", "turns"), r)) for r in cur.fetchall()]
    cur.execute("SELECT found, changed FROM eval_notes ORDER BY id")
    notes = [{"found": a, "changed": b} for a, b in cur.fetchall()]
    cur.close(); conn.close()
    latest = {r["kind"]: r["run_id"] for r in rows}
    summary = {}
    for k in latest:
        rs = [r for r in rows if r["kind"] == k]; p = sum(1 for r in rs if r["passed"])
        summary[k] = type("S", (), dict(total=len(rs), passed=p, pct=round(100 * p / len(rs)), checks=sum(len(r["checks"]) for r in rs)))
    return render_template_string(DASH, rows=rows, runs=bool(rows), summary=summary, notes=notes,
                                  kind_label={"llm": "LLM-played", "scripted": "Scripted protocol"})
