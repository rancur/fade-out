import {
  ArrowUpCircle,
  Download,
  Shield,
  Clock,
  CheckCircle2,
  AlertTriangle,
  HardDrive,
  RefreshCw,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { format } from 'date-fns'
import { useUpgradeStatus, useCheckUpgrade, useApplyUpgrade, useBackups, useCreateBackup } from '@/api/hooks'

export default function Upgrade() {
  const { data: status, isLoading: statusLoading } = useUpgradeStatus()
  const { data: backupData, isLoading: backupsLoading } = useBackups()
  const checkUpgrade = useCheckUpgrade()
  const applyUpgrade = useApplyUpgrade()
  const createBackup = useCreateBackup()

  const handleUpgrade = () => {
    if (!confirm('Apply the update? A backup will be created automatically.')) return
    applyUpgrade.mutate(undefined, {
      onSuccess: () => toast.success('Update applied successfully! Restart may be required.'),
      onError: () => toast.error('Update failed - check logs'),
    })
  }

  const handleCheck = () => {
    checkUpgrade.mutate(undefined, {
      onSuccess: () => toast.success('Upgrade check complete'),
      onError: () => toast.error('Failed to check for updates'),
    })
  }

  const handleBackup = () => {
    createBackup.mutate(undefined, {
      onSuccess: () => toast.success('Backup created'),
      onError: () => toast.error('Failed to create backup'),
    })
  }

  const isLoading = statusLoading || backupsLoading

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="h-8 w-48 bg-surface-light rounded animate-pulse" />
        <div className="h-48 bg-surface-light rounded-xl animate-pulse" />
      </div>
    )
  }

  const hasUpdate = status?.update_available ?? false
  const backups = backupData?.backups ?? []

  return (
    <div className="space-y-8 max-w-3xl">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
          <ArrowUpCircle className="w-6 h-6" /> System
        </h1>
        <p className="text-sm text-gray-500 mt-1">Version management and backups</p>
      </div>

      {/* Version Card */}
      <div
        className={clsx(
          'bg-surface-light border rounded-xl p-6 space-y-5',
          hasUpdate ? 'border-gold/30' : 'border-primary/10',
        )}
      >
        <div className="flex items-start justify-between">
          <div>
            <p className="text-[11px] text-gray-500 font-mono uppercase tracking-wider mb-1">Current Version</p>
            <p className="text-xl font-bold text-gray-100 font-mono">
              {status?.current_version ?? 'unknown'}
            </p>
          </div>
          {hasUpdate ? (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-gold/10 border border-gold/30 rounded-full">
              <AlertTriangle className="w-3.5 h-3.5 text-gold" />
              <span className="text-[11px] text-gold font-mono">Update Available</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-primary/10 border border-primary/30 rounded-full">
              <CheckCircle2 className="w-3.5 h-3.5 text-primary" />
              <span className="text-[11px] text-primary font-mono">Up to Date</span>
            </div>
          )}
        </div>

        {status?.last_checked && (
          <p className="text-[10px] text-gray-600 font-mono">
            Last checked: {format(new Date(status.last_checked), 'MMM d, yyyy HH:mm')}
          </p>
        )}

        <div className="flex gap-3">
          <button
            onClick={handleCheck}
            disabled={checkUpgrade.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-surface-light border border-primary/20 text-gray-400 rounded-lg text-sm hover:text-primary hover:border-primary/40 disabled:opacity-50 transition-all"
          >
            <RefreshCw className={clsx('w-4 h-4', checkUpgrade.isPending && 'animate-spin')} />
            {checkUpgrade.isPending ? 'Checking...' : 'Check for Updates'}
          </button>
        </div>

        {hasUpdate && (
          <>
            <div className="neon-line" />
            <div>
              <p className="text-[11px] text-gray-500 font-mono uppercase tracking-wider mb-1">Latest Version</p>
              <p className="text-lg font-bold text-gold font-mono">{status?.latest_version}</p>
            </div>
            <button
              onClick={handleUpgrade}
              disabled={applyUpgrade.isPending}
              className="flex items-center gap-2 px-6 py-3 bg-gold/10 text-gold border border-gold/30 rounded-lg text-sm font-semibold hover:bg-gold/20 disabled:opacity-50 transition-all"
            >
              <Download className="w-4 h-4" />
              {applyUpgrade.isPending ? 'Applying Update...' : `Update to ${status?.latest_version}`}
            </button>
          </>
        )}
      </div>

      {/* Backups */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-primary/10">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <HardDrive className="w-4 h-4" /> Backups
            {backupData?.total != null && (
              <span className="text-[10px] text-gray-600">({backupData.total})</span>
            )}
          </h2>
          <button
            onClick={handleBackup}
            disabled={createBackup.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 disabled:opacity-50 transition-all"
          >
            <Shield className="w-3.5 h-3.5" />
            {createBackup.isPending ? 'Creating...' : 'Create Backup'}
          </button>
        </div>
        <div className="divide-y divide-white/5">
          {backups.length === 0 ? (
            <div className="px-6 py-8 text-center text-gray-600 text-sm">No backups yet.</div>
          ) : (
            backups.map((backup) => (
              <div key={backup.filename} className="flex items-center justify-between px-6 py-3">
                <div className="flex items-center gap-3">
                  <HardDrive className="w-4 h-4 text-gray-600" />
                  <div>
                    <p className="text-sm text-gray-300 font-mono">{backup.filename}</p>
                    <p className="text-[10px] text-gray-600 font-mono flex items-center gap-2">
                      <Clock className="w-3 h-3" />
                      {format(new Date(backup.created_at), 'MMM d, yyyy HH:mm')}
                    </p>
                  </div>
                </div>
                <span className="text-[11px] text-gray-500 font-mono">
                  {(backup.size_bytes / (1024 * 1024)).toFixed(1)} MB
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
