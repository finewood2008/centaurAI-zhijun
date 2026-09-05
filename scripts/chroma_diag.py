"""只读诊断 chroma.sqlite3 的锁/WAL 状态（在副本上执行，不触碰原库）。"""
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
cur = con.cursor()

for sql, label in [
    ("SELECT count(*), sum(lock_status) FROM acquire_write", "acquire_write 写锁 (总数, 未释放数)"),
    ("SELECT count(*), min(seq_id), max(seq_id) FROM embeddings_queue", "embeddings_queue WAL (行数, min_seq, max_seq)"),
    ("SELECT count(*) FROM embeddings", "embeddings 总数"),
    ("SELECT segment, max(seq_id) FROM embeddings GROUP BY segment", "各段 embeddings/seq"),
    ("SELECT * FROM max_seq_id", "max_seq_id 表"),
    ("SELECT * FROM maintenance_log ORDER BY rowid DESC LIMIT 10", "maintenance_log 最近10条"),
]:
    try:
        cur.execute(sql)
        print(f"{label}:")
        for r in cur.fetchall():
            print("  ", r)
    except Exception as exc:
        print(f"{label}: ERROR {type(exc).__name__}: {exc}")

con.close()
