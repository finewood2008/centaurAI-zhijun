"""启动 Consumer API Mock（开发联调，仅 loopback，状态 SQLite 持久化）。

状态保存在 data/db/consumer_mock.db（CENTAURAI_DATABASE_DATA_ROOT 之下），
重启后登录/设备/同步/票据状态不丢失。

运行：
    .venv\\Scripts\\python.exe scripts/run_mock_consumer_api.py
"""
from __future__ import annotations

import os

os.environ.setdefault("MINDOS_CONSUMER_MOCK_ENABLED", "1")

import uvicorn  # noqa: E402

from consumer_api.app import create_app  # noqa: E402
from consumer_api.store import open_persistent_state  # noqa: E402

if __name__ == "__main__":
    app = create_app(state=open_persistent_state())
    uvicorn.run(app, host="127.0.0.1", port=8801, reload=False)
