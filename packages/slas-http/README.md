# slas-http

What every service's HTTP surface has in common (ADR-0015, `docs/api-contract-round-2.md`):

| Module | What it gives a service |
|---|---|
| `slas_http.app` | `create_service_app(name, …)`: a FastAPI app with no docs page (INV-1), `GET /health` (runs the service's checks, 503 names the failing one), `GET /metrics` (the shared registry), a trace middleware that binds the caller's trace id, echoes `X-Slas-Trace-Id` and turns an unexpected exception into a three-part 500. |
| `slas_http.errors` | `ServiceError(status, ThreePartMessage)` and the exception handlers that make every non-2xx body exactly `{what_happened, likely_cause, what_to_do, trace_id}`. |
| `slas_http.identity` | The acting person travels between services as headers, never as a token: `X-Slas-User`, `X-Slas-Display-Name`, `X-Slas-Capabilities`. `require(identity, capability)` runs authz where the action executes (CLAUDE.md §11). |
| `slas_http.client` | `ServiceClient(base_url)`: JSON in and out, the trace headers and the identity on every request, a three-part body raised as `ServiceError`, a connection failure as a three-part 503 naming the service. |
| `slas_http.serve` | `run(app, bind)`: uvicorn on the container's own interface. |

The api keeps its own richer middleware (sessions, `X-Requested-With`); the other services
build on this package so their surfaces behave alike.
