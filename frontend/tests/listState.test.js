import assert from 'node:assert/strict'
import test from 'node:test'
import {
  avatarHue, cartReadyItems, groupListItems, initials, parseQuickAdd, productSuggestions, timeAgo,
} from '../src/listState.js'

const products = [
  { sku: 'milk', name: 'Fresh Milk Full Fat, 1 L', category: 'Dairy', price_fils: 750 },
  { sku: 'oat-milk', name: 'Oat Drink', category: 'Dairy', price_fils: 1200 },
  { sku: 'eggs', name: 'Large White Eggs', category: 'Dairy', price_fils: 1275 },
  { sku: 'apples', name: 'Red Apples', category: 'Fruit', price_fils: 900 },
]
const bySku = new Map(products.map((product) => [product.sku, product]))
const item = (id, fields) => ({ id, quantity: 1, checked: false, sku: null, custom_name: null, ...fields })

test('quick add understands an optional leading quantity', () => {
  assert.deepEqual(parseQuickAdd('  3 eggs '), { quantity: 3, text: 'eggs' })
  assert.deepEqual(parseQuickAdd('2x bananas'), { quantity: 2, text: 'bananas' })
  assert.deepEqual(parseQuickAdd('7up'), { quantity: 1, text: '7up' })
  assert.deepEqual(parseQuickAdd('0 cake'), { quantity: 1, text: '0 cake' })
})

test('suggestions rank prefix matches before word and category matches', () => {
  assert.deepEqual(productSuggestions(products, 'm'), [])
  assert.deepEqual(productSuggestions(products, 'milk').map((p) => p.sku), ['milk'])
  assert.deepEqual(productSuggestions(products, 'dairy').map((p) => p.sku), ['milk', 'eggs', 'oat-milk'])
  assert.deepEqual(productSuggestions(products, 'o').length, 0)
  assert.deepEqual(productSuggestions(products, 'oa').map((p) => p.sku), ['oat-milk'])
})

test('items are grouped by catalogue order with free text last and done items separate', () => {
  const items = [
    item('a', { sku: 'apples' }),
    item('b', { custom_name: 'Candles' }),
    item('c', { sku: 'milk' }),
    item('d', { sku: 'eggs', checked: true }),
  ]
  const { groups, done } = groupListItems(items, bySku, ['Dairy', 'Fruit'])
  assert.deepEqual(groups.map(([category, rows]) => [category, rows.map((row) => row.id)]), [
    ['Dairy', ['c']], ['Fruit', ['a']], ['Other items', ['b']],
  ])
  assert.deepEqual(done.map((row) => row.id), ['d'])
})

test('only unticked catalogue items are cart-ready', () => {
  const items = [
    item('a', { sku: 'milk', quantity: 2 }),
    item('b', { custom_name: 'Candles' }),
    item('c', { sku: 'eggs', checked: true }),
    item('d', { sku: 'discontinued' }),
  ]
  const { ready, totalFils } = cartReadyItems(items, bySku)
  assert.deepEqual(ready.map((row) => row.id), ['a'])
  assert.equal(totalFils, 1500)
})

test('avatar helpers are stable', () => {
  assert.equal(initials('Morgan Lee Member'), 'MM')
  assert.equal(initials('pat'), 'P')
  assert.equal(initials(''), '?')
  assert.equal(avatarHue('user-1'), avatarHue('user-1'))
})

test('relative time', () => {
  const now = Date.parse('2026-01-01T12:00:00Z')
  assert.equal(timeAgo('2026-01-01T11:59:50Z', now), 'just now')
  assert.equal(timeAgo('2026-01-01T11:55:00Z', now), '5 minutes ago')
  assert.equal(timeAgo('2025-12-31T12:00:00Z', now), 'yesterday')
})

test('live batches never overwrite newer local state', async () => {
  const { mergeListUpdate } = await import('../src/listState.js')
  const current = { id: 'l', updated_at: '2026-01-01T10:00:05Z', items: [item('a'), item('tmp', { pending: true })] }
  const older = { id: 'l', updated_at: '2026-01-01T10:00:00Z', items: [] }
  const newer = { id: 'l', updated_at: '2026-01-01T10:00:09Z', items: [item('a'), item('b')] }
  assert.equal(mergeListUpdate(current, older), current)
  assert.equal(mergeListUpdate(current, newer, 1), current, 'skip while our own edit is saving')
  assert.deepEqual(mergeListUpdate(current, newer).items.map((row) => row.id), ['a', 'b', 'tmp'])
  assert.equal(mergeListUpdate(current, { ...newer, id: 'other' }), current)
})

test('overview cards follow details and deletions', async () => {
  const { upsertListSummary } = await import('../src/listState.js')
  let lists = [{ id: 'x', updated_at: '2026-01-01T09:00:00Z', item_count: 0, checked_count: 0 }]
  lists = upsertListSummary(lists, {
    id: 'y', updated_at: '2026-01-01T10:00:00Z',
    items: [item('1', { checked: true }), item('2'), item('3', { pending: true })],
  })
  assert.deepEqual(lists.map((list) => [list.id, list.item_count, list.checked_count]), [['y', 2, 1], ['x', 0, 0]])
  assert.equal(upsertListSummary(lists, { id: 'y', updated_at: '2026-01-01T08:00:00Z' }), lists)
  assert.deepEqual(upsertListSummary(lists, { id: 'y', deleted: true }).map((list) => list.id), ['x'])
})

test('pasted lists become bulk items', async () => {
  const { parsePastedList } = await import('../src/listState.js')
  const rows = parsePastedList('- 2 large white eggs\n\n• Red Apples\n3. birthday candles\n1) oat', products)
  assert.deepEqual(rows.map((row) => [row.quantity, row.text, row.product?.sku ?? null]), [
    [2, 'large white eggs', 'eggs'],
    [1, 'Red Apples', 'apples'],
    [1, 'birthday candles', null],
    [1, 'oat', 'oat-milk'],
  ])
})

test('only chosen items that can still be bought are sent to a cart', async () => {
  const { chosenForCart } = await import('../src/listState.js')
  const ready = [item('a', { sku: 'milk', quantity: 2 }), item('b', { sku: 'eggs' }), item('c', { sku: 'apples', pending: true })]
  const send = (...ids) => chosenForCart(ready, new Set(ids), bySku)
  assert.deepEqual(send().items, [], 'nothing is chosen to begin with')
  assert.deepEqual(send('a', 'c', 'gone').items.map((row) => row.id), ['a'])
  assert.equal(send('a').totalFils, 1500)
})

test('quick list changes are combined into one batch', async () => {
  const { applyChanges, changesBody, emptyChanges, hasChanges, queueAdd, queueRemove, queueSet } = await import('../src/listState.js')
  const items = [item('m', { sku: 'milk', quantity: 1 }), item('e', { sku: 'eggs', quantity: 1 }), item('a', { sku: 'apples' })]
  const waiting = emptyChanges('l')
  assert.equal(hasChanges(waiting), false)
  for (const quantity of [2, 3, 4]) queueSet(waiting, items[0], { quantity })
  queueAdd(waiting, items, { sku: 'milk', quantity: 2 }, 't0')        // on top of the waiting quantity
  queueAdd(waiting, items, { sku: 'eggs', quantity: 1 }, 't1')        // already on the list
  queueAdd(waiting, items, { sku: 'eggs', quantity: 1 }, 't2')
  queueAdd(waiting, items, { custom_name: 'Tape', quantity: 1 }, 't3')
  queueAdd(waiting, items, { custom_name: 'Glue', quantity: 1 }, 't4')
  queueSet(waiting, { id: 't3', pending: true }, { quantity: 6 })     // not saved yet
  assert.equal(queueRemove(waiting, { id: 't4', pending: true }), true)
  queueSet(waiting, items[2], { quantity: 9 })
  queueRemove(waiting, items[2])
  assert.equal(queueSet(waiting, { id: 'being-created', pending: true }, { quantity: 2 }), false)
  assert.deepEqual(changesBody(waiting), {
    add: [{ sku: 'eggs', quantity: 2 }, { custom_name: 'Tape', quantity: 6 }], set: [{ id: 'm', quantity: 6 }], remove: ['a'],
  })
  const shown = applyChanges(items, waiting, (body, id) => ({ id, ...body }))
  assert.deepEqual(shown.map((row) => [row.id, row.quantity]), [['m', 6], ['e', 3], ['t3', 6]])
})
