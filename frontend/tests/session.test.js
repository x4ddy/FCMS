import assert from 'node:assert/strict'
import test from 'node:test'
import { householdApi, logoutSession, onAccessTokenChanged, restoreSession, signInWithChildCode } from '../src/api.js'

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

test('child signup retries the same browser attempt until a session is received', async () => {
  const stored = new Map()
  globalThis.sessionStorage = {
    getItem: (key) => stored.get(key), setItem: (key, value) => stored.set(key, value),
    removeItem: (key) => stored.delete(key),
  }
  const keys = []
  const seen = listen()
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/v1/auth/child')
    assert.deepEqual(JSON.parse(options.body), { code: 'test-code', display_name: 'Robin' })
    keys.push(options.headers['Idempotency-Key'])
    if (keys.length === 1) throw new TypeError('Failed to fetch')
    return reply(200, { access_token: 'child-token' })()
  }
  await assert.rejects(signInWithChildCode('test-code', 'Robin'))
  await signInWithChildCode('test-code', 'Robin')
  assert.ok(keys[0])
  assert.equal(keys[0], keys[1])
  assert.equal(stored.size, 0)
  assert.deepEqual(seen, ['child-token'])
})

test('Add Child retries a 503 with the original creation key', async () => {
  const keys = []
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/v1/households/family/child-codes')
    keys.push(options.headers['Idempotency-Key'])
    return keys.length === 1 ? reply(503, { detail: 'Try again shortly.' })() : reply(201, { invitation_code: 'aB12cD' })()
  }
  const result = await householdApi('admin-token', 'family').createChildCode(undefined, 'creation-key')
  assert.equal(result.invitation_code, 'aB12cD')
  assert.deepEqual(keys, ['creation-key', 'creation-key'])
})

test('checkout retries a lost claim reply with the same key and uses it to renew and release', async () => {
  const calls = []
  globalThis.fetch = async (path, options) => {
    calls.push([path, options.method, options.headers['Idempotency-Key']])
    if (calls.length === 1) throw new TypeError('Failed to fetch')
    return reply(options.method === 'DELETE' ? 204 : 200, { id: 'cart' })()
  }
  const checkout = householdApi('token', 'family')
  const path = '/api/v1/households/family/carts/cart/checkout'
  await checkout.startCheckout('cart', 'claim-key')
  await checkout.renewCheckout('cart', 'claim-key')
  await checkout.leaveCheckout('cart', 'claim-key')
  assert.deepEqual(calls, [
    [path, 'POST', 'claim-key'], [path, 'POST', 'claim-key'],
    [`${path}?renew=true`, 'POST', 'claim-key'], [path, 'DELETE', 'claim-key'],
  ])
})
