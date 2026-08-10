import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import {
  Activity,
  Archive,
  AudioLines,
  AudioWaveform,
  Check,
  ChevronRight,
  CircleStop,
  Clock3,
  Cpu,
  Download,
  FlaskConical,
  Gauge,
  GitCompareArrows,
  Library,
  Mic2,
  Plus,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  WandSparkles,
  X,
} from 'lucide-react'
import { api } from './api'
import type { ArchiveAsset, AuthStatus, Capabilities, Comparison, Job, Prosody, Segment, Voice } from './types'

type View = 'studio' | 'voices' | 'archive' | 'compare' | 'activity' | 'settings'

const DEFAULT_ES = 'Cerrá los ojos por un momento y dejá que el sonido abra un espacio tranquilo. Observá qué imagen aparece primero, sin buscarla.'
const DEFAULT_EN = 'Close your eyes for a moment and let the sound open a quiet space. Notice which image appears first, without searching for it.'

function formatDuration(value?: number | null) {
  if (value == null) return '—'
  if (value < 60) return `${value.toFixed(1)} s`
  return `${Math.floor(value / 60)}:${Math.round(value % 60).toString().padStart(2, '0')}`
}

function shortHash(value: string) {
  return value ? `${value.slice(0, 8)}…${value.slice(-5)}` : '—'
}

function engineLabel(capabilities: Capabilities | null) {
  if (!capabilities) return 'conectando'
  if (capabilities.gpu_execution_mode !== 'wrapped-worker') return capabilities.engine
  const labels: Record<string, string> = {
    standby: 'qwen · gpu bajo demanda',
    starting: 'qwen · solicitando gpu',
    ready: 'qwen · gpu lista',
    running: 'qwen · renderizando',
    cooldown: 'qwen · prioridad producción',
    unavailable: 'qwen · gpu no disponible',
    misconfigured: 'qwen · configuración incompleta',
  }
  return labels[capabilities.gpu_worker_state] ?? capabilities.engine
}

function engineClass(capabilities: Capabilities | null) {
  if (!capabilities?.engine_ready) return 'blocked'
  if (capabilities.gpu_execution_mode === 'wrapped-worker' && capabilities.gpu_worker_state === 'standby') return 'standby'
  return 'ready'
}

function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [view, setView] = useState<View>('studio')
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [voices, setVoices] = useState<Voice[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [archive, setArchive] = useState<ArchiveAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)

  const refresh = useCallback(async (quiet = false) => {
    try {
      const nextAuth = await api.authStatus()
      setAuth(nextAuth)
      if (nextAuth.required && !nextAuth.authenticated) {
        setLoading(false)
        return
      }
      const [nextCapabilities, nextVoices, nextJobs, nextArchive] = await Promise.all([
        api.capabilities(), api.voices(), api.jobs(), quiet ? Promise.resolve(null) : api.archive(),
      ])
      setCapabilities(nextCapabilities)
      setVoices(nextVoices)
      setJobs(nextJobs)
      if (nextArchive) setArchive(nextArchive)
    } catch (error) {
      if (!quiet) setToast({ kind: 'error', text: (error as Error).message })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(true), 1800)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 4200)
    return () => window.clearTimeout(timer)
  }, [toast])

  const notify = (kind: 'ok' | 'error', text: string) => setToast({ kind, text })
  const activeJobs = jobs.filter((job) => job.status === 'running' || job.status === 'queued')

  if (auth?.required && !auth.authenticated) {
    return <LoginScreen onLogin={async (token) => {
      const nextAuth = await api.login(token)
      setAuth(nextAuth)
      await refresh()
    }} />
  }

  return (
    <div className="app-shell">
      <Sidebar view={view} setView={setView} activeJobs={activeJobs.length} />
      <main className="main-shell">
        <header className="topbar">
          <div>
            <p className="eyebrow">LOCAL VOICE INSTRUMENT</p>
            <h1>{pageTitle(view)}</h1>
          </div>
          <div className="topbar-actions">
            <button className="icon-button" onClick={() => void refresh()} title="Actualizar">
              <RefreshCw size={17} />
            </button>
            <div className={`engine-pill ${engineClass(capabilities)}`}>
              <span className="status-dot" />
              {engineLabel(capabilities)}
            </div>
          </div>
        </header>

        {loading ? (
          <LoadingState />
        ) : (
          <div className="page">
            {view === 'studio' && <Studio voices={voices} jobs={jobs} notify={notify} refresh={refresh} />}
            {view === 'voices' && <Voices voices={voices} jobs={jobs} notify={notify} refresh={refresh} />}
            {view === 'archive' && <ArchivePage assets={archive} />}
            {view === 'compare' && <Compare voices={voices} jobs={jobs} notify={notify} />}
            {view === 'activity' && <ActivityPage jobs={jobs} voices={voices} notify={notify} />}
            {view === 'settings' && <Settings capabilities={capabilities} voices={voices} jobs={jobs} auth={auth} onLogout={async () => {
              const nextAuth = await api.logout()
              setAuth(nextAuth)
            }} />}
          </div>
        )}
      </main>
      {toast && (
        <div className={`toast ${toast.kind}`}>
          {toast.kind === 'ok' ? <Check size={18} /> : <X size={18} />}
          {toast.text}
        </div>
      )}
    </div>
  )
}

function LoginScreen({ onLogin }: { onLogin: (token: string) => Promise<void> }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  return <main className="login-shell"><form className="panel login-card" onSubmit={async (event) => {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      await onLogin(token)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSubmitting(false)
    }
  }}><div className="brand-mark"><AudioLines size={28} /></div><span className="kicker">PRIVATE VOICE INSTRUMENT</span><h1>Qwen Voice Lab</h1><p>Ingresá el token de esta instalación para acceder a voces, renders y controles GPU.</p><label><span>Token de acceso</span><input type="password" autoComplete="current-password" value={token} onChange={(event) => setToken(event.target.value)} autoFocus /></label>{error && <div className="warning-box">{error}</div>}<button className="primary-button" disabled={submitting || !token}>{submitting ? 'Verificando…' : 'Entrar al laboratorio'}</button></form></main>
}

function Sidebar({ view, setView, activeJobs }: { view: View; setView: (view: View) => void; activeJobs: number }) {
  const items: { id: View; label: string; icon: ReactNode }[] = [
    { id: 'studio', label: 'Estudio', icon: <AudioWaveform size={19} /> },
    { id: 'voices', label: 'Voces', icon: <Library size={19} /> },
    { id: 'archive', label: 'Archivo', icon: <Archive size={19} /> },
    { id: 'compare', label: 'Comparar', icon: <GitCompareArrows size={19} /> },
    { id: 'activity', label: 'Actividad', icon: <Activity size={19} /> },
    { id: 'settings', label: 'Sistema', icon: <Settings2 size={19} /> },
  ]
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark"><AudioLines size={24} /></div>
        <div><strong>Qwen</strong><span>Voice Lab</span></div>
      </div>
      <nav>
        {items.map((item) => (
          <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}>
            {item.icon}<span>{item.label}</span>
            {item.id === 'activity' && activeJobs > 0 && <em>{activeJobs}</em>}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <ShieldCheck size={17} />
        <div><strong>Local only</strong><span>Sin telemetría · sin APIs pagas</span></div>
      </div>
    </aside>
  )
}

function Studio({ voices, jobs, notify, refresh }: {
  voices: Voice[]; jobs: Job[]; notify: (kind: 'ok' | 'error', text: string) => void; refresh: (quiet?: boolean) => Promise<void>
}) {
  const [voiceId, setVoiceId] = useState('')
  const [language, setLanguage] = useState<'es' | 'en'>('es')
  const [title, setTitle] = useState('Nueva locución')
  const [text, setText] = useState(DEFAULT_ES)
  const [seed, setSeed] = useState(20260805)
  const [scoreMode, setScoreMode] = useState(false)
  const [segments, setSegments] = useState<Segment[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [lastJobId, setLastJobId] = useState<string | null>(() => window.localStorage.getItem('qvl.lastJobId'))

  useEffect(() => {
    if (!voiceId && voices[0]) setVoiceId(voices[0].id)
  }, [voiceId, voices])
  useEffect(() => setText(language === 'es' ? DEFAULT_ES : DEFAULT_EN), [language])
  const synthesisJobs = jobs.filter((job) => job.kind === 'synthesis')
  const lastJob = jobs.find((job) => job.id === lastJobId) ?? synthesisJobs[0]
  const selectedVoice = voices.find((voice) => voice.id === voiceId)
  useEffect(() => {
    if (lastJob?.id) window.localStorage.setItem('qvl.lastJobId', lastJob.id)
  }, [lastJob?.id])

  const buildScore = () => {
    const paragraphs = text.split(/\n\s*\n/).map((row) => row.trim()).filter(Boolean)
    setSegments(paragraphs.map((row, index) => ({
      id: `p${String(index + 1).padStart(2, '0')}`,
      text: row,
      pause_after_ms: index === paragraphs.length - 1 ? 0 : 1500,
      prosody: 'neutral',
    })))
    setScoreMode(true)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!voiceId) return notify('error', 'Primero agregá o diseñá una voz.')
    const score = scoreMode ? segments : [{ id: 'p01', text: text.trim(), pause_after_ms: 0, prosody: 'neutral' as Prosody }]
    if (!score.length || score.some((row) => !row.text.trim())) return notify('error', 'La partitura tiene un bloque vacío.')
    if (score.some((row) => row.prosody !== 'neutral') && !selectedVoice?.prosody_profile) {
      return notify('error', `${selectedVoice?.name ?? 'Esta voz'} no tiene un perfil T/S/D/R activo. Generá y validá sus cuatro variantes primero.`)
    }
    setSubmitting(true)
    try {
      const job = await api.synthesize({ title, voice_id: voiceId, language, segments: score, seed })
      setLastJobId(job.id)
      notify('ok', 'Locución encolada. La GPU procesa un trabajo por vez.')
      await refresh(true)
    } catch (error) {
      notify('error', (error as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="studio-grid">
      <form className="panel compose-panel" onSubmit={submit}>
        <div className="panel-heading">
          <div><span className="kicker">COMPOSER</span><h2>Componer una locución</h2></div>
          <LanguageSwitch value={language} onChange={setLanguage} />
        </div>

        {!voices.length ? (
          <EmptyState icon={<Mic2 />} title="Todavía no hay voces" text="Diseñá una identidad con Qwen o importá una referencia autorizada desde la biblioteca." />
        ) : (
          <>
            <div className="field-grid two">
              <label><span>Identidad vocal</span><select value={voiceId} onChange={(e) => setVoiceId(e.target.value)}>
                {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name} · {voice.kind}</option>)}
              </select></label>
              <label><span>Nombre del render</span><input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
            </div>
            <label className="text-field"><span>Texto</span>
              <textarea value={text} onChange={(e) => setText(e.target.value)} rows={scoreMode ? 4 : 10} maxLength={12000} />
              <small>{text.length.toLocaleString()} caracteres</small>
            </label>

            <div className="score-toolbar">
              <div><strong>Partitura</strong><span>Bloques, pausas y función prosódica</span></div>
              <button type="button" className={scoreMode ? 'soft-button active' : 'soft-button'} onClick={scoreMode ? () => setScoreMode(false) : buildScore}>
                {scoreMode ? <><X size={16} /> Desactivar</> : <><Plus size={16} /> Crear desde párrafos</>}
              </button>
            </div>

            {scoreMode && <>
              <ScoreEditor segments={segments} setSegments={setSegments} />
              {selectedVoice && <ProsodyReadiness voice={selectedVoice} />}
            </>}

            <div className="run-strip">
              <label><span>Seed</span><input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></label>
              <div className="run-note"><Cpu size={17} /><span>Cola serial · salida WAV · métricas por render</span></div>
              <button className="primary-button" disabled={submitting}><Sparkles size={17} />{submitting ? 'Encolando…' : 'Generar voz'}</button>
            </div>
          </>
        )}
      </form>

      <aside className="studio-side">
        <section className="panel monitor-panel">
          <div className="panel-heading compact"><div><span className="kicker">MONITOR</span><h2>Último render</h2></div>{lastJob && <StatusBadge status={lastJob.status} />}</div>
          {!lastJob ? (
            <div className="monitor-idle"><div className="orb"><AudioLines size={31} /></div><strong>El estudio está listo</strong><span>Tu siguiente render aparecerá acá en tiempo real.</span></div>
          ) : (
            <JobDetail job={lastJob} compact />
          )}
          {!!synthesisJobs.length && <div className="recent-renders"><div className="recent-heading"><strong>Locuciones recientes</strong><span>Se conservan en Actividad</span></div>{synthesisJobs.slice(0, 5).map((job) => <button key={job.id} className={lastJob?.id === job.id ? 'active' : ''} onClick={() => setLastJobId(job.id)}><span>{job.title}</span><StatusBadge status={job.status} /></button>)}</div>}
        </section>
        <section className="insight-card">
          <div className="insight-icon"><Gauge size={20} /></div>
          <div><span>Métrica central</span><strong>RTF comparable</strong><p>Menor que 1 significa que el audio se genera más rápido que su duración.</p></div>
        </section>
      </aside>
    </div>
  )
}

function ProsodyReadiness({ voice }: { voice: Voice }) {
  const profile = voice.prosody_profile
  return (
    <section className={`prosody-readiness ${profile ? 'experimental' : 'unprepared'}`}>
      <div className="prosody-readiness-icon"><AudioWaveform size={19} /></div>
      <div>
        <div className="prosody-readiness-head">
          <strong>{profile ? `${voice.name} · orquestación experimental activa` : `${voice.name} · funciones por preparar`}</strong>
          <span>{profile ? 'T · S · D · R disponibles' : 'Sin set T · S · D · R'}</span>
        </div>
        <p>Cada bloque usa su función independiente y agrega después la pausa indicada. La opción Neutra conserva la referencia vocal seleccionada.</p>
        <p>{profile
          ? `El motor cambia de referencia entre bloques. Este perfil es ${profile.status}; revisá su procedencia y validación antes de declararlo canónico.`
          : `Un bloque T/S/D/R será rechazado. Para habilitarlo con ${voice.name}, primero hay que generar y validar sus cuatro variantes de identidad.`}</p>
      </div>
    </section>
  )
}

function ScoreEditor({ segments, setSegments }: { segments: Segment[]; setSegments: (rows: Segment[]) => void }) {
  const update = (index: number, patch: Partial<Segment>) => setSegments(segments.map((row, i) => i === index ? { ...row, ...patch } : row))
  const remove = (index: number) => setSegments(segments.filter((_, i) => i !== index).map((row, i) => ({ ...row, id: `p${String(i + 1).padStart(2, '0')}` })))
  const add = () => setSegments([...segments, { id: `p${String(segments.length + 1).padStart(2, '0')}`, text: '', pause_after_ms: 0, prosody: 'neutral' }])
  return (
    <div className="score-editor">
      {segments.map((segment, index) => (
        <article className="score-row" key={segment.id}>
          <div className="score-index">{String(index + 1).padStart(2, '0')}</div>
          <textarea rows={3} value={segment.text} onChange={(e) => update(index, { text: e.target.value })} />
          <div className="score-controls">
            <label><span>Función</span><select value={segment.prosody} onChange={(e) => update(index, { prosody: e.target.value as Prosody })}>
              <option value="neutral">Neutra</option><option value="T">T · Tónica</option><option value="S">S · Subdominante</option><option value="D">D · Dominante</option><option value="R">R · Resolución</option>
            </select></label>
            <label><span>Pausa ms</span><input type="number" min="0" max="60000" step="100" value={segment.pause_after_ms} onChange={(e) => update(index, { pause_after_ms: Number(e.target.value) })} /></label>
            <button type="button" className="icon-button danger" onClick={() => remove(index)}><Trash2 size={16} /></button>
          </div>
        </article>
      ))}
      <button type="button" className="add-row" onClick={add}><Plus size={16} /> Agregar bloque</button>
    </div>
  )
}

function Voices({ voices, jobs, notify, refresh }: {
  voices: Voice[]; jobs: Job[]; notify: (kind: 'ok' | 'error', text: string) => void; refresh: (quiet?: boolean) => Promise<void>
}) {
  const [mode, setMode] = useState<'design' | 'import'>('design')
  const [busy, setBusy] = useState(false)
  const [promoting, setPromoting] = useState<string | null>(null)
  const [design, setDesign] = useState({ name: '', description: '', instruction: 'Una voz cálida, serena y luminosa, adulta, de ritmo pausado y dicción clara; íntima sin sonar susurrada.', sample_text: DEFAULT_ES, language: 'es' as 'es' | 'en', seed: 20260805 })
  const designJobs = jobs.filter((job) => job.kind === 'design').slice(0, 6)

  const submitDesign = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true)
    try {
      await api.design(design)
      notify('ok', 'Diseño encolado. Escuchalo y agregalo a tus voces si querés conservarlo.')
      setDesign({ ...design, name: '' })
      await refresh(true)
    } catch (error) { notify('error', (error as Error).message) } finally { setBusy(false) }
  }

  const submitImport = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true)
    try {
      const form = new FormData(event.currentTarget)
      await api.createVoice(form)
      notify('ok', 'Referencia importada y sellada con hash.')
      event.currentTarget.reset()
      await refresh()
    } catch (error) { notify('error', (error as Error).message) } finally { setBusy(false) }
  }

  const remove = async (voice: Voice) => {
    if (!window.confirm(`Eliminar “${voice.name}” y su referencia local? Los renders existentes se conservan.`)) return
    try { await api.deleteVoice(voice.id); notify('ok', 'Voz eliminada.'); await refresh() } catch (error) { notify('error', (error as Error).message) }
  }

  const promote = async (job: Job) => {
    setPromoting(job.id)
    try {
      const voice = await api.promoteDesign(job.id)
      notify('ok', `“${voice.name}” ya está disponible en Estudio y Comparar.`)
      await refresh()
    } catch (error) {
      notify('error', (error as Error).message)
    } finally {
      setPromoting(null)
    }
  }

  return (
    <div className="stack">
      <section className="hero-card">
        <div><span className="kicker">IDENTITY WORKBENCH</span><h2>Creá una voz o traé una referencia</h2><p>VoiceDesign produce identidades originales. La clonación conserva una voz autorizada a partir de audio local.</p></div>
        <div className="segmented"><button className={mode === 'design' ? 'active' : ''} onClick={() => setMode('design')}><WandSparkles size={16} /> Diseñar</button><button className={mode === 'import' ? 'active' : ''} onClick={() => setMode('import')}><Upload size={16} /> Importar</button></div>
      </section>

      <section className="panel identity-form">
        {mode === 'design' ? (
          <form onSubmit={submitDesign}>
            <div className="panel-heading"><div><span className="kicker">QWEN VOICE DESIGN</span><h2>Descripción de identidad</h2></div><LanguageSwitch value={design.language} onChange={(language) => setDesign({ ...design, language })} /></div>
            <div className="field-grid two"><label><span>Nombre</span><input required value={design.name} onChange={(e) => setDesign({ ...design, name: e.target.value })} placeholder="Amara Sol" /></label><label><span>Descripción corta</span><input value={design.description} onChange={(e) => setDesign({ ...design, description: e.target.value })} placeholder="Narradora bilingüe cálida" /></label></div>
            <label className="text-field"><span>Dirección vocal</span><textarea required rows={4} value={design.instruction} onChange={(e) => setDesign({ ...design, instruction: e.target.value })} /></label>
            <label className="text-field"><span>Texto de referencia</span><textarea required rows={5} value={design.sample_text} onChange={(e) => setDesign({ ...design, sample_text: e.target.value })} /></label>
            <div className="form-footer"><label><span>Seed</span><input type="number" value={design.seed} onChange={(e) => setDesign({ ...design, seed: Number(e.target.value) })} /></label><span className="fine-print">La muestra no se suma al catálogo hasta que elijas conservarla.</span><button className="primary-button" disabled={busy}><Sparkles size={17} /> Generar muestra</button></div>
          </form>
        ) : (
          <form onSubmit={submitImport}>
            <div className="panel-heading"><div><span className="kicker">VOICE CLONE</span><h2>Referencia autorizada</h2></div><div className="privacy-chip"><ShieldCheck size={16} /> Solo disco local</div></div>
            <div className="field-grid two"><label><span>Nombre</span><input name="name" required placeholder="Nombre de la voz" /></label><label><span>Idioma de referencia</span><select name="language_hint" defaultValue="multilingual"><option value="multilingual">Multilingüe</option><option value="es">Español</option><option value="en">English</option></select></label></div>
            <label><span>Descripción</span><input name="description" placeholder="Registro, timbre y uso previsto" /></label>
            <label className="text-field"><span>Transcripción exacta de la referencia</span><textarea name="reference_text" rows={5} placeholder="Mejora mucho la fidelidad del clon." /></label>
            <label className="drop-zone"><Upload size={27} /><strong>Seleccionar audio de referencia</strong><span>WAV, FLAC, MP3, M4A, WebM u OGG</span><input type="file" name="file" accept="audio/*" required /></label>
            <label><span>Etiquetas separadas por coma</span><input name="tags" placeholder="narración, bilingüe, cálida" /></label>
            <label className="consent"><input type="checkbox" name="consent_confirmed" value="true" required /><span>Confirmo que tengo autorización para usar esta voz y generar derivados.</span></label>
            <div className="form-footer"><span className="fine-print">El audio no sale de esta máquina y no se agrega al repositorio.</span><button className="primary-button" disabled={busy}><Upload size={17} /> Importar referencia</button></div>
          </form>
        )}
      </section>

      {!!designJobs.length && <section><div className="section-heading"><div><span className="kicker">VOICE DESIGN SAMPLES</span><h2>Diseños recientes</h2></div></div><div className="design-samples">{designJobs.map((job) => <DesignSampleCard key={job.id} job={job} added={!!job.result_voice_id && voices.some((voice) => voice.id === job.result_voice_id)} promoting={promoting === job.id} onPromote={() => void promote(job)} />)}</div></section>}

      <section>
        <div className="section-heading"><div><span className="kicker">CATÁLOGO LOCAL</span><h2>{voices.length} {voices.length === 1 ? 'identidad' : 'identidades'}</h2></div></div>
        {!voices.length ? <EmptyState icon={<Library />} title="Tu biblioteca está vacía" text="El primer diseño o referencia importada aparecerá acá." /> : <div className="voice-grid">{voices.map((voice) => <VoiceCard key={voice.id} voice={voice} onDelete={() => void remove(voice)} />)}</div>}
      </section>
    </div>
  )
}

function VoiceCard({ voice, onDelete }: { voice: Voice; onDelete: () => void }) {
  return <article className="voice-card">
    <div className="voice-top"><div className={`voice-avatar ${voice.kind}`}><Mic2 size={22} /></div><div><strong>{voice.name}</strong><span>{voice.kind === 'designed' ? 'Identidad diseñada' : 'Clon autorizado'}</span></div><button className="icon-button danger ghost" onClick={onDelete}><Trash2 size={15} /></button></div>
    <p>{voice.description || 'Sin descripción.'}</p>
    <audio controls preload="metadata" src={`/api/voices/${voice.id}/audio`} />
    <div className="tag-row">{voice.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
    <dl><div><dt>Referencia</dt><dd>{formatDuration(voice.duration_seconds)}</dd></div><div><dt>SHA</dt><dd>{shortHash(voice.reference_sha256)}</dd></div></dl>
  </article>
}

function DesignSampleCard({ job, added, promoting, onPromote }: { job: Job; added: boolean; promoting: boolean; onPromote: () => void }) {
  return <article className="design-sample">
    <div className="design-sample-head"><div><strong>{job.title.replace(/^Design · /, '')}</strong><span>{new Date(job.created_at).toLocaleString()}</span></div><StatusBadge status={job.status} /></div>
    {['queued', 'running'].includes(job.status) && <JobProgress job={job} />}
    {job.status === 'complete' && <><audio controls preload="metadata" src={`/api/jobs/${job.id}/audio`} /><div className="design-actions"><DownloadButton job={job} />{added ? <span className="voice-added"><Check size={15} /> Agregada a mis voces</span> : <button className="primary-button" disabled={promoting} onClick={onPromote}><Plus size={16} />{promoting ? 'Agregando…' : 'Agregar a mis voces'}</button>}</div></>}
    {job.error && <p className="error-copy">{job.error}</p>}
  </article>
}

function ArchivePage({ assets }: { assets: ArchiveAsset[] }) {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<'all' | ArchiveAsset['kind']>('all')
  const [page, setPage] = useState(0)
  const pageSize = 48
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return assets.filter((asset) => {
      const matchesKind = kind === 'all' || asset.kind === kind
      const haystack = `${asset.name} ${asset.collection} ${asset.relative_path}`.toLocaleLowerCase()
      return matchesKind && (!needle || haystack.includes(needle))
    })
  }, [assets, kind, query])
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const visible = filtered.slice(page * pageSize, (page + 1) * pageSize)
  useEffect(() => setPage(0), [query, kind])

  return <div className="stack">
    <section className="hero-card archive-hero">
      <div><span className="kicker">PRIVATE LISTENING ARCHIVE</span><h2>Archivo local</h2><p>Referencias, pruebas, segmentos y locuciones disponibles sólo en esta instancia.</p></div>
      <div className="archive-total"><strong>{assets.length}</strong><span>audios</span></div>
    </section>
    <section className="panel archive-panel">
      <div className="archive-toolbar">
        <label><span>Buscar</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Referencias, segmentos, locuciones…" /></label>
        <label><span>Tipo</span><select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
          <option value="all">Todos</option><option value="source">Fuentes</option><option value="reference">Referencias</option><option value="locution">Locuciones</option><option value="segment">Segmentos</option><option value="experiment">Experimentos</option><option value="audio">Otros</option>
        </select></label>
        <div className="archive-count">{filtered.length.toLocaleString()} resultados</div>
      </div>
      {!visible.length ? <EmptyState icon={<Archive />} title="Sin coincidencias" text="Probá con otra voz, colección o tipo de activo." /> : <div className="archive-grid">{visible.map((asset) => <article className={asset.canonical ? 'archive-card canonical' : 'archive-card'} key={asset.id}>
        <div className="archive-card-head"><div><strong>{asset.name}</strong><span>{asset.kind} · {asset.format.toUpperCase()}</span></div>{asset.canonical && <em>CANÓNICA</em>}</div>
        <p>{asset.collection}</p>
        <audio controls preload="none" src={`/api/archive/${asset.id}/audio`} />
        <div className="archive-path" title={asset.relative_path}>{asset.relative_path}</div>
      </article>)}</div>}
      {pages > 1 && <div className="archive-pages"><button className="soft-button" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>Anterior</button><span>Página {page + 1} de {pages}</span><button className="soft-button" disabled={page + 1 >= pages} onClick={() => setPage((value) => value + 1)}>Siguiente</button></div>}
    </section>
  </div>
}

function Compare({ voices, jobs, notify }: { voices: Voice[]; jobs: Job[]; notify: (kind: 'ok' | 'error', text: string) => void }) {
  const [selected, setSelected] = useState<string[]>([])
  const [text, setText] = useState(DEFAULT_ES)
  const [language, setLanguage] = useState<'es' | 'en'>('es')
  const [title, setTitle] = useState('Comparación ciega rápida')
  const [seed, setSeed] = useState(20260805)
  const [comparison, setComparison] = useState<Comparison | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => setText(language === 'es' ? DEFAULT_ES : DEFAULT_EN), [language])
  const comparisonJobs = comparison ? comparison.job_ids.map((id) => jobs.find((job) => job.id === id)).filter(Boolean) as Job[] : []
  const toggle = (id: string) => setSelected((current) => current.includes(id) ? current.filter((row) => row !== id) : current.length < 5 ? [...current, id] : current)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (selected.length < 2) return notify('error', 'Elegí al menos dos voces.'); setBusy(true)
    try { const row = await api.compare({ title, voice_ids: selected, language, text, seed }); setComparison(row); notify('ok', `${selected.length} renders comparables encolados.`) } catch (error) { notify('error', (error as Error).message) } finally { setBusy(false) }
  }
  return <div className="compare-layout">
    <form className="panel compare-builder" onSubmit={submit}>
      <div className="panel-heading"><div><span className="kicker">A/B/N LAB</span><h2>Mismo texto, mismas condiciones</h2></div><LanguageSwitch value={language} onChange={setLanguage} /></div>
      <label><span>Nombre</span><input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <div className="voice-picker"><div className="picker-head"><strong>Elegí de 2 a 5 voces</strong><span>{selected.length}/5</span></div>{voices.map((voice) => <button type="button" key={voice.id} className={selected.includes(voice.id) ? 'selected' : ''} onClick={() => toggle(voice.id)}><span className="check-box">{selected.includes(voice.id) && <Check size={14} />}</span><div><strong>{voice.name}</strong><span>{voice.kind} · {formatDuration(voice.duration_seconds)}</span></div></button>)}</div>
      <label className="text-field"><span>Texto común</span><textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} /></label>
      <div className="form-footer"><label><span>Seed compartida</span><input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></label><button className="primary-button" disabled={busy || selected.length < 2}><GitCompareArrows size={17} /> Generar comparación</button></div>
    </form>
    <section className="comparison-results">
      <div className="section-heading"><div><span className="kicker">RESULTADOS</span><h2>{comparison ? comparison.title : 'Esperando una corrida'}</h2></div></div>
      {!comparison ? <EmptyState icon={<FlaskConical />} title="Comparación controlada" text="Cada voz recibe exactamente el mismo texto, idioma y seed. La tabla expone latencia, RTF, VRAM y audio." /> : <div className="result-stack">{comparisonJobs.map((job) => <ComparisonResult key={job.id} job={job} voice={voices.find((voice) => voice.id === job.request.voice_id)} />)}</div>}
    </section>
  </div>
}

function ComparisonResult({ job, voice }: { job: Job; voice?: Voice }) {
  return <article className="result-card"><div className="result-head"><div><strong>{voice?.name ?? 'Voz'}</strong><span>{voice?.kind}</span></div><StatusBadge status={job.status} /></div>{job.status === 'complete' ? <><audio controls preload="metadata" src={`/api/jobs/${job.id}/audio`} /><DownloadButton job={job} /><Metrics metrics={job.metrics} /></> : <JobProgress job={job} />}{job.error && <p className="error-copy">{job.error}</p>}</article>
}

function ActivityPage({ jobs, voices, notify }: { jobs: Job[]; voices: Voice[]; notify: (kind: 'ok' | 'error', text: string) => void }) {
  const cancel = async (job: Job) => { try { await api.cancel(job.id); notify('ok', 'Trabajo cancelado.') } catch (error) { notify('error', (error as Error).message) } }
  return <div className="stack"><section className="stats-row"><Stat icon={<Activity />} label="Total" value={jobs.length.toString()} /><Stat icon={<Clock3 />} label="En cola" value={jobs.filter((j) => j.status === 'queued').length.toString()} /><Stat icon={<AudioWaveform />} label="Completos" value={jobs.filter((j) => j.status === 'complete').length.toString()} /><Stat icon={<Mic2 />} label="Voces" value={voices.length.toString()} /></section><section className="panel jobs-panel"><div className="panel-heading"><div><span className="kicker">RUN LEDGER</span><h2>Actividad reciente</h2></div></div>{!jobs.length ? <EmptyState icon={<Activity />} title="Sin actividad" text="Los diseños, renders y comparaciones quedarán registrados acá." /> : <div className="job-list">{jobs.map((job) => <article key={job.id} className="job-row"><div className="job-main"><div className={`job-icon ${job.kind}`}><AudioWaveform size={18} /></div><div><strong>{job.title}</strong><span>{job.id} · {new Date(job.created_at).toLocaleString()}</span></div></div><div className="job-state"><StatusBadge status={job.status} />{['queued', 'running'].includes(job.status) && <button className="icon-button danger" onClick={() => void cancel(job)}><CircleStop size={16} /></button>}</div>{job.status === 'running' && <div className="progress"><i style={{ width: `${job.progress * 100}%` }} /></div>}{job.status === 'complete' && <div className="job-output"><div className="job-audio"><audio controls preload="none" src={`/api/jobs/${job.id}/audio`} /><DownloadButton job={job} /></div><Metrics metrics={job.metrics} /></div>}{job.error && <p className="error-copy">{job.error}</p>}</article>)}</div>}</section></div>
}

function Settings({ capabilities, voices, jobs, auth, onLogout }: { capabilities: Capabilities | null; voices: Voice[]; jobs: Job[]; auth: AuthStatus | null; onLogout: () => Promise<void> }) {
  const workerState = capabilities?.gpu_execution_mode === 'wrapped-worker'
    ? engineLabel(capabilities).replace('qwen · ', '')
    : capabilities?.gpu_wrapper_verified ? 'wrapper verificado' : 'GPU directa'
  return <div className="settings-grid"><section className="panel"><div className="panel-heading"><div><span className="kicker">RUNTIME</span><h2>Capacidades</h2></div><div className={`engine-pill ${engineClass(capabilities)}`}><span className="status-dot" />{capabilities?.gpu_worker_state === 'standby' ? 'bajo demanda' : capabilities?.engine_ready ? 'listo' : 'bloqueado'}</div></div><dl className="settings-list"><Setting label="Motor" value={capabilities?.engine ?? '—'} /><Setting label="Modelo de clonación" value={capabilities?.base_model ?? '—'} /><Setting label="Modelo VoiceDesign" value={capabilities?.design_model ?? '—'} /><Setting label="Idiomas" value={capabilities?.languages.join(' · ').toUpperCase() ?? '—'} /><Setting label="Voces por comparación" value={String(capabilities?.max_comparison_voices ?? '—')} /><Setting label="Ejecución GPU" value={capabilities?.gpu_execution_mode === 'wrapped-worker' ? 'worker aislado bajo demanda' : 'en proceso'} /><Setting label="Estado GPU" value={workerState ?? '—'} /><Setting label="Proveedores pagos" value={capabilities?.paid_providers.length ? capabilities.paid_providers.join(', ') : 'Ninguno'} /></dl>{capabilities?.engine_reason && <div className="warning-box">{capabilities.engine_reason}</div>}</section><section className="panel"><div className="panel-heading"><div><span className="kicker">PRIVACIDAD</span><h2>Contrato local</h2></div><ShieldCheck size={24} /></div><div className="privacy-list"><p><Check size={17} /><span>Referencias y renders guardados solo bajo el directorio local <code>data/</code>.</span></p><p><Check size={17} /><span>Sin telemetría, analytics ni llamadas a proveedores de voz.</span></p><p><Check size={17} /><span>Salvo la voz sintética inicial, los audios permanecen excluidos de Git.</span></p><p><Check size={17} /><span>{capabilities?.gpu_execution_mode === 'wrapped-worker' ? 'La web y la API permanecen en CPU; un controlador opcional admite el worker de Qwen bajo demanda.' : 'Qwen usa directamente la GPU configurada; no hace falta un controlador externo en una máquina dedicada.'}</span></p></div><div className="storage-summary"><div><span>Identidades</span><strong>{voices.length}</strong></div><div><span>Renders</span><strong>{jobs.filter((job) => job.status === 'complete').length}</strong></div></div></section><section className="panel api-card"><div><span className="kicker">API</span><h2>Automatizable desde el primer día</h2><p>El mismo contrato usado por la UI está documentado por FastAPI.</p></div><div className="api-actions"><a href="/docs" target="_blank" rel="noreferrer">Abrir OpenAPI <ChevronRight size={17} /></a>{auth?.required && <button className="soft-button" onClick={() => void onLogout()}>Cerrar sesión</button>}</div></section></div>
}

function JobDetail({ job, compact = false }: { job: Job; compact?: boolean }) {
  return <div className={compact ? 'job-detail compact' : 'job-detail'}><div className="job-title"><strong>{job.title}</strong><span>{job.id}</span></div>{['queued', 'running'].includes(job.status) && <JobProgress job={job} />}{job.status === 'complete' && <><audio controls autoPlay={false} preload="metadata" src={`/api/jobs/${job.id}/audio`} /><DownloadButton job={job} /><Metrics metrics={job.metrics} /></>}{job.error && <p className="error-copy">{job.error}</p>}</div>
}

function DownloadButton({ job }: { job: Job }) {
  return <a className="download-button" href={`/api/jobs/${job.id}/download`} download><Download size={15} /> Descargar WAV</a>
}

function JobProgress({ job }: { job: Job }) {
  return <div className="job-progress"><div><span>{job.status === 'queued' ? 'Esperando GPU' : 'Renderizando'}</span><strong>{Math.round(job.progress * 100)}%</strong></div><div className="progress"><i style={{ width: `${job.progress * 100}%` }} /></div></div>
}

function Metrics({ metrics }: { metrics?: Job['metrics'] }) {
  if (!metrics) return null
  return <dl className="metrics"><div><dt>Duración</dt><dd>{formatDuration(metrics.duration_seconds)}</dd></div><div><dt>Generación</dt><dd>{(metrics.generation_ms / 1000).toFixed(2)} s</dd></div><div><dt>RTF</dt><dd>{metrics.rtf.toFixed(3)}</dd></div><div><dt>VRAM</dt><dd>{metrics.peak_vram_mib ? `${Math.round(metrics.peak_vram_mib)} MiB` : '—'}</dd></div></dl>
}

function LanguageSwitch({ value, onChange }: { value: 'es' | 'en'; onChange: (value: 'es' | 'en') => void }) {
  return <div className="language-switch"><button type="button" className={value === 'es' ? 'active' : ''} onClick={() => onChange('es')}>ES</button><button type="button" className={value === 'en' ? 'active' : ''} onClick={() => onChange('en')}>EN</button></div>
}

function StatusBadge({ status }: { status: Job['status'] }) { return <span className={`status-badge ${status}`}>{status === 'complete' ? 'completo' : status === 'running' ? 'renderizando' : status === 'queued' ? 'en cola' : status === 'failed' ? 'error' : 'cancelado'}</span> }
function EmptyState({ icon, title, text }: { icon: ReactNode; title: string; text: string }) { return <div className="empty-state"><div>{icon}</div><strong>{title}</strong><p>{text}</p></div> }
function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: string }) { return <div className="stat-card"><div>{icon}</div><span>{label}</span><strong>{value}</strong></div> }
function Setting({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div> }
function LoadingState() { return <div className="loading-state"><div className="loading-wave"><i /><i /><i /><i /><i /></div><span>Preparando el estudio…</span></div> }
function pageTitle(view: View) { return ({ studio: 'Estudio de síntesis', voices: 'Biblioteca de voces', archive: 'Archivo de escucha', compare: 'Laboratorio comparativo', activity: 'Actividad y métricas', settings: 'Sistema local' })[view] }

export default App
