# T03: Essie predicate builder and the dimension registry

**Est. 20 min · depends on: T02 · unblocks: T05, T06**

This is the highest-leverage correctness surface in the codebase: every count in every
chart is only as trustworthy as the predicate that produced it. SPEC §5.3, notes §2.

## `app/ctg/essie.py`

```python
class Essie:
    @staticmethod
    def escape(text: str) -> str
    @staticmethod
    def field_eq(area: str, value: str) -> str            # AREA[Phase]PHASE2
    @staticmethod
    def full_match(area: str, value: str) -> str          # AREA[X]<OP>[FullMatch]"..."
    @staticmethod
    def date_range(area: str, start: date, end: date) -> str
    @staticmethod
    def numeric_range(area: str, lo: int | Literal["MIN"], hi: int | Literal["MAX"]) -> str
    @staticmethod
    def missing(area: str) -> str                         # AREA[Phase]MISSING
    @staticmethod
    def has_value(area: str) -> str                       # AREA[X]RANGE[MIN,MAX]
    @staticmethod
    def all_() -> str                                     # ALL
    @staticmethod
    def and_(*exprs: str) -> str                          # parenthesised
    @staticmethod
    def or_(*exprs: str) -> str
    @staticmethod
    def not_(expr: str) -> str                            # completes the boolean grammar
    @staticmethod
    def phrase(text: str) -> str                          # bare quoted phrase
```

Rules:

- `full_match` uses `FULL_MATCH_OP` from `app/constants.py` (T01 established `COVER` and
  `COVERAGE` are exact aliases and standardized on `COVERAGE`).
- **`full_match` always emits an `AREA[...]` prefix, and there is no API for emitting one
  without.** T01 measured that a bare `COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` is
  valid Essie, returns HTTP 200, and counts **4591** against the correct 1841: an
  unscoped full-corpus match that looks like a success. This is the same class of failure as
  `aggFilters=phase:na` returning 0, and the mitigation is the same: make the wrong
  expression unconstructible. `area` is a required positional parameter; assert it is
  non-empty and appears in the output. Test both the 1841 form and the assertion.
- Note for the error-handling path: the operator itself *does* fail loudly on a malformed
  argument (`FullMatc` → 400 `Invalid coverage: FullMatc`) and on a mangled operator name
  (`COV[`, `COVERAGEX[` → 400). Only the missing-scope case is silent, which is why the
  scope invariant is enforced in code rather than trusted to upstream validation.
- **`escape` is mandatory on all user-supplied text**, never optional, never skippable by a
  caller. Escape `"`, `\`, and the operator keywords (`AND OR NOT AREA SEARCH RANGE
  MISSING COVERAGE EXPANSION FullMatch DISTANCE ALL`) with a leading backslash per notes
  §2. Values reaching `field_eq`/`full_match` go through it unconditionally.
- `and_`/`or_` always parenthesise their operands. Never rely on the documented precedence
  (terms → NOT → AND → OR): write it explicitly.
- Use `DISTANCE[...]` with **square brackets** inside Essie; parens leak a Java exception
  (notes §2). If you add a distance helper, add the test that asserts brackets.
- `aggFilters` appears nowhere in this file or any other. Add a test that greps `app/` for
  the literal string `aggFilters` and fails if found.

## `app/engine/dimensions.py`

One frozen registry row per groupable dimension. SPEC §5.1 verbatim, no additions:

```python
@dataclass(frozen=True)
class Dimension:
    key: str                       # "phase"
    area: str                      # "Phase"          -> Essie AREA[...]
    enum_name: str | None          # "Phase"          -> Vocabulary; None => open vocab
    record_path: str               # dotted path into a study record (notes §5)
    is_list: bool                  # multi-valued in the record
    partition: bool                # False => groupby_semantics "overlapping"
    label: str                     # axis label, e.g. "Trial phase"
    query_param: str | None        # for open vocabs discovered via query.*

REGISTRY: Mapping[str, Dimension]
def resolve(key: str) -> Dimension        # raises unplannable_query on unknown
def is_temporal(dim: Dimension) -> bool
```

All ten rows from SPEC §5.1: `phase`, `overall_status`, `study_type`, `sponsor_class`,
`intervention_type`, `start_year`, `country`, `lead_sponsor`, `intervention_name`,
`condition`. `partition=False` for `phase`, `intervention_type`, `country`,
`intervention_name`, `condition`.

Note the trap in SPEC §5.1: `lead_sponsor` is `partition=True` (one lead sponsor per
study, notes §5) even though its vocabulary is open. Open vocabulary and non-partition are
**independent** axes: do not collapse them into one flag.

**`enum_name` and `area` are separate fields for a reason T02 measured.** `/studies/enums`
exposes a `pieces` list mapping each enum type to the `AREA[]` names it governs, and it is
**not 1:1**: `Status` governs both `OverallStatus` and `LastKnownStatus`; `AgencyClass`
governs three. So an enum type name is never usable as an `AREA[]` name. `overall_status`
is `enum_name="Status"`, `area="OverallStatus"`, and picking `LastKnownStatus` instead
would silently answer a different question (it's the last known status of studies that
stopped updating, which is the 16% `UNKNOWN` cohort in notes §6.6).

Add a registry-integrity test using `pieces` from the recorded enums fixture: for every
row with a non-`None` `enum_name`, assert `dim.area` appears in that enum type's `pieces`
list. This catches an `area`/`enum_name` mismatch at test time instead of as a wrong chart.
Reading `pieces` is test-only: it stays outside `CTGClient.enums()`'s frozen signature.

## Tests

Table-driven, asserting the exact strings from notes §2:

| Input | Expected |
|---|---|
| `field_eq("Phase", "PHASE3")` | `AREA[Phase]PHASE3` |
| `missing("Phase")` | `AREA[Phase]MISSING` |
| `full_match("LeadSponsorName", 'Merck Sharp & Dohme LLC')` | matches the T01-verified form |
| `date_range("StartDate", 2020-01-01, 2020-12-31)` | `AREA[StartDate]RANGE[2020-01-01,2020-12-31]` |
| `numeric_range("EnrollmentCount", 500, "MAX")` | `AREA[EnrollmentCount]RANGE[500,MAX]` |

Plus: injection attempts (`Merck" OR AREA[Phase]PHASE3`, `foo\bar`, `x MISSING y`) are
escaped such that the resulting expression contains no unescaped operator; every
`REGISTRY` `enum_name` exists in a recorded `/studies/enums` fixture; every `record_path`
exists in a recorded full-study fixture; no `aggFilters` anywhere in `app/`.

## Done when

A live `scripts/verify_upstream.py --predicates` run reproduces every count in the notes
§2 table (within daily drift) using only builder output: no hand-written query strings.
