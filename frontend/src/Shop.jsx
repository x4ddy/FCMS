import React, { useCallback, useDeferredValue, useEffect, useMemo, useState } from 'react'
import { Dropdown, Empty, Icon, money, Overlay, ProductImage, Stepper } from './ui.jsx'

const SHELF = 7
const SORTS = {
  featured: ['Featured', null],
  low: ['Price: low to high', (a, b) => a.price_fils - b.price_fils],
  high: ['Price: high to low', (a, b) => b.price_fils - a.price_fils],
  name: ['Name: A to Z', (a, b) => a.name.localeCompare(b.name)],
}
const CATEGORY_ART = {
  'Fruit & Vegetables': 'produce',
  'Milk, Eggs & Dairy': 'dairy',
  Bakery: 'bakery',
  'Meat & Poultry': 'meat',
  'Fish & Seafood': 'seafood',
  Pantry: 'pantry',
  'Snacks & Sweets': 'snacks',
  Drinks: 'drinks',
  Frozen: 'frozen',
  Household: 'household',
}

function GroceryArt({ className }) {
  return <img className={className} src="/storefront/grocery-hero.webp" width="509" height="179" alt="" />
}

export function Landing({ onOpenAuth }) {
  return <section className="landing page" aria-label="FamilyCart">
    <div className="landing-copy">
      <h1>One cart for the whole family</h1>
      <p>Everyone adds what they need, changes show up for the whole household, and one person checks out.</p>
      <button className="big" onClick={onOpenAuth}>Start shopping</button>
    </div>
    <GroceryArt className="landing-art" />
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
  const busy = shop.unresolved.has(product.sku)
  const remainingStock = shop.stock ? (shop.stock[product.sku] ?? 0) - quantity : null
  if (quantity < 1 && !busy) {
    const soldOut = remainingStock === 0
    return <button className={`add-button${soldOut ? ' secondary' : ''}`} disabled={!shop.canEdit || remainingStock == null}
      aria-label={soldOut ? `${product.name} is out of stock` : `Add ${product.name}`} onClick={() => shop.onAdjust(product, 1)}>
      {soldOut ? 'Out of stock' : <>Add <Icon name="plus" size={13} /></>}
    </button>
  }
  return <Stepper label={product.name} quantity={quantity} disabled={!shop.canEdit} canRemove={shop.canRemove(product.sku)}
    remainingStock={remainingStock}
    removeHint="You cannot remove this right now" busy={busy} onAdjust={(delta) => shop.onAdjust(product, delta)} />
}

/** Only cards whose own state changed are drawn again. */
function sameCard(before, after) {
  const a = before.shop
  const b = after.shop
  const { sku } = after.product
  return before.product === after.product && before.onOpen === after.onOpen && before.onWish === after.onWish && before.wished === after.wished
    && a.quantities[sku] === b.quantities[sku] && a.unresolved.has(sku) === b.unresolved.has(sku) && a.canEdit === b.canEdit && a.adultsOnly === b.adultsOnly
    && a.needsHousehold === b.needsHousehold && a.canRemove(sku) === b.canRemove(sku) && a.onAdjust === b.onAdjust
    && Boolean(a.stock) === Boolean(b.stock) && a.stock?.[sku] === b.stock?.[sku]
}

const ProductCard = React.memo(function ProductCard({ product, shop, onOpen, onWish, wished }) {
  const pack = product.name.match(/^(.+),\s*((?:\d|Pack of\b).+)$/i)
  return <article className="product-card">
    <button className="product-open" onClick={() => onOpen(product)} aria-label={`View ${product.name}`}>
      <ProductImage product={product} />
      <span className="product-name">{pack?.[1] ?? product.name}</span>
      {pack && <small className="muted">{pack[2]}</small>}
    </button>
    <button className={`wish-button${wished ? ' on' : ''}`} aria-label={`Add ${product.name} to a list`} title="Add to a list" onClick={() => onWish(product)}>
      <Icon name="heart" size={17} /></button>
    <div className="product-bottom">
      <span className="price">{money(product.price_fils)}</span>
      <CartControl product={product} shop={shop} />
    </div>
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
  childAccount, products, loading, query, onClearQuery, shop, carts, household, roleName, checkoutBy, userName, peopleFor, listApis,
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
    const order = [...Object.keys(CATEGORY_ART), ...first.keys()]
    return [...new Set(order)].filter((name) => first.has(name)).map((name) => [name, first.get(name)])
  }, [products])
  useEffect(() => {
    if (category !== 'All' && products.length && !categories.some(([name]) => name === category)) setCategory('All')
  }, [categories, category, products.length])

  const results = useMemo(() => {
    const words = search.split(/\s+/).filter(Boolean)
    const found = products.filter((product) => (search || category === 'All' || product.category === category)
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
  const card = (product) => <ProductCard key={product.sku} product={product} shop={shop} onOpen={openProduct}
    onWish={setWishing} wished={wished.has(product.sku)} />
  const showHero = shelves
  const cartPicker = household && <div className="cart-picker">
    {carts.length > 0 ? <>
      <span>Add products to</span>
      <Dropdown label="Add products to" value={shop.cartId} icon="cart" onChange={onSelectCart}
        options={carts.map((cart) => ({
          value: cart.id, label: cart.name, icon: cart.adults_only ? 'lock' : 'cart',
          note: [cart.adults_only && 'Adults only', cart.id === shop.cartId && (total > 0 ? `${total} ${total === 1 ? 'item' : 'items'}` : 'Empty')]
            .filter(Boolean).join(' · '),
        }))} />
    </> : household.role === 'primary' ? <button className="secondary" onClick={onOpenCart}>View household carts</button>
      : <p className="muted">Ask the household admin for access to a cart.</p>}
    {showHero && <div className="shop-role"><Icon name="people" size={17} /> <span>{household.name} · {roleName}</span></div>}
  </div>

  return <div className="page shop-page">
    {showHero ? <section className="shop-intro">
      <div className="shop-intro-copy">
        <h1>{household ? 'Household shopping, delivered together' : `Hello, ${userName}`}</h1>
        <p>{household ? 'Shop groceries and everyday essentials in one shared cart.'
          : childAccount ? 'You no longer belong to a household.' : 'Create or join a household to start shopping together.'}</p>
      </div>
      <GroceryArt className="shop-intro-art" />
      {household ? cartPicker : !childAccount && <div className="shop-setup"><button onClick={onSetUpHousehold}>Set up household</button></div>}
    </section> : household && <div className="shop-tools">{cartPicker}</div>}

    {shop.cartId && <div className="banner row" role="status">
      <span>{shop.storeName ? `Delivery from ${shop.storeName}${!shop.stock && !shop.inventoryFailed ? ' · Checking stock' : ''}` : shop.canSetStore
        ? 'Set a delivery location before adding products.' : 'An eligible adult needs to set this cart’s delivery location.'}</span>
      {shop.inventoryFailed
        ? <button className="secondary" onClick={shop.retryInventory}>Retry stock check</button>
        : shop.canSetStore && <button className="secondary" onClick={onOpenCart}>{shop.storeName ? 'View cart' : 'Set delivery'}</button>}
    </div>}

    {checkoutBy && checkoutBy !== 'You' && <div className="banner" role="status"><strong>{checkoutBy} is checking out this cart</strong>
      <p>You can keep adding. New items wait and go into the next cart.</p></div>}

    {!search && <div className="categories" role="group" aria-label="Shop by category">
      {[['All', null], ...categories].map(([name, sample]) => <button key={name} aria-pressed={category === name}
        className="category" onClick={() => choose(name)}>
        <span className="category-tile">{CATEGORY_ART[name] ? <img src={`/storefront/${CATEGORY_ART[name]}.webp`} alt="" />
          : sample ? <ProductImage product={sample} /> : <Icon name="grid" size={33} />}</span>
        <span>{name}</span>
      </button>)}
    </div>}

    {loading ? <div className="product-grid" aria-busy="true">
      {Array.from({ length: 12 }, (_, index) => <div className="product-card skeleton" key={index} />)}
    </div> : shelves && categories.length === 0 ? <Empty icon={<Icon name="cart" size={38} />} title="No products available" />
      : shelves ? categories.map(([name]) => {
      const items = results.filter((product) => product.category === name)
      return <section className="shelf-section" key={name}>
        <div className="section-head"><h2>{name}</h2>
          {items.length > SHELF && <button className="text-button" onClick={() => choose(name)}>See all {items.length} <Icon name="arrow" size={16} /></button>}</div>
        <div className="shelf">{items.slice(0, SHELF).map(card)}</div>
      </section>
    }) : <section>
      <div className="section-head">
        <h2>{search ? `Results for “${query.trim()}”` : category === 'All' ? 'All products' : category}{' '}
          <small className="muted">{results.length}</small></h2>
        <Dropdown className="sorter" label="Sort products" value={sort} onChange={setSort}
          options={Object.entries(SORTS).map(([id, [label]]) => ({ value: id, label }))} />
      </div>
      {results.length === 0 ? <Empty icon={<Icon name="search" size={38} />} title="Nothing matches">
        <p>Try a shorter word, or browse the categories.</p>
        <button className="secondary" onClick={() => choose('All')}>Browse all products</button>
      </Empty> : <div className="product-grid">{results.map(card)}</div>}
    </section>}

    {shelves && !loading && products.length > 0 && <button className="secondary see-all" onClick={() => { setSort('name'); window.scrollTo({ top: 0 }) }}>
      View all {products.length} products</button>}

    {viewing && <ProductSheet product={viewing} products={products} shop={shop} people={peopleFor(viewing.sku)} wished={wished} onWish={setWishing}
      onOpen={openProduct} onClose={() => setViewingSku(null)} />}
    {wishing && <ListPicker product={wishing} listApis={listApis} onNotify={onNotify} onClose={() => setWishing(null)}
      onAdded={(sku) => setWished((current) => new Set(current).add(sku))} />}
  </div>
}
