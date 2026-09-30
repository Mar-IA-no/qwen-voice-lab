import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { ScoreCompositionEditor } from './ScoreCompositionEditor'
import { addScoreBlock, changeScoreDraft, moveScoreBlock, redoScoreDraft, sameScoreDraft, scoreDraft, scoreDraftBlocks, scoreDraftExport, scoreTimeline, scoreUndoState, undoScoreDraft, validScoreDraft } from './score-draft'
import type { ScoreDraft, ScoreWorkspaceBlock, ScoreWorkspaceDetail } from './types'

function source(key: string, role: 'main' | 'variant' = 'main'): ScoreWorkspaceBlock {
  return {
    source_key: key, role, stage_id: role, stage_title: role === 'main' ? 'Etapa principal' : 'Camino alternativo', order: 0, text: `Texto de ${key}.`, pause_after_ms: 2000,
    speech_speed: 1.25, baseline_speed: 0.98, duration_seconds: 9.8, status: 'ready', unavailable_reason: null, response_marker: null,
    source: { project_id: 'fixture-project', revision_id: 'editorial-fixed', revision_sha256: 'a'.repeat(64), revision_snapshot_sha256: 'b'.repeat(64), segment_id: key, take_id: `take-${key}`, text_sha256: 'c'.repeat(64), audio_sha256: 'd'.repeat(64), take_revision_id: 'earlier', validation_status: 'needs_review', selection_override_reason: 'Motivo conservado', provenance: { nested: { origin: 'fixture' } }, take_provenance: {} },
  }
}
const sources = [source('a'), { ...source('b'), baseline_speed: 1, speech_speed: 1, duration_seconds: 3 }, source('variant', 'variant')]
const detail: ScoreWorkspaceDetail = { workspace: { id: 'fixture', title: 'Partitura de ejemplo', is_default: true, status: 'ready', main_block_count: 2, variant_block_count: 1 }, catalog_sha256: 'e'.repeat(64), catalog_blocks: sources, revision: { id: 'fixed', number: 2, lead_in_ms: 10000, blocks: sources.slice(0, 2), beacon: null } }

describe('recoverable score composition', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('copies only order keys and pauses from the composition, excluding unselected variants and source replacements', () => {
    const draft = scoreDraft(detail)
    expect(draft).toEqual({ lead_in_ms: 10000, blocks: [{ source_key: 'a', pause_after_ms: 2000 }, { source_key: 'b', pause_after_ms: 2000 }] })
    expect(Object.keys(draft.blocks[0])).toEqual(['source_key', 'pause_after_ms'])
    const expanded = scoreDraftBlocks(moveScoreBlock(draft, 0, 1), sources)
    expect(expanded.map((block) => block.source_key)).toEqual(['b', 'a'])
    expect(expanded[1].source).toEqual(sources[0].source)
    expect(expanded[1].text).toBe(sources[0].text)
  })

  it('includes variants only by explicit add, prevents duplicates, and supports removing and re-adding source blocks', () => {
    const draft = scoreDraft(detail)
    const added = addScoreBlock(draft, sources[2])
    expect(added.blocks.map((block) => block.source_key)).toEqual(['a', 'b', 'variant'])
    expect(addScoreBlock(added, sources[2])).toBe(added)
    expect(draft.blocks).toHaveLength(2)
    const removed = { ...added, blocks: added.blocks.filter((block) => block.source_key !== 'a') }
    expect(addScoreBlock(removed, sources[0]).blocks.map((block) => block.source_key)).toEqual(['b', 'variant', 'a'])
    expect(moveScoreBlock(draft, 0, -1)).toBe(draft)
    expect(moveScoreBlock(draft, 1, 1)).toBe(draft)
  })

  it('validates integer milliseconds from 0 through 60 seconds, membership and unique selection', () => {
    const valid = scoreDraft(detail)
    expect(validScoreDraft(valid, sources)).toBe(true)
    expect(validScoreDraft({ ...valid, lead_in_ms: 60000 }, sources)).toBe(true)
    for (const invalid of [NaN, Infinity, -1, 60001, 0.5]) expect(validScoreDraft({ ...valid, lead_in_ms: invalid }, sources)).toBe(false)
    expect(validScoreDraft({ ...valid, blocks: [{ source_key: 'foreign', pause_after_ms: 0 }] }, sources)).toBe(false)
    expect(validScoreDraft({ ...valid, blocks: [...valid.blocks, valid.blocks[0]] }, sources)).toBe(false)
    expect(validScoreDraft({ ...valid, blocks: [] }, sources)).toBe(false)
    expect(validScoreDraft({ ...valid, blocks: [{ source_key: 'a', pause_after_ms: 60001 }] }, sources)).toBe(false)
  })

  it('undoes and redoes timing, reordering and variant inclusion, and clears redo after a new edit', () => {
    const draft = scoreDraft(detail)
    const initial = scoreUndoState(draft)
    const changed = changeScoreDraft(initial, { ...draft, lead_in_ms: 3000 })
    const moved = changeScoreDraft(changed, moveScoreBlock(changed.current, 0, 1))
    const added = changeScoreDraft(moved, addScoreBlock(moved.current, sources[2]))
    expect(undoScoreDraft(added).current).toEqual(moved.current)
    expect(redoScoreDraft(undoScoreDraft(added)).current).toEqual(added.current)
    expect(undoScoreDraft(undoScoreDraft(undoScoreDraft(added))).current).toEqual(draft)
    const branched = changeScoreDraft(undoScoreDraft(added), { ...moved.current, lead_in_ms: 5000 })
    expect(branched.future).toEqual([])
    expect(sameScoreDraft(initial.current, draft)).toBe(true)
    expect(changeScoreDraft(initial, { ...draft })).toBe(initial)
  })

  it('calculates approximate voice and silence positions with source baseline applied once', () => {
    const timeline = scoreTimeline(scoreDraft(detail), sources)
    expect(timeline.rows[0].startSeconds).toBe(10)
    expect(timeline.rows[0].voiceSeconds).toBeCloseTo(8)
    expect(timeline.rows[0].endSeconds).toBeCloseTo(20)
    expect(timeline.rows[1].startSeconds).toBeCloseTo(20)
    expect(timeline.durationSeconds).toBeCloseTo(25)
    const unknown = scoreTimeline(scoreDraft(detail), [{ ...sources[0], duration_seconds: null }, sources[1]])
    expect(unknown.rows[0].voiceSeconds).toBeNull()
    expect(unknown.rows[1].startSeconds).toBeNull()
    expect(unknown.durationSeconds).toBeNull()
  })

  it('exports the original expected revision and local changes without altering fixed sources', () => {
    const draft = { ...scoreDraft(detail), lead_in_ms: 8000 }
    const payload = JSON.parse(scoreDraftExport(detail, draft))
    expect(payload.expected_revision_id).toBe('fixed')
    expect(payload.catalog_sha256).toBe(detail.catalog_sha256)
    expect(payload.lead_in_ms).toBe(8000)
    expect(payload.blocks).toEqual(draft.blocks)
    expect(payload.blocks[0].source).toBeUndefined()
    expect(detail.revision!.lead_in_ms).toBe(10000)
    expect(sources[0].source.selection_override_reason).toBe('Motivo conservado')
  })

  it('renders one selected script and explicit inactive variants, editing and approximate timing without audio or GPU actions', () => {
    const html = renderToStaticMarkup(createElement(ScoreCompositionEditor, { detail, onSaved: () => {}, onState: () => {} }))
    const active = html.slice(0, html.indexOf('class="score-outside"'))
    expect(active.match(/class="score-spoken-text"/g)).toHaveLength(2)
    expect(html.match(/class="score-spoken-text"/g)).toHaveLength(3)
    expect(html).toContain('Silencio inicial en segundos')
    expect(html).toContain('Pausa después del bloque 1 en segundos')
    expect(html).toContain('Guardar versión')
    expect(html).toContain('Deshacer')
    expect(html).toContain('Agregar al recorrido')
    expect(html).toContain('Posiciones aproximadas')
    expect(html).not.toMatch(/<audio|<textarea|Generar|Final validado|Otra toma/)
  })

  it('retains HTTP conflict status and submits only composition fields to additive save/restore APIs', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Another writer saved.' }), { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    const draft: ScoreDraft = { lead_in_ms: 2000, blocks: [{ source_key: 'a', pause_after_ms: 4000 }] }
    try { await api.saveScoreRevision('fixture', { ...draft, expected_revision_id: 'fixed' }); expect.fail('Expected conflict') }
    catch (reason) { expect(reason).toBeInstanceOf(ApiError); expect((reason as ApiError).status).toBe(409) }
    expect(fetch.mock.calls[0][0]).toBe('/api/score-workspaces/fixture/revisions')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ ...draft, expected_revision_id: 'fixed' })
    fetch.mockResolvedValueOnce(new Response(JSON.stringify(detail), { status: 200 }))
    await api.restoreScoreRevision('fixture', 'older', 'fixed')
    expect(fetch.mock.calls[1][0]).toBe('/api/score-workspaces/fixture/restore')
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ revision_id: 'older', expected_revision_id: 'fixed' })
  })
})
