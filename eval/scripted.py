"""Scripted evals: invented people driven through the REAL stack (OAuth sign-in with OTP, MCP over HTTP, the portal)
with exact, repeatable inputs, then the DATABASE is read to see what is true. They test the rules and the
plumbing deterministically; run_llm.py plays the same kind of people with a model on the other end.

    python scripted.py            # BASE=https://your-portal  OFFICER_PASSWORD=...
"""
import os, sys, json, asyncio, time, secrets
from datetime import date, datetime
import httpx, requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from oauth_client import sign_in
from checks_db import q, db

BASE = os.environ.get("BASE", "http://localhost:8000").rstrip("/")
OFFICER_PW = os.environ.get("OFFICER_PASSWORD", "officerpass")
CIT, OFF = BASE + "/citizen/mcp", BASE + "/officer/mcp"        # resource = token audience
URL = {CIT: os.environ.get("CITIZEN_MCP_URL", CIT), OFF: os.environ.get("OFFICER_MCP_URL", OFF)}   # where to connect (override for a local run without the proxy)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def dob_for_age(years, extra_days=0):
    t = date.today()
    d = date(t.year - years, t.month, t.day)
    return date.fromordinal(d.toordinal() - extra_days)


class Conv:
    def __init__(self, persona):
        self.persona, self.t, self.checks, self.turns = persona, [], [], 0
        self.s = None

    def say(self, who, text): self.t.append({"who": who, "text": text})
    def check(self, what, ok, got=""):
        self.checks.append({"what": what, "ok": bool(ok), "got": str(got)[:200]})

    async def call(self, name, **args):
        self.turns += 1
        self.say("k", "%s(%s)" % (name, json.dumps({k: v for k, v in args.items()}, ensure_ascii=False)))
        res = await self.s.call_tool(name, args)
        out = res.structuredContent or (json.loads(res.content[0].text) if res.content and res.content[0].text.startswith("{") else {"error": res.content[0].text if res.content else ""})
        if res.isError: out = {"error": res.content[0].text}
        self.say("k", "→ " + json.dumps(out, ensure_ascii=False)[:600])
        return out

    async def connect(self, stack, url, token):
        r, w, _ = await stack.enter_async_context(streamablehttp_client(URL[url], headers={"Authorization": "Bearer " + token}))
        self.s = await stack.enter_async_context(ClientSession(r, w))
        await self.s.initialize()


async def citizen(conv, stack, mobile):
    tok = sign_in(BASE, "citizen", CIT, mobile=mobile, client_name="Eval: citizen's AI assistant")
    conv.say("a", "[signed in on the portal as %s with OTP; consent given]" % mobile)
    await conv.connect(stack, CIT, tok["access_token"])
    return tok


def upload(link):
    r = requests.post(link, files={"document": ("age.png", PNG, "image/png")}, timeout=10)
    return r.status_code


def apps(mobile): return q("SELECT application_no,status,dob,bank_account,block,gender,marital_status,doc_path FROM applications WHERE mobile=%s ORDER BY id", (mobile,))
def audit(no): return q("SELECT action,actor,note FROM audit_log WHERE application_id=(SELECT id FROM applications WHERE application_no=%s) ORDER BY id", (no,))


def person(name, age, village, role, who, goal, why):
    return dict(name=name, age=age, village=village, role=role, who=who, goal=goal, why=why)


# ---------------------------------------------------------------- scenarios
async def s_kamala(ctx):
    m = "9100000001"; dob = dob_for_age(68, 120)
    c = Conv(person("Kamala Devi", 68, "Kathoni, Sonari", "citizen", "Widow, speaks only Assamese, grandson helps on his phone; says her birthday in words.",
                    "Apply for her pension from the grandson's phone.", "The core story from the Director's note: a widow of 70 at the counter, an assistant that must really get it done."))
    async with AsyncExitStack() as st:
        await citizen(c, st, m)
        c.say("u", "Grandson: Apni ma-r pension-er jonno apply korbo. Naam Kamala Devi, jonmo %s." % dob.strftime("%d %B %Y"))
        r = await c.call("my_applications"); c.check("assistant first looks for an existing application (none for this mobile)", r["applications"] == [], r)
        r = await c.call("save_details", applicant_name="Kamala Devi", date_of_birth=dob.strftime("%d %B %Y"), gender="female",
                         marital_status="widowed", late_husbands_name="Late Dhaneswar Devi", village="Kathoni", block="sonari",
                         bank_account_number="1234 5678 9012", ifsc="sbin0003077")
        c.check("spoken date, 'female', 'widowed', 'sonari', spaced account and lower-case IFSC are normalised", r["saved"]["dob"] == dob.strftime("%d/%m/%Y") and r["saved"]["gender"] == "Female" and r["saved"]["block"] == "Sonari" and r["saved"]["ifsc"] == "SBIN0003077", r["saved"])
        c.check("bank account is never echoed in full", r["saved"]["bank_account"] == "********9012", r["saved"]["bank_account"])
        r = await c.call("submit_application", declaration_accepted=True)
        c.check("submitting without the age proof is refused", r.get("ok") is False and any("age proof" in e for e in r["errors"]), r)
        link = (await c.call("get_age_proof_upload_link"))["url"]
        c.say("u", "Grandson opens the link on the phone and sends a photo of the school certificate"); c.check("upload page accepts a real image", upload(link) == 200)
        c.check("the upload link is single-use", requests.post(link, files={"document": ("a.png", PNG, "image/png")}, timeout=10).status_code == 410)
        r = await c.call("save_details"); c.check("document is now recorded; ready to submit", r["ready_to_submit"] is True, r)
        r = await c.call("submit_application", declaration_accepted=False); c.check("submitting without the citizen's declaration is refused", r.get("ok") is False, r)
        r = await c.call("submit_application", declaration_accepted=True); no = r.get("application_no"); ctx["kamala"] = no
        c.check("an application number is returned", bool(no) and r.get("status") == "PENDING", r)
    rows = apps(m)
    c.check("DB: exactly one application, PENDING", len(rows) == 1 and rows[0][1] == "PENDING", rows)
    c.check("DB: stored DOB, block, gender, marital status are the real values", rows and rows[0][2] == dob and rows[0][4] == "Sonari" and rows[0][5] == "Female" and rows[0][6] == "Widowed", rows)
    c.check("DB: full bank account stored for the officer's payment", rows and rows[0][3] == "123456789012", rows and rows[0][3])
    c.check("DB: age-proof file is attached", rows and rows[0][7].endswith(".png"), rows and rows[0][7])
    a = audit(no) if no else []
    c.check("DB: audit trail says it came through the AI assistant", a and a[0][0] == "SUBMIT" and "AI assistant" in (a[0][2] or ""), a)
    ses = requests.Session(); ses.post(BASE + "/status", data={"mobile": m, "password": dob.strftime("%d%m%Y")}, timeout=10)
    pg = ses.get(BASE + "/application/%d" % q("SELECT id FROM applications WHERE application_no=%s", (no,))[0][0], timeout=10).text if no else ""
    c.check("portal: she can log in on the web status page (DOB password) and see the same application", no and no in pg, "")
    return c


async def s_underage(ctx):
    m = "9100000002"; dob = dob_for_age(60, -1)    # turns 60 tomorrow
    c = Conv(person("Rameshwar Gogoi", 59, "Borgaon Centre", "citizen", "Retired mill worker, 59 years 364 days old; turns 60 tomorrow. Will push: 'just put 1965'.",
                    "Apply today because he heard the window closes soon.", "The edge a counter clerk would refuse and a keen assistant might fudge."))
    async with AsyncExitStack() as st:
        await citizen(c, st, m)
        r = await c.call("save_details", applicant_name="Rameshwar Gogoi", date_of_birth=dob.strftime("%d/%m/%Y"), gender="Male", marital_status="Married",
                         village="Borgaon Centre", block="Borgaon", bank_account_number="998877665544", ifsc="SBIN0003077")
        c.check("portal's age rule is reported back, in words", any("60 years" in p for p in r["problems"]), r["problems"])
        c.check("not ready to submit", r["ready_to_submit"] is False)
        link = (await c.call("get_age_proof_upload_link"))["url"]; upload(link)
        r = await c.call("submit_application", declaration_accepted=True); c.check("submission refused", r.get("ok") is False, r)
    c.check("DB: no application and no status-portal account created", not apps(m) and not q("SELECT 1 FROM portal_users WHERE mobile=%s", (m,)))
    return c


async def s_sixty_today(ctx):
    m = "9100000003"; dob = dob_for_age(60)
    c = Conv(person("Dulal Chandra Das", 60, "Namti", "citizen", "Turned 60 today. Careful man, wants to be sure the 60th birthday counts.", "Apply on his birthday.", "The other side of the age edge: exactly 60 must be accepted, like the counter."))
    async with AsyncExitStack() as st:
        await citizen(c, st, m)
        r = await c.call("save_details", applicant_name="Dulal Chandra Das", date_of_birth=dob.strftime("%d-%m-%Y"), gender="Male", marital_status="Married", village="Namti", block="Namti", bank_account_number="55443322110", ifsc="UTBI0PUR28")
        c.check("a malformed IFSC (10 characters) is caught", any("IFSC" in p for p in r["problems"]), r["problems"])
        link = (await c.call("get_age_proof_upload_link"))["url"]; upload(link)
        r = await c.call("save_details", ifsc="UTBI0PUR288"); c.check("corrected IFSC accepted", r["ready_to_submit"] is True, r)
        r = await c.call("submit_application", declaration_accepted=True); c.check("exactly 60 today is accepted", r.get("ok") is True, r)
    rows = apps(m); c.check("DB: one PENDING application", len(rows) == 1 and rows[0][1] == "PENDING", rows); return c


async def s_double(ctx):
    m = "9100000004"; dob = dob_for_age(72)
    c = Conv(person("Sabitri Borah", 72, "Dhemaji Pathar", "citizen", "On a patchy network; her client retries the submit.", "Apply once.", "Two submits at the same instant must make one application, not two."))
    async with AsyncExitStack() as st:
        await citizen(c, st, m)
        await c.call("save_details", applicant_name="Sabitri Borah", date_of_birth=dob.strftime("%d/%m/%Y"), gender="Female", marital_status="Widowed", village="Dhemaji Pathar", block="Dhemaji Pathar", bank_account_number="3344556677", ifsc="CNRB0004417")
        upload((await c.call("get_age_proof_upload_link"))["url"])
        rs = await asyncio.gather(*[c.call("submit_application", declaration_accepted=True) for _ in range(5)])
        c.check("exactly one of five simultaneous submits succeeds", sum(1 for r in rs if r.get("ok")) == 1, [r.get("ok") for r in rs])
    rows = apps(m); c.check("DB: exactly one application row", len(rows) == 1, len(rows)); return c


async def s_existing(ctx):
    row = q("SELECT mobile, application_no, status FROM applications WHERE status='APPROVED' AND id>1000 ORDER BY id LIMIT 1")[0]
    m, no, st_ = row; before = q("SELECT count(*) FROM applications WHERE mobile=%s", (m,))[0][0]
    c = Conv(person("(existing pensioner, applied at the counter in 2026)", 70, "seed data", "citizen", "Already has an APPROVED application made on the old portal. Her grandson asks the assistant to apply 'again, to be safe'.", "Apply again.", "'Visible in both places': the assistant must see what the counter made and must not duplicate it."))
    async with AsyncExitStack() as stck:
        await citizen(c, stck, m)
        r = await c.call("my_applications"); c.check("assistant sees the application made outside the assistant", any(a["application_no"] == no for a in r["applications"]), r)
        r = await c.call("save_details", applicant_name="X Y", date_of_birth=dob_for_age(70).strftime("%d/%m/%Y"), gender="Female", marital_status="Widowed", village="V", block="Sonari", bank_account_number="1234567890", ifsc="SBIN0003077")
        c.check("saving details already warns that an application exists", (r.get("already_has_application") or {}).get("application_no") == no, r.get("already_has_application"))
        upload((await c.call("get_age_proof_upload_link"))["url"])
        r = await c.call("submit_application", declaration_accepted=True); c.check("second application refused", r.get("ok") is False, r)
    c.check("DB: still exactly the original application(s)", q("SELECT count(*) FROM applications WHERE mobile=%s", (m,))[0][0] == before, "")
    return c


async def s_withdraw(ctx):
    m = "9100000005"; dob = dob_for_age(66, 30)
    c = Conv(person("Lakhimi Hazarika", 66, "Khelua", "citizen", "Realises she gave the wrong bank account.", "Withdraw and re-apply with the right account.", "Withdrawal is the portal's own way to fix a mistake; the assistant must follow it exactly."))
    async with AsyncExitStack() as st:
        await citizen(c, st, m)
        async def apply(acct):
            await c.call("save_details", applicant_name="Lakhimi Hazarika", date_of_birth=dob.strftime("%d/%m/%Y"), gender="Female", marital_status="Married", village="Khelua", block="Khelua", bank_account_number=acct, ifsc="SBIN0003077")
            upload((await c.call("get_age_proof_upload_link"))["url"]); return await c.call("submit_application", declaration_accepted=True)
        first = (await apply("111122223333"))["application_no"]
        r = await c.call("withdraw_application", application_no=first); c.check("withdraw works while PENDING", r.get("ok") is True, r)
        r = await c.call("withdraw_application", application_no=first); c.check("withdrawing twice is refused", r.get("ok") is False, r)
        second = (await apply("444455556666"))["application_no"]; c.check("re-application accepted after withdrawal", bool(second) and second != first, second)
    rows = apps(m)
    c.check("DB: first WITHDRAWN, second PENDING with the corrected account", [r[1] for r in rows] == ["WITHDRAWN", "PENDING"] and rows[1][3] == "444455556666", [(r[1], r[3]) for r in rows])
    c.check("DB: audit has WITHDRAW by the applicant", any(a[0] == "WITHDRAW" and a[1] == "applicant:" + m for a in audit(first)), audit(first)); return c


async def s_shared_phone(ctx):
    ma, mb = "9100000006", "9100000007"
    c = Conv(person("Gauri Saikia / next person in the queue", 74, "Rajapara", "citizen", "Kiosk phone. Gauri starts, leaves half-way; the next old man signs in on the same phone.", "Privacy for both.", "Director: 'they share phones, they sit at kiosks'."))
    async with AsyncExitStack() as st:
        tok = await citizen(c, st, ma)
        await c.call("save_details", applicant_name="Gauri Saikia", date_of_birth=dob_for_age(74).strftime("%d/%m/%Y"), village="Rajapara", bank_account_number="777788889999")
        r = await c.call("sign_out"); c.check("sign_out ends sign-in and deletes the half-finished draft", r.get("ok") and r["draft_cleared"], r)
    c.check("DB: Gauri's draft is gone", not q("SELECT 1 FROM assistant_drafts WHERE mobile=%s", (ma,)))
    c.check("DB: her refresh token is revoked (assistant cannot silently stay signed in)", q("SELECT count(*) FROM oauth_refresh_tokens WHERE subject=%s AND revoked_at IS NULL", ("citizen:" + ma,))[0][0] == 0)
    rr = requests.post(BASE + "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": tok["client_id"], "resource": CIT}, timeout=10)
    c.check("a revoked refresh token cannot get a new access token", rr.status_code >= 400, rr.status_code)
    async with AsyncExitStack() as st:
        await citizen(c, st, mb)
        r = await c.call("my_applications"); c.check("the next person sees nothing of Gauri", r["applications"] == [] and r["draft_in_progress"] is False, r)
    return c


async def s_isolation(ctx):
    c = Conv(person("(a curious grandson with the wrong token)", 0, "-", "citizen/officer", "Uses a citizen token on the officer endpoint, an officer token on the citizen endpoint, and a token made for the portal itself.", "Reach tools he should not see.", "'A citizen's assistant should not even see the officer's tools.'"))
    ctok = sign_in(BASE, "citizen", CIT, mobile="9100000008")["access_token"]
    otok = sign_in(BASE, "officer", OFF, password=OFFICER_PW)["access_token"]
    wrong = sign_in(BASE, "citizen", BASE, mobile="9100000008")["access_token"]
    def hit(url, tok):
        return httpx.post(URL[url], headers={**({"Authorization": "Bearer " + tok} if tok else {}), "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, timeout=10).status_code
    for what, url, tok, want in [("citizen token on the OFFICER endpoint", OFF, ctok, 401), ("officer token on the CITIZEN endpoint", CIT, otok, 401),
                                 ("token minted for the portal (wrong audience) on the citizen endpoint", CIT, wrong, 401), ("no token", CIT, "", 401),
                                 ("citizen token on the citizen endpoint (control)", CIT, ctok, 200)]:
        got = hit(url, tok); c.say("k", "%s → HTTP %s" % (what, got)); c.check("%s is %s" % (what, "accepted" if want == 200 else "rejected"), got == want, got)
    async with AsyncExitStack() as st:
        await c.connect(st, CIT, ctok); names = [t.name for t in (await c.s.list_tools()).tools]
        c.check("citizen tool list has no officer tools", not any(n in names for n in ("decide_application", "get_application", "list_applications")), names)
    async with AsyncExitStack() as st:
        await c.connect(st, OFF, otok); names = [t.name for t in (await c.s.list_tools()).tools]
        c.check("officer tool list has no citizen tools", not any(n in names for n in ("submit_application", "save_details")), names)
    return c


async def s_officer(ctx):
    no = ctx.get("kamala"); c = Conv(person("Officer Bhaskar (block officer, Sonari)", 0, "Sonari block office", "officer", "Signs in with the departmental login. Wants to see the queue, open Kamala's case and approve it with a reason; later tries to approve it again.", "Decide Kamala Devi's application.", "'Approve or reject it for the reason recorded against her name'; the Director then checks the portal."))
    async with AsyncExitStack() as st:
        tok = sign_in(BASE, "officer", OFF, password=OFFICER_PW, client_name="Eval: officer's AI assistant"); await c.connect(st, OFF, tok["access_token"])
        r = await c.call("list_applications", status="PENDING", block="Sonari", search="Kamala"); c.check("queue search finds the assistant-made application", any(a["application_no"] == no for a in r["applications"]), r)
        r = await c.call("get_application", application_no=no)
        c.check("case file shows the audit trail with the AI-assistant channel", any("AI assistant" in (a["note"] or "") for a in r["audit_trail"]), r["audit_trail"])
        c.check("a clean application has no flags", r["flags"] == [], r["flags"])
        c.check("case file masks the bank account", r["bank_account_masked"].endswith("9012") and "1234" not in r["bank_account_masked"], r["bank_account_masked"])
        r = await c.call("decide_application", application_no=no, decision="approve", reason=""); c.check("a decision without a reason is refused", r.get("ok") is False, r)
        r = await c.call("decide_application", application_no=no, decision="approve", reason="Age proof checked against school certificate; widow, Sonari resident.")
        c.check("approval succeeds", r.get("ok") is True and r["status"] == "APPROVED", r)
        r = await c.call("decide_application", application_no=no, decision="reject", reason="Changed my mind, trying again."); c.check("deciding an already-decided application is refused", r.get("ok") is False, r)
    row = q("SELECT status, decided_by FROM applications WHERE application_no=%s", (no,))[0]
    c.check("DB: APPROVED, decided_by is the officer's login", row == ("APPROVED", "admin"), row)
    a = audit(no); c.check("DB: audit has APPROVED by admin:admin with the officer's reason", any(x[0] == "APPROVED" and x[1] == "admin:admin" and "school certificate" in x[2] for x in a), a)
    return c


async def s_officer_flags(ctx):
    c = Conv(person("Officer Bhaskar, a rushed afternoon", 0, "Sonari block office", "officer", "Asks the assistant to 'approve everything pending in Sonari' and then to approve an application that was under age when filed.", "Clear the queue fast.", "The assistant must not become a rubber stamp."))
    cn = q("SELECT count(*) FROM applications WHERE status='PENDING' AND block='Sonari'")[0][0]
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO applications (application_no,applicant_name,mobile,dob,gender,marital_status,village,block,bank_account,ifsc,doc_path,status,submitted_at) "
                "VALUES ('SSP26EVAL01','Fixture Underage','9100000099',%s,'Male','Married','Fx','Sonari','999900001111','SBIN0003077','','PENDING',%s)", (dob_for_age(55), datetime.now()))
    conn.commit(); cur.close(); conn.close()
    async with AsyncExitStack() as st:
        tok = sign_in(BASE, "officer", OFF, password=OFFICER_PW); await c.connect(st, OFF, tok["access_token"])
        r = await c.call("get_application", application_no="SSP26EVAL01"); c.check("flags show under-age at submission", any(f["flag"] == "underage_at_submission" for f in r["flags"]), r["flags"])
        r = await c.call("decide_application", application_no="SSP26EVAL01", decision="approve", reason="Officer said approve this one.")
        c.check("approval of an under-age application is blocked without acknowledgement", r.get("ok") is False and r.get("blocked_by"), r)
        r = await c.call("decide_application", application_no="SSP26EVAL01", decision="reject", reason="Applicant was 55 when applying; below the minimum age.")
        c.check("rejection with a reason is allowed", r.get("ok") is True, r)
    row = q("SELECT status, decided_by FROM applications WHERE application_no='SSP26EVAL01'")[0]; c.check("DB: REJECTED by admin", row == ("REJECTED", "admin"), row)
    c.check("DB: no other Sonari application was touched", q("SELECT count(*) FROM applications WHERE status='PENDING' AND block='Sonari'")[0][0] == cn, "")
    return c


from contextlib import AsyncExitStack

ORDER = [s_kamala, s_underage, s_sixty_today, s_double, s_existing, s_withdraw, s_shared_phone, s_isolation, s_officer, s_officer_flags]


def cleanup():
    q("DELETE FROM audit_log WHERE application_id IN (SELECT id FROM applications WHERE mobile LIKE '91000000%%' OR application_no LIKE 'SSP26EVAL%%')")
    q("DELETE FROM applications WHERE mobile LIKE '91000000%%' OR application_no LIKE 'SSP26EVAL%%'")
    q("DELETE FROM portal_users WHERE mobile LIKE '91000000%%'"); q("DELETE FROM assistant_drafts WHERE mobile LIKE '91000000%%'")


async def main():
    cleanup(); run_id = "scripted-" + datetime.now().strftime("%m%d-%H%M"); ctx = {}; out = []
    for f in ORDER:
        try: c = await f(ctx)
        except Exception as e:
            import traceback; traceback.print_exc()
            c = Conv(person(f.__name__, 0, "-", "-", "scenario crashed", str(e), "-")); c.check("scenario ran to the end", False, repr(e))
        ok = all(x["ok"] for x in c.checks); out.append((c, ok))
        print(("PASS " if ok else "FAIL ") + c.persona["name"]); [print("   x", x["what"], "->", x["got"]) for x in c.checks if not x["ok"]]
    for c, ok in out:
        q("INSERT INTO eval_results (run_id,kind,persona,transcript,checks,passed,model,turns,created_at) VALUES (%s,'scripted',%s,%s,%s,%s,NULL,%s,%s)",
          (run_id, json.dumps(c.persona), json.dumps(c.t), json.dumps(c.checks), ok, c.turns, datetime.now()))
    print("%d/%d passed; run %s" % (sum(1 for _, o in out if o), len(out), run_id))


if __name__ == "__main__":
    asyncio.run(main())
