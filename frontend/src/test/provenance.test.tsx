import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { ValidationStep } from "../components/ValidationStep"
import type { ExperimentView, ValidationView } from "../workflow/model"

const experiment = {
  replayed: false,
  experiment: {
    experiment_id: "experiment:one",
    version: 1,
    assertions: [
      {
        assertion_id: "assertion:reference",
        field_name: "reference_scale",
        origin: "user_supplied",
        transformation: "none",
        requirement_class: "conditional",
        value: { state: "unknown", value: null, unit: null },
        evidence_ids: ["profile:one"],
        evidence_note: "Explicit profile declaration.",
      },
    ],
    active_assertion_ids: ["assertion:reference"],
  },
} as unknown as ExperimentView

const validation = {
  replayed: false,
  validation: {
    validation_id: "validation:one",
    release_decision: {
      status: "eligible",
      blocking_count: 0,
      warning_count: 0,
      unknown_count: 1,
      finding_ids: ["finding:reference"],
    },
    findings: [
      {
        finding_id: "finding:reference",
        code: "metadata.reference_scale.unknown",
        severity: "unknown",
        field_name: "reference_scale",
        requirement_class: "conditional",
        assertion_ids: ["assertion:reference"],
        evidence_ids: ["profile:one"],
        message: "reference_scale remains unknown; the Passport does not infer it.",
        resolution: "Append a user-supplied reference_scale assertion.",
      },
    ],
  },
} as unknown as ValidationView

describe("provenance and evidence boundaries", () => {
  it("renders origin and transformation as separate fields", () => {
    render(<ValidationStep experiment={experiment} validation={validation} />)

    expect(screen.getByText("user_supplied")).toBeInTheDocument()
    expect(screen.getByText("none")).toBeInTheDocument()
    expect(screen.getByText("unknown", { selector: ".value-state" })).toBeInTheDocument()
    expect(screen.getByText("conditional")).toBeInTheDocument()
  })

  it("renders severity, stable rule code, finding identity, and resolution", () => {
    render(<ValidationStep experiment={experiment} validation={validation} />)

    expect(screen.getByText("metadata.reference_scale.unknown")).toBeInTheDocument()
    expect(screen.getByText("finding:reference")).toBeInTheDocument()
    expect(screen.getByText("unknown", { selector: ".severity" })).toBeInTheDocument()
    expect(screen.getByText(/Append a user-supplied reference_scale assertion/)).toBeInTheDocument()
  })

  it("keeps four evidence concepts distinct", () => {
    render(<ValidationStep experiment={experiment} validation={validation} />)

    for (const label of ["Completeness", "Integrity", "Scientific validity", "Reproducibility"]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })
})
