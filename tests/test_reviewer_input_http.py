"""Add Input with files over HTTP: attach, keep, remove, the 2-file cap,
all-or-nothing on a bad file, removal from the card, the locked document,
and export/import carrying the files."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from reviewer_fixtures import pdf_bytes
from starlette import formparsers
from starlette.requests import Request

import celestra.main as app_mod
import celestra.services.orchestrator as orch_mod
import celestra.store as store_mod
from celestra.models import Insight, ReviewAction, Run, RunConfig, RunStatus
from celestra.services.file_store import LocalFileStore
from celestra.store import Store

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="celestra_rf_http_"))
STORE = Store(TMP / "t.db")
UPLOADS = TMP / "uploads"
for _mod in (store_mod, orch_mod, app_mod):
    _mod.store = STORE
app_mod.file_store = LocalFileStore(UPLOADS)

PDF = ("policy.pdf", pdf_bytes(3), "application/pdf")
TXT = ("notes.txt", b"Use the 2024 SEER release.\n\nPrefer adults.", "text/plain")


def seed(status: RunStatus = RunStatus.AWAITING_REVIEW) -> tuple[Run, Insight]:
    run = Run(config=RunConfig(indication="Chronic Lymphocytic Leukemia", indication_key="CLL"),
              status=status)
    STORE.save_run(run)
    card = Insight(run_id=run.id, stage="stage_2", bucket="C", category="Treatment",
                   title="First-line regimens", summary="Venetoclax-based regimens lead first line.")
    STORE.save_insights(run.id, [card])
    return run, card


def stored() -> list[str]:
    return sorted(p.name for p in UPLOADS.rglob("*") if p.is_file()) if UPLOADS.exists() else []


def multipart_body(fields: list[tuple[str, bytes]],
                   files: list[tuple[str, str, bytes]]) -> tuple[str, bytes]:
    """Build a controlled multipart body without httpx adding a length header."""
    boundary = "reviewer-file-test-boundary"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value,
            b"\r\n",
        ])
    for field, filename, data in files:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            (f'Content-Disposition: form-data; name="{field}"; '
             f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n').encode(),
            data,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


async def post_raw_multipart(client: httpx.AsyncClient, url: str, body: bytes,
                             boundary: str, content_length: str | None = None) -> httpx.Response:
    request = client.build_request(
        "POST", url, content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if content_length is None:
        request.headers.pop("Content-Length", None)
    else:
        request.headers["Content-Length"] = content_length
    return await client.send(request)


def multipart_stream_request(body: bytes, boundary: str, chunk_size: int = 64 * 1024):
    """A source whose unread multipart chunks remain observable after abort."""
    chunks = [body[start:start + chunk_size] for start in range(0, len(body), chunk_size)]
    state = {"next": 0}

    async def receive() -> dict:
        index = state["next"]
        if index >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        state["next"] += 1
        return {
            "type": "http.request", "body": chunks[index],
            "more_body": state["next"] < len(chunks),
        }

    scope = {
        "type": "http", "method": "POST", "scheme": "http", "path": "/",
        "query_string": b"", "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
        ], "app": app_mod.app,
    }
    return Request(scope, receive), state, len(chunks)


class Upload:
    """The small UploadFile surface the commit helper needs for overlap tests."""
    def __init__(self, filename: str, data: bytes) -> None:
        self.filename, self.data = filename, data

    async def read(self, size: int = -1) -> bytes:
        return self.data if size < 0 else self.data[:size]


class FailingFileStore:
    """A FileStore double that fails selected deletes but retains real bytes."""
    def __init__(self, delegate: LocalFileStore, failing_keys: set[str]) -> None:
        self.delegate, self.failing_keys = delegate, failing_keys
        self.deleted: list[str] = []
        self.written: list[str] = []

    def get(self, key: str) -> bytes:
        return self.delegate.get(key)

    def put(self, key: str, data: bytes) -> None:
        self.written.append(key)
        self.delegate.put(key, data)

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        if key in self.failing_keys:
            raise OSError("forced delete failure")
        self.delegate.delete(key)


async def main() -> int:
    transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"Accept": "application/json"}) as c:
        run, card = seed()
        url = f"/runs/{run.id}/insights/{card.id}/input"

        print("\n== multipart parser limits do not trust Content-Length ==")
        boundary, oversized = multipart_body(
            [("user_input", b"x" * (app_mod._MAX_ATTACH_BODY + 1))], [],
        )
        r = await post_raw_multipart(c, url, oversized, boundary)
        check("an oversized body without Content-Length is refused", r.status_code == 413,
              f"HTTP {r.status_code}")
        boundary, normal = multipart_body([("user_input", b"x")], [])
        r = await post_raw_multipart(c, url, normal, boundary, "not-a-number")
        check("a malformed Content-Length cannot crash the route", r.status_code == 200,
              f"HTTP {r.status_code}")
        boundary, too_many_files = multipart_body(
            [("user_input", b"x")],
            [("files", "one.txt", b"one"), ("files", "two.txt", b"two"),
             ("files", "three.txt", b"three")],
        )
        r = await post_raw_multipart(c, url, too_many_files, boundary)
        check("more than two multipart file parts are refused while parsing",
              r.status_code == 400 and "Maximum number of files is 2" in r.text,
              f"HTTP {r.status_code} {r.text[:160]}")
        boundary, too_many_fields = multipart_body(
            [("user_input", b"x"), ("keep_file_ids", b"one"),
             ("keep_file_ids", b"two"), ("unexpected", b"x")], [],
        )
        r = await post_raw_multipart(c, url, too_many_fields, boundary)
        check("more than the three allowed multipart fields are refused while parsing",
              r.status_code == 400 and "Maximum number of fields is 3" in r.text,
              f"HTTP {r.status_code} {r.text[:160]}")
        boundary, oversized_field = multipart_body(
            [("user_input", b"x" * (256 * 1024 + 1))], [],
        )
        r = await post_raw_multipart(c, url, oversized_field, boundary)
        check("an excessive multipart field is refused while parsing",
              r.status_code == 400 and "Part exceeded maximum size" in r.text,
              f"HTTP {r.status_code} {r.text[:160]}")

        print("\n== a file over 1 MB is stopped before full spooling ==")
        one_and_a_half_megabytes = b"x" * (1536 * 1024)
        spooled_bytes = 0
        original_write = formparsers.UploadFile.write

        async def count_spooled_bytes(upload, data: bytes):
            nonlocal spooled_bytes
            spooled_bytes += len(data)
            return await original_write(upload, data)

        formparsers.UploadFile.write = count_spooled_bytes
        try:
            r = await c.post(url, data={"user_input": "x"}, files=[
                ("files", ("large.txt", one_and_a_half_megabytes, "text/plain")),
            ])
        finally:
            formparsers.UploadFile.write = original_write
        check("an over-1-MB file still receives the normal 400 response", r.status_code == 400,
              f"HTTP {r.status_code}")
        check("an over-1-MB file is never fully spooled",
              spooled_bytes <= app_mod.MAX_FILE_BYTES,
              f"spooled {spooled_bytes} bytes")

        boundary = "reviewer-file-stream-boundary"
        streamed_body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="files"; filename="large.txt"\r\n',
            b"Content-Type: text/plain\r\n\r\n",
            one_and_a_half_megabytes,
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="later"\r\n\r\n'.encode(),
            b"should-not-be-consumed",
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        request, stream_state, total_chunks = multipart_stream_request(streamed_body, boundary)
        try:
            await app_mod._bounded_attach_form(request)
            stream_rejected = False
        except app_mod._AttachError as exc:
            stream_rejected = exc.status == 400
        check("the parser aborts the streaming source at the first oversized file part", stream_rejected)
        check("later multipart chunks are not consumed after the oversized file abort",
              stream_state["next"] < total_chunks,
              f"consumed {stream_state['next']} of {total_chunks} chunks")

        print("\n== overlapping attachments preserve both files ==")
        concurrent_run, concurrent_card = seed()
        original_extract = app_mod.extract_async
        started = 0
        both_started = asyncio.Event()

        async def pause_after_stale_read(kind: str, data: bytes):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            return await original_extract(kind, data)

        app_mod.extract_async = pause_after_stale_read
        try:
            await asyncio.gather(
                app_mod._apply_input_with_files(
                    concurrent_run.id, concurrent_card.id, "first", [], [Upload("one.txt", b"one")],
                ),
                app_mod._apply_input_with_files(
                    concurrent_run.id, concurrent_card.id, "second", [], [Upload("two.txt", b"two")],
                ),
            )
        finally:
            app_mod.extract_async = original_extract
        concurrent = STORE.get_insight(concurrent_run.id, concurrent_card.id)
        check("overlapping attachments retain both file records",
              len(concurrent.reviewer_files) == 2,
              str([f.filename for f in concurrent.reviewer_files]))
        check("overlapping attachments retain both originals", len(stored()) == 2, str(stored()))
        for path in UPLOADS.rglob("*"):
            if path.is_file():
                path.unlink()

        print("\n== a concurrent card removal cannot be undone by an attachment ==")
        removal_run, removal_card = seed()
        await app_mod._apply_input_with_files(
            removal_run.id, removal_card.id, "old", [], [Upload("old.txt", b"old")],
        )
        old_file = STORE.get_insight(removal_run.id, removal_card.id).reviewer_files[0]
        original_extract = app_mod.extract_async
        extraction_started = asyncio.Event()
        continue_extraction = asyncio.Event()

        async def wait_for_removal(kind: str, data: bytes):
            extraction_started.set()
            await continue_extraction.wait()
            return await original_extract(kind, data)

        app_mod.extract_async = wait_for_removal
        try:
            attach = asyncio.create_task(app_mod._apply_input_with_files(
                removal_run.id, removal_card.id, "new", [old_file.id],
                [Upload("new.txt", b"new")],
            ))
            await extraction_started.wait()
            r = await c.post(
                f"/runs/{removal_run.id}/insights/{removal_card.id}/files/{old_file.id}/remove"
            )
            continue_extraction.set()
            await attach
        finally:
            app_mod.extract_async = original_extract
        after_removal = STORE.get_insight(removal_run.id, removal_card.id)
        check("a completed card removal is not reattached by overlapping input",
              r.status_code == 200 and [f.filename for f in after_removal.reviewer_files] == ["new.txt"],
              str([f.filename for f in after_removal.reviewer_files]))
        check("the concurrent removal does not leave stale file metadata",
              stored() == [after_removal.reviewer_files[0].storage_key.split("/")[-1]], str(stored()))
        for path in UPLOADS.rglob("*"):
            if path.is_file():
                path.unlink()

        print("\n== attach two files with the input ==")
        r = await c.post(url, data={"user_input": "Use the payer policy."},
                         files=[("files", PDF), ("files", TXT)])
        check("accepted", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}")
        check("returns the insight-card partial", '<article class="icard' in r.text)
        saved = STORE.get_insight(run.id, card.id)
        check("two files on the insight", len(saved.reviewer_files) == 2)
        check("input recorded as the decision", saved.review_action is ReviewAction.INPUT_ADDED
              and saved.reviewer_input == "Use the payer policy.")
        check("markdown extracted", "Section 1 Heading" in saved.reviewer_files[0].markdown)
        check("both originals stored", len(stored()) == 2, str(stored()))
        check("storage keys never use the reviewer's filename",
              all("policy" not in f.storage_key and "notes" not in f.storage_key
                  for f in saved.reviewer_files))
        pdf_id, txt_id = saved.reviewer_files[0].id, saved.reviewer_files[1].id

        print("\n== the dialog lists the attached files ==")
        r = await c.get(url, headers={"Accept": "text/html"})
        check("dialog renders with the file block", r.status_code == 200 and "data-reviewer-files" in r.text)
        check("  existing files shown as removable pills",
              "policy.pdf" in r.text and "data-existing" in r.text and "data-file-remove" in r.text)
        check("  size limit handed to the browser", 'data-max-bytes="1048576"' in r.text)

        print("\n== a third file is refused ==")
        r = await c.post(url, data={"user_input": "More.", "keep_file_ids": [pdf_id, txt_id]},
                         files=[("files", TXT)])
        check("over the cap is refused", r.status_code == 400 and "2 files" in r.json()["detail"],
              f"HTTP {r.status_code}")
        check("  nothing changed", len(STORE.get_insight(run.id, card.id).reviewer_files) == 2
              and len(stored()) == 2)

        print("\n== a bad file saves nothing ==")
        before = stored()
        r = await c.post(url, data={"user_input": "Swap files.", "keep_file_ids": [pdf_id]},
                         files=[("files", ("scan.pdf", pdf_bytes(2, text=False), "application/pdf"))])
        check("unreadable file is 422", r.status_code == 422, f"HTTP {r.status_code}")
        body = r.json() if r.status_code == 422 else {}
        check("  reason names the file", bool(body.get("files")) and body["files"][0]["name"] == "scan.pdf"
              and "scanned" in body["files"][0]["error"], str(body))
        after = STORE.get_insight(run.id, card.id)
        check("  insight unchanged", len(after.reviewer_files) == 2
              and after.reviewer_input == "Use the payer policy.")
        check("  no bytes stored or deleted", stored() == before)

        print("\n== type, size and text are checked on the server ==")
        r = await c.post(url, data={"user_input": "x", "keep_file_ids": [pdf_id]},
                         files=[("files", ("old.doc", b"\xd0\xcf\x11\xe0", "application/msword"))])
        check(".doc refused with the save-as advice",
              r.status_code == 400 and ".docx or PDF" in r.json()["files"][0]["error"], r.text[:200])
        r = await c.post(url, data={"user_input": "x", "keep_file_ids": [pdf_id]},
                         files=[("files", ("big.txt", b"a" * 1_500_000, "text/plain"))])
        check("over 1 MB refused", r.status_code == 400 and "over 1 MB" in r.json()["files"][0]["error"],
              r.text[:200])
        r = await c.post(url, data={"user_input": "x"},
                         files=[("files", ("huge.txt", b"a" * 3_000_000, "text/plain"))])
        check("an oversized request is refused before it is read", r.status_code == 413,
              f"HTTP {r.status_code}")
        r = await c.post(url, data={"user_input": "  ", "keep_file_ids": [pdf_id, txt_id]})
        check("files without text are refused", r.status_code == 400, f"HTTP {r.status_code}")

        print("\n== resubmitting keeps and removes ==")
        r = await c.post(url, data={"user_input": "Only the policy now.", "keep_file_ids": [pdf_id]},
                         files=[("files", ("", b"", "application/octet-stream"))])
        after = STORE.get_insight(run.id, card.id)
        check("unlisted file removed", r.status_code == 200
              and [f.id for f in after.reviewer_files] == [pdf_id], f"HTTP {r.status_code}")
        check("  its bytes deleted", len(stored()) == 1, str(stored()))
        check("  text replaced", after.reviewer_input == "Only the policy now.")

        print("\n== typed input over JSON keeps the files ==")
        r = await c.post(url, json={"user_input": "Plain text update."})
        after = STORE.get_insight(run.id, card.id)
        check("JSON input still works", r.status_code == 200 and after.reviewer_input == "Plain text update.")
        check("  files untouched", [f.id for f in after.reviewer_files] == [pdf_id])

        print("\n== export and import carry the files ==")
        bundle = (await c.get(f"/runs/{run.id}/export")).json()
        exported = bundle["insights"][0]["reviewer_files"]
        check("export includes the file and its markdown",
              exported and exported[0]["filename"] == "policy.pdf" and exported[0]["markdown"])
        STORE.save_insights(run.id, [after.model_copy(update={"reviewer_files": []})])
        r = await c.post("/projects/import",
                         files={"bundle": ("p.json", json.dumps(bundle).encode(), "application/json")})
        check("import restores the file", r.status_code == 303
              and [f.id for f in STORE.get_insight(run.id, card.id).reviewer_files] == [pdf_id],
              f"HTTP {r.status_code}")

        print("\n== failed deletion keeps the attachment recoverable ==")
        before_direct = STORE.get_insight(run.id, card.id)
        original_store = app_mod.file_store
        direct_failures = FailingFileStore(original_store, {before_direct.reviewer_files[0].storage_key})
        app_mod.file_store = direct_failures
        try:
            r = await c.post(f"/runs/{run.id}/insights/{card.id}/files/{pdf_id}/remove")
        finally:
            app_mod.file_store = original_store
        direct_after = STORE.get_insight(run.id, card.id)
        check("a direct delete failure returns an error instead of a card", r.status_code == 500,
              f"HTTP {r.status_code}")
        check("a direct delete failure keeps its file metadata",
              [f.id for f in direct_after.reviewer_files] == [pdf_id])
        check("a direct delete failure keeps its original bytes", stored() == [
            before_direct.reviewer_files[0].storage_key.split("/")[-1],
        ], str(stored()))
        STORE.save_insights(run.id, [before_direct])

        print("\n== failed replacement deletion rolls back the whole Attach ==")
        before_replace = STORE.get_insight(run.id, card.id)
        replacement_failures = FailingFileStore(
            original_store, {before_replace.reviewer_files[0].storage_key},
        )
        app_mod.file_store = replacement_failures
        try:
            r = await c.post(url, data={"user_input": "replace", "keep_file_ids": []},
                             files=[("files", ("replacement.txt", b"replacement", "text/plain"))])
        finally:
            app_mod.file_store = original_store
        replacement_after = STORE.get_insight(run.id, card.id)
        check("a replacement delete failure returns an error instead of a card", r.status_code == 500,
              f"HTTP {r.status_code}")
        check("a replacement delete failure keeps prior metadata",
              [f.id for f in replacement_after.reviewer_files] == [pdf_id])
        check("a replacement delete failure removes newly written originals",
              stored() == [before_replace.reviewer_files[0].storage_key.split("/")[-1]], str(stored()))
        STORE.save_insights(run.id, [before_replace])
        for key in replacement_failures.written:
            original_store.delete(key)

        print("\n== metadata save failure restores deleted originals ==")
        before_direct_save = STORE.get_insight(run.id, card.id)
        direct_save_store = FailingFileStore(original_store, set())
        original_save_insights = STORE.save_insights
        direct_save_attempts = 0

        def fail_direct_save_once(run_id: str, items) -> None:
            nonlocal direct_save_attempts
            direct_save_attempts += 1
            if direct_save_attempts == 1:
                raise OSError("forced metadata save failure")
            original_save_insights(run_id, items)

        STORE.save_insights = fail_direct_save_once
        app_mod.file_store = direct_save_store
        try:
            r = await c.post(f"/runs/{run.id}/insights/{card.id}/files/{pdf_id}/remove")
        finally:
            STORE.save_insights = original_save_insights
            app_mod.file_store = original_store
        direct_save_after = STORE.get_insight(run.id, card.id)
        check("a direct metadata save failure returns an error", r.status_code == 500,
              f"HTTP {r.status_code}")
        check("a direct metadata save failure restores the deleted original",
              before_direct_save.reviewer_files[0].storage_key in direct_save_store.deleted
              and stored() == [before_direct_save.reviewer_files[0].storage_key.split("/")[-1]],
              str(direct_save_store.deleted))
        check("a direct metadata save failure keeps prior metadata",
              [f.id for f in direct_save_after.reviewer_files] == [pdf_id])

        before_replace_save = STORE.get_insight(run.id, card.id)
        replacement_save_store = FailingFileStore(original_store, set())
        replacement_save_attempts = 0

        def fail_replacement_save_once(run_id: str, items) -> None:
            nonlocal replacement_save_attempts
            replacement_save_attempts += 1
            if replacement_save_attempts == 1:
                raise OSError("forced metadata save failure")
            original_save_insights(run_id, items)

        STORE.save_insights = fail_replacement_save_once
        app_mod.file_store = replacement_save_store
        try:
            r = await c.post(url, data={"user_input": "replace", "keep_file_ids": []},
                             files=[("files", ("replacement.txt", b"replacement", "text/plain"))])
        finally:
            STORE.save_insights = original_save_insights
            app_mod.file_store = original_store
        replacement_save_after = STORE.get_insight(run.id, card.id)
        check("a replacement metadata save failure returns an error", r.status_code == 500,
              f"HTTP {r.status_code}")
        check("a replacement metadata save failure restores old bytes and removes new bytes",
              before_replace_save.reviewer_files[0].storage_key in replacement_save_store.deleted
              and stored() == [before_replace_save.reviewer_files[0].storage_key.split("/")[-1]],
              str(replacement_save_store.deleted))
        check("a replacement metadata save failure keeps prior metadata",
              [f.id for f in replacement_save_after.reviewer_files] == [pdf_id])

        print("\n== remove from the card ==")
        r = await c.post(f"/runs/{run.id}/insights/{card.id}/files/{pdf_id}/remove")
        after = STORE.get_insight(run.id, card.id)
        check("removed", r.status_code == 200 and after.reviewer_files == [], f"HTTP {r.status_code}")
        check("  card no longer names it", "policy.pdf" not in r.text)
        check("  bytes deleted", stored() == [], str(stored()))
        check("  decision kept", after.review_action is ReviewAction.INPUT_ADDED
              and after.reviewer_input == "Plain text update.")
        r = await c.post(f"/runs/{run.id}/insights/{card.id}/files/rf_missing/remove")
        check("unknown file is 404", r.status_code == 404, f"HTTP {r.status_code}")

        print("\n== a locked document refuses both ==")
        lrun, lcard = seed(RunStatus.APPROVED)
        r = await c.post(f"/runs/{lrun.id}/insights/{lcard.id}/input",
                         data={"user_input": "x"}, files=[("files", TXT)])
        check("attach refused", r.status_code == 409, f"HTTP {r.status_code}")
        r = await c.post(f"/runs/{lrun.id}/insights/{lcard.id}/files/rf_x/remove")
        check("remove refused", r.status_code == 409, f"HTTP {r.status_code}")
        check("  nothing stored", stored() == [])

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
