import type { components } from "../generated/api-v1"

type Schema<Name extends keyof components["schemas"]> = components["schemas"][Name]
type CVImportProfile = Schema<"CVImportProfile-Input">
type ColumnMapping = Schema<"ColumnMapping">
type UserAssertionRequest = Schema<"UserAssertionRequest">

export interface MappingChoice {
  sourceColumn: string
  role: ColumnMapping["role"]
  sourceUnit: string
  targetUnit: string
}

const unknown = { state: "unknown" as const, value: null, unit: null }

export function buildImportProfile(
  headers: string[],
  choices: MappingChoice[],
): CVImportProfile {
  if (
    choices.length !== headers.length ||
    choices.some((choice, index) => choice.sourceColumn !== headers[index])
  ) {
    throw new Error("every inspected column requires an explicit mapping decision")
  }
  const columns: ColumnMapping[] = choices.map((choice) => {
    if (choice.role === "ignored") {
      return { source_column: choice.sourceColumn, role: "ignored", source_unit: null, target_unit: null }
    }
    if (!choice.sourceUnit.trim() || !choice.targetUnit.trim()) {
      throw new Error("scientific mappings require explicit source and target units")
    }
    return {
      source_column: choice.sourceColumn,
      role: choice.role,
      source_unit: choice.sourceUnit,
      target_unit: choice.targetUnit,
    }
  })
  if (columns.filter((column) => column.role === "potential").length !== 1) {
    throw new Error("exactly one potential mapping is required")
  }
  if (columns.filter((column) => column.role === "current" || column.role === "current_density").length !== 1) {
    throw new Error("exactly one current or current-density mapping is required")
  }
  return {
    schema_version: "1",
    technique: "cyclic_voltammetry",
    environment_id: "cv_passport_demo",
    encoding: "utf-8",
    delimiter: ",",
    decimal_convention: "point",
    header_row: 1,
    missing_value_tokens: ["", "NA"],
    columns,
    metadata: {
      reference_scale: unknown,
      potential_treatment: unknown,
      current_basis: unknown,
      electrode_role: unknown,
      geometric_area: unknown,
      contact_area: unknown,
      scan_rate: unknown,
      cycle_information: unknown,
    },
  }
}

interface AssertionForm {
  expectedVersion: number
  value: string
  supplementsAssertionId: string
}

export function userAssertionRequest(form: AssertionForm): UserAssertionRequest {
  return {
    expected_experiment_version: form.expectedVersion,
    field_name: "reference_scale",
    requirement_class: "conditional",
    transformation: "none",
    value: { state: "known", value: form.value, unit: null },
    evidence_note: "Operator declaration retained as user-supplied demonstration evidence.",
    supplements_assertion_id: form.supplementsAssertionId,
    supersedes_assertion_id: null,
  }
}
