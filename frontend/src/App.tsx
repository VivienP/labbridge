import { useReducer, useRef } from "react"

import { ApiRequestError, createApiClient, type ApiClient } from "./api/client"
import { AppHeader } from "./components/AppHeader"
import { MappingStep } from "./components/MappingStep"
import { ObservationStep } from "./components/ObservationStep"
import { PackageStep } from "./components/PackageStep"
import { PassportStep } from "./components/PassportStep"
import { SourceStep } from "./components/SourceStep"
import { Stage } from "./components/Stage"
import { ValidationStep } from "./components/ValidationStep"
import { WorkflowRail } from "./components/WorkflowRail"
import type { ApiError, Remote, WorkflowEvent, WorkflowState } from "./workflow/model"
import { canCreatePackage, initialState, reduce } from "./workflow/model"
import { userAssertionRequest } from "./workflow/profile"
import { deriveStages, nextAction, type StageId, type StageStatus } from "./workflow/stages"

const FIXTURE_NAME = "synthetic-cv-passport-demo.csv"

interface AppProps {
  api?: ApiClient
  loadFixture?: () => Promise<Blob>
}

function remoteData<T>(remote: Remote<T>): T | null {
  return remote.status === "succeeded" ? remote.data : null
}

function remoteError<T>(remote: Remote<T>): string | undefined {
  return remote.status === "failed" ? `${remote.error.code}: ${remote.error.message}` : undefined
}

function firstError(...remotes: Remote<unknown>[]): string | undefined {
  for (const remote of remotes) {
    const message = remoteError(remote)
    if (message) return message
  }
  return undefined
}

function apiError(caught: unknown): ApiError {
  if (caught instanceof ApiRequestError) {
    return { status: caught.status, code: caught.code, message: caught.message }
  }
  return {
    status: 0,
    code: "unexpected_client_error",
    message: caught instanceof Error ? caught.message : "Unexpected client error",
  }
}

function defaultFixture(): Promise<Blob> {
  return fetch(`/demo-fixtures/${FIXTURE_NAME}`).then(async (response) => {
    if (!response.ok) throw new Error(`fixture_http_${response.status}`)
    return response.blob()
  })
}

interface StageCopy {
  eyebrow: string
  title: string
  summary: string
  hint: string
}

const STAGE_COPY: Record<StageId, StageCopy> = {
  source: {
    eyebrow: "Source",
    title: "Retain exact source bytes",
    summary:
      "LabBridge stores the file unchanged, then derives its checksum and identity. It reads no meaning from the contents.",
    hint: "",
  },
  mapping: {
    eyebrow: "Mapping",
    title: "Declare column roles and units",
    summary:
      "No column semantics are inferred. Every inspected column needs an explicit decision before anything is normalised.",
    hint: "Opens once a source is retained. LabBridge will list the headers it read and assign none of them a role.",
  },
  observation: {
    eyebrow: "Observation",
    title: "Normalised observation",
    summary:
      "The backend parses the bytes, converts the declared units, and assembles one observation. The browser displays only what it returns.",
    hint: "Opens once an explicit mapping has been normalised by the backend.",
  },
  validation: {
    eyebrow: "Validation",
    title: "Metadata, provenance, and findings",
    summary:
      "Deterministic completeness rules over the recorded assertions. Origin, transformation, requirement class, and value state stay independent.",
    hint: "Opens once the observation exists and an experiment has been opened for it.",
  },
  passport: {
    eyebrow: "Passport",
    title: "Operator assertion and Passport release",
    summary:
      "A Passport is an immutable snapshot of one experiment version. A correction is appended as a superseding Passport; nothing is overwritten.",
    hint: "Opens once the deterministic validation run has completed.",
  },
  package: {
    eyebrow: "Package",
    title: "Experiment Package",
    summary:
      "One checksummed archive closing the chain from the retained bytes to the released Passport. Verification happens outside the browser.",
    hint: "Opens once a superseding Passport has been released.",
  },
}

export function createIntentKey(scope: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  const label = scope.split(":", 1)[0]?.replace(/[^a-z0-9_-]/gi, "-") || "intent"
  return `cv-passport-demo:${label.slice(0, 40)}:${suffix}`
}

export function App({ api = createApiClient(), loadFixture = defaultFixture }: AppProps) {
  const [state, dispatch] = useReducer(reduce, initialState)
  const keys = useRef(new Map<string, string>())
  const key = (scope: string) => {
    const existing = keys.current.get(scope)
    if (existing) return existing
    const created = createIntentKey(scope)
    keys.current.set(scope, created)
    return created
  }
  const send = (event: WorkflowEvent) => dispatch(event)

  const retain = async (
    bytes: Blob,
    filename: string,
    dataOrigin: "observed" | "synthetic",
    executionMode: "replay" | "simulation" | "live",
  ) => {
    send({ type: "reset" })
    send({ type: "source.pending" })
    try {
      const source = await api.intakeSource(bytes, {
        filename,
        dataOrigin,
        executionMode,
        idempotencyKey: key(`source:${filename}:${bytes.size}:${dataOrigin}:${executionMode}`),
      })
      send({ type: "source.succeeded", data: source })
      send({ type: "inspection.pending" })
      try {
        const inspection = await api.inspectSource(source.source_artifact_id)
        send({ type: "inspection.succeeded", data: inspection })
      } catch (caught) {
        send({ type: "inspection.failed", error: apiError(caught) })
      }
    } catch (caught) {
      send({ type: "source.failed", error: apiError(caught) })
    }
  }

  const loadSyntheticFixture = async () => {
    try {
      const bytes = await loadFixture()
      await retain(bytes, FIXTURE_NAME, "synthetic", "replay")
    } catch (caught) {
      send({ type: "source.failed", error: apiError(caught) })
    }
  }

  const normalise = async (profileInput: Parameters<ApiClient["createProfile"]>[0]) => {
    const source = remoteData(state.source)
    if (!source) return
    send({ type: "profile.pending" })
    let operation: keyof WorkflowState = "profile"
    try {
      const profile = await api.createProfile(
        profileInput,
        key(`profile:${source.source_artifact_id}:${JSON.stringify(profileInput)}`),
      )
      send({ type: "profile.succeeded", data: profile })
      operation = "normalisation"
      send({ type: "normalisation.pending" })
      const normalisation = await api.normalise(
        source.source_artifact_id,
        profile.profile_id,
        key(`normalisation:${source.source_artifact_id}:${profile.profile_id}`),
      )
      send({ type: "normalisation.succeeded", data: normalisation })
      operation = "plot"
      send({ type: "plot.pending" })
      const observationId = normalisation.result.observation.observation_id
      const plot = await api.plot(observationId)
      send({ type: "plot.succeeded", data: plot })
      operation = "experiment"
      send({ type: "experiment.pending" })
      const experiment = await api.createExperiment(
        observationId,
        key(`experiment:${observationId}`),
      )
      send({ type: "experiment.succeeded", data: experiment })
      operation = "validation"
      send({ type: "validation.pending" })
      const validation = await api.validate(
        experiment.experiment.experiment_id,
        experiment.experiment.version,
        key(`validation:${experiment.experiment.experiment_id}:${experiment.experiment.version}`),
      )
      send({ type: "validation.succeeded", data: validation })
    } catch (caught) {
      send({ type: `${operation}.failed`, error: apiError(caught) } as WorkflowEvent)
    }
  }

  const releaseInitial = async () => {
    const experiment = remoteData(state.experiment)
    if (!experiment) return
    send({ type: "initialPassport.pending" })
    try {
      const released = await api.releasePassport(
        experiment.experiment.experiment_id,
        experiment.experiment.version,
        key(`passport:${experiment.experiment.experiment_id}:${experiment.experiment.version}`),
      )
      send({ type: "initialPassport.succeeded", data: released })
    } catch (caught) {
      send({ type: "initialPassport.failed", error: apiError(caught) })
    }
  }

  const declareReferenceScale = async (value: string) => {
    const current = remoteData(state.supplementedExperiment) ?? remoteData(state.experiment)
    if (!current) return
    const active = new Set(current.experiment.active_assertion_ids)
    const reference = current.experiment.assertions.find(
      (assertion) => active.has(assertion.assertion_id) && assertion.field_name === "reference_scale",
    )
    if (!reference) {
      send({
        type: "supplementedExperiment.failed",
        error: { status: 0, code: "reference_assertion_missing", message: "No active reference-scale assertion." },
      })
      return
    }
    send({ type: "supplementedExperiment.pending" })
    try {
      const request = userAssertionRequest({
        expectedVersion: current.experiment.version,
        value,
        supplementsAssertionId: reference.assertion_id,
      })
      const supplemented = await api.addAssertion(
        current.experiment.experiment_id,
        request,
        key(`assertion:${current.experiment.experiment_id}:${current.experiment.version}:${value}`),
      )
      send({ type: "supplementedExperiment.succeeded", data: supplemented })
      send({ type: "validation.pending" })
      const validation = await api.validate(
        supplemented.experiment.experiment_id,
        supplemented.experiment.version,
        key(`validation:${supplemented.experiment.experiment_id}:${supplemented.experiment.version}`),
      )
      send({ type: "validation.succeeded", data: validation })
      send({ type: "preview.pending" })
      const preview = await api.previewPassport(supplemented.experiment.experiment_id)
      send({ type: "preview.succeeded", data: preview })
    } catch (caught) {
      const error = apiError(caught)
      // Report before refreshing: a slow or failed refresh must not hide the actionable response
      // or leave the step stuck in its pending state.
      send({ type: "supplementedExperiment.failed", error })
      send({ type: "preview.failed", error })
      if (error.status === 409) {
        try {
          const latest = await api.getExperiment(current.experiment.experiment_id)
          send({ type: "supplementedExperiment.succeeded", data: latest })
        } catch {
          // Keep the reported conflict; the declaration can be retried against the known version.
        }
      }
    }
  }

  const releaseSuperseding = async () => {
    const experiment = remoteData(state.supplementedExperiment)
    if (!experiment) return
    send({ type: "supersedingPassport.pending" })
    try {
      const released = await api.releasePassport(
        experiment.experiment.experiment_id,
        experiment.experiment.version,
        key(`passport:${experiment.experiment.experiment_id}:${experiment.experiment.version}`),
      )
      send({ type: "supersedingPassport.succeeded", data: released })
    } catch (caught) {
      send({ type: "supersedingPassport.failed", error: apiError(caught) })
    }
  }

  const createPackage = async () => {
    const experiment = remoteData(state.supplementedExperiment)
    const passport = remoteData(state.supersedingPassport)
    if (!experiment || !passport || !canCreatePackage(state)) return
    send({ type: "package.pending" })
    try {
      const created = await api.createPackage(
        experiment.experiment.experiment_id,
        experiment.experiment.version,
        passport.passport.passport_id,
        key(`package:${experiment.experiment.experiment_id}:${passport.passport.passport_id}`),
      )
      send({ type: "package.succeeded", data: created })
    } catch (caught) {
      send({ type: "package.failed", error: apiError(caught) })
    }
  }

  const downloadPackage = async () => {
    const packageView = remoteData(state.package)
    if (!packageView) return
    send({ type: "package.pending" })
    try {
      const archive = await api.downloadPackage(packageView.package.package_id)
      const url = URL.createObjectURL(archive)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `${packageView.package.data_origin}-experiment-package-${packageView.package.package_id.replaceAll(":", "-")}.zip`
      anchor.click()
      URL.revokeObjectURL(url)
      send({ type: "package.succeeded", data: packageView })
    } catch (caught) {
      send({ type: "package.failed", error: apiError(caught) })
    }
  }

  const source = remoteData(state.source)
  const inspection = remoteData(state.inspection)
  const profile = remoteData(state.profile)
  const normalisationView = remoteData(state.normalisation)
  const plot = remoteData(state.plot)
  const baseExperiment = remoteData(state.experiment)
  const supplementedExperiment = remoteData(state.supplementedExperiment)
  const validation = remoteData(state.validation)
  const initialPassport = remoteData(state.initialPassport)
  const preview = remoteData(state.preview)
  const supersedingPassport = remoteData(state.supersedingPassport)
  const packageView = remoteData(state.package)

  const stages = deriveStages(state)
  const frame = (id: StageId) => {
    const stage = stages.find((candidate) => candidate.id === id)
    return {
      id,
      ordinal: stage?.ordinal ?? "00",
      status: stage?.status ?? ("locked" as StageStatus),
      eyebrow: STAGE_COPY[id].eyebrow,
      title: STAGE_COPY[id].title,
      summary: STAGE_COPY[id].summary,
    }
  }
  const hint = (id: StageId) => <p className="stage-hint">{STAGE_COPY[id].hint}</p>
  const observationError = firstError(state.plot)
  const experimentError = firstError(state.experiment, state.validation)

  return (
    <div className="app">
      <AppHeader
        dataOrigin={source?.data_origin}
        executionMode={source?.execution_mode}
        onReset={source ? () => send({ type: "reset" }) : undefined}
      />
      <div className="app-body">
        <WorkflowRail stages={stages} nextAction={nextAction(state)} />
        <main id="workspace" tabIndex={-1}>
          <Stage {...frame("source")}>
            <SourceStep
              source={source}
              pending={state.source.status === "pending" || state.inspection.status === "pending"}
              error={firstError(state.source, state.inspection)}
              onLoadFixture={() => void loadSyntheticFixture()}
              onUpload={(file, origin, mode) => void retain(file, file.name, origin, mode)}
            />
          </Stage>

          <Stage {...frame("mapping")}>
            {inspection ? (
              <MappingStep
                inspection={inspection}
                pending={
                  state.profile.status === "pending" || state.normalisation.status === "pending"
                }
                error={firstError(state.profile, state.normalisation)}
                onSubmit={(profile) => void normalise(profile)}
              />
            ) : (
              hint("mapping")
            )}
          </Stage>

          <Stage {...frame("observation")}>
            {normalisationView && plot ? (
              <ObservationStep normalisation={normalisationView} plot={plot} />
            ) : observationError ? (
              <p className="error-message" role="alert">
                {observationError}
              </p>
            ) : (
              hint("observation")
            )}
          </Stage>

          <Stage {...frame("validation")}>
            {baseExperiment && validation ? (
              <ValidationStep
                experiment={supplementedExperiment ?? baseExperiment}
                validation={validation}
              />
            ) : experimentError ? (
              <p className="error-message" role="alert">
                {experimentError}
              </p>
            ) : (
              hint("validation")
            )}
          </Stage>

          <Stage {...frame("passport")}>
            {baseExperiment && validation ? (
              <PassportStep
                initialPassport={initialPassport}
                preview={preview}
                supersedingPassport={supersedingPassport}
                pending={[
                  state.initialPassport,
                  state.supplementedExperiment,
                  state.preview,
                  state.supersedingPassport,
                ].some((remote) => remote.status === "pending")}
                error={firstError(
                  state.initialPassport,
                  state.supplementedExperiment,
                  state.preview,
                  state.supersedingPassport,
                )}
                unknownCount={validation.validation.release_decision.unknown_count}
                onReleaseInitial={() => void releaseInitial()}
                onDeclareReferenceScale={(value) => void declareReferenceScale(value)}
                onReleaseSuperseding={() => void releaseSuperseding()}
              />
            ) : (
              hint("passport")
            )}
          </Stage>

          <Stage {...frame("package")}>
            {supersedingPassport ? (
              <PackageStep
                packageView={packageView}
                pending={state.package.status === "pending"}
                error={remoteError(state.package)}
                chain={
                  source && profile && normalisationView
                    ? {
                        sourceArtifactId: source.source_artifact_id,
                        importProfileId: profile.profile_id,
                        observationId: normalisationView.result.observation.observation_id,
                      }
                    : undefined
                }
                onCreate={() => void createPackage()}
                onDownload={() => void downloadPackage()}
              />
            ) : (
              hint("package")
            )}
          </Stage>
        </main>
      </div>
    </div>
  )
}
