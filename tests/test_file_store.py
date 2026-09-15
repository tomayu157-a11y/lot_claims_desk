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
    check("no temporary file left behind", not any(p.suffix == ".part" for p in root.rglob("*")))
    fs.put(key, b"second")
    check("put overwrites", path.read_bytes() == b"second")
    fs.delete(key)
    check("delete removes the file", not path.exists())
    fs.delete(key)
    check("deleting a missing file is not an error", True)

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
