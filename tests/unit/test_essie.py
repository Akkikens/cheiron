"""Exact predicate strings from `docs/CTG-API-NOTES.md` §2, plus the injection cases.

Every expected string here was produced by a live call recorded in the notes, so a change to
the builder that changes a predicate fails against upstream's own numbers.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.constants import FULL_MATCH_OP
from app.ctg.essie import OPERATOR_KEYWORDS, Essie

MERCK = "Merck Sharp & Dohme LLC"

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


# --- the notes §2 table, verbatim ---------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (Essie.field_eq("Phase", "PHASE3"), "AREA[Phase]PHASE3"),
        (Essie.field_eq("Phase", "NA"), "AREA[Phase]NA"),
        (Essie.field_eq("StartDate", "2022"), "AREA[StartDate]2022"),
        (Essie.field_eq("Phase:size", "2"), "AREA[Phase:size]2"),
        (Essie.missing("Phase"), "AREA[Phase]MISSING"),
        (
            Essie.full_match("LeadSponsorName", MERCK),
            f'AREA[LeadSponsorName]{FULL_MATCH_OP}[FullMatch]"Merck Sharp & Dohme LLC"',
        ),
        (
            Essie.date_range("StartDate", date(2020, 1, 1), date(2020, 12, 31)),
            "AREA[StartDate]RANGE[2020-01-01,2020-12-31]",
        ),
        (
            Essie.numeric_range("EnrollmentCount", 500, "MAX"),
            "AREA[EnrollmentCount]RANGE[500,MAX]",
        ),
        (
            Essie.has_value("ResultsFirstPostDate"),
            "AREA[ResultsFirstPostDate]RANGE[MIN,MAX]",
        ),
        (
            Essie.distance("LocationGeoPoint", 42.36, -71.06, 50),
            "AREA[LocationGeoPoint]DISTANCE[42.36,-71.06,50mi]",
        ),
        (Essie.all_(), "ALL"),
    ],
)
def test_builder_reproduces_the_recorded_predicate(expression: str, expected: str) -> None:
    assert expression == expected


def test_full_match_is_the_1841_form() -> None:
    """notes §2: this exact string counted 1,841; the unscoped variant counted 4,591."""
    assert (
        Essie.full_match("LeadSponsorName", MERCK)
        == 'AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"'
    )


# --- the scope invariant: the 4591 expression must be unconstructible ---------------------


@pytest.mark.parametrize("area", ["", " ", "\t"])
def test_an_unscoped_predicate_cannot_be_built(area: str) -> None:
    with pytest.raises(ValueError, match="area is required"):
        Essie.full_match(area, MERCK)


@pytest.mark.parametrize(
    "build",
    [
        lambda area: Essie.field_eq(area, "PHASE2"),
        lambda area: Essie.full_match(area, MERCK),
        lambda area: Essie.missing(area),
        lambda area: Essie.has_value(area),
        lambda area: Essie.numeric_range(area, 1, "MAX"),
        lambda area: Essie.date_range(area, date(2020, 1, 1), date(2020, 12, 31)),
        lambda area: Essie.distance(area, 1.0, 2.0, 5),
    ],
)
def test_every_scoped_builder_emits_its_area(build: object) -> None:
    assert callable(build)
    assert build("LeadSponsorName").startswith("AREA[LeadSponsorName]")
    with pytest.raises(ValueError, match="area is required"):
        build("")


def test_full_match_has_no_unscoped_api() -> None:
    """There is deliberately no way to ask for a bare COVERAGE[FullMatch]."""
    for name in dir(Essie):
        if name.startswith("_"):
            continue
        assert "full_match_unscoped" not in name


# --- escaping (notes §2) ------------------------------------------------------------------


def test_quote_injection_is_neutralised() -> None:
    """Unescaped, notes §2 records this returning 49,659: the injected clause executes."""
    hostile = 'Merck" OR AREA[Phase]PHASE3'
    built = Essie.full_match("LeadSponsorName", hostile)

    assert built == ('AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck\\" \\OR \\AREA[Phase]PHASE3"')
    # The only unescaped AREA[ in the output is our own scope.
    assert built.count("AREA[") - built.count("\\AREA[") == 1


def test_backslash_is_escaped_before_anything_else() -> None:
    """Order matters: escaping keywords first would double-escape our own backslashes."""
    assert Essie.escape("foo\\bar") == "foo\\\\bar"
    assert Essie.escape('a\\"b') == 'a\\\\\\"b'


@pytest.mark.parametrize("keyword", OPERATOR_KEYWORDS)
def test_every_operator_keyword_is_escaped(keyword: str) -> None:
    assert Essie.escape(f"x {keyword} y") == f"x \\{keyword} y"


def test_lowercase_operators_are_left_alone() -> None:
    """Measured: lowercase and/or/not are search terms, not operators (486 vs 9,964)."""
    assert Essie.escape("head and neck") == "head and neck"
    assert Essie.escape("pain not cancer") == "pain not cancer"


def test_keywords_inside_words_are_not_escaped() -> None:
    assert Essie.escape("SMALLPOX") == "SMALLPOX"
    assert Essie.escape("ORAL") == "ORAL"
    assert Essie.escape("BRAND") == "BRAND"


def test_all_is_escaped_because_it_is_also_a_real_condition() -> None:
    """ALL is acute lymphoblastic leukaemia as well as the whole-corpus operator."""
    assert Essie.escape("ALL") == "\\ALL"
    assert Essie.full_match("Condition", "ALL") == ('AREA[Condition]COVERAGE[FullMatch]"\\ALL"')


def test_missing_injection_is_escaped() -> None:
    assert Essie.field_eq("Phase", "x MISSING y") == "AREA[Phase]x \\MISSING y"


def test_escaping_is_not_optional_on_field_eq() -> None:
    assert "\\OR" in Essie.field_eq("Condition", "cancer OR ALL")


# --- composition --------------------------------------------------------------------------


def test_and_parenthesises_every_operand() -> None:
    built = Essie.and_(
        Essie.field_eq("Phase", "PHASE2"), Essie.field_eq("StudyType", "INTERVENTIONAL")
    )
    assert built == "((AREA[Phase]PHASE2) AND (AREA[StudyType]INTERVENTIONAL))"


def test_or_parenthesises_every_operand() -> None:
    built = Essie.or_(Essie.field_eq("Phase", "PHASE2"), Essie.field_eq("Phase", "PHASE3"))
    assert built == "((AREA[Phase]PHASE2) OR (AREA[Phase]PHASE3))"


def test_nesting_never_relies_on_precedence() -> None:
    inner = Essie.or_(Essie.field_eq("Phase", "PHASE2"), Essie.field_eq("Phase", "PHASE3"))
    built = Essie.and_(inner, Essie.field_eq("StudyType", "INTERVENTIONAL"))

    assert built == (
        "(((AREA[Phase]PHASE2) OR (AREA[Phase]PHASE3)) AND (AREA[StudyType]INTERVENTIONAL))"
    )


def test_phrase_quotes_and_escapes() -> None:
    assert Essie.phrase("breast cancer") == '"breast cancer"'
    assert Essie.phrase('a" OR ALL') == '"a\\" \\OR \\ALL"'


def test_not_is_exclusion_not_negation() -> None:
    built = Essie.not_(Essie.phrase("pain"), Essie.phrase("cancer"))
    assert built == '(("pain") NOT ("cancer"))'


def test_not_requires_both_sides() -> None:
    with pytest.raises(ValueError, match="both an expression"):
        Essie.not_(Essie.phrase("pain"), "")


def test_boolean_grouping_reproduces_the_notes_expression() -> None:
    """This exact string counted 9,964 under filter.advanced (notes §2)."""
    built = Essie.not_(
        Essie.and_(
            Essie.or_(Essie.phrase("head"), Essie.phrase("neck")),
            Essie.phrase("pain"),
        ),
        Essie.phrase("cancer"),
    )
    assert built == '(((("head") OR ("neck")) AND ("pain")) NOT ("cancer"))'


def test_single_operand_needs_no_wrapper() -> None:
    assert Essie.and_(Essie.field_eq("Phase", "PHASE2")) == "AREA[Phase]PHASE2"


def test_empty_composition_is_loud() -> None:
    with pytest.raises(ValueError, match="at least one operand"):
        Essie.and_()


def test_blank_operands_are_dropped() -> None:
    assert Essie.and_("", Essie.field_eq("Phase", "PHASE2")) == "AREA[Phase]PHASE2"


# --- guards -------------------------------------------------------------------------------


def test_inverted_ranges_are_rejected() -> None:
    with pytest.raises(ValueError, match="precedes"):
        Essie.date_range("StartDate", date(2021, 1, 1), date(2020, 1, 1))
    with pytest.raises(ValueError, match="precedes"):
        Essie.numeric_range("EnrollmentCount", 500, 100)


def test_distance_uses_square_brackets_not_parens() -> None:
    """`DISTANCE(...)` leaks a raw Java parser exception (notes §2)."""
    built = Essie.distance("LocationGeoPoint", 42.36, -71.06, 50)
    assert "DISTANCE[" in built
    assert "DISTANCE(" not in built


def test_distance_rejects_an_unknown_unit() -> None:
    with pytest.raises(ValueError, match="mi or km"):
        Essie.distance("LocationGeoPoint", 1.0, 2.0, 5, unit="furlongs")


def test_aggfilters_appears_nowhere_in_app() -> None:
    """notes §2: `aggFilters=phase:na` returns 0 silently. It is not used anywhere."""
    offenders = [
        path.relative_to(APP_ROOT)
        for path in APP_ROOT.rglob("*.py")
        if "aggFilters" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
