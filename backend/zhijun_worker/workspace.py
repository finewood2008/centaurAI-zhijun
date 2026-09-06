"""Immutable process identity and independently locked domain storage."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .auth import strict_json


def secure_read(path, maximum):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("WORKER_CONFIG_PATH_INVALID")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > maximum:
            raise ValueError("WORKER_CONFIG_PERMISSIONS_INVALID")
        with os.fdopen(fd, "rb", closefd=False) as file:
            result = file.read(maximum + 1)
        if len(result) > maximum:
            raise ValueError("WORKER_CONFIG_TOO_LARGE")
        return result
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    account_id: str
    device_id: str
    ownership_epoch: int
    data_root: Path
    key: bytes
    socket_path: Path

    def validate(self):
        import json
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) for value in (self.account_id, self.device_id)):
            raise ValueError("WORKER_SUBJECT_INVALID")
        if type(self.ownership_epoch) is not int or not 0 < self.ownership_epoch <= 9007199254740991:
            raise ValueError("WORKER_SUBJECT_INVALID")
        digest = hashlib.sha256(json.dumps([self.device_id, self.account_id, self.ownership_epoch], separators=(",", ":")).encode()).hexdigest()
        if self.workspace_id != digest or len(self.key) != 32:
            raise ValueError("WORKER_SUBJECT_INVALID")
        for path in (self.data_root, self.socket_path):
            if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("WORKER_PATH_INVALID")
        return self

    @classmethod
    def from_environment(cls):
        subject = strict_json(secure_read(os.environ["ZHIJUN_WORKSPACE_SUBJECT_FILE"], 4096))
        if type(subject) is not dict or set(subject) != {"accountId", "deviceId", "ownershipEpoch"}:
            raise ValueError("WORKER_SUBJECT_INVALID")
        return cls(os.environ["ZHIJUN_WORKSPACE_ID"], subject["accountId"], subject["deviceId"],
                   subject["ownershipEpoch"], Path(os.environ["CENTAURAI_DATABASE_DATA_ROOT"]),
                   secure_read(os.environ["ZHIJUN_WORKSPACE_KEY_FILE"], 32),
                   Path(os.environ["ZHIJUN_WORKSPACE_SOCKET"])).validate()


class WorkspaceLock:
    def __init__(self, workspace):
        self.workspace = workspace
        self.fd = None

    def acquire(self):
        self.workspace.validate()
        self.workspace.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.workspace.data_root.stat().st_mode & 0o077:
            raise ValueError("WORKER_ROOT_PERMISSIONS_INVALID")
        path = self.workspace.data_root / ".zhijun-worker.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("WORKER_LOCK_INVALID")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            identity = self.workspace.data_root / ".workspace.json"
            expected = {"workspaceId": self.workspace.workspace_id, "accountId": self.workspace.account_id,
                        "deviceId": self.workspace.device_id, "ownershipEpoch": self.workspace.ownership_epoch}
            if identity.exists() or identity.is_symlink():
                if strict_json(secure_read(identity, 4096)) != expected:
                    raise ValueError("WORKER_ROOT_SUBJECT_MISMATCH")
            else:
                if any(child.name != ".zhijun-worker.lock" for child in self.workspace.data_root.iterdir()):
                    raise ValueError("WORKER_UNASSIGNED_ROOT_NOT_EMPTY")
                marker = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(marker, "wb") as output:
                    output.write(json.dumps(expected, separators=(",", ":")).encode())
                    output.flush()
                    os.fsync(output.fileno())
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
