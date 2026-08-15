import type { ExperimentView, ValidationView } from "../workflow/model"
import { Badge } from "./ui/Badge"
import { Panel } from "./ui/Panel"

interface ValidationStepProps {
  experiment: ExperimentView
  validation: ValidationView
}

type Assertion = ExperimentView["experiment"]["assertions"][number]
type Severity = ValidationView["validation"]["findings"][number]["severity"]

const SEVERITY_TONE: Record<Severity, "attention" | "blocking"> = {
  blocking: "blocking",
  warning: "attention",
  unknown: "attention",
}

const ORIGIN_TONE: Record<Assertion["origin"], "accent" | "quiet" | "attention"> = {
  source_file: "quiet",
  user_supplied: "accent",
  inferred: "attention",
}

function renderedValue(assertion: Assertion): string {
  if (assertion.value.state !== "known") return assertion.value.state
  return `${String(assertion.value.value)}${assertion.value.unit ? ` ${assertion.value.unit}` : ""}`
}

export function ValidationStep({ experiment, validation }: ValidationStepProps) {
  const active = new Set(experiment.experiment.active_assertion_ids)
  const assertions = experiment.experiment.assertions.filter((assertion) =>
    active.has(assertion.assertion_id),
  )
  const decision = validation.validation.release_decision
  const findings = validation.validation.findings
  return (
    <>
      <div className="decision-bar" data-status={decision.status}>
        <p className="decision-status">
          <span className="decision-label">Release decision</span>
          <Badge tone={decision.status === "eligible" ? "accent" : "blocking"}>
            {decision.status}
          </Badge>
        </p>
        <dl className="decision-counts">
          <div data-tone={decision.blocking_count > 0 ? "blocking" : "quiet"}>
            <dt>Blockers</dt>
            <dd>{decision.blocking_count}</dd>
          </div>
          <div data-tone={decision.warning_count > 0 ? "attention" : "quiet"}>
            <dt>Warnings</dt>
            <dd>{decision.warning_count}</dd>
          </div>
          <div data-tone={decision.unknown_count > 0 ? "attention" : "quiet"}>
            <dt>Unknowns</dt>
            <dd>{decision.unknown_count}</dd>
          </div>
        </dl>
        {experiment.experiment.data_origin === "synthetic" && (
          <p className="synthetic-banner">Synthetic experiment metadata — not measured</p>
        )}
      </div>
      <Panel
        title="Active metadata assertions"
        description="Origin, transformation, requirement class, and value state are recorded independently."
      >
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">
              Active metadata assertions with their value state, origin, transformation, and
              requirement class
            </caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Value state</th>
                <th scope="col">Origin</th>
                <th scope="col">Transformation</th>
                <th scope="col">Requirement</th>
              </tr>
            </thead>
            <tbody>
              {assertions.map((assertion) => (
                <tr key={assertion.assertion_id}>
                  <td>
                    <span className="field-name">{assertion.field_name}</span>
                    {assertion.supplements_assertion_id && (
                      <span className="assertion-relation">
                        supplements <code>{assertion.supplements_assertion_id}</code>
                      </span>
                    )}
                  </td>
                  <td className="value-state" data-state={assertion.value.state}>
                    {renderedValue(assertion)}
                  </td>
                  <td>
                    <Badge tone={ORIGIN_TONE[assertion.origin]}>{assertion.origin}</Badge>
                  </td>
                  <td>{assertion.transformation}</td>
                  <td>{assertion.requirement_class}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel
        title="Deterministic findings"
        description="Produced by the backend rule set, in the order it returned them."
      >
        {findings.length === 0 ? (
          <p className="empty-note">No blocking, warning, or unknown finding.</p>
        ) : (
          <div className="finding-grid">
            {findings.map((finding) => (
              <article className="finding" data-severity={finding.severity} key={finding.finding_id}>
                <p className="finding-head">
                  <Badge className="severity" tone={SEVERITY_TONE[finding.severity]}>
                    {finding.severity}
                  </Badge>
                  <code>{finding.code}</code>
                </p>
                <p className="finding-message">{finding.message}</p>
                <p className="resolution">
                  <strong>Resolution:</strong> {finding.resolution}
                </p>
                <code className="finding-id">{finding.finding_id}</code>
              </article>
            ))}
          </div>
        )}
      </Panel>
      <section className="evidence-boundaries" aria-labelledby="evidence-boundaries-title">
        <h3 id="evidence-boundaries-title">What this release decision does and does not establish</h3>
        <dl>
          <div data-established="yes">
            <dt>Completeness</dt>
            <dd>Declared fields and visible gaps</dd>
          </div>
          <div data-established="yes">
            <dt>Integrity</dt>
            <dd>Checksums and closed lineage</dd>
          </div>
          <div data-established="no">
            <dt>Scientific validity</dt>
            <dd>Not established by this release decision</dd>
          </div>
          <div data-established="no">
            <dt>Reproducibility</dt>
            <dd>Not established by metadata completeness</dd>
          </div>
        </dl>
      </section>
    </>
  )
}
