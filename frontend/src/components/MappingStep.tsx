import { useEffect, useState } from "react"

import type { SourceInspectionView } from "../workflow/model"
import { buildImportProfile, type MappingChoice } from "../workflow/profile"
import { Callout } from "./ui/Callout"

type Role = MappingChoice["role"] | ""
interface ChoiceForm {
  sourceColumn: string
  role: Role
  sourceUnit: string
  targetUnit: string
}

interface MappingStepProps {
  inspection: SourceInspectionView
  pending: boolean
  error?: string
  onSubmit: (profile: ReturnType<typeof buildImportProfile>) => void
}

const ROLES: { value: MappingChoice["role"]; label: string }[] = [
  { value: "potential", label: "Potential" },
  { value: "current", label: "Current" },
  { value: "current_density", label: "Current density" },
  { value: "time", label: "Time" },
  { value: "cycle", label: "Cycle" },
  { value: "ignored", label: "Ignored" },
]

export function MappingStep({ inspection, pending, error, onSubmit }: MappingStepProps) {
  const [choices, setChoices] = useState<ChoiceForm[]>([])
  const [localError, setLocalError] = useState<string | null>(null)
  useEffect(() => {
    setChoices(
      inspection.headers.map((sourceColumn) => ({
        sourceColumn,
        role: "",
        sourceUnit: "",
        targetUnit: "",
      })),
    )
    setLocalError(null)
  }, [inspection])

  const update = (index: number, patch: Partial<ChoiceForm>) => {
    setChoices((current) =>
      current.map((choice, choiceIndex) =>
        choiceIndex === index ? { ...choice, ...patch } : choice,
      ),
    )
  }
  const submit = () => {
    if (choices.some((choice) => choice.role === "")) {
      setLocalError("every inspected column requires an explicit mapping decision")
      return
    }
    try {
      onSubmit(buildImportProfile(inspection.headers, choices as MappingChoice[]))
      setLocalError(null)
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "Invalid explicit mapping")
    }
  }
  const undecided = choices.filter((choice) => choice.role === "").length
  return (
    <>
      <div className="inspection-summary">
        <p>
          The backend found {inspection.headers.length} headers and assigned none of them a role.
        </p>
        <p className="inspection-counts">
          {inspection.row_count.toLocaleString()} data rows read · {undecided} column
          {undecided === 1 ? "" : "s"} still undecided
        </p>
      </div>
      <div className="mapping-table" role="group" aria-label="Column mapping decisions">
        <div className="mapping-row mapping-header" aria-hidden="true">
          <span>Source column</span>
          <span>Role</span>
          <span>Source unit</span>
          <span>Target unit</span>
        </div>
        {choices.map((choice, index) => {
          const scientific = choice.role !== "" && choice.role !== "ignored"
          return (
            <div className="mapping-row" data-decided={choice.role !== ""} key={choice.sourceColumn}>
              <code className="column-name">{choice.sourceColumn}</code>
              <label className="mapping-field">
                <span className="mapping-field-label">Role</span>
                <select
                  aria-label={`Role for ${choice.sourceColumn}`}
                  value={choice.role}
                  onChange={(event) => update(index, { role: event.currentTarget.value as Role })}
                >
                  <option value="">Choose explicitly…</option>
                  {ROLES.map((role) => (
                    <option value={role.value} key={role.value}>
                      {role.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="mapping-field">
                <span className="mapping-field-label">Source unit</span>
                <input
                  aria-label={`Source unit for ${choice.sourceColumn}`}
                  value={choice.sourceUnit}
                  disabled={!scientific}
                  onChange={(event) => update(index, { sourceUnit: event.currentTarget.value })}
                  placeholder={scientific ? "e.g. V" : "—"}
                />
              </label>
              <label className="mapping-field">
                <span className="mapping-field-label">Target unit</span>
                <input
                  aria-label={`Target unit for ${choice.sourceColumn}`}
                  value={choice.targetUnit}
                  disabled={!scientific}
                  onChange={(event) => update(index, { targetUnit: event.currentTarget.value })}
                  placeholder={scientific ? "e.g. V" : "—"}
                />
              </label>
            </div>
          )
        })}
      </div>
      {(localError || error) && (
        <Callout tone="blocking" role="alert" title="Mapping rejected">
          <p className="error-message">{localError ?? error}</p>
        </Callout>
      )}
      <div className="action-row">
        <button type="button" className="primary" onClick={submit} disabled={pending} aria-busy={pending}>
          {pending && <span className="spinner" aria-hidden="true" />}
          Normalise explicit mapping
        </button>
        <p className="action-note">
          Sends this profile to the backend, which parses, converts units, assembles one normalised
          observation, and opens an experiment. Nothing is normalised in the browser.
        </p>
      </div>
      {pending && (
        <p className="working-note" role="status">
          Normalising against the declared profile…
        </p>
      )}
    </>
  )
}
