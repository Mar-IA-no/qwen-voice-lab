import type { ArchiveAsset, AuthStatus, Capabilities, Comparison, Job, Segment, Voice } from './types'

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json()
      message = payload.detail ?? message
    } catch {
      // The status line is sufficient when a response is not JSON.
    }
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  authStatus: () => request<AuthStatus>('/api/auth/status'),
  login: (token: string) => request<AuthStatus>('/api/auth/session', jsonPost({ token })),
  logout: () => request<AuthStatus>('/api/auth/session', { method: 'DELETE' }),
  capabilities: () => request<Capabilities>('/api/capabilities'),
  voices: () => request<Voice[]>('/api/voices'),
  jobs: () => request<Job[]>('/api/jobs?limit=100'),
  archive: () => request<ArchiveAsset[]>('/api/archive'),
  createVoice: (form: FormData) => request<Voice>('/api/voices', { method: 'POST', body: form }),
  deleteVoice: (id: string) => request<void>(`/api/voices/${id}`, { method: 'DELETE' }),
  design: (payload: {
    name: string
    description: string
    instruction: string
    sample_text: string
    language: 'es' | 'en'
    seed: number
  }) => request<Job>('/api/designs', jsonPost(payload)),
  promoteDesign: (jobId: string) => request<Voice>(`/api/jobs/${jobId}/promote`, { method: 'POST' }),
  synthesize: (payload: {
    title: string
    voice_id: string
    language: 'es' | 'en'
    segments: Segment[]
    seed: number
  }) => request<Job>('/api/jobs', jsonPost(payload)),
  compare: (payload: {
    title: string
    voice_ids: string[]
    language: 'es' | 'en'
    text: string
    seed: number
  }) => request<Comparison>('/api/comparisons', jsonPost(payload)),
  cancel: (id: string) => request<Job>(`/api/jobs/${id}`, { method: 'DELETE' }),
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}
