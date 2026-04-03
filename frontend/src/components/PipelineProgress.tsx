import clsx from 'clsx'
import { Check, X, Loader2, Circle, SkipForward } from 'lucide-react'
import type { PipelineStep } from '@/api/hooks'

const stepIcons: Record<string, React.ReactNode> = {
  complete: <Check className="w-4 h-4" />,
  failed: <X className="w-4 h-4" />,
  running: <Loader2 className="w-4 h-4 animate-spin" />,
  skipped: <SkipForward className="w-3.5 h-3.5" />,
  pending: <Circle className="w-3.5 h-3.5" />,
}

const stepColors: Record<string, string> = {
  complete: 'border-primary bg-primary/20 text-primary',
  failed: 'border-cyber-red bg-cyber-red/20 text-cyber-red',
  running: 'border-cyber-cyan bg-cyber-cyan/20 text-cyber-cyan shadow-neon-cyan animate-pulse-slow',
  skipped: 'border-gray-600 bg-gray-600/20 text-gray-500',
  pending: 'border-gray-700 bg-gray-700/10 text-gray-600',
}

const lineColors: Record<string, string> = {
  complete: 'bg-primary',
  failed: 'bg-cyber-red',
  running: 'bg-gradient-to-r from-cyber-cyan to-transparent animate-pulse',
  skipped: 'bg-gray-700',
  pending: 'bg-gray-800',
}

interface PipelineProgressProps {
  steps: PipelineStep[]
  vertical?: boolean
}

export default function PipelineProgress({ steps, vertical = false }: PipelineProgressProps) {
  if (!steps.length) return null

  return (
    <div
      className={clsx(
        'flex gap-0',
        vertical ? 'flex-col' : 'flex-row items-center',
      )}
    >
      {steps.map((step, i) => (
        <div
          key={step.name}
          className={clsx(
            'flex items-center',
            vertical ? 'flex-col' : 'flex-row',
          )}
        >
          {/* Node */}
          <div className="flex flex-col items-center gap-1">
            <div
              className={clsx(
                'w-9 h-9 rounded-full border-2 flex items-center justify-center transition-all duration-300',
                stepColors[step.status],
              )}
              title={`${step.name}: ${step.status}`}
            >
              {stepIcons[step.status]}
            </div>
            <span
              className={clsx(
                'text-[9px] font-mono uppercase tracking-wider max-w-[80px] text-center truncate',
                step.status === 'running' ? 'text-cyber-cyan' : 'text-gray-500',
              )}
            >
              {step.name}
            </span>
          </div>

          {/* Connector line */}
          {i < steps.length - 1 && (
            <div
              className={clsx(
                vertical ? 'w-0.5 h-6 mx-auto my-1' : 'h-0.5 w-8 my-auto mx-1',
                lineColors[step.status],
                'rounded-full transition-all duration-300',
              )}
            />
          )}
        </div>
      ))}
    </div>
  )
}
