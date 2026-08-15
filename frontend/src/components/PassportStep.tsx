import { useState } from "react"

import type { PassportView } from "../workflow/model"
import { Callout } from "./ui/Callout"

interface PassportStepProps {
  initialPassport: PassportView | null
  preview: PassportView | null
  supersedingPassport: PassportView | null
  pending: boolean
  error?: string
  unknownCount?: number
  onReleaseInitial: () => void
  onDeclareReferenceScale: (value: string) => void
  onReleaseSuperseding: () => void
}

function originOf(...passports: (PassportView | null)[]): string | undefined {
  return passports.find((passport) => passport?.passport.data_origin !== undefined)?.passport
    .data_origin
}

export function PassportStep({
  initialPassport,
  preview,
  supersedingPassport,
  pending,
  error,
  unknownCount,
  onReleaseInitial,
  onDeclareReferenceScale,
  onReleaseSuperseding,
}: PassportStepProps) {
  const [value, setValue] = useState("RHE")
  const origin = originOf(initialPassport, preview, supersedingPassport)
  const recorded =
    unknownCount === undefined
      ? null
      : `${unknownCount} unknown finding${unknownCount === 1 ? "" : "s"} will be recorded in it.`
  return (
    <>
      {origin === "synthetic" && <p className="synthetic-banner">Synthetic Experiment Passport</p>}
      {origin === "observed" && <p className="observed-banner">Observed Experiment Passport</p>}
      <ol className="passport-timeline">
        <li className="timeline-item" data-state={initialPassport ? "done" : "active"}>
          <div className="timeline-mark" aria-hidden="true" />
          <div className="timeline-body">
            {initialPassport ? (
              <div className="passport-card">
                <span>Initial immutable release</span>
                <code data-identity="initial-passport">{initialPassport.passport.passport_id}</code>
              </div>
            ) : (
              <div className="action-row">
                <button
                  type="button"
                  className="primary"
                  disabled={pending}
                  aria-busy={pending}
                  onClick={onReleaseInitial}
                >
                  {pending && <span className="spinner" aria-hidden="true" />}
                  Release initial Passport
                </button>
                <p className="action-note">
                  Creates an immutable snapshot of this experiment version. A released Passport is
                  never edited; a correction is appended as a new superseding Passport.
                </p>
                {recorded && <p className="action-note">{recorded}</p>}
              </div>
            )}
          </div>
        </li>
        {initialPassport && (
          <li className="timeline-item" data-state={preview ? "done" : "active"}>
            <div className="timeline-mark" aria-hidden="true" />
            <div className="timeline-body">
              {preview ? (
                <div className="declaration-record">
                  <p>
                    <strong>Reference-scale declaration recorded</strong> as a{" "}
                    <code>user_supplied</code> assertion. Its stored value and origin are listed in
                    the assertion table above.
                  </p>
                  <p>
                    Operator declaration only; LabBridge does not infer or validate this reference
                    scale as physically correct.
                  </p>
                </div>
              ) : (
                <>
                  <div className="operator-declaration">
                    <label className="field">
                      <span className="field-label">Declared reference scale</span>
                      <input
                        value={value}
                        onChange={(event) => setValue(event.currentTarget.value)}
                      />
                    </label>
                    <p>
                      Operator declaration only; LabBridge does not infer or validate this reference
                      scale as physically correct.
                    </p>
                    <button
                      type="button"
                      className="primary"
                      disabled={pending || value.trim() === ""}
                      aria-busy={pending}
                      onClick={() => onDeclareReferenceScale(value)}
                    >
                      {pending && <span className="spinner" aria-hidden="true" />}
                      Add user-supplied RHE declaration
                    </button>
                  </div>
                  <p className="action-note">
                    Appends a <code>user_supplied</code> assertion, advances the experiment version,
                    and re-runs validation. The unknown assertion it supplements stays in the
                    history.
                  </p>
                </>
              )}
            </div>
          </li>
        )}
        {preview && (
          <li className="timeline-item" data-state={supersedingPassport ? "done" : "active"}>
            <div className="timeline-mark" aria-hidden="true" />
            <div className="timeline-body">
              <div className="passport-card highlighted">
                <span>
                  {preview.passport.release_status === "preview"
                    ? "Passport preview"
                    : "Superseding release"}
                </span>
                <code>{preview.passport.passport_id}</code>
                {preview.passport.supersedes_passport_id && (
                  <p>Supersedes {preview.passport.supersedes_passport_id}</p>
                )}
              </div>
              {!supersedingPassport && (
                <div className="action-row">
                  <button
                    type="button"
                    className="primary"
                    disabled={pending}
                    aria-busy={pending}
                    onClick={onReleaseSuperseding}
                  >
                    {pending && <span className="spinner" aria-hidden="true" />}
                    Release superseding Passport
                  </button>
                  <p className="action-note">
                    Turns the preview above into an immutable release. This is not reversible.
                  </p>
                </div>
              )}
            </div>
          </li>
        )}
        {supersedingPassport && (
          <li className="timeline-item" data-state="done">
            <div className="timeline-mark" aria-hidden="true" />
            <div className="timeline-body">
              <div className="passport-card highlighted">
                <span>Superseding immutable release</span>
                <code data-identity="superseding-passport">
                  {supersedingPassport.passport.passport_id}
                </code>
                {supersedingPassport.passport.supersedes_passport_id && (
                  <p>Supersedes {supersedingPassport.passport.supersedes_passport_id}</p>
                )}
              </div>
            </div>
          </li>
        )}
      </ol>
      {pending && (
        <p className="working-note" role="status">
          Writing to the append-only experiment history…
        </p>
      )}
      {error && (
        <Callout tone="blocking" role="alert" title="Passport step failed">
          <p className="error-message">{error}</p>
        </Callout>
      )}
    </>
  )
}
