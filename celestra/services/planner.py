"""Turns seed questions into scoped research questions.

The seed list in research_questions.yaml is deliberately short. The planner
widens each seed into a question carrying the population, geography and
cutoff, plus the sub-aspects that the sufficiency check later measures
coverage against. With an LLM configured this is model-written; without one it
is templated, which is weaker but never wrong.
"""
from __future__ import annotations

import logging

from ..models import ResearchQuestion, RunConfig
from ..settings import get_framework, get_questions, get_thresholds
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.planner")

_SYSTEM = (
    "You are a clinical desk-research planner for US claims analytics. You turn a broad "
    "seed question into one precise, answerable research question, scoped to the named "
    "population and geography, plus the specific sub-aspects that a complete answer must "
    "cover. You never invent clinical facts; you only shape the question."
)


def _stage_indices() -> list[str]:
    return [f"stage_{i}" for i in range(1, 7)]


def bucket_for_stage(stage: str) -> str:
    fw = get_framework()
    for letter, spec in fw["buckets"].items():
        if stage in (spec.get("stages") or []):
            return letter
    return "F"


def seeds_for(indication_key: str, stage: str) -> list[str]:
    cfg = get_questions()["indications"].get(indication_key)
    if not cfg:
        return []
    return list(cfg.get(stage) or [])


def indication_config(indication_key: str) -> dict:
    return get_questions()["indications"].get(indication_key, {})


def _fallback_aspects(seed: str) -> list[str]:
    """Split a seed question into aspects without a model. Uses the question's
    own enumerations, which the seed list conveniently already contains."""
    import re

    inner = re.findall(r"\(([^)]+)\)", seed)
    parts: list[str] = []
    for group in inner:
        parts += [p.strip() for p in re.split(r",| or | and ", group) if len(p.strip()) > 2]
    if parts:
        return parts[:6]
    stripped = re.sub(r"^(what|which|how|where)\b\s*", "", seed.strip(" ?"), flags=re.I)
    return [stripped[:120]]


def _scoped(seed: str, cfg: RunConfig) -> str:
    population = cfg.target_population or "adult patients"
    if cfg.geography.lower() not in seed.lower():
        seed = seed.rstrip("?") + f" in {cfg.geography}?"
    return f"{seed.rstrip('?')} ({population}, as of {cfg.research_cutoff})?"


async def plan_stage(
    run_id: str, cfg: RunConfig, stage: str
) -> list[ResearchQuestion]:
    limits = get_thresholds()["limits"]
    seeds = seeds_for(cfg.indication_key, stage)[: limits["max_questions_per_stage"]]
    bucket = bucket_for_stage(stage)
    meta = get_questions()["stage_meta"][stage]

    if not seeds:
        return []

    planned: list[ResearchQuestion] = []
    if llm.available:
        try:
            payload = {
                "indication": cfg.indication,
                "population": cfg.target_population or "adults",
                "geography": cfg.geography,
                "objective": cfg.objective,
                "additional_context": cfg.additional_context,
                "research_cutoff": cfg.research_cutoff,
                "stage_name": meta["name"],
                "stage_core_question": meta["core_question"],
                "expected_output": meta["expected_output"],
                "seed_questions": seeds,
            }
            result = await llm.complete_json(
                _SYSTEM,
                "Expand each seed question into a scoped research question.\n"
                "Return JSON: [{\"seed\": str, \"question\": str, \"aspects\": [str]}]. "
                "aspects are 2-6 short noun phrases naming what a complete answer must "
                "cover, used later to score evidence coverage.\n\n"
                f"{payload}",
            )
            for item in result or []:
                seed = str(item.get("seed") or "")
                text = str(item.get("question") or "").strip()
                if not text:
                    continue
                planned.append(
                    ResearchQuestion(
                        run_id=run_id, stage=stage, bucket=bucket, text=text,
                        seed_text=seed or text,
                        aspects=[str(a) for a in (item.get("aspects") or [])][:6],
                    )
                )
        except LLMUnavailable as exc:
            log.info("planner falling back to templates: %s", exc)
            planned = []

    if not planned:
        planned = [
            ResearchQuestion(
                run_id=run_id, stage=stage, bucket=bucket,
                text=_scoped(seed, cfg), seed_text=seed,
                aspects=_fallback_aspects(seed),
            )
            for seed in seeds
        ]
    return planned


async def plan_run(run_id: str, cfg: RunConfig, stages: list[str]) -> list[ResearchQuestion]:
    out: list[ResearchQuestion] = []
    for stage in stages:
        out += await plan_stage(run_id, cfg, stage)
    return out
