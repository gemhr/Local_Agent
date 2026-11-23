import { useEffect, useEffectEvent, useState } from 'react'
import { ApiError, type WorkspaceApi } from './api'

type Entry = { cursor: number; event_type: string; payload: Record<string, unknown>; created_at?: string }
type History = { events: Entry[]; next_cursor: number; has_more: boolean }
const limit = 240
const eventNames: Record<string, string> = {
  'run.started': '执行已接纳', 'run.progress': '执行进展', 'output.delta': '输出片段',
  'run.completed': '执行结束', 'run.failed': '执行失败', 'run.error': '执行错误',
  'run.cancelled': '已取消', 'run.cancellation': '收到取消请求',
  'run.observation_lost': '观察中断', 'approval.required': '运行时请求审批',
}

function parseEvent(frame: string) {
  let event = ''
  let cursor = NaN
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    const at = line.indexOf(':')
    if (at <= 0) continue
    const field = line.slice(0, at)
    const value = line.slice(at + 1).replace(/^ /, '')
    if (field === 'event') event = value
    if (field === 'id') cursor = Number(value)
    if (field === 'data') data.push(value)
  }
  return { event, cursor, data: data.join('\n') }
}

export function RunEvents({ api, runId, active, onRevoked }: {
  api: WorkspaceApi; runId: string; active: boolean; onRevoked: () => void
}) {
  const [entries, setEntries] = useState<Entry[]>([])
  const [connection, setConnection] = useState('正在读取事件历史…')
  const [truncated, setTruncated] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const revoked = useEffectEvent(onRevoked)
  useEffect(() => {
    const controller = new AbortController()
    let cursor = 0
    let retries = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const seen = new Map<number, Entry>()
    const add = (batch: Entry[]) => {
      for (const entry of batch) {
        if (entry.cursor <= cursor) continue
        seen.set(entry.cursor, entry)
        cursor = entry.cursor
        if (seen.size > limit) {
          seen.delete(seen.keys().next().value!)
          setTruncated(true)
        }
      }
      if (!controller.signal.aborted) setEntries([...seen.values()])
    }
    const deny = () => {
      setEntries([])
      setConnection('当前身份已无法读取事件。')
      revoked()
    }
    async function connect() {
      if (controller.signal.aborted) return
      try {
        // 每次连接先补历史，再从最后一个已收事件订阅。
        for (let i = 0; i < 5; i++) {
          const history = await api.observation<History>(
            `/runs/${encodeURIComponent(runId)}/events/history?after_cursor=${cursor}&limit=100`, controller.signal,
          )
          add(history.events)
          if (!history.has_more) break
          if (i === 4) setTruncated(true)
        }
        if (!active) { setConnection('已读取持久事件'); return }
        const reader = (await api.eventStream(runId, cursor, controller.signal)).getReader()
        setConnection('实时观察中')
        let buffer = ''
        try {
          while (true) {
            const { value, done } = await reader.read()
            if (done) break
            buffer += value
            const frames = buffer.split(/\r?\n\r?\n/)
            buffer = frames.pop() ?? ''
            const batch: Entry[] = []
            for (const frame of frames) {
              const { event, cursor: id, data } = parseEvent(frame)
              if (event === 'access.revoked') { deny(); return }
              if (!event || !Number.isSafeInteger(id) || id <= cursor) continue
              batch.push({ cursor: id, event_type: event, payload: JSON.parse(data) })
            }
            if (batch.length) add(batch)
            if (batch.some(item => ['run.completed', 'run.failed', 'run.cancelled'].includes(item.event_type))) {
              setConnection('事件流已结束')
              return
            }
          }
        } finally {
          await reader.cancel().catch(() => undefined)
        }
      } catch (failure) {
        if (controller.signal.aborted) return
        if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) { deny(); return }
      }
      if (controller.signal.aborted) return
      if (retries >= 3) {
        setConnection('暂时无法连接事件流')
      } else {
        setConnection('观察连接中断，正在重连…')
        timer = setTimeout(() => void connect(), 1000 * 2 ** retries++)
      }
    }
    void connect()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [api, runId, active, attempt])

  return <section className="panel event-panel">
    <header className="panel-heading"><h2>执行事件</h2><span className="muted" role="status">{connection}</span></header>
    {connection === '暂时无法连接事件流' && <button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>重新连接</button>}
    {truncated && <p className="muted">事件较多，页面仅展示本次读取的最近片段。</p>}
    {entries.length === 0 && <p className="muted">暂无可读取事件。</p>}
    <ol className="event-list">{entries.map(entry => {
      const message = ['text', 'safe_message', 'error_code', 'reason', 'phase'].map(key => entry.payload[key]).find(value => typeof value === 'string')
      return <li key={entry.cursor}><span className="event-cursor">#{entry.cursor}</span>
        <div><strong>{eventNames[entry.event_type] ?? entry.event_type}</strong>
          {typeof message === 'string' && <p className="event-message">{message}</p>}</div>
        {entry.created_at && <time>{new Date(entry.created_at).toLocaleTimeString()}</time>}
      </li>
    })}</ol>
  </section>
}
