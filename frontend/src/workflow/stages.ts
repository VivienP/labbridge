import type { Remote, WorkflowState } from "./model"

export type StageId =
  | "source"
  | "mapping"
  | "observation"
  | "validation"
  | "passport"
  | "package"

export type StageStatus = "locked" | "ready" | "running" | "done" | "failed"

export interface StageModel {
  id: StageId
  ordinal: string
  label: string
  status: StageStatus
}

type AnyRemote = Remote<unknown>

const LABELS: Record<StageId, string> = {
  source: "Source",
  mapping: "Mapping",
  observation: "Observation",
  validation: "Validation",
  passport: "Passport",
  package: "Package",
}

export const STAGE_STATUS_TEXT: Record<StageStatus, string> = {
  locked: "Waiting",
  ready: "Ready",
  running: "Working",
  done: "Done",
  failed: "Failed",
}

function stageStatus(remotes: AnyRemote[], locked: boolean, completed: boolean): StageStatus {
  if (remotes.some((remote) => remote.status === "failed")) return "failed"
  if (remotes.some((remote) => remote.status === "pending")) return "running"
  if (completed) return "done"
  return locked ? "locked" : "ready"
}

/** Presentation status per workflow stage, derived only from received API results. */
export function deriveStages(state: WorkflowState): StageModel[] {
  const inspected = state.inspection.status === "succeeded"
  const normalised = state.normalisation.status === "succeeded"
  const plotted = state.plot.status === "succeeded"
  const validated = state.validation.status === "succeeded"
  const superseded = state.supersedingPassport.status === "succeeded"
  const statuses: Record<StageId, StageStatus> = {
    source: stageStatus([state.source, state.inspection], false, inspected),
    mapping: stageStatus([state.profile, state.normalisation], !inspected, normalised),
    observation: stageStatus([state.plot], !normalised, plotted),
    validation: stageStatus([state.experiment, state.validation], !plotted, validated),
    passport: stageStatus(
      [state.initialPassport, state.supplementedExperiment, state.preview, state.supersedingPassport],
      !validated,
      superseded,
    ),
    package: stageStatus([state.package], !superseded, state.package.status === "succeeded"),
  }
  return (Object.keys(LABELS) as StageId[]).map((id, index) => ({
    id,
    ordinal: String(index + 1).padStart(2, "0"),
    label: LABELS[id],
    status: statuses[id],
  }))
}

/** The single next operator action, so the rail never leaves the workflow position implicit. */
export function nextAction(state: WorkflowState): string {
  const failed = deriveStages(state).find((stage) => stage.status === "failed")
  if (failed) return `Resolve the reported ${failed.label.toLowerCase()} failure below.`
  if (state.inspection.status !== "succeeded") {
    return "Retain one CV source: load the synthetic fixture or upload a classified CSV."
  }
  if (state.normalisation.status !== "succeeded") {
    return "Give every source column an explicit role, source unit, and target unit."
  }
  if (state.plot.status !== "succeeded") return "Waiting for the normalised observation."
  if (state.validation.status !== "succeeded") return "Waiting for the deterministic validation run."
  if (state.initialPassport.status !== "succeeded") {
    return "Release the initial Passport for this experiment version."
  }
  if (state.preview.status !== "succeeded") {
    return "Append the user-supplied reference-scale declaration."
  }
  if (state.supersedingPassport.status !== "succeeded") return "Release the superseding Passport."
  if (state.package.status !== "succeeded") return "Create the Experiment Package."
  return "Download the Package, then verify it with the LabBridge CLI."
}
