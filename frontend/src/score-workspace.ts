import type { ScoreWorkspaceBlock, ScoreWorkspaceCatalog } from './types'

export function initialScoreWorkspace(catalog: ScoreWorkspaceCatalog): string | null {
  const id = catalog.default_workspace_id
  return id && catalog.workspaces.some((workspace) => workspace.id === id && workspace.is_default) ? id : null
}

export function scoreStages(blocks: ScoreWorkspaceBlock[], role: 'main' | 'variant') {
  const ordered = blocks.filter((block) => block.role === role).slice().sort((a, b) => a.order - b.order)
  const stages: { id: string; title: string; blocks: ScoreWorkspaceBlock[] }[] = []
  for (const block of ordered) {
    const previous = stages[stages.length - 1]
    if (previous?.id === block.stage_id) previous.blocks.push(block)
    else stages.push({ id: block.stage_id, title: block.stage_title, blocks: [block] })
  }
  return stages
}

export function scoreSeconds(ms: number) { if (!Number.isFinite(ms)) return 'No disponible'; return `${new Intl.NumberFormat('es', { maximumFractionDigits: 3 }).format(ms / 1000)} s` }
