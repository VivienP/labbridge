import { describe, expect, it } from "vitest"

import {
  canCreatePackage,
  initialState,
  reduce,
  type ApiError,
  type SourceArtifactView,
} from "../workflow/model"

const source: SourceArtifactView = {
  source_artifact_id: "source:synthetic",
  filename: "synthetic-cv-passport-demo.csv",
  media_type: "text/csv",
  byte_size: 42,
  sha256: "a".repeat(64),
  data_origin: "synthetic",
  execution_mode: "replay",
  state: "committed",
  object_uri: "s3://labbridge/sources/synthetic",
  replayed: false,
}

const apiError: ApiError = {
  status: 422,
  code: "profile_invalid",
  message: "One explicit potential and current mapping is required.",
}

describe("workflow reducer", () => {
  it("keeps successful backend responses when a later step fails", () => {
    const sourced = reduce(initialState, { type: "source.succeeded", data: source })
    const failed = reduce(sourced, { type: "profile.failed", error: apiError })

    expect(failed.source).toEqual({ status: "succeeded", data: source })
    expect(failed.profile).toEqual({ status: "failed", error: apiError })
  })

  it("marks only the requested operation pending", () => {
    const sourced = reduce(initialState, { type: "source.succeeded", data: source })
    const pending = reduce(sourced, { type: "inspection.pending" })

    expect(pending.inspection.status).toBe("pending")
    expect(pending.source).toEqual(sourced.source)
  })

  it("accepts no Package before a released Passport", () => {
    expect(canCreatePackage(initialState)).toBe(false)
  })
})
