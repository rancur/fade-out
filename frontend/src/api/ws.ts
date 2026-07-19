// Singleton WebSocket manager for /api/ws/status.
// Auto-reconnects with exponential backoff (1s -> 30s cap), sends a heartbeat
// ping every 30s, and exposes a "degraded" flag consumers can read so the UI
// can fall back to polling when the socket is down.

export interface WsMessage {
  event: string
  mix_id: string | null
  data: Record<string, unknown>
}

type EventCallback = (msg: WsMessage) => void
type StatusCallback = (connected: boolean) => void

const BACKOFF_BASE_MS = 1_000
const BACKOFF_CAP_MS = 30_000
const HEARTBEAT_MS = 30_000

class WsManager {
  private ws: WebSocket | null = null
  private listeners = new Map<string, Set<EventCallback>>()
  private statusListeners = new Set<StatusCallback>()
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private started = false
  private _connected = false

  /** True when the socket is down and consumers should poll instead. */
  get degraded(): boolean {
    return !this._connected
  }

  get connected(): boolean {
    return this._connected
  }

  /**
   * Subscribe to a WS event ("step_progress", "activity", ... or "*" for all).
   * Returns an unsubscribe function. Connects lazily on first subscriber.
   */
  subscribe(event: string, cb: EventCallback): () => void {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set())
    this.listeners.get(event)!.add(cb)
    this.ensureStarted()
    return () => {
      this.listeners.get(event)?.delete(cb)
    }
  }

  /** Subscribe to connection status changes (green dot / amber dot). */
  subscribeStatus(cb: StatusCallback): () => void {
    this.statusListeners.add(cb)
    this.ensureStarted()
    return () => {
      this.statusListeners.delete(cb)
    }
  }

  private ensureStarted() {
    if (this.started) return
    this.started = true
    this.connect()
  }

  private url(): string {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${window.location.host}/api/ws/status`
  }

  private connect() {
    if (typeof WebSocket === 'undefined') return // non-browser env (tests)
    try {
      this.ws = new WebSocket(this.url())
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.reconnectAttempt = 0
      this.setConnected(true)
      this.startHeartbeat()
    }

    this.ws.onmessage = (ev: MessageEvent) => {
      if (ev.data === 'pong') return
      let msg: WsMessage
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      if (!msg || typeof msg.event !== 'string' || msg.event === 'pong') return
      this.emit(msg)
    }

    this.ws.onclose = () => {
      this.setConnected(false)
      this.stopHeartbeat()
      this.scheduleReconnect()
    }

    this.ws.onerror = () => {
      // onclose fires after onerror; close explicitly in case it doesn't.
      this.ws?.close()
    }
  }

  private emit(msg: WsMessage) {
    this.listeners.get(msg.event)?.forEach((cb) => cb(msg))
    this.listeners.get('*')?.forEach((cb) => cb(msg))
  }

  private setConnected(connected: boolean) {
    if (this._connected === connected) return
    this._connected = connected
    this.statusListeners.forEach((cb) => cb(connected))
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return
    const delay = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** this.reconnectAttempt)
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('ping')
      }
    }, HEARTBEAT_MS)
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }
}

export const wsManager = new WsManager()
