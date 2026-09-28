import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowDown, ArrowUp, AudioWaveform, Check, Download, History, Plus, RefreshCw, Sparkles, Trash2 } from 'lucide-react'
import { api } from './api'
import type { Assembly, BeaconSettings, Language, Project, ProjectDetail, ProjectRun, SourceRevision, Take, Voice, WorkshopBlock } from './types'
import { effectiveSpeed, newBlock, validBlocks } from './workshop-timing'
import { WorkshopPlayer } from './WorkshopPlayer'

type Notify = (kind: 'ok' | 'error', text: string) => void
type Draft = { blocks: WorkshopBlock[]; lead_pause_ms: number; speech_speed: number; beacon: BeaconSettings }
const defaultBeacon = (): BeaconSettings => ({ enabled: false, asset_id: null, offset_seconds: 0, volume: 0.25 })
const emptyDraft = (): Draft => ({ blocks: [newBlock()], lead_pause_ms: 0, speech_speed: 1, beacon: defaultBeacon() })

function revisionDraft(detail: ProjectDetail): Draft {
  const revision = detail.revision
  return {
    blocks: (revision?.blocks?.length ? revision.blocks : detail.segments).map((row) => ({
      id: row.id, text: row.text, pause_after_ms: row.pause_after_ms, speed: row.speed ?? null,
      selected_take_id: row.selected_take_id ?? null,
    })),
    lead_pause_ms: revision?.lead_pause_ms ?? 0,
    speech_speed: revision?.speech_speed ?? 1,
    beacon: revision?.beacon ?? defaultBeacon(),
  }
}

function seconds(ms: number): string { return (ms / 1000).toString() }
function pauseMs(value: string): number { return Math.round(Number(value) * 1000) }
function conflict(error: unknown): boolean { return /\b409\b|stale|revision|conflict/i.test((error as Error).message) }
function validDraft(value: Draft): boolean {
  return validBlocks(value.blocks) && Number.isSafeInteger(value.lead_pause_ms) && value.lead_pause_ms >= 0 &&
    Number.isFinite(value.speech_speed) && value.speech_speed >= 0.5 && value.speech_speed <= 2 &&
    Number.isFinite(value.beacon.offset_seconds) && value.beacon.offset_seconds >= 0 &&
    Number.isFinite(value.beacon.volume) && value.beacon.volume >= 0 && value.beacon.volume <= 1 &&
    (!value.beacon.enabled || !!value.beacon.asset_id)
}

export function ProjectWorkshop({ projects, voices, notify, refresh }: {
  projects: Project[]; voices: Voice[]; notify: Notify; refresh: (quiet?: boolean) => Promise<void>
}) {
  const [selectedId, setSelectedId] = useState('')
  const [creating, setCreating] = useState(false)
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [savedId, setSavedId] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [stale, setStale] = useState(false)
  const [busy, setBusy] = useState(false)
  const [revisions, setRevisions] = useState<SourceRevision[]>([])
  const [runs, setRuns] = useState<ProjectRun[]>([])
  const [assemblies, setAssemblies] = useState<Assembly[]>([])
  const [takes, setTakes] = useState<Record<string, Take[]>>({})
  const [audition, setAudition] = useState<Assembly | null>(null)
  const [legacy, setLegacy] = useState(false)
  const [markdown, setMarkdown] = useState('')
  const [meta, setMeta] = useState({ title: '', voice_id: '', language: 'en' as Language, project_seed: 20260805 })
  const current = useRef('')
  const loadedRevision = useRef<string | null>(null)
  const sequence = useRef(0)
  const dirtyRef = useRef(false)
  const savedRef = useRef<string | null>(null)
  const notifyRef = useRef(notify)
  notifyRef.current = notify

  const hydrate = useCallback((next: ProjectDetail) => {
    setDraft(revisionDraft(next))
    setSavedId(next.revision?.id ?? null)
    savedRef.current = next.revision?.id ?? null
    setDirty(false)
    dirtyRef.current = false
    setStale(false)
  }, [])

  const load = useCallback(async (id: string, force = false) => {
    const token = ++sequence.current
    const [next, nextRevisions, nextRuns, nextAssemblies] = await Promise.all([
      api.project(id), api.projectRevisions(id), api.projectRuns(id), api.projectAssemblies(id),
    ])
    const nextTakes = await Promise.all(next.segments.map(async (segment) => [segment.id, await api.projectTakes(id, segment.id)] as const))
    if (token !== sequence.current || current.current !== id) return
    if (loadedRevision.current !== (next.revision?.id ?? null)) setAudition(null)
    loadedRevision.current = next.revision?.id ?? null
    setDetail(next)
    setRevisions(nextRevisions)
    setRuns(nextRuns)
    setAssemblies(nextAssemblies)
    setTakes(Object.fromEntries(nextTakes))
    if (force || !dirtyRef.current) hydrate(next)
    else if (savedRef.current !== next.revision?.id) setStale(true)
  }, [hydrate])

  useEffect(() => {
    if (!selectedId && !creating && projects[0]) setSelectedId(projects[0].id)
  }, [creating, projects, selectedId])
  useEffect(() => {
    if (!meta.voice_id && voices[0]) setMeta((value) => ({ ...value, voice_id: voices[0].id }))
  }, [meta.voice_id, voices])
  useEffect(() => {
    if (!selectedId || creating) return
    current.current = selectedId
    setAudition(null)
    dirtyRef.current = false
    void load(selectedId, true).catch((error) => notifyRef.current('error', (error as Error).message))
    const timer = window.setInterval(() => void load(selectedId).catch(() => undefined), 3000)
    return () => { window.clearInterval(timer); ++sequence.current }
  }, [selectedId, creating, load])

  const edit = (patch: Partial<Draft>) => {
    setDraft((value) => ({ ...value, ...patch }))
    setDirty(true)
    dirtyRef.current = true
  }
  const patchBlock = (id: string, patch: Partial<WorkshopBlock>) => edit({ blocks: draft.blocks.map((row) => row.id === id ? { ...row, ...patch } : row) })
  const moveBlock = (index: number, step: number) => {
    const rows = [...draft.blocks]
    const [row] = rows.splice(index, 1)
    rows.splice(index + step, 0, row)
    edit({ blocks: rows })
  }
  const switchProject = (id: string) => {
    if (busy) return
    if (dirty && !window.confirm('Descartar cambios sin guardar?')) return
    current.current = id
    setSelectedId(id)
    setCreating(false)
  }
  const startCreate = () => {
    if (busy) return
    if (dirty && !window.confirm('Descartar cambios sin guardar?')) return
    current.current = ''
    setCreating(true)
    setDetail(null)
    setDraft(emptyDraft())
    setLegacy(false)
    setMarkdown('')
    setDirty(false)
    dirtyRef.current = false
    setMeta((value) => ({ ...value, title: '' }))
  }
  const execute = async (work: () => Promise<unknown>, success: string, force = false) => {
    if (!detail) return
    setBusy(true)
    try {
      await work()
      notify('ok', success)
      await refresh(true)
      await load(detail.id, force)
    } catch (error) {
      if (conflict(error)) setStale(true)
      notify('error', (error as Error).message)
    } finally { setBusy(false) }
  }
  const create = async (event: FormEvent) => {
    event.preventDefault()
    if (!legacy && !validDraft(draft)) return notify('error', 'Revisá texto, pausas, velocidades e IDs de los bloques.')
    if (legacy && !markdown.trim()) return notify('error', 'El Markdown está vacío.')
    setBusy(true)
    try {
      let next = await api.createProject({
        title: meta.title.trim(), voice_id: meta.voice_id, language: meta.language, project_seed: meta.project_seed,
        ...(legacy ? { markdown } : { blocks: draft.blocks, lead_pause_ms: draft.lead_pause_ms, speech_speed: draft.speech_speed, beacon: draft.beacon }),
        sampling: { do_sample: true, temperature: 0.9, top_p: 1, top_k: 50, repetition_penalty: 1.05,
          subtalker_dosample: true, subtalker_temperature: 0.9, subtalker_top_p: 1, subtalker_top_k: 50, max_new_tokens: 2048 },
      })
      setSelectedId(next.id)
      setCreating(false)
      if (!legacy && (draft.lead_pause_ms !== 0 || draft.speech_speed !== 1) && next.revision?.id) {
        next = await api.reviseProject(next.id, { ...draft, expected_revision_id: next.revision.id })
      }
      await refresh(true)
      notify('ok', 'Proyecto creado.')
    } catch (error) { notify('error', (error as Error).message) } finally { setBusy(false) }
  }
  const save = () => {
    if (!detail || !savedId || !validDraft(draft)) return notify('error', 'Revisá el timing y los bloques antes de guardar.')
    const expected = savedId
    const submitted = draft
    setAudition(null)
    void execute(async () => {
      const revised = await api.reviseProject(detail.id, { expected_revision_id: expected, ...submitted })
      hydrate(revised)
    }, 'Revisión guardada.', true)
  }
  const restore = (revisionId: string) => {
    if (!detail || !savedId || !window.confirm('Restaurar esta revisión como una nueva revisión?')) return
    setAudition(null)
    void execute(() => api.restoreRevision(detail.id, revisionId, savedId), 'Revisión restaurada.', true)
  }
  const reloadLatest = async () => {
    if (!detail || busy || !window.confirm('Descartar este borrador y cargar la última revisión?')) return
    setBusy(true)
    try { await load(detail.id, true) }
    catch (error) { notify('error', (error as Error).message) }
    finally { setBusy(false) }
  }
  const revisionId = detail?.revision?.id
  const activeRun = runs.some((row) => row.status === 'queued' || row.status === 'running')
  const canOperate = !!detail && !!revisionId && !busy && !dirty && !stale
  const allSelected = !!detail?.segments.length && detail.segments.every((row) => row.selected_take_id)
  const currentPreview = assemblies.find((row) => row.kind === 'preview' && row.revision_id === revisionId && row.segment_id === null)
  const currentFinal = assemblies.find((row) => row.kind === 'final' && row.revision_id === revisionId)
  const savedBeacon = detail?.revision?.beacon
  const beaconSrc = savedBeacon?.asset_id && detail ? `/api/projects/${detail.id}/beacon` : undefined

  return <div className="projects-layout workshop-layout">
    <aside className="panel project-list"><div className="panel-heading compact"><div><span className="kicker">LONG FORM</span><h2>Proyectos</h2></div><button type="button" className="icon-button" title="Nuevo proyecto" disabled={busy} onClick={startCreate}><Plus size={17} /></button></div>
      {projects.map((project) => <button type="button" key={project.id} disabled={busy} className={selectedId === project.id && !creating ? 'active' : ''} onClick={() => switchProject(project.id)}><span><strong>{project.title}</strong><small>{project.language.toUpperCase()} · {project.status}</small></span></button>)}
    </aside>
    {creating || !detail ? <form className="project-workspace" onSubmit={create}><section className="panel project-editor">
      <div className="panel-heading"><div><span className="kicker">NEW PROJECT</span><h2>Nuevo proyecto</h2></div></div>
      <fieldset className="workshop-fields" disabled={busy}>
      <div className="field-grid two"><label><span>Nombre</span><input required value={meta.title} onChange={(e) => setMeta({ ...meta, title: e.target.value })} /></label><label><span>Voz</span><select required value={meta.voice_id} onChange={(e) => setMeta({ ...meta, voice_id: e.target.value })}>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select></label></div>
      <div className="field-grid two"><label><span>Idioma</span><select value={meta.language} onChange={(e) => setMeta({ ...meta, language: e.target.value as Language })}>{(['es', 'en', 'pt', 'fr', 'it', 'de'] as Language[]).map((row) => <option key={row} value={row}>{row.toUpperCase()}</option>)}</select></label><label><span>Seed</span><input type="number" min="0" value={meta.project_seed} onChange={(e) => setMeta({ ...meta, project_seed: Number(e.target.value) })} /></label></div>
      <div className="segmented workshop-mode"><button type="button" className={!legacy ? 'active' : ''} onClick={() => setLegacy(false)}>Bloques</button><button type="button" className={legacy ? 'active' : ''} onClick={() => setLegacy(true)}>Importar .md</button></div>
      {legacy ? <label><span>Markdown legado</span><textarea rows={10} value={markdown} onChange={(e) => setMarkdown(e.target.value)} /><input type="file" accept=".md,text/markdown,text/plain" onChange={(e) => { const file = e.target.files?.[0]; if (file) void file.text().then(setMarkdown) }} /></label> : <><TimingControls draft={draft} edit={edit} /><BlockEditor blocks={draft.blocks} speechSpeed={draft.speech_speed} patchBlock={patchBlock} moveBlock={moveBlock} edit={edit} /></>}
      <button className="primary-button" disabled={busy || !meta.voice_id}><Plus size={16} /> Crear proyecto</button>
      </fieldset>
    </section></form> : <div className="project-workspace">
      <section className="panel project-editor"><div className="panel-heading"><div><span className="kicker">REVISION {detail.revision?.number ?? '—'}</span><h2>{detail.title}</h2></div><span className="workshop-state">{stale ? 'Desactualizada' : dirty ? 'Sin guardar' : 'Guardada'}</span></div>
        {stale && <div className="warning-box" role="alert">Hay una revisión más reciente. Tu borrador no se ha sobrescrito. <button type="button" className="soft-button" disabled={busy} onClick={() => void reloadLatest()}><RefreshCw size={14} /> Cargar última</button></div>}
        <fieldset className="workshop-fields" disabled={busy}>
        <TimingControls draft={draft} edit={edit} />
        <BlockEditor blocks={draft.blocks} speechSpeed={draft.speech_speed} patchBlock={patchBlock} moveBlock={moveBlock} edit={edit} />
        <div className="workshop-beacon"><label className="check-field"><input type="checkbox" checked={draft.beacon.enabled} disabled={!draft.beacon.asset_id} onChange={(e) => edit({ beacon: { ...draft.beacon, enabled: e.target.checked } })} /><span>Beacon</span></label><label><span>Offset s</span><input type="number" min="0" step="0.01" value={draft.beacon.offset_seconds} onChange={(e) => edit({ beacon: { ...draft.beacon, offset_seconds: Number(e.target.value) } })} /></label><label><span>Volumen</span><input type="range" min="0" max="1" step="0.01" value={draft.beacon.volume} onChange={(e) => edit({ beacon: { ...draft.beacon, volume: Number(e.target.value) } })} /></label><span className="workshop-asset">{draft.beacon.asset_id ? 'Audio local disponible' : 'Sin audio Beacon local'}</span></div>
        </fieldset>
        <div className="project-actions"><button type="button" className="soft-button" disabled={busy || !dirty || stale || activeRun || !validDraft(draft)} onClick={save}><Check size={16} /> Guardar revisión</button>
          <button type="button" className="primary-button" disabled={!canOperate || activeRun || allSelected} title="Usa GPU al ejecutarse" onClick={() => void execute(() => api.runProject(detail.id, revisionId!), 'Generación de faltantes encolada.')}><Sparkles size={16} /> Generar faltantes · GPU</button>
          <button type="button" className="soft-button" disabled={!canOperate || !allSelected} onClick={() => void execute(async () => { const result = await api.previewProject(detail.id, revisionId!); setAudition({ ...result, segment_id: null }) }, 'Preview CPU creado.')}><AudioWaveform size={16} /> Preview completo</button>
          <button type="button" className="soft-button" disabled={!canOperate || !allSelected} title="La validación final puede usar GPU" onClick={() => void execute(() => api.assembleProject(detail.id, revisionId!), 'Final validado creado.')}><Check size={16} /> Final validado · GPU</button>
        </div>
        {runs[0] && <div className="project-run"><span>Run {runs[0].status}</span><div className="progress"><i style={{ width: `${runs[0].progress * 100}%` }} /></div>{runs[0].error && <p className="error-copy">{runs[0].error}</p>}</div>}
        <details className="workshop-history"><summary><History size={15} /> Historial · {revisions.length}</summary><div>{revisions.slice().reverse().map((row) => <div key={row.id}><span>#{row.number} · {new Date(row.created_at).toLocaleString()}</span><button type="button" className="soft-button" disabled={busy || stale || row.id === revisionId} onClick={() => restore(row.id)}>Restaurar</button></div>)}</div></details>
      </section>
      <section className="segment-stack">{detail.segments.map((segment, index) => <article className="panel segment-card" key={segment.id}><header><span>{String(index + 1).padStart(2, '0')}</span><p>{segment.text}</p><em>{seconds(segment.pause_after_ms)} s · {`×${effectiveSpeed(segment, draft.speech_speed, (takes[segment.id] ?? []).find((take) => take.id === segment.selected_take_id)?.baseline_speed ?? detail.baseline_speed ?? 1).toFixed(2)} efectiva`}</em></header>
        <div className="workshop-segment-actions"><button type="button" className="soft-button" disabled={!canOperate || !segment.selected_take_id} onClick={() => void execute(async () => { const result = await api.previewProject(detail.id, revisionId!, segment.id); setAudition({ ...result, segment_id: segment.id }) }, 'Preview ajustado del bloque creado.')}><AudioWaveform size={14} /> Preview ajustado</button><button type="button" className="soft-button" disabled={!canOperate || activeRun} title="Usa GPU al ejecutarse" onClick={() => void execute(() => api.generateTake(detail.id, segment.id, revisionId!), 'Nueva toma encolada.')}><Plus size={14} /> Otra toma · GPU</button></div>
        <div className="take-strip">{(takes[segment.id] ?? []).map((take) => <div className={take.selected ? 'take-card selected' : 'take-card'} key={take.id}><div><strong>Toma {take.attempt} {take.selected ? '· seleccionada' : ''}</strong><small>{take.status} · seed {take.seed}</small></div><label className="workshop-raw"><span>Original raw · sin ajustes de tiempo</span><audio controls preload="none" src={`/api/takes/${take.id}/audio?raw=true`} /></label><div className="qc-chips">{take.quality_reports.map((qc) => <span key={qc.id} className={qc.verdict} title={qc.reasons.join(' · ')}>{qc.validator.split('-')[0]} · {qc.verdict}</span>)}</div><div className="take-actions"><a className="download-button" href={`/api/takes/${take.id}/download?raw=true`} download><Download size={14} /> Raw</a>{!take.selected && <button type="button" className="soft-button" disabled={!canOperate} onClick={() => { const override = take.status !== 'pass'; const reason = override ? window.prompt('Motivo del override:') ?? '' : undefined; if (override && !reason) return; setAudition(null); void execute(() => api.selectTake(detail.id, segment.id, take.id, revisionId!, override, reason), 'Toma seleccionada.', true) }}>Elegir</button>}</div></div>)}</div>
      </article>)}</section>
      {(audition || currentPreview || currentFinal) && <section className="panel workshop-output"><div className="panel-heading compact"><div><span className="kicker">AUDITION</span><h2>Escucha ajustada</h2></div></div>{(dirty || stale || (!!audition && audition.revision_id !== revisionId)) && <p className="workshop-audio-state">Audio de una revisión guardada; los cambios actuales aún no están en el audio.</p>}<div className="workshop-output-list">{audition && <WorkshopPlayer key={audition.id} label={audition.segment_id ? 'Bloque ajustado' : 'Preview completo'} src={`/api/assemblies/${audition.id}/audio`} beacon={audition.revision_id === revisionId ? savedBeacon : undefined} beaconSrc={beaconSrc} />}{!audition && currentPreview && <WorkshopPlayer key={currentPreview.id} label="Preview completo" src={`/api/assemblies/${currentPreview.id}/audio`} beacon={savedBeacon} beaconSrc={beaconSrc} />}</div><div className="workshop-downloads">{currentPreview && <a className="download-button" href={`/api/assemblies/${currentPreview.id}/bundle`} download><Download size={15} /> Bundle CPU · WAV + partitura</a>}{currentFinal && <a className="download-button" href={`/api/assemblies/${currentFinal.id}/download`} download><Download size={15} /> Final validado · {currentFinal.audit_status}</a>}</div></section>}
    </div>}
  </div>
}

function TimingControls({ draft, edit }: { draft: Draft; edit: (patch: Partial<Draft>) => void }) {
  return <div className="workshop-timing"><label><span>Silencio inicial · s</span><input type="number" min="0" step="0.01" value={seconds(draft.lead_pause_ms)} onChange={(e) => edit({ lead_pause_ms: pauseMs(e.target.value) })} /></label><label><span>Velocidad general · ×</span><input type="number" min="0.5" max="2" step="0.01" value={draft.speech_speed} onChange={(e) => edit({ speech_speed: Number(e.target.value) })} /></label></div>
}

function BlockEditor({ blocks, speechSpeed, patchBlock, moveBlock, edit }: {
  blocks: WorkshopBlock[]; speechSpeed: number; patchBlock: (id: string, patch: Partial<WorkshopBlock>) => void;
  moveBlock: (index: number, step: number) => void; edit: (patch: Partial<Draft>) => void
}) {
  return <div className="workshop-blocks"><div className="workshop-block-heading"><strong>Partitura</strong><span>{blocks.length} {blocks.length === 1 ? 'bloque' : 'bloques'}</span></div>{blocks.map((block, index) => <div className="workshop-block" key={block.id}><span className="score-index">{String(index + 1).padStart(2, '0')}</span><label className="workshop-block-text"><span>Texto</span><textarea rows={2} value={block.text} onChange={(e) => patchBlock(block.id, { text: e.target.value })} /></label><label><span>Pausa después · s</span><input type="number" min="0" step="0.01" value={seconds(block.pause_after_ms)} onChange={(e) => patchBlock(block.id, { pause_after_ms: pauseMs(e.target.value) })} /></label><label><span>Velocidad</span><select value={block.speed === null ? 'inherit' : 'custom'} onChange={(e) => patchBlock(block.id, { speed: e.target.value === 'inherit' ? null : speechSpeed })}><option value="inherit">General ×{speechSpeed}</option><option value="custom">Propia</option></select>{block.speed !== null && <input aria-label={`Velocidad bloque ${index + 1}`} type="number" min="0.5" max="2" step="0.01" value={block.speed} onChange={(e) => patchBlock(block.id, { speed: Number(e.target.value) })} />}</label><div className="workshop-block-tools"><button type="button" className="icon-button" title="Subir bloque" aria-label="Subir bloque" disabled={index === 0} onClick={() => moveBlock(index, -1)}><ArrowUp size={15} /></button><button type="button" className="icon-button" title="Bajar bloque" aria-label="Bajar bloque" disabled={index === blocks.length - 1} onClick={() => moveBlock(index, 1)}><ArrowDown size={15} /></button><button type="button" className="icon-button danger" title="Eliminar bloque" aria-label="Eliminar bloque" disabled={blocks.length === 1} onClick={() => edit({ blocks: blocks.filter((row) => row.id !== block.id) })}><Trash2 size={15} /></button></div></div>)}<button type="button" className="add-row" onClick={() => edit({ blocks: [...blocks, newBlock()] })}><Plus size={15} /> Agregar bloque</button></div>
}
