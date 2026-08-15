import { useState } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { format } from 'date-fns'
import { toast } from 'sonner'
import { Activity, ChevronDown, ChevronRight, ExternalLink, ListRestart, RotateCcw } from 'lucide-react'
import { useRetryStep, useRereadTracklist, type PipelineStep } from '@/api/hooks'
import { isNotConfigured } from './PipelineProgress'
import StatusBadge from './StatusBadge'

const RETRYABLE = new Set(['failed', 'interrupted', 'completed', 'skipped', 'blocked'])
const REREAD_STEPS = ['analyze', 'generate_description']

function fmtDuration(started: string | null, completed: string | null): string | null {
  if (!started || !completed) return null
  const ms = new Date(completed).getTime() - new Date(started).getTime()
  if (isNaN(ms) || ms < 0) return null
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

function OutputValue({ k, v }: { k: string; v: unknown }) {
  if (typeof v === 'string' && (v.startsWith('http://') || v.startsWith('https://'))) {
    return (
      <a
        href={v}
        target="_blank"
        rel="noreferrer"
        className="text-primary hover:underline inline-flex items-center gap-1 break-all"
      >
        {v} <ExternalLink className="w-3 h-3 shrink-0" />
      </a>
    )
  }
  if (v == null) return <span className="text-gray-600">null</span>
  if (typeof v === 'object') {
    return <span className="text-gray-400 break-all">{JSON.stringify(v)}</span>
  }
  return (
    <span className={clsx('break-all', k.endsWith('_path') ? 'text-gray-500' : 'text-gray-300')}>
      {String(v)}
    </span>
  )
}

function OutputSummary({ output }: { output: Record<string, unknown> }) {
  const entries = Object.entries(output).filter(([, v]) => v !== null && v !== '')
  if (!entries.length) return null
  return (
    <dl className="mt-2 space-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-2 text-[11px] font-mono">
          <dt className="text-gray-600 shrink-0">{k}:</dt>
          <dd className="min-w-0">
            <OutputValue k={k} v={v} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

function StepRow({ step, mixId }: { step: PipelineStep; mixId: string }) {
  const [errorExpanded, setErrorExpanded] = useState(false)
  const retryStep = useRetryStep(mixId)
  const reread = useRereadTracklist()

  const duration = fmtDuration(step.started_at, step.completed_at)
  const hasOutput = step.output_json != null && Object.keys(step.output_json).length > 0
  const canReread = REREAD_STEPS.some((s) => step.step_name.includes(s))

  const handleRetryStep = () => {
    retryStep.mutate(step.step_name, {
      onSuccess: () => toast.success(`Retrying step: ${step.step_name}`),
      onError: () => toast.error(`Failed to retry ${step.step_name}`),
    })
  }

  const handleReread = () => {
    reread.mutate(mixId, {
      onSuccess: () => toast.success('Re-reading tracklist from CUE/analysis'),
      onError: () => toast.error('Failed to trigger tracklist re-read'),
    })
  }

  return (
    <div
      className={clsx(
        'rounded-lg bg-dark/50 px-4 py-3',
        step.status === 'interrupted' && 'border border-dashed border-gold/30',
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge status={step.status} />
          <span className="text-sm text-gray-300 font-mono">{step.step_name}</span>
          {step.retry_count > 0 && (
            <span className="text-[10px] text-gold font-mono px-1.5 py-0.5 rounded bg-gold/10 border border-gold/20">
              attempt {step.retry_count + 1}
            </span>
          )}
          {duration && (
            <span className="text-[11px] text-gray-500 font-mono">{duration}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-gray-600 font-mono">
            {step.started_at && format(new Date(step.started_at), 'HH:mm:ss')}
            {step.completed_at && ` - ${format(new Date(step.completed_at), 'HH:mm:ss')}`}
          </span>
          {RETRYABLE.has(step.status) && (
            <button
              onClick={handleRetryStep}
              disabled={retryStep.isPending}
              title="Retry this step"
              className="flex items-center gap-1 px-2 py-1 text-[11px] font-mono text-cyber-cyan bg-cyber-cyan/5 border border-cyber-cyan/20 rounded hover:bg-cyber-cyan/15 disabled:opacity-50 transition-all"
            >
              <RotateCcw className="w-3 h-3" /> Retry
            </button>
          )}
          {canReread && (
            <button
              onClick={handleReread}
              disabled={reread.isPending}
              title="Re-read tracklist"
              className="flex items-center gap-1 px-2 py-1 text-[11px] font-mono text-cyber-magenta bg-cyber-magenta/5 border border-cyber-magenta/20 rounded hover:bg-cyber-magenta/15 disabled:opacity-50 transition-all"
            >
              <ListRestart className="w-3 h-3" /> Re-read tracklist
            </button>
          )}
          <Link
            to={`/activity?mix_id=${mixId}`}
            title="View activity for this mix"
            className="flex items-center gap-1 px-2 py-1 text-[11px] font-mono text-gray-500 bg-white/[0.02] border border-white/10 rounded hover:text-gray-300 hover:border-white/20 transition-all"
          >
            <Activity className="w-3 h-3" /> Activity
          </Link>
        </div>
      </div>

      {/* Live progress while running */}
      {step.status === 'running' && (
        <div className="mt-3 space-y-1">
          <div className="h-1.5 bg-dark rounded-full overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full bg-gradient-to-r from-cyber-cyan to-primary transition-all duration-500',
                step.progress == null && 'w-full animate-pulse opacity-40',
              )}
              style={
                step.progress != null
                  ? { width: `${Math.min(100, Math.max(0, step.progress))}%` }
                  : undefined
              }
            />
          </div>
          <div className="flex justify-between text-[11px] font-mono">
            <span className="text-gray-500">{step.progress_detail ?? 'working…'}</span>
            {step.progress != null && <span className="text-cyber-cyan">{step.progress}%</span>}
          </div>
        </div>
      )}

      {/* Skipped reason */}
      {step.status === 'skipped' && typeof step.output_json?.reason === 'string' && (
        <p className="mt-2 text-[11px] text-gray-500 font-mono italic">
          skipped: {step.output_json.reason}
        </p>
      )}

      {/* Expandable error */}
      {step.error && (
        <div className="mt-2">
          <button
            onClick={() => setErrorExpanded((e) => !e)}
            className="flex items-center gap-1 text-[11px] font-mono text-cyber-red hover:text-cyber-red/80"
          >
            {errorExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            <span className={clsx(!errorExpanded && 'max-w-[400px] truncate')}>
              {errorExpanded ? 'error' : step.error}
            </span>
          </button>
          {errorExpanded && (
            <pre className="mt-1.5 text-[11px] text-cyber-red/90 font-mono bg-cyber-red/5 border border-cyber-red/20 rounded-lg p-3 whitespace-pre-wrap break-all">
              {step.error}
            </pre>
          )}
        </div>
      )}

      {/* Output summary */}
      {hasOutput && step.status !== 'skipped' && (
        <OutputSummary output={step.output_json!} />
      )}
    </div>
  )
}

export default function PipelineStepList({ steps, mixId }: { steps: PipelineStep[]; mixId: string }) {
  const visible = steps.filter((s) => !isNotConfigured(s))
  const hidden = steps.filter(isNotConfigured)

  return (
    <div className="space-y-2">
      {visible.map((step) => (
        <StepRow key={step.id ?? step.step_name} step={step} mixId={mixId} />
      ))}
      {hidden.length > 0 && (
        <p className="text-[10px] text-gray-600 font-mono px-1 pt-1">
          hidden: {hidden.map((s) => `${s.step_name} (not configured)`).join(', ')}
        </p>
      )}
    </div>
  )
}
