import { render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ObservationStep } from "../components/ObservationStep"
import type { NormalisationView, PlotSeriesView } from "../workflow/model"

const { plotlyReact } = vi.hoisted(() => ({ plotlyReact: vi.fn() }))
vi.mock("plotly.js-basic-dist-min", () => ({
  default: { react: plotlyReact, purge: vi.fn() },
}))

const plot: PlotSeriesView = {
  observation_id: "cv-observation:one",
  data_origin: "synthetic",
  execution_mode: "replay",
  environment_id: "synthetic_cv_passport_demo",
  provenance: {
    environment_id: "synthetic_cv_passport_demo",
    source_artifact_id: "source:one",
    source_sha256: "a".repeat(64),
    import_profile_id: "profile:one",
    transformation_ids: ["transform:parse", "transform:potential", "transform:current"],
  },
  series: [
    {
      series_id: "series:potential",
      schema_version: "1",
      dtype: "decimal",
      shape: [3],
      source_column: "channel_a",
      role: "potential",
      source_unit: "V",
      unit: "V",
      values: ["-0.3", "0.3", "-0.3"],
      transformation_id: "transform:potential",
    },
    {
      series_id: "series:current",
      schema_version: "1",
      dtype: "decimal",
      shape: [3],
      source_column: "channel_b",
      role: "current",
      source_unit: "A",
      unit: "A",
      values: ["-0.0012", "0.00091", "-0.0012"],
      transformation_id: "transform:current",
    },
  ],
}

const normalisation = {
  result: {
    observation: { observation_id: plot.observation_id, row_count: 3 },
    graph: { records: [] },
    findings: [],
  },
  replayed: false,
} as unknown as NormalisationView

describe("backend-approved observation presentation", () => {
  beforeEach(() => plotlyReact.mockClear())

  it("passes received values to Plotly in their original order", () => {
    render(<ObservationStep normalisation={normalisation} plot={plot} />)

    const traces = plotlyReact.mock.calls[0]?.[1]
    expect(traces[0].x).toEqual(["-0.3", "0.3", "-0.3"])
    expect(traces[0].y).toEqual(["-0.0012", "0.00091", "-0.0012"])
  })

  it("labels the plot and textual fallback as synthetic", () => {
    render(<ObservationStep normalisation={normalisation} plot={plot} />)

    expect(screen.getByRole("heading", { name: "Synthetic normalised CV trace" })).toBeInTheDocument()
    expect(
      screen.getByText((_, element) =>
        element?.classList.contains("plot-summary") === true &&
        /series:potential.*V.*series:current.*A/.test(element.textContent ?? ""),
      ),
    ).toBeInTheDocument()
  })

  it("does not relabel an observed response as synthetic", () => {
    const observed = { ...plot, data_origin: "observed" as const }

    render(<ObservationStep normalisation={normalisation} plot={observed} />)

    expect(screen.getByRole("heading", { name: "Observed normalised CV trace" })).toBeInTheDocument()
    expect(screen.queryByText(/Synthetic data/)).not.toBeInTheDocument()
    expect(screen.getByLabelText("Observed normalised CV trace plot")).toBeInTheDocument()
    expect(plotlyReact.mock.calls[0]?.[1][0].name).toBe("Observed CV")
  })
})
