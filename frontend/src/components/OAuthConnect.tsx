import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
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
  Eye,
  EyeOff,
  Save,
  Info,
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

interface CredentialInfo {
  name: string
  is_set: boolean
  source: string | null
  masked_value: string | null
}

interface CredentialsResponse {
  credentials: CredentialInfo[]
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
// Credential input field with show/hide toggle
// ---------------------------------------------------------------------------

function CredentialField({
  label,
  name,
  value,
  onChange,
  placeholder,
  credentialInfo,
}: {
  label: string
  name: string
  value: string
  onChange: (name: string, value: string) => void
  placeholder?: string
  credentialInfo?: CredentialInfo
}) {
  const [visible, setVisible] = useState(false)

  const hint = credentialInfo?.is_set
    ? credentialInfo.source === 'env'
      ? `Set via .env (${credentialInfo.masked_value})`
      : `Saved (${credentialInfo.masked_value})`
    : 'Not set'

  return (
    <div className="space-y-1">
      <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">
        {label}
      </label>
      <div className="relative">
        <input
          type={visible ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(name, e.target.value)}
          placeholder={placeholder || `Enter ${label}`}
          className="w-full bg-dark border border-gray-700 rounded-lg px-3 py-2 pr-10 text-sm font-mono text-gray-200 placeholder-gray-600 focus:border-primary/50 focus:outline-none"
        />
        <button
          type="button"
          onClick={() => setVisible(!visible)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 transition-colors"
        >
          {visible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
      <p className={clsx(
        'text-[10px] font-mono',
        credentialInfo?.is_set ? 'text-gray-500' : 'text-gray-600',
      )}>
        {hint}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Credentials Management Section
// ---------------------------------------------------------------------------

function CredentialsSection({
  onCredentialsSaved,
}: {
  onCredentialsSaved: () => void
}) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [formValues, setFormValues] = useState<Record<string, string>>({})
  const [hasChanges, setHasChanges] = useState(false)

  const { data: credentialsData, isLoading } = useQuery({
    queryKey: ['credentials'],
    queryFn: () =>
      client.get<CredentialsResponse>('/auth/credentials').then((r) => r.data),
  })

  const saveCredentials = useMutation({
    mutationFn: (creds: Record<string, string>) =>
      client.put<CredentialsResponse>('/auth/credentials', { credentials: creds }).then((r) => r.data),
    onSuccess: () => {
      toast.success('Credentials saved')
      setFormValues({})
      setHasChanges(false)
      qc.invalidateQueries({ queryKey: ['credentials'] })
      qc.invalidateQueries({ queryKey: ['auth-status-all'] })
      onCredentialsSaved()
    },
    onError: () => toast.error('Failed to save credentials'),
  })

  const handleChange = (name: string, value: string) => {
    setFormValues((prev) => ({ ...prev, [name]: value }))
    setHasChanges(true)
  }

  const handleSave = () => {
    // Only send fields that have been modified
    const toSave: Record<string, string> = {}
    for (const [k, v] of Object.entries(formValues)) {
      if (v !== '') {
        toSave[k] = v
      }
    }
    if (Object.keys(toSave).length > 0) {
      saveCredentials.mutate(toSave)
    }
  }

  const getCredInfo = (name: string): CredentialInfo | undefined =>
    credentialsData?.credentials.find((c) => c.name === name)

  // Auto-expand if nothing is configured
  const allConfigured = credentialsData?.credentials.every((c) => c.is_set) ?? false

  const credentialGroups = [
    {
      title: 'AI Services',
      fields: [
        { label: 'OpenAI API Key', name: 'openai_api_key', placeholder: 'sk-proj-...' },
        { label: 'fal.ai API Key', name: 'fal_api_key', placeholder: 'fal-...' },
      ],
    },
    {
      title: 'SoundCloud OAuth',
      fields: [
        { label: 'Client ID', name: 'soundcloud_client_id', placeholder: 'Your SoundCloud app Client ID' },
        { label: 'Client Secret', name: 'soundcloud_client_secret', placeholder: 'Your SoundCloud app Client Secret' },
      ],
    },
    {
      title: 'YouTube / Google OAuth',
      fields: [
        { label: 'Client ID', name: 'youtube_client_id', placeholder: 'xxxx.apps.googleusercontent.com' },
        { label: 'Client Secret', name: 'youtube_client_secret', placeholder: 'GOCSPX-...' },
        { label: 'API Key (optional, read-only)', name: 'youtube_api_key', placeholder: 'AIza...' },
      ],
    },
  ]

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-primary/10">
            <Key className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h3 className="text-sm font-mono text-gray-200">API Credentials</h3>
            <p className="text-[11px] text-gray-500">
              {allConfigured
                ? 'All credentials configured'
                : 'Configure API keys and OAuth client secrets'}
            </p>
          </div>
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="px-3 py-1.5 rounded-lg text-[11px] font-mono border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-all flex items-center gap-1.5"
        >
          {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          {allConfigured ? 'Edit' : 'Configure'}
        </button>
      </div>

      {(expanded || !allConfigured) && (
        <div className="mt-5 space-y-6">
          {isLoading ? (
            <div className="h-20 bg-dark/50 rounded-lg animate-pulse" />
          ) : (
            <>
              {credentialGroups.map((group) => (
                <div key={group.title} className="space-y-3">
                  <h4 className="text-[11px] font-mono text-gray-400 uppercase tracking-wider">
                    {group.title}
                  </h4>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {group.fields.map((field) => (
                      <CredentialField
                        key={field.name}
                        label={field.label}
                        name={field.name}
                        value={formValues[field.name] || ''}
                        onChange={handleChange}
                        placeholder={field.placeholder}
                        credentialInfo={getCredInfo(field.name)}
                      />
                    ))}
                  </div>
                </div>
              ))}

              {hasChanges && (
                <div className="flex justify-end">
                  <button
                    onClick={handleSave}
                    disabled={saveCredentials.isPending}
                    className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-xs font-mono hover:bg-primary/20 transition-all disabled:opacity-50"
                  >
                    {saveCredentials.isPending ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Save className="w-3.5 h-3.5" />
                    )}
                    Save Credentials
                  </button>
                </div>
              )}

              <div className="flex items-start gap-2 p-3 bg-dark/50 border border-gray-800 rounded-lg">
                <Info className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" />
                <p className="text-[10px] text-gray-500 font-mono">
                  Credentials saved here override .env values. Leave a field empty to use the .env
                  fallback. Values are stored server-side and never sent to the browser in plain text.
                </p>
              </div>
            </>
          )}
        </div>
      )}
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

  // Authorize via callback flow — opens in same window, SoundCloud redirects back
  const getOAuthUrl = useMutation({
    mutationFn: () =>
      client.get<OAuthURLResponse>('/auth/soundcloud/oauth-url').then((r) => r.data),
    onSuccess: (data) => {
      // Navigate to SoundCloud auth — callback will redirect back to /settings
      window.location.href = data.url
    },
    onError: (err: any) => {
      const msg = err.response?.data?.detail || 'Failed to generate auth URL. Make sure SoundCloud credentials are configured above.'
      toast.error(msg)
    },
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

      {/* Config hints */}
      {!isConnected && !showTokenInput && status?.error && (
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
  const [showRefreshTokenInput, setShowRefreshTokenInput] = useState(false)
  const [refreshToken, setRefreshToken] = useState('')

  const canUpload = status?.can_upload
  const isConnected = status?.connected

  // Authorize via callback flow
  const generateOAuthUrl = useMutation({
    mutationFn: () =>
      client
        .get<OAuthURLResponse>('/auth/youtube/oauth-url')
        .then((r) => r.data),
    onSuccess: (data) => {
      // Navigate to Google auth — callback will redirect back to /settings
      window.location.href = data.url
    },
    onError: (err: any) => {
      const msg = err.response?.data?.detail || 'Failed to generate OAuth URL. Make sure YouTube credentials are configured above.'
      toast.error(msg)
    },
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
              </p>
              <p className="text-[11px] text-gray-500 ml-7">
                If prompted for an OAuth consent screen, create one (External is fine for personal
                use). Add yourself as a test user.
              </p>
            </div>

            {/* Step 4 — Save credentials via UI */}
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/20 text-primary text-[10px] font-bold">
                  4
                </span>
                <span className="text-xs font-mono text-gray-300">
                  Enter your Client ID and Secret above
                </span>
              </div>
              <p className="text-[11px] text-gray-500 ml-7">
                Copy the Client ID and Client Secret from Google Cloud Console and paste them into
                the <strong className="text-gray-400">API Credentials</strong> section above, then
                click Save Credentials.
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
                Click the button below. You will be redirected to Google to sign in and approve access.
                After approval, you will be automatically redirected back here.
              </p>

              <div className="ml-7 space-y-3">
                {/* Desktop app localhost note */}
                <div className="flex items-start gap-2 p-3 bg-dark/50 border border-gold/20 rounded-lg">
                  <AlertTriangle className="w-4 h-4 text-gold shrink-0 mt-0.5" />
                  <p className="text-[10px] text-gray-400 font-mono">
                    <strong className="text-gold">Desktop OAuth client?</strong> Google only allows{' '}
                    <code className="text-cyber-cyan">http://127.0.0.1</code> redirects for Desktop
                    apps. Access fade-out at{' '}
                    <code className="text-cyber-cyan">http://127.0.0.1:8500</code> for this step.
                    After authorization, normal LAN access works fine.
                  </p>
                </div>

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
                  Authorize with Google
                </button>
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
// Main Setup Wizard
// ---------------------------------------------------------------------------

export default function SetupWizard() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const { data: status, isLoading } = useQuery({
    queryKey: ['auth-status-all'],
    queryFn: () => client.get<AllStatus>('/auth/status').then((r) => r.data),
    refetchInterval: 30_000,
  })

  // Detect OAuth callback results from URL params
  useEffect(() => {
    const auth = searchParams.get('auth')
    const success = searchParams.get('success')
    const error = searchParams.get('error')

    if (auth && (success || error)) {
      const service = auth === 'soundcloud' ? 'SoundCloud' : auth === 'youtube' ? 'YouTube' : auth

      if (success === '1') {
        toast.success(`${service} connected successfully!`)
        // Refresh status
        qc.invalidateQueries({ queryKey: ['auth-status-all'] })
      } else if (error) {
        const errorMessages: Record<string, string> = {
          no_code_received: 'No authorization code received from the provider.',
          missing_client_credentials: 'Client credentials are missing. Configure them in API Credentials above.',
          code_exchange_failed: 'Failed to exchange the authorization code for tokens. Try again.',
          no_refresh_token_returned: 'No refresh token was returned. Try revoking app access at myaccount.google.com/permissions and re-authorize.',
        }
        toast.error(`${service} auth failed: ${errorMessages[error] || error}`)
      }

      // Clean up URL params
      const newParams = new URLSearchParams(searchParams)
      newParams.delete('auth')
      newParams.delete('success')
      newParams.delete('error')
      setSearchParams(newParams, { replace: true })
    }
  }, [searchParams, setSearchParams, qc])

  const handleCredentialsSaved = () => {
    qc.invalidateQueries({ queryKey: ['auth-status-all'] })
  }

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

      {/* API Credentials (editable from UI) */}
      <CredentialsSection onCredentialsSaved={handleCredentialsSaved} />

      {/* SoundCloud */}
      <SoundCloudSection status={status?.soundcloud} />

      {/* YouTube */}
      <YouTubeSection status={status?.youtube} />
    </div>
  )
}
