import { describe, expect, it } from 'vitest'
import { revisionDraft } from './workshop-draft'
import type { ProjectDetail } from './types'

const source = {
  segments: [{ id: 'block_001', text: 'Primer bloque', pause_after_ms: 0, speed: null, selected_take_id: 'take-a', provenance: { origin: 'fixture', nested: { reviewed: false } } }],
  revision: { id: 'revision-fixed', number: 1, blocks: [
    { id: 'block_001', text: 'Primer bloque', pause_after_ms: 1000, speed: null, selected_take_id: 'take-a', provenance: { origin: 'fixed', nested: { reviewed: false } } },
    { id: 'block_002', text: 'Segundo bloque', pause_after_ms: 2000, speed: 1, selected_take_id: 'take-b', provenance: { origin: 'second' } },
  ], lead_pause_ms: 10000, speech_speed: 1, beacon: { enabled: false, asset_id: null, offset_seconds: 0, volume: 0.25 } },
} as unknown as ProjectDetail

describe('workshop revision provenance', () => {
  it('retains provenance and selected takes through pause, reorder and save payloads without mutating the source', () => {
    const draft = revisionDraft(source)
    const reordered = [...draft.blocks].reverse().map((block) => block.id === 'block_001' ? { ...block, pause_after_ms: 4500 } : block)
    const payload = JSON.parse(JSON.stringify({ ...draft, expected_revision_id: source.revision!.id, blocks: reordered }))
    expect(payload.blocks[1].provenance).toEqual(source.revision!.blocks[0].provenance)
    expect(payload.blocks[1].selected_take_id).toBe('take-a')
    expect(payload.blocks[1].pause_after_ms).toBe(4500)
    ;(draft.blocks[0].provenance!.nested as { reviewed: boolean }).reviewed = true
    expect(source.revision!.blocks[0].provenance!.nested).toEqual({ reviewed: false })
    expect(source.revision!.blocks[0].pause_after_ms).toBe(1000)
  })

  it('preserves provenance when recovering segment-only project details', () => {
    expect(revisionDraft({ ...source, revision: null }).blocks[0].provenance).toEqual(source.segments[0].provenance)
  })
})
