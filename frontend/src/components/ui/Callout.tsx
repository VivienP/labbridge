import type { ReactNode } from "react"

interface CalloutProps {
  children: ReactNode
  tone?: "neutral" | "accent" | "attention" | "blocking"
  title?: ReactNode
  role?: "alert" | "status" | "note"
  className?: string
}

/** A bounded note: an operator caveat, an evidence boundary, or a reported failure. */
export function Callout({ children, tone = "neutral", title, role, className }: CalloutProps) {
  return (
    <div className={className ? `callout ${className}` : "callout"} data-tone={tone} role={role}>
      {title !== undefined && <p className="callout-title">{title}</p>}
      <div className="callout-body">{children}</div>
    </div>
  )
}
