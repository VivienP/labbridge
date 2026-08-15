import type { ReactNode } from "react"

import { STAGE_STATUS_TEXT, type StageId, type StageStatus } from "../workflow/stages"

interface StageProps {
  id: StageId
  ordinal: string
  eyebrow: string
  title: string
  summary: string
  status: StageStatus
  children: ReactNode
}

/** One workflow stage frame: position, intent, status, and body. */
export function Stage({ id, ordinal, eyebrow, title, summary, status, children }: StageProps) {
  const titleId = `stage-${id}-title`
  return (
    <section id={`stage-${id}`} className="stage" data-status={status} aria-labelledby={titleId}>
      <div className="stage-rule" aria-hidden="true">
        <span className="stage-ordinal">{ordinal}</span>
      </div>
      <div className="stage-main">
        <header className="stage-head">
          <div className="stage-heading">
            <p className="stage-eyebrow">{eyebrow}</p>
            <h2 id={titleId}>{title}</h2>
          </div>
          <p className="stage-status" data-status={status}>
            <span className="stage-status-mark" aria-hidden="true" />
            {STAGE_STATUS_TEXT[status]}
          </p>
        </header>
        <p className="stage-summary">{summary}</p>
        <div className="stage-body">{children}</div>
      </div>
    </section>
  )
}
