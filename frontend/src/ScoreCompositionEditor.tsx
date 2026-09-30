import { useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, Check, Download, History, Plus, Redo2, Trash2, Undo2 } from 'lucide-react'
import { api, ApiError } from './api'
import { ScoreListener, type ScoreListenHandle } from './ScoreListener'
import { exactScoreRow } from './score-playback'
import { ScriptBlock } from './ScoreScriptBlock'
import { scoreSeconds, scoreStages } from './score-workspace'
import { addScoreBlock, changeScoreDraft, moveScoreBlock, redoScoreDraft, sameScoreDraft, scoreDraft, scoreDraftBlocks, scoreDraftExport, scoreTimeline, scoreUndoState, undoScoreDraft, validScoreDraft } from './score-draft'
import type { ScoreDraft, ScorePreview, ScoreRevisionSummary, ScoreWorkspaceDetail } from './types'

type EditorState = { busy: boolean; dirty: boolean }
function secondsInput(value: number) { return Number.isFinite(value) ? value / 1000 : '' }
function inputMilliseconds(value: string) { return value === '' ? NaN : Math.round(Number(value) * 1000) }
function displayTime(value: number | null) { return value === null ? 'No disponible' : scoreSeconds(value * 1000) }

export function ScoreCompositionEditor({ detail, onSaved, onState }: {
  detail: ScoreWorkspaceDetail
  onSaved: (detail: ScoreWorkspaceDetail) => void
  onState: (state: EditorState) => void
}) {
  const listener = useRef<ScoreListenHandle>(null)
  const [listenSelection, setListenSelection] = useState<Set<string>>(() => new Set())
  const [activePreview, setActivePreview] = useState<ScorePreview | null>(null)
  const [activeSource, setActiveSource] = useState<string | null>(null)
  const [undo, setUndo] = useState(() => scoreUndoState(scoreDraft(detail)))
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('Guardando…')
  const [error, setError] = useState<string | null>(null)
  const [conflicted, setConflicted] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [revisions, setRevisions] = useState<ScoreRevisionSummary[] | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const generation = useRef(0)
  const historyGeneration = useRef(0)
  const currentDetail = useRef(detail)
  currentDetail.current = detail
  const saved = scoreDraft(detail)
  const draft = undo.current
  const dirty = !sameScoreDraft(draft, saved)
  const sources = detail.catalog_blocks ?? detail.revision?.blocks ?? []
  const blocks = scoreDraftBlocks(draft, sources)
  const selectedKeys = new Set(draft.blocks.map((block) => block.source_key))
  const outside = sources.filter((block) => !selectedKeys.has(block.source_key))
  const outsideStages = [...scoreStages(outside, 'variant'), ...scoreStages(outside, 'main')]
  const timeline = scoreTimeline(draft, sources)
  const valid = validScoreDraft(draft, sources)
  const canSave = dirty && valid && !busy && !conflicted && detail.workspace.status === 'ready'

  useEffect(() => {
    listener.current?.invalidate()
    setListenSelection(new Set())
    setUndo(scoreUndoState(scoreDraft(detail)))
    setError(null)
    setConflicted(false)
  }, [detail.workspace.id, detail.revision?.id])
  useEffect(() => { onState({ busy, dirty }) }, [busy, dirty, onState])
  useEffect(() => () => { ++generation.current; ++historyGeneration.current; onState({ busy: false, dirty: false }) }, [onState])
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])

  const edit = (next: ScoreDraft) => {
    if (busy) return
    listener.current?.invalidate()
    setUndo((previous) => changeScoreDraft(previous, next))
    setNotice(null)
  }
  const loadHistory = async () => {
    const token = ++historyGeneration.current
    setHistoryLoading(true)
    setHistoryError(null)
    try {
      const rows = await api.scoreRevisions(detail.workspace.id)
      if (token === historyGeneration.current) setRevisions(rows)
    } catch (reason) { if (token === historyGeneration.current) setHistoryError((reason as Error).message) }
    finally { if (token === historyGeneration.current) setHistoryLoading(false) }
  }
  const operation = async (work: () => Promise<ScoreWorkspaceDetail>, success: string, label = 'Guardando…') => {
    if (busy || !detail.revision) return
    listener.current?.invalidate()
    const token = ++generation.current
    const workspaceId = detail.workspace.id
    const expectedRevision = detail.revision.id
    setBusyLabel(label)
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const next = await work()
      if (token !== generation.current || currentDetail.current.workspace.id !== workspaceId || currentDetail.current.revision?.id !== expectedRevision) return
      if (next.workspace.id !== workspaceId || !next.revision) throw new Error('No se recibió una versión guardada de esta partitura.')
      setUndo(scoreUndoState(scoreDraft(next)))
      setConflicted(false)
      onSaved(next)
      setNotice(success)
      if (historyOpen) void loadHistory()
    } catch (reason) {
      if (token !== generation.current) return
      const message = (reason as Error).message
      setConflicted((reason instanceof ApiError && reason.status === 409) || /409|conflict|stale|revision.*changed|revision.*match/i.test(message))
      setError(message)
    } finally { if (token === generation.current) setBusy(false) }
  }
  const listenKeys = draft.blocks.filter((block) => listenSelection.has(block.source_key)).map((block) => block.source_key)
  const stageJumps = blocks.filter((block, index) => blocks.findIndex((row) => row.stage_id === block.stage_id) === index).map((block) => ({ title: block.stage_title, index: blocks.findIndex((row) => row.source_key === block.source_key) }))
  const currentPreview = !dirty && !busy && activePreview?.revision_id === detail.revision?.id ? activePreview : null
  const setListenBlock = (key: string, selected: boolean) => { listener.current?.invalidate(); setListenSelection((previous) => { const next = new Set(previous); if (selected) next.add(key); else next.delete(key); return next }) }
  const save = () => {
    if (!canSave || !detail.revision) return
    void operation(() => api.saveScoreRevision(detail.workspace.id, { ...draft, expected_revision_id: detail.revision!.id }), 'Versión guardada.')
  }
  const restore = (revision: ScoreRevisionSummary) => {
    if (busy || !detail.revision) return
    const variants = revision.includes_variants ? ' Esta versión incluye variantes en el recorrido.' : ''
    const changes = dirty ? ' Se descartarán los cambios sin guardar; podés exportarlos antes de restaurar.' : ''
    if (!window.confirm(`Restaurar la versión ${revision.number} como una nueva versión?${variants}${changes}`)) return
    void operation(() => api.restoreScoreRevision(detail.workspace.id, revision.id, detail.revision!.id), 'Versión restaurada como una nueva versión.', 'Restaurando…')
  }
  const exportDraft = () => {
    const blob = new Blob([scoreDraftExport(detail, draft)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `partitura-${detail.workspace.id}-borrador.json`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }
  const reload = () => {
    if (busy || (dirty && !window.confirm('Exportá el borrador si querés conservarlo. ¿Descartar tus cambios y cargar la versión guardada?'))) return
    void operation(() => api.scoreWorkspace(detail.workspace.id), 'Versión guardada cargada.', 'Cargando versión…')
  }

  return <div className="score-composition-editor">
    <div className="score-editor-top"><div className="score-edit-toolbar" aria-label="Edición de la partitura"><span className="score-edit-state" role="status">{busy ? busyLabel : dirty ? 'Cambios sin guardar' : `Versión ${detail.revision?.number ?? '—'} guardada`}</span><div><button className="soft-button" type="button" disabled={busy || undo.past.length === 0} onClick={() => { listener.current?.invalidate(); setUndo(undoScoreDraft) }}><Undo2 size={15} /> Deshacer</button><button className="soft-button" type="button" disabled={busy || undo.future.length === 0} onClick={() => { listener.current?.invalidate(); setUndo(redoScoreDraft) }}><Redo2 size={15} /> Rehacer</button><button className="primary-button" type="button" disabled={!canSave} onClick={save}><Check size={16} /> Guardar versión</button></div></div><ScoreListener ref={listener} detail={detail} dirty={dirty} saving={busy} selectedKeys={listenKeys} onPreview={setActivePreview} onActiveBlock={setActiveSource} /></div>
    {notice && <p className="score-editor-notice" role="status">{notice}</p>}
    {error && <div className="warning-box score-editor-error" role="alert"><p>{conflicted ? 'Otra versión se guardó mientras editabas. Tu borrador sigue aquí.' : 'No se pudo completar la operación. Tu borrador sigue aquí.'}</p><p>{error}</p><button className="soft-button" type="button" disabled={busy} onClick={exportDraft}><Download size={14} /> Exportar borrador JSON</button><button className="soft-button" type="button" disabled={busy} onClick={reload}>Cargar versión guardada</button></div>}
    {detail.workspace.status !== 'ready' && <p className="warning-box" role="alert">Colección incompleta. Conservá o exportá el borrador mientras se revisan las fuentes fijadas.</p>}
    {!valid && <p className="warning-box" role="alert">Revisá los segundos: de 0 a 60, hasta tres decimales. El recorrido debe contener al menos un bloque.</p>}
    <section className="score-timeline" aria-label="Línea de tiempo aproximada"><div className="score-timeline-heading"><div><h3>Línea de tiempo</h3><p>Posiciones aproximadas. Los tiempos exactos se comprobarán al montar el audio. Las respuestas humanas tienen duración variable.</p></div><span>Duración aproximada · {displayTime(timeline.durationSeconds)}</span></div><div className="score-timeline-legend"><span><i className="score-time-voice" /> Voz</span><span><i className="score-time-silence" /> Silencio</span></div><div className="score-timeline-strip"><div className="score-time-lead"><span>Inicio</span><i className="score-time-silence" /><small>{scoreSeconds(draft.lead_in_ms)}</small></div>{timeline.rows.map((row, index) => <a href={`#composition-block-${index}`} key={row.block.source_key} className="score-time-block"><span>Bloque {index + 1} · {displayTime(row.startSeconds)}</span><div><i className="score-time-voice" style={{ flexGrow: row.voiceSeconds ?? 1 }} /><i className="score-time-silence" style={{ flexGrow: row.pauseSeconds ?? 1 }} /></div><small>Voz {displayTime(row.voiceSeconds)} · pausa {displayTime(row.pauseSeconds)}</small></a>)}</div></section>
    <label className="score-stage-jump"><span>Ir a una etapa</span><select aria-label="Ir a una etapa" value="" onChange={(event) => { if (event.target.value !== '') document.getElementById(`composition-block-${event.target.value}`)?.scrollIntoView({ block: 'start' }) }}><option value="">Elegí una etapa</option>{stageJumps.map((stage) => <option key={stage.index} value={stage.index}>{stage.title}</option>)}</select></label>
    <div className="score-composition-start"><div><h3>Inicio del recorrido</h3><p>{blocks.length} bloques en el orden de escucha</p></div><label><span>Silencio inicial · Segundos</span><input aria-label="Silencio inicial en segundos" type="number" min="0" max="60" step="0.001" disabled={busy} value={secondsInput(draft.lead_in_ms)} onChange={(event) => edit({ ...draft, lead_in_ms: inputMilliseconds(event.target.value) })} /></label></div>
    <section className="score-script score-composition-script" aria-label="Guion en orden de escucha">{blocks.map((block, index) => { const exact = currentPreview ? exactScoreRow(currentPreview, block.source_key) : null; return <div key={block.source_key} id={`composition-block-${index}`} className={`score-composition-block${!dirty && activeSource === block.source_key ? ' score-block-playing' : ''}`}><header>{(index === 0 || blocks[index - 1].stage_id !== block.stage_id) && <strong>{block.stage_title}</strong>}{block.role === 'variant' && <span>Variante incluida</span>}<small>{exact ? `En esta escucha · ${displayTime(exact.startSeconds)} · voz ${displayTime(exact.voiceSeconds)} · silencio ${displayTime(exact.pauseSeconds)}` : `Posición aproximada · ${displayTime(timeline.rows[index].startSeconds)}`}</small></header><ScriptBlock block={block} number={index + 1} controls={<><div className="score-block-listen-controls"><label className="score-listen-checkbox"><input type="checkbox" aria-label={`Seleccionar bloque ${index + 1} para escuchar`} checked={listenSelection.has(block.source_key)} disabled={busy} onChange={(event) => setListenBlock(block.source_key, event.target.checked)} /><span>Seleccionar para escuchar</span></label><button className="soft-button" type="button" disabled={dirty || busy || detail.workspace.status !== 'ready'} aria-label={`Escuchar este bloque ${index + 1}`} onClick={() => listener.current?.prepare([block.source_key])}>Escuchar este bloque</button></div><div className="score-block-edit-controls"><label><span>Pausa después · Segundos</span><input aria-label={`Pausa después del bloque ${index + 1} en segundos`} type="number" min="0" max="60" step="0.001" disabled={busy} value={secondsInput(block.pause_after_ms)} onChange={(event) => edit({ ...draft, blocks: draft.blocks.map((row) => row.source_key === block.source_key ? { ...row, pause_after_ms: inputMilliseconds(event.target.value) } : row) })} /></label><div><button className="icon-button" type="button" disabled={busy || index === 0} title="Subir bloque" aria-label={`Subir bloque ${index + 1}`} onClick={() => edit(moveScoreBlock(draft, index, -1))}><ArrowUp size={16} /></button><button className="icon-button" type="button" disabled={busy || index === blocks.length - 1} title="Bajar bloque" aria-label={`Bajar bloque ${index + 1}`} onClick={() => edit(moveScoreBlock(draft, index, 1))}><ArrowDown size={16} /></button><button className="icon-button" type="button" disabled={busy || blocks.length === 1} title="Quitar del recorrido" aria-label={`Quitar bloque ${index + 1} del recorrido`} onClick={() => edit({ ...draft, blocks: draft.blocks.filter((row) => row.source_key !== block.source_key) })}><Trash2 size={15} /></button></div></div></>} /></div>})}</section>
    <section className="score-outside"><h3>Variantes y bloques fuera del recorrido</h3><p>Al agregar uno aparecerá al final. Podés moverlo con los botones del guion.</p>{outsideStages.length === 0 && <p>Todos los bloques de la colección están incluidos.</p>}{outsideStages.map((stage, index) => <details key={`${stage.id}-${index}`}><summary>{stage.title}<span>{stage.blocks.length} bloques</span></summary>{stage.blocks.map((block, blockIndex) => <ScriptBlock key={block.source_key} block={block} number={blockIndex + 1} controls={<button className="soft-button" type="button" disabled={busy || block.status !== 'ready' || draft.blocks.length >= 256} onClick={() => edit(addScoreBlock(draft, block))}><Plus size={15} /> Agregar al recorrido</button>} />)}</details>)}</section>
    <details className="score-version-history" onToggle={(event) => { const open = event.currentTarget.open; setHistoryOpen(open); if (open && !revisions && !historyLoading) void loadHistory() }}><summary><History size={16} /> Historial de versiones</summary>{historyLoading && <p role="status">Cargando versiones…</p>}{historyError && <div role="alert"><p>No se pudo cargar el historial: {historyError}</p><button className="soft-button" type="button" onClick={() => void loadHistory()}>Reintentar</button></div>}{revisions?.slice().sort((a, b) => b.number - a.number).map((revision) => <div className="score-version-row" key={revision.id}><span>Versión {revision.number} · {revision.block_count} bloques{revision.kind === 'catalog_initial' ? ' · Catálogo inicial (incluye variantes)' : revision.includes_variants ? ' · Incluye variantes' : ''}{revision.created_at && <small>{new Date(revision.created_at).toLocaleString()}</small>}{revision.restored_from_revision_id && <small>Restaurada como una nueva versión</small>}</span><button className="soft-button" type="button" disabled={busy || conflicted || revision.id === detail.revision?.id || detail.workspace.status !== 'ready'} onClick={() => restore(revision)}>Restaurar</button></div>)}</details>
    <div className="score-editor-bottom"><button className="soft-button" type="button" disabled={busy} onClick={exportDraft}><Download size={15} /> Exportar borrador JSON</button><p>Los proyectos y las tomas de origen conservan sus versiones.</p></div>
  </div>
}
