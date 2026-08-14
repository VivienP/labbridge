import { STAGE_STATUS_TEXT, type StageModel } from "../workflow/stages"

interface WorkflowRailProps {
  stages: StageModel[]
  nextAction: string
}

/** Persistent workflow position: what is done, what is running, and what to do next. */
export function WorkflowRail({ stages, nextAction }: WorkflowRailProps) {
  const active = stages.find((stage) => stage.status === "failed")
    ?? stages.find((stage) => stage.status === "running")
    ?? stages.find((stage) => stage.status === "ready")
  const done = stages.filter((stage) => stage.status === "done").length
  return (
    <nav className="workflow-rail" aria-label="Workflow progress">
      <p className="rail-heading">
        Workflow
        <span className="rail-count">
          {done} of {stages.length} done
        </span>
      </p>
      <ol className="rail-stages">
        {stages.map((stage) => (
          <li key={stage.id}>
            <a
              href={`#stage-${stage.id}`}
              data-status={stage.status}
              aria-current={stage.id === active?.id ? "step" : undefined}
            >
              <span className="rail-ordinal" aria-hidden="true">
                {stage.ordinal}
              </span>
              <span className="rail-text">
                <span className="rail-label">{stage.label}</span>
                <span className="rail-state">{STAGE_STATUS_TEXT[stage.status]}</span>
              </span>
            </a>
          </li>
        ))}
      </ol>
      <p className="rail-next" role="status">
        <span className="rail-next-label">Next</span>
        {nextAction}
      </p>
    </nav>
  )
}
