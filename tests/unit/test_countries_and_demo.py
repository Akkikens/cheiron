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


# --- scatter selection and rendering ----------------------------------------------------------


def scatter_plan() -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.SCATTER,
        filters=StudyFilter(condition="glioblastoma"),
        group_by=GroupBy(dimension="enrollment_count"),
        interpretation="Enrollment against start date for glioblastoma trials.",
    )


def a_study(nct: str, year: str | None, enrollment: int | None) -> dict[str, Any]:
    status: dict[str, Any] = {} if year is None else {"startDateStruct": {"date": year}}
    design: dict[str, Any] = {} if enrollment is None else {"enrollmentInfo": {"count": enrollment}}
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct},
            "statusModule": status,
            "designModule": design,
        }
    }


def a_scatter_bucketset(mode: str) -> BucketSet:
    return BucketSet(
        buckets=[Bucket(key="11-50", label="11-50", value=3, exactness="exact")],
        total=3,
        unclassified=0,
        semantics="partition",
        mode=mode,  # type: ignore[arg-type]
    )


def test_scatter_needs_the_records_and_downgrades_without_them() -> None:
    """Same rule as network_graph: plotting bin midpoints as studies would invent data."""
    from app.render.registry import select_chart

    in_record_mode, _ = select_chart(
        scatter_plan(),
        a_scatter_bucketset("complete_records"),
        REGISTRY["enrollment_count"],
        Options(),
    )
    above, warnings = select_chart(
        scatter_plan(),
        a_scatter_bucketset("server_counts"),
        REGISTRY["enrollment_count"],
        Options(),
    )

    assert in_record_mode is ChartType.SCATTER_PLOT
    assert above is ChartType.HISTOGRAM
    assert any("one point per study" in w for w in warnings)


async def test_scatter_excludes_studies_missing_an_axis_and_counts_them(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Plotting a study with no enrollment at zero manufactures a cluster that is not there."""
    from app.render.encode import render_scatter

    ctx = await a_ctx(settings, vocab)
    studies = [
        a_study("NCT1", "2024-03", 40),
        a_study("NCT2", "2024", 640),
        a_study("NCT3", None, 100),  # no start date
        a_study("NCT4", "2025-01-01", None),  # no enrollment
    ]

    viz, _ = render_scatter(
        scatter_plan(),
        studies,
        a_scatter_bucketset("complete_records"),
        REGISTRY["enrollment_count"],
        ctx,
    )

    assert [point["nct_id"] for point in viz.data] == ["NCT1", "NCT2"]
    annotation = next(a for a in (viz.annotations or []) if a["type"] == "points")
    assert annotation["plotted"] == 2
    assert annotation["excluded"] == 2
    assert "excluded rather than plotted at zero" in annotation["text"]
    assert viz.data[0]["url"].endswith("NCT1")


async def test_scatter_titles_name_both_axes(settings: Settings, vocab: Vocabulary) -> None:
    from app.render.encode import render_scatter

    ctx = await a_ctx(settings, vocab)
    viz, _ = render_scatter(
        scatter_plan(),
        [a_study("NCT1", "2024", 40)],
        a_scatter_bucketset("complete_records"),
        REGISTRY["enrollment_count"],
        ctx,
    )

    assert viz.title == "Glioblastoma Trials: enrollment by start year"
    assert "by Enrollment:" not in viz.title  # the awkward double-naming this replaced


def test_the_page_ships_a_favicon_and_serves_it(settings: Settings, enums_handler: Handler) -> None:
    """The mark is a file, not a data URI, so /docs gets an icon too."""
    source = DEMO.read_text(encoding="utf-8")
    assert 'rel="icon"' in source
    assert "/assets/mark-32.png" in source

    app = create_app(settings, transport=stub_transport(settings, enums_handler))
    with TestClient(app) as client:
        icon = client.get("/assets/mark-32.png")
        touch = client.get("/assets/mark-180.png")

    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
    assert touch.status_code == 200
    assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_demo_escapes_values_on_the_way_out() -> None:
    """`fmt` values are numbers today, which is a property of upstream's schema, not this page."""
    source = DEMO.read_text(encoding="utf-8")
    fmt_line = next(line for line in source.splitlines() if line.startswith("const fmt ="))

    assert "esc(" in fmt_line


def test_a_browser_404_shows_the_envelope_an_api_client_would_have_got(
    settings: Settings, enums_handler: Handler
) -> None:
    """The 404 is where a reviewer first meets the error contract, so it demonstrates it."""
    app = create_app(settings, transport=stub_transport(settings, enums_handler))

    with TestClient(app) as client:
        page = client.get("/no-such-route", headers={"accept": "text/html,application/xhtml+xml"})

    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "invalid_request" in page.text
    # The id in the body is this request's, not a placeholder left in the template.
    assert page.headers["X-Request-Id"] in page.text
    assert "__REQUEST_ID__" not in page.text and "__MESSAGE__" not in page.text


def test_an_api_client_404_is_unchanged(settings: Settings, enums_handler: Handler) -> None:
    """Content negotiation, not a blanket switch: curl and the tests keep the JSON envelope."""
    app = create_app(settings, transport=stub_transport(settings, enums_handler))

    with TestClient(app) as client:
        for accept in ("*/*", "application/json"):
            response = client.get("/no-such-route", headers={"accept": accept})
            assert response.headers["content-type"].startswith("application/json")
            assert response.json()["error"]["code"] == "invalid_request"


def test_the_wordmark_links_home_and_the_opening_example_rotates() -> None:
    source = DEMO.read_text(encoding="utf-8")

    assert '<a href="/" aria-label="Cheiron home">' in source
    # Reloading walks the chart types rather than repeating one, and per tab rather than at
    # random, so a reviewer who reloads sees the range without touching a control.
    assert "sessionStorage" in source and "openingExample" in source
