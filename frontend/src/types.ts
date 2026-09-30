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
  codex_chat_url?: string | null
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
  speed?: number | null
  selected_take_id?: string | null
  provenance?: Record<string, unknown>
}

export interface WorkshopBlock {
  id: string
  text: string
  pause_after_ms: number
  speed: number | null
  selected_take_id?: string | null
  provenance?: Record<string, unknown>
}

export interface BeaconSettings {
  enabled: boolean
  asset_id: string | null
  offset_seconds: number
  volume: number
}

export interface SourceRevision {
  id: string
  project_id: string
  number: number
  markdown: string
  source_sha256: string
  blocks: WorkshopBlock[]
  lead_pause_ms: number
  speech_speed: number
  beacon: BeaconSettings
  created_at: string
}

export interface Project {
  id: string
  title: string
  voice_id: string
  language: Language
  project_seed: number
  sampling: SamplingSettings
  baseline_speed?: number
  provenance?: Record<string, unknown>
  status: 'draft' | 'generating' | 'needs_review' | 'ready'
  current_revision_id?: string | null
  handoff?: {
    revision_id: string
    revision_number: number
    source_sha256: string
    selected_take_ids: Record<string, string>
    marked_at: string
  } | null
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
  baseline_speed?: number
  provenance?: Record<string, unknown>
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
  segment_id?: string | null
  duration_seconds: number
  audit_status: 'pending' | 'pass' | 'review' | 'overridden' | 'unavailable'
  audit: Record<string, unknown>
  created_at: string
}

export interface ScoreWorkspaceSummary {
  id: string
  title: string
  is_default: boolean
  status: 'ready' | 'unavailable'
  main_block_count: number
  variant_block_count: number
  source_project_ids?: string[]
}

export interface ScoreWorkspaceCatalog {
  workspaces: ScoreWorkspaceSummary[]
  editorial_mode: boolean
  default_workspace_id: string | null
}

export interface ScoreSource {
  project_id: string
  revision_id: string
  revision_sha256: string
  revision_snapshot_sha256: string
  segment_id: string
  take_id: string
  text_sha256: string
  audio_sha256: string
  take_revision_id: string | null
  validation_status: string | null
  selection_override_reason: string | null
  provenance: Record<string, unknown>
  take_provenance: Record<string, unknown>
}

export interface ScoreWorkspaceBlock {
  source_key: string
  role: 'main' | 'variant'
  stage_id: string
  stage_title: string
  order: number
  text: string
  pause_after_ms: number
  speech_speed: number
  baseline_speed: number | null
  duration_seconds: number | null
  status: 'ready' | 'unavailable'
  unavailable_reason: string | null
  source: ScoreSource
  response_marker: string | null
}

export interface ScoreWorkspaceDetail {
  workspace: ScoreWorkspaceSummary
  catalog_sha256: string
  unavailable_reason?: string | null
  catalog_blocks?: ScoreWorkspaceBlock[]
  revision: {
    id: string
    number: number
    created_at?: string | null
    kind?: string
    includes_variants?: boolean
    restored_from_revision_id?: string | null
    lead_in_ms: number
    blocks: ScoreWorkspaceBlock[]
    beacon: Record<string, unknown> | null
  } | null
}

export interface ScoreDraft {
  lead_in_ms: number
  blocks: { source_key: string; pause_after_ms: number }[]
}

export interface ScoreRevisionSummary {
  id: string
  number: number
  created_at: string | null
  kind?: string
  includes_variants?: boolean
  restored_from_revision_id: string | null
  block_count: number
  lead_in_ms: number
}

export interface ScorePreview {
  id: string
  workspace_id: string
  revision_id: string
  source_keys: string[]
  selection_mode: 'full' | 'selection'
  catalog_sha256: string
  audio_url: string
  download_url: string
  manifest_url: string
  duration_seconds: number
  sample_rate: number
  total_samples: number
  timeline: {
    source_key: string
    start_sample: number
    voice_end_sample: number
    end_sample: number
    pause_samples: number
  }[]
  beacon: {
    enabled: boolean
    offset_seconds: number
    volume: number
    status: 'ready' | 'unavailable'
    audio_url: string
  } | null
}
