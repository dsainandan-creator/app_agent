import React, { useState, useEffect } from 'react'

const API = ''  // proxied via vite → localhost:8000

// ─── helpers ────────────────────────────────────────────────────────────────

function usePoll(url, intervalMs = 10000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      fetch(url)
        .then(r => r.json())
        .then(d => { if (alive) setData(d) })
        .catch(e => { if (alive) setError(e.message) })

    load()
    const id = setInterval(load, intervalMs)
    return () => { alive = false; clearInterval(id) }
  }, [url, intervalMs])

  return { data, error }
}

function fmt(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function statusColor(code) {
  if (!code) return '#94a3b8'
  if (code >= 500) return '#f87171'
  if (code >= 400) return '#fb923c'
  return '#4ade80'
}

function sevColor(sev) {
  const map = { 1: '#f87171', 2: '#fb923c', 3: '#facc15', 4: '#4ade80' }
  return map[sev] || '#94a3b8'
}

function sevLabel(sev) {
  const map = { 1: 'SEV-1 CRITICAL', 2: 'SEV-2 HIGH', 3: 'SEV-3 MEDIUM', 4: 'SEV-4 LOW' }
  return map[sev] || '—'
}

function eventColor(type) {
  const map = {
    RUN_START:    '#60a5fa',
    TOOL_CALL:    '#a78bfa',
    TOOL_RESULT:  '#34d399',
    TOOL_ERROR:   '#f87171',
    FINAL_REPORT: '#facc15',
  }
  return map[type] || '#94a3b8'
}

function alertTypeColor(type) {
  const map = { ONCALL: '#f87171', EMAIL: '#60a5fa', RAISE_ALERT: '#fb923c' }
  return map[type] || '#94a3b8'
}

// ─── styles ─────────────────────────────────────────────────────────────────

const s = {
  page: { padding: '16px', minHeight: '100vh' },

  header: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    marginBottom: '20px',
  },
  title: { fontSize: '22px', fontWeight: 700, color: '#f1f5f9', letterSpacing: '-0.5px' },
  subtitle: { fontSize: '12px', color: '#64748b', marginTop: '2px' },
  pulse: {
    display: 'flex', alignItems: 'center', gap: '6px',
    fontSize: '12px', color: '#4ade80',
  },

  cards: { display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '12px', marginBottom: '20px' },
  card: {
    background: '#1e2230', borderRadius: '10px', padding: '14px',
    border: '1px solid #2d3348',
  },
  cardLabel: { fontSize: '11px', color: '#64748b', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' },
  cardValue: { fontSize: '26px', fontWeight: 700 },

  grid: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' },
  grid3: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px' },

  panel: {
    background: '#1e2230', borderRadius: '10px',
    border: '1px solid #2d3348', overflow: 'hidden',
  },
  panelHead: {
    padding: '12px 16px', borderBottom: '1px solid #2d3348',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  panelTitle: { fontSize: '13px', fontWeight: 600, color: '#f1f5f9' },
  panelCount: { fontSize: '11px', color: '#64748b' },
  panelBody: { overflowY: 'auto', maxHeight: '340px' },

  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    padding: '8px 12px', fontSize: '10px', textTransform: 'uppercase',
    color: '#64748b', letterSpacing: '0.05em', textAlign: 'left',
    borderBottom: '1px solid #2d3348', position: 'sticky', top: 0,
    background: '#1e2230',
  },
  td: { padding: '7px 12px', fontSize: '12px', borderBottom: '1px solid #1a1f2e' },

  badge: (color) => ({
    display: 'inline-block', padding: '2px 7px', borderRadius: '4px',
    fontSize: '10px', fontWeight: 600, background: color + '22', color,
  }),

  tag: (color) => ({
    display: 'inline-block', padding: '1px 6px', borderRadius: '3px',
    fontSize: '10px', fontWeight: 500, background: color + '33', color,
  }),

  empty: { padding: '32px', textAlign: 'center', color: '#475569', fontSize: '13px' },
}

// ─── components ─────────────────────────────────────────────────────────────

function StatCard({ label, value, color }) {
  return (
    <div style={s.card}>
      <div style={s.cardLabel}>{label}</div>
      <div style={{ ...s.cardValue, color: color || '#f1f5f9' }}>{value ?? '—'}</div>
    </div>
  )
}

function LogsTable({ data }) {
  if (!data?.length) return <div style={s.empty}>No logs yet</div>
  return (
    <table style={s.table}>
      <thead>
        <tr>
          {['Time', 'Method', 'Endpoint', 'Status', 'Latency', 'Level'].map(h => (
            <th key={h} style={s.th}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map(r => (
          <tr key={r.id}>
            <td style={{ ...s.td, color: '#64748b', whiteSpace: 'nowrap' }}>{fmt(r.timestamp)}</td>
            <td style={s.td}>
              <span style={s.tag('#94a3b8')}>{r.method}</span>
            </td>
            <td style={{ ...s.td, fontFamily: 'monospace', fontSize: '11px', color: '#cbd5e1' }}>
              {r.endpoint}
            </td>
            <td style={s.td}>
              <span style={s.badge(statusColor(r.status_code))}>{r.status_code}</span>
            </td>
            <td style={{ ...s.td, color: r.response_time_ms > 1000 ? '#fb923c' : '#94a3b8' }}>
              {r.response_time_ms ? `${Math.round(r.response_time_ms)}ms` : '—'}
            </td>
            <td style={s.td}>
              <span style={s.badge(statusColor(r.status_code >= 500 ? 500 : r.status_code >= 400 ? 400 : 200))}>
                {r.log_level}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function MetricsTable({ data }) {
  if (!data?.length) return <div style={s.empty}>No agent runs yet — run python agent.py</div>
  return (
    <table style={s.table}>
      <thead>
        <tr>
          {['Time', 'Window', 'Requests', 'Errors', 'Error %', 'Avg Latency', 'Severity'].map(h => (
            <th key={h} style={s.th}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map(r => (
          <tr key={r.id}>
            <td style={{ ...s.td, color: '#64748b', whiteSpace: 'nowrap' }}>{fmt(r.timestamp)}</td>
            <td style={{ ...s.td, color: '#94a3b8' }}>{r.window_minutes}m</td>
            <td style={s.td}>{r.total_requests}</td>
            <td style={{ ...s.td, color: '#f87171' }}>{r.error_count}</td>
            <td style={s.td}>
              <span style={s.badge(r.error_rate > 30 ? '#f87171' : r.error_rate > 15 ? '#fb923c' : '#4ade80')}>
                {r.error_rate?.toFixed(1)}%
              </span>
            </td>
            <td style={{ ...s.td, color: '#94a3b8' }}>{r.avg_response_time_ms?.toFixed(0)}ms</td>
            <td style={s.td}>
              <span style={s.badge(sevColor(r.severity_assessment))}>
                {sevLabel(r.severity_assessment)}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function AlertsTable({ data }) {
  if (!data?.length) return <div style={s.empty}>No alerts yet</div>
  return (
    <table style={s.table}>
      <thead>
        <tr>
          {['Time', 'Type', 'Severity', 'Service', 'Message'].map(h => (
            <th key={h} style={s.th}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map(r => (
          <tr key={r.id}>
            <td style={{ ...s.td, color: '#64748b', whiteSpace: 'nowrap' }}>{fmt(r.timestamp)}</td>
            <td style={s.td}>
              <span style={s.badge(alertTypeColor(r.alert_type))}>{r.alert_type}</span>
            </td>
            <td style={s.td}>
              <span style={s.badge(sevColor(r.severity))}>{sevLabel(r.severity)}</span>
            </td>
            <td style={{ ...s.td, color: '#cbd5e1' }}>{r.service}</td>
            <td style={{ ...s.td, color: '#94a3b8', fontSize: '11px' }}>
              {r.message?.slice(0, 80)}{r.message?.length > 80 ? '…' : ''}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function AgentRunsTable({ data }) {
  if (!data?.length) return <div style={s.empty}>No agent activity yet — run python agent.py</div>
  return (
    <table style={s.table}>
      <thead>
        <tr>
          {['Time', 'Run ID', 'Event', 'Tool', 'Message'].map(h => (
            <th key={h} style={s.th}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map(r => (
          <tr key={r.id}>
            <td style={{ ...s.td, color: '#64748b', whiteSpace: 'nowrap' }}>{fmt(r.timestamp)}</td>
            <td style={{ ...s.td, fontFamily: 'monospace', fontSize: '11px', color: '#64748b' }}>
              {r.run_id}
            </td>
            <td style={s.td}>
              <span style={s.badge(eventColor(r.event_type))}>{r.event_type}</span>
            </td>
            <td style={{ ...s.td, fontFamily: 'monospace', fontSize: '11px', color: '#a78bfa' }}>
              {r.tool_name || '—'}
            </td>
            <td style={{ ...s.td, color: '#94a3b8', fontSize: '11px' }}>
              {r.message?.slice(0, 100)}{r.message?.length > 100 ? '…' : ''}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ─── main app ────────────────────────────────────────────────────────────────

export default function App() {
  const [tick, setTick] = useState(0)
  const [lastUpdated, setLastUpdated] = useState(new Date())

  const { data: summary } = usePoll(`${API}/dashboard/summary`, 10000)
  const { data: logs }    = usePoll(`${API}/dashboard/logs?limit=80&minutes=60`, 10000)
  const { data: metrics } = usePoll(`${API}/dashboard/metrics?limit=20`, 10000)
  const { data: alerts }  = usePoll(`${API}/dashboard/alerts?limit=40`, 10000)
  const { data: runs }    = usePoll(`${API}/dashboard/agent-runs?limit=60`, 10000)

  useEffect(() => {
    const id = setInterval(() => {
      setTick(t => t + 1)
      setLastUpdated(new Date())
    }, 10000)
    return () => clearInterval(id)
  }, [])

  const sev = summary?.latest_severity
  const sevBg = sev ? sevColor(sev) + '18' : '#1e2230'
  const oncallPages = summary?.oncall_pages_total || 0

  return (
    <div style={s.page}>

      {/* Header */}
      <div style={s.header}>
        <div>
          <div style={s.title}>Observability Dashboard</div>
          <div style={s.subtitle}>
            Gemini 2.0 Flash-Lite Agent · PostgreSQL · FastAPI · auto-refresh every 10s
          </div>
        </div>
        <div style={s.pulse}>
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#4ade80', display: 'inline-block' }} />
          Live · updated {lastUpdated.toLocaleTimeString()}
        </div>
      </div>

      {/* Summary cards */}
      <div style={s.cards}>
        <StatCard label="Requests (30m)"  value={summary?.total_requests_30m ?? '…'} />
        <StatCard label="Errors (30m)"    value={summary?.total_errors_30m ?? '…'}   color="#f87171" />
        <StatCard label="Error Rate"
          value={summary?.error_rate_pct != null ? `${summary.error_rate_pct}%` : '…'}
          color={summary?.error_rate_pct > 30 ? '#f87171' : summary?.error_rate_pct > 15 ? '#fb923c' : '#4ade80'}
        />
        <StatCard label="Avg Latency"
          value={summary?.avg_latency_ms != null ? `${summary.avg_latency_ms}ms` : '…'}
          color={summary?.avg_latency_ms > 1000 ? '#fb923c' : '#f1f5f9'}
        />
        <div style={{ ...s.card, background: sevBg, border: `1px solid ${sev ? sevColor(sev) + '44' : '#2d3348'}` }}>
          <div style={s.cardLabel}>Last Severity</div>
          <div style={{ ...s.cardValue, fontSize: '18px', color: sev ? sevColor(sev) : '#64748b' }}>
            {sev ? sevLabel(sev) : 'No runs yet'}
          </div>
        </div>
        <div style={{ ...s.card, background: oncallPages > 0 ? '#f8717118' : '#1e2230', border: `1px solid ${oncallPages > 0 ? '#f8717144' : '#2d3348'}` }}>
          <div style={s.cardLabel}>On-Call Pages</div>
          <div style={{ ...s.cardValue, color: oncallPages > 0 ? '#f87171' : '#f1f5f9' }}>{oncallPages}</div>
        </div>
      </div>

      {/* Logs + Alerts */}
      <div style={s.grid}>
        <div style={s.panel}>
          <div style={s.panelHead}>
            <span style={s.panelTitle}>Live Request Logs</span>
            <span style={s.panelCount}>{logs?.length ?? 0} rows</span>
          </div>
          <div style={s.panelBody}><LogsTable data={logs} /></div>
        </div>

        <div style={s.panel}>
          <div style={s.panelHead}>
            <span style={s.panelTitle}>Alerts</span>
            <span style={s.panelCount}>{alerts?.length ?? 0} rows</span>
          </div>
          <div style={s.panelBody}><AlertsTable data={alerts} /></div>
        </div>
      </div>

      {/* Metrics + Agent Activity */}
      <div style={s.grid}>
        <div style={s.panel}>
          <div style={s.panelHead}>
            <span style={s.panelTitle}>Agent Analysis Runs</span>
            <span style={s.panelCount}>{metrics?.length ?? 0} runs</span>
          </div>
          <div style={s.panelBody}><MetricsTable data={metrics} /></div>
        </div>

        <div style={s.panel}>
          <div style={s.panelHead}>
            <span style={s.panelTitle}>Agent Activity Log</span>
            <span style={s.panelCount}>{runs?.length ?? 0} events</span>
          </div>
          <div style={s.panelBody}><AgentRunsTable data={runs} /></div>
        </div>
      </div>

    </div>
  )
}