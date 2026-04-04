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

function basename(path: string | null): string | null {
  if (!path) return null
  return path.split('/').pop() ?? path
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

  const filename = basename(mix.audio_file_path)

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
          {mix.cover_art_path ? (
            <img src={`/api/mixes/${mix.id}/cover-art`} alt={mix.title} className="w-full h-full object-cover" />
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
              {filename && <p className="text-sm text-gray-500 font-mono mt-1">{filename}</p>}
            </div>
            <StatusBadge status={mix.pipeline_status} size="md" />
          </div>

          {/* Meta row */}
          <div className="flex flex-wrap gap-4 text-sm text-gray-400 font-mono">
            {mix.duration_seconds != null && (
              <span className="flex items-center gap-1.5">
                <Clock className="w-4 h-4" /> {formatDuration(mix.duration_seconds)}
              </span>
            )}
            {mix.pipeline_step && <span>Step: {mix.pipeline_step}</span>}
            <span>{format(new Date(mix.created_at), 'MMM d, yyyy h:mm a')}</span>
          </div>

          {/* Genres */}
          {mix.genres && mix.genres.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {mix.genres.map((g) => (
                <span key={g} className="text-xs px-2.5 py-1 rounded-full bg-primary/10 text-primary border border-primary/20 font-mono">
                  {g}
                </span>
              ))}
            </div>
          )}

          {/* Vibes */}
          {mix.vibes && mix.vibes.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {mix.vibes.map((v) => (
                <span key={v} className="text-xs px-2.5 py-1 rounded-full bg-cyber-magenta/10 text-cyber-magenta border border-cyber-magenta/20 font-mono">
                  {v}
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

          {/* Error */}
          {mix.pipeline_error && (
            <div className="text-xs text-cyber-red bg-cyber-red/5 border border-cyber-red/20 rounded-lg px-3 py-2 font-mono">
              {mix.pipeline_error}
            </div>
          )}

          {/* Action buttons */}
          <div className="flex gap-2 pt-2">
            {mix.pipeline_status === 'draft_review' && (
              <button
                onClick={handleApprove}
                disabled={approve.isPending}
                className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 disabled:opacity-50 transition-all"
              >
                <CheckCircle2 className="w-4 h-4" />
                {approve.isPending ? 'Approving...' : 'Approve & Upload'}
              </button>
            )}
            {mix.pipeline_status === 'failed' && (
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
              <EnergyChart data={mix.energy_profile ?? []} />
            </div>
            {mix.tags && mix.tags.length > 0 && (
              <div>
                <h3 className="text-sm font-mono text-gray-400 mb-2">Tags</h3>
                <div className="flex flex-wrap gap-2">
                  {mix.tags.map((t) => (
                    <span key={t} className="text-xs px-2 py-1 rounded bg-white/5 text-gray-400 font-mono">
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {tab === 'description' && (
          <div className="space-y-8">
            {/* SoundCloud */}
            <div className="border border-secondary/20 rounded-xl overflow-hidden">
              <div className="flex items-center gap-2 px-5 py-3 bg-secondary/5 border-b border-secondary/20">
                <div className="w-2.5 h-2.5 rounded-full bg-secondary shadow-[0_0_6px_theme(colors.secondary)]" />
                <span className="text-sm font-mono text-secondary">SoundCloud</span>
                <span className="text-[11px] text-gray-500 ml-auto font-mono">{mix.title}</span>
              </div>
              <div className="flex flex-col md:flex-row">
                {/* Cover art (square) */}
                <div className="w-full md:w-64 shrink-0">
                  {mix.cover_art_path ? (
                    <img
                      src={`/api/mixes/${mix.id}/cover-art`}
                      alt="SoundCloud cover art"
                      className="w-full md:w-64 md:h-64 object-cover"
                    />
                  ) : (
                    <div className="w-full h-48 md:h-64 bg-gradient-to-br from-secondary/10 to-cyber-magenta/5 flex items-center justify-center">
                      <Music className="w-16 h-16 text-secondary/20" />
                    </div>
                  )}
                </div>
                {/* Description */}
                <div className="flex-1 p-5">
                  {mix.description_soundcloud ? (
                    <div className="text-sm text-gray-300 whitespace-pre-wrap leading-relaxed max-h-[320px] overflow-y-auto pr-2 scrollbar-thin">
                      {mix.description_soundcloud}
                    </div>
                  ) : (
                    <p className="text-gray-600 text-sm italic">No SoundCloud description generated yet.</p>
                  )}
                </div>
              </div>
            </div>

            {/* YouTube */}
            <div className="border border-cyber-red/20 rounded-xl overflow-hidden">
              <div className="flex items-center gap-2 px-5 py-3 bg-cyber-red/5 border-b border-cyber-red/20">
                <div className="w-2.5 h-2.5 rounded-full bg-cyber-red shadow-[0_0_6px_theme(colors.cyber.red)]" />
                <span className="text-sm font-mono text-cyber-red">YouTube</span>
                {mix.title_youtube && (
                  <span className="text-[11px] text-gray-500 ml-auto font-mono">{mix.title_youtube}</span>
                )}
              </div>
              <div className="flex flex-col md:flex-row">
                {/* Thumbnail (16:9) */}
                <div className="w-full md:w-80 shrink-0">
                  {mix.thumbnail_path ? (
                    <img
                      src={`/api/mixes/${mix.id}/thumbnail`}
                      alt="YouTube thumbnail"
                      className="w-full md:w-80 md:h-[180px] object-cover"
                    />
                  ) : (
                    <div className="w-full h-48 md:h-[180px] bg-gradient-to-br from-cyber-red/10 to-cyber-orange/5 flex items-center justify-center">
                      <Music className="w-16 h-16 text-cyber-red/20" />
                    </div>
                  )}
                </div>
                {/* Description */}
                <div className="flex-1 p-5">
                  {mix.description_youtube ? (
                    <div className="text-sm text-gray-300 whitespace-pre-wrap leading-relaxed max-h-[320px] overflow-y-auto pr-2 scrollbar-thin">
                      {mix.description_youtube}
                    </div>
                  ) : (
                    <p className="text-gray-600 text-sm italic">No YouTube description generated yet.</p>
                  )}
                </div>
              </div>
            </div>
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
                    <span className="text-xs text-gray-500 font-mono w-16">
                      {track.timestamp_formatted ?? `${Math.floor(track.timestamp_seconds / 60)}:${String(track.timestamp_seconds % 60).padStart(2, '0')}`}
                    </span>
                    <span className="text-sm text-gray-300">
                      {track.artist} - {track.title}
                    </span>
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
            {mix.steps && mix.steps.length > 0 ? (
              <>
                <PipelineProgress steps={mix.steps} />
                <div className="space-y-2 mt-6">
                  {mix.steps.map((step) => (
                    <div
                      key={step.step_name}
                      className="flex items-center justify-between px-4 py-3 rounded-lg bg-dark/50"
                    >
                      <div className="flex items-center gap-3">
                        <StatusBadge status={step.status} />
                        <span className="text-sm text-gray-300 font-mono">{step.step_name}</span>
                        {step.retry_count > 0 && (
                          <span className="text-[10px] text-gray-500 font-mono">
                            (retries: {step.retry_count})
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-3">
                        {step.error && (
                          <span className="text-[10px] text-cyber-red font-mono max-w-[200px] truncate" title={step.error}>
                            {step.error}
                          </span>
                        )}
                        <span className="text-[11px] text-gray-600 font-mono">
                          {step.started_at && format(new Date(step.started_at), 'HH:mm:ss')}
                          {step.completed_at && ` - ${format(new Date(step.completed_at), 'HH:mm:ss')}`}
                        </span>
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
