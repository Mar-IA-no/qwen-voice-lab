import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import { ScoreListener } from './ScoreListener'
import { assertScorePreview, exactScoreRow, playingScoreBlock, selectedScoreKeys } from './score-playback'
import type { ScorePreview, ScoreWorkspaceDetail } from './types'

const detail = { workspace: { id: 'fixture', status: 'ready' }, catalog_sha256: 'a'.repeat(64), revision: { id: 'revision-fixed', number: 2, lead_in_ms: 1000, blocks: [{ source_key: 'first' }, { source_key: 'second' }, { source_key: 'included-variant', role: 'variant' }] } } as ScoreWorkspaceDetail
const preview: ScorePreview = {
  id: 'preview-fixed', workspace_id: 'fixture', revision_id: 'revision-fixed', catalog_sha256: detail.catalog_sha256,
  selection_mode: 'full', source_keys: ['first', 'second', 'included-variant'], audio_url: '/api/score-workspaces/fixture/previews/preview-fixed/audio', download_url: '/api/score-workspaces/fixture/previews/preview-fixed/download', manifest_url: '/api/score-workspaces/fixture/previews/preview-fixed/manifest',
  duration_seconds: 7, sample_rate: 24000, total_samples: 168000, beacon: null,
  timeline: [
    { source_key: 'first', start_sample: 24000, voice_end_sample: 48000, end_sample: 72000, pause_samples: 24000 },
    { source_key: 'second', start_sample: 72000, voice_end_sample: 96000, end_sample: 120000, pause_samples: 24000 },
    { source_key: 'included-variant', start_sample: 120000, voice_end_sample: 144000, end_sample: 168000, pause_samples: 24000 },
  ],
}

describe('exact saved-score playback', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses saved order for listening selections and rejects duplicate, absent and unselected variant keys', () => {
    expect(selectedScoreKeys(detail, ['included-variant', 'first'])).toEqual(['first', 'included-variant'])
    expect(selectedScoreKeys(detail)).toEqual(preview.source_keys)
    for (const keys of [[], ['first', 'first'], ['foreign'], ['inactive-variant']]) expect(() => selectedScoreKeys(detail, keys)).toThrow()
  })

  it('binds playable responses to workspace, revision, catalog, ordered keys and listening scope', () => {
    expect(() => assertScorePreview(preview, detail)).not.toThrow()
    for (const replacement of [{ revision_id: 'other' }, { workspace_id: 'other' }, { catalog_sha256: 'other' }, { source_keys: [...preview.source_keys].reverse() }, { selection_mode: 'selection' as const }]) expect(() => assertScorePreview({ ...preview, ...replacement }, detail)).toThrow()
    const selection = { ...preview, total_samples: 48000, duration_seconds: 2, source_keys: ['first'], selection_mode: 'selection' as const, timeline: [{ ...preview.timeline[0], start_sample: 0, voice_end_sample: 24000, end_sample: 48000 }] }
    expect(() => assertScorePreview(selection, detail, ['first'])).not.toThrow()
    expect(() => assertScorePreview({ ...selection, timeline: [preview.timeline[0]], total_samples: 72000 }, detail, ['first'])).toThrow()
  })

  it('rejects missing, nonfinite, reversed, overlapping and out-of-range sample timing', () => {
    expect(() => assertScorePreview({ ...preview, sample_rate: 0 }, detail)).toThrow()
    expect(() => assertScorePreview({ ...preview, total_samples: Infinity }, detail)).toThrow()
    expect(() => assertScorePreview({ ...preview, timeline: preview.timeline.slice(1) }, detail)).toThrow()
    for (const replacement of [{ start_sample: NaN }, { voice_end_sample: 1 }, { end_sample: 200000 }, { start_sample: -1 }]) expect(() => assertScorePreview({ ...preview, timeline: [{ ...preview.timeline[0], ...replacement }, ...preview.timeline.slice(1)] }, detail)).toThrow()
    expect(() => assertScorePreview({ ...preview, timeline: [preview.timeline[0], { ...preview.timeline[1], start_sample: 70000 }, preview.timeline[2]] }, detail)).toThrow()
    expect(() => assertScorePreview({ ...preview, total_samples: preview.total_samples + 1 }, detail)).toThrow()
  })

  it('highlights real blocks through voice and post-block silence while leaving lead and completion unassigned', () => {
    expect(playingScoreBlock(preview, 0.9)).toBeNull()
    expect(playingScoreBlock(preview, 1)).toBe('first')
    expect(playingScoreBlock(preview, 2.5)).toBe('first')
    expect(playingScoreBlock(preview, 3)).toBe('second')
    expect(playingScoreBlock(preview, 5)).toBe('included-variant')
    expect(playingScoreBlock(preview, 7)).toBeNull()
    expect(playingScoreBlock(preview, NaN)).toBeNull()
    expect(exactScoreRow(preview, 'first')).toEqual({ startSeconds: 1, voiceSeconds: 1, pauseSeconds: 1 })
    expect(exactScoreRow(preview, 'missing')).toBeNull()
  })

  it('shows a disabled transport and an explanation for unsaved edits without mounting any old audio', () => {
    const html = renderToStaticMarkup(createElement(ScoreListener, { detail, dirty: true, saving: false, selectedKeys: ['first'], onPreview: () => {}, onActiveBlock: () => {} }))
    expect(html).toContain('Escuchar todo')
    expect(html).toContain('Escuchar selección')
    expect(html).toContain('Guardá los cambios para escucharlos.')
    expect(html).toContain('Escucha de edición: omite respuestas humanas y tramos variables.')
    expect(html.match(/disabled=""/g)).toHaveLength(2)
    expect(html).not.toContain('<audio')
  })

  it('sends one exact revision and omits source_keys only for full listening, with no synthesis or FINAL API', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(preview), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    await api.previewScore('fixture', 'revision-fixed')
    expect(fetch.mock.calls[0][0]).toBe('/api/score-workspaces/fixture/preview')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ revision_id: 'revision-fixed' })
    fetch.mockResolvedValue(new Response(JSON.stringify(preview), { status: 200 }))
    await api.previewScore('fixture', 'revision-fixed', ['second'])
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ revision_id: 'revision-fixed', source_keys: ['second'] })
  })
})
