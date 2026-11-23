# FamilyCart — Stage 3

## Goal

A new user can register, join or create a household, and shop from a small catalogue. A newly onboarded child can join the household, starts with no access to existing carts, and can replay Stage 2’s grant → edit → revoke demo. The catalogue also demonstrates the difference between an adult and an under-18 account.

## Starting point

Keep the two FastAPI apps, React, and the existing FamilyCart SQLite data. Add a separate SQLite database owned by Auth, and add a small Product app in 3E. Keep the existing seeded logins for regression checks. Run the new demo with freshly registered accounts.

| Owner | Data and rules |
|---|---|
| Auth | Accounts, passwords, date of birth, verified email, guardian links, setup and reset codes, and session validity. |
| FamilyCart | Households, primary holders, invitations, memberships, cart grants, and contributions. FamilyCart decides whether the caller can act on a cart. |
| Product | Seeded SKUs, product names, and each SKU’s minimum age. Product applies the age rule when returning catalogue data. |

Keep service databases private. Use a local test inbox for verification, invitations, and recovery links. A message arriving is a delivery event. Verification or acceptance happens when the user completes the corresponding app flow.

## 3A · Standalone signup

1. Ask for date of birth and email. The server calculates age from date of birth. A user aged 13+ can proceed with email verification. A younger user goes through dependent signup in 3C.
2. Email a time-limited signup link. The link opens a React page where the user enters their name and chooses a password. A POST consumes the token and creates the verified account together.
3. Keep `POST /auth/login` and its `identifier` plus `password` request. Look up users in Auth’s database and preserve the existing response shape. Derive the adult eligibility claim from stored date of birth at sign-in. Email verification confirms control of the email, and date of birth is self-reported in this demo.

| Proposed route | Behaviour |
|---|---|
| `POST /auth/signup` | Accepts email and date of birth. For a valid standalone request, returns a generic `202` and queues the verification link when eligible. |
| `POST /auth/signup/complete` | Accepts the token, name, and password. Returns `201` after successful verification and account creation. |

**Done when** a newly registered adult and a newly registered 13–17-year-old can both sign in after verification. A pending signup cannot sign in. An expired or already used link fails. Submitting an under-13 date of birth directly to the standalone endpoint cannot create an account.

## 3B · Households and invitations

1. An adult creates a household. Create the household and its initial `primary` membership in one write. An adult invited into someone else’s household joins as a regular `member`.
2. A primary invites someone by email. The recipient registers if needed and accepts while signed in to the account with the matching verified email. FamilyCart gets the caller's verified email from Auth through a private identity lookup and uses the same email comparison policy as Auth. Consume the invite and add the membership together in FamilyCart. An invitation starts with zero cart grants.
3. Add a household picker in React. Replace the fixed `/households/demo-household` in `frontend/src/api.js` with the selected household ID. Keep the existing cart routes under that household.

| Proposed route | Behaviour |
|---|---|
| `GET /households` | Lists the caller’s active households and their role in each. |
| `POST /households` | Creates a named household and makes the adult caller its primary. |
| `POST /households/{household_id}/invitations` | Primary only. Creates an expiring invitation for an email address. |
| `POST /households/invitations/accept` | Accepts the token for the signed-in account with the matching verified email. |

**Done when** an adult creates a household, a second verified account accepts its own invitation, and the household appears in both account views. A different account cannot accept the invite. An existing Stage 2 cart stays visible to the same users as before.

## 3C · Dependent signup

1. The under-13 signup form asks for the guardian’s email. Only an existing, verified adult account aged 18+ can receive and act on the request. Return a generic submission response so the child form does not reveal whether that email has an account.
2. The signed-in guardian opens the request in their dashboard, declines it or approves it, and on approval chooses a unique child username and a household. For this increment, the guardian can select a household where they are a primary, or create one first. Household authority remains specific to each household.
3. Approval gives the guardian a one-time setup code. The guardian passes it to the child, who chooses their own password. The child gets a separate user ID and signs in with the username and password.
4. During setup, Auth asks FamilyCart over an authenticated private API to create the child’s regular membership for the approved household. Use the same child ID on retries, and have FamilyCart recheck that the guardian can still add a member. Keep sign-in disabled until setup and membership provisioning succeed. A timeout or repeat request should leave at most one account and one membership. Provision zero cart grants.

| Proposed route | Behaviour |
|---|---|
| `POST /auth/dependent-requests` | Accepts the child’s name, date of birth, and guardian email. Returns a generic `202`. |
| `GET /auth/dependent-requests` | Lists the signed-in guardian’s pending requests. |
| `POST /auth/dependent-requests/{request_id}/approve` | Accepts a child username and household ID. Authorizes the matching guardian and returns the setup code. |
| `POST /auth/dependent-requests/{request_id}/decline` | Allows the matching guardian to close the request. |
| `POST /auth/dependents/setup` | Accepts the setup code and the child’s new password. Activates the account after provisioning membership. |

Use case-insensitive usernames and reserve `@` for email login. Keep guardian approval and recovery in Auth. For cart grants to real users, use Auth’s server-side age eligibility for the target instead of the hardcoded `shared/demo_members.py` lookup. Do not expose birth dates in member or cart responses.

**Done when** a verified adult approves a child, the child sets their own password, and the child’s first cart list is empty even when the selected household already has carts. An unverified account, a teen, and an unrelated adult cannot approve the request. Replay grant → edit → revoke with the new child, and confirm the next edit is denied using their existing JWT.

## 3D · Password recovery

- A standalone user requests a reset by email. Return the same response whether an eligible account exists or not. Route a child who needs help to their guardian.
- The linked guardian starts a child reset from their dashboard. The guardian issues a one-time code, and the child picks the new password. A primary role by itself grants no recovery authority.
- Apply these rules when each flow is introduced. Store signup, setup, invitation, and reset secrets as hashes. Bind each to its intended purpose and account, set an expiry, and consume it once. Rate-limit requests and code attempts. Use a POST to finish a link flow so opening a link does not consume it. Store passwords using Argon2id. See [OWASP’s password recovery guidance](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html) and [email verification guidance](https://cheatsheetseries.owasp.org/cheatsheets/Email_Validation_and_Verification_Cheat_Sheet.html).
- Make reset revoke existing JWTs across services. Add an Auth-owned session version to users and newly issued tokens. Change it when a password reset succeeds. Each protected FamilyCart and Product request checks the version through a private Auth endpoint after JWT validation. An expired or superseded token returns `401`. If Auth is unavailable, reject the protected request with `503`. After introducing the version claim, require a fresh sign-in for older prototype tokens.

| Proposed route | Behaviour |
|---|---|
| `POST /auth/password/forgot` | Accepts email and responds with a generic `202`. |
| `POST /auth/dependents/{child_id}/password/reset-code` | The linked guardian requests a reset code for the child. |
| `POST /auth/password/reset` | Accepts the reset token or code and a new password. Consumes the secret, updates the password, and invalidates old sessions. |

**Done when** the previous password and tokens from before the reset are rejected, the new password works, and an unrelated adult cannot start a child’s reset.

## 3E · Mock catalogue and age restrictions

Run a small Product FastAPI app with a static fixture catalogue. Seed staples like milk, eggs, bread, rice, apples, and cereal, plus one fictional tobacco SKU with `min_age: 18`. Keep the existing `milk` SKU to preserve Stage 2 demo data. Product provides authenticated `GET /products` with optional search and `GET /products/{sku}`. React uses the returned catalogue for the item picker and replaces `setMilk()` with a quantity operation that accepts a SKU.

| Scenario | Expected result |
|---|---|
| Minor browses or searches | Restricted products never appear in results. A direct lookup of a restricted SKU returns `404`. |
| Minor tries to add a restricted SKU | FamilyCart rejects the request. Product treats the SKU as unavailable to this caller, and FamilyCart makes no cart change. |
| Adult adds a restricted SKU to a regular cart | FamilyCart returns `403`. Regular household carts contain only products visible to minors. |
| Adult adds a restricted SKU to an adults-only cart they can edit | The write succeeds. Minors remain unable to view or edit the cart. |
| Product is unavailable during a positive quantity write | FamilyCart returns `503` and leaves the cart unchanged. Removing an existing contribution can use the existing cart permission path. |

For positive quantities, FamilyCart queries Product by SKU using the caller’s authenticated identity and uses the returned metadata to enforce the cart rule. Run this lookup before opening a SQLite write transaction. Recheck current membership, grants, and the cart’s adults-only flag in the transaction before updating. Allow an adult primary to create an adults-only cart in their household. Keep that setting fixed after creation for this increment.

**Done when** a new adult, a standalone teen, and a dependent child see the expected catalogue. Show the adult adding the restricted item to an adults-only cart, then verify a minor’s direct catalogue lookup and attempted cart write fail. Show that all eligible cart editors can still add ordinary groceries.

## Build and demo order

Build **3A first** and prove signup → email verification → login through the API before building household screens. Then build 3B and 3C, 3D recovery, and finally 3E. After each slice, rerun `python -m unittest discover -s tests -v`. Use an isolated test database and the local inbox for repeatable runs.

For the full demo, register an adult, create a family, onboard a child, and replay Stage 2’s grant → edit → revoke flow. End by comparing the adult and minor catalogues. Shared lists, saved addresses, personal history and preferences, inventory, store selection, and checkout come later.
