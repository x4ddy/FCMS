import assert from 'node:assert/strict'
import test from 'node:test'
import { logoutSession, onAccessTokenChanged, restoreSession } from '../src/api.js'

const reply = (status, body) => async () => ({ status, ok: status < 400, json: async () => body })

function listen() {
  const seen = []
  onAccessTokenChanged((token) => seen.push(token))
  return seen
}

test('a temporary server error while renewing does not sign the user out', async () => {
  for (const status of [429, 500, 503]) {
    const seen = listen()
    globalThis.fetch = reply(status, { detail: 'Try again shortly.' })
    await assert.rejects(restoreSession(), (error) => error.status === status)
    assert.deepEqual(seen, [], `status ${status}`)
  }
  const seen = listen()
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch') }
  await assert.rejects(restoreSession())
  assert.deepEqual(seen, [])
})

test('a definite 401 while renewing signs the user out', async () => {
  const seen = listen()
  globalThis.fetch = reply(401, { detail: 'Invalid or expired session' })
  await assert.rejects(restoreSession(), (error) => error.status === 401)
  assert.deepEqual(seen, [null])
})

test('logout only reports signed out when the server confirms it', async () => {
  for (const failing of [reply(503, { detail: 'x' }), async () => { throw new TypeError('Failed to fetch') }]) {
    const seen = listen()
    globalThis.fetch = failing
    await assert.rejects(logoutSession('token'), /still signed in/)
    assert.deepEqual(seen, [])
  }
  for (const done of [reply(204), reply(401, { detail: 'ended' })]) {
    const seen = listen()
    globalThis.fetch = done
    await logoutSession('token')
    assert.deepEqual(seen, [null])
  }
})
