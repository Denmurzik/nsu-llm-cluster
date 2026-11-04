# e2e drivers

CLI drivers for the NSU LLM Cluster: `smoke.py` checks the whole user path, `admin.py` does what the admin UI does.
They only speak the portal HTTP API (and the OpenAI-compatible `/v1`), so they work against a laptop stack,
a lab cluster or production.

Setup (Python 3.12+):

    python -m venv e2e/.venv
    e2e/.venv/Scripts/python -m pip install -r e2e/requirements.txt    # Linux: .venv/bin/python

Exit codes: 0 success, 1 assertion failed (stderr names it), 2 usage or connection error.

## Connection flags (both scripts)

| Flag | Meaning |
|---|---|
| `--base URL` | portal URL, e.g. `https://localhost` |
| `--resolve host:ip` | dial `ip`, keep Host and TLS SNI/verification on `host` (repeatable) |
| `--cacert FILE` | CA bundle for the portal certificate |
| `--login fake` (default) | follow `/auth/login` -> provider `/authorize` -> `/auth/callback`, take the session cookie |
| `--login cookie --cookie V` | use an existing `nsullm_session` cookie value (real NSU ID) |

Who you are is decided by the OIDC provider: the fake provider issues the claims it was started with, so use
an admin e-mail (`ADMIN_EMAILS`) for `admin.py`.

## smoke.py

    python e2e/smoke.py --base https://localhost --cacert ca.pem --model tiny --expect-backends 2 [--timeout 900]

Success requires: login works; `/api/models` shows the model `ready` with at least N backends; a key is created;
streaming chat returns text; a non-streaming reply has `usage.total_tokens > 0`; after deleting the key the
gateway answers 401. Prints `{"ttft_s": ..., "tokens_per_s": ..., "backends": N}`; progress goes to stderr.

## admin.py

    python e2e/admin.py --base ... create-deployment --name tiny-b --repo ggml-org/gemma-3-270m-it-GGUF \
        --file gemma-3-270m-it-Q8_0.gguf --mode replicas --accel cpu --ctx 1024 --parallel 1 [--group-size 2] \
        [--nodes a,b] [--auto-assign] [--engine llamacpp|vllm]
    python e2e/admin.py --base ... patch-deployment --name tiny-b --ctx 4096
        # also --parallel --mode --accel --group-size --nodes --enabled/--no-enabled --auto-assign/--no-auto-assign
    python e2e/admin.py --base ... assign --node laptop --deployment tiny-b      # or --none
    python e2e/admin.py --base ... wait-ready --name tiny-b --backends 1 --timeout 600
    python e2e/admin.py --base ... status --name tiny-b
    python e2e/admin.py --base ... delete-deployment --name tiny-b

`--ctx` is the context per request. `--engine vllm` needs no `--file` and defaults `--accel` to `nvidia`
(for llama.cpp `--file` is required and `--accel` defaults to `any`). Deployments are addressed by name; the script resolves the id.

## Tests

    e2e/.venv/Scripts/python -m pytest -q e2e/tests

Offline (respx-mocked portal and fake provider), no network or cluster needed.
