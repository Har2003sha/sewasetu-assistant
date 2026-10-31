# -*- coding: utf-8 -*-
"""SMS sending with a real-provider option.

SMS_PROVIDER=simulator (default)  the demo gateway: the OTP shows on /__gateway/, it is NOT sent to a real phone
SMS_PROVIDER=fast2sms             FAST2SMS_API_KEY
SMS_PROVIDER=msg91                MSG91_AUTHKEY, MSG91_TEMPLATE_ID  (OTP template approved on DLT)
SMS_PROVIDER=smshorizon           SMSH_API_KEY, SMSH_USER, SMSH_SENDERID, SMSH_TID, SMSH_TEMPLATE_TEXT  (see _smshorizon)
SMS_PROVIDER=twilio               TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM
SMS_FALLBACK_CONSOLE=1            if the real provider refuses (sandbox, no balance, bad template), also put the OTP on the
                                  demo console /__gateway/ so a tester can still sign in
SMS_COUNTRY_CODE defaults to 91. send() returns (ok, detail) and never raises.
"""
import os, re
import requests

PROVIDER = os.environ.get("SMS_PROVIDER", "simulator").lower()
FALLBACK = os.environ.get("SMS_FALLBACK_CONSOLE", "") == "1"
CC = os.environ.get("SMS_COUNTRY_CODE", "91")
GATEWAY = os.environ.get("SMS_GATEWAY_URL", "")      # empty = the built-in console (/__gateway/)
T = 8


def _ok(r, what):
    return (200 <= r.status_code < 300, "%s HTTP %s %s" % (what, r.status_code, r.text[:160]))


def _simulator(mobile, text, code):
    """Demo mode: the message is stored and shown on the portal's own console, /__gateway/, after a 1-4 s 'carrier'
    delay. It is NOT sent to a phone. (Set SMS_GATEWAY_URL to use an external gateway instead.)"""
    if GATEWAY:
        return _ok(requests.post(GATEWAY + "/api/send", json={"to": mobile, "text": text}, timeout=T), "gateway")
    import time, random, db
    c = db.connect(); cur = c.cursor(); now = time.time()
    cur.execute("INSERT INTO sms_outbox (to_mobile, text, sent_at, delivery_at) VALUES (%s,%s,%s,%s)", (mobile, text, now, now + random.randint(1, 4)))
    c.commit(); c.close()
    return True, "console"


def _fast2sms(mobile, text, code):
    h = {"authorization": os.environ["FAST2SMS_API_KEY"]}
    if code:
        r = requests.post("https://www.fast2sms.com/dev/bulkV2", headers=h, json={"route": "otp", "variables_values": code, "numbers": mobile}, timeout=T)
    else:
        r = requests.post("https://www.fast2sms.com/dev/bulkV2", headers=h, json={"route": "q", "message": text, "numbers": mobile}, timeout=T)
    ok, d = _ok(r, "fast2sms")
    return ok and r.json().get("return") is True, d


def _msg91(mobile, text, code):
    if not code:
        return False, "msg91: only OTP messages are configured"
    r = requests.post("https://control.msg91.com/api/v5/otp", params={"template_id": os.environ["MSG91_TEMPLATE_ID"], "mobile": CC + mobile,
                      "authkey": os.environ["MSG91_AUTHKEY"], "otp": code}, timeout=T)
    ok, d = _ok(r, "msg91")
    return ok and r.json().get("type") == "success", d


def _twilio(mobile, text, code):
    sid = os.environ["TWILIO_SID"]
    return _ok(requests.post("https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid, auth=(sid, os.environ["TWILIO_TOKEN"]),
               data={"From": os.environ["TWILIO_FROM"], "To": "+" + CC + mobile, "Body": text}, timeout=T), "twilio")


def _smshorizon(mobile, text, code):
    """SmsHorizon HTTP API (https://www.smshorizon.in/sms-api): POST /api/v2/sendsms.php, Bearer key.
    DLT rule: the message must be the APPROVED template text with only the variable replaced, so the text
    comes from SMSH_TEMPLATE_TEXT (use {otp} where the code goes), not from the portal's own wording."""
    if not code:
        return False, "smshorizon: only the OTP template is configured"
    tpl = os.environ.get("SMSH_TEMPLATE_TEXT", "Your OTP is {otp}")
    r = requests.post(os.environ.get("SMSH_URL", "https://smshorizon.co.in/api/v2/sendsms.php"),
                      headers={"Authorization": "Bearer " + os.environ["SMSH_API_KEY"]},
                      data={"user": os.environ["SMSH_USER"], "mobile": os.environ.get("SMSH_MOBILE_PREFIX", "") + mobile,
                            "senderid": os.environ.get("SMSH_SENDERID", "HORIZN"), "message": tpl.replace("{otp}", code),
                            "tid": os.environ.get("SMSH_TID", ""), "type": os.environ.get("SMSH_TYPE", "txt")}, timeout=T)
    try:
        j = r.json()
    except ValueError:
        j = {}
    ok = r.ok and not j.get("error") and (bool(j.get("msgid")) or str(j.get("status", "")).lower() in ("queued", "success", "sent", "submitted"))
    return ok, "smshorizon HTTP %s %s" % (r.status_code, r.text[:200])


_P = {"smshorizon": _smshorizon, "simulator": _simulator, "fast2sms": _fast2sms, "msg91": _msg91, "twilio": _twilio}


def demo_mode():
    """True when the OTP may be readable on the demo console (so the verify page shows the hint)."""
    return PROVIDER == "simulator" or FALLBACK


def send(mobile, text):
    m = re.search(r"OTP is (\d{6})", text or "")
    try:
        ok, detail = _P[PROVIDER](mobile, text, m.group(1) if m else None)
    except KeyError as e:
        ok, detail = False, "SMS provider '%s' is not configured correctly (missing %s)" % (PROVIDER, e)
    except Exception as e:
        ok, detail = False, "%s: %s" % (PROVIDER, e)
    if ok and PROVIDER != "simulator":
        import sys
        print("SMS accepted by %s for ...%s" % (PROVIDER, mobile[-4:]), file=sys.stderr, flush=True)
    if not ok and FALLBACK and PROVIDER != "simulator":
        try:
            ok2, d2 = _simulator(mobile, text, m.group(1) if m else None)
        except Exception as e:
            ok2, d2 = False, "console: %s" % e
        import sys
        print("SMS NOT DELIVERED to the phone (%s). OTP was put on the demo console instead: %s" % (detail, d2), file=sys.stderr, flush=True)
        return ok2, "real SMS failed (%s); console fallback: %s" % (detail, d2)
    return ok, detail