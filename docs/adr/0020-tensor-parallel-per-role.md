# ADR-0020: Tensor parallel per role in the model registry

Status: accepted
Date: 2026-10-01

Accepted on the project owner's instruction of 2026-10-01, on an HGX B300 NVL8: "let more
GPU work I see the 8 GPU only one or two".

## Context
The model manager starts one vLLM instance per role and one per voter (§7, decision 14) and
gives each the fewest GPUs its `vram_gib` needs. On the quickstart registry that is seven
instances of one GPU each on an eight-GPU host. The agent works one step at a time, so the
instance answering (the coder, writing whole files) is busy while the others wait, and one
GPU sits idle. Placement had a TODO for a tensor-parallel field "with that ADR". Each vLLM
instance takes vLLM's default 90% of every GPU it runs on, so two instances cannot share a
GPU; placement already prefers an empty GPU, so they only meet when GPUs run out.

## Decision
- `Models/models.yaml` gains an optional `tensor_parallel: {<role>: n}` with n in 1, 2, 4,
  8. It is per role, not per model: one model serves several roles and a voter (the
  quickstart Qwen is coder, planner and a voter), and only the role that does the long work
  is worth spreading. A role's instance gets max(n, the GPUs its memory needs) GPUs and
  vLLM runs it with `--tensor-parallel-size` of that count.
- An instance already running on another count is stopped and started again on the new one
  by the next reconcile (INV-9: an edit of the file or the Models page, no restart). A
  role that is no longer served drops its entry.
- A role's instance that crash-loops on the tensor-parallel path with CUDA's "no kernel
  image is available for execution on the device" (on the HGX B300 with the pinned vLLM
  build: `cooperative_topk` during CUDA graph capture on two GPUs, while one GPU worked) is
  started again on the GPUs its memory needs, once, like the other crash remedies of §7;
  its Models page row says it runs on fewer GPUs than the registry asks and why. A model
  manager restart forgets that and tries the registry's count once more.
- The file is written with the key only when it is set, so existing registries are
  unchanged. The shipped registries do not set it: how many GPUs are free depends on the
  host. On an eight-GPU quickstart host, `tensor_parallel: {coder: 2}` uses the idle GPU.
- TODO(SLAS-MODELS): n must divide the model's attention heads; 1, 2, 4 and 8 divide those
  of the shipped models. A new model with another head count needs that check.

## Consequences
Easier: a host with idle GPUs puts them behind the coder, which writes faster. Harder: more
GPUs for one role means fewer for the others; on eight GPUs the coder gets two unless an
instance is removed or a voter shares a role's instance (decision 14, its own ADR). A count
the free GPUs cannot hold leaves the instance unplaced with the usual sentence.

## Invariants touched
INV-9 (the change applies by editing the registry, no config edit elsewhere and no
restart). INV-8 is unaffected: no new image or dependency. No invariant is relaxed.
