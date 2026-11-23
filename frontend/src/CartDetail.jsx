import React, { useState } from 'react'

const aed = new Intl.NumberFormat('en-AE', { style: 'currency', currency: 'AED' })

function QuantityButton({ delta, label, disabled, onAdjust }) {
  return (
    <button type="button" className="secondary quantity-button" disabled={disabled}
      aria-label={`${delta > 0 ? 'Add one' : 'Subtract one'} ${label}`} onClick={() => onAdjust(delta)}>
      {delta > 0 ? '+' : '−'}
    </button>
  )
}

function Catalogue({ products, cart, user, busy, onAdjustQuantity, onRemoveItem }) {
  const [query, setQuery] = useState('')
  const search = query.trim().toLowerCase()
  const matching = products.filter((product) =>
    [product.name, product.category, product.sku].some((value) => value.toLowerCase().includes(search)),
  )
  const sharedQuantities = cart.contributions.reduce((quantities, row) => {
    quantities.set(row.sku, (quantities.get(row.sku) ?? 0) + row.quantity)
    return quantities
  }, new Map())

  return (
    <section className="catalogue">
      <div className="page-heading">
        <h2>Catalogue</h2>
        <input type="search" aria-label="Search catalogue" placeholder="Search products"
          value={query} onChange={(event) => setQuery(event.target.value)} />
      </div>
      {matching.length === 0 && <p className="muted">No products match your search.</p>}
      <ul className="product-grid">
        {matching.map((product) => {
          const quantity = sharedQuantities.get(product.sku)
          const restricted = product.min_age > 0 && !cart.adults_only
          return (
            <li className="panel product" key={product.sku}>
              <img src={product.image_url} alt="" loading="lazy" />
              <h3>{product.name}</h3>
              <p>{aed.format(product.price_fils / 100)}</p>
              {restricted ? <small>Requires an adults-only cart</small> : (
                <>
                  <div className="quantity-controls catalogue-stepper" aria-label={`Quantity for ${product.name}`}>
                    <QuantityButton delta={-1} label={product.name} disabled={busy || !quantity}
                      onAdjust={(delta) => onAdjustQuantity(product.sku, delta)} />
                    <strong>{quantity ?? 0}</strong>
                    <QuantityButton delta={1} label={product.name} disabled={busy}
                      onAdjust={(delta) => onAdjustQuantity(product.sku, delta)} />
                  </div>
                  {quantity > 0 && cart.can_manage_access && <button className="secondary" disabled={busy}
                    aria-label={`Remove ${product.name}`} onClick={() => onRemoveItem(product.sku)}>
                    Remove
                  </button>}
                </>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

export function CartDetail({
  cart, user, members, grants, busy, products,
  onAdjustQuantity, onRemoveItem, onChangeGrant, onDeleteCart,
}) {
  const names = new Map(members.map((member) => [member.id, member.display_name]))
  names.set(user.id, user.display_name)
  const productNames = new Map(products.map((product) => [product.sku, product.name]))
  const sharedItems = [...cart.contributions.reduce((items, contribution) => {
    const item = items.get(contribution.sku) ?? { sku: contribution.sku, quantity: 0, contributors: [] }
    item.quantity += contribution.quantity
    item.contributors.push(names.get(contribution.contributor_id) ?? contribution.contributor_id)
    items.set(contribution.sku, item)
    return items
  }, new Map()).values()]

  return (
    <>
      <div className="page-heading">
        <h1>{cart.name}</h1>
        <div className="contribution-actions">
          {cart.adults_only && <span className="tag">Adults only</span>}
          {cart.can_delete_cart && <button className="secondary danger" disabled={busy}
            onClick={() => {
              if (window.confirm(`Delete “${cart.name}” and all its items?`)) onDeleteCart()
            }}>
            Delete list
          </button>}
        </div>
      </div>
      <div className="detail-grid">
        <section className="panel" aria-label="Cart items">
          <h2>Items</h2>
          {sharedItems.length === 0 ? <p className="muted">This cart is empty.</p> : (
            <ul className="rows">
              {sharedItems.map((item) => (
                <li key={item.sku}>
                  <div>
                    <strong>{productNames.get(item.sku) ?? item.sku}</strong>
                    <small>Added by {item.contributors.join(', ')}</small>
                  </div>
                  <div className="contribution-actions">
                    {cart.can_edit_items && (
                      <QuantityButton delta={-1} label={productNames.get(item.sku) ?? item.sku} disabled={busy}
                        onAdjust={(delta) => onAdjustQuantity(item.sku, delta)} />
                    )}
                    <strong>{item.quantity}</strong>
                    {cart.can_edit_items && (
                      <QuantityButton delta={1} label={productNames.get(item.sku) ?? item.sku} disabled={busy}
                        onAdjust={(delta) => onAdjustQuantity(item.sku, delta)} />
                    )}
                    {cart.can_manage_access && (
                      <button className="secondary" disabled={busy}
                        aria-label={`Remove ${productNames.get(item.sku) ?? item.sku}`}
                        onClick={() => onRemoveItem(item.sku)}>
                        Remove
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
        {cart.can_manage_access && (
          <section className="panel">
            <h2>Cart access</h2>
            <ul className="rows">
              {members.filter((member) => member.role !== 'primary' && (!cart.adults_only || member.is_adult)).map((member) => {
                const granted = grants.includes(member.id)
                return (
                  <li key={member.id}>
                    <div>
                      <strong>{member.display_name}</strong>
                      <small>{granted ? 'Has access' : 'No access'}</small>
                    </div>
                    <button className="secondary" disabled={busy}
                      aria-label={`${granted ? 'Revoke' : 'Grant'} access for ${member.display_name}`}
                      onClick={() => onChangeGrant(member.id, !granted)}>
                      {granted ? 'Revoke' : 'Grant'}
                    </button>
                  </li>
                )
              })}
            </ul>
          </section>
        )}
      </div>
      {cart.can_edit_items && <Catalogue {...{ products, cart, user, busy, onAdjustQuantity, onRemoveItem }} />}
    </>
  )
}
