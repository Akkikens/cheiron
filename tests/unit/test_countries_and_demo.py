"""The generated country map, the choropleth's partial-coverage rule, and the demo renderer.

The demo is exercised here as a **contract test**, not as a UI test: it renders every chart type
from `encoding` + `data` alone, so a type it cannot draw is a gap in the specification rather
than in the page.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import new_context
from app.engine.dimensions import REGISTRY
from app.main import create_app
from app.models.plan import AnalysisPlan, ChartType, GroupBy, Intent, StudyFilter
from app.models.request import Options
from app.render.countries import ISO_A3
from app.render.encode import CHOROPLETH_MIN_COVERAGE, render
from tests.conftest import Handler, stub_transport

DEMO = Path(__file__).resolve().parents[2] / "demo" / "index.html"
DATA_TIMESTAMP = "2026-08-14T09:00:05"


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


async def a_ctx(settings: Settings, vocab: Vocabulary) -> Any:
    return new_context(
        CTGClient(stub_transport(settings, lambda _r: httpx.Response(404))),
        vocab,
        Options(include_citations=False),
        settings=settings,
        data_timestamp=DATA_TIMESTAMP,
    )


def geo_plan() -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.GEO,
        filters=StudyFilter(condition="glioblastoma"),
        group_by=GroupBy(dimension="country"),
        interpretation="Geographic distribution of trials by country.",
    )


def geo_bucketset(counts: dict[str, int]) -> BucketSet:
    return BucketSet(
        buckets=[
            Bucket(key=key, label=key, value=value, exactness="exact")
            for key, value in counts.items()
        ],
        total=sum(counts.values()),
        unclassified=0,
        semantics="overlapping",
        mode="server_counts",
    )


# --- the generated map ----------------------------------------------------------------------


def test_the_map_covers_every_country_the_corpus_uses() -> None:
    """226 distinct LocationCountry values live; a hand-written map covered 44."""
    assert len(ISO_A3) == 226
    assert all(len(code) == 3 and code.isupper() for code in ISO_A3.values())


def test_the_map_uses_upstream_spellings_not_iso_spellings() -> None:
    """ "South Korea" is what the API returns; ISO calls it "Korea, Republic of".

    One unmatched name used to collapse a working choropleth into a table, so the keys have to
    be the corpus's own strings.
    """
    assert ISO_A3["South Korea"] == "KOR"
    assert ISO_A3["Turkey (Türkiye)"] == "TUR"
    assert ISO_A3["Côte d’Ivoire"] == "CIV"  # curly apostrophe, as upstream sends it


def test_a_trailing_space_in_an_upstream_name_is_preserved() -> None:
    """The corpus really does contain "Bonaire, Saint Eustatius and Saba " with a trailing space.

    Stripping it would make the key never match the value it exists to translate.
    """
    assert "Bonaire, Saint Eustatius and Saba " in ISO_A3
    assert ISO_A3["Bonaire, Saint Eustatius and Saba "] == "BES"


# --- partial coverage -------------------------------------------------------------------------


async def test_a_map_is_drawn_when_it_represents_nearly_all_the_studies(
    settings: Settings, vocab: Vocabulary
) -> None:
    ctx = await a_ctx(settings, vocab)
    bucketset = geo_bucketset({"United States": 1_000, "France": 100, "Notacountry": 10})

    viz, warnings = render(
        geo_plan(), bucketset, ChartType.CHOROPLETH_MAP, REGISTRY["country"], ctx
    )

    assert viz.type is ChartType.CHOROPLETH_MAP
    assert {row["country"] for row in viz.data} == {"United States", "France"}
    annotation = next(a for a in (viz.annotations or []) if a["type"] == "unmapped")
    assert annotation["countries"] == ["Notacountry"]
    assert "Notacountry (10)" in annotation["text"]  # named with its count, not just dropped
    assert any("ISO-3166" in warning for warning in warnings)


async def test_a_map_becomes_a_table_when_too_much_is_unplaceable(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Below the coverage floor the map would misrepresent the distribution."""
    ctx = await a_ctx(settings, vocab)
    bucketset = geo_bucketset({"United States": 100, "Notacountry": 900})

    viz, warnings = render(
        geo_plan(), bucketset, ChartType.CHOROPLETH_MAP, REGISTRY["country"], ctx
    )

    assert viz.type is ChartType.TABLE
    assert any("table of all countries" in warning for warning in warnings)
    # The table keeps every country, including the one that could not be placed.
    assert {row["country"] for row in viz.data} == {"United States", "Notacountry"}


def test_the_coverage_floor_is_stated_not_implied() -> None:
    assert 0.5 < CHOROPLETH_MIN_COVERAGE < 1.0


# --- the demo as a contract test ---------------------------------------------------------------


def test_the_demo_renders_every_chart_type_in_the_contract() -> None:
    """A type the demo cannot draw is a gap in the spec, not in the page."""
    source = DEMO.read_text(encoding="utf-8")
    block = source.split("const RENDERERS = {", 1)[1].split("};", 1)[0]
    handled = set(re.findall(r"(\w+):", block))

    assert {chart.value for chart in ChartType} <= handled


def test_the_demo_is_served_from_the_app(settings: Settings, enums_handler: Handler) -> None:
    app = create_app(settings, transport=stub_transport(settings, enums_handler))

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Cheiron" in response.text


def test_the_demo_reads_the_spec_rather_than_hardcoding_questions() -> None:
    """It must key off `encoding`/`data`, never off which question was asked.

    If the page ever branches on a drug name or a dimension key, it stops being evidence that
    the specification is renderable.
    """
    source = DEMO.read_text(encoding="utf-8")
    renderer_body = source.split("// --- generic renderers", 1)[1].split("// --- meta panels", 1)[0]

    assert "encoding" in renderer_body
    for leak in ("pembrolizumab", "glioblastoma", "phase ===", "'phase'"):
        assert leak not in renderer_body.lower()
