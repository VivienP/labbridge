import { useEffect, useState } from "react"

import type { SourceInspectionView } from "../workflow/model"
import { buildImportProfile, type MappingChoice } from "../workflow/profile"

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
  return (
    <section className="step" aria-labelledby="mapping-title">
      <div className="step-number">02</div>
      <div className="step-content">
        <p className="eyebrow">No semantic inference</p>
        <h2 id="mapping-title">Explicit column mapping</h2>
        <p className="lede">
          The backend found {inspection.headers.length} headers and assigned none of them a role.
        </p>
        <div className="mapping-table" role="group" aria-label="Column mapping decisions">
          {choices.map((choice, index) => {
            const scientific = choice.role !== "" && choice.role !== "ignored"
            return (
              <div className="mapping-row" key={choice.sourceColumn}>
                <code className="column-name">{choice.sourceColumn}</code>
                <label>
                  Role for {choice.sourceColumn}
                  <select
                    value={choice.role}
                    onChange={(event) => update(index, { role: event.currentTarget.value as Role })}
                  >
                    <option value="">Choose explicitly…</option>
                    <option value="potential">Potential</option>
                    <option value="current">Current</option>
                    <option value="current_density">Current density</option>
                    <option value="time">Time</option>
                    <option value="cycle">Cycle</option>
                    <option value="ignored">Ignored</option>
                  </select>
                </label>
                <label>
                  Source unit for {choice.sourceColumn}
                  <input
                    value={choice.sourceUnit}
                    disabled={!scientific}
                    onChange={(event) => update(index, { sourceUnit: event.currentTarget.value })}
                    placeholder="e.g. V"
                  />
                </label>
                <label>
                  Target unit for {choice.sourceColumn}
                  <input
                    value={choice.targetUnit}
                    disabled={!scientific}
                    onChange={(event) => update(index, { targetUnit: event.currentTarget.value })}
                    placeholder="e.g. V"
                  />
                </label>
              </div>
            )
          })}
        </div>
        {(localError || error) && <p role="alert" className="error">{localError ?? error}</p>}
        <button type="button" className="primary" onClick={submit} disabled={pending}>
          {pending ? "Normalising…" : "Normalise explicit mapping"}
        </button>
      </div>
    </section>
  )
}
