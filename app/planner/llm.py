"""The LLM planner. SPEC §1, §3, §7.

**The model's entire job is question → `AnalysisPlan`.** It never sees study data, so it cannot
invent a count. Everything downstream of this module operates on exact upstream responses, which
is the property the whole service is built around: and
`test_no_study_data_reaches_the_model` is what proves it holds in the implementation rather than
in the prose.

Three things keep a misbehaving model from becoming a wrong answer:

1. **Structured Outputs with `strict: true`**, using the schema published by
   `AnalysisPlan.json_schema_strict()` so the prompt shape and the parsed shape cannot drift.
2. **A repair loop of at most two retries** (three model calls, SPEC §7's budget), fed the
   validator's own sentences: they are written to be actionable for exactly this reason.
3. **A terminal fallback to the heuristic planner.** The request never fails because the model
   misbehaved; a model outage degrades coverage, not availability (SPEC §5.5).

The hard constraints in the request are stated in the prompt *and* stamped on afterwards by
`enforce_hard_constraints`. The prompt is a courtesy to the model; the stamping is the guarantee.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Protocol, cast

from pydantic import ValidationError

from app.cache import Cache, plan_cache_key
from app.config import Settings
from app.ctg.vocab import Vocabulary
from app.engine.dimensions import REGISTRY
from app.models.plan import AnalysisPlan
from app.models.request import AnalyzeRequest
from app.models.response import PlannerName
from app.planner.base import PlanResult
from app.planner.heuristic import HeuristicPlanner
from app.planner.validate import validate_plan

logger = logging.getLogger("cheiron.planner")

MAX_ATTEMPTS: Final = 3
"""One initial call plus at most two repairs. SPEC §3 and §7's model-call budget."""

SCHEMA_NAME: Final = "analysis_plan"


@dataclass(frozen=True)
class CachedPlan:
    """What the plan cache stores: the IR plus how it was obtained.

    Caching only the plan erased `llm_repaired` on every hit, so provenance lied about
    whether the model needed repair.
    """

    plan: AnalysisPlan
    planner: PlannerName


SYSTEM_PROMPT: Final = """\
You translate a question about clinical trials into an AnalysisPlan. That is your entire job.

You must never produce a count, a total, a study identifier, a sponsor name you were not given, \
or any other factual claim about the data. You do not have the data. The plan you emit is \
executed by deterministic code that queries ClinicalTrials.gov and computes every number.

The `interpretation` field describes what WILL BE COMPUTED, in descriptive terms: for example \
"Annual count of interventional trials studying pembrolizumab, 2015-2025." It must never state \
a result. Digits are allowed only when they belong to something the caller named (COVID-19, \
PD-1, phase 1/2) or when they are a year you also put in `filters.start_year` or \
`filters.end_year`. Any other number is rejected as a smuggled count.

Resolve relative dates against today's date, given below, into `filters.start_year` and \
`filters.end_year`. "The last five years" and "since 2020" are year bounds, not prose.

Choose `group_by.dimension` from the available dimensions. Do not invent dimension names.
Leave `viz_hint` null unless the question explicitly asks for a chart form; the chart is chosen \
by a downstream registry that knows which forms are safe for the dimension.

Pick `intent` by what the question is asking for, not by what sounds impressive:

  distribution  how many trials fall into each category. The default.
  trend         how something changed over time. Use group_by "start_year".
  geo           where trials are run. Use group_by "country"; this renders as a map.
  network       which entities appear together in the same trial, such as which drugs are
                studied alongside each other. Use group_by "intervention_name".
  histogram     how a numeric quantity is spread. Use group_by "enrollment_count".
  scatter       one point per study, enrollment against start date. Use group_by
                "enrollment_count".
  comparison    two to four named groups set against each other. Fill `series`, one entry per
                group, each with the filter that defines it. Only for an explicit comparison.
  list          the caller wants rows rather than a chart.
"""


class ChatCompleter(Protocol):
    """The one call this planner makes, narrowed to what it needs.

    A protocol rather than the SDK type so tests drive the repair loop without a network or an
    API key, and so the planner has no import-time dependency on a configured client.
    """

    async def __call__(self, messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class _Attempt:
    plan: AnalysisPlan | None
    errors: list[str]


class LLMPlanner:
    """Satisfies `Planner`. Falls back to `HeuristicPlanner` on any terminal failure."""

    def __init__(
        self,
        completer: ChatCompleter,
        *,
        fallback: HeuristicPlanner | None = None,
        cache: Cache[CachedPlan] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self._complete = completer
        self._fallback = fallback or HeuristicPlanner()
        self._cache = cache
        self._warnings = warnings if warnings is not None else []

    @property
    def warnings(self) -> list[str]:
        """Why the plan came from where it did. The route merges these into `meta.warnings`."""
        return self._warnings

    async def plan(self, req: AnalyzeRequest, vocab: Vocabulary) -> PlanResult:
        # The year is part of the key because it is part of the prompt: a plan for "the last
        # five years" cached in December must not be served in January.
        today = date.today()
        key = plan_cache_key(req.query, _structured_hints(req) | {"_year": today.year})
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return PlanResult(plan=cached.plan, planner=cached.planner, attempts=0)

        schema = AnalysisPlan.json_schema_strict()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _system_prompt(vocab)},
            {"role": "user", "content": _user_prompt(req, today)},
        ]

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = await self._complete(messages, schema)
            except Exception as exc:
                # Any model failure degrades to the fallback; it never fails the request.
                #
                # The exception *type* only. Provider messages quote the request back: an
                # OpenAI 401 includes the partially-masked API key, and this string reaches an
                # anonymous caller through meta.warnings. The detail belongs in the log.
                logger.warning("planner: model call failed", exc_info=exc)
                return await self._degrade(req, vocab, type(exc).__name__)

            result = _parse(raw, vocab)
            if result.plan is not None:
                planner: PlannerName = "llm" if attempt == 1 else "llm_repaired"
                if self._cache is not None:
                    self._cache.set(key, CachedPlan(plan=result.plan, planner=planner))
                return PlanResult(
                    plan=result.plan,
                    planner=planner,
                    attempts=attempt,
                )

            if attempt == MAX_ATTEMPTS:
                break

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": _repair_prompt(result.errors)})

        return await self._degrade(
            req,
            vocab,
            f"the model returned an unusable plan {MAX_ATTEMPTS} times",
        )

    async def _degrade(self, req: AnalyzeRequest, vocab: Vocabulary, reason: str) -> PlanResult:
        """Fall back to the deterministic planner and say why, in `meta.warnings`.

        If the heuristic planner also cannot plan, its `unplannable_query` propagates: the model
        could not serve the question and neither can the fallback, which is the caller's answer
        (SPEC §3). That is the one path where a planning failure reaches the client.
        """
        self._warnings.append(
            f"Planning fell back to the deterministic planner ({reason}); coverage is limited to "
            f"the template questions it handles."
        )
        return await self._fallback.plan(req, vocab)


def _parse(raw: str, vocab: Vocabulary) -> _Attempt:
    """Parse and validate. Parse failures and validation failures both feed the repair loop."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _Attempt(None, [f"the response was not valid JSON: {exc}"])

    try:
        plan = AnalysisPlan.model_validate(payload)
    except ValidationError as exc:
        return _Attempt(None, [_readable(error) for error in exc.errors()])

    errors = validate_plan(plan, vocab)
    if errors:
        return _Attempt(None, errors)
    return _Attempt(plan, [])


def _readable(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
    return f"{location}: {error.get('msg', 'invalid')}"


def _system_prompt(vocab: Vocabulary) -> str:
    """System prompt plus the live vocabulary and dimension registry.

    Both are injected from the loaded vocabulary rather than hardcoded. SPEC §3 requires the
    plan to be validated against the live enums, and a prompt listing stale values would produce
    plans that fail validation for reasons the model cannot see.
    """
    return "\n".join(
        [
            SYSTEM_PROMPT,
            "",
            "Available dimensions (group_by.dimension):",
            *(
                f"  - {dim.key}: {dim.label}"
                f"{'' if dim.partition else ' (multi-valued; buckets overlap)'}"
                for dim in REGISTRY.values()
            ),
            "",
            "Valid enum values:",
            *(
                f"  - {name}: {', '.join(vocab.values(name))}"
                for name in ("Phase", "Status", "StudyType")
            ),
        ]
    )


def _user_prompt(req: AnalyzeRequest, today: date) -> str:
    # The model has no clock. Without this, "the last five years" was planned against whatever
    # year the training data made likely, and the resulting bounds were quietly wrong.
    lines = [f"Today is {today.isoformat()}.", "", f"Question: {req.query}"]
    hints = {field: value for field, value in _structured_hints(req).items() if value}
    if hints:
        lines.append("")
        lines.append(
            "The caller supplied these as HARD CONSTRAINTS. Copy them into `filters` exactly "
            "and do not contradict them:"
        )
        lines.extend(f"  {field}: {value}" for field, value in sorted(hints.items()))
    return "\n".join(lines)


def _repair_prompt(errors: Sequence[str]) -> str:
    """The validator's own sentences, verbatim: they name the field and a valid alternative."""
    listed = "\n".join(f"  - {error}" for error in errors)
    return (
        f"That plan is invalid:\n{listed}\n\n"
        f"Return a corrected AnalysisPlan. Change only what the errors require."
    )


def _structured_hints(req: AnalyzeRequest) -> dict[str, Any]:
    """The request's structured fields. **This is the only request data that reaches a prompt.**

    Note what is absent: no study record, no count, no upstream response. The model is not in the
    data path (SPEC §1), and keeping this function the single source of prompt content is what
    makes that auditable.
    """
    return {
        "drug_name": req.drug_name,
        "condition": req.condition,
        "sponsor": req.sponsor,
        "country": req.country,
        "phase": list(req.phase or []),
        "status": list(req.status or []),
        "study_type": req.study_type,
        "start_year": req.start_year,
        "end_year": req.end_year,
    }


def openai_completer(settings: Settings) -> ChatCompleter:
    """The real completer. Imported lazily so `LLM_ENABLED=false` needs no SDK and no key."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def complete(messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=cast(Any, messages),
            temperature=0,
            seed=settings.openai_seed,
            max_tokens=settings.openai_max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("model returned an empty completion")
        return str(content)

    return complete
