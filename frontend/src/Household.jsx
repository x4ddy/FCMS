import React, { useState } from 'react'
import { AddressBook, AddressForm } from './AddressBook.jsx'
import { Avatar, confirmAction, Dropdown, Icon } from './ui.jsx'

function HouseholdSetup({ busy, api, displayName, onCreate, onJoin }) {
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  // Nobody joins or creates a household without giving one delivery address.
  const [step, setStep] = useState(null)
  if (step) return <section className="panel">
    <button className="text-button" disabled={busy} onClick={() => setStep(null)}>← Back</button>
    <h2>Your delivery address</h2>
    <p className="muted">{step === 'create'
      ? `One address is needed to create ${name}. You can add more later.`
      : 'One address is needed to join. You and the household admin can see it.'}</p>
    <AddressForm api={api} busy={busy} displayName={displayName}
      buttonLabel={step === 'create' ? 'Save and create household' : 'Save and join household'}
      onSave={(address) => step === 'create' ? onCreate(name.trim(), address) : onJoin(code.trim(), address)} />
  </section>
  return <div className="two-columns">
    <form className="panel" onSubmit={(event) => {
      event.preventDefault()
      if (name.trim()) setStep('create')
    }}>
      <h2>Create a household</h2>
      <p className="muted">Share carts and lists with your family. You become the admin.</p>
      <label>Household name<input required maxLength="100" placeholder="The Sharma Family"
        value={name} onChange={(event) => setName(event.target.value)} /></label>
      <button disabled={busy || !name.trim()}>Next: your address</button>
    </form>
    <form className="panel" onSubmit={(event) => {
      event.preventDefault()
      if (code.trim()) setStep('join')
    }}>
      <h2>Join a household</h2>
      <p className="muted">Paste the private invitation code shared by a household.</p>
      <label>Invitation code<input required minLength="20" maxLength="200" placeholder="Paste invitation code"
        value={code} onChange={(event) => setCode(event.target.value)} /></label>
      <button disabled={busy || !code.trim()}>Next: your address</button>
    </form>
  </div>
}

export function AddressesPage({ required = false, household, userId, members, addresses, ...addressBook }) {
  const isPrimary = household.role === 'primary'
  const name = (id) => id === userId ? 'You' : members.find((member) => member.id === id)?.display_name ?? 'Member'
  return <section className="page narrow">
    <h1>{required ? 'Add your delivery address' : isPrimary ? 'Family addresses' : 'My addresses'}</h1>
    <p className="muted">{required ? `Everyone in ${household.name} needs at least one address.`
      : isPrimary ? 'As the admin, you can see every member’s addresses.'
        : 'You and the household admin can see these.'}</p>
    <AddressBook {...addressBook} addresses={required ? [] : addresses} startOpen={required}
      ownerName={isPrimary ? (address) => name(address.created_by) : undefined}
      buttonLabel={required ? 'Save and continue' : 'Add address'} />
  </section>
}

const ROLE_TEXT = {
  Admin: 'You are the admin: add and remove members, add items and check out.',
  'Primary user': 'You are a primary user: add items, check out, and ask the admin to add members.',
  'Child user': 'You are a child user: you can add items to carts.',
}

function RequestForm({ busy, onRequest }) {
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  return <form className="request-form" onSubmit={async (event) => {
    event.preventDefault()
    if (name.trim() && await onRequest(name.trim(), note.trim() || null)) { setName(''); setNote('') }
  }}>
    <input required maxLength="100" aria-label="Name of the person" placeholder="Their name" value={name} onChange={(event) => setName(event.target.value)} />
    <input maxLength="200" aria-label="Note for the admin" placeholder="Note for the admin (optional)" value={note} onChange={(event) => setNote(event.target.value)} />
    <button disabled={busy || !name.trim()}>Send request</button>
  </form>
}

function MemberRow({ member, household, session, busy, onDecideAge, onTransfer, onRevoke }) {
  const role = member.id === session.user.id ? household.role : member.role
  const manage = household.role === 'primary' && member.role !== 'primary'
  return <li className="member-row">
    <Avatar id={member.id} name={member.display_name} size="large" />
    <div className="member-text">
      <strong>{member.display_name}{member.id === session.user.id && <span className="muted"> (you)</span>}</strong>
      <span>
        {/* Members only see their own role. */}
        {role && <span className={`tag ${role === 'primary' ? 'accent' : ''}`}>{role === 'primary' ? 'Admin' : member.is_adult ? 'Primary user' : 'Child user'}</span>}
        {member.age_status === 'PENDING' && <span className="tag warn">
          Age check{member.claimed_age != null ? `: says ${member.claimed_age}` : ' waiting'}</span>}
        {member.age_status === 'REJECTED' && <span className="tag bad">Age not approved</span>}
      </span>
    </div>
    {manage && <div className="row-actions">
      {session.is_adult && ['PENDING', 'REJECTED'].includes(member.age_status) &&
        <button disabled={busy} onClick={() => onDecideAge(member.id, true)}>Make primary user</button>}
      {session.is_adult && ['PENDING', 'APPROVED'].includes(member.age_status) && (member.is_adult || member.age_status === 'PENDING') &&
        <button className="secondary" disabled={busy} onClick={() => onDecideAge(member.id, false)}>{member.age_status === 'PENDING' ? 'Keep as child' : 'Make child user'}</button>}
      <button className="secondary" disabled={busy} onClick={async () => {
        if (await confirmAction({
          title: `Make ${member.display_name} the admin?`,
          text: 'They will manage members, carts and checkout, and see every address. You stop being the admin.', action: 'Transfer ownership',
        })) onTransfer(member.id)
      }}>Make admin</button>
      <button className="secondary danger" disabled={busy} onClick={async () => {
        if (await confirmAction({
          title: `Remove ${member.display_name}?`, text: 'They lose access to this household’s carts and lists straight away.',
          action: 'Remove member', danger: true,
        })) onRevoke(member.id)
      }}>Remove</button>
    </div>}
  </li>
}

export function HouseholdPage({
  household, households, session, account, carts, members, addresses, busy, invitation, roleName, memberRequests,
  onRequestMember, onDecideRequest, onCollectInvitation,
  onLeave, onDelete, onCreate, onJoin, onSelectHousehold, onInvite, onNotify, onRevoke, onDecideAge, onTransferHousehold,
  onOpenAddresses, onOpenLists, onOpenCart,
}) {
  if (!household) return <section className="page narrow">
    <h1>Set up your household</h1>
    <p className="muted">A household has one admin, and as many primary users and child users as you like.</p>
    <HouseholdSetup api={account} busy={busy} displayName={session.user.display_name} onCreate={onCreate} onJoin={onJoin} />
  </section>
  const self = { id: session.user.id, display_name: session.user.display_name, role: household.role, is_adult: session.is_adult, age_status: session.age_status }
  const shown = members.length > 0 ? members : [self]
  return <section className="page">
    <div className="section-head">
      <div><h1>{household.name}</h1><p className="muted">{ROLE_TEXT[roleName]}</p></div>
      {households.length > 1 && <div className="cart-picker"><span>Active household</span>
        <Dropdown label="Active household" value={household.id} icon="home" onChange={onSelectHousehold}
          options={households.map((item) => ({ value: item.id, label: item.name, note: item.role === 'primary' ? 'Admin' : 'Member' }))} /></div>}
    </div>
    {session.age_status === 'PENDING' && <div className="banner" role="status">
      <strong>Your age is waiting for approval</strong>
      <p>The admin checks it. Until then you are a child user.</p>
    </div>}
    {session.age_status === 'REJECTED' && <div className="banner" role="status">
      <strong>Your age was not approved</strong>
      <p>You are a child user in this household. Contact the admin if this is wrong.</p>
    </div>}
    <section className="panel">
      <div className="section-head"><h2>Members</h2>
        {household.role === 'primary' && <button onClick={onInvite} disabled={busy}><Icon name="plus" size={16} /> Invite member</button>}
      </div>
      {invitation && <div className="invite-code">
        <small>Private one-time invitation code</small><code>{invitation.invitation_code}</code>
        <button className="secondary" onClick={() => navigator.clipboard.writeText(invitation.invitation_code)
          .then(() => onNotify('Invitation code copied.'), () => onNotify('Copying failed. Select the code and copy it.', 'error'))}>Copy</button>
      </div>}
      <ul className="member-list">
        {shown.map((member) => <MemberRow key={member.id} member={member} household={household} session={session} busy={busy}
          onDecideAge={onDecideAge} onTransfer={onTransferHousehold} onRevoke={onRevoke} />)}
      </ul>
    </section>
    {roleName !== 'Child user' && (roleName === 'Primary user' || memberRequests.length > 0) && <section className="panel">
      <h2>{roleName === 'Admin' ? 'Requests to add members' : 'Ask the admin to add someone'}</h2>
      {roleName === 'Primary user' && <RequestForm busy={busy} onRequest={onRequestMember} />}
      {memberRequests.length > 0 && <ul className="member-list">{memberRequests.map((item) => <li className="member-row" key={item.id}>
        <div className="member-text"><strong>{item.name}</strong>
          <small className="muted">{[roleName === 'Admin' && `Asked by ${shown.find((member) => member.id === item.requested_by)?.display_name ?? 'a member'}`, item.note]
            .filter(Boolean).join(' · ')}</small></div>
        {roleName === 'Admin' ? <div className="row-actions">
          <button disabled={busy} onClick={() => onDecideRequest(item.id, true)}>Approve</button>
          <button className="secondary danger" disabled={busy} onClick={() => onDecideRequest(item.id, false)}>Decline</button>
        </div> : item.status === 'APPROVED' ? <button disabled={busy} onClick={() => onCollectInvitation(item.id)}>Get invitation code</button>
          : <span className="tag warn">Waiting for the admin</span>}
      </li>)}</ul>}
    </section>}
    <div className="link-cards">
      <button className="link-card" onClick={onOpenCart}><Icon name="cart" size={26} /><strong>Shared carts</strong>
        <span className="muted">{carts.length} {carts.length === 1 ? 'cart' : 'carts'} you can use</span></button>
      <button className="link-card" onClick={onOpenLists}><Icon name="list" size={26} /><strong>Shopping lists</strong>
        <span className="muted">Plan together, then pick what goes in a cart</span></button>
      <button className="link-card" onClick={onOpenAddresses}><Icon name="pin" size={26} /><strong>{household.role === 'primary' ? 'Family addresses' : 'My addresses'}</strong>
        <span className="muted">{addresses.length} saved</span></button>
    </div>
    <section className="panel leave-panel">
      {roleName === 'Admin' ? <>
        <div><h2>Delete household</h2><p className="muted">Ends {household.name} for everyone and erases its carts, lists and addresses.
          To leave without deleting it, make another member the admin first.</p></div>
        <button className="danger-solid" disabled={busy} onClick={async () => {
          if (await confirmAction({
            title: `Delete ${household.name}?`, type: household.name, action: 'Delete household', danger: true,
            text: 'All members lose access. Carts, lists and addresses are erased. This can’t be undone. Placed orders stay in each buyer’s history.',
          })) onDelete()
        }}>Delete household</button>
      </> : <>
        <div><h2>Exit household</h2><p className="muted">You lose access to its carts and lists. What you added stays. You need a new invitation to return.</p></div>
        <button className="secondary danger" disabled={busy} onClick={async () => {
          if (await confirmAction({
            title: `Exit ${household.name}?`, text: 'You need a new invitation to come back.', action: 'Exit household', danger: true,
          })) onLeave()
        }}>Exit household</button>
      </>}
    </section>
    <details className="fold">
      <summary>Create or join another household</summary>
      <HouseholdSetup key={household.id} api={account} busy={busy} displayName={session.user.display_name}
        onCreate={onCreate} onJoin={onJoin} />
    </details>
  </section>
}
