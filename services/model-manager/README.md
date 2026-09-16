# services/model-manager

The only component that starts inference containers (CLAUDE.md §7, §11).

| Module | Owns |
|---|---|
| `registry.py` | `Models/models.yaml`: models, role assignments, voters; validation with sentences; `models.example.yaml` is rendered from `EXAMPLE_REGISTRY`, and `config/models.quickstart.yaml` / `config/models.prod.yaml` from `PROFILE_REGISTRIES` (install.sh writes the profile's one as `Models/models.yaml` when none exists) |
| `fit.py` | the fit sentence (`slas model fit`) and the quantisation rule per GPU generation |
| `runtime.py` | the `ContainerRuntime` protocol, the vLLM container spec with the mandatory flags, and `FakeRuntime` |
| `swap.py` | blue/green swap: start the candidate alongside, smoke-test, switch the route, drain; rollback within 24 hours |
| `reconcile.py` | desired (registry) versus running containers → start/stop/keep actions |

The Podman driver and the HTTP surface arrive with their approved dependencies.
