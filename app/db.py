# -*- coding: utf-8 -*-
"""SQLite access with the small psycopg2-style surface the portal was written against:
conn.cursor(), cur.execute(sql, args) with %s placeholders, fetchone/fetchall, commit, close.

* %s -> ?, and a tuple/list argument (WHERE status IN %s) expands to (?,?,?)
* TIMESTAMP / DATE / JSON columns come back as datetime / date / parsed objects, like Postgres did
* Postgres' per-mobile advisory lock becomes BEGIN IMMEDIATE (SQLite allows one writer at a time), so
  "check, then insert" in submit_application() is still atomic
* WAL mode + busy timeout, so several gunicorn workers can share the file
DB_PATH picks the file (default: app/data/sewasetu.db). Put it on a persistent disk in production.
"""
import os, re, json, sqlite3
import datetime as _dt

PATH = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sewasetu.db")


def _ts(b):
    try:
        return _dt.datetime.fromisoformat(b.decode().strip())
    except ValueError:
        return None


def _date(b):
    try:
        return _dt.date.fromisoformat(b.decode().strip()[:10])
    except ValueError:
        return None


def _json(b):
    try:
        return json.loads(b.decode())
    except ValueError:
        return None


sqlite3.register_converter("timestamp", _ts)
sqlite3.register_converter("date", _date)
sqlite3.register_converter("json", _json)
sqlite3.register_adapter(_dt.datetime, lambda d: d.isoformat(" "))
sqlite3.register_adapter(_dt.date, lambda d: d.isoformat())


def _translate(sql, args):
    if not args:                       # psycopg2 does not touch % when there are no arguments
        return sql, ()
    out, params, i = [], [], 0
    for part in re.split(r"(%s|%%)", sql):
        if part == "%%":
            out.append("%")
        elif part == "%s":
            v = args[i]; i += 1
            if isinstance(v, (tuple, list)):
                out.append("(" + (",".join("?" * len(v)) if v else "NULL") + ")"); params.extend(v)
            else:
                out.append("?"); params.append(v)
        else:
            out.append(part)
    return "".join(out), tuple(params)


class Cursor:
    def __init__(self, conn):
        self._conn, self._rows, self._i = conn, [], 0
        self.rowcount, self.description, self.lastrowid = -1, None, None

    def execute(self, sql, args=None):
        raw = self._conn._raw
        if "pg_advisory_xact_lock" in sql:
            if not raw.in_transaction:
                raw.execute("BEGIN IMMEDIATE")
            self._rows, self._i, self.description = [(None,)], 0, None
            return self
        sql, params = _translate(sql, args)
        cur = raw.execute(sql, params)
        self.description = cur.description
        self._rows = cur.fetchall() if cur.description else []     # fetch eagerly: no half-read statements
        self._i, self.rowcount, self.lastrowid = 0, cur.rowcount, cur.lastrowid
        return self

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def close(self):
        pass


class Connection:
    def __init__(self):
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        self._raw = sqlite3.connect(PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=20)
        self._raw.execute("PRAGMA journal_mode=WAL")
        self._raw.execute("PRAGMA synchronous=NORMAL")
        self._raw.execute("PRAGMA busy_timeout=20000")

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            if self._raw.in_transaction:
                self._raw.rollback()
        finally:
            self._raw.close()


def connect():
    return Connection()