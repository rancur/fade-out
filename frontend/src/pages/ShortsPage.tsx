import { useEffect, useRef, useState } from 'react'
import clsx from 'clsx'
import { format } from 'date-fns'
import { toast } from 'sonner'
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  ExternalLink,
  Filter,
  Loader2,
  Music,
  Pencil,
  RefreshCw,
  ScanSearch,
  SkipForward,
  Smartphone,
  Sparkles,
  Undo2,
  UploadCloud,
  X,
} from 'lucide-react'
import {
  useRegenerateShortMetadata,
  useShorts,
  useShortsScanStatus,
  useShortsStats,
  useSkipShort,
  useStartShortsScan,
  useUpdateShort,
  useUploadShort,
  type ActivityItem,
  type ShortItem,
  type ShortsScanSummary,
} from '@/api/hooks'
import { wsManager, type WsMessage } from '@/api/ws'
import { useQueryClient } from '@tanstack/react-query'

const PAGE_SIZE = 12
const STATUS_FILTERS = [
  'all',
  'detected',
  'analyzing',
  'ready',
  'queued',
  'uploading',
  'uploaded',
  'failed',
  'skipped',
] as const

// Status chip palette (cyberpunk theme tokens from tailwind config)
const STATUS_STYLES: Record<string, string> = {
  detected: 'text-gray-400 border-gray-500/40 bg-gray-500/10',
  analyzing: 'text-cyber-cyan border-cyber-cyan/40 bg-cyber-cyan/10',
  ready: 'text-cyber-lime border-cyber-lime/40 bg-cyber-lime/10',
  queued: 'text-gold border-gold/40 bg-gold/10',
  uploading: 'text-cyber-cyan border-cyber-cyan/40 bg-cyber-cyan/10',
  uploaded: 'text-primary border-primary/40 bg-primary/10',
  failed: 'text-cyber-red border-cyber-red/40 bg-cyber-red/10',
  skipped: 'text-gray-500 border-white/10 bg-white/5',
}

function formatDuration(seconds: number): string {
  const total = Math.round(seconds)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

function scanSummaryText(s: ShortsScanSummary): string {
  return (
    `Scan ${s.status}: ${s.files_seen} files seen, ${s.ingested} ingested, ` +
    `${s.already_known} already known, ${s.catalog_matched} matched to existing uploads` +
    (s.errors?.length ? `, ${s.errors.length} errors` : '')
  )
}

function StatusChip({ status }: { status: string }) {
  const busy = status === 'analyzing' || status === 'uploading'
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-mono uppercase tracking-wider border',
        STATUS_STYLES[status] ?? STATUS_STYLES.detected,
      )}
    >
      {busy && <Loader2 className="w-2.5 h-2.5 animate-spin" />}
      {status}
    </span>
  )
}

function ShortCard({ short }: { short: ShortItem }) {
  const upload = useUploadShort()
  const skip = useSkipShort()
  const regenerate = useRegenerateShortMetadata()
  const update = useUpdateShort()

  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(short.title ?? '')
  const [description, setDescription] = useState(short.description ?? '')

  // Keep local edit state in sync when fresh data arrives (unless editing)
  useEffect(() => {
    if (!editing) {
      setTitle(short.title ?? '')
      setDescription(short.description ?? '')
    }
  }, [short.title, short.description, editing])

  const canUpload =
    !!short.title && ['ready', 'queued', 'failed', 'skipped'].includes(short.status)
  const canSkip = !['uploaded', 'uploading', 'skipped'].includes(short.status)
  const canEdit = !['uploaded', 'uploading'].includes(short.status)
  const recorded = short.detected_at ? new Date(short.detected_at) : null

  const saveEdits = () => {
    update.mutate(
      { id: short.id, title, description },
      {
        onSuccess: () => {
          setEditing(false)
          toast.success('Short metadata saved')
        },
        onError: () => toast.error('Failed to save short metadata'),
      },
    )
  }

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-4 space-y-3 card-hover hover:border-primary/30">
      {/* Top row: filename + status */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-mono text-gray-400 truncate" title={short.file_path}>
            {short.filename}
          </p>
          <div className="flex items-center gap-3 mt-1 text-[11px] text-gray-500 font-mono">
            {short.duration_seconds != null && (
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" /> {formatDuration(short.duration_seconds)}
              </span>
            )}
            {short.width != null && short.height != null && short.width > 0 && (
              <span>
                {short.width}x{short.height}
              </span>
            )}
            {recorded && <span>{format(recorded, 'MMM d, yyyy')}</span>}
          </div>
        </div>
        <StatusChip status={short.status} />
      </div>

      {/* Track ID */}
      <div className="flex items-center gap-2 text-xs">
        <Music className="w-3.5 h-3.5 text-secondary shrink-0" />
        {short.track_artist && short.track_title ? (
          <span className="text-gray-300 truncate">
            {short.track_artist} — {short.track_title}
          </span>
        ) : (
          <span className="text-gray-600 font-mono">no track ID</span>
        )}
      </div>

      {/* Title / description (inline editable) */}
      {editing ? (
        <div className="space-y-2">
          <input
            aria-label="Short title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={100}
            className="w-full px-3 py-2 bg-dark border border-primary/20 rounded-lg text-sm text-gray-200 focus:border-primary/50 focus:outline-none font-mono"
          />
          <textarea
            aria-label="Short description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={5}
            className="w-full px-3 py-2 bg-dark border border-primary/20 rounded-lg text-xs text-gray-300 focus:border-primary/50 focus:outline-none font-mono"
          />
          <div className="flex gap-2">
            <button
              onClick={saveEdits}
              disabled={update.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyber-lime/10 text-cyber-lime border border-cyber-lime/30 text-[11px] font-mono uppercase hover:bg-cyber-lime/20 disabled:opacity-50 transition-all"
            >
              <Check className="w-3.5 h-3.5" /> Save
            </button>
            <button
              onClick={() => setEditing(false)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 text-gray-400 border border-white/10 text-[11px] font-mono uppercase hover:text-gray-200 transition-all"
            >
              <X className="w-3.5 h-3.5" /> Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <p className="text-sm font-semibold text-gray-100">
            {short.title ?? <span className="text-gray-600 font-normal">no title yet</span>}
          </p>
          {short.description && (
            <p className="text-[11px] text-gray-500 font-mono whitespace-pre-line line-clamp-3">
              {short.description}
            </p>
          )}
        </div>
      )}

      {short.error && (
        <p className="text-[11px] text-cyber-red/80 font-mono break-words">{short.error}</p>
      )}

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-white/5">
        {short.youtube_url ? (
          <a
            href={short.youtube_url}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 text-[11px] text-cyber-red hover:underline font-mono"
          >
            <ExternalLink className="w-3 h-3" /> Watch
          </a>
        ) : null}
        {canUpload && (
          <button
            onClick={() =>
              upload.mutate(short.id, {
                onSuccess: () => toast.success('Upload started'),
                onError: () => toast.error('Failed to start upload'),
              })
            }
            disabled={upload.isPending}
            title="Upload now (bypasses the daily cap, not the quota budget)"
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary/10 text-primary border border-primary/30 text-[11px] font-mono uppercase hover:bg-primary/20 disabled:opacity-50 transition-all"
          >
            <UploadCloud className="w-3.5 h-3.5" /> Upload now
          </button>
        )}
        {canEdit && !editing && (
          <button
            onClick={() => setEditing(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 text-gray-400 border border-white/10 text-[11px] font-mono uppercase hover:text-gray-200 hover:border-primary/20 transition-all"
          >
            <Pencil className="w-3.5 h-3.5" /> Edit
          </button>
        )}
        {canEdit && (
          <button
            onClick={() =>
              regenerate.mutate(short.id, {
                onSuccess: () => toast.success('Regenerating metadata'),
                onError: () => toast.error('Failed to regenerate metadata'),
              })
            }
            disabled={regenerate.isPending}
            title="Re-run the AI title/description/tags generation"
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyber-magenta/10 text-cyber-magenta border border-cyber-magenta/30 text-[11px] font-mono uppercase hover:bg-cyber-magenta/20 disabled:opacity-50 transition-all"
          >
            <Sparkles className="w-3.5 h-3.5" /> Regenerate
          </button>
        )}
        {canSkip && (
          <button
            onClick={() =>
              skip.mutate(
                { id: short.id, skip: true },
                {
                  onSuccess: () => toast.success('Short skipped'),
                  onError: () => toast.error('Failed to skip short'),
                },
              )
            }
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 text-gray-500 border border-white/10 text-[11px] font-mono uppercase hover:text-gray-300 transition-all"
          >
            <SkipForward className="w-3.5 h-3.5" /> Skip
          </button>
        )}
        {short.status === 'skipped' && (
          <button
            onClick={() =>
              skip.mutate(
                { id: short.id, skip: false },
                {
                  onSuccess: () => toast.success('Short back in the queue'),
                  onError: () => toast.error('Failed to un-skip short'),
                },
              )
            }
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gold/10 text-gold border border-gold/30 text-[11px] font-mono uppercase hover:bg-gold/20 transition-all"
          >
            <Undo2 className="w-3.5 h-3.5" /> Un-skip
          </button>
        )}
      </div>
    </div>
  )
}

export default function ShortsPage() {
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<(typeof STATUS_FILTERS)[number]>('all')
  const [page, setPage] = useState(1)

  const { data, isLoading } = useShorts({
    page,
    page_size: PAGE_SIZE,
    status: statusFilter === 'all' ? undefined : statusFilter,
  })
  const stats = useShortsStats()
  const scanStatus = useShortsScanStatus()
  const startScan = useStartShortsScan()

  const scanning = scanStatus.data?.running ?? false

  // Live scan progress from WS shorts_scan activity events
  const [scanLive, setScanLive] = useState<string | null>(null)
  useEffect(() => {
    return wsManager.subscribe('activity', (msg: WsMessage) => {
      const item = msg.data as unknown as ActivityItem
      if (item?.event === 'shorts_scan' && item.message) setScanLive(item.message)
    })
  }, [])

  // Toast the summary when a running scan finishes
  const wasScanning = useRef(false)
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      const last = scanStatus.data?.last_scan
      if (last) {
        const fn = last.status === 'ok' ? toast.success : toast.warning
        fn(scanSummaryText(last))
        if (last.errors?.length) last.errors.slice(0, 5).forEach((e) => toast.error(e))
      }
      setScanLive(null)
      qc.invalidateQueries({ queryKey: ['shorts'] })
    }
    wasScanning.current = scanning
  }, [scanning, scanStatus.data, qc])

  const shorts = data?.items ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)
  const s = stats.data
  const lastScan = scanStatus.data?.last_scan

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
            <Smartphone className="w-6 h-6" /> Shorts
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {total} vertical clips
            {s && (
              <span className="text-gray-600">
                {' '}
                · watching {s.watcher_active ? s.watch_path : 'off'}
              </span>
            )}
          </p>
        </div>

        <button
          onClick={() =>
            startScan.mutate(undefined, {
              onSuccess: (res) => {
                if (res.status === 'already_running') toast.info('A shorts scan is already running')
                else toast.success('Shorts backlog scan started')
              },
              onError: () => toast.error('Failed to start shorts scan'),
            })
          }
          disabled={scanning || startScan.isPending}
          title="Scan the watch folder and ingest every clip not yet in the table (already-uploaded clips are detected and skipped)."
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary/10 text-primary border border-primary/30 text-xs font-mono uppercase tracking-wider hover:bg-primary/20 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
        >
          {scanning ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <ScanSearch className="w-4 h-4" />
          )}
          {scanning ? 'Scanning…' : 'Scan folder'}
        </button>
      </div>

      {/* Daily cap indicator */}
      {s && (
        <div
          className={clsx(
            'flex flex-wrap items-center gap-x-6 gap-y-1 rounded-lg px-4 py-2.5 text-xs font-mono border',
            s.cap_reached
              ? 'bg-gold/5 border-gold/20 text-gold'
              : 'bg-surface-light border-primary/10 text-gray-300',
          )}
        >
          <span className="flex items-center gap-1.5">
            <UploadCloud className="w-3.5 h-3.5" />
            {s.uploaded_today}/{s.daily_cap} uploaded today
          </span>
          <span className="text-gray-500">{s.queued} queued</span>
          <span className="text-gray-600">
            quota {s.quota_used}/{s.quota_budget} units
          </span>
        </div>
      )}

      {/* Scan status line */}
      {scanning ? (
        <div className="flex items-center gap-2 bg-surface-light border border-primary/20 rounded-lg px-4 py-2.5 text-xs font-mono text-gray-300">
          <Loader2 className="w-3.5 h-3.5 text-primary animate-spin shrink-0" />
          <span className="truncate">{scanLive ?? 'Shorts scan running…'}</span>
        </div>
      ) : lastScan ? (
        <div className="flex items-center gap-2 bg-surface-light border border-white/5 rounded-lg px-4 py-2.5 text-xs font-mono text-gray-500">
          <RefreshCw className="w-3.5 h-3.5 text-primary/60 shrink-0" />
          <span className="truncate">{scanSummaryText(lastScan)}</span>
          {lastScan.finished_at && (
            <span className="ml-auto shrink-0 text-gray-600">
              {format(new Date(lastScan.finished_at), 'MMM d, HH:mm')}
            </span>
          )}
        </div>
      ) : null}

      {/* Status filters */}
      <div className="flex flex-wrap gap-2">
        <Filter className="w-4 h-4 text-gray-600 mt-1" />
        {STATUS_FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => {
              setStatusFilter(f)
              setPage(1)
            }}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
              statusFilter === f
                ? 'bg-primary/10 text-primary border-primary/30'
                : 'bg-surface-light text-gray-500 border-white/5 hover:border-primary/20 hover:text-gray-300',
            )}
          >
            {f}
            {f !== 'all' && s?.status_counts?.[f] ? ` (${s.status_counts[f]})` : ''}
          </button>
        ))}
      </div>

      {/* Grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="bg-surface-light border border-primary/5 rounded-xl h-52 animate-pulse"
            />
          ))}
        </div>
      ) : shorts.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <p className="font-pixel text-xs">No shorts found</p>
          <p className="text-sm mt-2">
            Drop vertical clips in the watch folder or run a scan to import the backlog.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
          {shorts.map((short) => (
            <ShortCard key={short.id} short={short} />
          ))}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 pt-4">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="p-2 rounded-lg bg-surface-light border border-primary/10 text-gray-400 hover:text-primary disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="text-sm font-mono text-gray-500 px-4">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="p-2 rounded-lg bg-surface-light border border-primary/10 text-gray-400 hover:text-primary disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}
    </div>
  )
}
