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


async def main() -> int:
    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"Accept": "application/json"}) as c:
        run, card = seed()
        url = f"/runs/{run.id}/insights/{card.id}/input"

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
