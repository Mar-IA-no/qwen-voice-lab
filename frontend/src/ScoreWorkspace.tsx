import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Clock3, RefreshCw } from 'lucide-react'
import { api } from './api'
import { ScoreCompositionEditor } from './ScoreCompositionEditor'
import { initialScoreWorkspace, scoreSeconds, scoreStages } from './score-workspace'
import { ScriptBlock } from './ScoreScriptBlock'
import type { ScoreWorkspaceCatalog, ScoreWorkspaceDetail } from './types'

export function ScoreWorkspace({ catalog, catalogError, reloadCatalog, openOtherWorks, onEditorState }: {
  catalog: ScoreWorkspaceCatalog | null
  catalogError: string | null
  reloadCatalog: () => void
  openOtherWorks: () => void
  onEditorState?: (state: { busy: boolean; dirty: boolean }) => void
}) {
  const [selectedId, setSelectedId] = useState<string | null>(() => catalog ? initialScoreWorkspace(catalog) : null)
  const [detail, setDetail] = useState<ScoreWorkspaceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [requestVersion, setRequestVersion] = useState(0)
  const [editorState, setEditorState] = useState({ busy: false, dirty: false })
  const sequence = useRef(0)

  useEffect(() => {
    if (!catalog || editorState.busy || editorState.dirty) return
    setSelectedId((current) => current && catalog.workspaces.some((row) => row.id === current) ? current : initialScoreWorkspace(catalog))
  }, [catalog, editorState.busy, editorState.dirty])

  useEffect(() => {
    const token = ++sequence.current
    setDetail(null)
    setError(null)
    if (!selectedId) { setLoading(false); return }
    setLoading(true)
    void api.scoreWorkspace(selectedId).then((next) => {
      if (token !== sequence.current) return
      if (next.workspace.id !== selectedId) throw new Error('La respuesta no corresponde a la partitura elegida.')
      setDetail(next)
    }).catch((reason: unknown) => {
      if (token === sequence.current) setError((reason as Error).message)
    }).finally(() => { if (token === sequence.current) setLoading(false) })
    return () => { ++sequence.current }
  }, [selectedId, requestVersion])


  useEffect(() => { onEditorState?.(editorState) }, [editorState, onEditorState])
  useEffect(() => () => { onEditorState?.({ busy: false, dirty: false }) }, [onEditorState])
  const canLeave = () => !editorState.busy && (!editorState.dirty || window.confirm('Hay cambios sin guardar. Podés exportar el borrador antes de salir. ¿Descartar los cambios y continuar?'))
  const reload = () => { if (canLeave()) { reloadCatalog(); setRequestVersion((value) => value + 1) } }

  const selectedDetail = detail?.workspace.id === selectedId ? detail : null

  return <div className="score-workspace">
    <header className="score-workspace-heading">
      <div><span className="kicker">GUION DE ESCUCHA</span><h2>{selectedDetail?.workspace.title ?? 'Partitura'}</h2><p>Recorrido principal y variantes de la colección elegida.</p></div>
      <button type="button" className="soft-button" disabled={editorState.busy} onClick={() => { if (canLeave()) openOtherWorks() }}>Otros trabajos <ArrowRight size={15} /></button>
    </header>
    {catalogError && <section className="panel score-empty" role="alert"><h3>No se pudo cargar la colección</h3><p>{catalogError}</p><button type="button" className="soft-button" disabled={editorState.busy} onClick={reloadCatalog}><RefreshCw size={15} /> Reintentar</button></section>}
    {!catalog ? (!catalogError && <p role="status">Cargando colecciones…</p>) : <>
      {catalog.workspaces.length > 0 && <div className="score-collection-bar">{catalog.workspaces.length > 1 && <label><span>Colección</span><select disabled={editorState.busy} value={selectedId ?? ''} onChange={(event) => { if (canLeave()) setSelectedId(event.target.value || null) }}>{!selectedId && <option value="">Elegí una colección</option>}{catalog.workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.title}{workspace.status === 'unavailable' ? ' · incompleta' : ''}</option>)}</select></label>}<button type="button" className="soft-button" disabled={editorState.busy} onClick={reload}><RefreshCw size={14} /> Actualizar</button></div>}
      {!selectedId && <section className="panel score-empty"><h3>{catalog.workspaces.length ? 'Elegí una colección' : 'No hay una colección configurada'}</h3><p>{catalog.workspaces.length ? 'No hay una colección inicial fijada. Podés elegir una de la lista.' : 'Los trabajos guardados siguen disponibles en Otros trabajos.'}</p></section>}
      {loading && <p role="status" className="score-empty">Cargando el guion completo…</p>}
      {error && <section className="panel score-empty" role="alert"><h3>No se pudo abrir esta partitura</h3><p>{error}</p><button type="button" className="soft-button" onClick={() => setRequestVersion((value) => value + 1)}>Reintentar</button></section>}
      {selectedDetail && !selectedDetail.revision && <section className="panel score-empty" role="alert"><h3>Colección no disponible</h3><p>Las fuentes fijadas de esta colección no pudieron verificarse. Actualizá la colección después de revisar sus fuentes.</p></section>}
      {selectedDetail?.revision && <ScoreCompositionEditor key={selectedDetail.workspace.id} detail={selectedDetail} onSaved={setDetail} onState={setEditorState} />}
    </>}
  </div>
}

export function ScoreScriptContent({ detail }: { detail: ScoreWorkspaceDetail }) {
  const revision = detail.revision
  if (!revision) return null
  const main = scoreStages(revision.blocks, 'main')
  const variants = scoreStages(revision.blocks, 'variant')
  let mainIndex = 0
  return <>
        {detail?.workspace.status !== 'ready' && <p className="warning-box" role="alert">Colección incompleta. Los bloques no disponibles están señalados en el guion; sus fuentes siguen fijadas.</p>}
        <div className="score-script-layout">
          <nav className="score-stage-nav" aria-label="Etapas del recorrido principal"><span className="kicker">RECORRIDO PRINCIPAL</span><a href="#score-start">Inicio</a>{main.map((stage, index) => <a key={`${stage.id}-${index}`} href={`#score-stage-${index}`}><span>{index + 1}</span>{stage.title}</a>)}{variants.length > 0 && <a href="#score-variants">Variantes aparte</a>}</nav>
          <div className="score-script">
            <section className="score-start" id="score-start"><Clock3 size={19} /><div><h3>Inicio</h3><p>Silencio inicial · {scoreSeconds(revision.lead_in_ms)}</p></div><span>Revisión {revision.number}</span></section>
            {main.map((stage, index) => <section className="score-stage" id={`score-stage-${index}`} key={`${stage.id}-${index}`}><header><span>Etapa {index + 1}</span><h3>{stage.title}</h3></header>{stage.blocks.map((block) => <ScriptBlock block={block} number={++mainIndex} key={block.source_key} />)}</section>)}
            {main.length === 0 && <p className="score-empty">Esta colección no contiene bloques del recorrido principal.</p>}
            {variants.length > 0 && <section className="score-variants" id="score-variants"><header><span className="kicker">FUERA DEL RECORRIDO PRINCIPAL</span><h3>Variantes</h3><p>Consultá estos caminos por separado.</p></header>{variants.map((stage, index) => <details key={`${stage.id}-${index}`}><summary>{stage.title}<span>{stage.blocks.length} bloques</span></summary>{stage.blocks.map((block, blockIndex) => <ScriptBlock key={block.source_key} block={block} number={blockIndex + 1} />)}</details>)}</section>}
          </div>
        </div>
        <details className="score-provenance"><summary>Procedencia de la colección</summary><p>Revisión fijada: {revision.id}</p><p>Catálogo SHA-256: {detail?.catalog_sha256}</p><p>Las selecciones provisionales conservan su estado. La validación automática no acredita escucha humana.</p></details>
      </>
}
