"""Reviewer files are cut into routable sections: by heading where the file
has headings, by ~2,000-char blocks where it does not, with stable ids."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.services.reviewer_files import (
    SECTION_TARGET_CHARS,
    Extraction,
    build_reviewer_file,
    split_sections,
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def para(word: str, n: int) -> str:
    return " ".join([word] * n)


def main() -> int:
    print("\n== headed Markdown ==")
    md = ("Intro line before any heading.\n\n# Scope\n\nCovers adults.\n\n"
          "## Step therapy\n\nBTK first.\n\n### Exceptions\n\nNone.")
    secs = split_sections([(None, md)])
    check("headings become sections",
          [s.heading for s in secs] == ["Part 1 — Intro line before any heading.", "Scope",
                                        "Step therapy", "Exceptions"], str([s.heading for s in secs]))
    check("ids in reading order", [s.id for s in secs] == ["s1", "s2", "s3", "s4"])
    check("section text is the body", secs[1].text == "Covers adults.")
    check("markup stripped from headings",
          split_sections([(None, "## **Bold** heading ##\n\nText.")])[0].heading == "Bold heading")
    check("a heading with nothing under it is skipped",
          [s.heading for s in split_sections([(None, "# Empty\n\n# Full\n\nText.")])] == ["Full"])

    print("\n== a long heading section is cut into parts ==")
    big = "# Big\n\n" + "\n\n".join([para("alpha", 100)] * 12)
    secs = split_sections([(None, big)])
    check("parts are labelled", len(secs) >= 3 and all(s.heading.startswith("Big (part ") for s in secs),
          str([s.heading for s in secs]))
    check("parts stay near the target", max(len(s.text) for s in secs) <= SECTION_TARGET_CHARS,
          str(max(len(s.text) for s in secs)))
    secs = split_sections([(None, "# Short big\n\n" + "a" * 3001)])
    check("a 3,001-char headed paragraph is split to the target",
          len(secs) == 2 and all(len(s.text) <= SECTION_TARGET_CHARS for s in secs),
          str([len(s.text) for s in secs]))

    print("\n== no headings: blocks ==")
    flat = "\n\n".join([para("beta", 80)] * 20)
    secs = split_sections([(None, flat)])
    check("blocks labelled Part n", all(s.heading.startswith(f"Part {i} — ") for i, s in enumerate(secs, 1)))
    check("blocks within target", all(len(s.text) <= SECTION_TARGET_CHARS for s in secs))
    check("nothing lost", sum(s.text.count("beta") for s in secs) == 80 * 20)
    giant = "x" * 9000
    secs = split_sections([(None, giant)])
    check("an unbroken block is hard-split", all(len(s.text) <= SECTION_TARGET_CHARS for s in secs)
          and "".join(s.text for s in secs) == giant)
    secs = split_sections([(None, "b" * 3001)])
    check("a 3,001-char flat paragraph is split to the target",
          len(secs) == 2 and all(len(s.text) <= SECTION_TARGET_CHARS for s in secs),
          str([len(s.text) for s in secs]))

    print("\n== headings do not require blank lines ==")
    secs = split_sections([(None, "Intro text.\n# Heading\n\nBody.")])
    check("a heading after text starts its own section",
          [s.heading for s in secs] == ["Part 1 — Intro text.", "Heading"],
          str([s.heading for s in secs]))

    print("\n== PDF pages ==")
    pages = [(1, "A" * 1500), (2, "B" * 1500), (3, "C" * 1500)]
    secs = split_sections(pages)
    check("blocks labelled by page", [s.heading.split(" — ")[0] for s in secs] == ["Page 1", "Page 2", "Page 3"],
          str([s.heading for s in secs]))
    check("page recorded", [s.page for s in secs] == [1, 2, 3])
    headed = [(1, "# Section 1 Heading\n\nBody one."), (2, "# Section 2 Heading\n\nBody two.")]
    secs = split_sections(headed)
    check("PDF headings used", [s.heading for s in secs] == ["Section 1 Heading", "Section 2 Heading"])
    check("heading page recorded", [s.page for s in secs] == [1, 2])

    print("\n== stable ==")
    a = split_sections([(None, md)])
    b = split_sections([(None, md)])
    check("same input, same sections", [(s.id, s.heading, s.text) for s in a] == [(s.id, s.heading, s.text) for s in b])

    print("\n== the stored record ==")
    ex = Extraction(markdown="# Scope\n\nCovers adults.", pages=[(1, "# Scope\n\nCovers adults.")],
                    pages_read=1, pages_total=3, truncated=False)
    rec = build_reviewer_file(r"C:\fakepath\Payer policy.pdf", "pdf", b"12345", ex)
    check("display name only", rec.filename == "Payer policy.pdf")
    check("size is the upload's", rec.size_bytes == 5)
    check("token estimate is chars / 4", rec.token_estimate == len(ex.markdown) // 4)
    check("sections built", [s.heading for s in rec.sections] == ["Scope"])
    check("page counts copied", rec.pages_read == 1 and rec.pages_total == 3)
    check("storage key left for the caller", rec.storage_key == "")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
