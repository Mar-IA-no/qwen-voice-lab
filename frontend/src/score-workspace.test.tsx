import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ScoreScriptContent, ScoreWorkspace } from './ScoreWorkspace'
import { initialScoreWorkspace, scoreSeconds, scoreStages } from './score-workspace'
import type { ScoreWorkspaceBlock, ScoreWorkspaceCatalog, ScoreWorkspaceDetail } from './types'

function block(index: number, role: 'main' | 'variant' = 'main'): ScoreWorkspaceBlock {
  return {
    source_key: `source-${index}`, role, stage_id: `stage-${Math.floor(index / 12)}`, stage_title: `Etapa de ejemplo ${Math.floor(index / 12) + 1}`,
    order: index, text: `Texto completo del bloque ${index + 1}.`, pause_after_ms: 2500,
    speech_speed: 1, baseline_speed: 0.98, duration_seconds: 3, status: 'ready', unavailable_reason: null,
    response_marker: index === 0 ? 'Respuesta de la persona · duración variable' : null,
    source: { project_id: `project-${index % 2}`, revision_id: 'fixed-editorial', revision_sha256: 'a'.repeat(64), revision_snapshot_sha256: 'b'.repeat(64), segment_id: 'block_001', take_id: `take-${index}`, text_sha256: 'c'.repeat(64), audio_sha256: 'd'.repeat(64), take_revision_id: 'earlier-generation', validation_status: index === 0 ? 'pass' : 'needs_review', selection_override_reason: index === 1 ? 'Selección histórica conservada' : null, provenance: { origin: 'fixture' }, take_provenance: {} },
  }
}

const summary = { id: 'fixed', title: 'Guion de ejemplo', is_default: true, status: 'ready' as const, main_block_count: 72, variant_block_count: 10 }
const catalog: ScoreWorkspaceCatalog = { workspaces: [{ ...summary, id: 'later', is_default: false }, summary], default_workspace_id: 'fixed', editorial_mode: true }
const detail: ScoreWorkspaceDetail = { workspace: summary, catalog_sha256: 'e'.repeat(64), revision: { id: 'fixed-revision', number: 1, lead_in_ms: 10000, beacon: null, blocks: [...Array.from({ length: 72 }, (_, index) => block(index)), ...Array.from({ length: 10 }, (_, index) => block(72 + index, 'variant'))] } }

describe('editorial score workspace', () => {
  it('opens only the explicit configured default and never chooses another by list position', () => {
    expect(initialScoreWorkspace(catalog)).toBe('fixed')
    expect(initialScoreWorkspace({ ...catalog, default_workspace_id: 'missing' })).toBeNull()
    expect(initialScoreWorkspace({ ...catalog, default_workspace_id: null })).toBeNull()
    expect(initialScoreWorkspace({ ...catalog, workspaces: [], default_workspace_id: 'fixed' })).toBeNull()
    expect(initialScoreWorkspace({ ...catalog, workspaces: [{ ...summary, is_default: false }] })).toBeNull()
  })

  it('orders the complete primary script independently from its variants and preserves duplicate source segment ids', () => {
    const input = [...detail.revision!.blocks].reverse()
    const stages = scoreStages(input, 'main')
    expect(stages.flatMap((stage) => stage.blocks).map((row) => row.source_key)).toEqual(Array.from({ length: 72 }, (_, index) => `source-${index}`))
    expect(scoreStages(input, 'variant').flatMap((stage) => stage.blocks)).toHaveLength(10)
    expect(input[0].source_key).toBe('source-81')
    expect(scoreSeconds(2500)).toBe('2,5 s')
  })

  it('renders all 72 primary blocks and separate collapsed variants without edit, generation or montage actions', () => {
    const html = renderToStaticMarkup(createElement(ScoreScriptContent, { detail }))
    const principal = html.slice(0, html.indexOf('class="score-variants"'))
    expect(principal.match(/class="score-spoken-text"/g)).toHaveLength(72)
    expect(html.match(/class="score-spoken-text"/g)).toHaveLength(82)
    expect(html).toContain('Silencio inicial · 10 s')
    expect(html).toContain('Selección provisional')
    expect(html).toContain('Validación automática')
    expect(html).toContain('Selección histórica conservada')
    expect(html.match(/Respuesta de la persona · duración variable/g)).toHaveLength(1)
    expect(html).not.toMatch(/<textarea|<input|<audio|<button|<details open|Generar|Final validado|Otra toma/)
  })

  it('keeps unavailable sources visible and does not promote provisional status', () => {
    const missing = block(0)
    missing.status = 'unavailable'
    missing.unavailable_reason = 'La toma fijada no está disponible.'
    missing.source.validation_status = 'needs_review'
    const html = renderToStaticMarkup(createElement(ScoreScriptContent, { detail: { ...detail, workspace: { ...summary, status: 'unavailable' }, revision: { ...detail.revision!, blocks: [missing] } } }))
    expect(html).toContain('Colección incompleta')
    expect(html).toContain(missing.text)
    expect(html).toContain('Fuente no disponible')
    expect(html).toContain('Selección provisional')
    expect(html).not.toContain('Validación automática')
  })

  it('reports absent default and catalog errors honestly', () => {
    const props = { catalog: { ...catalog, default_workspace_id: null }, catalogError: null, reloadCatalog: () => {}, openOtherWorks: () => {} }
    expect(renderToStaticMarkup(createElement(ScoreWorkspace, props))).toContain('No hay una colección inicial fijada')
    expect(renderToStaticMarkup(createElement(ScoreWorkspace, { ...props, catalog: null, catalogError: 'No disponible' }))).toContain('No se pudo cargar la colección')
  })
})
