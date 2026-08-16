export type Language = 'es' | 'en' | 'pt' | 'fr' | 'it' | 'de'
export type Prosody = 'neutral' | 'T' | 'S' | 'D' | 'R'
export type JobStatus = 'queued' | 'running' | 'complete' | 'failed' | 'cancelled'

export interface AuthStatus {
  required: boolean
  authenticated: boolean
}

export interface Capabilities {
  engine: string
  engine_ready: boolean
  engine_reason?: string | null
  base_model: string
  design_model: string
  languages: string[]
  max_upload_mib: number
  max_text_chars: number
  max_segments: number
  max_comparison_voices: number
  voice_design: boolean
  voice_cloning: boolean
  paid_providers: string[]
  gpu_wrapper_required: boolean
  gpu_wrapper_verified: boolean
  gpu_execution_mode: 'in-process' | 'wrapped-worker'
  gpu_worker_state: string
  gpu_worker_reason?: string | null
}

export interface ArchiveAsset {
  id: string
  name: string
  relative_path: string
  collection: string
  kind: 'source' | 'reference' | 'segment' | 'locution' | 'experiment' | 'audio'
  format: string
  size_bytes: number
  canonical: boolean
}

export interface Voice {
  id: string
  name: string
  description: string
  kind: 'clone' | 'designed'
  language_hint: Language | 'multilingual'
  reference_text: string
  reference_file: string
  reference_sha256: string
  duration_seconds?: number | null
  tags: string[]
  created_at: string
  design_instruction?: string | null
  prosody_profile?: {
    id: string
    status: 'experimental' | 'canonical'
    functions: Prosody[]
    notes: string[]
  } | null
}

export interface Segment {
  id: string
  text: string
  pause_after_ms: number
  prosody: Prosody
}

export interface JobMetrics {
  model: string
  device: string
  load_ms: number
  generation_ms: number
  first_audio_ms: number
  duration_seconds: number
  rtf: number
  peak_vram_mib?: number | null
  output_sha256: string
  output_bytes: number
}

export interface Job {
  id: string
  kind: 'synthesis' | 'design'
  status: JobStatus
  title: string
  progress: number
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  request: Record<string, unknown>
  output_file?: string | null
  result_voice_id?: string | null
  metrics?: JobMetrics | null
  error?: string | null
}

export interface Comparison {
  id: string
  title: string
  voice_ids: string[]
  job_ids: string[]
  language: Language
  text: string
  seed: number
  created_at: string
}
