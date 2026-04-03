import clsx from 'clsx'

const statusConfig: Record<string, { label: string; color: string; glow: string }> = {
  pending: { label: 'Pending', color: 'text-gray-400 border-gray-500', glow: '' },
  processing: { label: 'Processing', color: 'text-cyber-cyan border-cyber-cyan', glow: 'shadow-neon-cyan animate-pulse-slow' },
  generating: { label: 'Generating', color: 'text-cyber-magenta border-cyber-magenta', glow: 'shadow-neon-magenta animate-pulse-slow' },
  reviewing: { label: 'Reviewing', color: 'text-gold border-gold', glow: '' },
  approved: { label: 'Approved', color: 'text-primary border-primary', glow: 'shadow-neon' },
  uploaded: { label: 'Uploaded', color: 'text-cyber-lime border-cyber-lime', glow: '' },
  failed: { label: 'Failed', color: 'text-cyber-red border-cyber-red', glow: 'shadow-neon-red' },
}

interface StatusBadgeProps {
  status: string
  size?: 'sm' | 'md'
}

export default function StatusBadge({ status, size = 'sm' }: StatusBadgeProps) {
  const config = statusConfig[status] ?? statusConfig.pending

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 border rounded-full font-mono uppercase tracking-wider',
        config.color,
        config.glow,
        size === 'sm' ? 'text-[10px] px-2.5 py-0.5' : 'text-xs px-3 py-1',
      )}
    >
      <span
        className={clsx(
          'rounded-full',
          size === 'sm' ? 'w-1.5 h-1.5' : 'w-2 h-2',
          status === 'processing' || status === 'generating' ? 'animate-pulse bg-current' : 'bg-current',
        )}
      />
      {config.label}
    </span>
  )
}
