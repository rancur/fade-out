import clsx from 'clsx'
import { Check, X, Loader2, Circle, SkipForward, Hourglass, PauseOctagon } from 'lucide-react'
import type { PipelineStep } from '@/api/hooks'

const stepIcons: Record<string, React.ReactNode> = {
  complete: <Check className="w-4 h-4" />,
  completed: <Check className="w-4 h-4" />,
  failed: <X className="w-4 h-4" />,
  running: <Loader2 className="w-4 h-4 animate-spin" />,
  skipped: <SkipForward className="w-3.5 h-3.5" />,
  waiting: <Hourglass className="w-3.5 h-3.5" />,
  interrupted: <PauseOctagon className="w-4 h-4" />,
  pending: <Circle className="w-3.5 h-3.5" />,
}

const stepColors: Record<string, string> = {
  complete: 'border-primary bg-primary/20 text-primary',
  completed: 'border-primary bg-primary/20 text-primary',
  failed: 'border-cyber-red bg-cyber-red/20 text-cyber-red',
  running: 'border-cyber-cyan bg-cyber-cyan/20 text-cyber-cyan shadow-neon-cyan animate-pulse-slow',
  skipped: 'border-gray-600 bg-gray-600/20 text-gray-500',
  waiting: 'border-gold bg-gold/10 text-gold',
  interrupted: 'border-gold border-dashed bg-gold/10 text-gold',
  pending: 'border-gray-700 bg-gray-700/10 text-gray-600',
}

const lineColors: Record<string, string> = {
  complete: 'bg-primary',
  completed: 'bg-primary',
  failed: 'bg-cyber-red',
  running: 'bg-gradient-to-r from-cyber-cyan to-transparent animate-pulse',
  skipped: 'bg-gray-700',
  waiting: 'bg-gold/40',
  interrupted: 'bg-gold/40',
  pending: 'bg-gray-800',
}

/** Skipped because the platform is not configured — hidden from the graph. */
export function isNotConfigured(step: PipelineStep): boolean {
  if (step.status !== 'skipped') return false
  const reason = step.output_json?.reason
  return typeof reason === 'string' && reason.toLowerCase().includes('not configured')
}

interface PipelineProgressProps {
  steps: PipelineStep[]
  vertical?: boolean
}

export default function PipelineProgress({ steps, vertical = false }: PipelineProgressProps) {
  const visible = steps.filter((s) => !isNotConfigured(s))
  if (!visible.length) return null

  return (
    <div
      className={clsx(
        'flex gap-0',
        vertical ? 'flex-col' : 'flex-row items-start',
      )}
    >
      {visible.map((step, i) => (
        <div
          key={step.step_name}
          className={clsx(
            'flex',
            vertical ? 'flex-col items-center' : 'flex-row items-start',
          )}
        >
          {/* Node */}
          <div className="flex flex-col items-center gap-1">
            <div
              className={clsx(
                'w-9 h-9 rounded-full border-2 flex items-center justify-center transition-all duration-300',
                stepColors[step.status] ?? stepColors.pending,
              )}
              title={`${step.step_name}: ${step.status}`}
            >
              {stepIcons[step.status] ?? stepIcons.pending}
            </div>
            <span
              className={clsx(
                'text-[9px] font-mono uppercase tracking-wider max-w-[80px] text-center truncate',
                step.status === 'running'
                  ? 'text-cyber-cyan'
                  : step.status === 'interrupted'
                    ? 'text-gold'
                    : 'text-gray-500',
              )}
            >
              {step.step_name}
            </span>

            {/* Live progress inside the running step node */}
            {step.status === 'running' && step.progress != null && (
              <div className="w-20 space-y-0.5">
                <div className="h-1 bg-dark rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-cyber-cyan to-primary rounded-full transition-all duration-500"
                    style={{ width: `${Math.min(100, Math.max(0, step.progress))}%` }}
                  />
                </div>
                <p className="text-[9px] font-mono text-cyber-cyan text-center">
                  {step.progress}%
                </p>
              </div>
            )}
            {step.status === 'running' && step.progress_detail && (
              <p
                className="text-[9px] font-mono text-gray-500 max-w-[100px] text-center truncate"
                title={step.progress_detail}
              >
                {step.progress_detail}
              </p>
            )}
            {step.status === 'interrupted' && (
              <p className="text-[9px] font-mono text-gold/70">interrupted</p>
            )}
          </div>

          {/* Connector line */}
          {i < visible.length - 1 && (
            <div
              className={clsx(
                vertical ? 'w-0.5 h-6 mx-auto my-1' : 'h-0.5 w-8 mx-1 mt-[17px]',
                lineColors[step.status] ?? lineColors.pending,
                'rounded-full transition-all duration-300',
              )}
            />
          )}
        </div>
      ))}
    </div>
  )
}
