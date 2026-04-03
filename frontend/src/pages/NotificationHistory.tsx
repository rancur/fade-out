import { useState } from 'react'
import {
  Bell,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Info,
  Filter,
  Send,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { format } from 'date-fns'
import { useNotifications, useTestNotification } from '@/api/hooks'
import { Link } from 'react-router-dom'

const typeIcons: Record<string, { icon: React.ElementType; color: string }> = {
  success: { icon: CheckCircle2, color: 'text-primary' },
  error: { icon: XCircle, color: 'text-cyber-red' },
  warning: { icon: AlertTriangle, color: 'text-gold' },
  info: { icon: Info, color: 'text-accent' },
}

const typeFilters = ['all', 'success', 'error', 'warning', 'info']
const channelFilters = ['all', 'email', 'discord', 'webhook']

export default function NotificationHistory() {
  const [typeFilter, setTypeFilter] = useState('all')
  const [channelFilter, setChannelFilter] = useState('all')
  const { data, isLoading } = useNotifications({
    type: typeFilter === 'all' ? undefined : typeFilter,
    channel: channelFilter === 'all' ? undefined : channelFilter,
  })
  const testNotif = useTestNotification()

  const handleTest = (channel: string) => {
    testNotif.mutate(channel, {
      onSuccess: () => toast.success(`Test ${channel} notification sent`),
      onError: () => toast.error(`Failed to send test ${channel} notification`),
    })
  }

  const notifications = data?.items ?? []

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
            <Bell className="w-6 h-6" /> Notifications
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Pipeline events and alerts
            {data ? ` (${data.total} total)` : ''}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => handleTest('email')}
            disabled={testNotif.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-400 hover:text-primary hover:border-primary/30 disabled:opacity-50 transition-all"
          >
            <Send className="w-3.5 h-3.5" /> Test Email
          </button>
          <button
            onClick={() => handleTest('discord')}
            disabled={testNotif.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-surface-light border border-primary/10 rounded-lg text-sm text-gray-400 hover:text-accent hover:border-accent/30 disabled:opacity-50 transition-all"
          >
            <Send className="w-3.5 h-3.5" /> Test Discord
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          <Filter className="w-4 h-4 text-gray-600 mt-1.5" />
          <span className="text-[10px] text-gray-600 font-mono uppercase mt-2 mr-1">Type:</span>
          {typeFilters.map((t) => (
            <button
              key={t}
              onClick={() => setTypeFilter(t)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
                typeFilter === t
                  ? 'bg-primary/10 text-primary border-primary/30'
                  : 'bg-surface-light text-gray-500 border-white/5 hover:border-primary/20 hover:text-gray-300',
              )}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-2 pl-6">
          <span className="text-[10px] text-gray-600 font-mono uppercase mt-2 mr-1">Channel:</span>
          {channelFilters.map((c) => (
            <button
              key={c}
              onClick={() => setChannelFilter(c)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
                channelFilter === c
                  ? 'bg-accent/10 text-accent border-accent/30'
                  : 'bg-surface-light text-gray-500 border-white/5 hover:border-accent/20 hover:text-gray-300',
              )}
            >
              {c}
            </button>
          ))}
        </div>
      </div>

      {/* Notification List */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        {isLoading ? (
          <div className="space-y-0">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-20 border-b border-white/5 animate-pulse" />
            ))}
          </div>
        ) : notifications.length === 0 ? (
          <div className="px-6 py-16 text-center">
            <Bell className="w-10 h-10 text-gray-700 mx-auto mb-3" />
            <p className="text-sm text-gray-600">No notifications yet</p>
            <p className="text-[11px] text-gray-700 mt-1">Events will appear here as mixes are processed</p>
          </div>
        ) : (
          <div className="divide-y divide-white/5">
            {notifications.map((notif) => {
              const { icon: Icon, color } = typeIcons[notif.type] ?? typeIcons.info
              return (
                <div
                  key={notif.id}
                  className={clsx(
                    'flex items-start gap-4 px-6 py-4 transition-colors',
                    notif.sent ? 'opacity-60' : 'hover:bg-white/[0.02]',
                  )}
                >
                  {/* Icon */}
                  <div className={clsx('mt-0.5 shrink-0', color)}>
                    <Icon className="w-5 h-5" />
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-sm text-gray-200 font-medium">{notif.message}</p>
                        <div className="flex items-center gap-2 mt-1">
                          <span className="text-[10px] text-gray-600 font-mono px-1.5 py-0.5 bg-dark rounded">
                            {notif.channel}
                          </span>
                          <span className="text-[10px] text-gray-600 font-mono px-1.5 py-0.5 bg-dark rounded">
                            {notif.type}
                          </span>
                          {notif.sent && (
                            <span className="flex items-center gap-1 text-[10px] text-primary font-mono">
                              <CheckCircle2 className="w-3 h-3" /> sent
                            </span>
                          )}
                        </div>
                      </div>
                      <span className="text-[10px] text-gray-600 font-mono shrink-0">
                        {format(new Date(notif.created_at), 'MMM d, HH:mm')}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 mt-2">
                      {notif.mix_id && (
                        <Link
                          to={`/mixes/${notif.mix_id}`}
                          className="text-[11px] text-primary hover:underline"
                        >
                          View mix
                        </Link>
                      )}
                      {notif.sent_at && (
                        <span className="text-[10px] text-gray-600 font-mono">
                          Sent {format(new Date(notif.sent_at), 'MMM d, HH:mm')}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
