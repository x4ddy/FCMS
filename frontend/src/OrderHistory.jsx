import React, { useEffect, useRef, useState } from 'react'
import { Empty, Icon, money } from './ui.jsx'

const placedDate = new Intl.DateTimeFormat('en-AE', { dateStyle: 'medium', timeStyle: 'short' })

export function OrderHistory({ account, onOpen, onShop }) {
  const [orders, setOrders] = useState([])
  const [next, setNext] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const activeRequest = useRef(null)
  const currentAccount = useRef(account)
  currentAccount.current = account

  async function load(before) {
    if (activeRequest.current) return
    const controller = new AbortController()
    activeRequest.current = controller
    setLoading(true)
    setError('')
    try {
      const result = await currentAccount.current.orders(before, controller.signal)
      if (controller.signal.aborted) return
      setOrders((previous) => before ? [...previous, ...result.orders] : result.orders)
      setNext(result.next_cursor)
    } catch (issue) {
      if (!controller.signal.aborted) setError(issue.message)
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null
        setLoading(false)
      }
    }
  }

  useEffect(() => {
    load(null)
    return () => {
      activeRequest.current?.abort()
      activeRequest.current = null
    }
  }, [])

  return <section className="page narrow">
    <h1>Order history</h1>
    <p className="muted">Orders you placed, including split deliveries.</p>
    {orders.length > 0 && <ul className="order-history-list">
      {orders.map((order) => <li key={order.order_id}>
        <button className="panel order-history-row" aria-label={`View order ${order.order_number}`}
          onClick={() => onOpen(order.order_id)}>
          <Icon name="orders" size={26} />
          <span className="order-history-main">
            <strong>{order.order_number}</strong>
            <time dateTime={order.placed_at}>{placedDate.format(new Date(order.placed_at))}</time>
            <span className="muted">{order.is_shared ? 'Shared order' : 'Personal order'} · {order.delivery_count}{' '}
              {order.delivery_count === 1 ? 'delivery' : 'deliveries'}</span>
          </span>
          <span className="order-history-total">
            <strong>{money(order.total_fils)}</strong>
            <span className="tag">{order.status === 'PLACED' ? 'Placed' : order.status}</span>
          </span>
        </button>
      </li>)}
    </ul>}
    {error && <p role="alert" className="notice error">{error}</p>}
    {loading ? <div role="status" aria-label="Loading orders">{[0, 1, 2].map((key) => <div className="panel skeleton order-skeleton" key={key} />)}</div> : error ?
      <button className="secondary order-history-more" onClick={() => load(next)}>Try again</button> : next ?
        <button className="secondary order-history-more" onClick={() => load(next)}>Load more orders</button> :
        !orders.length && <Empty icon="📦" title="No orders yet">
          <p>You haven’t placed any orders yet.</p><button onClick={onShop}>Start shopping</button></Empty>}
  </section>
}
