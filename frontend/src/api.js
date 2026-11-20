async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  visitors: () => req('/api/visitors'),
  sessions: (visitorId) =>
    req(visitorId ? `/api/sessions?visitor_id=${encodeURIComponent(visitorId)}` : '/api/sessions'),
  session: (id) => req(`/api/sessions/${encodeURIComponent(id)}`),
  quarantine: () => req('/api/quarantine'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (sessionId) =>
    req(sessionId ? `/api/session-proofs?session_id=${encodeURIComponent(sessionId)}` : '/api/session-proofs'),
  settings: () => req('/api/settings'),
  dayBoundaries: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.visitorId) qs.set('visitor_id', params.visitorId)
    if (params.sessionId) qs.set('session_id', params.sessionId)
    const tail = qs.toString()
    return req(tail ? `/api/day-boundaries?${tail}` : '/api/day-boundaries')
  },
  saveCalendarTimezone: (tz) =>
    req('/api/settings/calendar-timezone', {
      method: 'PUT',
      body: JSON.stringify({ calendar_timezone: tz }),
    }),
}
