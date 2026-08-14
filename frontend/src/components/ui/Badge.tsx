import type { ReactNode } from "react"

export type Tone = "neutral" | "quiet" | "accent" | "attention" | "blocking"

interface BadgeProps {
  children: ReactNode
  tone?: Tone
  className?: string
}

/** A compact status token. The tone is decorative; the label always carries the meaning. */
export function Badge({ children, tone = "neutral", className }: BadgeProps) {
  return (
    <span className={className ? `badge ${className}` : "badge"} data-tone={tone}>
      {children}
    </span>
  )
}
