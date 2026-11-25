import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { avatarHue, initials } from './listState.js'

const currency = new Intl.NumberFormat('en-AE', { style: 'currency', currency: 'AED' })
export const money = (fils) => currency.format(fils / 100)

const paths = {
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 4 4',
  cart: 'M3 4h2l2.4 11h10.2L20 7H6M9 20h.01M17 20h.01',
  pin: 'M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11Zm0-8.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z',
  home: 'M4 11l8-7 8 7v9h-5v-6H9v6H4z',
  list: 'M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01',
  orders: 'M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6',
  people: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm-6 9c0-3.3 2.7-6 6-6s6 2.7 6 6m1-16a3.5 3.5 0 0 1 0 7m1 3c2.4.6 4 3 4 6',
  bell: 'M6 17V11a6 6 0 1 1 12 0v6l1.5 1.5h-15zM10 21h4',
  close: 'M6 6l12 12M18 6L6 18',
  trash: 'M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13',
  chevron: 'M6 9l6 6 6-6',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  heart: 'M12 20s-7.5-4.6-9-9.3C2 7.4 4.2 5 7 5c2 0 3.8 1.1 5 3 1.2-1.9 3-3 5-3 2.8 0 5 2.4 4 5.7C19.5 15.4 12 20 12 20Z',
  lock: 'M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3',
}

export function Icon({ name, size = 20 }) {
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>
}

export function Avatar({ id, name, size = 'small' }) {
  return <span className={`avatar ${size}`} title={name} aria-label={name}
    style={{ '--hue': avatarHue(id) }}>{initials(name)}</span>
}

/** A product picture that falls back to a letter when the image cannot load. */
export function ProductImage({ product, className = '' }) {
  const [failed, setFailed] = useState(null)
  if (!product?.image_url || failed === product.image_url) {
    return <span className={`product-image missing ${className}`} aria-hidden="true">{product?.name?.[0] ?? '?'}</span>
  }
  return <img className={`product-image ${className}`} src={product.image_url} alt="" loading="lazy" decoding="async"
    onError={() => setFailed(product.image_url)} />
}

/** − n + with large targets. */
export function Stepper({ label, quantity, onAdjust, disabled, canRemove = true, removeHint }) {
  return <span className="stepper" role="group" aria-label={`Quantity for ${label}`}>
    <button type="button" aria-label={`Remove one ${label}`} disabled={disabled || quantity < 1 || !canRemove}
      title={canRemove ? undefined : removeHint} onClick={() => onAdjust(-1)}><Icon name="minus" size={16} /></button>
    <strong key={quantity}>{quantity}</strong>
    <button type="button" aria-label={`Add one ${label}`} disabled={disabled || quantity >= 99}
      onClick={() => onAdjust(1)}><Icon name="plus" size={16} /></button>
  </span>
}

const focusable = 'a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),summary,[tabindex="0"]'
const opened = []  // overlays from bottom to top; only the top one reacts to the keyboard

/** A dialog above the page: traps focus, closes on Escape or a click outside, and restores focus. */
export function Overlay({ kind = 'sheet', label, onClose, children }) {
  const panel = useRef(null)
  const close = useRef(onClose)
  close.current = onClose
  const timer = useRef(null)
  const [closing, setClosing] = useState(false)
  const leave = () => {
    if (timer.current) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { close.current(); return }
    setClosing(true)
    timer.current = window.setTimeout(() => close.current(), 170)
  }
  useEffect(() => {
    const token = {}
    opened.push(token)
    const before = document.activeElement
    document.body.classList.add('locked')
    panel.current?.focus({ preventScroll: true })
    function onKey(event) {
      if (opened.at(-1) !== token) return
      if (event.key === 'Escape') { event.preventDefault(); leave() }
      if (event.key !== 'Tab') return
      const items = [...panel.current.querySelectorAll(focusable)].filter((item) => item.offsetParent !== null)
      if (items.length === 0) return
      const at = document.activeElement
      if (event.shiftKey && (at === items[0] || at === panel.current)) { event.preventDefault(); items.at(-1).focus() }
      else if (!event.shiftKey && at === items.at(-1)) { event.preventDefault(); items[0].focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(timer.current)
      document.removeEventListener('keydown', onKey)
      opened.splice(opened.indexOf(token), 1)
      if (opened.length === 0) document.body.classList.remove('locked')
      before?.focus?.({ preventScroll: true })
    }
  }, [])
  return createPortal(<div className={`overlay ${kind}${closing ? ' closing' : ''}`}
    onMouseDown={(event) => { if (event.target === event.currentTarget) leave() }}>
    <section className="overlay-panel" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} ref={panel}>
      {typeof children === 'function' ? children(leave) : children}
    </section>
  </div>, document.body)
}

let ask = null
/** Ask before something that cannot be undone. Resolves to true or false. */
export function confirmAction(options) {
  return new Promise((resolve) => ask ? ask({ ...options, resolve }) : resolve(false))
}

export function ConfirmHost() {
  const [request, setRequest] = useState(null)
  const [typed, setTyped] = useState('')
  useEffect(() => { ask = (next) => { setTyped(''); setRequest(next) }; return () => { ask = null } }, [])
  if (!request) return null
  const answer = (value) => { request.resolve(value); setRequest(null) }
  return <Overlay kind="dialog" label={request.title} onClose={() => answer(false)}>
    <h2>{request.title}</h2>
    {request.text && <p className="muted">{request.text}</p>}
    {request.type && <label>Type “{request.type}” to confirm
      <input value={typed} autoComplete="off" onChange={(event) => setTyped(event.target.value)} /></label>}
    <div className="dialog-actions">
      <button className="secondary" onClick={() => answer(false)}>Cancel</button>
      <button className={request.danger ? 'danger-solid' : ''} disabled={Boolean(request.type) && typed.trim() !== request.type.trim()} onClick={() => answer(true)}>{request.action}</button>
    </div>
  </Overlay>
}

export function Empty({ icon, title, children }) {
  return <div className="empty-state"><span aria-hidden="true">{icon}</span><h2>{title}</h2>{children}</div>
}
