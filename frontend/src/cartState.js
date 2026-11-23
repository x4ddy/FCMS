const contributionKey = ({ sku, contributor_id }) => JSON.stringify([sku, contributor_id])

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
