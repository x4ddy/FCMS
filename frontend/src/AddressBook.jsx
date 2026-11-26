import React, { useEffect, useMemo, useRef, useState } from 'react'
import { DeliveryMap } from './DeliveryMap.jsx'
import { confirmAction, Icon, IconTile } from './ui.jsx'

function coordinateFields(pin) {
  return { latitude: String(pin?.latitude ?? ''), longitude: String(pin?.longitude ?? '') }
}

function readPin({ latitude, longitude }) {
  if (!latitude.trim() || !longitude.trim()) return null
  const lat = Number(latitude)
  const lng = Number(longitude)
  if (!Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180) return null
  return { latitude: lat, longitude: lng }
}

function useCoverage(api, addressId, latitude, longitude) {
  const [attempt, setAttempt] = useState(0)
  const [settled, setSettled] = useState(null)
  const lookup = useMemo(() => latitude === null || longitude === null ? null : {
    api, addressId, latitude, longitude,
  }, [api, addressId, latitude, longitude])

  useEffect(() => {
    if (!lookup) return
    const controller = new AbortController()
    const { signal } = controller
    const timer = setTimeout(async () => {
      try {
        const result = lookup.addressId
          ? await lookup.api.resolveAddress(lookup.addressId, signal)
          : await lookup.api.previewPin({ latitude: lookup.latitude, longitude: lookup.longitude }, signal)
        if (!signal.aborted) setSettled({ lookup, attempt, result })
      } catch (error) {
        if (!signal.aborted) setSettled({ lookup, attempt, error })
      }
    }, lookup.addressId ? 0 : 220)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [lookup, attempt])

  return {
    pending: Boolean(lookup) && (settled?.lookup !== lookup || settled.attempt !== attempt),
    current: lookup && settled?.lookup === lookup && settled.attempt === attempt ? settled : null,
    retry: () => setAttempt((current) => current + 1),
  }
}

const emptyStores = []

function PinCoverage({ api, fields, onChange, addressId, busy, emptyMessage = 'Address needs a map pin.' }) {
  const [storeState, setStoreState] = useState(null)
  const [storeAttempt, setStoreAttempt] = useState(0)
  const [locating, setLocating] = useState(false)
  const [locationError, setLocationError] = useState('')
  const [dragging, setDragging] = useState(false)
  const locationRequest = useRef(0)
  const pin = readPin(fields)
  const editable = Boolean(onChange)
  const { pending, current, retry } = useCoverage(api, addressId,
    dragging ? null : pin?.latitude ?? null, dragging ? null : pin?.longitude ?? null)

  useEffect(() => {
    const controller = new AbortController()
    api.mapStores(controller.signal).then((stores) => {
      if (!controller.signal.aborted) setStoreState({ api, storeAttempt, stores })
    }, () => {
      if (!controller.signal.aborted) setStoreState({ api, storeAttempt, failed: true })
    })
    return () => controller.abort()
  }, [api, storeAttempt])

  useEffect(() => () => { locationRequest.current += 1 }, [])

  function changeFields(next) {
    locationRequest.current += 1
    setLocating(false)
    setLocationError('')
    setDragging(false)
    onChange(next)
  }

  function startDrag() {
    locationRequest.current += 1
    setLocating(false)
    setLocationError('')
    setDragging(true)
  }

  function useLocation() {
    const request = ++locationRequest.current
    setLocationError('')
    if (!navigator.geolocation) {
      setLocationError('Location is unavailable. Click the map or enter coordinates.')
      return
    }
    setLocating(true)
    navigator.geolocation.getCurrentPosition(({ coords }) => {
      if (request === locationRequest.current) changeFields(coordinateFields(coords))
    }, () => {
      if (request !== locationRequest.current) return
      setLocating(false)
      setLocationError('Location could not be read. Click the map or enter coordinates.')
    }, { timeout: 8000 })
  }

  const stores = storeState?.api === api ? storeState.stores ?? emptyStores : emptyStores
  const storeLookupFailed = storeState?.api === api && storeState.storeAttempt === storeAttempt && storeState.failed
  const result = current?.result
  const error = current?.error

  return <div className="delivery-pin-panel">
    <DeliveryMap stores={stores} latitude={pin?.latitude ?? null} longitude={pin?.longitude ?? null}
      selectedStore={result?.store_id}
      onDragStart={startDrag}
      onPick={editable && !busy ? ({ lat, lng }) => changeFields(coordinateFields({ latitude: lat, longitude: lng })) : null} />
    {editable && <div className="delivery-map-status">
      <p>Tap the map, drag the pin, or enter coordinates.</p>
      <button type="button" className="text-button" disabled={busy || locating} onClick={useLocation}>
        <Icon name="pin" size={16} />{locating ? 'Locating…' : 'Use my location'}
      </button>
    </div>}
    {storeLookupFailed && <div className="delivery-map-status">
      <p role="status">Store markers could not load.</p>
      <button type="button" className="text-button" onClick={() => setStoreAttempt((current) => current + 1)}>Retry stores</button>
    </div>}
    {editable && <div className="delivery-coordinates">
      <label>Latitude<input type="number" min="-90" max="90" step="any" disabled={busy}
        value={fields.latitude} onChange={(event) => changeFields({ ...fields, latitude: event.target.value })} /></label>
      <label>Longitude<input type="number" min="-180" max="180" step="any" disabled={busy}
        value={fields.longitude} onChange={(event) => changeFields({ ...fields, longitude: event.target.value })} /></label>
    </div>}
    {locationError && <p className="muted" role="status">{locationError}</p>}
    <div className="delivery-coverage" role="status">
      {dragging ? 'Move the pin to check coverage.' :
        !pin ? (fields.latitude || fields.longitude ? 'Enter a valid latitude and longitude.' : emptyMessage) :
        pending ? 'Checking delivery coverage…' :
        result?.status === 'DELIVERABLE' ? <>
          <strong className="deliverable">Deliverable</strong>
          <span>{result.store_name} · {result.distance_km.toFixed(2)} km straight-line distance</span>
        </> : result?.status === 'OUTSIDE_DELIVERY_AREA' ? <strong>Outside delivery area</strong> :
        error?.status === 409 ? 'Address needs a map pin.' :
        error?.status === 404 || error?.status === 403 ? 'This address is no longer accessible.' :
        error ? <>
          <span>Delivery coverage is unavailable.</span>
          <button type="button" className="text-button" onClick={retry}>Retry coverage</button>
        </> : null}
    </div>
  </div>
}

export function AddressForm({ busy, displayName, api, onSave, buttonLabel }) {
  const initialAddress = () => ({
    label: 'Home', recipient_name: displayName, address_line1: '', address_line2: '',
    city: '', postal_code: '', phone: '',
  })
  const [address, setAddress] = useState(initialAddress)
  const [fields, setFields] = useState(coordinateFields)
  const [confirmedPin, setConfirmedPin] = useState(null)
  const showDetails = confirmedPin !== null
  const stepHeading = useRef(null)
  const previousStep = useRef(showDetails)
  const pin = readPin(fields)
  const field = (name) => ({
    value: address[name],
    onChange: (event) => {
      const { value } = event.target
      setAddress((current) => ({ ...current, [name]: value }))
    },
  })

  useEffect(() => {
    if (showDetails !== previousStep.current) stepHeading.current?.focus()
    previousStep.current = showDetails
  }, [showDetails])

  async function submitAddress(event) {
    event.preventDefault()
    if (busy || !confirmedPin) return
    if (await onSave({ ...address, ...confirmedPin })) {
      setAddress(initialAddress())
      setFields(coordinateFields())
      setConfirmedPin(null)
    }
  }

  return <div className="address-wizard">
    <ol className="address-steps" aria-label="Add address progress">
      <li aria-current={showDetails ? undefined : 'step'}><span aria-hidden="true">{showDetails ? <Icon name="check" size={16} /> : 1}</span> Select location</li>
      <li aria-current={showDetails ? 'step' : undefined}><span aria-hidden="true">2</span> Address details</li>
    </ol>
    {showDetails ? <section>
      <h3 ref={stepHeading} tabIndex={-1}>Enter the full address</h3>
      <div className="address-pin-summary">
        <div>
          <strong>Delivery pin selected</strong>
          <small>{confirmedPin.latitude.toFixed(5)}, {confirmedPin.longitude.toFixed(5)}</small>
        </div>
        <button type="button" className="secondary" disabled={busy} onClick={() => setConfirmedPin(null)}>Change pin</button>
      </div>
      <form className="address-form" onSubmit={submitAddress}>
        <label>Label<input required maxLength="100" disabled={busy} {...field('label')} /></label>
        <label>Recipient<input required maxLength="100" autoComplete="shipping name" disabled={busy} {...field('recipient_name')} /></label>
        <label className="wide">Address line 1<input required maxLength="160" autoComplete="shipping address-line1"
          disabled={busy} {...field('address_line1')} /></label>
        <label className="wide">Address line 2 <span className="muted">(optional)</span><input maxLength="160"
          autoComplete="shipping address-line2" disabled={busy} {...field('address_line2')} /></label>
        <label>City<input required maxLength="100" autoComplete="shipping address-level2" disabled={busy} {...field('city')} /></label>
        <label>Postal code <span className="muted">(optional)</span><input maxLength="24" autoComplete="shipping postal-code"
          disabled={busy} {...field('postal_code')} /></label>
        <label>Phone <span className="muted">(optional)</span><input type="tel" maxLength="24" autoComplete="shipping tel"
          disabled={busy} {...field('phone')} /></label>
        <div className="address-step-actions wide"><button type="submit" disabled={busy}>{buttonLabel}</button></div>
      </form>
    </section> : <section>
      <h3 ref={stepHeading} tabIndex={-1}>Set your delivery pin</h3>
      <PinCoverage api={api} fields={fields} onChange={setFields} busy={busy} emptyMessage="Place a pin to continue." />
      <div className="address-step-actions">
        <button type="button" disabled={busy || !pin} onClick={() => setConfirmedPin(pin)}>Continue to address details <Icon name="arrow" size={17} /></button>
      </div>
    </section>}
  </div>
}

function SavedAddressMap({ address, api, canEdit, busy, onUpdatePin, onClose }) {
  const [draft, setDraft] = useState(() => canEdit && address.latitude == null ? coordinateFields() : null)
  const editing = draft !== null && canEdit

  return <div className="saved-address-map">
    <h3>{address.label} delivery coverage</h3>
    <PinCoverage key={editing ? 'edit' : 'view'} api={api} fields={editing ? draft : coordinateFields(address)}
      addressId={editing ? undefined : address.id} onChange={editing ? setDraft : null} busy={busy} />
    <div className="address-actions">
      {editing ? <>
        <button type="button" disabled={busy || !readPin(draft)} onClick={async () => {
          const pin = readPin(draft)
          if (pin && await onUpdatePin(address.id, pin)) setDraft(null)
        }}>Save map pin</button>
        {address.latitude != null && <button type="button" className="secondary" disabled={busy}
          onClick={() => setDraft(null)}>Cancel move</button>}
      </> : canEdit && <button type="button" className="secondary" disabled={busy}
        onClick={() => setDraft(coordinateFields(address))}>Move pin</button>}
      <button type="button" className="text-button" disabled={busy} onClick={onClose}>Close coverage</button>
    </div>
  </div>
}

export function AddressBook({ addresses, api, displayName, busy, onSave, onDelete, onUpdatePin,
  canEdit = () => true, buttonLabel = 'Save address', ownerName, startOpen = false, split = false }) {
  const [selectedId, setSelectedId] = useState(null)
  const [adding, setAdding] = useState(startOpen || split)
  const addButton = useRef(null)
  const formHeading = useRef(null)
  const focusTarget = useRef(null)

  useEffect(() => {
    focusTarget.current?.current?.focus()
    focusTarget.current = null
  }, [adding])

  function openForm() {
    if (adding) formHeading.current?.focus()
    else {
      focusTarget.current = formHeading
      setAdding(true)
    }
  }

  function closeForm() {
    focusTarget.current = addButton
    setAdding(false)
  }

  const form = <AddressForm api={api} busy={busy} displayName={displayName} buttonLabel={buttonLabel}
    onSave={async (address) => {
      const saved = await onSave(address)
      if (saved && !startOpen) closeForm()
      return saved
    }} />
  const saved = addresses.length > 0 && <ul className="address-list">{addresses.map((address) => {
      const selected = selectedId === address.id
      const editable = canEdit(address)
      const label = address.label.toLowerCase()
      return <li key={address.id} className={`panel saved-address${selected ? ' selected' : ''}`}>
        <div className="address-card">
          <IconTile name={label === 'home' ? 'home' : 'pin'} />
          <div className="address-card-text">
            <strong>{address.label}{ownerName && <span className="tag">{ownerName(address)}</span>}</strong>
            <span>{address.recipient_name}</span>
            <small className="muted">{address.address_line1}{address.address_line2 && `, ${address.address_line2}`}, {address.city}</small>
          </div>
          <div className="row-actions">
            <button type="button" className="secondary" disabled={busy} aria-expanded={selected}
              onClick={() => setSelectedId(selected ? null : address.id)}>
              {selected ? 'Hide map' : address.latitude == null && editable ? 'Add map pin' : 'Check coverage'}
            </button>
            {onDelete && editable && <button className="secondary danger" disabled={busy} onClick={async () => {
              if (await confirmAction({ title: `Remove ${address.label}?`, text: `${address.address_line1}, ${address.city}`, action: 'Remove', danger: true })) {
                onDelete(address.id)
              }
            }}>Remove</button>}
          </div>
        </div>
        {selected && <SavedAddressMap address={address} api={api} canEdit={editable} busy={busy}
          onUpdatePin={onUpdatePin} onClose={() => setSelectedId(null)} />}
      </li>
    })}</ul>
  const add = <button type="button" className="secondary add-address" ref={addButton} onClick={openForm}><Icon name="plus" size={18} /> Add a new address</button>
  const editor = adding && <section className="panel address-editor">
    {!startOpen && <div className="section-head">
      <h2 ref={formHeading} tabIndex={-1}>Add a new address</h2>
      <button type="button" className="text-button" disabled={busy} onClick={closeForm}>Cancel</button>
    </div>}
    {form}
  </section>

  return <div className={`address-book${split ? ' address-book-split' : ''}`}>
    {split ? <>
      <section className="saved-addresses">
        <div className="section-head"><h2>Saved addresses</h2><small className="muted">{addresses.length} {addresses.length === 1 ? 'address' : 'addresses'}</small></div>
        {saved || <p className="panel muted">No addresses saved yet.</p>}
        {add}
      </section>
      {editor}
    </> : <>{saved}{editor || add}</>}
  </div>
}
