import sqlite3, os, sys

db = r"d:\项目文件\mindos\nexusaos-centuarai-database\data\db\index_registry.db"
sp = r"D:\项目文件\mindos\nexusaos-centuarai-database\data\watch_folder\.mindos_uploads\92337c43_o_1i4qop009177v1tgf14db15he1iaj1is.jpg"
mid = "mindos_31bcb67764ee"

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
conn.row_factory = sqlite3.Row
try:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("TABLES:", tables)
    for t in tables:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if "source_path" in cols or "material_id" in cols or "job_id" in cols:
            print(f"\n== {t} cols={cols}")
            try:
                if "source_path" in cols:
                    rows = conn.execute(f"SELECT * FROM {t} WHERE source_path=? OR material_id=?", (sp, mid)).fetchall()
                elif "material_id" in cols:
                    rows = conn.execute(f"SELECT * FROM {t} WHERE material_id=?", (mid,)).fetchall()
                else:
                    rows = conn.execute(f"SELECT * FROM {t} WHERE job_id='job_31bcb677'").fetchall()
                for r in rows:
                    print(" ", dict(r))
            except Exception as e:
                print("  QUERY_ERR", repr(e))
finally:
    conn.close()