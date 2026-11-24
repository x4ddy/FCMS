import React, { useEffect, useRef, useState } from 'react'

const leafletUrl = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet'
const tileUrl = import.meta.env.VITE_MAP_TILE_URL ?? 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
const tileAttribution = import.meta.env.VITE_MAP_TILE_ATTRIBUTION ||
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
const uaeCenter = [24.85, 55.15]
let leafletPromise

function loadLeaflet() {
  if (leafletPromise) return leafletPromise

  const css = document.createElement('link')
  css.rel = 'stylesheet'
  css.href = `${leafletUrl}.css`
  css.integrity = 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY='
  const script = document.createElement('script')
  script.src = `${leafletUrl}.js`
  script.integrity = 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo='

  function load(element) {
    return new Promise((resolve, reject) => {
      element.crossOrigin = 'anonymous'
      element.onload = resolve
      element.onerror = reject
      document.head.append(element)
    })
  }

  let timeout
  const deadline = new Promise((_, reject) => { timeout = setTimeout(reject, 8000) })
  leafletPromise = Promise.race([Promise.all([load(css), load(script)]), deadline])
    .then(() => {
      if (!window.L) throw new Error('Leaflet did not load')
      return window.L
    })
    .catch((error) => {
      css.remove()
      script.remove()
      leafletPromise = null
      throw error
    })
    .finally(() => clearTimeout(timeout))
  return leafletPromise
}

function LeafletMap({ leaflet: L, stores, latitude, longitude, selectedStore, onPick, onDragStart }) {
  const container = useRef(null)
  const mapRef = useRef(null)
  const tileLayer = useRef(null)
  const markers = useRef(new Map())
  const handlers = useRef({ onPick, onDragStart })
  const [tileFailed, setTileFailed] = useState(false)
  const editable = Boolean(onPick)

  useEffect(() => { handlers.current = { onPick, onDragStart } }, [onPick, onDragStart])

  useEffect(() => {
    const map = L.map(container.current, { worldCopyJump: true }).setView(uaeCenter, 7)
    mapRef.current = map
    if (tileUrl) {
      tileLayer.current = L.tileLayer(tileUrl, { attribution: tileAttribution, maxZoom: 19 })
        .on('tileerror', () => setTileFailed(true)).addTo(map)
    }
    map.on('click', ({ latlng }) => handlers.current.onPick?.(latlng.wrap()))
    const resize = new ResizeObserver(() => map.invalidateSize({ pan: false }))
    resize.observe(container.current)
    return () => {
      resize.disconnect()
      map.remove()
      mapRef.current = null
      tileLayer.current = null
    }
  }, [L])

  useEffect(() => {
    const map = mapRef.current
    const layers = L.layerGroup().addTo(map)
    const bounds = L.latLngBounds([])
    const currentMarkers = new Map()
    for (const store of stores) {
      const location = [store.latitude, store.longitude]
      const circle = L.circle(location, { radius: store.radius_km * 1000, interactive: false }).addTo(layers)
      const label = document.createElement('span')
      label.textContent = store.name
      const marker = L.circleMarker(location, { radius: 7, bubblingMouseEvents: true })
        .bindTooltip(label).addTo(layers)
      bounds.extend(circle.getBounds())
      currentMarkers.set(store.store_id, { circle, marker })
    }
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [16, 16], maxZoom: 8, animate: false })
    markers.current = currentMarkers
    return () => layers.remove()
  }, [L, stores])

  useEffect(() => {
    for (const [id, { circle, marker }] of markers.current) {
      const selected = id === selectedStore
      const color = selected ? '#137443' : '#386bc0'
      circle.setStyle({ color, fillOpacity: selected ? 0.22 : 0.08, weight: selected ? 3 : 1.5 })
      marker.setStyle({ color, fillColor: color, fillOpacity: 1, weight: 2 })
      if (selected) marker.bringToFront()
    }
  }, [stores, selectedStore])

  const pinMarker = useRef(null)
  useEffect(() => {
    const map = mapRef.current
    const marker = L.marker(uaeCenter, {
      title: 'Address pin', draggable: editable,
      icon: L.divIcon({ className: 'delivery-pin', iconSize: [22, 22], iconAnchor: [11, 11] }),
    })
    marker.on('dragstart', () => handlers.current.onDragStart?.())
    marker.on('dragend', () => handlers.current.onPick?.(marker.getLatLng().wrap()))
    pinMarker.current = marker
    return () => { marker.remove(); pinMarker.current = null }
  }, [L, editable])

  useEffect(() => {
    const marker = pinMarker.current
    if (latitude === null || longitude === null) {
      marker.remove()
      return
    }
    const location = [latitude, longitude]
    marker.setLatLng(location).addTo(mapRef.current)
    mapRef.current.panInside(location, { padding: [24, 24], animate: false })
  }, [latitude, longitude, stores, editable])

  return <>
    <div className={`delivery-map${editable ? ' editable' : ''}`} ref={container}
      aria-label="Delivery map with store coverage" />
    {(!tileUrl || tileFailed) && <div className="delivery-map-status">
      <p role="status">Background map tiles are unavailable. Pins and coverage still work.</p>
      {tileUrl && <button type="button" className="text-button" onClick={() => {
        setTileFailed(false)
        tileLayer.current?.redraw()
      }}>Retry tiles</button>}
    </div>}
  </>
}

export function DeliveryMap(props) {
  const [library, setLibrary] = useState({})
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let current = true
    loadLeaflet().then((leaflet) => {
      if (current) setLibrary({ leaflet })
    }, () => {
      if (current) setLibrary({ failed: true })
    })
    return () => { current = false }
  }, [attempt])

  if (library.leaflet) return <LeafletMap leaflet={library.leaflet} {...props} />
  if (library.failed) return <div className="delivery-map-status">
    <p role="status">The map could not load. You can check delivery using coordinates.</p>
    <button type="button" className="text-button" onClick={() => {
      setLibrary({})
      setAttempt((current) => current + 1)
    }}>Retry map</button>
  </div>
  return <p className="muted" role="status">Loading map…</p>
}
