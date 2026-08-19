import { describe, expect, it } from 'vitest'

import { LatestRequest } from './latest-request'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}

describe('LatestRequest', () => {
  it('rejects an older same-project response after a successful revision save', async () => {
    const gate = new LatestRequest()
    const delayedRevisionFour = deferred<number>()
    let visibleRevision = 4

    const oldGeneration = gate.begin()
    const oldLoad = (async () => {
      const revision = await delayedRevisionFour.promise
      if (gate.isCurrent(oldGeneration)) visibleRevision = revision
    })()

    // The revision-five POST has committed. Invalidate every GET that began before it.
    gate.invalidate()
    visibleRevision = 5
    delayedRevisionFour.resolve(4)
    await oldLoad

    expect(visibleRevision).toBe(5)
  })
})
