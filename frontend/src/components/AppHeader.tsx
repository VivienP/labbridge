import { Badge } from "./ui/Badge"

interface AppHeaderProps {
  dataOrigin?: string
  executionMode?: string
  onReset?: () => void
}

/**
 * The persistent product frame. It carries the run classification received from the backend and the
 * evidence-status boundary that applies to every screen below it.
 */
export function AppHeader({ dataOrigin, executionMode, onReset }: AppHeaderProps) {
  return (
    <header className="app-header">
      <div className="brand-bar">
        <a className="skip-link" href="#workspace">
          Skip to workflow
        </a>
        <h1 className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">LabBridge</span>
          <span className="brand-divider" aria-hidden="true" />
          <span className="brand-context">Cyclic voltammetry — Experiment Passport demo</span>
        </h1>
        <div className="run-context">
          {dataOrigin !== undefined && (
            <span className="run-chip">
              <span className="run-chip-label">Data origin</span>
              <Badge tone={dataOrigin === "synthetic" ? "attention" : "accent"}>{dataOrigin}</Badge>
            </span>
          )}
          {executionMode !== undefined && (
            <span className="run-chip">
              <span className="run-chip-label">Execution mode</span>
              <Badge tone="neutral">{executionMode}</Badge>
            </span>
          )}
          {onReset !== undefined && (
            <button type="button" className="ghost" onClick={onReset}>
              Reset this view
            </button>
          )}
        </div>
      </div>
      <p className="status-boundary">
        Implementation evidence only — blocker/warning classification awaits human electrochemistry
        domain review.
      </p>
    </header>
  )
}
