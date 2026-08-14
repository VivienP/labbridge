import { describe, expect, it } from "vitest"

import type { ApiError, WorkflowState } from "../workflow/model"
import { initialState } from "../workflow/model"
import { deriveStages, nextAction, type StageId, type StageStatus } from "../workflow/stages"

const apiError: ApiError = {
  status: 422,
  code: "experiment_request_invalid",
  message: "experiment contains duplicate assertions",
}

function statuses(state: WorkflowState): Record<StageId, StageStatus> {
  return Object.fromEntries(deriveStages(state).map((stage) => [stage.id, stage.status])) as Record<
    StageId,
    StageStatus
  >
}

function succeeded(...operations: (keyof WorkflowState)[]): WorkflowState {
  return operations.reduce<WorkflowState>(
    (state, operation) => ({ ...state, [operation]: { status: "succeeded", data: {} } }),
    initialState,
  )
}

describe("derived workflow stages", () => {
  it("locks every stage after the first before any source is retained", () => {
    expect(statuses(initialState)).toEqual({
      source: "ready",
      mapping: "locked",
      observation: "locked",
      validation: "locked",
      passport: "locked",
      package: "locked",
    })
  })

  it("opens exactly the next stage as each backend result arrives", () => {
    expect(statuses(succeeded("source", "inspection"))).toMatchObject({
      source: "done",
      mapping: "ready",
      observation: "locked",
    })
    expect(statuses(succeeded("source", "inspection", "profile", "normalisation"))).toMatchObject({
      mapping: "done",
      observation: "ready",
      validation: "locked",
    })
  })

  it("reports a running operation on its own stage", () => {
    const state: WorkflowState = { ...initialState, source: { status: "pending" } }

    expect(statuses(state).source).toBe("running")
  })

  it("attributes a failure to the stage that issued the request", () => {
    const state: WorkflowState = {
      ...succeeded("source", "inspection", "profile", "normalisation", "plot"),
      experiment: { status: "failed", error: apiError },
    }

    expect(statuses(state)).toMatchObject({
      mapping: "done",
      observation: "done",
      validation: "failed",
    })
    expect(nextAction(state)).toBe("Resolve the reported validation failure below.")
  })

  it("names one next action for every position in the workflow", () => {
    expect(nextAction(initialState)).toMatch(/load the synthetic fixture/i)
    expect(nextAction(succeeded("source", "inspection"))).toMatch(/explicit role/i)
    const validated = succeeded(
      "source",
      "inspection",
      "profile",
      "normalisation",
      "plot",
      "experiment",
      "validation",
    )
    expect(nextAction(validated)).toMatch(/Release the initial Passport/i)
  })
})
