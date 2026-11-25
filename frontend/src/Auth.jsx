import React, { useEffect, useRef, useState } from 'react'
import { googleConfiguration, signInWithGoogle } from './api.js'
import { Icon, Overlay } from './ui.jsx'

export function DateOfBirthForm({ busy, onSubmit }) {
  const [birth, setBirth] = useState('')
  return <form className="birth-date-form" onSubmit={(event) => {
    event.preventDefault()
    onSubmit(birth)
  }}>
    <label>Date of birth<input type="date" autoComplete="bday" required value={birth}
      max={new Date().toISOString().slice(0, 10)} disabled={busy} onChange={(event) => setBirth(event.target.value)} /></label>
    <button disabled={busy}>Save and continue</button>
  </form>
}

export function GoogleAuthModal({ busy, onClose, onAuthenticated }) {
  const buttonRef = useRef(null)
  const completeSignIn = useRef(onAuthenticated)
  completeSignIn.current = onAuthenticated
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [pendingCredential, setPendingCredential] = useState(null)
  const inFlight = useRef(false)
  const disabled = busy || submitting

  async function signIn(credential, birth) {
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setError('')
    try { await completeSignIn.current(await signInWithGoogle(credential, birth)) }
    catch (issue) {
      if (issue.code === 'DATE_OF_BIRTH_REQUIRED') setPendingCredential(credential)
      else {
        if (issue.status === 401 && birth) setPendingCredential(null)
        setError(issue.message)
      }
    } finally {
      setSubmitting(false)
      inFlight.current = false
    }
  }

  useEffect(() => {
    if (pendingCredential) return
    let cancelled = false
    const render = (clientId) => {
      if (cancelled || !buttonRef.current || !window.google?.accounts?.id) return
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: ({ credential }) => { if (!cancelled) signIn(credential) },
      })
      window.google.accounts.id.renderButton(buttonRef.current, {
        type: 'standard', theme: 'filled_black', size: 'large', shape: 'pill',
        text: 'continue_with', width: 280,
      })
    }
    googleConfiguration().then(({ client_id: clientId }) => {
      if (cancelled) return
      const existing = document.querySelector('script[data-familycart-google]')
      if (existing && window.google?.accounts?.id) render(clientId)
      else {
        const script = existing ?? document.createElement('script')
        script.src = 'https://accounts.google.com/gsi/client'
        script.async = true; script.defer = true
        script.dataset.familycartGoogle = 'true'
        script.onload = () => render(clientId)
        script.onerror = () => setError('Google sign-in could not be loaded.')
        if (!existing) document.head.appendChild(script)
      }
    }).catch((caught) => setError(caught.message))
    return () => { cancelled = true }
  }, [pendingCredential])

  return <Overlay kind="dialog auth" label={pendingCredential ? 'Finish account setup' : 'Sign in'} onClose={() => { if (!disabled) onClose() }}>
    {(leave) => <>
      <button className="icon-button overlay-close" aria-label="Close login" onClick={leave} disabled={disabled}><Icon name="close" /></button>
      <div className="auth-logo" aria-hidden="true"><Icon name="cart" size={30} /></div>
      <h1>{pendingCredential ? 'Finish account setup' : 'Welcome to FamilyCart'}</h1>
      {pendingCredential ? <>
        <p className="muted">Enter your date of birth to finish setting up your account.</p>
        <DateOfBirthForm busy={disabled} onSubmit={(birth) => signIn(pendingCredential, birth)} />
        <button className="text-button" disabled={disabled}
          onClick={() => { setPendingCredential(null); setError('') }}>Use a different Google account</button>
      </> : <>
        <p className="muted">Log in or sign up with your Gmail account</p>
        <div className={disabled ? 'google-button disabled' : 'google-button'} ref={buttonRef} />
      </>}
      {error && <p className="notice error" role="alert">{error}</p>}
      <small className="muted">By continuing, you agree to our Terms of service and Privacy policy.</small>
    </>}
  </Overlay>
}
