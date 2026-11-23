import assert from 'node:assert/strict'
import test from 'node:test'
import { applyCartSnapshot, applyConfirmedEdit } from '../src/cartState.js'

const cartId = 'regular-cart'
const ownMilk = (quantity) => ({ sku: 'milk', contributor_id: 'user-primary', quantity })
const childMilk = { sku: 'milk', contributor_id: 'user-child', quantity: 2 }
const initial = () => ({
  cart: { id: cartId, contributions: [] }, snapshotRevision: 0, pendingEdits: new Map(),
})

test('confirmed changes appear only in the editing view until a shared snapshot arrives', () => {
  let editor = applyConfirmedEdit(initial(), cartId, { ...ownMilk(4), revision: 2 })
  const viewer = initial()
  assert.deepEqual(editor.cart.contributions, [ownMilk(4)])
  assert.deepEqual(viewer.cart.contributions, [])

  editor = applyCartSnapshot(editor, { id: cartId, contributions: [childMilk] }, 1)
  assert.deepEqual(editor.cart.contributions, [childMilk, ownMilk(4)])
  const finalCart = { id: cartId, contributions: [childMilk, ownMilk(4)] }
  editor = applyCartSnapshot(editor, finalCart, 2)
  assert.deepEqual(editor.cart, applyCartSnapshot(viewer, finalCart, 2).cart)
  assert.equal(editor.pendingEdits.size, 0)
})

test('a stale batch or acknowledgement cannot resurrect a removed contribution', () => {
  let editor = applyCartSnapshot(initial(), { id: cartId, contributions: [ownMilk(4)] }, 2)
  editor = applyConfirmedEdit(editor, cartId, { ...ownMilk(0), revision: 4 })
  assert.deepEqual(editor.cart.contributions, [])
  editor = applyCartSnapshot(editor, { id: cartId, contributions: [ownMilk(6)] }, 3)
  assert.deepEqual(editor.cart.contributions, [])
  editor = applyCartSnapshot(editor, { id: cartId, contributions: [] }, 4)
  assert.equal(editor.pendingEdits.size, 0)
  assert.equal(applyConfirmedEdit(editor, cartId, { ...ownMilk(6), revision: 3 }), editor)
})

test('removing another user’s item preserves your pending quantity for the same product', () => {
  const oldCart = { id: cartId, contributions: [childMilk] }
  let editor = applyCartSnapshot(initial(), oldCart, 1)
  editor = applyConfirmedEdit(editor, cartId, { ...ownMilk(5), revision: 2 })
  editor = applyConfirmedEdit(editor, cartId, { ...childMilk, quantity: 0, revision: 3 })
  editor = applyCartSnapshot(editor, oldCart, 1)
  assert.deepEqual(editor.cart.contributions, [ownMilk(5)])
})
