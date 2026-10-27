import os, psycopg2
def db():
    return psycopg2.connect(host=os.environ.get("DB_HOST", "127.0.0.1"), port=os.environ.get("DB_PORT", "5432"), dbname="sewasetu",
                            user="sewasetu", password=os.environ.get("DB_PASSWORD", "sewasetu123"))
def q(sql, args=()):
    c = db(); cur = c.cursor(); cur.execute(sql, args)
    rows = cur.fetchall() if cur.description else []
    c.commit(); cur.close(); c.close(); return rows
