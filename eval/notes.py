"""Findings from building the evals (code review of the portal while designing the cases). Re-runnable."""
from datetime import datetime
from checks_db import q
NOTES = [
 ("The portal checks 'no live application on this mobile' and then inserts, with nothing tying the two together. Two submits at the same instant (a double tap, a retrying AI client) could both pass.",
  "One submit_application() now serves the web form and the assistant: validate, rule check and insert in one transaction under a per-mobile lock. Eval 'Sabitri Borah' fires five submits at once and expects exactly one row."),
 ("Application numbers are SSP + year + 6 random digits with no uniqueness check. 58,567 of the 900,000 possible 2026 numbers are already used, so about 6.5% of new applications would get a number someone already holds; officers and applicants look cases up by that number.",
  "Numbers are checked against the table before use and a unique index backs it up."),
 ("The web upload builds the file path from the client's file name. The assistant's upload link only checked the extension in the first design.",
  "Web: file name sanitised. Assistant link: single-use, 30-minute, and the file's first bytes must match JPG/PNG/PDF."),
 ("The 700 pending applications in the data snapshot are all past the 15-day line; the scheduler's start-up run will deem them approved, so a fresh deployment's officer queue is almost empty.",
  "No code change (it is the law). Officer evals create their own applications; the queue summary shows how many are within 3 days of automatic approval."),
 ("The portal's required-field rule lets gender, marital status and IFSC stay blank, which would leave a pension with no way to be paid.",
  "Rule left as the department wrote it (assistant must not be stricter than the counter in what it admits). The assistant's instructions make it ask for all of them; flagged in the design note for the department to decide."),
]
if __name__ == "__main__":
    q("DELETE FROM eval_notes")
    for f, c in NOTES: q("INSERT INTO eval_notes (run_id, found, changed, created_at) VALUES ('review',%s,%s,%s)", (f, c, datetime.now()))
    print("notes:", len(NOTES))
