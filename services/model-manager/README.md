# services/model-manager

The only component that starts inference containers (CLAUDE.md §7, §11).

| Module | Owns |
|---|---|
| `registry.py` | `Models/models.yaml`: models, role assignments, voters; validation with sentences; `models.example.yaml` is rendered from `EXAMPLE_REGISTRY`, and `config/models.quickstart.yaml` / `config/models.prod.yaml` from `PROFILE_REGISTRIES` (install.sh writes the profile's one as `Models/models.yaml` when none exists) |
| `fit.py` | the fit sentence (`slas model fit`) and the quantisation rule per GPU generation |
| `runtime.py` | the `ContainerRuntime` protocol, the vLLM container spec with the mandatory flags, and `FakeRuntime` |
| `swap.py` | blue/green swap: start the candidate alongside, smoke-test, switch the route, drain; rollback within 24 hours |
| `reconcile.py` | desired (registry) versus running containers → start/stop/keep actions |
| `driver.py` | `ContainerApiRuntime`: the `ContainerRuntime` over the runtime socket (`slas_container`), `ContainerSpec` → `CreateSpec` per docs/api-contract-round-2.md §3; `HttpProber` (`GET /health`); `PatientRuntime` waits for a model to load during a swap |
| `placement.py` | greedy GPU placement by `vram_gib` against `SLAS_GPU_VRAM_GIB` per GPU: roles before voters, biggest first, several GPUs in tensor parallel when one is not enough; what does not fit gets a sentence and never starts |
| `controller.py` | the reconcile applier: registry → placement → containers → health → `PUT /v1/instances` on the gateway; per-instance state and sentence; swaps and rollbacks in a thread; a loop every `SLAS_RECONCILE_INTERVAL_S` |
| `smoke.py` | `HttpSmokeTester`: one tiny chat completion against a swap candidate |
| `service/` | `settings.py` (environment), `routes.py` (contract §3), `app.py` (`create_app`, every collaborator injectable) |
| `cli.py` | `slas-model-manager serve` |

Environment (defaults): `SLAS_RUNTIME_SOCKET` (`/run/podman/podman.sock`), `SLAS_MODELS_FILE`
(`/data/Models/models.yaml`), `SLAS_GATEWAY_URL` (`http://llm-gateway:8000`), `SLAS_GPU_IDS`
(`0,1,2,3`), `SLAS_GPU_VRAM_GIB` (`180`), `SLAS_HOST_MODELS_DIR` (`${SLAS_DATA_ROOT}/Models`),
`SLAS_INFERENCE_NETWORK` (`slas_slas-inference`), `SLAS_VLLM_IMAGE` (required; empty is a
sentence on the Models page, not a start), `SLAS_VLLM_SHM` (`16g`), `SLAS_RECONCILE_INTERVAL_S`
(`30`), `SLAS_MODEL_START_TIMEOUT_S` (`900`, how long a swap waits for the candidate),
`SLAS_BIND` (`0.0.0.0:8000`).
