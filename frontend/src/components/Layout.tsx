import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import clsx from 'clsx'
import { useLiveEvents, useWsConnected } from '../api/hooks'
import {
  LayoutDashboard,
  Library,
  Music2,
  Palette,
  Settings,
  Brain,
  Bell,
  Activity,
  ArrowUpCircle,
  ChevronLeft,
  ChevronRight,
  Radio,
} from 'lucide-react'

const nav = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/mixes', icon: Music2, label: 'Mixes' },
  { to: '/catalog', icon: Library, label: 'Catalog' },
  { to: '/activity', icon: Activity, label: 'Activity' },
  { to: '/brand', icon: Palette, label: 'Brand' },
  { to: '/settings', icon: Settings, label: 'Settings' },
  { to: '/ai', icon: Brain, label: 'AI Usage' },
  { to: '/notifications', icon: Bell, label: 'Notifications' },
  { to: '/upgrade', icon: ArrowUpCircle, label: 'Upgrade' },
]

function LiveDot({ collapsed }: { collapsed: boolean }) {
  const connected = useWsConnected()
  return (
    <div
      className="flex items-center gap-2 px-4 py-2 border-t border-primary/10"
      title={connected ? 'Live updates connected' : 'Live updates degraded — polling'}
    >
      <span
        className={clsx(
          'w-2 h-2 rounded-full shrink-0',
          connected
            ? 'bg-cyber-lime shadow-[0_0_6px_theme(colors.cyber.lime)]'
            : 'bg-gold shadow-[0_0_6px_theme(colors.gold)] animate-pulse',
        )}
      />
      {!collapsed && (
        <span className={clsx('text-[10px] font-mono', connected ? 'text-gray-500' : 'text-gold')}>
          {connected ? 'live' : 'polling'}
        </span>
      )}
    </div>
  )
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false)

  // Bridge WebSocket events into react-query caches app-wide
  useLiveEvents()

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside
        className={clsx(
          'flex flex-col bg-surface-light border-r border-primary/20 transition-all duration-300 relative z-20',
          collapsed ? 'w-16' : 'w-56',
        )}
      >
        {/* Logo */}
        <div className="flex items-center gap-3 px-4 h-16 border-b border-primary/10">
          <Radio className="w-7 h-7 text-primary shrink-0" />
          {!collapsed && (
            <div className="overflow-hidden">
              <h1 className="font-pixel text-xs text-primary tracking-wider leading-tight">
                fade-out
              </h1>
              <p className="text-[10px] text-gray-500 font-mono mt-0.5">mix automation</p>
            </div>
          )}
        </div>

        {/* Nav */}
        <nav className="flex-1 py-4 space-y-1 px-2 overflow-y-auto">
          {nav.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all duration-200 group',
                  isActive
                    ? 'bg-primary/10 text-primary shadow-neon/20'
                    : 'text-gray-400 hover:text-gray-200 hover:bg-white/5',
                )
              }
            >
              <Icon
                className={clsx(
                  'w-5 h-5 shrink-0 transition-colors',
                  'group-[.active]:text-primary',
                )}
              />
              {!collapsed && <span className="truncate">{label}</span>}
            </NavLink>
          ))}
        </nav>

        {/* Live connection indicator */}
        <LiveDot collapsed={collapsed} />

        {/* Collapse toggle */}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="flex items-center justify-center h-12 border-t border-primary/10 text-gray-500 hover:text-primary transition-colors"
        >
          {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
        </button>

        {/* Version */}
        {!collapsed && (
          <div className="px-4 py-2 border-t border-primary/10">
            <p className="text-[10px] text-gray-600 font-mono">v2.1.0</p>
          </div>
        )}
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto cyber-grid">
        <div className="p-6 max-w-7xl mx-auto animate-fade-in">{children}</div>
      </main>
    </div>
  )
}
