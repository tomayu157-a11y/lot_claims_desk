"""Files a reviewer attaches with Add Input: what may be attached, how it is
read into Markdown, and how it is cut into sections the later agents can be
routed to.

Everything here works on bytes, never on a filesystem path, so where the
original is kept does not matter to it. The Markdown is reviewer context. It
is never evidence and is never cited.
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath, PureWindowsPath

from ..models import ReviewerFile, ReviewerFileSection

MAX_FILES_PER_INSIGHT = 2
MAX_FILE_BYTES = 1024 * 1024          # 1 MB per file
MAX_PDF_PAGES = 10
CHARS_PER_TOKEN = 4
MAX_CHARS = 20_000 * CHARS_PER_TOKEN  # ~20k tokens
EXTRACT_TIMEOUT_SECONDS = 30.0
SECTION_TARGET_CHARS = 2_000
SECTION_SPLIT_OVER_CHARS = 4_000

KINDS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md"}

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


class FileRejected(ValueError):
    """A file that cannot be attached. The message is shown to the reviewer."""


@dataclass
class Extraction:
    markdown: str
    pages: list[tuple[int | None, str]]   # (PDF page number or None, text), in order
    pages_read: int | None = None
    pages_total: int | None = None
    truncated: bool = False


def display_name(filename: str) -> str:
    """The file's own name without any client-side path. Display only."""
    return PureWindowsPath(filename or "").name.strip()[:200] or "file"


def kind_for(filename: str, data: bytes) -> str:
    """The file's kind, or FileRejected with the reason. Checks the content as
    well as the extension, so a renamed file is caught."""
    ext = PurePath(display_name(filename)).suffix.lower()
    if ext == ".doc":
        raise FileRejected("Word 97-2003 (.doc) files can't be read. "
                           "Save it as .docx or PDF and attach that.")
    kind = KINDS.get(ext)
    if kind is None:
        raise FileRejected("Only PDF, DOCX, TXT and MD files can be attached.")
    if len(data) > MAX_FILE_BYTES:
        raise FileRejected("This file is over 1 MB.")
    if not data:
        raise FileRejected("This file is empty.")
    if kind == "pdf" and not data.startswith(b"%PDF"):
        raise FileRejected("This file is not a valid PDF.")
    if kind == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                ok = "word/document.xml" in archive.namelist()
        except zipfile.BadZipFile:
            ok = False
        if not ok:
            raise FileRejected("This file is not a valid Word document.")
    if kind in ("txt", "md"):
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FileRejected("This file is not UTF-8 text.") from None
    return kind


def extract(kind: str, data: bytes) -> Extraction:
    """Read a file `kind_for` accepted into Markdown, capped at MAX_CHARS."""
    try:
        if kind == "pdf":
            pages, read, total = _pdf_pages(data)
        elif kind == "docx":
            pages, read, total = [(None, _docx_markdown(data))], None, None
        else:
            text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            pages, read, total = [(None, text)], None, None
    except FileRejected:
        raise
    except Exception as exc:  # any parser failure means unreadable
        raise FileRejected("This file could not be read.") from exc

    pages = [(page, text.strip()) for page, text in pages if text and text.strip()]
    if not pages:
        raise FileRejected("No readable text — this looks like a scanned document."
                           if kind == "pdf" else "This file has no readable text.")
    pages, truncated = _cap(pages, MAX_CHARS)
    return Extraction(markdown="\n\n".join(text for _, text in pages), pages=pages,
                      pages_read=read, pages_total=total, truncated=truncated)


async def extract_async(kind: str, data: bytes,
                        timeout: float = EXTRACT_TIMEOUT_SECONDS) -> Extraction:
    """`extract` off the event loop, so live run streams keep flowing. A read
    that times out is abandoned; its thread finishes in the background."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(extract, kind, data), timeout)
    except asyncio.TimeoutError:
        raise FileRejected("Reading this file took too long.") from None


def _pdf_pages(data: bytes) -> tuple[list[tuple[int | None, str]], int, int]:
    import pymupdf
    import pymupdf4llm

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass:
            raise FileRejected("This PDF is password-protected.")
        total = doc.page_count
        read = min(total, MAX_PDF_PAGES)
        chunks = pymupdf4llm.to_markdown(
            doc, pages=list(range(read)), page_chunks=True,
            ignore_images=True, ignore_graphics=True, show_progress=False,
        )
        pages = [
            (int((chunk.get("metadata") or {}).get("page_number") or i + 1),
             str(chunk.get("text") or ""))
            for i, chunk in enumerate(chunks)
        ]
        return pages, read, total
    finally:
        doc.close()


def _docx_markdown(data: bytes) -> str:
    from markitdown import MarkItDown, StreamInfo

    # A fresh converter per file: cheap to build, and never shared between
    # the worker threads of two concurrent submissions.
    result = MarkItDown(enable_plugins=False).convert_stream(
        io.BytesIO(data), stream_info=StreamInfo(extension=".docx"))
    return _IMAGE.sub("", result.markdown or "")


def _cap(pages: list[tuple[int | None, str]], limit: int) -> tuple[list[tuple[int | None, str]], bool]:
    """Keep text up to `limit` chars, cutting at the last paragraph break,
    else line break, else space before the limit."""
    kept: list[tuple[int | None, str]] = []
    used = 0
    for page, text in pages:
        sep = 2 if kept else 0
        room = limit - used - sep
        if len(text) <= room:
            kept.append((page, text))
            used += sep + len(text)
            continue
        cut = -1
        for mark in ("\n\n", "\n", " "):
            cut = text.rfind(mark, 0, room)
            if cut > 0:
                break
        if cut <= 0:
            cut = room
        if cut > 0:
            kept.append((page, text[:cut].rstrip()))
        return kept, True
    return kept, False


def build_reviewer_file(filename: str, kind: str, data: bytes, extraction: Extraction) -> ReviewerFile:
    """The stored record for one attached file. The caller sets storage_key."""
    return ReviewerFile(
        filename=display_name(filename), kind=kind, size_bytes=len(data),
        markdown=extraction.markdown, sections=split_sections(extraction.pages),
        pages_read=extraction.pages_read, pages_total=extraction.pages_total,
        truncated=extraction.truncated,
        token_estimate=len(extraction.markdown) // CHARS_PER_TOKEN,
    )


@dataclass
class _Para:
    page: int | None
    text: str
    heading: str | None = None       # set when this paragraph is a heading line


def split_sections(pages: list[tuple[int | None, str]]) -> list[ReviewerFileSection]:
    """Cut extracted text into routable sections.

    A file with headings splits on its #, ## and ### headings, and a section
    longer than SECTION_SPLIT_OVER_CHARS is cut again into parts. A file with
    no headings is cut into ~SECTION_TARGET_CHARS blocks on paragraph
    boundaries, labelled by page (PDF) or part number and opening line. Ids
    are s1, s2, ... in reading order, so the same file yields the same ids.
    """
    groups: list[tuple[str | None, int | None, list[_Para]]] = []
    for para in _paragraphs(pages):
        if para.heading is not None:
            groups.append((para.heading, para.page, []))
        else:
            if not groups:
                groups.append((None, para.page, []))
            groups[-1][2].append(para)

    out: list[tuple[str, int | None, str]] = []
    part = 0
    for heading, heading_page, paras in groups:
        if not paras:
            continue                 # a heading with nothing under it has nothing to route
        if heading is None:
            for page, text in _blocks(paras):
                part += 1
                out.append((_label(page, text, part), page, text))
            continue
        text = "\n\n".join(p.text for p in paras)
        if len(text) <= SECTION_TARGET_CHARS:
            out.append((heading, heading_page, text))
            continue
        for i, (page, block) in enumerate(_blocks(paras), start=1):
            out.append((f"{heading} (part {i})", page, block))
    return [ReviewerFileSection(id=f"s{i}", heading=h, text=t, page=p)
            for i, (h, p, t) in enumerate(out, start=1)]


def _paragraphs(pages: list[tuple[int | None, str]]) -> list[_Para]:
    out: list[_Para] = []
    for page, text in pages:
        for heading_block in re.split(r"(?m)(?=^#{1,3}\s+)", text):
            for block in re.split(r"\n\s*\n", heading_block):
                block = block.strip()
                if not block:
                    continue
                first, _, rest = block.partition("\n")
                match = _HEADING.match(first.strip())
                if match:
                    out.append(_Para(page, "", _clean_heading(match.group(2))))
                    if rest.strip():
                        out.append(_Para(page, rest.strip()))
                else:
                    out.append(_Para(page, block))
    return out


def _clean_heading(text: str) -> str:
    return " ".join(re.sub(r"[*_`]+", "", text).split())


def _blocks(paras: list[_Para]) -> list[tuple[int | None, str]]:
    """Group paragraphs into blocks of at most ~SECTION_TARGET_CHARS."""
    pieces: list[tuple[int | None, str]] = []
    for p in paras:
        if len(p.text) > SECTION_TARGET_CHARS:
            pieces += [(p.page, chunk) for chunk in _hard_split(p.text, SECTION_TARGET_CHARS)]
        else:
            pieces.append((p.page, p.text))
    blocks: list[tuple[int | None, str]] = []
    page: int | None = None
    buf: list[str] = []
    size = 0
    for piece_page, text in pieces:
        if buf and size + 2 + len(text) > SECTION_TARGET_CHARS:
            blocks.append((page, "\n\n".join(buf)))
            buf, size = [], 0
        if not buf:
            page = piece_page
        size += (2 if buf else 0) + len(text)
        buf.append(text)
    if buf:
        blocks.append((page, "\n\n".join(buf)))
    return blocks


def _hard_split(text: str, size: int) -> list[str]:
    out: list[str] = []
    while len(text) > size:
        cut = text.rfind(" ", 0, size)
        if cut <= 0:
            cut = size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _label(page: int | None, text: str, part: int) -> str:
    where = f"Page {page}" if page else f"Part {part}"
    return f"{where} — {_opening(text)}"


def _opening(text: str) -> str:
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    line = " ".join(re.sub(r"[#*_`>|]+", " ", line).split())
    return line if len(line) <= 60 else line[:60].rstrip() + "…"
