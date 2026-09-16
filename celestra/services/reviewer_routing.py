"""Which parts of the reviewer's files each research question receives.

A reviewer's files can run to tens of thousands of words, and the agents
that run afterwards make several model calls per question. Sending every
file to every call would bury the sources. So when an agent starts, one
model call reads its planned questions against an outline of each file and
names, per question, the sections that bear on it. Only those sections
travel, within a per-question ceiling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import Insight, ResearchQuestion, ReviewerFile, ReviewerFileSection
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.reviewer_routing")

PROFILE_OPENING_CHARS = 8_000

_SYSTEM = (
    "You decide which parts of reviewer-supplied files bear on which clinical research "
    "questions. A section bears on a question only when its content would change, narrow "
    "or focus the answer to that question. You use only the section ids listed. You never "
    "invent an id."
)


@dataclass
class AttachedFile:
    """A reviewer file with the finding it was attached to."""
    file: ReviewerFile
    card_title: str
    card_input: str


def attached_files(insights: list[Insight]) -> list[AttachedFile]:
    return [AttachedFile(f, i.title, i.reviewer_input) for i in insights for f in i.reviewer_files]


def routing_available() -> bool:
    return llm.available


def section_ref(file: ReviewerFile, section: ReviewerFileSection) -> str:
    return f"{file.id}.{section.id}"


def _first_line(text: str) -> str:
    return next((ln.strip()[:120] for ln in text.splitlines() if ln.strip()), "")


def _profile(item: AttachedFile) -> str:
    f = item.file
    outline = "\n".join(
        f"- {section_ref(f, s)} | {s.heading} | {len(s.text)} chars | {_first_line(s.text)}"
        for s in f.sections
    )
    return (
        f"=== FILE {f.id}: {f.filename} ===\n"
        f"Attached to finding: {item.card_title}\n"
        f"Reviewer's note on that finding: {item.card_input or '(none)'}\n"
        f"Outline (section id | heading | size | first line):\n{outline}\n"
        f"Opening text:\n{f.markdown[:PROFILE_OPENING_CHARS]}"
    )


async def route_for_agent(
    agent_name: str, objective: str, questions: list[ResearchQuestion],
    files: list[AttachedFile],
) -> dict[str, list[str]] | None:
    """Per question id, the section refs that bear on it, most important
    first. None when the routing call failed; the caller then sends no files."""
    if not questions or not files:
        return {}
    known_questions = {q.id for q in questions}
    known_refs = {section_ref(a.file, s) for a in files for s in a.file.sections}
    prompt = (
        f"Agent: {agent_name}. Objective: {objective}\n\n"
        "Research questions:\n" + "\n".join(f"- {q.id}: {q.text}" for q in questions)
        + "\n\nReviewer files:\n\n" + "\n\n".join(_profile(a) for a in files)
        + '\n\nReturn JSON: {"routes": [{"question_id": str, "sections": [str]}]}. '
        "List a question only when at least one section bears on it. Order each "
        "question's sections most important first."
    )
    try:
        result = await llm.complete_json(_SYSTEM, prompt, max_tokens=2000)
    except LLMUnavailable:
        return None
    except Exception:  # A failed routing call must not fail the agent.
        log.warning("reviewer file routing failed", exc_info=True)
        return None
    if not isinstance(result, dict) or not isinstance(result.get("routes"), list):
        return None
    routes: dict[str, list[str]] = {}
    for row in result["routes"]:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("question_id", ""))
        if qid not in known_questions:
            continue
        refs = routes.setdefault(qid, [])
        for ref in row.get("sections") or []:
            ref = str(ref).strip()
            if ref in known_refs and ref not in refs:
                refs.append(ref)
    return {qid: refs for qid, refs in routes.items() if refs}


def _lookup(files: list[AttachedFile]) -> dict[str, tuple[AttachedFile, ReviewerFileSection]]:
    return {section_ref(a.file, s): (a, s) for a in files for s in a.file.sections}


def apply_ceiling(refs: list[str], files: list[AttachedFile],
                  max_chars: int) -> tuple[list[str], list[str]]:
    """Keep refs in priority order until the next would pass max_chars. The
    rest are returned as dropped."""
    lookup = _lookup(files)
    kept: list[str] = []
    used = 0
    for i, ref in enumerate(refs):
        if ref not in lookup:
            continue
        section = lookup[ref][1]
        size = len(section.heading) + len(section.text)
        if used + size > max_chars:
            return kept, [r for r in refs[i:] if r in lookup]
        kept.append(ref)
        used += size
    return kept, []


def documents_for(refs: list[str], files: list[AttachedFile]) -> list[dict]:
    """The routed sections grouped by file, in the shape answer_batch renders."""
    lookup = _lookup(files)
    docs: dict[str, dict] = {}
    for ref in refs:
        if ref not in lookup:
            continue
        item, section = lookup[ref]
        doc = docs.setdefault(item.file.id, {
            "filename": item.file.filename, "card": item.card_title, "sections": [],
        })
        doc["sections"].append({"heading": section.heading, "text": section.text})
    return list(docs.values())
