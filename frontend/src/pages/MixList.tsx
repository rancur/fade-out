import { useState } from 'react'
import { Search, Filter, SortDesc, ChevronLeft, ChevronRight } from 'lucide-react'
import { useMixes } from '@/api/hooks'
import MixCard from '@/components/MixCard'
import clsx from 'clsx'

const statusFilters = ['all', 'pending', 'processing', 'generating', 'reviewing', 'approved', 'uploaded', 'failed']
const PAGE_SIZE = 12

export default function MixList() {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [page, setPage] = useState(1)
  const [sort, setSort] = useState<'newest' | 'oldest' | 'title'>('newest')

  const { data, isLoading } = useMixes({
    status: status === 'all' ? undefined : status,
    page,
    page_size: PAGE_SIZE,
  })

  const mixes = data?.items ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  // Client-side search filter (API doesn't support search param)
  const filtered = search
    ? mixes.filter((m) => m.title.toLowerCase().includes(search.toLowerCase()))
    : mixes

  const sorted = [...filtered].sort((a, b) => {
    if (sort === 'oldest') return new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
    if (sort === 'title') return a.title.localeCompare(b.title)
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  })

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text">Mixes</h1>
        <p className="text-sm text-gray-500 mt-1">{total} mixes in library</p>
      </div>

      {/* Toolbar */}
      <div className="flex flex-col sm:flex-row gap-3">
        {/* Search */}
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
          <input
            type="text"
            placeholder="Search mixes..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setPage(1)
            }}
            className="w-full pl-10 pr-4 py-2.5 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-200 placeholder-gray-600 focus:border-primary/40 focus:outline-none transition-colors font-mono"
          />
        </div>

        {/* Sort */}
        <div className="relative">
          <SortDesc className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
            className="pl-10 pr-8 py-2.5 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
          >
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
            <option value="title">Title</option>
          </select>
        </div>
      </div>

      {/* Status Filters */}
      <div className="flex flex-wrap gap-2">
        <Filter className="w-4 h-4 text-gray-600 mt-1" />
        {statusFilters.map((s) => (
          <button
            key={s}
            onClick={() => {
              setStatus(s)
              setPage(1)
            }}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
              status === s
                ? 'bg-primary/10 text-primary border-primary/30'
                : 'bg-surface-light text-gray-500 border-white/5 hover:border-primary/20 hover:text-gray-300',
            )}
          >
            {s}
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
      ) : sorted.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <p className="font-pixel text-xs">No mixes found</p>
          <p className="text-sm mt-2">Try adjusting your filters or drop an audio file to get started.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {sorted.map((mix) => (
            <MixCard key={mix.id} mix={mix} />
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
