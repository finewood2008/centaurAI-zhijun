"""Local development lifecycle; no installation, model calls, or database writes.

Own only supervisor processes created here. Existing project services may be
reused, but are never adopted or stopped. PID files alone are not ownership.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "backend": (8618, "/api/health", "start-backend.sh"),
    "web": (5173, "/mindos/", "start-web.sh"),
}


def healthy(port, path):
    try:
        # Ignore inherited HTTP proxy settings for local health checks.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=1.5) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def process_command(pid):
    try:
        return subprocess.check_output(["ps", "-p", str(int(pid)), "-o", "command="], text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, ValueError, TypeError):
        return ""


class Runtime:
    def __init__(self, root=ROOT, components=None):
        self.root = Path(root).resolve()
        self.components = components or COMPONENTS
        self.folder = self.root / "data" / "run" / "dev"
        self.state_path = self.folder / "services.json"
        self.children = {}

    @contextmanager
    def lock(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / "control.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self):
        try:
            state = json.loads(self.state_path.read_text())
            return state.get("services", {}) if state.get("project") == str(self.root) else {}
        except (OSError, ValueError):
            return {}

    def save(self, state):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"project": str(self.root), "services": state}, indent=2))
        temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def owned(self, name, record):
        if not record or record.get("project") != str(self.root):
            return False
        command = process_command(record.get("pid"))
        token = record.get("token", "")
        return bool(token and str(Path(__file__).resolve()) in command
                    and f"--serve {name}" in command and f"--token {token}" in command
                    and f"--project {self.root}" in command)

    def existing_project_service(self, port, record=None):
        try:
            pids = subprocess.check_output(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], text=True, stderr=subprocess.DEVNULL).split()
            for pid in set(pids):
                if record is not None and os.getpgid(int(pid)) != record["pid"]:
                    continue
                cwd = subprocess.check_output(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], text=True, stderr=subprocess.DEVNULL)
                for line in cwd.splitlines():
                    if line.startswith("n"):
                        path = Path(line[1:]).resolve()
                        if path == self.root or self.root in path.parents:
                            return True
        except (OSError, subprocess.CalledProcessError):
            pass
        return False

    def status(self):
        records = self.read()
        result = {}
        for name, (port, path, _) in self.components.items():
            ready = healthy(port, path)
            managed = self.owned(name, records.get(name))
            project = ready and self.existing_project_service(port, records.get(name) if managed else None)
            result[name] = {"healthy": ready and project, "managed": managed,
                "state": "ready" if ready and project else "starting" if managed else "occupied" if listening(port) else "stopped",
                "pid": records.get(name, {}).get("pid") if managed else None,
                "url": f"http://127.0.0.1:{port}{path}", "log": str(self.folder / f"{name}.log")}
        return result

    def start(self, timeout=60):
        with self.lock():
            state = self.read()
            for name, (port, path, script) in self.components.items():
                if self.owned(name, state.get(name)):
                    continue
                if listening(port):
                    if healthy(port, path) and self.existing_project_service(port):
                        state.pop(name, None)
                        continue
                    raise RuntimeError(f"{port} 端口已有服务但无法确认为本项目的健康服务；未启动或停止该进程。")
                if not (self.root / script).is_file():
                    raise RuntimeError(f"缺少启动脚本：{script}")
                previous = self.children.get(name)
                if previous is not None:
                    previous.poll()
                token = uuid.uuid4().hex
                with (self.folder / f"{name}.log").open("ab") as log:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", name,
                        "--token", token, "--project", str(self.root)], cwd=self.root,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.children[name] = process
                state[name] = {"pid": process.pid, "token": token, "project": str(self.root), "startedAt": time.time()}
                self.save(state)
            self.save(state)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            result = self.status()
            if all(item["healthy"] for item in result.values()):
                return result
            if any(item["state"] == "stopped" for item in result.values()):
                break
            time.sleep(.3)
        raise RuntimeError("服务尚未就绪；已保留日志。运行 status 查看状态；修正原因后再次 start 可恢复。")

    def stop(self, timeout=8):
        with self.lock():
            state = self.read()
            for name, record in list(state.items()):
                if name not in self.components or not self.owned(name, record):
                    state.pop(name, None)
                    continue
                # Each verified supervisor owns its own session and child group.
                try:
                    if os.getpgid(record["pid"]) != record["pid"]:
                        raise RuntimeError("进程组已变化，未停止该服务。")
                    os.killpg(record["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                until = time.monotonic() + timeout
                while self.owned(name, record) and time.monotonic() < until:
                    time.sleep(.1)
                if self.owned(name, record):
                    raise RuntimeError(f"{name} 仍在退出，记录已保留；没有强制终止。")
                state.pop(name, None)
                self.save(state)
            self.save(state)
            for name, child in list(self.children.items()):
                if child.poll() is not None:
                    child.wait()
                    self.children.pop(name, None)
        return self.status()


def serve(root, name):
    """Keep a verifiable group leader alive until its script and children exit."""
    child = subprocess.Popen(["bash", str(root / COMPONENTS[name][2])], cwd=root)
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        # The controller already signals our complete process group.
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    code = child.wait()
    if not stopping and code:
        print(f"{name} exited ({code}); inspect its log and run start to recover.", flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description="知君本机开发服务：start / status / stop")
    parser.add_argument("action", nargs="?", choices=("start", "status", "stop"), default="start")
    parser.add_argument("--serve", choices=COMPONENTS, help=argparse.SUPPRESS)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--project", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        return serve(args.project.resolve(), args.serve)
    runtime = Runtime()
    try:
        result = getattr(runtime, args.action)()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        print(f"日志目录：{runtime.folder}", file=sys.stderr)
        return 1
    labels = {"ready": "已就绪", "starting": "正在启动", "occupied": "端口被占用或服务未就绪", "stopped": "未启动"}
    for name, item in result.items():
        print(f"{name}: {labels[item['state']]} · {'本入口管理' if item['managed'] else '未接管'} · {item['url']}")
    print(f"日志与进程记录：{runtime.folder}")
    if args.action == "start":
        print("打开应用：http://127.0.0.1:5173/mindos/")
    return 0 if args.action == "stop" or all(item["healthy"] for item in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
