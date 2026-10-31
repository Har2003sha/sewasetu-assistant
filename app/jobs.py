# -*- coding: utf-8 -*-
"""Nightly deemed-approval job, run inside the web process (no cron container needed).

Under the RTPS Act an application not decided within SLA_DAYS stands approved. Runs once at start-up (so a freshly
restored database is brought up to date) and then every day at 02:00 server time. With several gunicorn workers a
file lock makes sure only one of them runs the scheduler."""
import os, time, threading
from datetime import datetime, timedelta
import db

_keep = []


def deemed_approval(sla_days, send_sms):
    conn = db.connect(); cur = conn.cursor()
    now = datetime.now()
    cur.execute("""UPDATE applications SET status='DEEMED_APPROVED', decided_at=%s, decided_by='RTPS-AUTO'
                   WHERE status='PENDING' AND submitted_at < %s RETURNING id, application_no, mobile""",
                (now, now - timedelta(days=sla_days)))
    rows = cur.fetchall()
    for app_id, _, _ in rows:
        cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) VALUES (%s,'DEEMED_APPROVED','system:rtps-cron','not processed within SLA',%s)", (app_id, now))
    conn.commit(); conn.close()
    for _, no, mobile in rows:
        try:
            send_sms(mobile, "Sewa Setu: your pension application %s stands approved under the RTPS Act." % no)
        except Exception:
            pass
    print("%s deemed approval: %d applications approved" % (now.strftime("%Y-%m-%d %H:%M:%S"), len(rows)), flush=True)
    return len(rows)


def start_scheduler(sla_days, send_sms):
    try:
        import fcntl
        f = open("/tmp/sewasetu-scheduler.lock", "w"); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); _keep.append(f)
    except ImportError:
        pass                                   # not on Linux (local dev): single process anyway
    except OSError:
        return                                 # another worker already runs it

    def loop():
        time.sleep(5)
        while True:
            try:
                deemed_approval(sla_days, send_sms)
            except Exception as e:
                print("deemed approval failed:", e, flush=True)
            n = datetime.now(); nxt = n.replace(hour=2, minute=0, second=0, microsecond=0)
            time.sleep(max(60, ((nxt if nxt > n else nxt + timedelta(days=1)) - n).total_seconds()))
    threading.Thread(target=loop, daemon=True, name="deemed-approval").start()