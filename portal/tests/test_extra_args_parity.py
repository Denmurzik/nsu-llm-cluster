"""The node re-checks the admin's own flags (serve-llama.sh) with the rules of the portal (`validate_extra_args`): the two
must accept and refuse the same inputs. One corpus, both implementations, compared by decision (not by the lists of
names alone). The portal saves what it accepts, the node must start it; the node must refuse what the portal refuses,
because the node does not trust the portal blindly."""

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from app.deployments.llama_args import ExtraArgsInvalid, validate_extra_args

NODE_ROOT = Path(__file__).resolve().parents[2] / "node" / "rootfs" / "opt" / "llm"
BASH = shutil.which("bash")

# each case is the list of tokens the portal would hand to the node (EXTRA_ARGS, one per line)
ACCEPTED = [
    ["--no-warmup"],
    ["--seed", "42"],
    ["--Seed", "5"],  # long flags are compared in lower case
    ["--no_warmup"],  # and with `_` read as `-`
    ["--no-warmup", "--seed", "7", "--flash-attn"],
    ["--override-kv", "tokenizer.ggml.add_bos_token=bool:false"],
    ["--note", "a b"],  # a quoted value with a space stays one token
    ["--note=a b"],
    ["--x", "-"],
    ["--x", "-5"],
    ["--x=-y"],
    ["-C", "10"],  # a short flag is compared as typed: -C is not -c
    ["--chat-template-kwargs", '{"enable_thinking": false}'],
    ["--grp-attn-n", "4", "--grp-attn-w", "512"],
]
REFUSED = [
    ["--port", "80"],
    ["--port=80"],
    ["--PORT=80"],
    ["--Port", "80"],
    ["--api_key=x"],
    ["--api_key", "x"],
    ["--API-KEY", "x"],
    ["--Api_Key=x"],
    ["--hf_repo", "org/x"],
    ["--hf-repo", "org/x"],
    ["--n_gpu_layers", "1"],
    ["-ngl", "0"],
    ["-c", "10"],
    ["--ctx_size", "10"],
    ["--cache_type_k", "q4_0"],
    ["--reasoning", "on"],
    ["--no-mmproj"],
    ["--temp", "1"],
    ["--rpc", "10.0.0.1:1"],
    ["--grammar-file", "g.gbnf"],  # on the list
    ["--my-file", "x"],  # the generic rule: a flag that names a file
    ["--MY-FILE", "x"],
    ["--my_file=x"],
    ["--media-path", "x"],
    ["--models-dir"],
    ["--any-dir", "x"],
    ["--some-url=x"],
    ["--my-key"],
    ["--secret-key-thing"],
    ["--lookup-cache-static", "x"],
    ["--some-cache-dynamic"],
    ["--tools", "all"],
    ["--props"],
    ["--chat-template", ""],  # an empty token: the node cannot start with it
    ["--seed", "1", "--note", ""],
    ["-dr", "ai/gemma"],
    ["--docker-repo", "ai/gemma:latest"],
    ["--docker_repo=ai/gemma"],
    ["-otd", "x=CPU"],
    ["--override-tensor-draft", "x=CPU"],
    ["-cmoed"],
    ["--cpu-moe-draft"],
    ["-ncmoed", "3"],
    ["--n-cpu-moe-draft", "3"],
    ["-fitt", "512"],
    ["--fit-target", "512"],
    ["-fitc", "4096"],
    ["--fit-ctx", "4096"],
    ["-mmdev", "none"],
    ["--mmproj-device", "none"],
    ["--spec-draft-model", "x.gguf"],  # b12033 names the draft flags --spec-draft-*; the old names are aliases
    ["--spec-draft-hf", "org/x"],
    ["--spec-draft-ngl", "9"],
    ["--spec-draft-device", "CPU"],
    ["--spec-draft-type-k", "q4_0"],
    ["--temperature", "1"],
    ["--mmproj-auto"],
    ["--no-mmproj-auto"],
    ["--mmproj-offload"],
    ["--no-mmproj-offload"],
    ["--models-max"],
    ["--models-autoload"],
    ["--no-models-autoload"],
    ["--models-memory-margin"],
    ["--reuse-port"],
    ["--no-host"],
    ["-fit", "off"],
    ["-dev", "CPU"],
    ["-ngld", "99"],
    ["-mm", "x"],
    ["--seed", "/etc/passwd"],
    ["--seed=/etc/passwd"],
    ["--seed", "~/x"],
    ["--seed=~"],
    ["--seed", ".."],
    ["--seed", "."],
    ["--seed=.."],
    ["--seed", "C:\\x"],  # a backslash
    ["--seed", "a\\b"],
    ["42"],  # the flags start with «-»
    ["42", "--seed", "1"],
    ["--note", "привет"],
    ["--note", "tab\there"],
    ["--x" + str(i) for i in range(41)],  # more than 40 tokens
]


def portal_accepts(tokens: list[str]) -> bool:
    try:
        validate_extra_args(shlex.join(tokens))
        return True
    except ExtraArgsInvalid:
        return False


def node_accepts(tokens: list[str]) -> bool:
    env = {
        "PATH": "/usr/bin:/bin",
        "MODEL_REPO": "org/repo",
        "MODEL_FILE": "m.gguf",
        "ACCEL": "cpu",
        "LLAMA_CACHE": "/var/lib/llm-models",
        "FORBIDDEN_FLAGS_FILE": str(NODE_ROOT / "forbidden-flags"),
        "EXTRA_ARGS": "\n".join(tokens),
    }
    done = subprocess.run(
        [BASH, str(NODE_ROOT / "bin" / "serve-llama.sh"), "--print-cmd"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    if done.returncode == 0:
        # accepted flags reach llama-server as separate arguments, one per token, last on the command line
        line = next(x for x in done.stdout.splitlines() if x.startswith("llama-server "))
        assert line.endswith(" ".join(tokens)), line
    return done.returncode == 0


def test_the_corpus_is_what_it_says():
    assert all(portal_accepts(t) for t in ACCEPTED), [t for t in ACCEPTED if not portal_accepts(t)]
    assert not any(portal_accepts(t) for t in REFUSED), [t for t in REFUSED if portal_accepts(t)]


@pytest.mark.skipif(BASH is None, reason="bash is needed to run serve-llama.sh")
@pytest.mark.parametrize("tokens", ACCEPTED, ids=lambda t: " ".join(t)[:40])
def test_the_node_accepts_what_the_portal_accepts(tokens):
    assert node_accepts(tokens)


@pytest.mark.skipif(BASH is None, reason="bash is needed to run serve-llama.sh")
@pytest.mark.parametrize("tokens", REFUSED, ids=lambda t: " ".join(t)[:40])
def test_the_node_refuses_what_the_portal_refuses(tokens):
    assert not node_accepts(tokens)
