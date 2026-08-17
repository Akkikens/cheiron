// Executes the demo's renderers against real specification shapes and asserts what they drew.
//
// The Python contract test proves the demo *handles* every chart type; this proves the drawing
// is real — that a bar chart emits one rect per row, that a network emits a line per edge, that
// a scatter drops nothing silently. Run from `tests/unit/test_demo_renders.py`, which skips if
// node is unavailable so the suite stays Python-only where it has to be.
//
// Usage: node test_demo_renders.js <path-to-demo/index.html>

const fs = require("fs");
const assert = require("assert");

const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.split("<script>")[1].split("</script>")[0];

// The page reads its palette from CSS custom properties at load. Everything after that is pure
// string building, so a two-line shim is the whole browser this needs.
const shim = `
  const document = {
    documentElement: {},
    getElementById: () => ({ addEventListener() {}, value: "", dataset: {} }),
    querySelectorAll: () => [],
  };
  const getComputedStyle = () => ({ getPropertyValue: (name) => "#4c9aff" });
`;

const exported = `
  module.exports = { barLike, timeSeries, histogram, scatter, network, choropleth, table, kpi,
                     RENDERERS, metaPanels, renderErr };
`;

const module_ = { exports: {} };
new Function("module", "require", shim + script + exported)(module_, require);
const R = module_.exports;

const count = (svg, tag) => (svg.match(new RegExp(`<${tag}\\b`, "g")) || []).length;

// --- bar chart -------------------------------------------------------------------------------
{
  const viz = {
    type: "bar_chart",
    encoding: {
      x: { field: "phase", type: "ordinal", label: "Trial phase" },
      y: { field: "study_count", type: "quantitative", label: "Number of trials", unit: "studies" },
    },
    data: [
      { phase: "PHASE1", phase_label: "Phase 1", study_count: 1039 },
      { phase: "PHASE2", phase_label: "Phase 2", study_count: 1750 },
      { phase: "PHASE3", phase_label: "Phase 3", study_count: 363 },
    ],
  };
  const out = R.barLike(viz);
  assert.strictEqual(count(out, "rect"), 3, "one bar per row");
  assert.ok(out.includes("Phase 2"), "uses the human label, not the enum key");
  assert.ok(out.includes("1,750"), "tooltip carries the formatted value");
}

// --- grouped bars ----------------------------------------------------------------------------
{
  const viz = {
    type: "grouped_bar_chart",
    encoding: {
      x: { field: "phase", type: "ordinal", label: "Trial phase" },
      y: { field: "study_count", type: "quantitative", label: "Trials" },
      series: { field: "series", type: "nominal", label: "Series" },
    },
    data: [
      { phase: "PHASE2", study_count: 400, series: "Merck" },
      { phase: "PHASE2", study_count: 900, series: "Pfizer" },
      { phase: "PHASE3", study_count: 120, series: "Merck" },
      { phase: "PHASE3", study_count: 260, series: "Pfizer" },
    ],
  };
  const out = R.barLike(viz, { grouped: true });
  assert.strictEqual(count(out, "rect"), 4, "one bar per (category, series)");
  assert.ok(out.includes("Merck") && out.includes("Pfizer"), "legend names both series");
}

// --- histogram -------------------------------------------------------------------------------
{
  const viz = {
    type: "histogram",
    encoding: {
      x: { field: "enrollment_count", type: "ordinal", label: "Enrollment",
           bin_start: "bin_start", bin_end: "bin_end" },
      y: { field: "study_count", type: "quantitative", label: "Trials" },
    },
    data: [
      { enrollment_count: "0-10", study_count: 31, bin_start: 0, bin_end: 10 },
      { enrollment_count: "11-50", study_count: 107, bin_start: 11, bin_end: 50 },
    ],
  };
  const out = R.histogram(viz);
  assert.strictEqual(count(out, "rect"), 2, "one bar per bin");
  assert.ok(out.includes("11-50"), "bins are labelled by their range");
}

// --- scatter ---------------------------------------------------------------------------------
{
  const viz = {
    type: "scatter_plot",
    encoding: {
      x: { field: "start_year", type: "temporal", label: "Start year" },
      y: { field: "enrollment", type: "quantitative", label: "Enrollment" },
    },
    data: [
      { nct_id: "NCT1", start_year: 2024, enrollment: 40 },
      { nct_id: "NCT2", start_year: 2025, enrollment: 640 },
      { nct_id: "NCT3", start_year: 2025, enrollment: 0 },
    ],
  };
  const out = R.scatter(viz);
  assert.strictEqual(count(out, "circle"), 3, "every point is plotted, including enrollment 0");
  assert.ok(out.includes("NCT2"), "points carry their study id");
}

// --- network ---------------------------------------------------------------------------------
{
  const viz = {
    type: "network_graph",
    encoding: { nodes: { id: "id", label: "label" }, edges: { source: "source", target: "target" } },
    data: {
      nodes: [
        { id: "Glioblastoma", label: "Glioblastoma", group: "condition", weight: 100 },
        { id: "Temozolomide", label: "Temozolomide", group: "intervention", weight: 20 },
        { id: "Bevacizumab", label: "Bevacizumab", group: "intervention", weight: 8 },
      ],
      edges: [
        { source: "Glioblastoma", target: "Temozolomide", weight: 11 },
        { source: "Glioblastoma", target: "Bevacizumab", weight: 4 },
      ],
    },
  };
  const out = R.network(viz);
  assert.strictEqual(count(out, "circle"), 3, "one node per node");
  // Edges are drawn as quadratic paths curving toward the centre, so a dense graph reads as a
  // weave rather than a scribble. Still exactly one element per edge.
  assert.strictEqual(count(out, "path"), 2, "one path per edge");
  assert.ok(out.includes("condition") && out.includes("intervention"), "legend names both groups");
}

// --- table, kpi, choropleth ------------------------------------------------------------------
{
  const rows = [{ lead_sponsor: "Pfizer", study_count: 3862 }];
  assert.ok(R.table({ data: rows }).includes("3,862"));
  assert.ok(
    R.kpi({ encoding: { value: { field: "study_count", label: "Trials" }, label: { field: "x" } },
            data: [{ study_count: 125, x: "Interventional" }] }).includes("125"),
  );
  const geo = R.choropleth({
    encoding: { location: { field: "iso_a3" }, value: { field: "study_count", label: "Trials" } },
    data: [{ country: "United States", iso_a3: "USA", study_count: 1373 }],
  });
  assert.ok(geo.includes("USA") && geo.includes("1,373"));
}

// --- errors and empties ----------------------------------------------------------------------
{
  assert.ok(R.table({ data: [] }).includes("No rows"), "an empty result says so");
  const err = R.renderErr(
    { error: { code: "unplannable_query", message: "nope", request_id: "abc",
               details: [{ suggestion: "try this" }] } },
    422,
  );
  assert.ok(err.includes("unplannable_query") && err.includes("try this") && err.includes("abc"));
}

// --- meta panels -----------------------------------------------------------------------------
{
  const panel = R.metaPanels({
    coverage: { aggregation_mode: "server_counts", groupby_semantics: "overlapping",
                bucket_sum: 3273, unclassified_count: 169, overlap_note: "…", sample_size: null },
    total_matching_studies: 2927,
    warnings: ["a warning"],
    assumptions: ["an assumption"],
    provenance: { source: "clinicaltrials.gov", api_version: "2.0.5",
                  data_timestamp: "2026-08-14T09:00:05" },
    timing_ms: { total: 420 },
    planner: "heuristic_fallback",
    interpretation: "what was computed",
  });
  for (const expected of ["3,273", "169", "a warning", "an assumption", "2.0.5", "overlapping"]) {
    assert.ok(panel.includes(expected), `meta panel shows ${expected}`);
  }
}

console.log("demo renderers: all assertions passed");
