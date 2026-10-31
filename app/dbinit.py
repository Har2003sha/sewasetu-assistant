# -*- coding: utf-8 -*-
"""Create the tables and, on a brand-new database, load seed/seed.sql (a pg_dump with COPY blocks).
Safe to run on every start:  python dbinit.py"""
import os, re, sys
import db

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.environ.get("SEED_PATH") or next((p for p in (os.path.join(HERE, "..", "seed", "seed.sql"), "/seed/seed.sql") if os.path.exists(p)), None)

EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS assistant_drafts (
    mobile VARCHAR(15) PRIMARY KEY, data JSON, doc_path VARCHAR(200) DEFAULT '', bank_doc_path VARCHAR(200) DEFAULT '',
    upload_token VARCHAR(64), upload_expires TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id VARCHAR(40), kind VARCHAR(12), persona JSON, transcript JSON,
    checks JSON, passed BOOLEAN, model VARCHAR(60), turns INTEGER, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS eval_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id VARCHAR(40), found TEXT, changed TEXT, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS web_users (
    mobile VARCHAR(15) PRIMARY KEY, name VARCHAR(100) DEFAULT '', email VARCHAR(200), google_sub VARCHAR(64) UNIQUE,
    created_at TIMESTAMP, last_login TIMESTAMP);
CREATE TABLE IF NOT EXISTS sms_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, to_mobile VARCHAR(15), text TEXT, sent_at REAL, delivery_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_app_no ON applications(application_no);
"""


def ensure_schema():
    conn = db.connect()
    ddl = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    ddl = re.sub(r"\bSERIAL PRIMARY KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", ddl).replace("JSONB", "JSON")
    conn._raw.executescript(ddl + EXTRA_DDL)
    for table, col, typ in (("applications", "state", "VARCHAR(60)"), ("applications", "district", "VARCHAR(80)"),
                            ("applications", "bank_doc_path", "VARCHAR(200)"), ("applications", "verification", "TEXT"),
                            ("assistant_drafts", "bank_doc_path", "VARCHAR(200) DEFAULT ''")):
        if col not in [r[1] for r in conn._raw.execute("PRAGMA table_info(%s)" % table)]:
            conn._raw.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    conn._raw.commit()
    conn.close()


def _unescape(v):
    if v == "\\N":
        return None
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), v)


def load_seed(path):
    conn = db.connect(); raw = conn._raw
    if raw.execute("SELECT count(*) FROM applications").fetchone()[0]:
        conn.close(); return 0
    n, table, sql, batch = 0, None, None, []
    raw.execute("BEGIN")
    with open(path, encoding="utf-8") as f:
        for line in f:
            if table is None:
                m = re.match(r"COPY (\w+) \((.*?)\) FROM stdin;", line)
                if m:
                    table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
                    sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join("?" * len(cols)))
            elif line.startswith("\\."):
                raw.executemany(sql, batch); n += len(batch); table, batch = None, []
            else:
                batch.append([_unescape(v) for v in line.rstrip("\n").split("\t")])
                if len(batch) >= 5000:
                    raw.executemany(sql, batch); n += len(batch); batch = []
    raw.commit(); conn.close()
    return n


if __name__ == "__main__":
    ensure_schema()
    if SEED:
        print("seed rows loaded:", load_seed(SEED))
    else:
        print("no seed file found; starting with an empty database")