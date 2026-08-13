import { describe, expect, it } from "vitest"

import { buildImportProfile, userAssertionRequest } from "../workflow/profile"

describe("explicit mapping serialization", () => {
  it("requires a decision for every inspected column", () => {
    expect(() =>
      buildImportProfile(
        ["sample_index", "channel_a", "channel_b"],
        [
          { sourceColumn: "channel_a", role: "potential", sourceUnit: "V", targetUnit: "V" },
          { sourceColumn: "channel_b", role: "current", sourceUnit: "A", targetUnit: "A" },
        ],
      ),
    ).toThrow("every inspected column requires an explicit mapping decision")
  })

  it("preserves explicit column strings and declares unknown context", () => {
    const profile = buildImportProfile(
      ["sample_index", "channel_a", "channel_b"],
      [
        { sourceColumn: "sample_index", role: "ignored", sourceUnit: "", targetUnit: "" },
        { sourceColumn: "channel_a", role: "potential", sourceUnit: "mV", targetUnit: "V" },
        { sourceColumn: "channel_b", role: "current", sourceUnit: "µA", targetUnit: "A" },
      ],
    )

    expect(profile.columns.map((column) => column.source_column)).toEqual([
      "sample_index",
      "channel_a",
      "channel_b",
    ])
    expect(profile.columns[1]).toMatchObject({ source_unit: "mV", target_unit: "V" })
    expect(profile.environment_id).toBe("cv_passport_demo")
    expect(new Set(Object.values(profile.metadata).map((value) => value.state))).toEqual(
      new Set(["unknown"]),
    )
  })

  it("serializes an operator declaration with no selectable origin", () => {
    const request = userAssertionRequest({
      expectedVersion: 1,
      value: "RHE",
      supplementsAssertionId: "assertion:reference-scale",
    })

    expect(request.value).toEqual({ state: "known", value: "RHE", unit: null })
    expect(Object.keys(request)).not.toContain("origin")
  })
})
