import { Link } from 'react-router-dom'
import { Clock, Music, ExternalLink } from 'lucide-react'
import clsx from 'clsx'
import { format } from 'date-fns'
import type { Mix } from '@/api/hooks'
import StatusBadge from './StatusBadge'

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

export default function MixCard({ mix }: { mix: Mix }) {
  return (
    <Link
      to={`/mixes/${mix.id}`}
      className={clsx(
        'block bg-surface-light border border-primary/10 rounded-xl overflow-hidden card-hover',
        'hover:border-primary/30 group',
      )}
    >
      {/* Cover art */}
      <div className="relative aspect-video bg-dark overflow-hidden">
        {mix.cover_art_url ? (
          <img
            src={mix.cover_art_url}
            alt={mix.title}
            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-primary/10 to-cyber-cyan/5">
            <Music className="w-12 h-12 text-primary/30" />
          </div>
        )}
        {/* Gradient overlay */}
        <div className="absolute inset-0 bg-gradient-to-t from-surface-light via-transparent to-transparent" />
        {/* Status */}
        <div className="absolute top-3 right-3">
          <StatusBadge status={mix.status} />
        </div>
      </div>

      {/* Content */}
      <div className="p-4 space-y-3">
        <h3 className="text-sm font-semibold text-gray-100 truncate group-hover:text-primary transition-colors">
          {mix.title}
        </h3>

        {/* Genres */}
        {mix.genres.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {mix.genres.slice(0, 3).map((g) => (
              <span
                key={g}
                className="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary/80 font-mono"
              >
                {g}
              </span>
            ))}
            {mix.genres.length > 3 && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-white/5 text-gray-500 font-mono">
                +{mix.genres.length - 3}
              </span>
            )}
          </div>
        )}

        {/* Meta */}
        <div className="flex items-center justify-between text-[11px] text-gray-500 font-mono">
          <span className="flex items-center gap-1">
            <Clock className="w-3 h-3" />
            {formatDuration(mix.duration_seconds)}
          </span>
          <span>{format(new Date(mix.created_at), 'MMM d, yyyy')}</span>
        </div>

        {/* Links */}
        {(mix.soundcloud_url || mix.youtube_url) && (
          <div className="flex gap-2 pt-1 border-t border-white/5">
            {mix.soundcloud_url && (
              <span className="flex items-center gap-1 text-[10px] text-secondary">
                <ExternalLink className="w-3 h-3" /> SC
              </span>
            )}
            {mix.youtube_url && (
              <span className="flex items-center gap-1 text-[10px] text-cyber-red">
                <ExternalLink className="w-3 h-3" /> YT
              </span>
            )}
          </div>
        )}
      </div>
    </Link>
  )
}
