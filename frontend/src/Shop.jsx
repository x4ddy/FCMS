import React, { useCallback, useDeferredValue, useEffect, useMemo, useState } from 'react'
import { Empty, Icon, money, Overlay, ProductImage, Stepper } from './ui.jsx'

const SHELF = 12
const SORTS = {
  featured: ['Featured', null],
  low: ['Price: low to high', (a, b) => a.price_fils - b.price_fils],
  high: ['Price: high to low', (a, b) => b.price_fils - a.price_fils],
  name: ['Name: A to Z', (a, b) => a.name.localeCompare(b.name)],
}
const PREVIEW = ['strawberries', 'croissants', 'cheese', 'avocados', 'cookies', 'juice-boxes', 'watermelon', 'ice-cream']

export function Landing({ onOpenAuth }) {
  return <section className="landing page" aria-label="FamilyCart">
    <div className="landing-copy">
      <h1>One cart for the whole family</h1>
      <p>Everyone adds what they need, changes show up for the whole household, and one person checks out.</p>
      <button className="big" onClick={onOpenAuth}>Start shopping</button>
    </div>
    <div className="landing-art" aria-hidden="true">
      {PREVIEW.map((sku, index) => <span key={sku} style={{ '--i': index }}><img src={`/products/${sku}.webp`} alt="" /></span>)}
    </div>
    <ul className="landing-points">
      <li><strong>Shared carts</strong><p>Several carts per household, with access you control.</p></li>
      <li><strong>Lists that become carts</strong><p>Private and household lists. Send only what you pick.</p></li>
      <li><strong>Order what you choose</strong><p>Check out some items now and leave the rest for later.</p></li>
    </ul>
  </section>
}

function CartControl({ product, shop }) {
  const quantity = shop.quantities[product.sku] ?? 0
  if (shop.needsHousehold) return null
  if (product.min_age > 0 && !shop.adultsOnly) return <span className="card-note">18+ cart only</span>
  if (quantity < 1) return <button className="add-button" disabled={!shop.canEdit}
    aria-label={`Add ${product.name}`} onClick={() => shop.onAdjust(product, 1)}>ADD <span aria-hidden="true">+</span></button>
  return <Stepper label={product.name} quantity={quantity} disabled={!shop.canEdit} canRemove={shop.canRemove(product.sku)}
    removeHint="You cannot remove this right now" onAdjust={(delta) => shop.onAdjust(product, delta)} />
}

/** Only cards whose own state changed are drawn again. */
function sameCard(before, after) {
  const a = before.shop
  const b = after.shop
  const { sku } = after.product
  return before.product === after.product && before.onOpen === after.onOpen && before.onWish === after.onWish && before.wished === after.wished
    && a.quantities[sku] === b.quantities[sku] && a.canEdit === b.canEdit && a.adultsOnly === b.adultsOnly
    && a.needsHousehold === b.needsHousehold && a.canRemove(sku) === b.canRemove(sku) && a.onAdjust === b.onAdjust
}

const ProductCard = React.memo(function ProductCard({ product, shop, onOpen, onWish, wished }) {
  return <article className="product-card">
    <div className="product-tile">
      <button className="product-open" tabIndex={-1} aria-hidden="true" onClick={() => onOpen(product)}><ProductImage product={product} /></button>
      <button className={`wish-button${wished ? ' on' : ''}`} aria-label={`Add ${product.name} to a list`} title="Add to a list" onClick={() => onWish(product)}>
        <Icon name="heart" size={18} /></button>
      <CartControl product={product} shop={shop} />
    </div>
    <button className="product-text" onClick={() => onOpen(product)} aria-label={`View ${product.name}`}>
      <span className="price">{money(product.price_fils)}</span>
      <span className="product-name">{product.name}</span>
    </button>
  </article>
}, sameCard)

/** Choose which list a product goes on, or start a new one. */
function ListPicker({ product, listApis, onAdded, onClose, onNotify }) {
  const [groups, setGroups] = useState(null)
  const [name, setName] = useState('')
  const [where, setWhere] = useState(0)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let cancelled = false
    Promise.all(listApis.map(async (source) => ({ ...source, lists: await source.api.lists() }))).then((found) => {
      if (!cancelled) setGroups(found)
    }, (error) => {
      if (!cancelled) { onNotify(error.message, 'error'); onClose() }
    })
    return () => { cancelled = true }
  }, [])
  async function save(source, list) {
    setBusy(true)
    try {
      const target = list ?? await source.api.createList(name.trim())
      await source.api.addListItem(target.id, { sku: product.sku, quantity: 1 })
      onNotify(`Added ${product.name} to ${target.name}.`)
      onAdded(product.sku)
      onClose()
    } catch (error) { onNotify(error.message, 'error'); setBusy(false) }
  }
  return <Overlay kind="dialog" label={`Add ${product.name} to a list`} onClose={onClose}>{(leave) => <>
    <button className="icon-button overlay-close" aria-label="Close" onClick={leave}><Icon name="close" /></button>
    <div className="picker-product"><ProductImage product={product} /><div><h2>Add to a list</h2><p className="muted">{product.name}</p></div></div>
    {!groups ? <div className="skeleton picker-loading" role="status" aria-label="Loading your lists" /> : <>
      {groups.map((source) => source.lists.length > 0 && <section key={source.label} className="picker-group">
        <h3>{source.title}</h3>
        <ul>{source.lists.map((list) => <li key={list.id}><button disabled={busy} onClick={() => save(source, list)}>
          <span>{list.name}</span><small>{list.item_count} {list.item_count === 1 ? 'item' : 'items'}</small></button></li>)}</ul>
      </section>)}
      <form className="picker-new" onSubmit={(event) => {
        event.preventDefault()
        if (name.trim()) save(groups[where])
      }}>
        <label>New list<input maxLength="80" placeholder="e.g. Wishlist" value={name} onChange={(event) => setName(event.target.value)} /></label>
        {groups.length > 1 && <select aria-label="Who can see the new list" value={where} onChange={(event) => setWhere(Number(event.target.value))}>
          {groups.map((source, index) => <option key={source.label} value={index}>{source.title}</option>)}
        </select>}
        <button disabled={busy || !name.trim()}>Create and add</button>
      </form>
    </>}
  </>}</Overlay>
}

function ProductSheet({ product, products, shop, people, wished, onWish, onOpen, onClose }) {
  const similar = products.filter((other) => other.category === product.category && other.sku !== product.sku).slice(0, 8)
  return <Overlay kind="sheet" label={product.name} onClose={onClose}>{(leave) => <>
    <button className="icon-button overlay-close" aria-label="Close" onClick={leave}><Icon name="close" /></button>
    <div className="product-sheet" key={product.sku}>
      <div className="product-sheet-image"><ProductImage product={product} /></div>
      <div className="product-sheet-info">
        <small className="muted">{product.category}</small>
        <h2>{product.name}</h2>
        <p className="price large">{money(product.price_fils)}</p>
        {product.min_age > 0 && <span className="tag">Adults only, {product.min_age}+</span>}
        <div className="product-sheet-actions">
          <CartControl product={product} shop={shop} />
          <button className="secondary" onClick={() => onWish(product)}><Icon name="heart" size={18} /> Add to a list</button>
        </div>
        {people.length > 0 && <p className="muted">In the cart: {people.map((person) => `${person.name} × ${person.quantity}`).join(', ')}</p>}
      </div>
    </div>
    {similar.length > 0 && <>
      <h3>More in {product.category}</h3>
      <div className="shelf">{similar.map((other) => <ProductCard key={other.sku} product={other} shop={shop} onOpen={onOpen} onWish={onWish} wished={wished.has(other.sku)} />)}</div>
    </>}
  </>}</Overlay>
}

export function ShopPage({
  products, loading, query, onClearQuery, shop, carts, household, roleName, checkoutBy, userName, peopleFor, listApis,
  onSelectCart, onSetUpHousehold, onOpenCart, onNotify,
}) {
  const [category, setCategory] = useState('All')
  const [sort, setSort] = useState('featured')
  const [viewingSku, setViewingSku] = useState(null)
  const [wishing, setWishing] = useState(null)
  const [wished, setWished] = useState(() => new Set())  // added while this page is open
  const openProduct = useCallback((product) => setViewingSku(product.sku), [])
  const viewing = products.find((product) => product.sku === viewingSku)
  const search = useDeferredValue(query.trim().toLowerCase())
  const categories = useMemo(() => {
    const first = new Map()
    for (const product of products) if (!first.has(product.category)) first.set(product.category, product)
    return [...first]
  }, [products])
  useEffect(() => { if (search) setCategory('All') }, [search])
  useEffect(() => {
    if (category !== 'All' && products.length && !categories.some(([name]) => name === category)) setCategory('All')
  }, [categories, category, products.length])

  const results = useMemo(() => {
    const words = search.split(/\s+/).filter(Boolean)
    const found = products.filter((product) => (category === 'All' || product.category === category)
      && words.every((word) => `${product.name} ${product.category}`.toLowerCase().includes(word)))
    const order = SORTS[sort][1]
    return order ? [...found].sort(order) : found
  }, [products, category, search, sort])
  const shelves = category === 'All' && !search && sort === 'featured'
  const choose = (name) => {
    setCategory(name)
    setSort('featured')
    if (search) onClearQuery()
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const total = Object.values(shop.quantities).reduce((sum, quantity) => sum + quantity, 0)
  const value = products.reduce((sum, product) => sum + product.price_fils * (shop.quantities[product.sku] ?? 0), 0)
  const card = (product) => <ProductCard key={product.sku} product={product} shop={shop} onOpen={openProduct}
    onWish={setWishing} wished={wished.has(product.sku)} />

  return <div className="page shop-page">
    {!search && category === 'All' && <div className="shop-intro">
      <div>
        <h1>{household ? 'Shopping for your household' : `Hello, ${userName}`}</h1>
        <p className="muted">{household ? `${household.name} · ${roleName}`
          : 'Create or join a household to start shopping. My lists work already.'}</p>
      </div>
      {!household ? <button onClick={onSetUpHousehold}>Set up household</button>
        : carts.length > 0 ? <label className="cart-picker"><span>Add products to</span>
          <select value={shop.cartId ?? ''} onChange={(event) => onSelectCart(event.target.value)}>
            {carts.map((cart) => <option key={cart.id} value={cart.id}>{cart.name}{cart.adults_only ? ' (18+)' : ''}</option>)}
          </select></label>
          : household.role !== 'primary' && <p className="muted">You have no cart yet. Ask the admin for access.</p>}
    </div>}

    {checkoutBy && checkoutBy !== 'You' && <div className="banner" role="status"><strong>{checkoutBy} is checking out this cart</strong>
      <p>You can keep adding. New items wait and go into the next cart.</p></div>}

    {!search && <div className="categories" role="group" aria-label="Shop by category">
      {[['All', null], ...categories].map(([name, sample]) => <button key={name} aria-pressed={category === name}
        className="category" onClick={() => choose(name)}>
        <span className="category-tile">{sample ? <ProductImage product={sample} /> : <Icon name="cart" size={34} />}</span>
        <span>{name}</span>
      </button>)}
    </div>}

    {loading ? <div className="product-grid" aria-busy="true">
      {Array.from({ length: 12 }, (_, index) => <div className="product-card skeleton" key={index} />)}
    </div> : shelves ? categories.map(([name]) => {
      const items = results.filter((product) => product.category === name)
      return <section className="shelf-section" key={name}>
        <div className="section-head"><h2>{name}</h2>
          {items.length > 4 && <button className="text-button" onClick={() => choose(name)}>See all {items.length}</button>}</div>
        <div className="shelf">{items.slice(0, SHELF).map(card)}</div>
      </section>
    }) : <section>
      <div className="section-head">
        <h2>{search ? `Results for “${query.trim()}”` : category === 'All' ? 'All products' : category}{' '}
          <small className="muted">{results.length}</small></h2>
        <select className="sorter" aria-label="Sort products" value={sort} onChange={(event) => setSort(event.target.value)}>
          {Object.entries(SORTS).map(([id, [label]]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </div>
      {results.length === 0 ? <Empty icon="🔎" title="Nothing matches">
        <p>Try a shorter word, or browse the categories.</p>
        <button className="secondary" onClick={onClearQuery}>Clear search</button>
      </Empty> : <div className="product-grid">{results.map(card)}</div>}
    </section>}

    {shelves && !loading && <button className="secondary see-all" onClick={() => { setSort('name'); window.scrollTo({ top: 0 }) }}>
      View all {products.length} products</button>}

    {total > 0 && <button className="mobile-cart-bar" onClick={onOpenCart}>
      <span>{total} {total === 1 ? 'item' : 'items'} · {money(value)}</span><strong>View cart</strong>
    </button>}

    {viewing && <ProductSheet product={viewing} products={products} shop={shop} people={peopleFor(viewing.sku)} wished={wished} onWish={setWishing}
      onOpen={openProduct} onClose={() => setViewingSku(null)} />}
    {wishing && <ListPicker product={wishing} listApis={listApis} onNotify={onNotify} onClose={() => setWishing(null)}
      onAdded={(sku) => setWished((current) => new Set(current).add(sku))} />}
  </div>
}
