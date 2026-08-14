import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { App, createIntentKey } from "../App"
import { ApiRequestError, type ApiClient } from "../api/client"

vi.mock("plotly.js-basic-dist-min", () => ({
  default: { react: vi.fn(), purge: vi.fn() },
}))

const source = {
  source_artifact_id: "source:synthetic",
  sha256: "a".repeat(64),
  byte_size: 42,
  media_type: "text/csv",
  filename: "synthetic-cv-passport-demo.csv",
  data_origin: "synthetic",
  execution_mode: "replay",
  state: "accepted_evidence",
  replayed: false,
}

const inspection = {
  source_artifact_id: source.source_artifact_id,
  source_sha256: source.sha256,
  headers: ["sample_index", "channel_a", "channel_b"],
  row_count: 13,
}

const observation = {
  observation_id: "cv-observation:synthetic",
  row_count: 13,
}

const plot = {
  observation_id: observation.observation_id,
  data_origin: "synthetic",
  execution_mode: "replay",
  environment_id: "synthetic_cv_passport_demo",
  provenance: {
    environment_id: "synthetic_cv_passport_demo",
    source_artifact_id: source.source_artifact_id,
    source_sha256: source.sha256,
    import_profile_id: "profile:synthetic",
    transformation_ids: [],
  },
  series: [
    { series_id: "series:potential", role: "potential", unit: "V", values: ["-0.3", "0.3"] },
    { series_id: "series:current", role: "current", unit: "A", values: ["-0.001", "0.001"] },
  ],
}

const referenceAssertion = (state: "unknown" | "known", version: number) => ({
  assertion_id: `assertion:reference:${version}`,
  field_name: "reference_scale",
  value: state === "known" ? { state, value: "RHE" } : { state },
  origin: state === "known" ? "user_supplied" : "unknown",
  transformation: "none",
  requirement_class: "conditional",
})

const experiment = (version: number, state: "unknown" | "known") => ({
  experiment: {
    experiment_id: "experiment:synthetic",
    version,
    observation_id: observation.observation_id,
    assertions: [referenceAssertion(state, version)],
    active_assertion_ids: [`assertion:reference:${version}`],
    validation_ids: [],
    passport_ids: version === 1 ? ["passport:initial"] : ["passport:initial", "passport:final"],
  },
  replayed: false,
})

const validation = (version: number, unknownCount: number) => ({
  validation: {
    validation_id: `validation:${version}`,
    experiment_id: "experiment:synthetic",
    experiment_version: version,
    findings: unknownCount
      ? [{
          finding_id: "finding:reference",
          code: "metadata_unknown",
          severity: "unknown",
          status: "open",
          message: "reference_scale is unknown",
          resolution: "Supply an operator declaration if known.",
        }]
      : [],
    release_decision: {
      status: "eligible",
      blocking_count: 0,
      warning_count: 0,
      unknown_count: unknownCount,
    },
  },
  replayed: false,
})

const passport = (id: string, version: number, status: "preview" | "released", supersedes?: string) => ({
  passport: {
    passport_id: id,
    experiment_id: "experiment:synthetic",
    experiment_version: version,
    release_status: status,
    supersedes_passport_id: supersedes ?? null,
  },
  replayed: false,
})

function fakeApi() {
  return {
    intakeSource: vi.fn().mockResolvedValue(source),
    inspectSource: vi.fn().mockResolvedValue(inspection),
    createProfile: vi.fn().mockResolvedValue({ profile_id: "profile:synthetic", profile: {}, replayed: false }),
    normalise: vi.fn().mockResolvedValue({ result: { observation, graph: { records: [] }, findings: [] }, replayed: false }),
    plot: vi.fn().mockResolvedValue(plot),
    createExperiment: vi.fn().mockResolvedValue(experiment(1, "unknown")),
    getExperiment: vi.fn().mockResolvedValue(experiment(2, "known")),
    addAssertion: vi.fn().mockResolvedValue(experiment(2, "known")),
    validate: vi.fn()
      .mockResolvedValueOnce(validation(1, 1))
      .mockResolvedValueOnce(validation(2, 0)),
    previewPassport: vi.fn().mockResolvedValue(passport("passport:preview", 2, "preview", "passport:initial")),
    releasePassport: vi.fn()
      .mockResolvedValueOnce(passport("passport:initial", 1, "released"))
      .mockResolvedValueOnce(passport("passport:final", 2, "released", "passport:initial")),
    createPackage: vi.fn().mockResolvedValue({
      package: {
        package_id: "package:synthetic",
        passport_id: "passport:final",
        archive_sha256: "b".repeat(64),
        archive_byte_size: 900,
        data_origin: "synthetic",
        execution_mode: "replay",
      },
      replayed: false,
    }),
    downloadPackage: vi.fn().mockResolvedValue(new Blob(["zip"])),
  } as unknown as ApiClient
}

async function reachInitialPassport(api: ApiClient) {
  const user = userEvent.setup()
  render(
    <App
      api={api}
      loadFixture={() => Promise.resolve(new Blob(["sample_index,channel_a,channel_b\n"], { type: "text/csv" }))}
    />,
  )
  await user.click(screen.getByRole("button", { name: "Load synthetic fixture" }))
  const mapping = await screen.findByRole("group", { name: "Column mapping decisions" })
  await user.selectOptions(within(mapping).getByLabelText("Role for sample_index"), "ignored")
  await user.selectOptions(within(mapping).getByLabelText("Role for channel_a"), "potential")
  await user.type(within(mapping).getByLabelText("Source unit for channel_a"), "V")
  await user.type(within(mapping).getByLabelText("Target unit for channel_a"), "V")
  await user.selectOptions(within(mapping).getByLabelText("Role for channel_b"), "current")
  await user.type(within(mapping).getByLabelText("Source unit for channel_b"), "A")
  await user.type(within(mapping).getByLabelText("Target unit for channel_b"), "A")
  await user.click(screen.getByRole("button", { name: "Normalise explicit mapping" }))
  await user.click(await screen.findByRole("button", { name: "Release initial Passport" }))
  await screen.findByText("passport:initial")
  return user
}

describe("single-user CV Passport workflow", () => {
  it("keeps backend idempotency keys bounded for large request intents", () => {
    expect(createIntentKey(`profile:${"x".repeat(2_000)}`).length).toBeLessThanOrEqual(255)
  })

  it("reports a version conflict without waiting for the experiment refresh", async () => {
    const api = fakeApi()
    vi.mocked(api.addAssertion).mockRejectedValueOnce(
      new ApiRequestError(409, "experiment_version_conflict", "Refresh and retry."),
    )
    // A refresh that never settles must not hide the conflict or strand the pending state.
    vi.mocked(api.getExperiment).mockImplementation(() => new Promise(() => {}))
    const user = await reachInitialPassport(api)

    await user.click(screen.getByRole("button", { name: "Add user-supplied RHE declaration" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("experiment_version_conflict")
    expect(screen.getByRole("button", { name: "Add user-supplied RHE declaration" })).toBeEnabled()
  })

  it("uses only API results through source, Passport supersession, and Package release", async () => {
    const user = userEvent.setup()
    const api = fakeApi()
    render(
      <App
        api={api}
        loadFixture={() => Promise.resolve(new Blob(["sample_index,channel_a,channel_b\n"], { type: "text/csv" }))}
      />,
    )

    await user.click(screen.getByRole("button", { name: "Load synthetic fixture" }))
    expect(await screen.findByText(source.source_artifact_id)).toBeInTheDocument()

    const mapping = screen.getByRole("group", { name: "Column mapping decisions" })
    await user.selectOptions(within(mapping).getByLabelText("Role for sample_index"), "ignored")
    await user.selectOptions(within(mapping).getByLabelText("Role for channel_a"), "potential")
    await user.type(within(mapping).getByLabelText("Source unit for channel_a"), "V")
    await user.type(within(mapping).getByLabelText("Target unit for channel_a"), "V")
    await user.selectOptions(within(mapping).getByLabelText("Role for channel_b"), "current")
    await user.type(within(mapping).getByLabelText("Source unit for channel_b"), "A")
    await user.type(within(mapping).getByLabelText("Target unit for channel_b"), "A")
    await user.click(screen.getByRole("button", { name: "Normalise explicit mapping" }))

    expect(await screen.findByRole("heading", { name: "Synthetic normalised CV trace" })).toBeInTheDocument()
    expect(vi.mocked(api.createProfile).mock.calls[0]?.[1].length).toBeLessThanOrEqual(255)
    expect(screen.getByText("reference_scale")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Release initial Passport" }))
    expect(await screen.findByText("passport:initial")).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Add user-supplied RHE declaration" }))
    expect(await screen.findByText("passport:preview")).toBeInTheDocument()
    expect(api.addAssertion).toHaveBeenCalledWith(
      "experiment:synthetic",
      expect.objectContaining({ value: expect.objectContaining({ value: "RHE" }) }),
      expect.any(String),
    )

    await user.click(screen.getByRole("button", { name: "Release superseding Passport" }))
    expect(await screen.findByText("passport:final")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Create Experiment Package" }))
    expect(await screen.findByText("package:synthetic")).toBeInTheDocument()
    expect(api.createPackage).toHaveBeenCalledWith(
      "experiment:synthetic",
      2,
      "passport:final",
      expect.any(String),
    )
  })
})
