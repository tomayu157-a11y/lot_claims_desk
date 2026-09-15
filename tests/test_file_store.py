"""Local storage for reviewer files: put, delete, nested keys, and no way
for a key to escape the store root."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.services.file_store import LocalFileStore, storage_key

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="celestra_fs_"))
    fs = LocalFileStore(root)

    print("\n== keys ==")
    key = storage_key("run_1", "ins_2", "rf_3", "pdf")
    check("key layout", key == "runs/run_1/insights/ins_2/rf_3.pdf", key)

    print("\n== put and delete ==")
    fs.put(key, b"%PDF-1.7 hello")
    path = root / "runs" / "run_1" / "insights" / "ins_2" / "rf_3.pdf"
    check("bytes written under nested directories", path.read_bytes() == b"%PDF-1.7 hello")
    try:
        original = fs.get(key)
    except AttributeError:
        original = None
    check("get returns the stored bytes for lifecycle compensation", original == b"%PDF-1.7 hello")
    check("no temporary file left behind", not any(p.suffix == ".part" for p in root.rglob("*")))
    fs.put(key, b"second")
    check("put overwrites", path.read_bytes() == b"second")
    fs.delete(key)
    check("delete removes the file", not path.exists())
    fs.delete(key)
    check("deleting a missing file is not an error", True)

    print("\n== failed writes are cleaned up ==")
    fs.put(key, b"original")
    tmp = path.with_name(path.name + ".part")
    path_type = type(tmp)
    original_write_bytes = path_type.write_bytes

    def write_then_fail(self: Path, data: bytes) -> int:
        original_write_bytes(self, data)
        raise OSError("forced write failure")

    path_type.write_bytes = write_then_fail
    try:
        fs.put(key, b"replacement")
        write_failed = False
    except OSError:
        write_failed = True
    finally:
        path_type.write_bytes = original_write_bytes
    check("a failed write is raised", write_failed)
    check("a failed write keeps the original file", path.read_bytes() == b"original")
    check("a failed write leaves no temporary file", not tmp.exists())

    print("\n== failed cleanup keeps the storage error ==")
    original_unlink = path_type.unlink
    cleanup_attempts = 0

    def fail_first_cleanup(self: Path, missing_ok: bool = False) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise PermissionError("temporary file is still locked")
        original_unlink(self, missing_ok=missing_ok)

    path_type.write_bytes = write_then_fail
    path_type.unlink = fail_first_cleanup
    try:
        fs.put(key, b"replacement")
        retry_error: OSError | None = None
    except OSError as exc:
        retry_error = exc
    finally:
        path_type.write_bytes = original_write_bytes
        path_type.unlink = original_unlink
    check("a transient cleanup failure keeps the write error",
          retry_error is not None and str(retry_error) == "forced write failure", str(retry_error))
    check("a transient cleanup failure is retried", cleanup_attempts == 2, str(cleanup_attempts))
    check("a retried cleanup removes the temporary file", not tmp.exists())

    cleanup_attempts = 0

    def fail_every_cleanup(self: Path, missing_ok: bool = False) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        raise PermissionError("temporary file remains locked")

    path_type.write_bytes = write_then_fail
    path_type.unlink = fail_every_cleanup
    try:
        fs.put(key, b"replacement")
        permanent_error: OSError | None = None
    except OSError as exc:
        permanent_error = exc
    finally:
        path_type.write_bytes = original_write_bytes
        path_type.unlink = original_unlink
    check("a permanent cleanup failure keeps the write error",
          permanent_error is not None and str(permanent_error) == "forced write failure",
          str(permanent_error))
    check("a permanent cleanup failure is attached for diagnostics",
          isinstance(permanent_error.__cause__ if permanent_error else None, PermissionError),
          repr(permanent_error.__cause__ if permanent_error else None))
    check("a permanently locked temporary file remains visible", tmp.exists())
    original_unlink(tmp, missing_ok=True)

    print("\n== control-flow interruptions bypass cleanup ==")

    def write_then_interrupt(self: Path, data: bytes) -> int:
        original_write_bytes(self, data)
        raise KeyboardInterrupt("forced interruption")

    path_type.write_bytes = write_then_interrupt
    try:
        fs.put(key, b"replacement")
        interruption: KeyboardInterrupt | None = None
    except KeyboardInterrupt as exc:
        interruption = exc
    finally:
        path_type.write_bytes = original_write_bytes
    check("an interruption is propagated", interruption is not None, repr(interruption))
    check("an interruption does not enter temporary-file cleanup", tmp.exists())
    original_unlink(tmp, missing_ok=True)

    original_replace = path_type.replace

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("forced replace failure")

    path_type.replace = fail_replace
    try:
        fs.put(key, b"replacement")
        replace_failed = False
    except OSError:
        replace_failed = True
    finally:
        path_type.replace = original_replace
    check("a failed replace is raised", replace_failed)
    check("a failed replace keeps the original file", path.read_bytes() == b"original")
    check("a failed replace leaves no temporary file", not tmp.exists())

    print("\n== keys cannot escape the root ==")
    for bad in ("../outside.txt", "runs/../../outside.txt", "/etc/passwd"):
        try:
            fs.put(bad, b"x")
            escaped = True
        except ValueError:
            escaped = False
        check(f"refused {bad!r}", not escaped)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
