import sys, json
sys.path.insert(0, "backend")
from mindos.services import ingestion

mid = "mindos_31bcb67764ee"
try:
    rec = ingestion.JobStore.instance().get(mid)
except Exception as e:
    print("JOBSTORE_ERR", repr(e)); rec = None
print("RAW_REC:", json.dumps(rec, ensure_ascii=False) if rec else None)
try:
    print("STATUS:", json.dumps(ingestion.status_of(mid), ensure_ascii=False))
except Exception as e:
    print("STATUS_ERR", repr(e))