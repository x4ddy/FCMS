import React, { useState } from 'react'
import { Avatar, confirmAction, Empty, Icon, money, ProductImage, Stepper } from './ui.jsx'

function Tick({ label, checked, onChange }) {
  return <label className="tick">
    <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} aria-label={label} />
    <span aria-hidden="true"><Icon name="check" size={14} /></span>
  </label>
}

function NewCart({ busy, canAdultsOnly, onCreate }) {
  const [name, setName] = useState('')
  const [adultsOnly, setAdultsOnly] = useState(false)
  return <details className="fold">
    <summary>Create another cart</summary>
    <form onSubmit={(event) => {
      event.preventDefault()
      if (!name.trim()) return
      event.currentTarget.closest('details').open = false
      onCreate(name.trim(), adultsOnly)
      setName('')
      setAdultsOnly(false)
    }}>
      <div className="inline-form">
        <input aria-label="New cart name" placeholder="Cart name" required maxLength="100" value={name}
          onChange={(event) => setName(event.target.value)} />
        <button disabled={busy || !name.trim()}>Create</button>
      </div>
      {canAdultsOnly && <label className="checkbox-label"><input type="checkbox" checked={adultsOnly}
        onChange={(event) => setAdultsOnly(event.target.checked)} /> Adults only</label>}
    </form>
  </details>
}

function CartRow({ row, cart, user, names, isPrimary, selectable, selected, onSelect, locked, canRemove, busy, onAdjust, onRemove }) {
  const label = row.product?.name ?? row.sku
  return <li className={`cart-row${selected ? ' selected' : ''}`}>
    {selectable && <Tick label={`Select ${label} for checkout`} checked={selected} onChange={onSelect} />}
    <ProductImage product={row.product} />
    <div className="cart-row-text">
      <strong>{label}</strong>
      <span className="people">{row.people.map((item) => {
        const name = names.get(item.contributor_id) ?? 'Member'
        return <span key={item.contributor_id}><Avatar id={item.contributor_id} name={name} size="tiny" />{item.contributor_id === user.id ? 'You' : name} × {item.quantity}</span>
      })}</span>
    </div>
    <div className="cart-row-end">
      <span className="price">{row.product ? money(row.product.price_fils * row.quantity) : 'Unavailable'}</span>
      <span className="cart-row-actions">
        {locked ? <strong className="quantity">× {row.quantity}</strong> : cart.can_edit_items && row.product && <Stepper label={label} quantity={row.quantity}
          canRemove={canRemove} removeHint="Only the admin can remove what others added" busy={busy} onAdjust={onAdjust} />}
        {isPrimary && !locked && <button className="icon-button" aria-label={`Remove ${label}`} title="Remove" onClick={onRemove}>
          <Icon name="trash" size={18} /></button>}
      </span>
    </div>
  </li>
}

export function CartView({
  carts, cart, cartId, rows, waitingRows, user, household, members, names, grants, selected, busy, unresolved, live, canAdultsOnly,
  checkoutBy, mine, canRemove, onEndCheckout, onSelectCart, onToggle, onSelectAll, onAdjust, onRemove, onChangeGrant, onDeleteCart, onCreateCart, onCheckout,
}) {
  const isPrimary = household.role === 'primary'
  const available = rows.filter((row) => row.product)
  const chosen = available.filter((row) => selected.has(row.sku))
  const sum = (list) => list.reduce((total, row) => total + row.product.price_fils * row.quantity, 0)
  const units = (list) => list.reduce((total, row) => total + row.quantity, 0)
  const locked = Boolean(checkoutBy)
  const selectable = Boolean(cart?.can_checkout) && !locked
  const others = members.filter((member) => member.role !== 'primary' && (!cart?.adults_only || member.is_adult))
  // Members whose access is being saved. Their switch shows the saved state only, with a spinner meanwhile.
  const [savingAccess, setSavingAccess] = useState(() => new Set())
  async function changeAccess(member, grant) {
    setSavingAccess((current) => new Set(current).add(member))
    try { await onChangeGrant(member, grant) }
    finally { setSavingAccess((current) => { const next = new Set(current); next.delete(member); return next }) }
  }

  return <>
    <div className="drawer-body">
      <div className="cart-tabs" role="group" aria-label="Household carts">
        {carts.map((item) => <button key={item.id} aria-pressed={item.id === cartId} className="chip"
          onClick={() => onSelectCart(item.id)}>
          {item.adults_only && <Icon name="lock" size={14} />}{item.name}
          {item.id === cartId && rows.length > 0 ? ` (${units(rows)})` : ''}
        </button>)}
      </div>
      {isPrimary && <NewCart busy={busy} canAdultsOnly={canAdultsOnly} onCreate={onCreateCart} />}
      {carts.length === 0 ? <Empty icon="🛒" title="No cart yet">
        <p>{isPrimary ? 'Create a cart above to start.' : 'Ask the household admin for access to a cart.'}</p>
      </Empty> : !cart ? <div aria-busy="true">{[0, 1, 2].map((key) => <div className="cart-row skeleton" key={key} />)}</div> : <>
        <div className="section-head cart-title">
          <h3>{cart.name}</h3>
          <span className={`live-badge ${live ? 'on' : ''}`} title="Changes from others appear within about 20 seconds">
            <span aria-hidden="true" />{live && 'Live'}</span>
        </div>
        {locked && <div className="banner row" role="status">
          <span><strong>{checkoutBy} {mine ? 'are' : 'is'} checking out this cart</strong>
            <p>Anything added now waits for the next cart.</p></span>
          {(mine || isPrimary) && <button className="secondary" disabled={busy} onClick={onEndCheckout}>End checkout</button>}
        </div>}
        {rows.length === 0 ? <Empty icon="🧺" title="This cart is empty">
          <p>Add products and everyone with access will see them.</p>
        </Empty> : <>
          {selectable && available.length > 0 && <div className="select-all">
            <Tick label="Select all items" checked={chosen.length === available.length} onChange={onSelectAll} />
            <span aria-hidden="true">Select all items</span>
          </div>}
          <ul className="cart-rows">{rows.map((row) => <CartRow key={row.sku} row={row} cart={cart} user={user} names={names}
            isPrimary={isPrimary} selectable={selectable && Boolean(row.product)} selected={selectable && selected.has(row.sku)}
            onSelect={() => onToggle(row.sku)} locked={locked} canRemove={canRemove(row.sku)} busy={unresolved.has(row.sku)}
            onAdjust={(delta) => onAdjust(row.product, delta)} onRemove={() => onRemove(row)} />)}</ul>
        </>}
        {waitingRows.length > 0 && <>
          <div className="section-head cart-title"><h3>Waiting for the next cart</h3></div>
          <ul className="cart-rows waiting">{waitingRows.map((row) => <CartRow key={row.sku} row={row} cart={cart} user={user} names={names}
            isPrimary={isPrimary} canRemove={canRemove(row.sku)} busy={unresolved.has(row.sku)} onAdjust={(delta) => onAdjust(row.product, delta)}
            onRemove={() => onRemove(row)} />)}</ul>
        </>}
        {cart.can_manage_access && <details className="fold">
          <summary>Who can use this cart</summary>
          {others.length === 0 ? <p className="muted">Nobody else can be given access yet.</p> : <ul className="plain-rows">
            {others.map((member) => {
              const granted = grants.includes(member.id)
              const saving = savingAccess.has(member.id)
              return <li key={member.id}>
                <span className="person"><Avatar id={member.id} name={member.display_name} />{member.display_name}</span>
                <span className="switch-cell">
                  {saving && <i className="switch-spinner" aria-hidden="true" />}
                  <button type="button" role="switch" aria-checked={granted} aria-busy={saving} aria-label={`Access for ${member.display_name}`}
                    className="switch" disabled={saving} onClick={() => changeAccess(member.id, !granted)}><span /></button>
                </span>
              </li>
            })}
          </ul>}
        </details>}
        {cart.can_delete_cart && <button className="text-button danger-text" disabled={busy} onClick={async () => {
          if (await confirmAction({
            title: `Delete “${cart.name}”?`, text: 'The cart and all its items are removed for everyone.', action: 'Delete cart', danger: true,
          })) onDeleteCart()
        }}>Delete this cart</button>}
      </>}
    </div>
    {cart && available.length > 0 && <footer className="drawer-footer">
      {selectable ? <>
        <div><strong>{units(chosen)} selected · {money(sum(chosen))}</strong>
          <small className="muted">Unselected items stay in the cart.</small></div>
        <button className="big" disabled={chosen.length === 0} onClick={onCheckout}>Checkout</button>
      </> : <div><strong>{units(available)} {units(available) === 1 ? 'item' : 'items'} · {money(sum(available))}</strong>
        <small className="muted">{locked ? 'Checkout is in progress.' : isPrimary ? 'Checkout needs an adult account.' : 'The admin or a primary user can place the order.'}</small></div>}
    </footer>}
  </>
}
