import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, ExternalLink, Loader2, XCircle } from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import client from '@/api/client'

interface OAuthStatus {
  connected: boolean
  username: string | null
  channel_name: string | null
}

function useOAuthStatus(provider: 'soundcloud' | 'youtube') {
  return useQuery<OAuthStatus>({
    queryKey: ['oauth-status', provider],
    queryFn: () => client.get<OAuthStatus>(`/auth/${provider}/status`).then((r) => r.data),
    refetchInterval: 30_000,
  })
}

function ProviderCard({
  provider,
  label,
  color,
  status,
  isLoading,
}: {
  provider: 'soundcloud' | 'youtube'
  label: string
  color: string
  status: OAuthStatus | undefined
  isLoading: boolean
}) {
  const connected = status?.connected ?? false
  const displayName = status?.username || status?.channel_name || null

  const handleConnect = () => {
    window.location.href = `/api/auth/${provider}`
  }

  return (
    <div
      className={clsx(
        'flex items-center justify-between gap-4 p-4 rounded-lg border transition-colors',
        connected ? 'border-primary/20 bg-primary/5' : 'border-gray-700/50 bg-dark',
      )}
    >
      <div className="flex items-center gap-3 min-w-0">
        <div
          className={clsx(
            'w-10 h-10 rounded-lg flex items-center justify-center text-sm font-bold shrink-0',
            connected ? 'bg-primary/20 text-primary' : `bg-${color}/10 text-${color}`,
          )}
          style={
            !connected
              ? { backgroundColor: `${color}15`, color }
              : undefined
          }
        >
          {provider === 'soundcloud' ? 'SC' : 'YT'}
        </div>
        <div className="min-w-0">
          <p className="text-sm text-gray-300 font-medium">{label}</p>
          {isLoading ? (
            <span className="flex items-center gap-1.5 text-[11px] text-gray-600">
              <Loader2 className="w-3 h-3 animate-spin" /> Checking...
            </span>
          ) : connected ? (
            <span className="flex items-center gap-1.5 text-[11px] text-primary truncate">
              <CheckCircle2 className="w-3 h-3 shrink-0" />
              Connected{displayName ? ` as @${displayName}` : ''}
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-[11px] text-gray-600">
              <XCircle className="w-3 h-3 shrink-0" /> Not connected
            </span>
          )}
        </div>
      </div>

      {!connected && !isLoading && (
        <button
          onClick={handleConnect}
          className={clsx(
            'flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold transition-all shrink-0',
            provider === 'soundcloud'
              ? 'bg-[#ff5500] hover:bg-[#ff5500]/80 text-white'
              : 'bg-[#ff0000] hover:bg-[#ff0000]/80 text-white',
          )}
        >
          Connect
          <ExternalLink className="w-3 h-3" />
        </button>
      )}

      {connected && (
        <span className="text-[11px] text-primary/60 font-mono shrink-0">Authorized</span>
      )}
    </div>
  )
}

export default function OAuthConnect() {
  const soundcloud = useOAuthStatus('soundcloud')
  const youtube = useOAuthStatus('youtube')

  // Show toast on OAuth redirect with success/error params
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const authProvider = params.get('auth')
    const success = params.get('success')
    const error = params.get('error')

    if (authProvider && success) {
      toast.success(`${authProvider === 'soundcloud' ? 'SoundCloud' : 'YouTube'} connected successfully`)
      // Clean up URL params
      window.history.replaceState({}, '', window.location.pathname)
      // Refetch status
      if (authProvider === 'soundcloud') soundcloud.refetch()
      if (authProvider === 'youtube') youtube.refetch()
    } else if (authProvider && error) {
      toast.error(`Failed to connect ${authProvider === 'soundcloud' ? 'SoundCloud' : 'YouTube'}: ${error}`)
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-4">
      <div>
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <ExternalLink className="w-4 h-4" /> Platform Connections
        </h2>
        <p className="text-[11px] text-gray-600 mt-1">
          Connect your accounts for 1-click uploads
        </p>
      </div>

      <div className="space-y-3">
        <ProviderCard
          provider="soundcloud"
          label="SoundCloud"
          color="#ff5500"
          status={soundcloud.data}
          isLoading={soundcloud.isLoading}
        />
        <ProviderCard
          provider="youtube"
          label="YouTube"
          color="#ff0000"
          status={youtube.data}
          isLoading={youtube.isLoading}
        />
      </div>
    </div>
  )
}
