import type { ScorePreview, ScoreWorkspaceDetail } from './types'

export function selectedScoreKeys(detail: ScoreWorkspaceDetail, keys?: string[]) {
  const ordered = detail.revision?.blocks.map((block) => block.source_key) ?? []
  if (!keys) return ordered
  const included = new Set(keys)
  if (!keys.length || included.size !== keys.length || keys.some((key) => !ordered.includes(key))) throw new Error('Elegí bloques incluidos en el recorrido guardado.')
  return ordered.filter((key) => included.has(key))
}

export function assertScorePreview(preview: ScorePreview, detail: ScoreWorkspaceDetail, requestedKeys?: string[]) {
  const keys = selectedScoreKeys(detail, requestedKeys)
  if (preview.workspace_id !== detail.workspace.id || preview.revision_id !== detail.revision?.id || preview.catalog_sha256 !== detail.catalog_sha256 ||
      preview.selection_mode !== (requestedKeys ? 'selection' : 'full') || JSON.stringify(preview.source_keys) !== JSON.stringify(keys)) throw new Error('La escucha recibida no corresponde a la versión y selección pedidas.')
  if (!Number.isSafeInteger(preview.sample_rate) || preview.sample_rate <= 0 || !Number.isSafeInteger(preview.total_samples) || preview.total_samples <= 0 || preview.timeline.length !== keys.length) throw new Error('La escucha no contiene una línea de tiempo válida.')
  let previous = 0
  preview.timeline.forEach((row, index) => {
    const positions = [row.start_sample, row.voice_end_sample, row.end_sample]
    if (row.source_key !== keys[index] || !positions.every((sample) => Number.isSafeInteger(sample) && sample >= 0) || row.start_sample < previous || (index > 0 && row.start_sample !== previous) || (index === 0 && requestedKeys && row.start_sample !== 0) || row.voice_end_sample < row.start_sample || row.end_sample < row.voice_end_sample || row.end_sample > preview.total_samples || row.pause_samples !== row.end_sample - row.voice_end_sample) throw new Error('La línea de tiempo no corresponde a los bloques de la escucha.')
    previous = row.end_sample
  })
  if (previous !== preview.total_samples) throw new Error('La duración no corresponde a la línea de tiempo de la escucha.')
}

export function playingScoreBlock(preview: ScorePreview, seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds < 0) return null
  const sample = Math.floor(seconds * preview.sample_rate)
  return preview.timeline.find((row) => sample >= row.start_sample && sample < row.end_sample)?.source_key ?? null
}

export function exactScoreRow(preview: ScorePreview, sourceKey: string) {
  const row = preview.timeline.find((entry) => entry.source_key === sourceKey)
  return row ? { startSeconds: row.start_sample / preview.sample_rate, voiceSeconds: (row.voice_end_sample - row.start_sample) / preview.sample_rate, pauseSeconds: (row.end_sample - row.voice_end_sample) / preview.sample_rate } : null
}
