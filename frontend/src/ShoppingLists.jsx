import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { connectLists, refreshAccessToken } from './api.js'
import {
  avatarHue, cartReadyItems, chosenForCart, groupListItems, initials, itemLabel, mergeListUpdate, parsePastedList, parseQuickAdd,
  productSuggestions, timeAgo, upsertListSummary,
} from './listState.js'

const money = new Intl.NumberFormat('en-AE', { style: 'currency', currency: 'AED' })
const LIST_IDEAS = ['Weekly groceries', 'Weekend BBQ', 'Pharmacy run', 'School lunches']

function Avatar({ id, name, size = 'small' }) {
  return <span className={`avatar ${size}`} title={name} aria-label={name}
    style={{ '--hue': avatarHue(id) }}>{initials(name)}</span>
}

function ProgressBar({ done, total }) {
  const percent = total === 0 ? 0 : Math.round(done / total * 100)
  return <div className="list-progress" role="progressbar" aria-valuemin={0} aria-valuemax={total}
    aria-valuenow={done} aria-label={`${done} of ${total} items done`}>
    <span style={{ width: `${percent}%` }} />
  </div>
}

/* ------------------------------------------------------------------------ */
/* Overview                                                                 */
/* ------------------------------------------------------------------------ */

function ListsOverview({ mine, lists, loading, busy, onCreate, onOpen }) {
  const [name, setName] = useState('')
  const create = (value) => {
    const trimmed = value.trim()
    if (trimmed) { onCreate(trimmed); setName('') }
  }
  return (
    <>
      <form className="panel new-list" onSubmit={(event) => { event.preventDefault(); create(name) }}>
        <label htmlFor="new-list-name">Start a new list</label>
        <div className="new-list-row">
          <input id="new-list-name" placeholder="e.g. Weekly groceries" maxLength="80" value={name}
            onChange={(event) => setName(event.target.value)} />
          <button disabled={busy || !name.trim()}>Create list</button>
        </div>
        <div className="list-ideas" aria-label="Suggested list names">
          {LIST_IDEAS.map((idea) => <button type="button" key={idea} className="chip" disabled={busy}
            onClick={() => create(idea)}>+ {idea}</button>)}
        </div>
      </form>

      {loading ? <div className="list-grid" aria-busy="true">
        {[0, 1, 2].map((key) => <div className="list-card skeleton" key={key} />)}
      </div> : lists.length === 0 ? (
        <div className="list-empty">
          <span aria-hidden="true">📝</span>
          <h2>No lists yet</h2>
          <p>{mine
            ? 'These lists are private to you. Nobody in your household can see them.'
            : 'Household lists are shared with everyone in your household.'} Jot things down as you think of them,
            then select items and add them to a household cart.</p>
        </div>
      ) : (
        <ul className="list-grid">
          {lists.map((list) => {
            const remaining = list.item_count - list.checked_count
            return <li key={list.id}>
              <button className="list-card" onClick={() => onOpen(list.id)}>
                <span className="list-card-top">
                  <strong>{list.name}</strong>
                  <Avatar id={list.created_by} name={list.created_by_name} />
                </span>
                <span className="list-card-count">
                  {list.item_count === 0 ? 'Empty list'
                    : remaining === 0 ? 'All done 🎉'
                      : `${remaining} to get`}
                  {list.checked_count > 0 && remaining > 0 && <small> · {list.checked_count} done</small>}
                </span>
                <ProgressBar done={list.checked_count} total={list.item_count} />
                <small>Updated {timeAgo(list.updated_at)}</small>
              </button>
            </li>
          })}
        </ul>
      )}
    </>
  )
}

/* ------------------------------------------------------------------------ */
/* Quick add combobox                                                       */
/* ------------------------------------------------------------------------ */

function PastePreview({ rows, onChange, onConfirm, onCancel }) {
  return (
    <div className="paste-preview" role="dialog" aria-label="Add pasted items">
      <div className="paste-preview-head">
        <strong>Add {rows.length} pasted {rows.length === 1 ? 'item' : 'items'}?</strong>
        <small>Matched products can be sent to a cart later. Others are added as notes.</small>
      </div>
      <ul>
        {rows.map((row, index) => (
          <li key={index}>
            {row.product ? <img src={row.product.image_url} alt="" /> : <span className="custom-icon" aria-hidden="true">✎</span>}
            <span><strong>{row.quantity > 1 && `${row.quantity} × `}{row.product?.name ?? row.text}</strong>
              <small>{row.product ? row.product.category : 'Note'}</small></span>
            <button type="button" className="icon-button" aria-label={`Skip ${row.text}`}
              onClick={() => onChange(rows.filter((_, other) => other !== index))}>×</button>
          </li>
        ))}
      </ul>
      <div className="paste-preview-actions">
        <button type="button" className="secondary" onClick={onCancel}>Cancel</button>
        <button type="button" disabled={rows.length === 0} onClick={onConfirm}>Add {rows.length}</button>
      </div>
    </div>
  )
}

function QuickAdd({ products, busy, onAddProduct, onAddCustom, onAddMany }) {
  const [value, setValue] = useState('')
  const [active, setActive] = useState(-1)
  const [open, setOpen] = useState(false)
  const [pasted, setPasted] = useState(null)
  const inputRef = useRef(null)
  const listboxId = useId()
  const { quantity, text } = parseQuickAdd(value)
  const suggestions = useMemo(() => productSuggestions(products, text), [products, text])

  function reset() { setValue(''); setActive(-1); inputRef.current?.focus() }

  function submit(index = active) {
    if (!text) return
    if (index >= 0 && suggestions[index]) onAddProduct(suggestions[index], quantity)
    else onAddCustom(text, quantity)
    reset()
  }

  function onKeyDown(event) {
    if (event.key === 'ArrowDown') {
      event.preventDefault(); setOpen(true)
      setActive((current) => Math.min(current + 1, suggestions.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); setActive((current) => Math.max(current - 1, -1))
    } else if (event.key === 'Escape') {
      setOpen(false); setActive(-1)
    } else if (event.key === 'Enter') {
      event.preventDefault(); submit()
    }
  }

  const showMenu = open && text.length >= 2
  return (
    <div className="quick-add" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
    }}>
      <div className="quick-add-field">
        <span aria-hidden="true">＋</span>
        <input ref={inputRef} role="combobox" aria-expanded={showMenu} aria-controls={listboxId}
          aria-autocomplete="list" aria-label="Add an item"
          aria-activedescendant={active >= 0 ? `${listboxId}-${active}` : undefined}
          placeholder="Add an item — try “2 milk” or “birthday candles”" maxLength="124"
          value={value} disabled={busy}
          onChange={(event) => { setValue(event.target.value); setActive(-1); setOpen(true) }}
          onFocus={() => setOpen(true)} onKeyDown={onKeyDown}
          onPaste={(event) => {
            // Pasting several lines (e.g. from notes or a chat) adds them in one request.
            const clipboard = event.clipboardData.getData('text')
            if (!/\r?\n/.test(clipboard.trim())) return
            event.preventDefault()
            setOpen(false)
            setPasted(parsePastedList(clipboard, products))
          }} />
        <button type="button" disabled={busy || !text} onClick={() => submit()}>Add</button>
      </div>
      {pasted && <PastePreview rows={pasted} onChange={setPasted} onCancel={() => setPasted(null)}
        onConfirm={() => { onAddMany(pasted); setPasted(null); inputRef.current?.focus() }} />}
      {showMenu && <ul className="quick-add-menu" id={listboxId} role="listbox">
        {suggestions.map((product, index) => (
          <li key={product.sku} id={`${listboxId}-${index}`} role="option" aria-selected={index === active}
            className={index === active ? 'active' : ''}
            onMouseEnter={() => setActive(index)}
            onMouseDown={(event) => { event.preventDefault(); submit(index) }}>
            <img src={product.image_url} alt="" loading="lazy" />
            <span><strong>{product.name}</strong><small>{product.category}</small></span>
            <em>{quantity > 1 && `${quantity} × `}{money.format(product.price_fils / 100)}</em>
          </li>
        ))}
        <li id={`${listboxId}-custom`} role="option" aria-selected={active === -1}
          className={`custom ${active === -1 ? 'active' : ''}`}
          onMouseEnter={() => setActive(-1)}
          onMouseDown={(event) => { event.preventDefault(); submit(-1) }}>
          <span className="custom-icon" aria-hidden="true">✎</span>
          <span><strong>Add “{text}”</strong><small>As a note — not linked to a product</small></span>
          <kbd>Enter</kbd>
        </li>
      </ul>}
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* One list item                                                            */
/* ------------------------------------------------------------------------ */

function ListItemRow({ item, product, chosen, onChoose, onToggle, onQuantity, onNote, onDelete }) {
  const [editingNote, setEditingNote] = useState(false)
  const [note, setNote] = useState(item.note ?? '')
  useEffect(() => { if (!editingNote) setNote(item.note ?? '') }, [item.note, editingNote])
  const label = item.custom_name ?? product?.name ?? item.sku
  const saveNote = () => {
    setEditingNote(false)
    if ((note.trim() || null) !== (item.note ?? null)) onNote(note.trim())
  }
  return (
    <li className={`list-item ${item.checked ? 'checked' : ''} ${item.pending ? 'pending' : ''} ${chosen ? 'chosen' : ''}`}>
      {/* The circle selects an item for the cart. On a done item it puts the item back on the list. */}
      {item.checked || onChoose ? <label className="list-check">
        <input type="checkbox" checked={item.checked || Boolean(chosen)} disabled={item.pending}
          onChange={() => item.checked ? onToggle(false) : onChoose()}
          aria-label={item.checked ? `Put ${label} back on the list` : `Select ${label} for the cart`} />
        <span aria-hidden="true" />
      </label> : <span className="list-check-space" aria-hidden="true" />}
      {product ? <img src={product.image_url} alt="" loading="lazy" />
        : <span className="list-item-placeholder" aria-hidden="true">✎</span>}
      <div className="list-item-body">
        <strong>{label}</strong>
        {editingNote ? (
          <input className="note-input" autoFocus maxLength="200" value={note} placeholder="Add a note"
            aria-label={`Note for ${label}`}
            onChange={(event) => setNote(event.target.value)} onBlur={saveNote}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur()
              if (event.key === 'Escape') { setNote(item.note ?? ''); setEditingNote(false) }
            }} />
        ) : (
          <button className="note-button" onClick={() => setEditingNote(true)}>
            {item.note ? <span>“{item.note}”</span> : <span className="add-note">+ note</span>}
          </button>
        )}
        <small className="list-item-meta">
          {product && <>{money.format(product.price_fils * item.quantity / 100)} · </>}
          {item.checked && item.checked_by_name ? `Got by ${item.checked_by_name}` : `Added by ${item.added_by_name}`}
        </small>
      </div>
      <div className="list-item-actions">
        {!item.checked && <button className="secondary done-button" disabled={item.pending}
          title="Move to Done without adding it to a cart" aria-label={`Mark ${label} as done`}
          onClick={() => onToggle(true)}>Mark done</button>}
        {!item.checked && <div className="mini-stepper" aria-label={`Quantity for ${label}`}>
          <button className="secondary" disabled={item.quantity <= 1} aria-label={`One less ${label}`}
            onClick={() => onQuantity(item.quantity - 1)}>−</button>
          <strong>{item.quantity}</strong>
          <button className="secondary" disabled={item.quantity >= 999} aria-label={`One more ${label}`}
            onClick={() => onQuantity(item.quantity + 1)}>+</button>
        </div>}
        <Avatar id={item.added_by} name={item.added_by_name} />
        {item.can_delete && <button className="icon-button" aria-label={`Remove ${label}`} title="Remove"
          onClick={onDelete}>×</button>}
      </div>
    </li>
  )
}

/* ------------------------------------------------------------------------ */
/* List detail                                                              */
/* ------------------------------------------------------------------------ */

function ListDetail({
  mine, list, products, productsBySku, categoryOrder, carts, defaultCartId, busy,
  onBack, onRename, onDelete, onAddProduct, onAddCustom, onAddMany, onUpdateItem, onDeleteItem, onClearDone, onMove,
}) {
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState(list.name)
  const [showDone, setShowDone] = useState(false)
  const [target, setTarget] = useState(defaultCartId ?? carts[0]?.id ?? '')
  useEffect(() => { if (!renaming) setName(list.name) }, [list.name, renaming])
  useEffect(() => {
    if (!carts.some((cart) => cart.id === target)) setTarget(defaultCartId ?? carts[0]?.id ?? '')
  }, [carts, defaultCartId, target])

  const { groups, done } = useMemo(
    () => groupListItems(list.items, productsBySku, categoryOrder), [list.items, productsBySku, categoryOrder],
  )
  const { ready } = useMemo(() => cartReadyItems(list.items, productsBySku), [list.items, productsBySku])
  // Only items the user chose are sent. Nothing is chosen to begin with.
  const [chosenIds, setChosenIds] = useState(() => new Set())
  const chosen = useMemo(() => chosenForCart(ready, chosenIds, productsBySku), [ready, chosenIds, productsBySku])
  const isChosen = (item) => chosen.items.some((other) => other.id === item.id)
  const canSend = carts.length > 0
  const toggleChosen = (id) => setChosenIds((current) => {
    const next = new Set(current)
    if (!next.delete(id)) next.add(id)
    return next
  })
  const openCount = list.items.length - done.length
  const removableDone = done.filter((item) => item.can_delete).length

  const finishRename = () => {
    setRenaming(false)
    if (name.trim() && name.trim() !== list.name) onRename(name.trim())
    else setName(list.name)
  }

  return (
    <section className="list-detail">
      <div className="list-detail-head">
        <button className="text-button" onClick={onBack}>← All lists</button>
        <div className="list-title-row">
          {renaming ? (
            <input className="title-input" autoFocus maxLength="80" value={name} aria-label="List name"
              onChange={(event) => setName(event.target.value)} onBlur={finishRename}
              onKeyDown={(event) => {
                if (event.key === 'Enter') event.currentTarget.blur()
                if (event.key === 'Escape') { setName(list.name); setRenaming(false) }
              }} />
          ) : (
            <h1>
              {list.name}
              {list.can_manage && <button className="icon-button rename" aria-label="Rename list" title="Rename"
                onClick={() => setRenaming(true)}>✎</button>}
            </h1>
          )}
          {list.can_manage && <button className="secondary danger" disabled={busy} onClick={() => {
            if (window.confirm(`Delete “${list.name}” and all ${list.items.length} items? This can’t be undone.`)) onDelete()
          }}>Delete list</button>}
        </div>
        <p className="muted">
          {list.items.length === 0 ? 'Nothing on this list yet.'
            : openCount === 0 ? 'Everything is ticked off. Nice work!'
              : `${openCount} to get · ${done.length} done`}
          {mine ? ' · Only you can see this list' : ` · Started by ${list.created_by_name}`}
        </p>
        <ProgressBar done={done.length} total={list.items.length} />
      </div>

      <QuickAdd products={products} busy={false} onAddProduct={onAddProduct} onAddCustom={onAddCustom} onAddMany={onAddMany} />

      {list.items.length === 0 && <div className="list-empty compact">
        <span aria-hidden="true">🛒</span>
        <p>Start typing above. Pick a product to price it and send it to a cart later,
          or press Enter to add anything as a note.</p>
      </div>}

      {groups.map(([category, items]) => (
        <section className="list-group" key={category}>
          <h2>{category} <span>{items.length}</span></h2>
          <ul>
            {items.map((item) => <ListItemRow key={item.id} item={item} product={productsBySku.get(item.sku)}
              chosen={isChosen(item)}
              onChoose={canSend && ready.includes(item) ? () => toggleChosen(item.id) : null}
              onToggle={(checked) => onUpdateItem(item, { checked })}
              onQuantity={(quantity) => onUpdateItem(item, { quantity })}
              onNote={(note) => onUpdateItem(item, { note: note || null })}
              onDelete={() => onDeleteItem(item)} />)}
          </ul>
        </section>
      ))}

      {done.length > 0 && <section className="list-group done">
        <div className="done-head">
          <button className="text-button" aria-expanded={showDone} onClick={() => setShowDone(!showDone)}>
            {showDone ? '▾' : '▸'} Done ({done.length})
          </button>
          {removableDone > 0 && <button className="text-button" disabled={busy} onClick={onClearDone}>
            Clear {removableDone === done.length ? 'all done' : `${removableDone} of mine`}
          </button>}
        </div>
        {showDone && <ul>
          {done.map((item) => <ListItemRow key={item.id} item={item} product={productsBySku.get(item.sku)}
            onToggle={(checked) => onUpdateItem(item, { checked })}
            onQuantity={(quantity) => onUpdateItem(item, { quantity })}
            onNote={(note) => onUpdateItem(item, { note: note || null })}
            onDelete={() => onDeleteItem(item)} />)}
        </ul>}
      </section>}

      {ready.length > 0 && canSend && <div className="move-bar" role="region" aria-label="Send to cart">
        <div>
          <strong>{chosen.items.length} of {ready.length} selected</strong>
          <small>{chosen.items.length ? `About ${money.format(chosen.totalFils / 100)}` : 'Tick the items to add'}</small>
          <button className="text-button" onClick={() => setChosenIds(
            chosen.items.length === ready.length ? new Set() : new Set(ready.map((item) => item.id)),
          )}>{chosen.items.length === ready.length ? 'Clear' : 'Select all'}</button>
        </div>
        <label className="move-target">
          <span>Send to</span>
          <select value={target} onChange={(event) => setTarget(event.target.value)}>
            {carts.map((cart) => <option key={cart.id} value={cart.id}>{cart.name}</option>)}
          </select>
        </label>
        <button disabled={busy || !target || chosen.items.length === 0} onClick={() => {
          onMove(target, chosen.items.map((item) => item.id))
          setChosenIds(new Set())
        }}>
          Add {chosen.items.length || ''} to cart →
        </button>
      </div>}
    </section>
  )
}

/* ------------------------------------------------------------------------ */
/* Container: data loading, optimistic updates and live sync               */
/* ------------------------------------------------------------------------ */

/** My lists and household lists, kept apart under two tabs. */
export function ListsPage({ account, api, household, onBack, ...rest }) {
  const [tab, setTab] = useState('mine')
  const shared = tab === 'household' && household
  const tabs = [['mine', 'My lists'], ['household', 'Household lists']]
  return (
    <div className="shopping-lists-page">
      <div className="list-tabs" role="tablist" aria-label="Which lists">
        {tabs.map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}
      </div>
      {tab === 'household' && !household ? <div className="list-empty">
        <span aria-hidden="true">🏠</span>
        <h2>No household yet</h2>
        <p>Create or join a household to share lists with your family. Your own lists are under My lists.</p>
      </div> : <ShoppingLists key={shared ? `household-${household.id}` : 'mine'} {...rest} mine={!shared}
        api={shared ? api : account} household={household} onBack={onBack} />}
    </div>
  )
}

function ShoppingLists({
  mine, api, token, household, user, products, carts, defaultCartId, onNotify, onCartChanged, onAccessLost, onBack,
}) {
  const [lists, setLists] = useState([])
  const [loading, setLoading] = useState(true)
  const [openId, setOpenId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState(false)
  const [interval, setLiveInterval] = useState(30)
  const openIdRef = useRef(openId)
  openIdRef.current = openId
  const apiRef = useRef(api)
  apiRef.current = api
  // Number of our own edits still being saved. Live batches are skipped while
  // this is above zero so an older snapshot cannot undo an edit on screen.
  const inFlight = useRef(0)
  // Quantity taps per item, sent as one request once the user pauses.
  const quantityTimers = useRef(new Map())
  // Parent callbacks change identity on every render; keep them in refs so
  // the live socket and data loaders are not torn down and rebuilt each time.
  const callbacks = useRef({ onNotify, onAccessLost })
  callbacks.current = { onNotify, onAccessLost }
  const notify = useCallback((text, kind) => callbacks.current.onNotify(text, kind), [])
  const accessLost = useCallback((reason) => callbacks.current.onAccessLost(reason), [])

  const productsBySku = useMemo(() => new Map(products.map((product) => [product.sku, product])), [products])
  const categoryOrder = useMemo(() => [...new Set(products.map((product) => product.category))], [products])

  const fail = useCallback((error) => {
    if (error?.status === 403 && /revoked/i.test(error.message)) { accessLost(error.message); return }
    notify(error?.message ?? 'Something went wrong.', 'error')
  }, [notify, accessLost])

  const refreshLists = useCallback(async () => {
    try { setLists(await apiRef.current.lists()) } catch (error) { fail(error) } finally { setLoading(false) }
  }, [fail])

  const refreshDetail = useCallback(async (id = openIdRef.current) => {
    if (!id) return
    try {
      const fresh = await apiRef.current.list(id)
      if (openIdRef.current === id) setDetail(fresh)
    } catch (error) {
      if (error.status === 404 && openIdRef.current === id) {
        setOpenId(null); setDetail(null); notify('That list was deleted.', 'error')
      } else fail(error)
    }
  }, [fail, notify])

  useEffect(() => { setLoading(true); setOpenId(null); setDetail(null); refreshLists() }, [refreshLists])

  // The overview cards follow the open list locally: no extra request after each edit.
  useEffect(() => { if (detail) setLists((current) => upsertListSummary(current, detail)) }, [detail])

  // Live channel: every ~30 s the server pushes the lists that changed, already
  // filtered for this viewer. Nothing is fetched per change.
  const liveHouseholdId = mine ? null : household.id
  useEffect(() => {
    if (!liveHouseholdId) return  // private lists have nobody to sync with
    let stopped = false
    let socket
    let retry
    let connectedBefore = false
    const connect = () => {
      socket = connectLists(token, liveHouseholdId)
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data)
        if (message.type === 'ready') {
          setLive(true)
          if (message.interval) setLiveInterval(Math.round(message.interval))
          // After a reconnect, catch up once on anything missed while offline.
          if (connectedBefore) { refreshLists(); refreshDetail() }
          connectedBefore = true
          return
        }
        if (message.type !== 'lists_update') return
        for (const entry of message.lists) {
          setLists((current) => upsertListSummary(current, entry))
          if (entry.id !== openIdRef.current) continue
          if (entry.deleted) {
            setOpenId(null); setDetail(null); notify('This list was deleted by another member.', 'error')
          } else {
            setDetail((current) => mergeListUpdate(current, entry, inFlight.current))
          }
        }
      }
      socket.onclose = async ({ code, reason }) => {
        setLive(false)
        if (stopped) return
        if (code === 4401) {
          try { await refreshAccessToken() } catch (error) {
            // On 401 the session is cleared upstream; otherwise try again.
            if (error.status !== 401 && !stopped) retry = window.setTimeout(() => { if (!stopped) connect() }, 2000)
          }
          return
        }
        if (code === 4404) { accessLost(reason); return }
        retry = window.setTimeout(() => { if (!stopped) connect() }, 2000)
      }
    }
    connect()
    return () => { stopped = true; window.clearTimeout(retry); socket?.close() }
  }, [token, liveHouseholdId, refreshLists, refreshDetail, accessLost, notify])

  async function run(action) {
    setBusy(true)
    try { return await action() } catch (error) { fail(error) } finally { setBusy(false) }
  }

  /** Count an edit as "being saved" for as long as its request runs. */
  async function saving(request) {
    inFlight.current += 1
    try { return await request() } finally { inFlight.current -= 1 }
  }

  async function openList(id) {
    setOpenId(id)
    setDetail(null)
    await refreshDetail(id)
  }

  /** Apply a change locally first, then reconcile with the server's answer. */
  function patchItems(mutate) {
    setDetail((current) => current && { ...current, items: mutate(current.items) })
  }

  function confirmItem(listId, saved, temporaryId) {
    setDetail((current) => {
      // The user may have opened another list while this request was running.
      if (!current || current.id !== listId) return current
      const items = current.items.filter((item) => item.id !== temporaryId)
      return {
        ...current,
        updated_at: saved.updated_at > current.updated_at ? saved.updated_at : current.updated_at,
        items: items.some((item) => item.id === saved.id)
          ? items.map((item) => item.id === saved.id ? saved : item)
          : [...items, saved],
      }
    })
  }

  function temporaryItem(body, index = 0) {
    return {
      id: `pending-${Date.now()}-${index}`, sku: body.sku ?? null, custom_name: body.custom_name ?? null,
      quantity: body.quantity, note: null, added_by: user.id, added_by_name: user.display_name, checked: false,
      checked_by: null, checked_by_name: null, created_at: new Date().toISOString(), can_delete: true, pending: true,
    }
  }

  async function addItem(body) {
    const listId = detail.id
    const existing = body.sku && detail.items.find((item) => item.sku === body.sku && !item.checked)
    const temporary = existing ? null : temporaryItem(body)
    if (existing) patchItems((items) => items.map((item) => item.id === existing.id
      ? { ...item, quantity: Math.min(999, item.quantity + body.quantity) } : item))
    else patchItems((items) => [...items, temporary])
    try {
      confirmItem(listId, await saving(() => api.addListItem(listId, body)), temporary?.id)
    } catch (error) {
      fail(error)
      refreshDetail()
    }
  }

  async function addMany(rows) {
    const listId = detail.id
    const bodies = rows.map((row) => row.product
      ? { sku: row.product.sku, quantity: row.quantity }
      : { custom_name: row.text, quantity: row.quantity })
    const temporaries = bodies.map(temporaryItem)
    patchItems((items) => [...items, ...temporaries])
    try {
      // One request and one transaction for the whole pasted list.
      const fresh = await saving(() => api.addListItems(listId, bodies))
      if (openIdRef.current === listId) setDetail(fresh)
      notify(`Added ${bodies.length} ${bodies.length === 1 ? 'item' : 'items'}.`)
    } catch (error) {
      fail(error)
      refreshDetail()
    }
  }

  async function sendItemChange(listId, item, changes) {
    try {
      confirmItem(listId, await saving(() => api.updateListItem(listId, item.id, changes)))
    } catch (error) {
      fail(error)
      refreshDetail()
    }
  }

  function updateItem(item, changes) {
    if (item.pending) return
    patchItems((items) => items.map((row) => row.id === item.id ? {
      ...row, ...changes,
      ...('checked' in changes ? { checked_by_name: changes.checked ? user.display_name : null } : {}),
    } : row))
    if ('quantity' in changes && Object.keys(changes).length === 1) {
      // Fast taps on + / − become one request with the final quantity.
      const timers = quantityTimers.current
      const listId = detail.id
      window.clearTimeout(timers.get(item.id)?.timer)
      if (!timers.has(item.id)) inFlight.current += 1
      const send = () => {
        timers.delete(item.id)
        inFlight.current -= 1
        sendItemChange(listId, item, changes)
      }
      timers.set(item.id, { timer: window.setTimeout(send, 450), send })
      return
    }
    sendItemChange(detail.id, item, changes)
  }

  // Leaving the list sends any quantity change that is still waiting, straight away.
  useEffect(() => () => {
    for (const { timer, send } of [...quantityTimers.current.values()]) {
      window.clearTimeout(timer)
      send()
    }
  }, [openId])

  async function deleteItem(item) {
    if (item.pending) return
    const label = itemLabel(item, productsBySku)
    const listId = detail.id
    patchItems((items) => items.filter((row) => row.id !== item.id))
    try {
      await saving(() => api.deleteListItem(listId, item.id))
      notify(`Removed ${label}.`)
    } catch (error) {
      fail(error)
      refreshDetail()
    }
  }

  const back = openId ? () => { setOpenId(null); setDetail(null) } : onBack
  const liveTitle = live
    ? `Changes from others appear within about ${interval} seconds`
    : 'Reconnecting…'

  return (
    <section className="shopping-lists">
      {!openId && <div className="page-heading">
        <div>
          <button className="text-button" onClick={onBack}>← Back to groceries</button>
          <h1>{mine ? 'My lists' : 'Household lists'}</h1>
          <p className="muted">{mine ? 'Private. Only you can see these.' : `Shared with everyone in ${household.name}.`}</p>
        </div>
        {!mine && <span className={`live-badge ${live ? 'on' : ''}`} title={liveTitle}>
          <span aria-hidden="true" /> {live ? `Synced every ${interval}s` : 'Offline'}
        </span>}
      </div>}

      {!openId ? (
        <ListsOverview mine={mine} lists={lists} loading={loading} busy={busy} onOpen={openList}
          onCreate={(name) => run(async () => {
            const created = await api.createList(name)
            setOpenId(created.id); setDetail(created)
          })} />
      ) : !detail ? (
        <div className="list-detail"><button className="text-button" onClick={back}>← All lists</button>
          <div className="list-card skeleton tall" aria-busy="true" /></div>
      ) : (
        <ListDetail key={detail.id} mine={mine} list={detail} products={products} productsBySku={productsBySku} categoryOrder={categoryOrder}
          carts={carts} defaultCartId={defaultCartId} busy={busy} onBack={back}
          onRename={(name) => run(async () => setDetail(await saving(() => api.renameList(detail.id, name))))}
          onDelete={() => run(async () => {
            await saving(() => api.deleteList(detail.id))
            setLists((current) => upsertListSummary(current, { id: detail.id, deleted: true }))
            notify(`Deleted ${detail.name}.`)
            back()
          })}
          onAddProduct={(product, quantity) => addItem({ sku: product.sku, quantity })}
          onAddCustom={(text, quantity) => addItem({ custom_name: text, quantity })}
          onAddMany={addMany}
          onUpdateItem={updateItem}
          onDeleteItem={deleteItem}
          onClearDone={() => run(async () => setDetail(await saving(() => api.clearCheckedItems(detail.id))))}
          onMove={(cartId, itemIds) => run(async () => {
            const result = await saving(() => api.moveListToCart(detail.id, cartId, itemIds, mine ? household.id : null))
            const cartName = carts.find((cart) => cart.id === cartId)?.name ?? 'the cart'
            if (result.moved.length > 0) {
              notify(`Added ${result.moved.length} ${result.moved.length === 1 ? 'product' : 'products'} to ${cartName}.`)
              onCartChanged(cartId)
            }
            const reasons = [...new Set(result.skipped.map((skip) => skip.reason))]
            if (reasons.length > 0) notify(`${result.skipped.length} not added: ${reasons.join('; ')}`, 'error')
            await refreshDetail()
          })} />
      )}
    </section>
  )
}
