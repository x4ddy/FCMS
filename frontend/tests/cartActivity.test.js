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

// One live batch can contain changes from several people (updates are sent every ~30 s).
const both = ['member', 'primary']
const bothAdded = {
  previous: [],
  current: [
    { sku: 'milk', contributor_id: 'member', quantity: 2 },
    { sku: 'milk', contributor_id: 'primary', quantity: 1 },
  ],
}

test('when two people add to the same cart in one batch, each sees the other', () => {
  const { previous, current } = bothAdded
  assert.deepEqual(cartActivity(previous, current, 'primary', 'primary', names, products, both),
    ['Mohan added 2 × Milk'])
  assert.deepEqual(cartActivity(previous, current, 'member', 'primary', names, products, both),
    ['Priya added 1 × Milk'])
})

test('a third viewer is told about both people', () => {
  const { previous, current } = bothAdded
  assert.deepEqual(cartActivity(previous, current, 'someone-else', 'primary', names, products, both),
    ['Mohan added 2 × Milk', 'Priya added 1 × Milk'])
})

test('removals in a batch: own removals stay with the owner, others go to the other actor', () => {
  const previous = [
    { sku: 'milk', contributor_id: 'member', quantity: 3 },
    { sku: 'bread', contributor_id: 'member', quantity: 1 },
  ]
  const current = [{ sku: 'milk', contributor_id: 'member', quantity: 1 }]
  // Only the primary acted (e.g. checkout emptied part of the cart): the primary removed them.
  assert.deepEqual(cartActivity(previous, current, 'viewer', 'primary', names, new Map([['milk', 'Milk'], ['bread', 'Bread']]), ['primary']),
    ['Priya removed 2 × Milk', 'Priya removed 1 × Bread'])
  // The member acted too: their own removals are theirs.
  assert.deepEqual(cartActivity(previous, current, 'viewer', 'primary', names, new Map([['milk', 'Milk'], ['bread', 'Bread']]), both),
    ['Mohan removed 2 × Milk', 'Mohan removed 1 × Bread'])
})
