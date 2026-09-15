"""Reviewer files: what may be attached, and how each kind is read into
Markdown within the page and token caps."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reviewer_fixtures import docx_bytes, pdf_bytes

from celestra.services import reviewer_files as rf
from celestra.services.reviewer_files import FileRejected, extract, extract_async, kind_for

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def rejected(name: str, data: bytes) -> str:
    try:
        kind_for(name, data)
    except FileRejected as exc:
        return str(exc)
    return ""


def extract_error(kind: str, data: bytes) -> str:
    try:
        extract(kind, data)
    except FileRejected as exc:
        return str(exc)
    return ""


def main() -> int:
    print("\n== what may be attached ==")
    check(".doc gets the save-as advice", ".docx or PDF" in rejected("notes.doc", b"\xd0\xcf\x11\xe0"))
    check("other types refused", "Only PDF, DOCX, TXT and MD" in rejected("a.exe", b"MZ"))
    check("over 1 MB refused", "over 1 MB" in rejected("big.txt", b"a" * (rf.MAX_FILE_BYTES + 1)))
    check("exactly 1 MB accepted", rejected("ok.txt", b"a" * rf.MAX_FILE_BYTES) == "")
    check("empty refused", "empty" in rejected("empty.txt", b""))
    check("renamed non-PDF refused", "not a valid PDF" in rejected("fake.pdf", b"hello"))
    check("renamed non-DOCX refused", "not a valid Word" in rejected("fake.docx", b"not a zip"))
    check("non-UTF-8 text refused", "not UTF-8" in rejected("latin.txt", "café".encode("latin-1")))
    check("pdf by content and extension", kind_for("REPORT.PDF", pdf_bytes(1)) == "pdf")
    check("docx", kind_for("policy.docx", docx_bytes()) == "docx")
    check("md", kind_for("notes.md", b"# Hi") == "md")
    check("client path stripped", kind_for(r"C:\fakepath\a.txt", b"hi") == "txt")
    check("display name drops the path", rf.display_name(r"C:\fakepath\Payer policy.pdf") == "Payer policy.pdf")

    print("\n== PDF: first 10 pages, headings kept ==")
    ex = extract("pdf", pdf_bytes(12))
    check("10 of 12 pages read", ex.pages_read == 10 and ex.pages_total == 12, f"{ex.pages_read}/{ex.pages_total}")
    check("page 10 present, page 11 not", "Section 10 Heading" in ex.markdown and "Section 11" not in ex.markdown)
    check("headings become Markdown headings", "# Section 1 Heading" in ex.markdown)
    check("pages carry their numbers", ex.pages[0][0] == 1 and ex.pages[-1][0] == 10, str([p for p, _ in ex.pages]))
    check("not truncated", ex.truncated is False)
    check("textless PDF reads as scanned", "scanned" in extract_error("pdf", pdf_bytes(2, text=False)))
    check("password-protected PDF refused", "password-protected" in extract_error("pdf", pdf_bytes(1, password="pw")))
    check("corrupt PDF refused", "could not be read" in extract_error("pdf", b"%PDF-1.4 garbage"))

    print("\n== DOCX: structure kept, images dropped ==")
    ex = extract("docx", docx_bytes())
    check("heading levels kept", "# Payer policy" in ex.markdown and "## Step therapy" in ex.markdown, ex.markdown[:200])
    check("table kept", "| VenO | 1L |" in ex.markdown)
    check("no image data", "data:image" not in ex.markdown and "![" not in ex.markdown)
    check("no page counts for DOCX", ex.pages_read is None and ex.pages_total is None)

    print("\n== TXT and MD: as written ==")
    check("line endings normalised", extract("txt", b"Line one\r\nLine two").markdown == "Line one\nLine two")
    check("markdown verbatim", extract("md", b"# Title\n\nBody").markdown == "# Title\n\nBody")
    check("whitespace-only text refused", "no readable text" in extract_error("txt", b" \n\n \t"))

    print("\n== the ~20k-token cap ==")
    para = " ".join(["word"] * 199) + " end."
    ex = extract("txt", "\n\n".join([para] * 120).encode())
    check("capped at 80,000 chars", len(ex.markdown) <= rf.MAX_CHARS, str(len(ex.markdown)))
    check("marked truncated", ex.truncated is True)
    check("cut at a paragraph boundary", ex.markdown.endswith("end."))
    ex = extract("txt", ("x" * 100_000).encode())
    check("one unbroken block is still capped", 0 < len(ex.markdown) <= rf.MAX_CHARS and ex.truncated)

    print("\n== a slow read is abandoned ==")
    original = rf.extract
    rf.extract = lambda kind, data: (time.sleep(0.5), None)[1]
    try:
        asyncio.run(extract_async("txt", b"x", timeout=0.05))
        msg = ""
    except FileRejected as exc:
        msg = str(exc)
    finally:
        rf.extract = original
    check("timeout gives a reason", "took too long" in msg, msg)
    ok = asyncio.run(extract_async("txt", b"hello"))
    check("async path returns the extraction", ok.markdown == "hello")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
