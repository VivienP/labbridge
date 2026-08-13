import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { PackageStep } from "../components/PackageStep"
import { PassportStep } from "../components/PassportStep"
import type { PackageView, PassportView } from "../workflow/model"

const initialPassport = {
  replayed: false,
  passport: {
    passport_id: "passport:v1",
    experiment_id: "experiment:one",
    experiment_version: 1,
    release_status: "released",
    supersedes_passport_id: null,
  },
} as unknown as PassportView

const supersedingPassport = {
  replayed: false,
  passport: {
    passport_id: "passport:v2",
    experiment_id: "experiment:one",
    experiment_version: 2,
    release_status: "released",
    supersedes_passport_id: "passport:v1",
  },
} as unknown as PassportView

const packageView = {
  replayed: false,
  package: {
    package_id: "experiment-package:one",
    passport_id: "passport:v2",
    archive_sha256: "b".repeat(64),
    archive_byte_size: 2048,
    data_origin: "synthetic",
    execution_mode: "replay",
  },
} as unknown as PackageView

describe("Passport and Package presentation", () => {
  it("offers an operator declaration with no origin selector", () => {
    const submit = vi.fn()
    render(
      <PassportStep
        initialPassport={initialPassport}
        preview={null}
        supersedingPassport={null}
        pending={false}
        onReleaseInitial={vi.fn()}
        onDeclareReferenceScale={submit}
        onReleaseSuperseding={vi.fn()}
      />,
    )

    expect(screen.getByText(/LabBridge does not infer or validate this reference scale/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/origin/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Add user-supplied RHE declaration" }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it("shows the explicit supersession relation", () => {
    render(
      <PassportStep
        initialPassport={initialPassport}
        preview={supersedingPassport}
        supersedingPassport={supersedingPassport}
        pending={false}
        onReleaseInitial={vi.fn()}
        onDeclareReferenceScale={vi.fn()}
        onReleaseSuperseding={vi.fn()}
      />,
    )

    expect(screen.getAllByText("passport:v2")).not.toHaveLength(0)
    expect(screen.getAllByText(/Supersedes passport:v1/)).not.toHaveLength(0)
  })

  it("labels an observed Passport without a synthetic banner", () => {
    const observedPassport = {
      ...initialPassport,
      passport: { ...initialPassport.passport, data_origin: "observed" as const },
    }
    render(
      <PassportStep
        initialPassport={observedPassport}
        preview={null}
        supersedingPassport={null}
        pending={false}
        onReleaseInitial={vi.fn()}
        onDeclareReferenceScale={vi.fn()}
        onReleaseSuperseding={vi.fn()}
      />,
    )

    expect(screen.getByText("Observed Experiment Passport")).toBeInTheDocument()
    expect(screen.queryByText("Synthetic Experiment Passport")).not.toBeInTheDocument()
  })

  it("does not claim browser download is independent verification", () => {
    render(
      <PackageStep
        packageView={packageView}
        pending={false}
        onCreate={vi.fn()}
        onDownload={vi.fn()}
      />,
    )

    expect(screen.getByText("Released; CLI verification required")).toBeInTheDocument()
    expect(screen.queryByText("Independently verified")).not.toBeInTheDocument()
  })

  it("does not label an observed Package as synthetic", () => {
    const observedPackage = {
      ...packageView,
      package: { ...packageView.package, data_origin: "observed" as const },
    }

    render(
      <PackageStep
        packageView={observedPackage}
        pending={false}
        onCreate={vi.fn()}
        onDownload={vi.fn()}
      />,
    )

    expect(screen.getByText("Observed Experiment Package")).toBeInTheDocument()
    expect(screen.queryByText("Synthetic Experiment Package")).not.toBeInTheDocument()
  })
})
