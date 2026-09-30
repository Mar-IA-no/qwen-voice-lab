import type { ArchiveAsset, Assembly, AuthStatus, BeaconSettings, Capabilities, Comparison, Job, Language, Project, ProjectDetail, ProjectRun, SamplingSettings, Segment, SourceRevision, Take, Voice, WorkshopBlock, ScoreWorkspaceCatalog, ScoreWorkspaceDetail, ScoreDraft, ScoreRevisionSummary, ScorePreview } from './types'

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); this.name = 'ApiError' }
}

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
    throw new ApiError(response.status, message)
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
  scoreWorkspaces: () => request<ScoreWorkspaceCatalog>('/api/score-workspaces'),
  scoreWorkspace: (id: string) => request<ScoreWorkspaceDetail>(`/api/score-workspaces/${encodeURIComponent(id)}`),
  scoreRevisions: (id: string) => request<ScoreRevisionSummary[]>(`/api/score-workspaces/${encodeURIComponent(id)}/revisions`),
  saveScoreRevision: (id: string, payload: ScoreDraft & { expected_revision_id: string }) => request<ScoreWorkspaceDetail>(`/api/score-workspaces/${encodeURIComponent(id)}/revisions`, jsonPost(payload)),
  restoreScoreRevision: (id: string, revision_id: string, expected_revision_id: string) => request<ScoreWorkspaceDetail>(`/api/score-workspaces/${encodeURIComponent(id)}/restore`, jsonPost({ revision_id, expected_revision_id })),
  previewScore: (id: string, revision_id: string, source_keys?: string[]) => request<ScorePreview>(`/api/score-workspaces/${encodeURIComponent(id)}/preview`, jsonPost({ revision_id, ...(source_keys ? { source_keys } : {}) })),
  projects: () => request<Project[]>('/api/projects'),
  project: (id: string) => request<ProjectDetail>(`/api/projects/${id}`),
  markHandoff: (id: string, expected_revision_id: string) => request<ProjectDetail>(`/api/projects/${id}/handoff`, jsonPost({ expected_revision_id })),
  createProject: (payload: { title: string; voice_id: string; language: Language; blocks?: WorkshopBlock[]; markdown?: string; lead_pause_ms?: number; speech_speed?: number; beacon?: BeaconSettings; project_seed: number; sampling: SamplingSettings }) => request<ProjectDetail>('/api/projects', jsonPost(payload)),
  reviseProject: (id: string, payload: { expected_revision_id: string; blocks: WorkshopBlock[]; lead_pause_ms: number; speech_speed: number; beacon: BeaconSettings }) => request<ProjectDetail>(`/api/projects/${id}/revisions`, jsonPost(payload)),
  projectRevisions: (id: string) => request<SourceRevision[]>(`/api/projects/${id}/revisions`),
  restoreRevision: (id: string, revision_id: string, expected_revision_id: string) => request<ProjectDetail>(`/api/projects/${id}/restore`, jsonPost({ revision_id, expected_revision_id })),
  runProject: (id: string, expected_revision_id: string) => request<ProjectRun>(`/api/projects/${id}/runs`, jsonPost({ expected_revision_id })),
  projectRuns: (id: string) => request<ProjectRun[]>(`/api/projects/${id}/runs`),
  projectTakes: (id: string, segmentId: string) => request<Take[]>(`/api/projects/${id}/segments/${segmentId}/takes`),
  generateTake: (id: string, segmentId: string, expected_revision_id: string) => request<ProjectRun>(`/api/projects/${id}/segments/${segmentId}/takes`, jsonPost({ expected_revision_id })),
  selectTake: (id: string, segmentId: string, takeId: string, expected_revision_id: string, override = false, reason?: string) => request<ProjectDetail>(`/api/projects/${id}/segments/${segmentId}/takes/${takeId}/select`, jsonPost({ expected_revision_id, override, reason })),
  previewProject: (id: string, revision_id: string, segment_id?: string) => request<Assembly>(`/api/projects/${id}/preview`, jsonPost({ revision_id, segment_id })),
  assembleProject: (id: string, revision_id: string, override_reason?: string) => request<Assembly>(`/api/projects/${id}/assemblies`, jsonPost({ revision_id, override_reason })),
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
