import type { components } from "../generated/api-v1"
import type {
  ExperimentView,
  NormalisationView,
  PackageView,
  PassportView,
  PlotSeriesView,
  ProfileView,
  SourceArtifactView,
  SourceInspectionView,
  ValidationView,
} from "../workflow/model"

type Schema<Name extends keyof components["schemas"]> = components["schemas"][Name]
type CVImportProfile = Schema<"CVImportProfile-Input">
type UserAssertionRequest = Schema<"UserAssertionRequest">

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = "ApiRequestError"
  }
}

interface IntakeOptions {
  filename: string
  dataOrigin: "observed" | "synthetic"
  executionMode: "replay" | "simulation" | "live"
  idempotencyKey: string
}

interface ErrorPayload {
  detail?: { code?: unknown; message?: unknown }
}

async function apiError(response: Response): Promise<ApiRequestError> {
  let payload: ErrorPayload = {}
  try {
    payload = (await response.json()) as ErrorPayload
  } catch {
    return new ApiRequestError(response.status, "http_error", response.statusText || "Request failed")
  }
  const code = typeof payload.detail?.code === "string" ? payload.detail.code : "http_error"
  const message =
    typeof payload.detail?.message === "string"
      ? payload.detail.message
      : response.statusText || "Request failed"
  return new ApiRequestError(response.status, code, message)
}

export function createApiClient(fetcher: typeof fetch = fetch) {
  const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
    const response = await fetcher(path, init)
    if (!response.ok) {
      throw await apiError(response)
    }
    return (await response.json()) as T
  }
  const mutation = <T>(
    path: string,
    body: object,
    idempotencyKey: string,
  ): Promise<T> =>
    request<T>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    })

  return {
    intakeSource(bytes: Blob, options: IntakeOptions): Promise<SourceArtifactView> {
      const query = new URLSearchParams({
        filename: options.filename,
        data_origin: options.dataOrigin,
        execution_mode: options.executionMode,
      })
      return request<SourceArtifactView>(`/source-artifacts?${query}`, {
        method: "POST",
        headers: { "Content-Type": bytes.type || "text/csv", "Idempotency-Key": options.idempotencyKey },
        body: bytes,
      })
    },
    inspectSource(sourceArtifactId: string): Promise<SourceInspectionView> {
      return request<SourceInspectionView>("/cv/source-inspections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_artifact_id: sourceArtifactId,
          encoding: "utf-8",
          delimiter: ",",
          header_row: 1,
        }),
      })
    },
    createProfile(profile: CVImportProfile, key: string): Promise<ProfileView> {
      return mutation<ProfileView>("/cv/import-profiles", profile, key)
    },
    normalise(sourceArtifactId: string, profileId: string, key: string): Promise<NormalisationView> {
      return mutation<NormalisationView>(
        "/cv/normalisations",
        { source_artifact_id: sourceArtifactId, profile_id: profileId },
        key,
      )
    },
    plot(observationId: string): Promise<PlotSeriesView> {
      return request(`/cv/normalised-observations/${encodeURIComponent(observationId)}/plot-series`)
    },
    createExperiment(observationId: string, key: string): Promise<ExperimentView> {
      return mutation<ExperimentView>(
        "/experiments",
        { observation_id: observationId, expected_experiment_version: 0 },
        key,
      )
    },
    getExperiment(experimentId: string): Promise<ExperimentView> {
      return request(`/experiments/${encodeURIComponent(experimentId)}`)
    },
    addAssertion(
      experimentId: string,
      body: UserAssertionRequest,
      key: string,
    ): Promise<ExperimentView> {
      return mutation(`/experiments/${encodeURIComponent(experimentId)}/assertions`, body, key)
    },
    validate(experimentId: string, version: number, key: string): Promise<ValidationView> {
      return mutation(
        `/experiments/${encodeURIComponent(experimentId)}/validations`,
        { expected_experiment_version: version },
        key,
      )
    },
    previewPassport(experimentId: string): Promise<PassportView> {
      return request(`/experiments/${encodeURIComponent(experimentId)}/passport-preview`)
    },
    releasePassport(experimentId: string, version: number, key: string): Promise<PassportView> {
      return mutation(
        `/experiments/${encodeURIComponent(experimentId)}/passports`,
        { expected_experiment_version: version },
        key,
      )
    },
    createPackage(
      experimentId: string,
      version: number,
      passportId: string,
      key: string,
    ): Promise<PackageView> {
      return mutation(
        `/experiments/${encodeURIComponent(experimentId)}/packages`,
        { expected_experiment_version: version, passport_id: passportId },
        key,
      )
    },
    async downloadPackage(packageId: string): Promise<Blob> {
      const response = await fetcher(`/experiment-packages/${encodeURIComponent(packageId)}/download`)
      if (!response.ok) {
        throw await apiError(response)
      }
      return response.blob()
    },
  }
}

export type ApiClient = ReturnType<typeof createApiClient>
