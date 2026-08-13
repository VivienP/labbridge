import type { ExperimentView, ValidationView } from "../workflow/model"

interface ValidationStepProps {
  experiment: ExperimentView
  validation: ValidationView
}

function renderedValue(assertion: ExperimentView["experiment"]["assertions"][number]): string {
  if (assertion.value.state !== "known") return assertion.value.state
  return `${String(assertion.value.value)}${assertion.value.unit ? ` ${assertion.value.unit}` : ""}`
}

export function ValidationStep({ experiment, validation }: ValidationStepProps) {
  const active = new Set(experiment.experiment.active_assertion_ids)
  const assertions = experiment.experiment.assertions.filter((assertion) => active.has(assertion.assertion_id))
  const decision = validation.validation.release_decision
  return (
    <section className="step" aria-labelledby="validation-title">
      <div className="step-number">04</div>
      <div className="step-content">
        <p className="eyebrow">Deterministic completeness rules</p>
        <h2 id="validation-title">Metadata, provenance, and findings</h2>
        {experiment.experiment.data_origin === "synthetic" && (
          <p className="synthetic-banner">Synthetic experiment metadata — not measured</p>
        )}
        <div className="decision-bar" data-status={decision.status}>
          <strong>{decision.status}</strong>
          <span>{decision.blocking_count} blockers</span>
          <span>{decision.warning_count} warnings</span>
          <span>{decision.unknown_count} unknowns</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Field</th><th>Value state</th><th>Origin</th><th>Transformation</th><th>Requirement</th></tr></thead>
            <tbody>
              {assertions.map((assertion) => (
                <tr key={assertion.assertion_id}>
                  <td>{assertion.field_name}</td>
                  <td className="value-state">{renderedValue(assertion)}</td>
                  <td><span className={`tag origin-${assertion.origin}`}>{assertion.origin}</span></td>
                  <td>{assertion.transformation}</td>
                  <td>{assertion.requirement_class}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="finding-grid">
          {validation.validation.findings.map((finding) => (
            <article className="finding" key={finding.finding_id}>
              <div><span className={`severity severity-${finding.severity}`}>{finding.severity}</span> <code>{finding.code}</code></div>
              <p>{finding.message}</p>
              <p className="resolution"><strong>Resolution:</strong> {finding.resolution}</p>
              <code className="finding-id">{finding.finding_id}</code>
            </article>
          ))}
          {validation.validation.findings.length === 0 && <p>No blocking, warning, or unknown finding.</p>}
        </div>
        <div className="evidence-boundaries" aria-label="Evidence concepts">
          <div><strong>Completeness</strong><span>Declared fields and visible gaps</span></div>
          <div><strong>Integrity</strong><span>Checksums and closed lineage</span></div>
          <div><strong>Scientific validity</strong><span>Not established by this release decision</span></div>
          <div><strong>Reproducibility</strong><span>Not established by metadata completeness</span></div>
        </div>
      </div>
    </section>
  )
}
