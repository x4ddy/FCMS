import assert from 'node:assert/strict'
import test from 'node:test'
import { applyAdjustment, cartQuantities, cartRows, withTaps } from '../src/cartState.js'

const row = (sku, contributor_id, quantity) => ({ sku, contributor_id, quantity })

test('quantities add up across members and unsaved taps go on top', () => {
  const totals = cartQuantities([row('milk', 'a', 2), row('milk', 'b', 1), row('eggs', 'a', 1)])
  assert.deepEqual(totals, { milk: 3, eggs: 1 })
  assert.deepEqual(withTaps(totals, { milk: 1 }, { milk: 1, eggs: -5, bread: 2 }), { milk: 5, eggs: 0, bread: 2 })
  assert.deepEqual(totals, { milk: 3, eggs: 1 })
})

test('adding goes to your own line', () => {
  assert.deepEqual(applyAdjustment([row('milk', 'b', 1)], 'milk', 2, 'a'), [row('milk', 'b', 1), row('milk', 'a', 2)])
  assert.deepEqual(applyAdjustment([row('milk', 'a', 1)], 'milk', 2, 'a'), [row('milk', 'a', 3)])
})

test('removing takes your own units first and then other members in order', () => {
  const before = [row('milk', 'c', 2), row('milk', 'b', 1), row('milk', 'z', 1), row('eggs', 'b', 4)]
  assert.deepEqual(applyAdjustment(before, 'milk', -3, 'z'), [row('milk', 'c', 1), row('eggs', 'b', 4)])
  assert.equal(before[0].quantity, 2)
})

test('cart rows follow the catalogue, hide empty ones and keep products that are no longer listed', () => {
  const products = [{ sku: 'eggs' }, { sku: 'milk' }, { sku: 'bread' }]
  const contributions = [row('milk', 'a', 1), row('gone', 'b', 1), row('eggs', 'a', 1)]
  const rows = cartRows(contributions, { milk: 1, eggs: 1, bread: 0, gone: 1 }, products)
  assert.deepEqual(rows.map((item) => item.sku), ['eggs', 'milk', 'gone'])
  assert.equal(rows[2].product, null)
  assert.deepEqual(rows[1].people, [row('milk', 'a', 1)])
})
