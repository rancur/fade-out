import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  ArrowLeft,
  CheckCircle2,
  RotateCcw,
  Trash2,
  ExternalLink,
  Clock,
  Music,
  Disc3,
  FileText,
  ListOrdered,
  Activity,
  Code2,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { format } from 'date-fns'
import { useMix, useApproveMix, useRetryMix, useDeleteMix } from '@/api/hooks'
import StatusBadge from '@/components/StatusBadge'
import PipelineProgress from '@/components/PipelineProgress'
import EnergyChart from '@/components/EnergyChart'

const tabs = [
  { key: 'overview', label: 'Overview', icon: Disc3 },
  { key: 'description', label: 'Description', icon: FileText },
  { key: 'tracklist', label: 'Tracklist', icon: ListOrdered },
  { key: 'pipeline', label: 'Pipeline', icon: Activity },
  { key: 'raw', label: 'Raw', icon: Code2 },
] as const

type TabKey = (typeof tabs)[number]['key']

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

export default function MixDetail() {
  const { id } = useParams<{ id: string }>()
  const { data: mix, isLoading, error } = useMix(id ?? '')
  const [tab, setTab] = useState<TabKey>('overview')
  const approve = useApproveMix()
  const retry = useRetryMix()
  const deleteMix = useDeleteMix()

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="h-8 w-48 bg-surface-light rounded animate-pulse" />
        <div className="h-64 bg-surface-light rounded-xl animate-pulse" />
      </div>
    )
  }

  if (error || !mix) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-gray-600">
        <p className="font-pixel text-xs">Mix not found</p>
        <Link to="/mixes" className="text-primary text-sm mt-3 hover:underline">
          Back to mixes
        </Link>
      </div>
    )
  }

  const handleApprove = () => {
    approve.mutate(mix.id, {
      onSuccess: () => toast.success('Mix approved for upload'),
      onError: () => toast.error('Failed to approve mix'),
    })
  }

  const handleRetry = () => {
    retry.mutate(mix.id, {
      onSuccess: () => toast.success('Pipeline restarted'),
      onError: () => toast.error('Failed to retry'),
    })
  }

  const handleDelete = () => {
    if (!confirm('Delete this mix permanently?')) return
    deleteMix.mutate(mix.id, {
      onSuccess: () => {
        toast.success('Mix deleted')
        window.history.back()
      },
      onError: () => toast.error('Failed to delete'),
    })
  }

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Back link */}
      <Link
        to="/mixes"
        className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-primary transition-colors"
      >
        <ArrowLeft className="w-4 h-4" /> Back to mixes
      </Link>

      {/* Header */}
      <div className="flex flex-col sm:flex-row gap-6">
        {/* Cover */}
        <div className="w-full sm:w-48 h-48 rounded-xl overflow-hidden bg-surface-light border border-primary/10 shrink-0">
          {mix.cover_art_url ? (
            <img src={mix.cover_art_url} alt={mix.title} className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-primary/10 to-cyber-cyan/5">
              <Music className="w-16 h-16 text-primary/20" />
            </div>
          )}
        </div>

        {/* Info */}
        <div className="flex-1 space-y-3">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h1 className="text-xl font-bold text-gray-100">{mix.title}</h1>
              <p className="text-sm text-gray-500 font-mono mt-1">{mix.filename}</p>
            </div>
            <StatusBadge status={mix.status} size="md" />
          </div>

          {/* Meta row */}
          <div className="flex flex-wrap gap-4 text-sm text-gray-400 font-mono">
            <span className="flex items-center gap-1.5">
              <Clock className="w-4 h-4" /> {formatDuration(mix.duration_seconds)}
            </span>
            {mix.bpm && <span>{mix.bpm} BPM</span>}
            <span>{format(new Date(mix.created_at), 'MMM d, yyyy h:mm a')}</span>
          </div>

          {/* Genres */}
          {mix.genres.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {mix.genres.map((g) => (
                <span key={g} className="text-xs px-2.5 py-1 rounded-full bg-primary/10 text-primary border border-primary/20 font-mono">
                  {g}
                </span>
              ))}
            </div>
          )}

          {/* Links */}
          <div className="flex gap-3">
            {mix.soundcloud_url && (
              <a
                href={mix.soundcloud_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 text-xs text-secondary hover:text-secondary/80 transition-colors"
              >
                <ExternalLink className="w-3.5 h-3.5" /> SoundCloud
              </a>
            )}
            {mix.youtube_url && (
              <a
                href={mix.youtube_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 text-xs text-cyber-red hover:text-cyber-red/80 transition-colors"
              >
                <ExternalLink className="w-3.5 h-3.5" /> YouTube
              </a>
            )}
          </div>

          {/* Action buttons */}
          <div className="flex gap-2 pt-2">
            {mix.status === 'reviewing' && (
              <button
                onClick={handleApprove}
                disabled={approve.isPending}
                className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 disabled:opacity-50 transition-all"
              >
                <CheckCircle2 className="w-4 h-4" />
                {approve.isPending ? 'Approving...' : 'Approve & Upload'}
              </button>
            )}
            {mix.status === 'failed' && (
              <button
                onClick={handleRetry}
                disabled={retry.isPending}
                className="flex items-center gap-2 px-4 py-2 bg-cyber-cyan/10 text-cyber-cyan border border-cyber-cyan/30 rounded-lg text-sm hover:bg-cyber-cyan/20 disabled:opacity-50 transition-all"
              >
                <RotateCcw className="w-4 h-4" />
                {retry.isPending ? 'Retrying...' : 'Retry Pipeline'}
              </button>
            )}
            <button
              onClick={handleDelete}
              disabled={deleteMix.isPending}
              className="flex items-center gap-2 px-4 py-2 bg-cyber-red/5 text-cyber-red/70 border border-cyber-red/20 rounded-lg text-sm hover:bg-cyber-red/10 hover:text-cyber-red disabled:opacity-50 transition-all"
            >
              <Trash2 className="w-4 h-4" />
              Delete
            </button>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-primary/10">
        <div className="flex gap-1 -mb-px">
          {tabs.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={clsx(
                'flex items-center gap-2 px-4 py-3 text-sm font-mono border-b-2 transition-all',
                tab === key
                  ? 'text-primary border-primary'
                  : 'text-gray-500 border-transparent hover:text-gray-300 hover:border-primary/20',
              )}
            >
              <Icon className="w-4 h-4" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6">
        {tab === 'overview' && (
          <div className="space-y-6">
            <div>
              <h3 className="text-sm font-mono text-gray-400 mb-3">Energy Profile</h3>
              <EnergyChart data={mix.energy_data ?? []} />
            </div>
            {mix.ai_cost !== undefined && (
              <div className="flex items-center gap-2 text-sm text-gray-400">
                <span className="font-mono">AI Cost:</span>
                <span className="text-gold font-mono">${mix.ai_cost.toFixed(3)}</span>
              </div>
            )}
          </div>
        )}

        {tab === 'description' && (
          <div className="prose prose-invert max-w-none">
            {mix.description ? (
              <div className="text-sm text-gray-300 whitespace-pre-wrap leading-relaxed">
                {mix.description}
              </div>
            ) : (
              <p className="text-gray-600 text-sm italic">No description generated yet.</p>
            )}
          </div>
        )}

        {tab === 'tracklist' && (
          <div>
            {mix.tracklist && mix.tracklist.length > 0 ? (
              <ol className="space-y-2">
                {mix.tracklist.map((track, i) => (
                  <li
                    key={i}
                    className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-white/[0.02] transition-colors"
                  >
                    <span className="text-xs text-gray-600 font-mono w-6 text-right">{i + 1}</span>
                    <span className="text-sm text-gray-300">{track}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="text-gray-600 text-sm italic">No tracklist available.</p>
            )}
          </div>
        )}

        {tab === 'pipeline' && (
          <div className="space-y-6">
            {mix.pipeline_steps && mix.pipeline_steps.length > 0 ? (
              <>
                <PipelineProgress steps={mix.pipeline_steps} />
                <div className="space-y-2 mt-6">
                  {mix.pipeline_steps.map((step) => (
                    <div
                      key={step.name}
                      className="flex items-center justify-between px-4 py-3 rounded-lg bg-dark/50"
                    >
                      <div className="flex items-center gap-3">
                        <StatusBadge status={step.status} />
                        <span className="text-sm text-gray-300 font-mono">{step.name}</span>
                      </div>
                      <div className="text-[11px] text-gray-600 font-mono">
                        {step.started_at && format(new Date(step.started_at), 'HH:mm:ss')}
                        {step.finished_at && ` - ${format(new Date(step.finished_at), 'HH:mm:ss')}`}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <p className="text-gray-600 text-sm italic">Pipeline not started.</p>
            )}
          </div>
        )}

        {tab === 'raw' && (
          <pre className="text-xs text-gray-400 font-mono overflow-x-auto p-4 bg-dark rounded-lg">
            {JSON.stringify(mix, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}
