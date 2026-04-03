import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Cloud,
  Youtube,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Key,
  Loader2,
  ExternalLink,
  ChevronDown,
  ChevronRight,
  Sparkles,
  Cpu,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import client from '@/api/client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SoundCloudStatus {
  connected: boolean
  username?: string | null
  error?: string | null
}

interface YouTubeStatus {
  connected: boolean
  channel_name?: string | null
  can_upload: boolean
  error?: string | null
}

interface ServiceConfigured {
  configured: boolean
}

interface AllStatus {
  soundcloud: SoundCloudStatus
  youtube: YouTubeStatus
  openai: ServiceConfigured
  fal: ServiceConfigured
}

interface OAuthURLResponse {
  url: string
  redirect_uri: string
}

// ---------------------------------------------------------------------------
// Status indicator dot
// ---------------------------------------------------------------------------

type StatusColor = 'green' | 'yellow' | 'red'

function StatusDot({ color }: { color: StatusColor }) {
  const colorClasses: Record<StatusColor, string> = {
    green: 'bg-primary shadow-[0_0_6px_theme(colors.primary)]',
    yellow: 'bg-gold shadow-[0_0_6px_theme(colors.gold)]',
    red: 'bg-gray-600',
  }
  return <span className={clsx('inline-block w-2.5 h-2.5 rounded-full', colorClasses[color])} />
}

// ---------------------------------------------------------------------------
// Overall status bar
// ---------------------------------------------------------------------------

function StatusBar({ status }: { status: AllStatus | undefined }) {
  if (!status) return null

  const items: { label: string; color: StatusColor }[] = [
    {
      label: 'OpenAI',
      color: status.openai.configured ? 'green' : 'red',
    },
    {
      label: 'fal.ai',
      color: status.fal.configured ? 'green' : 'red',
    },
    {
      label: 'SoundCloud',
      color: status.soundcloud.connected ? 'green' : 'red',
    },
    {
      label: 'YouTube',
      color: status.youtube.can_upload
        ? 'green'
        : status.youtube.connected
          ? 'yellow'
          : 'red',
    },
  ]

  return (
    <div className="flex items-center gap-5 bg-surface-light border border-primary/10 rounded-xl px-5 py-3">
      {items.map((item) => (
        <div key={item.label} className="flex items-center gap-2">
          <StatusDot color={item.color} />
          <span className="text-[11px] font-mono text-gray-400">{item.label}</span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SoundCloud Section
// ---------------------------------------------------------------------------

function SoundCloudSection({ status }: { status: SoundCloudStatus | undefined }) {
  const qc = useQueryClient()
  const [showTokenInput, setShowTokenInput] = useState(false)
  const [token, setToken] = useState('')

  const saveToken = useMutation({
    mutationFn: (t: string) =>
      client.post<SoundCloudStatus>('/auth/soundcloud/token', { token: t }).then((r) => r.data),
    onSuccess: (data) => {
      if (data.connected) {
        toast.success(`SoundCloud connected as @${data.username}!`)
        setShowTokenInput(false)
        setToken('')
      } else {
        toast.error(data.error || 'Token invalid')
      }
      qc.invalidateQueries({ queryKey: ['auth-status-all'] })
    },
    onError: () => toast.error('Failed to save token'),
  })

  const [showAuthFlow, setShowAuthFlow] = useState(false)
  const [authCode, setAuthCode] = useState('')

  const getOAuthUrl = useMutation({
    mutationFn: () =>
      client.get<OAuthURLResponse>('/auth/soundcloud/oauth-url').then((r) => r.data),
    onSuccess: (data) => {
      window.open(data.url, '_blank')
      setShowAuthFlow(true)
    },
    onError: () => toast.error('Failed to generate auth URL'),
  })

  const exchangeCode = useMutation({
    mutationFn: (code: string) =>
      client.post<SoundCloudStatus>('/auth/soundcloud/exchange-code', { code, redirect_uri: 'https://soundcloud.com' }).then((r) => r.data),
    onSuccess: (data) => {
      if (data.connected) {
        toast.success(`SoundCloud connected as @${data.username}!`)
        setShowAuthFlow(false)
        setAuthCode('')
      } else {
        toast.error(data.error || 'Code exchange failed')
      }
      qc.invalidateQueries({ queryKey: ['auth-status-all'] })
    },
    onError: () => toast.error('Code exchange failed'),
  })

  const isConnected = status?.connected

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-secondary/10">
            <Cloud className="w-5 h-5 text-secondary" />
          </div>
          <div>
            <h3 className="text-sm font-mono text-gray-200">SoundCloud</h3>
            {isConnected ? (
              <div className="flex items-center gap-1.5">
                <CheckCircle2 className="w-3 h-3 text-primary" />
                <span className="text-[11px] text-primary font-mono">
                  Connected as @{status.username}
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

        {!isConnected && (
          <div className="flex items-center gap-2">
            <button
              onClick={() => getOAuthUrl.mutate()}
              disabled={getOAuthUrl.isPending}
              className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-secondary/30 text-secondary hover:bg-secondary/10 transition-all disabled:opacity-50"
            >
              {getOAuthUrl.isPending ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                'Authorize'
              )}
            </button>
            <button
              onClick={() => setShowTokenInput(!showTokenInput)}
              className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-all flex items-center gap-1.5"
            >
              <Key className="w-3 h-3" />
              Paste Token
            </button>
          </div>
        )}

        {isConnected && (
          <button
            onClick={() => setShowTokenInput(!showTokenInput)}
            className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-all"
          >
            Reconfigure
          </button>
        )}
      </div>

      {/* Auth code paste flow */}
      {showAuthFlow && !isConnected && (
        <div className="mt-4 space-y-3 p-3 bg-dark/50 border border-secondary/20 rounded-lg">
          <p className="text-[11px] text-gray-400">
            A SoundCloud authorization page opened in a new tab. After authorizing, you'll be redirected to soundcloud.com.
            Copy the <strong className="text-gray-200">code</strong> from the URL bar (after <code className="text-secondary">?code=</code>) and paste it below.
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              value={authCode}
              onChange={(e) => setAuthCode(e.target.value)}
              placeholder="Paste the code from the URL"
              className="flex-1 bg-dark border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:border-secondary/50 focus:outline-none"
            />
            <button
              onClick={() => authCode && exchangeCode.mutate(authCode)}
              disabled={!authCode || exchangeCode.isPending}
              className="px-4 py-2 bg-secondary/10 text-secondary border border-secondary/30 rounded-lg text-sm font-mono hover:bg-secondary/20 transition-all disabled:opacity-40"
            >
              {exchangeCode.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Connect'}
            </button>
          </div>
        </div>
      )}

      {/* Config hints */}
      {!isConnected && !showTokenInput && !showAuthFlow && status?.error && (
        <p className="text-[11px] text-cyber-orange mt-3 font-mono">{status.error}</p>
      )}

      {/* Token input (advanced) */}
      {showTokenInput && (
        <div className="mt-4 space-y-3">
          <p className="text-[11px] text-gray-500">
            Paste an OAuth access token if you have one from the SoundCloud API directly.
          </p>
          <div className="flex gap-2">
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Paste SoundCloud OAuth access token"
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

// ---------------------------------------------------------------------------
// YouTube Section
// ---------------------------------------------------------------------------

function YouTubeSection({ status }: { status: YouTubeStatus | undefined }) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [authCode, setAuthCode] = useState('')
  const [oauthRedirectUri, setOauthRedirectUri] = useState('')
  const [showRefreshTokenInput, setShowRefreshTokenInput] = useState(false)
  const [refreshToken, setRefreshToken] = useState('')
  const [awaitingCode, setAwaitingCode] = useState(false)

  const canUpload = status?.can_upload
  const isConnected = status?.connected

  // Generate OAuth URL
  const generateOAuthUrl = useMutation({
    mutationFn: () =>
      client
        .get<OAuthURLResponse>('/auth/youtube/oauth-url', {
          params: { redirect_uri: 'urn:ietf:wg:oauth:2.0:oob' },
        })
        .then((r) => r.data),
    onSuccess: (data) => {
      setOauthRedirectUri(data.redirect_uri)
      window.open(data.url, '_blank')
      setAwaitingCode(true)
    },
    onError: (err: any) => {
      const msg = err.response?.data?.detail || 'Failed to generate OAuth URL'
      toast.error(msg)
    },
  })

  // Exchange code for tokens
  const exchangeCode = useMutation({
    mutationFn: (code: string) =>
      client
        .post<YouTubeStatus>('/auth/youtube/exchange-code', {
          code,
          redirect_uri: oauthRedirectUri || 'urn:ietf:wg:oauth:2.0:oob',
        })
        .then((r) => r.data),
    onSuccess: (data) => {
      if (data.can_upload) {
        toast.success(`YouTube connected to ${data.channel_name} — uploads enabled!`)
        setAwaitingCode(false)
        setAuthCode('')
        setExpanded(false)
      } else if (data.error) {
        toast.error(data.error)
      }
      qc.invalidateQueries({ queryKey: ['auth-status-all'] })
    },
    onError: () => toast.error('Code exchange failed'),
  })

  // Save refresh token directly
  const saveRefreshToken = useMutation({
    mutationFn: (t: string) =>
      client.post<YouTubeStatus>('/auth/youtube/token', { token: t }).then((r) => r.data),
    onSuccess: (data) => {
      if (data.can_upload) {
        toast.success(`YouTube connected to ${data.channel_name} — uploads enabled!`)
        setShowRefreshTokenInput(false)
        setRefreshToken('')
      } else if (data.connected) {
        toast.success('Token saved, but upload not verified yet.')
      } else {
        toast.error(data.error || 'Token invalid')
      }
      qc.invalidateQueries({ queryKey: ['auth-status-all'] })
    },
    onError: () => toast.error('Failed to save token'),
  })

  // Determine badge
  let badge: React.ReactNode = null
  if (canUpload) {
    badge = (
      <div className="flex items-center gap-1.5">
        <CheckCircle2 className="w-3 h-3 text-primary" />
        <span className="text-[11px] text-primary font-mono">
          Connected to {status?.channel_name} — Uploads enabled
        </span>
      </div>
    )
  } else if (isConnected) {
    badge = (
      <div className="flex items-center gap-1.5">
        <AlertTriangle className="w-3 h-3 text-gold" />
        <span className="text-[11px] text-gold font-mono">
          Connected (read-only) — Upload auth needed
        </span>
      </div>
    )
  } else {
    badge = (
      <div className="flex items-center gap-1.5">
        <XCircle className="w-3 h-3 text-gray-600" />
        <span className="text-[11px] text-gray-600">Not connected</span>
      </div>
    )
  }

  const showSetupByDefault = !canUpload

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-cyber-red/10">
            <Youtube className="w-5 h-5 text-cyber-red" />
          </div>
          <div>
            <h3 className="text-sm font-mono text-gray-200">YouTube</h3>
            {badge}
          </div>
        </div>

        <button
          onClick={() => setExpanded(!expanded)}
          className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-all flex items-center gap-1.5"
        >
          {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          {canUpload ? 'Reconfigure' : 'Setup'}
        </button>
      </div>

      {!canUpload && !expanded && status?.error && (
        <p className="text-[11px] text-cyber-orange mt-3 font-mono">{status.error}</p>
      )}

      {/* Setup steps (auto-expand if not connected) */}
      {(expanded || (showSetupByDefault && !canUpload && !isConnected)) && (
        <div className="mt-5 space-y-4">
          <div className="border-l-2 border-gray-700 pl-4 space-y-5">
            {/* Step 1 */}
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/20 text-primary text-[10px] font-bold">
                  1
                </span>
                <span className="text-xs font-mono text-gray-300">
                  Create a Google Cloud project
                </span>
              </div>
              <p className="text-[11px] text-gray-500 ml-7">
                Go to the Google Cloud Console and create a new project (or use an existing one).
              </p>
              <a
                href="https://console.cloud.google.com/apis/credentials"
                target="_blank"
                rel="noopener noreferrer"
                className="ml-7 inline-flex items-center gap-1.5 text-[11px] font-mono text-cyber-cyan hover:underline"
              >
                <ExternalLink className="w-3 h-3" /> Open Google Cloud Console
              </a>
            </div>

            {/* Step 2 */}
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/20 text-primary text-[10px] font-bold">
                  2
                </span>
                <span className="text-xs font-mono text-gray-300">
                  Enable the YouTube Data API v3
                </span>
              </div>
              <p className="text-[11px] text-gray-500 ml-7">
                In the API Library, search for "YouTube Data API v3" and enable it.
              </p>
              <a
                href="https://console.cloud.google.com/apis/library/youtube.googleapis.com"
                target="_blank"
                rel="noopener noreferrer"
                className="ml-7 inline-flex items-center gap-1.5 text-[11px] font-mono text-cyber-cyan hover:underline"
              >
                <ExternalLink className="w-3 h-3" /> Open API Library
              </a>
            </div>

            {/* Step 3 */}
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/20 text-primary text-[10px] font-bold">
                  3
                </span>
                <span className="text-xs font-mono text-gray-300">
                  Create an OAuth 2.0 Client ID
                </span>
              </div>
              <p className="text-[11px] text-gray-500 ml-7">
                Go to Credentials, click "Create Credentials" &gt; "OAuth client ID". Choose
                application type <strong className="text-gray-400">"Desktop application"</strong>.
                No redirect URI needed — Google handles it for desktop apps.
              </p>
              <p className="text-[11px] text-gray-500 ml-7">
                If prompted for an OAuth consent screen, create one (External is fine for personal
                use). Add yourself as a test user.
              </p>
            </div>

            {/* Step 4 */}
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/20 text-primary text-[10px] font-bold">
                  4
                </span>
                <span className="text-xs font-mono text-gray-300">
                  Add credentials to your .env file
                </span>
              </div>
              <div className="ml-7 bg-dark border border-gray-700 rounded-lg p-3 font-mono text-[11px] text-gray-400">
                <code>
                  FADEOUT_YOUTUBE_CLIENT_ID=your-client-id.apps.googleusercontent.com
                  <br />
                  FADEOUT_YOUTUBE_CLIENT_SECRET=your-client-secret
                </code>
              </div>
              <p className="text-[11px] text-gray-500 ml-7">
                Then restart the backend so it picks up the new env vars.
              </p>
            </div>

            {/* Step 5 — Authorize */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/20 text-primary text-[10px] font-bold">
                  5
                </span>
                <span className="text-xs font-mono text-gray-300">Authorize with Google</span>
              </div>
              <p className="text-[11px] text-gray-500 ml-7">
                Click the button below. A Google sign-in page will open. After you approve, Google
                will show you an authorization code. Copy it and paste it here.
              </p>

              <div className="ml-7 space-y-3">
                <button
                  onClick={() => generateOAuthUrl.mutate()}
                  disabled={generateOAuthUrl.isPending}
                  className="flex items-center gap-2 px-4 py-2 bg-cyber-red/10 text-cyber-red border border-cyber-red/30 rounded-lg text-xs font-mono hover:bg-cyber-red/20 transition-all disabled:opacity-50"
                >
                  {generateOAuthUrl.isPending ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <ExternalLink className="w-3.5 h-3.5" />
                  )}
                  Open Google Authorization
                </button>

                {awaitingCode && (
                  <div className="space-y-2 p-3 bg-dark border border-cyber-red/20 rounded-lg">
                    <p className="text-[11px] text-gray-400 font-mono">
                      After authorizing, paste the code from the Google page below:
                    </p>
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={authCode}
                        onChange={(e) => setAuthCode(e.target.value)}
                        placeholder="Paste authorization code here"
                        className="flex-1 bg-surface border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:border-cyber-red/50 focus:outline-none"
                      />
                      <button
                        onClick={() => authCode && exchangeCode.mutate(authCode)}
                        disabled={!authCode || exchangeCode.isPending}
                        className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm font-mono hover:bg-primary/20 transition-all disabled:opacity-40"
                      >
                        {exchangeCode.isPending ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          'Connect'
                        )}
                      </button>
                    </div>
                    {exchangeCode.isError && (
                      <p className="text-[11px] text-cyber-red font-mono">
                        Exchange failed. Make sure you copied the full code and try again.
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Alternative: paste refresh token directly */}
            <div className="space-y-2 pt-2 border-t border-gray-800">
              <button
                onClick={() => setShowRefreshTokenInput(!showRefreshTokenInput)}
                className="flex items-center gap-1.5 text-[11px] font-mono text-gray-500 hover:text-gray-300 transition-colors"
              >
                <Key className="w-3 h-3" />
                {showRefreshTokenInput
                  ? 'Hide manual refresh token input'
                  : 'Already have a refresh token? Paste it directly'}
              </button>

              {showRefreshTokenInput && (
                <div className="space-y-2 ml-4">
                  <p className="text-[11px] text-gray-500">
                    If you obtained a refresh token from Google OAuth Playground or another method,
                    paste it here.
                  </p>
                  <div className="flex gap-2">
                    <input
                      type="password"
                      value={refreshToken}
                      onChange={(e) => setRefreshToken(e.target.value)}
                      placeholder="Paste YouTube OAuth refresh token"
                      className="flex-1 bg-dark border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:border-primary/50 focus:outline-none"
                    />
                    <button
                      onClick={() => refreshToken && saveRefreshToken.mutate(refreshToken)}
                      disabled={!refreshToken || saveRefreshToken.isPending}
                      className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm font-mono hover:bg-primary/20 transition-all disabled:opacity-40"
                    >
                      {saveRefreshToken.isPending ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        'Save'
                      )}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// API Key status cards (OpenAI, fal)
// ---------------------------------------------------------------------------

function ApiKeyCard({
  label,
  icon: Icon,
  iconColor,
  bgColor,
  configured,
  envVar,
}: {
  label: string
  icon: React.ElementType
  iconColor: string
  bgColor: string
  configured: boolean
  envVar: string
}) {
  return (
    <div className="flex items-center gap-3 bg-surface-light border border-primary/10 rounded-xl px-4 py-3">
      <div className={clsx('w-8 h-8 rounded-lg flex items-center justify-center', bgColor)}>
        <Icon className={clsx('w-4 h-4', iconColor)} />
      </div>
      <div className="flex-1">
        <span className="text-xs font-mono text-gray-300">{label}</span>
        <div className="flex items-center gap-1.5">
          {configured ? (
            <>
              <CheckCircle2 className="w-3 h-3 text-primary" />
              <span className="text-[11px] text-primary font-mono">Configured</span>
            </>
          ) : (
            <>
              <XCircle className="w-3 h-3 text-gray-600" />
              <span className="text-[11px] text-gray-600 font-mono">
                Add {envVar} to .env
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Setup Wizard
// ---------------------------------------------------------------------------

export default function SetupWizard() {
  const { data: status, isLoading } = useQuery({
    queryKey: ['auth-status-all'],
    queryFn: () => client.get<AllStatus>('/auth/status').then((r) => r.data),
    refetchInterval: 30_000,
  })

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="h-12 bg-surface-light rounded-xl animate-pulse" />
        <div className="h-24 bg-surface-light rounded-xl animate-pulse" />
        <div className="h-24 bg-surface-light rounded-xl animate-pulse" />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <h2 className="text-sm font-mono text-gray-400 uppercase tracking-wider">
        Platform Connections
      </h2>

      {/* Overall status bar */}
      <StatusBar status={status} />

      {/* API Key cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <ApiKeyCard
          label="OpenAI"
          icon={Sparkles}
          iconColor="text-cyber-cyan"
          bgColor="bg-cyber-cyan/10"
          configured={status?.openai.configured ?? false}
          envVar="FADEOUT_OPENAI_API_KEY"
        />
        <ApiKeyCard
          label="fal.ai"
          icon={Cpu}
          iconColor="text-cyber-magenta"
          bgColor="bg-cyber-magenta/10"
          configured={status?.fal.configured ?? false}
          envVar="FADEOUT_FAL_API_KEY"
        />
      </div>

      {/* SoundCloud */}
      <SoundCloudSection status={status?.soundcloud} />

      {/* YouTube */}
      <YouTubeSection status={status?.youtube} />
    </div>
  )
}
