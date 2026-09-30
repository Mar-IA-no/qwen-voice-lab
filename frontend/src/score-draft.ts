import type { ScoreDraft, ScoreWorkspaceBlock, ScoreWorkspaceDetail } from './types'

export function scoreDraft(detail: ScoreWorkspaceDetail): ScoreDraft {
  return { lead_in_ms: detail.revision?.lead_in_ms ?? 0, blocks: (detail.revision?.blocks ?? []).map(({ source_key, pause_after_ms }) => ({ source_key, pause_after_ms })) }
}

export function sameScoreDraft(a: ScoreDraft, b: ScoreDraft) { return JSON.stringify(a) === JSON.stringify(b) }

export function validScoreDraft(draft: ScoreDraft, sources: ScoreWorkspaceBlock[]) {
  const keys = new Set(sources.map((block) => block.source_key))
  const validPause = (value: number) => Number.isSafeInteger(value) && value >= 0 && value <= 60000
  return validPause(draft.lead_in_ms) && draft.blocks.length > 0 && draft.blocks.length <= 256 &&
    new Set(draft.blocks.map((block) => block.source_key)).size === draft.blocks.length &&
    draft.blocks.every((block) => keys.has(block.source_key) && validPause(block.pause_after_ms))
}

export function scoreDraftBlocks(draft: ScoreDraft, sources: ScoreWorkspaceBlock[]) {
  const byKey = new Map(sources.map((source) => [source.source_key, source]))
  return draft.blocks.map((block, order) => {
    const source = byKey.get(block.source_key)
    if (!source) throw new Error('El borrador contiene una fuente fuera de la colección fijada.')
    return { ...source, ...block, order }
  })
}

export function moveScoreBlock(draft: ScoreDraft, index: number, step: number): ScoreDraft {
  if (index < 0 || index >= draft.blocks.length || index + step < 0 || index + step >= draft.blocks.length) return draft
  const blocks = [...draft.blocks]
  const [block] = blocks.splice(index, 1)
  blocks.splice(index + step, 0, block)
  return { ...draft, blocks }
}

export function addScoreBlock(draft: ScoreDraft, source: ScoreWorkspaceBlock): ScoreDraft {
  return draft.blocks.some((block) => block.source_key === source.source_key) ? draft :
    { ...draft, blocks: [...draft.blocks, { source_key: source.source_key, pause_after_ms: source.pause_after_ms }] }
}

export function scoreTimeline(draft: ScoreDraft, sources: ScoreWorkspaceBlock[]) {
  let position: number | null = Number.isFinite(draft.lead_in_ms) ? draft.lead_in_ms / 1000 : null
  const rows = scoreDraftBlocks(draft, sources).map((block) => {
    const speed = block.baseline_speed === null ? null : block.baseline_speed * block.speech_speed
    const voiceSeconds = block.duration_seconds !== null && speed !== null && Number.isFinite(speed) && speed > 0 ? block.duration_seconds / speed : null
    const pauseSeconds = Number.isFinite(block.pause_after_ms) ? block.pause_after_ms / 1000 : null
    const startSeconds: number | null = position
    position = position !== null && voiceSeconds !== null && pauseSeconds !== null ? position + voiceSeconds + pauseSeconds : null
    return { block, startSeconds, voiceSeconds, pauseSeconds, endSeconds: position }
  })
  return { rows, durationSeconds: position }
}

export type ScoreUndoState = { current: ScoreDraft; past: ScoreDraft[]; future: ScoreDraft[] }
export function scoreUndoState(current: ScoreDraft): ScoreUndoState { return { current, past: [], future: [] } }
export function changeScoreDraft(state: ScoreUndoState, current: ScoreDraft): ScoreUndoState {
  return sameScoreDraft(state.current, current) ? state : { current, past: [...state.past.slice(-99), state.current], future: [] }
}
export function undoScoreDraft(state: ScoreUndoState): ScoreUndoState {
  if (!state.past.length) return state
  return { current: state.past[state.past.length - 1], past: state.past.slice(0, -1), future: [state.current, ...state.future] }
}
export function redoScoreDraft(state: ScoreUndoState): ScoreUndoState {
  if (!state.future.length) return state
  return { current: state.future[0], past: [...state.past, state.current], future: state.future.slice(1) }
}

export function scoreDraftExport(detail: ScoreWorkspaceDetail, draft: ScoreDraft) {
  return JSON.stringify({ workspace_id: detail.workspace.id, expected_revision_id: detail.revision?.id, catalog_sha256: detail.catalog_sha256, ...draft }, null, 2)
}
