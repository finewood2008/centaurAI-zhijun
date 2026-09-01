import sqlite3

db = r"d:\项目文件\mindos\nexusaos-centuarai-database\data\db\job_store.db"
sp = "92337c43_o_1i4qop009177v1tgf14db15he1iaj1is.jpg"
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
conn.row_factory = sqlite3.Row
try:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("TABLES:", tables)
    for t in tables:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if "source_path" in cols:
            print(f"\n== {t} cols={cols}")
            rows = conn.execute(f"SELECT * FROM {t} WHERE source_path LIKE ? OR source_path LIKE ?", ("%"+sp, sp)).fetchall()
            for r in rows:
                print(" ", dict(r))
finally:
    conn.close()