"""The LLM planner, its repair loop, and the caches. SPEC §1, §3, §7.

No test here reaches OpenAI. The planner takes a `ChatCompleter`, so the repair loop, the
fallback, and the cache are all driven from scripted responses, which is also why the suite
runs with no API key present at all.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from app.cache import TTLStore, normalize_question, plan_cache_key, result_cache_key
from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.models.plan import AnalysisPlan, Intent, Metric
from app.models.request import AnalyzeRequest
from app.planner.llm import MAX_ATTEMPTS, CachedPlan, LLMPlanner, _structured_hints, _system_prompt
from app.planner.validate import validate_plan
from tests.conftest import Handler, stub_transport


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


def a_request(query: str = "How many trials by phase?", **kwargs: Any) -> AnalyzeRequest:
    return AnalyzeRequest(query=query, **kwargs)


def a_plan_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "intent": "distribution",
        "filters": {
            "condition": None,
            "intervention": "Pembrolizumab",
            "sponsor": None,
            "term": None,
            "country": None,
            "phase": [],
            "status": [],
            "study_type": None,
            "start_year": None,
            "end_year": None,
        },
        "series": [],
        "group_by": {"dimension": "phase", "bin": None},
        "secondary_group_by": None,
        "metric": "study_count",
        "viz_hint": None,
        "interpretation": "Distribution of pembrolizumab trials across phases.",
    }
    payload.update(overrides)
    return payload


class Model:
    """Scripted completions, with a record of every prompt it was shown."""

    def __init__(self, *responses: str | Exception) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.prompts: list[list[dict[str, str]]] = []

    async def __call__(self, messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str:
        self.prompts.append([dict(message) for message in messages])
        self.calls += 1
        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


# --- the happy path -------------------------------------------------------------------------


async def test_a_valid_plan_is_used_as_is(vocab: Vocabulary) -> None:
    model = Model(json.dumps(a_plan_payload()))

    result = await LLMPlanner(model).plan(a_request(), vocab)

    assert result.planner == "llm"
    assert result.attempts == 1
    assert model.calls == 1
    assert result.plan.group_by.dimension == "phase"


async def test_the_published_schema_is_what_the_model_is_given(vocab: Vocabulary) -> None:
    """One source of truth: the prompt schema is generated from the model, never hand-written."""
    captured: list[dict[str, Any]] = []

    async def completer(messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str:
        captured.append(schema)
        return json.dumps(a_plan_payload())

    await LLMPlanner(completer).plan(a_request(), vocab)

    assert captured[0] == AnalysisPlan.json_schema_strict()
    assert captured[0]["additionalProperties"] is False


async def test_the_model_is_told_todays_date(vocab: Vocabulary) -> None:
    """ "The last five years" is unanswerable without a clock, and the model does not have one."""
    model = Model(json.dumps(a_plan_payload()))

    await LLMPlanner(model).plan(a_request("trials in the last five years"), vocab)

    assert f"Today is {date.today().isoformat()}." in model.prompts[0][1]["content"]


async def test_a_cached_plan_does_not_outlive_the_year_it_was_planned_in(
    vocab: Vocabulary,
) -> None:
    """The date is in the prompt, so it has to be in the key."""
    from app.cache import plan_cache_key

    hints = {"drug_name": "pembrolizumab"}

    assert plan_cache_key("last five years", hints | {"_year": 2025}) != plan_cache_key(
        "last five years", hints | {"_year": 2026}
    )


# --- the repair loop ------------------------------------------------------------------------


async def test_an_invalid_enum_is_repaired(vocab: Vocabulary) -> None:
    broken = a_plan_payload(
        filters={**a_plan_payload()["filters"], "phase": ["PHASE_SEVEN"]},
    )
    model = Model(json.dumps(broken), json.dumps(a_plan_payload()))

    result = await LLMPlanner(model).plan(a_request(), vocab)

    assert result.planner == "llm_repaired"
    assert result.attempts == 2
    assert model.calls == 2


async def test_the_repair_prompt_names_the_value_and_the_alternatives(vocab: Vocabulary) -> None:
    """The validator's sentences are the repair input, which is why they must be actionable."""
    broken = a_plan_payload(filters={**a_plan_payload()["filters"], "phase": ["PHASE_SEVEN"]})
    model = Model(json.dumps(broken), json.dumps(a_plan_payload()))

    await LLMPlanner(model).plan(a_request(), vocab)

    repair = model.prompts[1][-1]["content"]
    assert "PHASE_SEVEN" in repair
    assert "PHASE3" in repair  # a valid alternative, listed


async def test_unparseable_json_also_feeds_the_repair_loop(vocab: Vocabulary) -> None:
    model = Model("{not json", json.dumps(a_plan_payload()))

    result = await LLMPlanner(model).plan(a_request(), vocab)

    assert result.planner == "llm_repaired"
    assert "not valid JSON" in model.prompts[1][-1]["content"]


async def test_three_bad_plans_fall_back_and_still_answer(vocab: Vocabulary) -> None:
    """SPEC §3: the request never fails because the model misbehaved."""
    model = Model("garbage")
    warnings: list[str] = []

    result = await LLMPlanner(model, warnings=warnings).plan(a_request(), vocab)

    assert model.calls == MAX_ATTEMPTS == 3
    assert result.planner == "heuristic_fallback"
    assert result.plan.group_by.dimension == "phase"
    assert any("fell back to the deterministic planner" in warning for warning in warnings)


async def test_a_model_outage_degrades_rather_than_fails(vocab: Vocabulary) -> None:
    model = Model(TimeoutError("upstream model timed out"))
    warnings: list[str] = []

    result = await LLMPlanner(model, warnings=warnings).plan(a_request(), vocab)

    assert result.planner == "heuristic_fallback"
    assert model.calls == 1  # an outage is not retried into the repair budget
    assert any("TimeoutError" in warning for warning in warnings)


async def test_a_question_neither_planner_can_serve_is_unplannable(vocab: Vocabulary) -> None:
    from app.errors import CheironError, ErrorCode

    model = Model("garbage")

    with pytest.raises(CheironError) as caught:
        await LLMPlanner(model).plan(
            a_request("What is the airspeed of an unladen swallow?"), vocab
        )

    assert caught.value.code is ErrorCode.UNPLANNABLE_QUERY


# --- SPEC §1: the model is not in the data path ---------------------------------------------


async def test_no_study_data_reaches_the_model(vocab: Vocabulary) -> None:
    """The thesis, as a test.

    The only dynamic content in a prompt is the question, the structured hints, the live
    vocabulary, and the dimension keys. If a count, an NCT id, or a study record ever appears in
    a prompt, the model is in the data path and every number it touches becomes suspect.
    """
    model = Model(json.dumps(a_plan_payload()))
    request = a_request("How many pembrolizumab trials by phase?", drug_name="Pembrolizumab")

    await LLMPlanner(model).plan(request, vocab)

    prompt = " ".join(message["content"] for message in model.prompts[0])
    assert "NCT" not in prompt
    assert "2927" not in prompt and "2,927" not in prompt
    assert "totalCount" not in prompt
    assert "protocolSection" not in prompt


def test_structured_hints_are_the_only_request_data_in_a_prompt() -> None:
    request = a_request(drug_name="Pembrolizumab", condition="melanoma", start_year=2015)
    hints = _structured_hints(request)

    assert set(hints) == {
        "drug_name",
        "condition",
        "sponsor",
        "country",
        "phase",
        "status",
        "study_type",
        "start_year",
        "end_year",
    }
    assert "query" not in hints  # the question travels as the user message, not as a hint


async def test_the_prompt_carries_the_live_vocabulary(vocab: Vocabulary) -> None:
    prompt = _system_prompt(vocab)

    for value in vocab.values("Phase"):
        assert value in prompt
    assert "multi-valued; buckets overlap" in prompt  # partition flags reach the model


async def test_interpretation_carrying_a_count_is_rejected(vocab: Vocabulary) -> None:
    """A number smuggled into prose is still the model asserting a fact (T05 rule 5)."""
    smuggled = a_plan_payload(interpretation="There are 2,927 pembrolizumab trials.")
    plan = AnalysisPlan.model_validate(smuggled)

    assert validate_plan(plan, vocab)


async def test_a_smuggled_count_triggers_repair(vocab: Vocabulary) -> None:
    smuggled = a_plan_payload(interpretation="There are 2,927 pembrolizumab trials.")
    model = Model(json.dumps(smuggled), json.dumps(a_plan_payload()))

    result = await LLMPlanner(model).plan(a_request(), vocab)

    assert result.planner == "llm_repaired"


# --- hard constraints -----------------------------------------------------------------------


async def test_the_model_cannot_contradict_a_structured_hint(vocab: Vocabulary) -> None:
    from app.planner.validate import enforce_hard_constraints

    contradicting = a_plan_payload(
        filters={**a_plan_payload()["filters"], "intervention": "aspirin"}
    )
    model = Model(json.dumps(contradicting))
    request = a_request(drug_name="Pembrolizumab")

    result = await LLMPlanner(model).plan(request, vocab)
    plan, assumptions = enforce_hard_constraints(result.plan, request)

    assert plan.filters.intervention == "Pembrolizumab"
    assert assumptions


async def test_hard_constraints_are_stated_in_the_prompt_too(vocab: Vocabulary) -> None:
    model = Model(json.dumps(a_plan_payload()))

    await LLMPlanner(model).plan(a_request(drug_name="Pembrolizumab"), vocab)

    user_message = model.prompts[0][-1]["content"]
    assert "HARD CONSTRAINTS" in user_message
    assert "Pembrolizumab" in user_message


async def test_a_repaired_plan_stays_marked_repaired_on_cache_hit(vocab: Vocabulary) -> None:
    broken = a_plan_payload(filters={**a_plan_payload()["filters"], "phase": ["PHASE_SEVEN"]})
    model = Model(json.dumps(broken), json.dumps(a_plan_payload()))
    cache: TTLStore[CachedPlan] = TTLStore()
    planner = LLMPlanner(model, cache=cache)

    first = await planner.plan(a_request(), vocab)
    second = await planner.plan(a_request(), vocab)

    assert first.planner == "llm_repaired"
    assert second.planner == "llm_repaired"
    assert model.calls == 2  # second call is a cache hit


# --- caching --------------------------------------------------------------------------------


async def test_a_repeat_question_skips_the_model(vocab: Vocabulary) -> None:
    model = Model(json.dumps(a_plan_payload()))
    cache: TTLStore[CachedPlan] = TTLStore()
    planner = LLMPlanner(model, cache=cache)

    first = await planner.plan(a_request(), vocab)
    second = await planner.plan(a_request(), vocab)

    assert model.calls == 1
    assert second.plan == first.plan
    assert cache.hits == 1


async def test_whitespace_and_case_do_not_split_the_cache(vocab: Vocabulary) -> None:
    model = Model(json.dumps(a_plan_payload()))
    cache: TTLStore[CachedPlan] = TTLStore()
    planner = LLMPlanner(model, cache=cache)

    await planner.plan(a_request("How many trials by phase?"), vocab)
    await planner.plan(a_request("  HOW   many trials by phase?  "), vocab)

    assert model.calls == 1


async def test_different_structured_hints_are_different_cache_entries(vocab: Vocabulary) -> None:
    """The hints change the plan, so they must change the key."""
    model = Model(json.dumps(a_plan_payload()))
    cache: TTLStore[CachedPlan] = TTLStore()
    planner = LLMPlanner(model, cache=cache)

    await planner.plan(a_request(drug_name="Pembrolizumab"), vocab)
    await planner.plan(a_request(drug_name="Nivolumab"), vocab)

    assert model.calls == 2


def test_stopwords_are_not_normalized_away() -> None:
    """`in France` and `for France` probably mean the same thing; probably is not good enough."""
    assert normalize_question("trials in France") != normalize_question("trials for France")


def test_the_result_key_binds_to_the_dataset_revision() -> None:
    plan = AnalysisPlan.model_validate(a_plan_payload())

    first = result_cache_key(plan.normalized_key(), "2026-08-14T09:00:05")
    second = result_cache_key(plan.normalized_key(), "2026-08-15T09:00:05")

    assert first != second


def test_prose_only_differences_share_a_result_key() -> None:
    """`interpretation` and `viz_hint` do not change which numbers come back (T04)."""
    plan = AnalysisPlan.model_validate(a_plan_payload())
    reworded = AnalysisPlan.model_validate(
        a_plan_payload(interpretation="Pembrolizumab trials, split by phase.")
    )

    assert plan.normalized_key() == reworded.normalized_key()


def test_the_plan_key_ignores_empty_hints() -> None:
    assert plan_cache_key("q", {"phase": [], "drug_name": None}) == plan_cache_key("q", {})


# --- degraded mode --------------------------------------------------------------------------


async def test_metric_and_intent_survive_a_repair(vocab: Vocabulary) -> None:
    """A repair must not quietly change the question that was asked."""
    broken = a_plan_payload(group_by={"dimension": "not_a_dimension", "bin": None})
    fixed = a_plan_payload(intent="trend", group_by={"dimension": "start_year", "bin": {"size": 1}})
    model = Model(json.dumps(broken), json.dumps(fixed))

    result = await LLMPlanner(model).plan(a_request("trials over time"), vocab)

    assert result.plan.intent is Intent.TREND
    assert result.plan.metric is Metric.STUDY_COUNT


def test_the_prompt_explains_every_intent_the_schema_publishes() -> None:
    """The schema is the model's action space; an unexplained option gets chosen badly.

    Live, "Which countries run these trials?" planned as `distribution` rather than `geo`, so a
    country question returned a bar chart where the README documents a map. The enum reached the
    model through the schema, but nothing told it what the values meant.
    """
    from app.models.plan import Intent
    from app.planner.llm import SYSTEM_PROMPT

    for intent in Intent:
        assert f"  {intent.value}" in SYSTEM_PROMPT, f"{intent.value} is undocumented in the prompt"


def test_the_prompt_points_the_spatial_intents_at_their_dimensions() -> None:
    """Naming the intent is not enough; each one has a dimension that makes it work."""
    from app.planner.llm import SYSTEM_PROMPT

    for intent, dimension in (
        ("geo", "country"),
        ("trend", "start_year"),
        ("network", "intervention_name"),
        ("histogram", "enrollment_count"),
    ):
        line = next(ln for ln in SYSTEM_PROMPT.splitlines() if ln.strip().startswith(intent))
        block = SYSTEM_PROMPT.split(line, 1)[1][:200]
        assert dimension in line or dimension in block, f"{intent} does not name {dimension}"
