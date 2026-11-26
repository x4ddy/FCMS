import assert from 'node:assert/strict'
import test from 'node:test'
import { cartQuantities, cartRows, withOperations, withTaps } from '../src/cartState.js'

const row = (sku, contributor_id, quantity) => ({ sku, contributor_id, quantity })

test('quantities add up across members and unsaved taps go on top', () => {
  const totals = cartQuantities([row('milk', 'a', 2), row('milk', 'b', 1), row('eggs', 'a', 1)])
  assert.deepEqual(totals, { milk: 3, eggs: 1 })
  assert.deepEqual(withTaps(totals, { milk: 1 }, { milk: 1, eggs: -5, bread: 2 }), { milk: 5, eggs: 0, bread: 2 })
  assert.deepEqual(totals, { milk: 3, eggs: 1 })
})

test('unconfirmed changes go on top in the order they were made', () => {
  const change = (delta, sku = 'milk') => ({ kind: 'adjust', sku, delta })
  assert.deepEqual(withOperations({ milk: 2 }, [change(3), { kind: 'remove', sku: 'milk' }, change(1), change(1, 'eggs')]), { milk: 1, eggs: 1 })
  assert.deepEqual(withOperations({ milk: 1 }, [change(-3), change(2)]), { milk: 2 })
})

test('cart rows follow the catalogue, hide empty ones and keep products that are no longer listed', () => {
  const products = [{ sku: 'eggs' }, { sku: 'milk' }, { sku: 'bread' }]
  const contributions = [row('milk', 'a', 1), row('gone', 'b', 1), row('eggs', 'a', 1)]
  const rows = cartRows(contributions, { milk: 1, eggs: 1, bread: 0, gone: 1 }, products)
  assert.deepEqual(rows.map((item) => item.sku), ['eggs', 'milk', 'gone'])
  assert.equal(rows[2].product, null)
  assert.deepEqual(rows[1].people, [row('milk', 'a', 1)])
})

test('an unresolved product stays listed at 0', () => {
  const rows = cartRows([], { milk: 0 }, [{ sku: 'eggs' }, { sku: 'milk' }], new Set(['milk']))
  assert.deepEqual(rows.map((item) => [item.sku, item.quantity]), [['milk', 0]])
})
