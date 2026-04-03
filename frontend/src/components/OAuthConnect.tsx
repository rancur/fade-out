import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Cloud, Youtube, CheckCircle2, XCircle, Key, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import client from '@/api/client'

interface ConnectionStatus {
  connected: boolean
  username?: string | null
  channel_name?: string | null
  error?: string | null
}

function ConnectionCard({
  provider,
  label,
  icon: Icon,
  iconColor,
  bgColor,
  statusUrl,
  tokenUrl,
  authUrl,
  tokenPlaceholder,
  instructions,
}: {
  provider: string
  label: string
  icon: React.ElementType
  iconColor: string
  bgColor: string
  statusUrl: string
  tokenUrl: string
  authUrl?: string
  tokenPlaceholder: string
  instructions: string[]
}) {
  const qc = useQueryClient()
  const [showTokenInput, setShowTokenInput] = useState(false)
  const [token, setToken] = useState('')

  const { data: status, isLoading } = useQuery({
    queryKey: ['auth-status', provider],
    queryFn: () => client.get<ConnectionStatus>(statusUrl).then((r) => r.data),
    refetchInterval: 60_000,
  })

  const saveToken = useMutation({
    mutationFn: (t: string) => client.post<ConnectionStatus>(tokenUrl, { token: t }).then((r) => r.data),
    onSuccess: (data) => {
      if (data.connected) {
        toast.success(`${label} connected!`)
        setShowTokenInput(false)
        setToken('')
      } else {
        toast.error(data.error || 'Token invalid')
      }
      qc.invalidateQueries({ queryKey: ['auth-status', provider] })
    },
    onError: () => toast.error('Failed to save token'),
  })

  const authenticate = useMutation({
    mutationFn: () => client.post<ConnectionStatus>(authUrl!).then((r) => r.data),
    onSuccess: (data) => {
      if (data.connected) {
        toast.success(`${label} connected as ${data.username || data.channel_name}!`)
      } else {
        toast.error(data.error || 'Authentication failed')
      }
      qc.invalidateQueries({ queryKey: ['auth-status', provider] })
    },
    onError: () => toast.error('Authentication failed'),
  })

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <div className={clsx('w-10 h-10 rounded-lg flex items-center justify-center', bgColor)}>
            <Icon className={clsx('w-5 h-5', iconColor)} />
          </div>
          <div>
            <h3 className="text-sm font-mono text-gray-200">{label}</h3>
            {isLoading ? (
              <p className="text-[11px] text-gray-600">Checking...</p>
            ) : status?.connected ? (
              <div className="flex items-center gap-1.5">
                <CheckCircle2 className="w-3 h-3 text-primary" />
                <span className="text-[11px] text-primary font-mono">
                  Connected{status.username ? ` as @${status.username}` : status.channel_name ? ` — ${status.channel_name}` : ''}
                </span>
              </div>
            ) : (
              <div className="flex items-center gap-1.5">
                <XCircle className="w-3 h-3 text-gray-600" />
                <span className="text-[11px] text-gray-600">Not connected</span>
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {authUrl && !status?.connected && (
            <button
              onClick={() => authenticate.mutate()}
              disabled={authenticate.isPending}
              className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-secondary/30 text-secondary hover:bg-secondary/10 transition-all"
            >
              {authenticate.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : 'Auto Connect'}
            </button>
          )}
          <button
            onClick={() => setShowTokenInput(!showTokenInput)}
            className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-all flex items-center gap-1.5"
          >
            <Key className="w-3 h-3" />
            {status?.connected ? 'Update Token' : 'Enter Token'}
          </button>
        </div>
      </div>

      {status?.error && !status.connected && !showTokenInput && (
        <p className="text-[11px] text-cyber-orange mt-2 font-mono">{status.error}</p>
      )}

      {showTokenInput && (
        <div className="mt-4 space-y-3">
          <div className="text-[11px] text-gray-500 space-y-1">
            {instructions.map((line, i) => (
              <p key={i}>{line}</p>
            ))}
          </div>
          <div className="flex gap-2">
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={tokenPlaceholder}
              className="flex-1 bg-dark border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:border-primary/50 focus:outline-none"
            />
            <button
              onClick={() => token && saveToken.mutate(token)}
              disabled={!token || saveToken.isPending}
              className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm font-mono hover:bg-primary/20 transition-all disabled:opacity-40"
            >
              {saveToken.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Save'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export default function OAuthConnect() {
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-mono text-gray-400 uppercase tracking-wider">Platform Connections</h2>

      <ConnectionCard
        provider="soundcloud"
        label="SoundCloud"
        icon={Cloud}
        iconColor="text-secondary"
        bgColor="bg-secondary/10"
        statusUrl="/auth/soundcloud/status"
        tokenUrl="/auth/soundcloud/token"
        authUrl="/auth/soundcloud/authenticate"
        tokenPlaceholder="Paste SoundCloud OAuth access token"
        instructions={[
          'Option 1: Click "Auto Connect" if EMAIL + PASSWORD are in your .env',
          'Option 2: Paste an OAuth access token from the SoundCloud API',
        ]}
      />

      <ConnectionCard
        provider="youtube"
        label="YouTube"
        icon={Youtube}
        iconColor="text-cyber-red"
        bgColor="bg-cyber-red/10"
        statusUrl="/auth/youtube/status"
        tokenUrl="/auth/youtube/token"
        tokenPlaceholder="Paste YouTube OAuth refresh token"
        instructions={[
          '1. Go to developers.google.com/oauthplayground',
          '2. Click gear icon, check "Use your own OAuth credentials"',
          '3. Enter your YOUTUBE_CLIENT_ID and CLIENT_SECRET from .env',
          '4. Authorize YouTube Data API v3 scopes',
          '5. Exchange code for tokens, copy the refresh_token',
        ]}
      />
    </div>
  )
}
