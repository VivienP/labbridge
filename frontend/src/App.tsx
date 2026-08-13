import { useReducer, useRef } from "react"

import { ApiRequestError, createApiClient, type ApiClient } from "./api/client"
import { MappingStep } from "./components/MappingStep"
import { ObservationStep } from "./components/ObservationStep"
import { PackageStep } from "./components/PackageStep"
import { PassportStep } from "./components/PassportStep"
import { SourceStep } from "./components/SourceStep"
import { ValidationStep } from "./components/ValidationStep"
import type { ApiError, Remote, WorkflowEvent, WorkflowState } from "./workflow/model"
import { canCreatePackage, initialState, reduce } from "./workflow/model"
import { userAssertionRequest } from "./workflow/profile"

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
      if (error.status === 409) {
        try {
          const latest = await api.getExperiment(current.experiment.experiment_id)
          send({ type: "supplementedExperiment.succeeded", data: latest })
        } catch {
          // Preserve the actionable version-conflict response below.
        }
      }
      send({ type: "preview.failed", error })
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
  const normalisationView = remoteData(state.normalisation)
  const plot = remoteData(state.plot)
  const baseExperiment = remoteData(state.experiment)
  const supplementedExperiment = remoteData(state.supplementedExperiment)
  const validation = remoteData(state.validation)
  const initialPassport = remoteData(state.initialPassport)
  const preview = remoteData(state.preview)
  const supersedingPassport = remoteData(state.supersedingPassport)
  const packageView = remoteData(state.package)
  const passportPending = [state.initialPassport, state.supplementedExperiment, state.preview, state.supersedingPassport]
    .some((remote) => remote.status === "pending")

  return (
    <>
      <header className="hero">
        <p className="product-mark">LabBridge / CV Passport</p>
        <h1>From exact source bytes to a closed evidence Package</h1>
        <p>One operator, one local service, one provenance-preserving vertical slice.</p>
        <p className="status-boundary">
          Implementation evidence only — blocker/warning classification awaits human electrochemistry domain review.
        </p>
      </header>
      <main>
        <SourceStep
          source={source}
          pending={state.source.status === "pending"}
          error={remoteError(state.source) ?? remoteError(state.inspection)}
          onLoadFixture={() => void loadSyntheticFixture()}
          onUpload={(file, origin, mode) => void retain(file, file.name, origin, mode)}
        />
        {inspection && (
          <MappingStep
            inspection={inspection}
            pending={[state.profile, state.normalisation, state.plot, state.experiment, state.validation]
              .some((remote) => remote.status === "pending")}
            error={remoteError(state.profile) ?? remoteError(state.normalisation) ?? remoteError(state.plot) ?? remoteError(state.experiment) ?? remoteError(state.validation)}
            onSubmit={(profile) => void normalise(profile)}
          />
        )}
        {normalisationView && plot && <ObservationStep normalisation={normalisationView} plot={plot} />}
        {baseExperiment && validation && (
          <ValidationStep experiment={supplementedExperiment ?? baseExperiment} validation={validation} />
        )}
        {baseExperiment && validation && (
          <PassportStep
            initialPassport={initialPassport}
            preview={preview}
            supersedingPassport={supersedingPassport}
            pending={passportPending}
            error={remoteError(state.initialPassport) ?? remoteError(state.supplementedExperiment) ?? remoteError(state.preview) ?? remoteError(state.supersedingPassport)}
            onReleaseInitial={() => void releaseInitial()}
            onDeclareReferenceScale={(value) => void declareReferenceScale(value)}
            onReleaseSuperseding={() => void releaseSuperseding()}
          />
        )}
        {supersedingPassport && (
          <PackageStep
            packageView={packageView}
            pending={state.package.status === "pending"}
            error={remoteError(state.package)}
            onCreate={() => void createPackage()}
            onDownload={() => void downloadPackage()}
          />
        )}
      </main>
    </>
  )
}
