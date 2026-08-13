import { useState } from "react"

import type { PassportView } from "../workflow/model"

interface PassportStepProps {
  initialPassport: PassportView | null
  preview: PassportView | null
  supersedingPassport: PassportView | null
  pending: boolean
  error?: string
  onReleaseInitial: () => void
  onDeclareReferenceScale: (value: string) => void
  onReleaseSuperseding: () => void
}

export function PassportStep({
  initialPassport,
  preview,
  supersedingPassport,
  pending,
  error,
  onReleaseInitial,
  onDeclareReferenceScale,
  onReleaseSuperseding,
}: PassportStepProps) {
  const [value, setValue] = useState("RHE")
  return (
    <section className="step" aria-labelledby="passport-title">
      <div className="step-number">05</div>
      <div className="step-content">
        <p className="eyebrow">Append-only correction</p>
        <h2 id="passport-title">Operator assertion and superseding Passport</h2>
        {(initialPassport?.passport.data_origin === "synthetic" ||
          preview?.passport.data_origin === "synthetic" ||
          supersedingPassport?.passport.data_origin === "synthetic") && (
          <p className="synthetic-banner">Synthetic Experiment Passport</p>
        )}
        {(initialPassport?.passport.data_origin === "observed" ||
          preview?.passport.data_origin === "observed" ||
          supersedingPassport?.passport.data_origin === "observed") && (
          <p>Observed Experiment Passport</p>
        )}
        {!initialPassport && (
          <button type="button" className="primary" disabled={pending} onClick={onReleaseInitial}>
            Release initial Passport
          </button>
        )}
        {initialPassport && (
          <>
            <div className="passport-card">
              <span>Initial immutable release</span>
              <code>{initialPassport.passport.passport_id}</code>
            </div>
            <div className="operator-declaration">
              <label>
                Declared reference scale
                <input value={value} onChange={(event) => setValue(event.currentTarget.value)} />
              </label>
              <p>Operator declaration only; LabBridge does not infer or validate this reference scale as physically correct.</p>
              <button
                type="button"
                className="primary"
                disabled={pending || value.trim() === "" || preview !== null}
                onClick={() => onDeclareReferenceScale(value)}
              >
                Add user-supplied RHE declaration
              </button>
            </div>
          </>
        )}
        {preview && (
          <div className="passport-card highlighted">
            <span>{preview.passport.release_status === "preview" ? "Passport preview" : "Superseding release"}</span>
            <code>{preview.passport.passport_id}</code>
            {preview.passport.supersedes_passport_id && <p>Supersedes {preview.passport.supersedes_passport_id}</p>}
          </div>
        )}
        {supersedingPassport && (
          <div className="passport-card highlighted">
            <span>Superseding immutable release</span>
            <code>{supersedingPassport.passport.passport_id}</code>
            {supersedingPassport.passport.supersedes_passport_id && (
              <p>Supersedes {supersedingPassport.passport.supersedes_passport_id}</p>
            )}
          </div>
        )}
        {preview && !supersedingPassport && (
          <button type="button" className="primary" disabled={pending} onClick={onReleaseSuperseding}>
            Release superseding Passport
          </button>
        )}
        {error && <p role="alert" className="error">{error}</p>}
      </div>
    </section>
  )
}
