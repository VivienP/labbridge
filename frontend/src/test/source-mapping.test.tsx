import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { MappingStep } from "../components/MappingStep"
import { SourceStep } from "../components/SourceStep"
import type { SourceArtifactView, SourceInspectionView } from "../workflow/model"

const source: SourceArtifactView = {
  source_artifact_id: "source:synthetic",
  filename: "synthetic-cv-passport-demo.csv",
  media_type: "text/csv",
  byte_size: 298,
  sha256: "a".repeat(64),
  data_origin: "synthetic",
  execution_mode: "replay",
  state: "committed",
  object_uri: "s3://labbridge/source",
  replayed: false,
}

const inspection: SourceInspectionView = {
  source_artifact_id: source.source_artifact_id,
  source_sha256: source.sha256,
  headers: ["sample_index", "channel_a", "channel_b"],
  row_count: 13,
}

describe("source and mapping presentation", () => {
  it("labels the retained source as synthetic on the visible surface", () => {
    render(
      <SourceStep
        source={source}
        pending={false}
        onLoadFixture={vi.fn()}
        onUpload={vi.fn()}
      />,
    )

    expect(screen.getByText("Synthetic data — not measured")).toBeInTheDocument()
    expect(screen.getByText(source.filename)).toBeInTheDocument()
    expect(screen.getByText(source.sha256)).toBeInTheDocument()
    expect(screen.getByText("synthetic + replay")).toBeInTheDocument()
  })

  it("requires explicit origin and execution-mode declarations for uploads", () => {
    const upload = vi.fn()
    render(
      <SourceStep
        source={null}
        pending={false}
        onLoadFixture={vi.fn()}
        onUpload={upload}
      />,
    )
    const file = new File(["potential,current\n0,0\n"], "operator.csv", { type: "text/csv" })
    fireEvent.change(screen.getByLabelText("CV CSV"), { target: { files: [file] } })

    const button = screen.getByRole("button", { name: "Upload classified CSV" })
    expect(screen.getByLabelText("Data origin")).toHaveValue("")
    expect(screen.getByLabelText("Execution mode")).toHaveValue("")
    expect(button).toBeDisabled()

    fireEvent.change(screen.getByLabelText("Data origin"), { target: { value: "observed" } })
    fireEvent.change(screen.getByLabelText("Execution mode"), { target: { value: "live" } })
    fireEvent.click(button)

    expect(upload).toHaveBeenCalledWith(file, "observed", "live")
  })

  it("shows source columns without roles before the operator maps them", () => {
    render(<MappingStep inspection={inspection} pending={false} onSubmit={vi.fn()} />)

    expect(screen.getByRole("heading", { name: "Explicit column mapping" })).toBeInTheDocument()
    expect(screen.getByLabelText("Role for sample_index")).toHaveValue("")
    expect(screen.getByLabelText("Role for channel_a")).toHaveValue("")
    expect(screen.getByLabelText("Role for channel_b")).toHaveValue("")
  })

  it("blocks an incomplete mapping before submission", () => {
    const submit = vi.fn()
    render(<MappingStep inspection={inspection} pending={false} onSubmit={submit} />)

    fireEvent.click(screen.getByRole("button", { name: "Normalise explicit mapping" }))

    expect(submit).not.toHaveBeenCalled()
    expect(screen.getByRole("alert")).toHaveTextContent(
      "every inspected column requires an explicit mapping decision",
    )
  })
})
