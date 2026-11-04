"""Unit tests of main/litellm/system_prompt_hook.py with LiteLLM stubbed out.

The real behaviour inside LiteLLM v1.106.6 is covered by portal/tests/integration/test_litellm_real.py.
"""

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "litellm" / "system_prompt_hook.py"
PROMPT = "Отвечай по-русски и коротко. СЕКРЕТНАЯ-МЕТКА"


class FakeRouter:
    def __init__(self, entries: dict[str, list[dict]]):
        self.entries = entries
        self.asked: list[str] = []

    def get_model_list(self, model_name=None):
        self.asked.append(model_name)
        return self.entries.get(model_name)


def entry(prompt=None, **info):
    model_info = {"id": "pm-1", "portal_managed": True, **info}
    if prompt is not None:
        model_info["system_prompt"] = prompt
    return {"model_name": "m", "litellm_params": {}, "model_info": model_info}


@pytest.fixture
def make_hook(monkeypatch):
    """Loads the hook with LiteLLM stubbed; returns (module, set_router)."""
    proxy_server = types.ModuleType("litellm.proxy.proxy_server")
    proxy_server.llm_router = None
    stubs = {
        "litellm": types.ModuleType("litellm"),
        "litellm.integrations": types.ModuleType("litellm.integrations"),
        "litellm.integrations.custom_logger": types.ModuleType("litellm.integrations.custom_logger"),
        "litellm.proxy": types.ModuleType("litellm.proxy"),
        "litellm.proxy.proxy_server": proxy_server,
    }
    stubs["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
    stubs["litellm.proxy"].proxy_server = proxy_server
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("system_prompt_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def set_router(router):
        proxy_server.llm_router = router

    return module, set_router


def run(module, data, call_type="completion"):
    return asyncio.run(module.handler.async_pre_call_hook(None, None, data, call_type))


def chat(*messages, model="m", **extra):
    return {"model": model, "messages": list(messages), **extra}


USER = {"role": "user", "content": "привет"}


def test_prompt_is_inserted_first_when_there_is_no_system_message(make_hook):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    out = run(module, chat(USER, {"role": "assistant", "content": "да"}))
    assert out["messages"] == [{"role": "system", "content": PROMPT}, USER, {"role": "assistant", "content": "да"}]


@pytest.mark.parametrize("role", ["system", "developer"])
def test_a_clients_own_instruction_message_is_left_alone(make_hook, role):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    mine = {"role": role, "content": "свои правила"}
    assert run(module, chat(mine, USER))["messages"] == [mine, USER]
    # not necessarily the first message
    assert run(module, chat(USER, mine))["messages"] == [USER, mine]


@pytest.mark.parametrize("prompt", [None, "", "   \n"])
def test_a_model_without_a_prompt_is_untouched(make_hook, prompt):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(prompt)]}))
    assert run(module, chat(USER))["messages"] == [USER]


def test_the_first_entry_that_has_a_prompt_wins(make_hook):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(""), entry(PROMPT), entry("другой")]}))
    assert run(module, chat(USER))["messages"][0] == {"role": "system", "content": PROMPT}


def test_streaming_requests_get_it_too(make_hook):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    out = run(module, chat(USER, stream=True, max_tokens=5))
    assert out["messages"][0] == {"role": "system", "content": PROMPT}
    assert out["stream"] is True and out["max_tokens"] == 5


@pytest.mark.parametrize("call_type", ["embeddings", "text_completion", "image_generation", "audio_transcription"])
def test_only_chat_completions_are_touched(make_hook, call_type):
    module, set_router = make_hook
    router = FakeRouter({"m": [entry(PROMPT)]})
    set_router(router)
    data = chat(USER)
    assert run(module, data, call_type)["messages"] == [USER]
    assert router.asked == []


def test_async_chat_call_type_counts_as_chat(make_hook):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    assert run(module, chat(USER), "acompletion")["messages"][0]["role"] == "system"


@pytest.mark.parametrize(
    "data",
    [
        {"model": "m"},  # no messages
        {"model": "m", "messages": "text"},
        {"model": "m", "messages": None},
        {"messages": [USER]},  # no model
        {"model": 5, "messages": [USER]},
        {"model": "unknown", "messages": [USER]},  # no entries
    ],
)
def test_unexpected_shapes_pass_through(make_hook, data):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    before = dict(data)
    assert run(module, data) == before


def test_no_router_yet_passes_through(make_hook):
    module, set_router = make_hook
    set_router(None)
    assert run(module, chat(USER))["messages"] == [USER]


def test_a_failing_lookup_never_fails_the_chat_and_never_logs_the_text(make_hook, caplog):
    module, set_router = make_hook

    class Broken:
        def get_model_list(self, model_name=None):
            raise RuntimeError(f"boom {PROMPT}")

    set_router(Broken())
    with caplog.at_level(logging.DEBUG, logger="system_prompt_hook"):
        assert run(module, chat(USER))["messages"] == [USER]
    assert "СЕКРЕТНАЯ-МЕТКА" not in caplog.text


def test_the_prompt_is_never_logged_on_the_normal_path(make_hook, caplog):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    with caplog.at_level(logging.DEBUG):
        run(module, chat(USER))
    assert "СЕКРЕТНАЯ-МЕТКА" not in caplog.text


def test_the_callers_list_is_not_mutated(make_hook):
    module, set_router = make_hook
    set_router(FakeRouter({"m": [entry(PROMPT)]}))
    messages = [USER]
    run(module, {"model": "m", "messages": messages})
    assert messages == [USER]
