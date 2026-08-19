import type { ArchiveAsset, Assembly, AuthStatus, Capabilities, Comparison, Job, Language, Project, ProjectDetail, ProjectRun, SamplingSettings, Segment, Take, Voice } from './types'

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
  projects: () => request<Project[]>('/api/projects'),
  project: (id: string) => request<ProjectDetail>(`/api/projects/${id}`),
  createProject: (payload: { title: string; voice_id: string; language: Language; markdown: string; project_seed: number; sampling: SamplingSettings }) => request<ProjectDetail>('/api/projects', jsonPost(payload)),
  reviseProject: (id: string, markdown: string) => request<ProjectDetail>(`/api/projects/${id}/revisions`, jsonPost({ markdown })),
  runProject: (id: string) => request<ProjectRun>(`/api/projects/${id}/runs`, jsonPost({})),
  projectRuns: (id: string) => request<ProjectRun[]>(`/api/projects/${id}/runs`),
  projectTakes: (id: string, segmentId: string) => request<Take[]>(`/api/projects/${id}/segments/${segmentId}/takes`),
  generateTake: (id: string, segmentId: string) => request<ProjectRun>(`/api/projects/${id}/segments/${segmentId}/takes`, jsonPost({})),
  selectTake: (id: string, segmentId: string, takeId: string, override = false, reason?: string) => request<ProjectDetail>(`/api/projects/${id}/segments/${segmentId}/takes/${takeId}/select`, jsonPost({ override, reason })),
  previewProject: (id: string) => request<Assembly>(`/api/projects/${id}/preview`, jsonPost({})),
  assembleProject: (id: string, override_reason?: string) => request<Assembly>(`/api/projects/${id}/assemblies`, jsonPost({ override_reason })),
  projectAssemblies: (id: string) => request<Assembly[]>(`/api/projects/${id}/assemblies`),
  archive: () => request<ArchiveAsset[]>('/api/archive'),
  createVoice: (form: FormData) => request<Voice>('/api/voices', { method: 'POST', body: form }),
  deleteVoice: (id: string) => request<void>(`/api/voices/${id}`, { method: 'DELETE' }),
  design: (payload: {
    name: string
    description: string
    instruction: string
    sample_text: string
    language: Language
    seed: number
  }) => request<Job>('/api/designs', jsonPost(payload)),
  promoteDesign: (jobId: string) => request<Voice>(`/api/jobs/${jobId}/promote`, { method: 'POST' }),
  synthesize: (payload: {
    title: string
    voice_id: string
    language: Language
    segments: Segment[]
    seed: number
  }) => request<Job>('/api/jobs', jsonPost(payload)),
  compare: (payload: {
    title: string
    voice_ids: string[]
    language: Language
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
