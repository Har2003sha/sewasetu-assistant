"""Acts as the PERSON at a browser: signs in on the portal (citizen: mobile + OTP read from the SMS gateway;
officer: departmental login), approves the consent screen, and returns the tokens a real MCP client would get."""
import re, time, base64, hashlib, secrets
from urllib.parse import urlparse, parse_qs, urlencode
import requests

REDIRECT = "http://127.0.0.1:53682/callback"


def _otp(base, mobile, since_id, wait=12):
    end = time.time() + wait
    while time.time() < end:
        msgs = requests.get(f"{base}/__gateway/api/messages", params={"to": mobile}, timeout=5).json()["messages"]
        for m in reversed(msgs):
            if m["id"] > since_id and m["delivered"]:
                x = re.search(r"OTP is (\d{6})", m["text"] or "")
                if x: return x.group(1)
        time.sleep(0.5)
    raise RuntimeError("no OTP delivered")


def _last_id(base, mobile):
    msgs = requests.get(f"{base}/__gateway/api/messages", params={"to": mobile}, timeout=5).json()["messages"]
    return max([m["id"] for m in msgs] or [0])


def sign_in(base, role, resource, mobile=None, username="admin", password=None, client_name="Eval client"):
    reg = requests.post(f"{base}/oauth/register", json={"client_name": client_name, "redirect_uris": [REDIRECT],
          "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
          "response_types": ["code"]}, timeout=10).json()
    cid = reg["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    s = requests.Session()
    r = s.get(f"{base}/oauth/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256", "scope": role, "resource": resource, "state": "x"}, timeout=10)
    rid = parse_qs(urlparse(r.url).query)["rid"][0]
    login = f"{base}/oauth/login?rid={rid}"
    if role == "citizen":
        before = _last_id(base, mobile)
        s.post(login, data={"mobile": mobile}, timeout=10)
        s.post(login, data={"otp": _otp(base, mobile, before)}, timeout=10)
    else:
        s.post(login, data={"username": username, "password": password}, timeout=10)
    r = s.post(f"{base}/oauth/consent?rid={rid}", data={"decision": "allow"}, allow_redirects=False, timeout=10)
    code = parse_qs(urlparse(r.headers["Location"]).query)["code"][0]
    t = requests.post(f"{base}/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": cid, "code_verifier": verifier, "resource": resource}, timeout=10).json()
    t["client_id"] = cid
    return t
