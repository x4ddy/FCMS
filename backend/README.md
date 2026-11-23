# Backend

The backend is a Python package. Run modules from the repository root using their complete package names, for example `backend.api_gateway.app:app`.

## Request flow

```text
Browser -> API Gateway -> Auth / Refresh / FamilyCart service
                         -> User and service-owned databases
```

`api_gateway/app.py` is the public edge. It applies rate limits, origin checks, access-token validation, active-session validation, security headers, and then proxies requests to the responsible service.

`services/` contains business capabilities. Services may import stable helpers from `shared/`, but should not import from the Gateway. New public endpoints must use `shared.api.API_PREFIX` so they remain under the current `/api/v1` contract.

`tests/` exercises service behavior and Gateway security using temporary databases.
