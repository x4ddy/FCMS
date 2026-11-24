import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { isUnconfirmed, readAttempt, saveAttempt } from '../src/checkoutState.js'

test('a pending submission preserves the quote and key and is scoped to the signed-in user', () => {
  const values = new Map()
  const storage = { getItem: (key) => values.get(key), setItem: (...pair) => values.set(...pair),
    removeItem: (key) => values.delete(key) }
  const submitted = { key: 'attempt', cart: { kind: 'personal' }, body: { quote_id: 'quote', mock_outcome: 'decline' } }
  saveAttempt('owner', submitted, storage)
  assert.deepEqual(readAttempt('owner', storage), submitted)
  assert.equal(readAttempt('member', storage), null)
  saveAttempt('owner', null, storage)
  assert.equal(readAttempt('owner', storage), null)
})

test('uncertain results preserve the key and a known decline starts a fresh attempt', () => {
  assert.equal(isUnconfirmed(new Error('network interrupted')), true)
  assert.equal(isUnconfirmed({ status: 503 }), true)
  assert.equal(isUnconfirmed({ status: 401 }), true)
  assert.equal(isUnconfirmed({ status: 429 }), true)
  assert.equal(isUnconfirmed({ status: 409, code: 'IDEMPOTENCY_KEY_REUSED' }), true)
  assert.equal(isUnconfirmed({ status: 402, code: 'MOCK_PAYMENT_DECLINED' }), false)
  assert.equal(isUnconfirmed({ status: 409, code: 'QUOTE_STALE' }), false)
})
