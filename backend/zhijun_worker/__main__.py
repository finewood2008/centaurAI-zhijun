"""Run only the private worker app on its service-owned Unix socket."""
import os
from pathlib import Path

from .workspace import Workspace
from .capabilities import HttpCapabilities
from .app import create_app


def main():
    import uvicorn
    os.umask(0o077)
    workspace = Workspace.from_environment()
    socket = workspace.socket_path
    if socket.exists() or socket.is_symlink():
        raise RuntimeError("WORKER_SOCKET_ALREADY_EXISTS")
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket.parent.stat().st_mode & 0o077:
        raise RuntimeError("WORKER_SOCKET_PERMISSIONS_INVALID")
    capabilities = HttpCapabilities(workspace, os.environ["ZHIJUN_CAPABILITY_URL"])
    try:
        uvicorn.run(create_app(workspace, capabilities), uds=str(socket), access_log=False, log_level="warning")
    finally:
        if socket.exists() and socket.is_socket():
            socket.unlink()


if __name__ == "__main__":
    main()
