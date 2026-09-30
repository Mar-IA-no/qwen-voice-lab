import type { BeaconSettings, ProjectDetail, WorkshopBlock } from './types'

export function revisionDraft(detail: ProjectDetail): { blocks: WorkshopBlock[]; lead_pause_ms: number; speech_speed: number; beacon: BeaconSettings } {
  const revision = detail.revision
  return {
    blocks: (revision?.blocks?.length ? revision.blocks : detail.segments).map((row) => ({
      id: row.id, text: row.text, pause_after_ms: row.pause_after_ms, speed: row.speed ?? null,
      selected_take_id: row.selected_take_id ?? null,
      provenance: structuredClone(row.provenance ?? {}),
    })),
    lead_pause_ms: revision?.lead_pause_ms ?? 0,
    speech_speed: revision?.speech_speed ?? 1,
    beacon: revision?.beacon ? { ...revision.beacon } : { enabled: false, asset_id: null, offset_seconds: 0, volume: 0.25 },
  }
}
