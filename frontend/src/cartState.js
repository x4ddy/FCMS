const contributionKey = ({ sku, contributor_id }) => JSON.stringify([sku, contributor_id])

function contributionQuantities(contributions = []) {
  return new Map(contributions.map((item) => [
    `${item.contributor_id}\u0000${item.sku}`,
    item.quantity,
  ]))
}

/**
 * Who made one change. Live updates arrive in batches, so several people may
 * have edited the cart since the last snapshot (`actorIds`, in order).
 * - Adding always goes to the adder's own line, so the line's owner added it.
 * - A removal is the owner's own if they acted in this batch; otherwise someone
 *   else removed it (the primary, or checkout emptying the cart).
 */
function responsibleFor(contributorId, difference, actorId, actorIds) {
  if (!actorIds) return actorId ?? contributorId
  if (difference > 0 || actorIds.includes(contributorId)) return contributorId
  const others = actorIds.filter((id) => id !== contributorId)
  return others.at(-1) ?? actorId ?? contributorId
}

export function cartActivity(previous, current, currentUserId, actorId, memberNames, productNames, actorIds = null) {
  const before = contributionQuantities(previous)
  const after = contributionQuantities(current)
  const keys = new Set([...before.keys(), ...after.keys()])

  return [...keys].flatMap((key) => {
    const separator = key.indexOf('\u0000')
    const contributorId = key.slice(0, separator)
    const sku = key.slice(separator + 1)
    const difference = (after.get(key) ?? 0) - (before.get(key) ?? 0)
    const responsibleUserId = responsibleFor(contributorId, difference, actorId, actorIds)
    if (!difference || responsibleUserId === currentUserId) return []
    const person = memberNames.get(responsibleUserId) ?? 'A household member'
    const product = productNames.get(sku) ?? sku
    return [`${person} ${difference > 0 ? 'added' : 'removed'} ${Math.abs(difference)} × ${product}`]
  })
}

function mergeContributions(contributions, edits) {
  const rows = new Map(contributions.map((row) => [contributionKey(row), row]))
  for (const { sku, contributor_id, quantity } of edits) {
    const row = { sku, contributor_id, quantity }
    if (quantity === 0) rows.delete(contributionKey(row))
    else rows.set(contributionKey(row), row)
  }
  return [...rows.values()].sort((a, b) =>
    a.sku.localeCompare(b.sku) || a.contributor_id.localeCompare(b.contributor_id),
  )
}

export function applyConfirmedEdit(detail, cartId, edit) {
  if (detail?.cart.id !== cartId || edit.revision <= detail.snapshotRevision) return detail
  const key = contributionKey(edit)
  const previous = detail.pendingEdits.get(key)
  if (previous?.revision >= edit.revision) return detail

  const pendingEdits = new Map(detail.pendingEdits).set(key, edit)
  const contributions = mergeContributions(detail.cart.contributions, [edit])
  return { ...detail, pendingEdits, cart: { ...detail.cart, contributions } }
}

export function applyCartSnapshot(detail, cart, revision) {
  if (detail?.cart.id !== cart.id || revision < detail.snapshotRevision) return detail

  const pendingEdits = new Map([...detail.pendingEdits].filter(([, edit]) => edit.revision > revision))
  const contributions = mergeContributions(cart.contributions, pendingEdits.values())
  return { ...detail, snapshotRevision: revision, pendingEdits, cart: { ...cart, contributions } }
}
