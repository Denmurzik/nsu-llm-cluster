import pytest

from app.deployments import llama_args
from app.deployments.llama_args import (
    FORBIDDEN_FLAGS,
    ExtraArgsInvalid,
    SamplingInvalid,
    normalize_flag,
    validate_extra_args,
    validate_sampling,
)

# the families of the brief and of review round 1: every one must be refused, as `--x value` and as `--x=value`
FAMILIES = {
    "network": ["--host", "--port", "--api-key", "--api-key-file", "--rpc"],
    "model": ["-m", "--model", "--model-url", "-hf", "-hfr", "--hf-repo", "--hf-file", "-hff", "--hf-token", "-hft"],
    "draft": ["--hf-repo-draft", "--hf-file-draft", "-md", "--model-draft"],
    "images": ["--mmproj", "--mmproj-url", "--hf-file-mmproj", "--no-mmproj"],
    "resources": ["-c", "--ctx-size", "-np", "--parallel", "-ngl", "--n-gpu-layers", "--gpu-layers", "-ts",
                  "--tensor-split", "--fit", "-t", "--threads"],
    "identity": ["--alias", "-a"],
    "files": ["--ssl-key-file", "--ssl-cert-file", "--log-file", "--slot-save-path", "--chat-template-file", "--lora",
              "--lora-scaled", "--control-vector", "--path", "--static-path"],
    "portal settings": ["--reasoning", "--cache-type-k", "-ctk", "--cache-type-v", "-ctv", "--temp", "--top-p",
                        "--top-k", "--min-p", "--repeat-penalty"],
    "web": ["--webui", "--no-webui"],
    "devices and split": ["-dev", "--device", "-sm", "--split-mode", "-mg", "--main-gpu", "-ot", "--override-tensor",
                          "--cpu-moe", "--n-cpu-moe", "-ncmoe", "-cmoe", "-devd", "--device-draft", "-ngld",
                          "--gpu-layers-draft", "--n-gpu-layers-draft", "-cd", "--ctx-size-draft", "-ctkd",
                          "--cache-type-k-draft", "-ctvd", "--cache-type-v-draft"],
    "short synonyms": ["-mm", "-mmu", "-mu", "-hfd", "-hfrd", "-hffd", "-hfv", "-hfrv", "-hffv", "--hf-repo-v",
                       "--hf-file-v", "-mv", "--model-vocoder"],
    "read or write files": ["--grammar-file", "-jf", "--json-schema-file", "-f", "--file", "--prompt-cache",
                            "--lookup-cache-static", "--lookup-cache-dynamic", "-lcs", "-lcd", "--media-path",
                            "--tools", "--models-dir", "--models-preset", "--webui-config-file"],
    "api contract": ["--api-prefix", "--props", "--metrics", "--slots", "--no-slots", "--embedding", "--embeddings",
                     "--reranking", "--rerank", "--pooling", "--reasoning-format", "--reasoning-budget"],
    "b12033 names and the flags found by the final review": [
        "-dr", "--docker-repo", "-otd", "--override-tensor-draft", "--spec-draft-override-tensor", "-cmoed",
        "--cpu-moe-draft", "--spec-draft-cpu-moe", "-ncmoed", "--n-cpu-moe-draft", "--spec-draft-n-cpu-moe",
        "--spec-draft-ncmoe", "-fit", "-fitt", "--fit-target", "-fitc", "--fit-ctx", "-mmdev", "--mmproj-device",
        "--spec-draft-hf", "--spec-draft-model", "--spec-draft-device", "--spec-draft-ngl", "--spec-draft-type-k",
        "--spec-draft-type-v", "--temperature", "--ui", "-rea", "-cl",
    ],
    "projector, router mode and network (ruling of the lead)": [
        "--mmproj-auto", "--no-mmproj-auto", "--mmproj-offload", "--no-mmproj-offload", "--models-max",
        "--models-autoload", "--no-models-autoload", "--models-memory-margin", "--reuse-port", "--no-host",
    ],
    "leave at once": ["-h", "--help", "--usage", "--version", "--list-devices", "--completion-bash", "--offline",
                      "--cache-list"],
}
ALL_FLAGS = [f for flags in FAMILIES.values() for f in flags]
LONG_FLAGS = [f for f in ALL_FLAGS if f.startswith("--")]


def test_every_forbidden_flag_is_listed_once():
    assert set(ALL_FLAGS) == set(FORBIDDEN_FLAGS)
    assert len(ALL_FLAGS) == len(set(ALL_FLAGS))


@pytest.mark.parametrize("flag", ALL_FLAGS)
def test_forbidden_flag_is_refused_alone_with_a_value_and_with_equals(flag):
    for text in (flag, f"{flag} 1", f"--ok {flag} 1", f"{flag}=1"):
        with pytest.raises(ExtraArgsInvalid) as err:
            validate_extra_args(text)
        assert err.value.args[0] == f"Флаг {flag} задаётся порталом", text


def test_normalize_flag():
    assert normalize_flag("--api_key") == "--api-key"
    assert normalize_flag("--N_GPU_Layers") == "--n-gpu-layers"
    assert normalize_flag("-c") == "-c" and normalize_flag("-ngl") == "-ngl"
    assert normalize_flag("-n_x") == "-n_x"  # a short option is not rewritten


@pytest.mark.parametrize("flag", LONG_FLAGS)
def test_underscore_and_case_spellings_of_a_forbidden_long_flag_are_refused(flag):
    """llama.cpp reads `_` as `-` in a long option: `--api_key` is `--api-key`."""
    for typed in ("--" + flag[2:].replace("-", "_"), flag.upper(), "--" + flag[2:].replace("-", "_").upper()):
        for text in (typed, f"{typed} 1", f"--ok {typed} 1", f"{typed}=1"):
            with pytest.raises(ExtraArgsInvalid) as err:
                validate_extra_args(text)
            assert err.value.args[0] == f"Флаг {typed} задаётся порталом", text


def test_a_mixed_spelling_is_refused():
    for typed in ("--api_key", "--n_gpu_layers", "--tensor_split", "--log_file", "--cache_type_k", "--api-key_file"):
        with pytest.raises(ExtraArgsInvalid):
            validate_extra_args(f"{typed} x")


@pytest.mark.parametrize(
    "flag",
    [
        "--some-file", "--other-path", "--scratch-dir", "--remote-url", "--some_file", "--SOME-PATH",
        "--x-cache-static", "--x-cache-dynamic", "--x_cache_static", "--secret-key", "--my_key_id", "--tls-key",
    ],
)
def test_any_long_flag_about_files_paths_or_keys_is_refused(flag):
    for text in (flag, f"{flag} 1", f"{flag}=1"):
        with pytest.raises(ExtraArgsInvalid) as err:
            validate_extra_args(text)
        assert err.value.args[0] == f"Флаги с файлами, путями и ключами задаются порталом: {flag}", text


def test_the_generic_rule_leaves_other_flags_alone():
    assert validate_extra_args("--keep 10 --rope-scaling yarn --cache-reuse 256 --mlock -fa on") == [
        "--keep", "10", "--rope-scaling", "yarn", "--cache-reuse", "256", "--mlock", "-fa", "on",
    ]


def test_error_names_the_first_offending_token():
    with pytest.raises(ExtraArgsInvalid, match="Флаг --port задаётся порталом"):
        validate_extra_args("--mlock --port 1 --host x")


@pytest.mark.parametrize(
    "text,tokens",
    [
        ("", []),
        ("   ", []),
        (None, []),
        ("--mlock", ["--mlock"]),
        ("--rope-freq-base 1e6 --no-context-shift", ["--rope-freq-base", "1e6", "--no-context-shift"]),
        ("--rope-freq-base=1e6", ["--rope-freq-base=1e6"]),
        ('--override-kv "tokenizer.ggml.add_bos_token=bool:false"', ["--override-kv", "tokenizer.ggml.add_bos_token=bool:false"]),
        ("--temp-x 0.5", ["--temp-x", "0.5"]),  # not the forbidden --temp: matched by the whole name
        ("--threads-batch 4 -tb 4", ["--threads-batch", "4", "-tb", "4"]),
        ("--logit-bias -1", ["--logit-bias", "-1"]),
        ("--chat-template 'chatml'", ["--chat-template", "chatml"]),
    ],
)
def test_allowed_flags_are_split_like_a_shell(text, tokens):
    assert validate_extra_args(text) == tokens


@pytest.mark.parametrize(
    "text",
    [
        "--foo /etc/passwd",
        "--foo C:\x5cx\x5cy.gbnf",
        "--foo ..\x5cy",
        "--foo '..\x5c..\x5cy'",
        "--foo ~/g.gbnf",
        "--foo ~",
        "--foo=/etc/passwd",
        "--foo=~/x",
        "--x org/model",
        "--x https://host/model.gguf",
        "--x ./relative",
        "--x ../up",
        "--x .",
        "--x ..",
        "--x=.",
    ],
)
def test_a_path_like_value_is_refused(text):
    with pytest.raises(ExtraArgsInvalid, match="Пути к файлам в своих флагах запрещены"):
        validate_extra_args(text)


def test_the_path_error_names_the_token():
    with pytest.raises(ExtraArgsInvalid, match="Пути к файлам в своих флагах запрещены: /etc/passwd"):
        validate_extra_args("--foo /etc/passwd")


def test_flags_must_start_with_a_dash():
    with pytest.raises(ExtraArgsInvalid, match="начинаются с «-»: mlock"):
        validate_extra_args("mlock")
    with pytest.raises(ExtraArgsInvalid):
        validate_extra_args("value --mlock")


def test_at_most_40_tokens():
    assert len(validate_extra_args(" ".join(["--a"] * 40))) == 40
    with pytest.raises(ExtraArgsInvalid, match="не больше 40"):
        validate_extra_args(" ".join(["--a"] * 41))
    assert len(validate_extra_args("--a " + " ".join(["1"] * 39))) == 40
    with pytest.raises(ExtraArgsInvalid, match="не больше 40"):
        validate_extra_args("--a " + " ".join(["1"] * 40))


def test_the_text_size_is_capped():
    with pytest.raises(ExtraArgsInvalid, match="2000"):
        validate_extra_args("--a " + "x" * 2000)
    validate_extra_args("--a " + "x" * 1900)


@pytest.mark.parametrize("text", ["--x 'a\nb'", "--x 'a\tb'", "--x é", "--x '\x00'", "--x 'a\x7f'", "--x 日本"])
def test_a_token_is_printable_ascii_without_a_newline(text):
    with pytest.raises(ExtraArgsInvalid, match="печатные символы ASCII"):
        validate_extra_args(text)


def test_a_quoted_token_may_hold_a_space():
    assert validate_extra_args("--override-kv 'a b=int:1'") == ["--override-kv", "a b=int:1"]


@pytest.mark.parametrize("text", ['--chat-template ""', "--note ''", "--seed 1 --x ''", '--x=a ""'])
def test_an_empty_token_is_refused(text):
    """The node refuses an empty token (serve-llama.sh), so the portal must not save it: the pods would crash-loop."""
    with pytest.raises(ExtraArgsInvalid, match="Пустое значение флага"):
        validate_extra_args(text)


def test_an_unclosed_quote_is_refused():
    with pytest.raises(ExtraArgsInvalid, match="кавычка"):
        validate_extra_args("--x 'abc")


def test_quoting_does_not_hide_a_forbidden_flag():
    for text in ("'--port' 1", '"--host"=x', "--port=8080", '--p"ort" 1'):
        with pytest.raises(ExtraArgsInvalid):
            validate_extra_args(text)


def test_errors_are_value_errors_for_the_node():
    assert issubclass(ExtraArgsInvalid, ValueError) and issubclass(SamplingInvalid, ValueError)
    assert llama_args.MAX_TOKENS == 40


# ---------- sampling ----------


def test_sampling_none_and_empty_are_none():
    assert validate_sampling(None) is None
    assert validate_sampling({}) is None


def test_sampling_values_are_normalised():
    got = validate_sampling({"temperature": 1, "top_p": 0.9, "top_k": 40, "min_p": 0, "repeat_penalty": 1.1})
    assert got == {"temperature": 1.0, "top_p": 0.9, "top_k": 40, "min_p": 0.0, "repeat_penalty": 1.1}
    assert isinstance(got["temperature"], float) and isinstance(got["top_k"], int)
    assert validate_sampling({"top_k": 40.0}) == {"top_k": 40}
    assert validate_sampling({"temperature": 0.7}) == {"temperature": 0.7}


@pytest.mark.parametrize(
    "value",
    [
        {"temperature": 2}, {"temperature": 0}, {"top_p": 1}, {"top_p": 0}, {"top_k": 0}, {"top_k": 1000},
        {"min_p": 1}, {"repeat_penalty": 0.5}, {"repeat_penalty": 2},
    ],
)
def test_sampling_bounds_are_inclusive(value):
    assert validate_sampling(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        {"temperature": 2.01}, {"temperature": -0.1}, {"top_p": 1.01}, {"top_p": -0.01}, {"top_k": 1001}, {"top_k": -1},
        {"min_p": 1.5}, {"repeat_penalty": 0.49}, {"repeat_penalty": 2.1},
        {"top_k": 1.5}, {"temperature": "hot"}, {"temperature": True}, {"temperature": None},
        {"temperature": float("nan")}, {"temperature": float("inf")},
    ],
)
def test_sampling_out_of_range_or_wrong_type_is_refused(value):
    with pytest.raises(SamplingInvalid):
        validate_sampling(value)


def test_sampling_unknown_key_is_refused_in_russian():
    with pytest.raises(SamplingInvalid, match="Неизвестный параметр сэмплинга: seed"):
        validate_sampling({"temperature": 1, "seed": 3})
    with pytest.raises(SamplingInvalid):
        validate_sampling([1])
