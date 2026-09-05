"""Run each backend test module in its own pytest/data process.

Some legacy tests import server at collection time and replace shared routers or
singletons. Module isolation keeps the supported pytest data guard while avoiding
order-dependent false failures. This does not suppress or alter any assertions.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    out = args.output or Path(tempfile.mkdtemp(prefix="zhijun-qa-"))
    out.mkdir(parents=True, exist_ok=True)
    files = [Path(p) for p in args.files] or sorted((ROOT / "backend/tests").glob("test_*.py"))

    def run(path):
        name = path.name
        started = time.monotonic()
        with (out / (name + ".log")).open("w") as log:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/run_tests.py"),
                "tests/" + name, "-q", "--tb=short", "--junitxml=" + str(out / (name + ".xml"))],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        return {"file": name, "exitCode": result.returncode,
                "seconds": round(time.monotonic() - started, 2)}

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        for future in as_completed([pool.submit(run, p) for p in files]):
            result = future.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    (out / "summary.json").write_text(json.dumps(sorted(results, key=lambda r: r["file"]), indent=2))
    print("Reports:", out)
    return int(any(r["exitCode"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
