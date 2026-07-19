import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import { formatDistanceToNow, format } from 'date-fns'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  Info,
  Loader2,
  Music2,
  Search,
  X,
} from 'lucide-react'
import {
  useActivityInfinite,
  type ActivityFilters,
  type ActivityItem,
} from '@/api/hooks'
import { wsManager, type WsMessage } from '@/api/ws'

const LEVELS = ['all', 'info', 'warn', 'error'] as const

const levelStyle: Record<string, { text: string; icon: typeof Info }> = {
  info: { text: 'text-accent', icon: Info },
  warn: { text: 'text-gold', icon: AlertTriangle },
  error: { text: 'text-cyber-red', icon: AlertCircle },
}

function Row({ item }: { item: ActivityItem }) {
  const [expanded, setExpanded] = useState(false)
  const style = levelStyle[item.level] ?? levelStyle.info
  const Icon = style.icon
  const hasContext = item.context != null && Object.keys(item.context).length > 0
  const ts = new Date(item.ts)
  const validTs = !isNaN(ts.getTime())

  return (
    <div className="border-b border-white/5 hover:bg-white/[0.02] animate-slide-in">
      <div className="flex gap-3 px-5 py-3">
        <Icon className={clsx('w-4 h-4 mt-0.5 shrink-0', style.text)} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="text-[11px] font-mono text-gray-500"
              title={validTs ? format(ts, 'yyyy-MM-dd HH:mm:ss') : item.ts}
            >
              {validTs ? formatDistanceToNow(ts, { addSuffix: true }) : item.ts}
            </span>
            {item.event && (
              <span
                className={clsx(
                  'text-[10px] font-mono px-1.5 py-0.5 rounded border border-current/20 bg-current/5',
                  style.text,
                )}
              >
                {item.event}
              </span>
            )}
            {item.platform && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-dark text-gray-500">
                {item.platform}
              </span>
            )}
            {item.stage && (
              <span className="text-[10px] font-mono text-gray-600">{item.stage}</span>
            )}
          </div>
          <p className="text-sm text-gray-300 break-words leading-snug mt-1">{item.message}</p>
          <div className="flex items-center gap-3 mt-1">
            {item.mix_id && (
              <Link
                to={`/mixes/${item.mix_id}`}
                className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
              >
                <Music2 className="w-3 h-3" /> View mix
              </Link>
            )}
            {item.filename && (
              <span className="text-[10px] text-gray-600 font-mono truncate">{item.filename}</span>
            )}
            {hasContext && (
              <button
                onClick={() => setExpanded((e) => !e)}
                className="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-300 font-mono"
              >
                {expanded ? (
                  <ChevronDown className="w-3 h-3" />
                ) : (
                  <ChevronRight className="w-3 h-3" />
                )}
                context
              </button>
            )}
          </div>
          {expanded && hasContext && (
            <pre className="mt-2 text-[11px] text-gray-400 font-mono bg-dark rounded-lg p-3 overflow-x-auto">
              {JSON.stringify(item.context, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

export default function ActivityPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const mixId = searchParams.get('mix_id') ?? ''
  const urlQ = searchParams.get('q') ?? ''

  const [level, setLevel] = useState('all')
  const [platform, setPlatform] = useState('all')
  const [eventType, setEventType] = useState('all')
  const [search, setSearch] = useState(urlQ)
  const [debouncedSearch, setDebouncedSearch] = useState(urlQ)
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')

  // 300ms debounce on the search box
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300)
    return () => clearTimeout(t)
  }, [search])

  const filters: ActivityFilters = useMemo(
    () => ({
      level: level === 'all' ? undefined : level,
      platform: platform === 'all' ? undefined : platform,
      event: eventType === 'all' ? undefined : eventType,
      mix_id: mixId.trim() || undefined,
      q: debouncedSearch.trim() || undefined,
      since: since ? new Date(since).toISOString() : undefined,
      until: until ? new Date(until).toISOString() : undefined,
    }),
    [level, platform, eventType, mixId, debouncedSearch, since, until],
  )

  const { data, isLoading, isError, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useActivityInfinite(filters)

  const fetchedItems = useMemo(() => data?.pages.flatMap((p) => p.items) ?? [], [data])
  const total = data?.pages[0]?.total ?? 0

  // ----- Live prepend via WS -----
  const [liveItems, setLiveItems] = useState<ActivityItem[]>([])
  const [pendingCount, setPendingCount] = useState(0)
  const pendingBuffer = useRef<ActivityItem[]>([])
  const atTop = useRef(true)
  const topSentinel = useRef<HTMLDivElement>(null)
  const bottomSentinel = useRef<HTMLDivElement>(null)

  // Reset live state when filters change (fresh query replaces everything)
  useEffect(() => {
    setLiveItems([])
    setPendingCount(0)
    pendingBuffer.current = []
  }, [filters])

  useEffect(() => {
    return wsManager.subscribe('activity', (msg: WsMessage) => {
      const item = msg.data as unknown as ActivityItem
      if (item == null || typeof item.id !== 'number') return
      if (atTop.current) {
        setLiveItems((prev) => [item, ...prev.filter((p) => p.id !== item.id)].slice(0, 200))
      } else {
        pendingBuffer.current = [item, ...pendingBuffer.current.filter((p) => p.id !== item.id)]
        setPendingCount(pendingBuffer.current.length)
      }
    })
  }, [])

  const flushPending = () => {
    setLiveItems((prev) => {
      const merged = [...pendingBuffer.current, ...prev]
      const seen = new Set<number>()
      return merged.filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true))).slice(0, 200)
    })
    pendingBuffer.current = []
    setPendingCount(0)
    topSentinel.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }

  // Track whether the user is scrolled to the top (for live-prepend vs pill)
  useEffect(() => {
    const el = topSentinel.current
    if (!el) return
    const obs = new IntersectionObserver(([entry]) => {
      atTop.current = entry.isIntersecting
      if (entry.isIntersecting && pendingBuffer.current.length) flushPending()
    })
    obs.observe(el)
    return () => obs.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Infinite scroll: fetch next page when bottom sentinel appears
  useEffect(() => {
    const el = bottomSentinel.current
    if (!el) return
    const obs = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && hasNextPage && !isFetchingNextPage) fetchNextPage()
    })
    obs.observe(el)
    return () => obs.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage])

  // Merge live + fetched, dedupe by id (live wins)
  const items = useMemo(() => {
    const fetchedIds = new Set(fetchedItems.map((i) => i.id))
    return [...liveItems.filter((i) => !fetchedIds.has(i.id)), ...fetchedItems]
  }, [liveItems, fetchedItems])

  // Select options derived from loaded rows
  const eventOptions = useMemo(() => {
    const s = new Set<string>()
    items.forEach((i) => i.event && s.add(i.event))
    if (eventType !== 'all') s.add(eventType)
    return Array.from(s).sort()
  }, [items, eventType])

  const platformOptions = useMemo(() => {
    const s = new Set<string>(['soundcloud', 'youtube'])
    items.forEach((i) => i.platform && s.add(i.platform))
    if (platform !== 'all') s.add(platform)
    return Array.from(s).sort()
  }, [items, platform])

  const setMixId = (value: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value.trim()) next.set('mix_id', value.trim())
        else next.delete('mix_id')
        return next
      },
      { replace: true },
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
          <Activity className="w-6 h-6" /> Activity
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          Full event log{total ? ` (${total} events)` : ''}
        </p>
      </div>

      {/* Toolbar */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {/* Level chips */}
          {LEVELS.map((l) => (
            <button
              key={l}
              onClick={() => setLevel(l)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
                level === l
                  ? 'bg-primary/10 text-primary border-primary/30'
                  : 'bg-surface text-gray-500 border-white/5 hover:border-primary/20 hover:text-gray-300',
              )}
            >
              {l}
            </button>
          ))}

          {/* Platform select */}
          <select
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
            className="bg-surface border border-white/10 rounded-lg px-2 py-1.5 text-[11px] font-mono text-gray-300 focus:outline-none focus:border-primary/40"
          >
            <option value="all">all platforms</option>
            {platformOptions.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>

          {/* Event type select */}
          <select
            value={eventType}
            onChange={(e) => setEventType(e.target.value)}
            className="bg-surface border border-white/10 rounded-lg px-2 py-1.5 text-[11px] font-mono text-gray-300 focus:outline-none focus:border-primary/40"
          >
            <option value="all">all events</option>
            {eventOptions.map((ev) => (
              <option key={ev} value={ev}>
                {ev}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Search */}
          <div className="relative flex-1 min-w-[200px]">
            <Search className="w-3.5 h-3.5 text-gray-600 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="search messages…"
              className="w-full bg-surface border border-white/10 rounded-lg pl-8 pr-2 py-1.5 text-xs font-mono text-gray-300 placeholder:text-gray-600 focus:outline-none focus:border-primary/40"
            />
          </div>

          {/* Mix filter */}
          <div className="relative min-w-[180px]">
            <Music2 className="w-3.5 h-3.5 text-gray-600 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={mixId}
              onChange={(e) => setMixId(e.target.value)}
              placeholder="filter by mix id…"
              className="w-full bg-surface border border-white/10 rounded-lg pl-8 pr-7 py-1.5 text-xs font-mono text-gray-300 placeholder:text-gray-600 focus:outline-none focus:border-primary/40"
            />
            {mixId && (
              <button
                onClick={() => setMixId('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                title="Clear mix filter"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>

          {/* Date range */}
          <input
            type="datetime-local"
            value={since}
            onChange={(e) => setSince(e.target.value)}
            title="From"
            className="bg-surface border border-white/10 rounded-lg px-2 py-1.5 text-[11px] font-mono text-gray-400 focus:outline-none focus:border-primary/40 [color-scheme:dark]"
          />
          <span className="text-[10px] text-gray-600 font-mono">to</span>
          <input
            type="datetime-local"
            value={until}
            onChange={(e) => setUntil(e.target.value)}
            title="Until"
            className="bg-surface border border-white/10 rounded-lg px-2 py-1.5 text-[11px] font-mono text-gray-400 focus:outline-none focus:border-primary/40 [color-scheme:dark]"
          />
        </div>
      </div>

      {/* "N new" pill */}
      {pendingCount > 0 && (
        <div className="sticky top-2 z-10 flex justify-center">
          <button
            onClick={flushPending}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded-full bg-primary/20 text-primary border border-primary/40 text-xs font-mono shadow-neon hover:bg-primary/30 transition-all"
          >
            <ArrowUp className="w-3.5 h-3.5" /> {pendingCount} new
          </button>
        </div>
      )}

      {/* List */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        <div ref={topSentinel} />
        {isLoading && (
          <div className="flex items-center gap-2 p-6 text-sm text-gray-500 font-mono">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading activity…
          </div>
        )}
        {isError && (
          <p className="p-6 text-sm text-cyber-red font-mono">Failed to load activity.</p>
        )}
        {!isLoading && !isError && items.length === 0 && (
          <div className="px-6 py-16 text-center">
            <Activity className="w-10 h-10 text-gray-700 mx-auto mb-3" />
            <p className="text-sm text-gray-600">No activity matches your filters</p>
          </div>
        )}
        {items.map((item) => (
          <Row key={item.id} item={item} />
        ))}
        <div ref={bottomSentinel} />
        {isFetchingNextPage && (
          <div className="flex items-center gap-2 p-4 text-xs text-gray-500 font-mono">
            <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading more…
          </div>
        )}
        {!hasNextPage && items.length > 0 && (
          <p className="p-4 text-center text-[10px] text-gray-600 font-mono">End of log</p>
        )}
      </div>
    </div>
  )
}
