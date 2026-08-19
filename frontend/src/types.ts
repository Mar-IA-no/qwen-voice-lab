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
  long_form_projects: boolean
  local_validator_enabled: boolean
  validator_models: string[]
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
    languages: Language[]
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

export interface SamplingSettings {
  do_sample: boolean
  temperature: number
  top_p: number
  top_k: number
  repetition_penalty: number
  subtalker_dosample: boolean
  subtalker_temperature: number
  subtalker_top_p: number
  subtalker_top_k: number
  max_new_tokens: number
}

export interface ProjectSegment {
  id: string
  project_id: string
  revision_id: string
  position: number
  text: string
  normalized_text: string
  text_sha256: string
  pause_after_ms: number
  selected_take_id?: string | null
}

export interface SourceRevision {
  id: string
  project_id: string
  number: number
  markdown: string
  source_sha256: string
  created_at: string
}

export interface Project {
  id: string
  title: string
  voice_id: string
  language: Language
  project_seed: number
  sampling: SamplingSettings
  status: 'draft' | 'generating' | 'needs_review' | 'ready'
  current_revision_id?: string | null
  created_at: string
  updated_at: string
}

export interface ProjectDetail extends Project {
  revision?: SourceRevision | null
  segments: ProjectSegment[]
}

export interface QualityReport {
  id: string
  take_id: string
  validator: string
  verdict: 'pass' | 'retry' | 'review' | 'unavailable'
  transcript: string
  normalized_transcript: string
  wer?: number | null
  cer?: number | null
  token_coverage?: number | null
  prefix_coverage?: number | null
  suffix_coverage?: number | null
  block_coverages: number[]
  missing_block_indexes: number[]
  leaked_reference_phrases: string[]
  identity_median?: number | null
  identity_min?: number | null
  identity_windows: number[]
  calibration_id?: string | null
  validator_model_sha256?: string | null
  alignment: Array<Record<string, unknown>>
  reasons: string[]
}

export interface Take {
  id: string
  project_id: string
  revision_id: string
  segment_id: string
  attempt: number
  seed: number
  status: 'generated' | 'pass' | 'retry' | 'needs_review' | 'overridden'
  duration_seconds: number
  raw_sha256: string
  trimmed_sha256: string
  trim_start_ms: number
  trim_end_ms: number
  trim_threshold_db: number
  trim_padding_ms: number
  voice_id: string
  voice_reference_sha256: string
  model: string
  text_sha256: string
  sampling: SamplingSettings
  selected: boolean
  override_reason?: string | null
  quality_reports: QualityReport[]
}

export interface ProjectRun {
  id: string
  project_id: string
  revision_id: string
  status: 'queued' | 'running' | 'complete' | 'needs_review' | 'failed'
  progress: number
  error?: string | null
}

export interface Assembly {
  id: string
  project_id: string
  revision_id: string
  kind: 'preview' | 'final'
  duration_seconds: number
  audit_status: 'pending' | 'pass' | 'review' | 'overridden' | 'unavailable'
  audit: Record<string, unknown>
  created_at: string
}
