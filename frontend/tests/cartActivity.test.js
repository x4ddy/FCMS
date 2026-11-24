import test from 'node:test'
import assert from 'node:assert/strict'

import { cartActivity } from '../src/cartState.js'

const names = new Map([
  ['primary', 'Priya'],
  ['member', 'Mohan'],
])
const products = new Map([['milk', 'Milk']])

test('a primary removing a member contribution is attributed to the primary', () => {
  const previous = [{ sku: 'milk', contributor_id: 'member', quantity: 2 }]
  const current = [{ sku: 'milk', contributor_id: 'member', quantity: 1 }]

  assert.deepEqual(
    cartActivity(previous, current, 'member', 'primary', names, products),
    ['Priya removed 1 × Milk'],
  )
})

test('the actor does not receive a notification about their own change', () => {
  const previous = [{ sku: 'milk', contributor_id: 'member', quantity: 2 }]
  const current = [{ sku: 'milk', contributor_id: 'member', quantity: 1 }]

  assert.deepEqual(cartActivity(previous, current, 'primary', 'primary', names, products), [])
})
