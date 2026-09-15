"""Where the original bytes of reviewer files are kept.

Everything else works on bytes and a storage key, never on a filesystem
path, so moving to S3 or Azure Blob later is one new class with the same two
methods. On Render the local root sits on the persistent disk, because it
lives under CELESTRA_DATA_DIR.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..settings import UPLOAD_DIR


class FileStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def delete(self, key: str) -> None: ...


def storage_key(run_id: str, insight_id: str, file_id: str, kind: str) -> str:
    """The key for one reviewer file. The reviewer's filename is never part of
    it, so a hostile name cannot choose where the bytes land."""
    return f"runs/{run_id}/insights/{insight_id}/{file_id}.{kind}"


class LocalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        path = (root / key).resolve()
        if root not in path.parents:
            raise ValueError(f"storage key escapes the store root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a reader never sees half a file.
        tmp = path.with_name(path.name + ".part")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


file_store: FileStore = LocalFileStore(UPLOAD_DIR)
