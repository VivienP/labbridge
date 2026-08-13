import type { components } from "../generated/api-v1"

type Schema<Name extends keyof components["schemas"]> = components["schemas"][Name]

export type SourceArtifactView = Schema<"SourceArtifactView">
export type SourceInspectionView = Schema<"SourceInspectionView">
export type ProfileView = Schema<"ProfileView">
export type NormalisationView = Schema<"NormalisationView">
export type PlotSeriesView = Schema<"PlotSeriesView">
export type ExperimentView = Schema<"ExperimentView">
export type ValidationView = Schema<"ValidationView">
export type PassportView = Schema<"PassportView">
export type PackageView = Schema<"PackageView">

export interface ApiError {
  status: number
  code: string
  message: string
}

export type Remote<T> =
  | { status: "idle" }
  | { status: "pending" }
  | { status: "succeeded"; data: T }
  | { status: "failed"; error: ApiError }

export interface WorkflowState {
  source: Remote<SourceArtifactView>
  inspection: Remote<SourceInspectionView>
  profile: Remote<ProfileView>
  normalisation: Remote<NormalisationView>
  plot: Remote<PlotSeriesView>
  experiment: Remote<ExperimentView>
  validation: Remote<ValidationView>
  initialPassport: Remote<PassportView>
  supplementedExperiment: Remote<ExperimentView>
  preview: Remote<PassportView>
  supersedingPassport: Remote<PassportView>
  package: Remote<PackageView>
}

type Operation = keyof WorkflowState
type ViewFor<Key extends Operation> = WorkflowState[Key] extends Remote<infer View> ? View : never
type PendingEvent = { [Key in Operation]: { type: `${Key}.pending` } }[Operation]
type FailedEvent = {
  [Key in Operation]: { type: `${Key}.failed`; error: ApiError }
}[Operation]
type SucceededEvent = {
  [Key in Operation]: { type: `${Key}.succeeded`; data: ViewFor<Key> }
}[Operation]

export type WorkflowEvent = PendingEvent | FailedEvent | SucceededEvent | { type: "reset" }

const idle = <T>(): Remote<T> => ({ status: "idle" })

export const initialState: WorkflowState = {
  source: idle(),
  inspection: idle(),
  profile: idle(),
  normalisation: idle(),
  plot: idle(),
  experiment: idle(),
  validation: idle(),
  initialPassport: idle(),
  supplementedExperiment: idle(),
  preview: idle(),
  supersedingPassport: idle(),
  package: idle(),
}

export function reduce(state: WorkflowState, event: WorkflowEvent): WorkflowState {
  if (event.type === "reset") {
    return initialState
  }
  const separator = event.type.lastIndexOf(".")
  const operation = event.type.slice(0, separator) as Operation
  const transition = event.type.slice(separator + 1)
  if (transition === "pending") {
    return { ...state, [operation]: { status: "pending" } }
  }
  if (transition === "failed" && "error" in event) {
    return { ...state, [operation]: { status: "failed", error: event.error } }
  }
  if (transition === "succeeded" && "data" in event) {
    return { ...state, [operation]: { status: "succeeded", data: event.data } }
  }
  return state
}

export function canCreatePackage(state: WorkflowState): boolean {
  return (
    state.supersedingPassport.status === "succeeded" &&
    state.supersedingPassport.data.passport.release_status === "released"
  )
}
