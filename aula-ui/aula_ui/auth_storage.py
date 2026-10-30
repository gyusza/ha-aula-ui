import fcntl
import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def atomic_write(path: Path, data: bytes):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Vault:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.directory = directory
        self._lock = (directory / ".instance.lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ValueError("Data directory is already in use. Each instance needs its own volume.") from None
        key = directory / "vault.key"
        try:
            if not key.exists():
                if (directory / "connection.enc").exists():
                    raise ValueError("vault.key is missing; restore it from backup, do not replace it.")
                atomic_write(key, Fernet.generate_key())
            os.chmod(key, 0o600)
            self.fernet = Fernet(key.read_bytes())
            self.path = directory / "connection.enc"
            self.read()
        except Exception:
            self.close()
            raise

    def read(self):
        if not self.path.exists():
            return None
        value = json.loads(self.fernet.decrypt(self.path.read_bytes()))
        if not isinstance(value, dict):
            raise ValueError("Invalid encrypted connection file.")
        return value

    def write(self, value):
        atomic_write(self.path, self.fernet.encrypt(json.dumps(value).encode()))

    def close(self):
        self._lock.close()
