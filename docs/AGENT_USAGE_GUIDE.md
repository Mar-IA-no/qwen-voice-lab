# Qwen Voice Lab — manual operativo para agentes

Este documento describe cómo inspeccionar y operar Qwen Voice Lab sin depender de la interfaz web. Es un contrato de uso: la especificación exacta y vigente de cada payload vive en `GET /openapi.json` y en la UI de FastAPI bajo `/docs`.

## 1. Frontera del producto

Qwen Voice Lab crea identidades sintéticas originales, importa referencias vocales autorizadas, sintetiza texto, renderiza partituras por bloques, compara voces y conserva trabajos y métricas. No es un asistente conversacional, un grabador de participantes ni un scheduler general de GPU.

Reglas no negociables:

- no importar ni clonar una voz humana sin consentimiento verificable;
- no enviar referencias, renders o catálogos a proveedores externos;
- no incorporar audios privados, pesos, bases de datos, perfiles prosódicos ni `.env` a Git;
- no asumir que T/S/D/R son estilos universales: pertenecen a un perfil completo de una identidad concreta;
- no declarar una voz o perfil como canónico sólo porque renderiza;
- en infraestructura compartida, no iniciar CUDA fuera del wrapper de admisión configurado;
- tratar una preempción como un fallo explícito y reintentable, nunca como permiso para saltar la prioridad del sistema.

La única identidad incluida en una instalación nueva es **Amara Sol**, una voz sintética original CC0. Ninguna identidad humana privada pertenece al repositorio público.

## 2. Modelo mental

```text
identidad vocal ─┐
texto/partitura ─┼─> trabajo asíncrono ─> WAV + métricas + hash
idioma + seed ───┘          │
                            └─ queued | running | complete | failed | cancelled
```

Hay dos clases de identidad:

- `designed`: muestra original producida por VoiceDesign y promovida explícitamente;
- `clone`: referencia local de una voz para la que el operador confirmó permiso.

Una muestra de VoiceDesign no entra automáticamente al catálogo. Primero se escucha y luego se promueve con `POST /api/jobs/{job_id}/promote`.

## 3. Descubrimiento y autenticación

Use una URL base suministrada por el operador; los ejemplos usan `http://127.0.0.1:8788` sólo como valor local convencional.

1. Consulte `GET /api/auth/status`.
2. Si `required` es `true`, obtenga el token por el canal autorizado y envíe:

   ```http
   POST /api/auth/session
   Content-Type: application/json

   {"token":"<installation-token>"}
   ```

3. Conserve la cookie HttpOnly de sesión en el cliente HTTP. No registre el token ni la cookie en logs, prompts o archivos.
4. Consulte `GET /api/capabilities` antes de crear trabajo.

Campos de capacidad relevantes:

- `engine`, `engine_ready`, `engine_reason`;
- `languages`, `max_text_chars`, `max_segments`, `max_comparison_voices`;
- `gpu_execution_mode`, `gpu_worker_state`, `gpu_worker_reason`;
- modelos Base y VoiceDesign;
- `paid_providers`, que debe permanecer vacío.

Estados habituales del worker compartido:

| Estado | Interpretación | Acción del agente |
|---|---|---|
| `standby` | API activa; Qwen se cargará al primer trabajo | Puede encolar |
| `starting` | El wrapper está solicitando la GPU | Espere y observe el trabajo |
| `ready` | Worker admitido y modelo disponible | Puede encolar |
| `running` | Hay un render activo | Puede encolar; la cola es serial |
| `cooldown` | Prioridad superior preemptó o bloqueó el worker | No martille; reintente más tarde |
| `unavailable` | El scheduler no admite el worker | Informe el motivo al operador |
| `misconfigured` | Falta contrato del wrapper | No intente CUDA por otra vía |

`GET /api/health` prueba la API CPU; no demuestra que un modelo esté residente ni que la GPU esté libre.

## 4. Consultar voces

`GET /api/voices` devuelve el catálogo sin rutas absolutas. Conserve el `id`, no derive rutas desde el nombre.

Antes de una síntesis revise:

- `language_hint` como orientación, no como garantía perceptual;
- `kind` (`designed` o `clone`);
- `reference_sha256` para trazabilidad;
- `prosody_profile`, que será `null` si T/S/D/R no está disponible.

El audio de referencia puede escucharse mediante `GET /api/voices/{voice_id}/audio`. No lo redistribuya por el solo hecho de que el endpoint sea accesible.

## 5. Diseñar una identidad sintética

Encole una muestra:

```http
POST /api/designs
Content-Type: application/json

{
  "name": "Brisa Ámbar",
  "description": "Voz sintética adulta para narración serena",
  "instruction": "Una voz adulta, cálida y clara, de ritmo pausado y dicción precisa.",
  "sample_text": "Cada voz abre una forma distinta de recorrer el mismo relato.",
  "language": "es",
  "seed": 20260809
}
```

La respuesta `202` es un trabajo, no una voz. Espere un estado terminal. Si queda `complete`:

1. escuche `GET /api/jobs/{job_id}/audio`;
2. revise texto, pronunciación, identidad y artefactos;
3. promueva sólo una muestra aceptada con `POST /api/jobs/{job_id}/promote`;
4. confirme que el `VoiceView` devuelto aparece en `GET /api/voices`.

La promoción es explícita e idempotente. Una generación técnicamente correcta no equivale a aprobación perceptual.

## 6. Importar una referencia autorizada

`POST /api/voices` usa `multipart/form-data` y requiere:

- `file`: WAV, FLAC, MP3, M4A, WebM u OGG;
- `name`;
- `consent_confirmed=true`;
- opcionales: `description`, `language_hint`, `reference_text`, `tags`.

Ejemplo conceptual:

```bash
curl -b session.cookies -X POST "$BASE/api/voices" \
  -F 'file=@authorized-reference.wav' \
  -F 'name=Referencia autorizada' \
  -F 'description=Uso interno acordado con la persona' \
  -F 'language_hint=es' \
  -F 'reference_text=Transcripción exacta del audio de referencia.' \
  -F 'tags=narración,autorizada' \
  -F 'consent_confirmed=true'
```

La confirmación registra una decisión del operador; no crea el permiso. Ante dudas, no importar. La transcripción exacta mejora el condicionamiento y debe corresponder al audio, sin “correcciones” editoriales.

## 7. Sintetizar texto neutro

Toda síntesis es una partitura de uno o más segmentos. Para un texto simple use un único bloque neutro:

```http
POST /api/jobs
Content-Type: application/json

{
  "title": "Prueba de dicción ES",
  "voice_id": "<voice-id>",
  "language": "es",
  "segments": [
    {
      "id": "p01",
      "text": "El sonido dejó una línea clara sobre el paisaje.",
      "pause_after_ms": 0,
      "prosody": "neutral"
    }
  ],
  "seed": 20260809
}
```

La respuesta `202` entrega un `job_id`. No interprete esa respuesta como audio terminado.

## 8. Renderizar una partitura

Cada bloque contiene texto exacto, una pausa posterior en milisegundos y una función prosódica:

```json
{
  "title": "Recorrido en tres bloques",
  "voice_id": "<voice-id-con-perfil-completo>",
  "language": "es",
  "segments": [
    {"id":"p01","text":"Abrí un espacio de escucha.","pause_after_ms":1200,"prosody":"T"},
    {"id":"p02","text":"Observá qué cambia cuando el ritmo se abre.","pause_after_ms":2400,"prosody":"D"},
    {"id":"p03","text":"Volvé despacio al entorno.","pause_after_ms":0,"prosody":"R"}
  ],
  "seed": 20260809
}
```

Semántica operativa:

- `pause_after_ms` es silencio **después** del bloque;
- `neutral` usa la referencia principal de la identidad;
- T/S/D/R seleccionan referencias distintas del perfil de esa misma identidad;
- un bloque funcional para una voz sin `prosody_profile` se rechaza antes de encolarse;
- el perfil debe declarar las cuatro funciones; no mezcle referencias entre identidades;
- los IDs de segmento son únicos y estables dentro de la partitura.

La notación T/S/D/R se inspira en funciones tonales para organizar continuidad, apertura, tensión y cierre. No garantiza por sí sola una intención expresiva: el resultado requiere escucha humana.

## 9. Comparar voces de forma controlada

`POST /api/comparisons` acepta de dos a cinco voces. El servicio crea trabajos normales con el mismo texto, idioma y seed:

```json
{
  "title": "Comparación de timbres",
  "voice_ids": ["<voice-a>", "<voice-b>"],
  "language": "es",
  "text": "La tarde dejó una línea dorada sobre el agua.",
  "seed": 20260809
}
```

Conserve el `comparison_id` y consulte `GET /api/comparisons/{comparison_id}`. Compare sólo lo que la corrida controla. Igualar texto, idioma y seed no iguala duración de referencia, calidad del clon ni perfil prosódico.

## 10. Observar, cancelar y recuperar resultados

Consulte `GET /api/jobs/{job_id}` con backoff moderado. Una pauta razonable para una UI o agente interactivo es comenzar en 1–2 segundos y reducir frecuencia durante esperas largas. No cree otro trabajo sólo porque el modelo todavía está cargando.

Estados terminales:

- `complete`: audio y métricas disponibles;
- `failed`: conserve `error` y decida si el motivo es reintentable;
- `cancelled`: no espere audio.

`DELETE /api/jobs/{job_id}` solicita cancelación de un trabajo en cola o activo. No borra el registro histórico.

Recuperación:

- reproducir: `GET /api/jobs/{job_id}/audio`;
- descargar con nombre: `GET /api/jobs/{job_id}/download`;
- listar actividad: `GET /api/jobs?limit=100`;
- exportar metadatos no-audio: `GET /api/catalog/export`.

Nunca invente una URL de archivo local. Use sólo los endpoints devueltos por el contrato.

## 11. Leer las métricas

Un trabajo completo puede informar:

| Campo | Lectura correcta |
|---|---|
| `load_ms` | tiempo de carga atribuible a esa ejecución |
| `generation_ms` | tiempo de síntesis registrado por el motor |
| `first_audio_ms` | demora hasta el primer audio según la instrumentación disponible |
| `duration_seconds` | duración del WAV final, incluidas las pausas renderizadas |
| `rtf` | tiempo de generación / duración de salida; menor que 1 es más rápido que tiempo real |
| `peak_vram_mib` | pico observado; puede ser `null` fuera de CUDA |
| `output_bytes` | tamaño del resultado |
| `output_sha256` | identidad de contenido del WAV |

No compare métricas de `mock` con Qwen como si midieran el mismo motor. Tampoco generalice un único render a todas las longitudes, idiomas o estados de residencia del modelo.

## 12. Fallos y política de reintento

Clasifique antes de reintentar:

- **validación 4xx:** corrija el payload; no repita igual;
- **voz inexistente 404:** refresque catálogo y solicite un ID válido;
- **prosodia no soportada 409:** use `neutral` o una identidad con perfil completo; no fabrique el perfil;
- **preempción/cooldown:** preserve el trabajo fallido, espere el fin del cooldown y cree un nuevo pedido explícito si sigue siendo necesario;
- **worker mal configurado/no disponible:** escale al operador; no ejecute el modelo directamente;
- **cancelado por el usuario:** no reintente sin una nueva intención explícita.

Una preempción puede terminar la generación activa, pero la API, el catálogo y los WAV ya completados deben seguir disponibles.

## 13. Checklist previo a una operación

- [ ] Tengo una URL base autorizada y no la publicaré.
- [ ] `auth/status` y `capabilities` responden.
- [ ] El motor y su estado permiten encolar.
- [ ] La identidad existe y su uso está autorizado.
- [ ] El idioma, texto y seed están fijados.
- [ ] Si uso T/S/D/R, la voz expone un perfil completo.
- [ ] La partitura no contiene bloques vacíos y las pausas están en milisegundos.
- [ ] Registraré `job_id`, estado terminal, hash y métricas.
- [ ] Recuperaré el audio mediante `/download`, no desde rutas internas.

## 14. Superficie API resumida

| Método y ruta | Uso |
|---|---|
| `GET /api/auth/status` | estado de sesión |
| `POST/DELETE /api/auth/session` | abrir/cerrar sesión |
| `GET /api/health` | vida de la API CPU |
| `GET /api/capabilities` | capacidades y estado del worker |
| `GET /api/voices` | catálogo y prosodia disponible |
| `POST /api/voices` | importar referencia autorizada |
| `GET /api/voices/{id}/audio` | escuchar la referencia de una identidad |
| `DELETE /api/voices/{id}` | retirar una identidad del catálogo |
| `POST /api/designs` | generar muestra VoiceDesign |
| `POST /api/jobs/{id}/promote` | promover diseño al catálogo |
| `POST /api/jobs` | sintetizar una partitura |
| `GET/DELETE /api/jobs/{id}` | consultar o cancelar trabajo |
| `GET /api/jobs/{id}/audio` | streaming WAV |
| `GET /api/jobs/{id}/download` | descarga WAV |
| `POST /api/comparisons` | crear comparación controlada |
| `GET /api/comparisons/{id}` | recuperar comparación y trabajos |
| `GET /api/archive` | listar archivo privado sin rutas host |
| `GET /api/archive/{id}/audio` | escuchar activo archivado |
| `GET /api/catalog/export` | exportar metadatos no-audio |

Ante una diferencia entre este manual y `/openapi.json`, rige el contrato de la instalación activa. Documente la divergencia antes de automatizarla.
