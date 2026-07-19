import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import { format } from 'date-fns'
import { toast } from 'sonner'
import {
  Calendar,
  ChevronLeft,
  ChevronRight,
  Clock,
  ExternalLink,
  FileEdit,
  Filter,
  Library,
  ListChecks,
  ListMusic,
  Loader2,
  Lock,
  Music,
  RefreshCw,
  Search,
  SortDesc,
  Sparkles,
  Unlock,
} from 'lucide-react'
import {
  useCatalogBackfillStatus,
  useCatalogImprove,
  useCatalogMixes,
  useCatalogSyncStatus,
  useLockTitle,
  useProposals,
  useStartCatalogBackfill,
  useStartCatalogSync,
  type ActivityItem,
  type CatalogBackfillSummary,
  type CatalogMix,
  type CatalogSort,
  type CatalogSyncSummary,
} from '@/api/hooks'
import { wsManager, type WsMessage } from '@/api/ws'
import { useQueryClient } from '@tanstack/react-query'

const PAGE_SIZE = 12
const PLATFORM_FILTERS = ['all', 'yt-only', 'sc-only', 'both'] as const
const SOURCE_FILTERS = ['all', 'pipeline', 'imported'] as const
const SORT_OPTIONS: { value: CatalogSort; label: string }[] = [
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' },
  { value: 'title', label: 'Title' },
  { value: 'duration', label: 'Duration' },
]

function formatDuration(seconds: number): string {
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

function summaryText(s: CatalogSyncSummary): string {
  return (
    `Sync ${s.status}: ${s.youtube_items ?? 0} YT + ${s.soundcloud_items ?? 0} SC items, ` +
    `${s.pairs_created ?? 0} pairs + ${s.singles_created ?? 0} singles created, ` +
    `${s.existing_updated ?? 0} updated`
  )
}

function backfillSummaryText(s: CatalogBackfillSummary): string {
  return (
    `Backfill ${s.status}: ${s.processed ?? 0}/${s.matched ?? 0} matched mixes analyzed, ` +
    `${s.tracks_found ?? 0} tracks found, ${s.proposals_created ?? 0} proposals approved, ` +
    `${s.unmatched_mixes?.length ?? 0} mixes without local audio`
  )
}

function LockToggle({ mix }: { mix: CatalogMix }) {
  const lockTitle = useLockTitle(mix.id)
  const Icon = mix.title_locked ? Lock : Unlock
  return (
    <button
      onClick={(e) => {
        e.preventDefault()
        e.stopPropagation()
        lockTitle.mutate(!mix.title_locked, {
          onSuccess: (res) =>
            toast.success(res.title_locked ? 'Title locked (keeper)' : 'Title unlocked'),
          onError: () => toast.error('Failed to toggle title lock'),
        })
      }}
      disabled={lockTitle.isPending}
      title={mix.title_locked ? 'Title locked — AI will never propose changes. Click to unlock.' : 'Title unlocked — click to lock as a keeper.'}
      className={clsx(
        'p-1 rounded shrink-0 transition-colors',
        mix.title_locked ? 'text-gold hover:text-gold/70' : 'text-gray-600 hover:text-gray-400',
      )}
    >
      <Icon className="w-3.5 h-3.5" />
    </button>
  )
}

function CatalogMixCard({ mix }: { mix: CatalogMix }) {
  const navigate = useNavigate()
  const [imgError, setImgError] = useState(false)
  const image = mix.thumbnail_url || mix.artwork_url
  const published = mix.youtube_published_at || mix.soundcloud_published_at

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={() => navigate(`/catalog/${mix.id}`)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') navigate(`/catalog/${mix.id}`)
      }}
      className="block bg-surface-light border border-primary/10 rounded-xl overflow-hidden card-hover hover:border-primary/30 group cursor-pointer"
    >
      {/* Thumbnail */}
      <div className="relative aspect-video bg-dark overflow-hidden">
        {image && !imgError ? (
          <img
            src={image}
            alt={mix.title}
            onError={() => setImgError(true)}
            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-primary/10 to-cyber-cyan/5">
            <Music className="w-12 h-12 text-primary/30" />
          </div>
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-surface-light via-transparent to-transparent" />
        {/* Source chip */}
        <div className="absolute top-3 left-3">
          <span
            className={clsx(
              'text-[10px] px-2 py-0.5 rounded-full font-mono uppercase tracking-wider border',
              mix.source === 'imported'
                ? 'text-accent border-accent/40 bg-accent/10'
                : 'text-primary border-primary/40 bg-primary/10',
            )}
          >
            {mix.source}
          </span>
        </div>
        {/* Open proposals */}
        {mix.open_proposals > 0 && (
          <div className="absolute top-3 right-3">
            <Link
              to={`/catalog/review?mix_id=${mix.id}`}
              onClick={(e) => e.stopPropagation()}
              className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-mono bg-cyber-magenta/15 text-cyber-magenta border border-cyber-magenta/40 hover:bg-cyber-magenta/25 transition-colors"
              title="Open proposals — review them"
            >
              <FileEdit className="w-3 h-3" /> {mix.open_proposals}
            </Link>
          </div>
        )}
      </div>

      {/* Content */}
      <div className="p-4 space-y-3">
        <div className="flex items-start gap-1.5">
          <h3 className="flex-1 text-sm font-semibold text-gray-100 truncate group-hover:text-primary transition-colors">
            {mix.title}
          </h3>
          <LockToggle mix={mix} />
        </div>

        {/* Meta */}
        <div className="flex items-center justify-between text-[11px] text-gray-500 font-mono">
          {mix.duration_seconds != null ? (
            <span className="flex items-center gap-1">
              <Clock className="w-3 h-3" /> {formatDuration(mix.duration_seconds)}
            </span>
          ) : (
            <span />
          )}
          {published && (
            <span className="flex items-center gap-1">
              <Calendar className="w-3 h-3" /> {format(new Date(published), 'MMM d, yyyy')}
            </span>
          )}
        </div>

        {/* Platform badges */}
        <div className="flex gap-2 pt-1 border-t border-white/5">
          {mix.youtube_url ? (
            <a
              href={mix.youtube_url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="flex items-center gap-1 text-[10px] text-cyber-red hover:underline"
            >
              <ExternalLink className="w-3 h-3" /> YT
            </a>
          ) : mix.platforms.includes('youtube') ? (
            <span className="flex items-center gap-1 text-[10px] text-cyber-red/60">YT</span>
          ) : null}
          {mix.soundcloud_url ? (
            <a
              href={mix.soundcloud_url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="flex items-center gap-1 text-[10px] text-secondary hover:underline"
            >
              <ExternalLink className="w-3 h-3" /> SC
            </a>
          ) : mix.platforms.includes('soundcloud') ? (
            <span className="flex items-center gap-1 text-[10px] text-secondary/60">SC</span>
          ) : null}
        </div>
      </div>
    </div>
  )
}

export default function CatalogPage() {
  const qc = useQueryClient()
  const [platform, setPlatform] = useState<(typeof PLATFORM_FILTERS)[number]>('all')
  const [source, setSource] = useState<(typeof SOURCE_FILTERS)[number]>('all')
  const [sort, setSort] = useState<CatalogSort>('newest')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [page, setPage] = useState(1)

  // 300ms search debounce
  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(search)
      setPage(1)
    }, 300)
    return () => clearTimeout(t)
  }, [search])

  const { data, isLoading } = useCatalogMixes({
    page,
    page_size: PAGE_SIZE,
    platform: platform === 'all' ? undefined : platform,
    source: source === 'all' ? undefined : source,
    q: debouncedSearch.trim() || undefined,
    sort,
  })

  const syncStatus = useCatalogSyncStatus()
  const startSync = useStartCatalogSync()
  const improve = useCatalogImprove()
  const backfillStatus = useCatalogBackfillStatus()
  const startBackfill = useStartCatalogBackfill()
  const draftProposals = useProposals({ status: 'draft', limit: 1 })

  const running = syncStatus.data?.running ?? false
  const backfillRunning = backfillStatus.data?.running ?? false

  // Live progress lines from WS catalog_sync / catalog_backfill activity events
  const [liveStatus, setLiveStatus] = useState<string | null>(null)
  const [backfillLive, setBackfillLive] = useState<string | null>(null)
  useEffect(() => {
    return wsManager.subscribe('activity', (msg: WsMessage) => {
      const item = msg.data as unknown as ActivityItem
      if (item?.event === 'catalog_sync' && item.message) setLiveStatus(item.message)
      if (item?.event === 'catalog_backfill' && item.message) setBackfillLive(item.message)
    })
  }, [])

  // Toast the summary when a running sync finishes
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !running) {
      const last = syncStatus.data?.last_sync
      if (last) {
        const fn = last.status === 'ok' ? toast.success : toast.warning
        fn(summaryText(last))
        if (last.errors?.length) last.errors.forEach((e) => toast.error(e))
      }
      setLiveStatus(null)
      qc.invalidateQueries({ queryKey: ['catalog'] })
    }
    wasRunning.current = running
  }, [running, syncStatus.data, qc])

  // Toast the summary when a running backfill finishes
  const backfillWasRunning = useRef(false)
  useEffect(() => {
    if (backfillWasRunning.current && !backfillRunning) {
      const last = backfillStatus.data?.last_backfill
      if (last) {
        const fn = last.status === 'ok' ? toast.success : toast.warning
        fn(backfillSummaryText(last))
        if (last.errors?.length) last.errors.forEach((e) => toast.error(e))
      }
      setBackfillLive(null)
      qc.invalidateQueries({ queryKey: ['catalog'] })
    }
    backfillWasRunning.current = backfillRunning
  }, [backfillRunning, backfillStatus.data, qc])

  const mixes = data?.items ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)
  const openCount = draftProposals.data?.total ?? 0
  const lastSync = syncStatus.data?.last_sync
  const lastBackfill = backfillStatus.data?.last_backfill

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
            <Library className="w-6 h-6" /> Catalog
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {total} mixes across YouTube + SoundCloud
            {lastSync?.finished_at && !running && (
              <span className="text-gray-600">
                {' '}
                · last sync {format(new Date(lastSync.finished_at), 'MMM d, HH:mm')} (
                {lastSync.status})
              </span>
            )}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() =>
              startSync.mutate(undefined, {
                onSuccess: (res) => {
                  if (res.status === 'already_running') toast.info('A catalog sync is already running')
                  else toast.success('Catalog sync started')
                },
                onError: () => toast.error('Failed to start catalog sync'),
              })
            }
            disabled={running || startSync.isPending}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary/10 text-primary border border-primary/30 text-xs font-mono uppercase tracking-wider hover:bg-primary/20 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          >
            {running ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
            {running ? 'Syncing…' : 'Sync'}
          </button>

          <button
            onClick={() =>
              startBackfill.mutate(undefined, {
                onSuccess: (res) => {
                  if (res.status === 'already_running')
                    toast.info('A tracklist backfill is already running')
                  else toast.success('Tracklist backfill started')
                },
                onError: () => toast.error('Failed to start tracklist backfill'),
              })
            }
            disabled={backfillRunning || startBackfill.isPending}
            title="Match local audio files to imported mixes without tracklists, analyze them, and draft refreshed descriptions with the detected tracklist."
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyber-cyan/10 text-cyber-cyan border border-cyber-cyan/30 text-xs font-mono uppercase tracking-wider hover:bg-cyber-cyan/20 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          >
            {backfillRunning ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <ListMusic className="w-4 h-4" />
            )}
            {backfillRunning ? 'Backfilling…' : 'Backfill tracklists'}
          </button>

          <button
            onClick={() =>
              improve.mutate('all_generic', {
                onSuccess: (res) => {
                  if (res.status === 'already_running') toast.info('AI improve is already running')
                  else
                    toast.success(
                      'AI improve started — drafting title/description proposals for generic titles',
                    )
                },
                onError: () => toast.error('Failed to start AI improve'),
              })
            }
            disabled={improve.isPending}
            title="Drafts click-optimized titles + descriptions for mixes with generic titles. Locked titles are never touched. Review results in the queue."
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyber-magenta/10 text-cyber-magenta border border-cyber-magenta/30 text-xs font-mono uppercase tracking-wider hover:bg-cyber-magenta/20 disabled:opacity-50 transition-all"
          >
            <Sparkles className="w-4 h-4" /> AI Improve
          </button>

          <Link
            to="/catalog/review"
            className="relative flex items-center gap-2 px-4 py-2 rounded-lg bg-surface-light text-gray-300 border border-white/10 text-xs font-mono uppercase tracking-wider hover:border-primary/30 hover:text-primary transition-all"
          >
            <ListChecks className="w-4 h-4" /> Review Queue
            {openCount > 0 && (
              <span className="absolute -top-2 -right-2 min-w-[18px] h-[18px] px-1 rounded-full bg-cyber-magenta text-dark text-[10px] font-mono font-bold flex items-center justify-center">
                {openCount}
              </span>
            )}
          </Link>
        </div>
      </div>

      {/* Live sync status line */}
      {running && (
        <div className="flex items-center gap-2 bg-surface-light border border-primary/20 rounded-lg px-4 py-2.5 text-xs font-mono text-gray-300">
          <Loader2 className="w-3.5 h-3.5 text-primary animate-spin shrink-0" />
          <span className="truncate">{liveStatus ?? 'Catalog sync running…'}</span>
        </div>
      )}

      {/* Tracklist backfill status line */}
      {backfillRunning ? (
        <div className="flex items-center gap-2 bg-surface-light border border-cyber-cyan/20 rounded-lg px-4 py-2.5 text-xs font-mono text-gray-300">
          <Loader2 className="w-3.5 h-3.5 text-cyber-cyan animate-spin shrink-0" />
          <span className="truncate">{backfillLive ?? 'Tracklist backfill running…'}</span>
        </div>
      ) : lastBackfill ? (
        <div className="flex items-center gap-2 bg-surface-light border border-white/5 rounded-lg px-4 py-2.5 text-xs font-mono text-gray-500">
          <ListMusic className="w-3.5 h-3.5 text-cyber-cyan/60 shrink-0" />
          <span className="truncate">{backfillSummaryText(lastBackfill)}</span>
          {lastBackfill.finished_at && (
            <span className="ml-auto shrink-0 text-gray-600">
              {format(new Date(lastBackfill.finished_at), 'MMM d, HH:mm')}
            </span>
          )}
        </div>
      ) : null}

      {/* Toolbar */}
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
          <input
            type="text"
            placeholder="Search catalog..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2.5 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-200 placeholder-gray-600 focus:border-primary/40 focus:outline-none transition-colors font-mono"
          />
        </div>
        <select
          value={source}
          onChange={(e) => {
            setSource(e.target.value as typeof source)
            setPage(1)
          }}
          className="px-3 py-2.5 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
        >
          {SOURCE_FILTERS.map((s) => (
            <option key={s} value={s}>
              {s === 'all' ? 'all sources' : s}
            </option>
          ))}
        </select>
        <div className="relative">
          <SortDesc className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
          <select
            aria-label="Sort catalog"
            value={sort}
            onChange={(e) => {
              setSort(e.target.value as CatalogSort)
              setPage(1)
            }}
            className="pl-10 pr-8 py-2.5 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Platform filters */}
      <div className="flex flex-wrap gap-2">
        <Filter className="w-4 h-4 text-gray-600 mt-1" />
        {PLATFORM_FILTERS.map((p) => (
          <button
            key={p}
            onClick={() => {
              setPlatform(p)
              setPage(1)
            }}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
              platform === p
                ? 'bg-primary/10 text-primary border-primary/30'
                : 'bg-surface-light text-gray-500 border-white/5 hover:border-primary/20 hover:text-gray-300',
            )}
          >
            {p}
          </button>
        ))}
      </div>

      {/* Grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="bg-surface-light border border-primary/5 rounded-xl h-64 animate-pulse" />
          ))}
        </div>
      ) : mixes.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <p className="font-pixel text-xs">No catalog mixes found</p>
          <p className="text-sm mt-2">Run a sync to import your YouTube + SoundCloud back-catalog.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {mixes.map((mix) => (
            <CatalogMixCard key={mix.id} mix={mix} />
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
