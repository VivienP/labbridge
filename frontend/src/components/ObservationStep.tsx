import type { NormalisationView, PlotSeriesView } from "../workflow/model"
import { Plot } from "./Plot"
import { Badge } from "./ui/Badge"
import { KeyValue } from "./ui/KeyValue"
import { Panel } from "./ui/Panel"

interface ObservationStepProps {
  normalisation: NormalisationView
  plot: PlotSeriesView
}

type StructuralStatus = NormalisationView["result"]["findings"][number]["status"]

const STRUCTURAL_TONE: Record<StructuralStatus, "accent" | "attention" | "blocking"> = {
  pass: "accent",
  warning: "attention",
  blocking: "blocking",
}

export function ObservationStep({ normalisation, plot }: ObservationStepProps) {
  const originLabel = plot.data_origin === "synthetic" ? "Synthetic" : "Observed"
  const findings = normalisation.result.findings
  const passed = findings.filter((finding) => finding.status === "pass")
  const raised = findings.filter((finding) => finding.status !== "pass")
  const records = normalisation.result.graph.records
  return (
    <>
      <Panel
        title={`${originLabel} normalised CV trace`}
        description="Every plotted value is a decimal string returned by the backend."
        aside={
          plot.data_origin === "synthetic" ? (
            <p className="synthetic-banner">Synthetic data — illustrative, not measured</p>
          ) : undefined
        }
      >
        <Plot plot={plot} />
      </Panel>
      <KeyValue
        columns={3}
        items={[
          {
            label: "Observation",
            value: <code data-identity="observation">{plot.observation_id}</code>,
            wide: true,
          },
          { label: "Rows", value: normalisation.result.observation.row_count.toLocaleString() },
          { label: "Environment", value: plot.environment_id },
          {
            label: "Data origin",
            value: (
              <Badge tone={plot.data_origin === "synthetic" ? "attention" : "accent"}>
                {plot.data_origin}
              </Badge>
            ),
          },
          { label: "Execution mode", value: <Badge tone="neutral">{plot.execution_mode}</Badge> },
        ]}
      />
      <div className="disclosure-row">
        {records.length > 0 && (
          <details className="disclosure">
            <summary>
              Transformation provenance
              <span className="disclosure-count">{records.length}</span>
            </summary>
            <ol className="provenance-list">
              {records.map((record) => (
                <li key={record.transformation_id}>
                  <p className="provenance-head">
                    <Badge tone="quiet">{record.kind}</Badge>
                    <span className="provenance-impl">
                      {record.implementation} {record.implementation_version}
                    </span>
                  </p>
                  <code className="provenance-id">{record.transformation_id}</code>
                  {record.parameters.length > 0 && (
                    <ul className="provenance-parameters">
                      {record.parameters.map((parameter) => (
                        <li key={parameter.name}>
                          <span className="parameter-name">{parameter.name}</span>
                          <code>{parameter.value}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ol>
          </details>
        )}
        {findings.length > 0 && (
          <details className="disclosure" open={raised.length > 0}>
            <summary>
              Structural checks
              <span className="disclosure-count">
                {passed.length}/{findings.length} pass
              </span>
            </summary>
            <ul className="findings structural">
              {findings.map((finding) => (
                <li key={finding.finding_id} data-status={finding.status}>
                  <Badge tone={STRUCTURAL_TONE[finding.status]}>{finding.status}</Badge>
                  <code>{finding.code}</code>
                  <span>{finding.message}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      <p className="result-note">
        A structural check confirms only that the file was read and mapped as declared. It
        establishes nothing about the electrochemistry the numbers represent.
      </p>
    </>
  )
}
