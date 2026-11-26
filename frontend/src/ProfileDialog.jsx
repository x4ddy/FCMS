import React, { useState } from 'react'
import { Avatar, Icon, Overlay } from './ui.jsx'

export function ProfileDialog({ user, household, roleName, onSave, onClose }) {
  const [draft, setDraft] = useState(user.display_name)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const name = draft.trim()
  const canSave = name && name !== user.display_name && !saving

  async function submit(event) {
    event.preventDefault()
    if (!canSave) return
    setSaving(true)
    setError('')
    try {
      setDraft(await onSave(name))
      setSaved(true)
    } catch (problem) {
      setError(problem.message)
    } finally {
      setSaving(false)
    }
  }

  return <Overlay kind="dialog profile" label="Your profile" onClose={onClose}>
    {(close) => <>
      <div className="profile-heading">
        <Avatar id={user.id} name={user.display_name} size="large" />
        <h2>Your profile</h2>
        <button type="button" className="icon-button" aria-label="Close profile" onClick={close}><Icon name="close" /></button>
      </div>
      <form onSubmit={submit}>
        <label>Username
          <input name="display_name" autoComplete="nickname" value={draft} required maxLength={100} disabled={saving}
            onChange={(event) => { setDraft(event.target.value); setSaved(false); setError('') }} />
        </label>
        {error && <p className="notice error" role="alert">{error}</p>}
        <div className="dialog-actions">
          {saved && <span role="status">Username saved</span>}
          <button type="submit" disabled={!canSave}>{saving ? 'Saving…' : 'Save username'}</button>
        </div>
      </form>
      {(user.email || household) && <dl>
        {user.email && <><dt>Email</dt><dd>{user.email}</dd></>}
        {household && <><dt>Household</dt><dd>{household.name} · {roleName}</dd></>}
      </dl>}
    </>}
  </Overlay>
}
