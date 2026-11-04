"""Checks of the llama-server settings an admin types in: «свои флаги» and the sampling defaults.

The portal builds the whole llama-server command line itself; the admin's own flags only add to it. So everything the
portal (or the node) sets, and everything that reads a file or opens a port, is refused here. The node re-checks the
flags with `validate_extra_args` before it starts the server (task 2-22b): the node does not trust the portal blindly.
Errors are `ExtraArgsInvalid` / `SamplingInvalid` (both `ValueError`) with the Russian message for the admin.
"""

import math
import shlex
from typing import Any

MAX_TOKENS = 40
MAX_LENGTH = 2000  # model_deployments.extra_args is String(2000)

# Flag names the portal sets itself or that must not be changed, in normalised form (`normalize_flag`). Matched
# exactly: `--x`, `--x=value`, and for long flags also `--x_y` for `--x-y` (llama.cpp reads `_` as `-`).
FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        # the brief of 2-22a
        "--host", "--port", "--api-key", "--api-key-file", "--rpc",
        "-m", "--model", "--model-url", "-hf", "-hfr", "--hf-repo", "--hf-file", "-hff", "--hf-token", "-hft",
        "--hf-repo-draft", "--hf-file-draft", "-md", "--model-draft",
        "--mmproj", "--mmproj-url", "--hf-file-mmproj", "--no-mmproj",
        "-c", "--ctx-size", "-np", "--parallel", "-ngl", "--n-gpu-layers", "--gpu-layers", "-ts", "--tensor-split",
        "--fit", "--alias", "-a", "--ssl-key-file", "--ssl-cert-file", "--log-file", "--slot-save-path",
        "--chat-template-file", "--lora", "--lora-scaled", "--control-vector", "--reasoning",
        "--cache-type-k", "-ctk", "--cache-type-v", "-ctv",
        "--temp", "--top-p", "--top-k", "--min-p", "--repeat-penalty",
        "-t", "--threads", "--path", "--static-path", "--webui", "--no-webui",
        # devices and split placement (the portal sets them from the nodes it groups)
        "-dev", "--device", "-sm", "--split-mode", "-mg", "--main-gpu", "-ot", "--override-tensor",
        "--cpu-moe", "--n-cpu-moe", "-ncmoe", "-cmoe",
        "-otd", "--override-tensor-draft", "--spec-draft-override-tensor",
        "-cmoed", "--cpu-moe-draft", "--spec-draft-cpu-moe",
        "-ncmoed", "--n-cpu-moe-draft", "--spec-draft-n-cpu-moe", "--spec-draft-ncmoe",
        "-fit", "-fitt", "--fit-target", "-fitc", "--fit-ctx", "-mmdev", "--mmproj-device",
        # the names b12033 gives the draft flags (the old names above are aliases of these), and other aliases
        "--spec-draft-hf", "--spec-draft-model", "--spec-draft-device", "--spec-draft-ngl",
        "--spec-draft-type-k", "--spec-draft-type-v", "--temperature", "--ui", "-rea", "-cl",
        # the portal owns the projector placement, the router mode of the server (the API the gateway expects) and the network
        "--mmproj-auto", "--no-mmproj-auto", "--mmproj-offload", "--no-mmproj-offload", "--models-max",
        "--models-autoload", "--no-models-autoload", "--models-memory-margin", "--reuse-port", "--no-host",
        # a model from Docker Hub: sets the model path like -hf does
        "-dr", "--docker-repo",
        "-devd", "--device-draft", "-ngld", "--gpu-layers-draft", "--n-gpu-layers-draft", "-cd", "--ctx-size-draft",
        "-ctkd", "--cache-type-k-draft", "-ctvd", "--cache-type-v-draft",
        # short synonyms of the model flags above, and the vocoder model
        "-mm", "-mmu", "-mu", "-hfd", "-hfrd", "-hffd", "-hfv", "-hfrv", "-hffv", "--hf-repo-v", "--hf-file-v", "-mv",
        "--model-vocoder",
        # read or write files
        "--grammar-file", "-jf", "--json-schema-file", "-f", "--file", "--prompt-cache", "--lookup-cache-static",
        "--lookup-cache-dynamic", "-lcs", "-lcd", "--media-path", "--tools", "--models-dir", "--models-preset",
        "--webui-config-file",
        # the API the gateway expects, and what the portal owns
        "--api-prefix", "--props", "--metrics", "--slots", "--no-slots", "--embedding", "--embeddings", "--reranking",
        "--rerank", "--pooling", "--reasoning-format", "--reasoning-budget",
        # leave at once
        "-h", "--help", "--usage", "--version", "--list-devices", "--completion-bash", "--offline", "--cache-list",
    }
)
# any long flag that, normalised, ends with one of these or contains one of the others, however it is spelled
_PORTAL_SUFFIXES = ("-file", "-path", "-dir", "-url")
_PORTAL_PARTS = ("cache-static", "cache-dynamic", "-key")
PORTAL_OWNED = "Флаги с файлами, путями и ключами задаются порталом"
EMPTY_VALUE = "Пустое значение флага: уберите пустые кавычки"

# sampling defaults: key -> (low, high, integer)
SAMPLING_LIMITS: dict[str, tuple[float, float, bool]] = {
    "temperature": (0.0, 2.0, False),
    "top_p": (0.0, 1.0, False),
    "top_k": (0, 1000, True),
    "min_p": (0.0, 1.0, False),
    "repeat_penalty": (0.5, 2.0, False),
}


class ExtraArgsInvalid(ValueError):
    """The own flags break a rule; the message is Russian."""


class SamplingInvalid(ValueError):
    """The sampling defaults break a rule; the message is Russian."""


def _printable(token: str) -> bool:
    return all(" " <= c <= "~" for c in token)  # printable ASCII, no newline, no control characters


def _is_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.startswith("~") or value in (".", "..")


def normalize_flag(name: str) -> str:
    """The name llama.cpp looks a flag up by: a long option reads `_` as `-` (`--api_key` is `--api-key`) and is
    matched in lower case here; a short one (`-c`) stays as it is. Compare this, never the name as typed."""
    return name.replace("_", "-").lower() if name.startswith("--") else name


def validate_extra_args(text: str | None) -> list[str]:
    """The own flags split into the tokens for the command line; raises ExtraArgsInvalid for the first offence."""
    if text is None or not text.strip():
        return []
    if len(text) > MAX_LENGTH:
        raise ExtraArgsInvalid(f"Свои флаги не длиннее {MAX_LENGTH} символов")
    if "\x5c" in text:  # shlex would eat the backslash of `C:<bs>x` and the path check below would not see a path
        raise ExtraArgsInvalid("Пути к файлам в своих флагах запрещены: обратная косая черта не допускается")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as e:
        raise ExtraArgsInvalid("В своих флагах не закрыта кавычка") from e
    if len(tokens) > MAX_TOKENS:
        raise ExtraArgsInvalid(f"В своих флагах не больше {MAX_TOKENS} частей")
    for i, token in enumerate(tokens):
        if token == "":  # `--chat-template ""`: the node refuses an empty token, so the pods would crash-loop
            raise ExtraArgsInvalid(EMPTY_VALUE)
        if not _printable(token):
            raise ExtraArgsInvalid(f"В своих флагах допустимы только печатные символы ASCII: {token!r}")
        if token.startswith("-") and len(token) > 1:
            name, eq, value = token.partition("=")
            flag = normalize_flag(name)
            if flag in FORBIDDEN_FLAGS:
                raise ExtraArgsInvalid(f"Флаг {name} задаётся порталом")
            if flag.startswith("--") and (flag.endswith(_PORTAL_SUFFIXES) or any(p in flag for p in _PORTAL_PARTS)):
                raise ExtraArgsInvalid(f"{PORTAL_OWNED}: {name}")
            if eq and _is_path(value):
                raise ExtraArgsInvalid(f"Пути к файлам в своих флагах запрещены: {token}")
        else:
            if i == 0:
                raise ExtraArgsInvalid(f"Свои флаги начинаются с «-»: {token}")
            if _is_path(token):
                raise ExtraArgsInvalid(f"Пути к файлам в своих флагах запрещены: {token}")
    return tokens


def validate_sampling(value: Any) -> dict[str, float | int] | None:
    """The sampling defaults normalised (floats, `top_k` int); an empty object is None. Raises SamplingInvalid."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SamplingInvalid("Сэмплинг: объект с параметрами")
    unknown = sorted(str(k) for k in value if k not in SAMPLING_LIMITS)
    if unknown:
        raise SamplingInvalid(
            f"Неизвестный параметр сэмплинга: {', '.join(unknown)}. Допустимы: {', '.join(SAMPLING_LIMITS)}"
        )
    result: dict[str, float | int] = {}
    for key, (low, high, integer) in SAMPLING_LIMITS.items():
        if key not in value:
            continue
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, int | float) or math.isnan(v) or math.isinf(v):
            raise SamplingInvalid(f"{key}: нужно число")
        if integer and v != int(v):
            raise SamplingInvalid(f"{key}: нужно целое число")
        if not low <= v <= high:
            raise SamplingInvalid(f"{key}: от {low:g} до {high:g}")
        result[key] = int(v) if integer else float(v)
    return result or None
