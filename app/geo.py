# -*- coding: utf-8 -*-
"""State / district / block lists for India (static/india.json) and the rules for checking them.

The data is a public dataset (about 29 states, 600+ districts, 5,900 blocks) and is NOT the live official list:
some districts and blocks have been renamed or created since. So the rule is: the state must be real, the
district must belong to that state (when we have its list), and the block must look like a place name. Replace
static/india.json with a newer LGD export whenever you like; the format is {state: {district: [blocks]}}."""
import os, re, json
from difflib import get_close_matches

_P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "india.json")
DATA = json.load(open(_P, encoding="utf-8"))
_NAME = re.compile(r"[A-Za-z][A-Za-z .'()/&-]{1,59}")


def _canon(value, options):
    v = " ".join((value or "").split())
    if not v:
        return ""
    low = {o.lower(): o for o in options}
    if v.lower() in low:
        return low[v.lower()]
    m = get_close_matches(v.lower(), list(low), n=1, cutoff=0.88)
    return low[m[0]] if m else ""


def check(state, district, block, legacy_blocks):
    """Returns (errors, (state, district, block)) with canonical spelling."""
    state, district, block = [" ".join((x or "").split()) for x in (state, district, block)]
    if not state:                                   # records and tools from before the all-India list
        legacy = {x.lower(): x for x in legacy_blocks}
        if block.lower() in legacy:
            return [], ("", "", legacy[block.lower()])
        return ["state is required"], ("", district, block)
    errors = []
    s = _canon(state, DATA)
    if not s:
        return ["state '%s' was not found; please pick it from the list" % state], (state, district, block)
    d = ""
    if DATA[s]:
        d = _canon(district, DATA[s])
        if not d:
            errors.append("district '%s' is not in %s; please pick it from the list" % (district or "", s))
    elif not _NAME.fullmatch(district or ""):
        errors.append("district is required")
    else:
        d = district
    if not block or not _NAME.fullmatch(block):
        errors.append("block / tehsil is required (letters only)")
        b = block
    else:
        b = _canon(block, DATA[s].get(d, [])) or block
    return errors, (s, d or district, b)