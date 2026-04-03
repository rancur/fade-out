import {
  ArrowUpCircle,
  Download,
  Shield,
  Clock,
  CheckCircle2,
  AlertTriangle,
  HardDrive,
  FileText,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { format } from 'date-fns'
import { useUpgradeStatus, usePerformUpgrade, useCreateBackup } from '@/api/hooks'

export default function Upgrade() {
  const { data: status, isLoading } = useUpgradeStatus()
  const doUpgrade = usePerformUpgrade()
  const createBackup = useCreateBackup()

  const handleUpgrade = () => {
    if (!confirm('Apply the update? A backup will be created automatically.')) return
    doUpgrade.mutate(undefined, {
      onSuccess: () => toast.success('Update applied successfully! Restart may be required.'),
      onError: () => toast.error('Update failed - check logs'),
    })
  }

  const handleBackup = () => {
    createBackup.mutate(undefined, {
      onSuccess: () => toast.success('Backup created'),
      onError: () => toast.error('Failed to create backup'),
    })
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="h-8 w-48 bg-surface-light rounded animate-pulse" />
        <div className="h-48 bg-surface-light rounded-xl animate-pulse" />
      </div>
    )
  }

  const hasUpdate = status?.update_available ?? false

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

        {hasUpdate && (
          <>
            <div className="neon-line" />
            <div>
              <p className="text-[11px] text-gray-500 font-mono uppercase tracking-wider mb-1">Latest Version</p>
              <p className="text-lg font-bold text-gold font-mono">{status?.latest_version}</p>
            </div>
            <button
              onClick={handleUpgrade}
              disabled={doUpgrade.isPending}
              className="flex items-center gap-2 px-6 py-3 bg-gold/10 text-gold border border-gold/30 rounded-lg text-sm font-semibold hover:bg-gold/20 disabled:opacity-50 transition-all"
            >
              <Download className="w-4 h-4" />
              {doUpgrade.isPending ? 'Applying Update...' : `Update to ${status?.latest_version}`}
            </button>
          </>
        )}
      </div>

      {/* Changelog */}
      {status?.changelog && (
        <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-4">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <FileText className="w-4 h-4" /> Changelog
          </h2>
          <div className="text-sm text-gray-400 whitespace-pre-wrap leading-relaxed bg-dark rounded-lg p-4 font-mono max-h-80 overflow-y-auto">
            {status.changelog}
          </div>
        </div>
      )}

      {/* Backups */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-primary/10">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <HardDrive className="w-4 h-4" /> Backups
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
          {!status?.backups || status.backups.length === 0 ? (
            <div className="px-6 py-8 text-center text-gray-600 text-sm">No backups yet.</div>
          ) : (
            status.backups.map((backup) => (
              <div key={backup.name} className="flex items-center justify-between px-6 py-3">
                <div className="flex items-center gap-3">
                  <HardDrive className="w-4 h-4 text-gray-600" />
                  <div>
                    <p className="text-sm text-gray-300 font-mono">{backup.name}</p>
                    <p className="text-[10px] text-gray-600 font-mono flex items-center gap-2">
                      <Clock className="w-3 h-3" />
                      {format(new Date(backup.date), 'MMM d, yyyy HH:mm')}
                    </p>
                  </div>
                </div>
                <span className="text-[11px] text-gray-500 font-mono">{backup.size_mb.toFixed(1)} MB</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
