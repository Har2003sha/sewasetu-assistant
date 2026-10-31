# -*- coding: utf-8 -*-
"""Do the uploaded documents belong to the person applying?

Reads the age-proof and the bank passbook / cancelled-cheque photo with OCR (Tesseract) and compares them with what
was typed in:   name on the age proof, date of birth on the age proof, name and account number on the bank document,
and whether the IFSC exists (free public lookup).  It is deliberately careful:

  * clear mismatch on a READABLE document  -> the application is refused and the person is told why
  * blurry / Hindi-only / PDF / OCR missing -> accepted, but marked "not machine-verified" so the officer is warned

This catches the casual case (someone else's certificate or passbook). It cannot prove a document is genuine or that
the account really belongs to the person: that needs government / bank verification (DigiLocker, penny-drop)."""
import os, re, difflib
from datetime import date

STOP = {"shri", "sri", "smt", "mr", "mrs", "ms", "miss", "late", "dr", "kumari", "the", "of", "son", "daughter", "wife", "name"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_BIRTH = re.compile(r"birth|dob|d\.o\.b|born|yob", re.I)


def _ocr(path):
    if not path or path.rsplit(".", 1)[-1].lower() not in ("jpg", "jpeg", "png"):
        return None
    try:
        import pytesseract
        from PIL import Image, ImageOps
        if os.environ.get("TESSERACT_CMD"):
            pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
        elif os.name == "nt" and os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
            pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        img = ImageOps.autocontrast(Image.open(path).convert("L"))
        if img.width < 1400:
            img = img.resize((img.width * 2, img.height * 2))
        d = pytesseract.image_to_data(img, lang=os.environ.get("OCR_LANG", "eng"), config="--psm 6", output_type=pytesseract.Output.DICT)
    except Exception:
        return None
    words = [(w, float(c)) for w, c in zip(d["text"], d["conf"]) if w.strip() and float(c) >= 0]
    good = [w for w, c in words if c >= 55 and re.search(r"[A-Za-z0-9]", w)]
    return {"text": " ".join(w for w, _ in words), "readable": len(good) >= 8}


def _tokens(s):
    return [t for t in re.findall(r"[a-z]+", (s or "").lower()) if len(t) >= 2 and t not in STOP]


def name_score(name, text):
    want, have = _tokens(name), set(_tokens(text))
    if not want:
        return 1.0
    hit = sum(1 for t in want if any(abs(len(x) - len(t)) <= 2 and difflib.SequenceMatcher(None, t, x).ratio() >= 0.8 for x in have))
    return hit / len(want)


def _dates(text):
    found = []
    for m in re.finditer(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})", text):
        found.append((m.start(), m.group(1), m.group(2), m.group(3)))
    for m in re.finditer(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", text):
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            found.append((m.start(), m.group(1), str(mon), m.group(3)))
    out = []
    for pos, d, mth, y in found:
        try:
            out.append((date(int(y), int(mth), int(d)), bool(_BIRTH.search(text[max(0, pos - 30):pos]))))
        except ValueError:
            pass
    return out


def dob_result(dob, text):
    ds = _dates(text)
    if any(x == dob for x, _ in ds):
        return "ok"
    y = re.search(r"(?:yob|year of birth|birth year)\D{0,8}(\d{4})", text, re.I)
    if y:
        return "ok" if int(y.group(1)) == dob.year else "mismatch"
    labelled = [x for x, lab in ds if lab]
    return "mismatch" if labelled else "unknown"


def account_result(acct, text):
    digits = re.sub(r"\D", "", text)
    if acct in digits or (len(acct) >= 10 and acct[-6:] in digits):
        return "ok"
    return "mismatch"


def ifsc_result(ifsc):
    if os.environ.get("VERIFY_IFSC", "1") != "1" or not ifsc:
        return "skipped", ""
    try:
        import requests
        r = requests.get("https://ifsc.razorpay.com/" + ifsc, timeout=3)
        if r.status_code == 404:
            return "invalid", ""
        if r.ok:
            j = r.json()
            return "ok", "%s, %s" % (j.get("BANK", ""), j.get("BRANCH", ""))
    except Exception:
        pass
    return "skipped", ""


def check(c, age_path, bank_path):
    """c = cleaned application. Returns (errors, note)."""
    errors, parts = [], []
    a, b = _ocr(age_path), _ocr(bank_path)
    if a and a["readable"]:
        n, d = name_score(c["applicant_name"], a["text"]), dob_result(c["dob"], a["text"])
        parts.append("age proof name=%s dob=%s" % ("ok" if n >= 0.6 else "MISMATCH", d if d != "mismatch" else "MISMATCH"))
        if n < 0.6:
            errors.append("the name on the age-proof document does not match the applicant name you entered")
        if d == "mismatch":
            errors.append("the date of birth on the age-proof document does not match the date of birth you entered")
    else:
        parts.append("age proof not machine-verified")
    if b and b["readable"]:
        n, acc = name_score(c["applicant_name"], b["text"]), account_result(c["bank_account"], b["text"])
        parts.append("bank doc name=%s account=%s" % ("ok" if n >= 0.6 else "MISMATCH", acc if acc != "mismatch" else "MISMATCH"))
        if acc == "mismatch":
            errors.append("the bank document does not show the account number you entered")
        if n < 0.6:
            errors.append("the name on the bank document does not match the applicant name; the account must be in the applicant's own name")
    else:
        parts.append("bank doc not machine-verified")
    st, info = ifsc_result(c["ifsc"])
    if st == "invalid":
        errors.append("IFSC %s does not exist; please copy it from the passbook" % c["ifsc"])
    elif st == "ok":
        parts.append("ifsc ok (%s)" % info)
    return errors, "; ".join(parts)