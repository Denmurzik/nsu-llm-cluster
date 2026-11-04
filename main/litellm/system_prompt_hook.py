"""LiteLLM proxy callback: adds the model's system prompt to chats that have none.

Loaded by LiteLLM from the directory of config.yaml (`litellm_settings.callbacks: [..., system_prompt_hook.handler]`).
The prompt is set per deployment in the portal and reaches LiteLLM as `model_info.system_prompt` of every backend entry
of the model (portal/app/controllers/backends.py); a change of the text updates the entries in place.

The hook runs before the call (`async_pre_call_hook`, chat completions only, streaming or not). It inserts
`{"role": "system", "content": <prompt>}` at the start of `messages` when the model has a non-empty prompt and no
message of the request has the role `system` or `developer`: a client that sends its own instructions keeps them.
The text is never logged, and nothing here fails the request: a model without entries, an unexpected request shape or
an error while looking the model up leaves the request as it came.
"""

import logging

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("system_prompt_hook")

CHAT_CALL_TYPES = ("completion", "acompletion")
OWN_INSTRUCTION_ROLES = ("system", "developer")


def _router():
    from litellm.proxy import proxy_server  # imported here: the hook loads (and is tested) without the proxy

    return proxy_server.llm_router


def _prompt_of(model: str) -> str | None:
    """The system prompt of the first backend entry of the model that has one (all entries of a model share it)."""
    router = _router()
    if router is None:
        return None
    for entry in router.get_model_list(model_name=model) or []:
        prompt = (entry.get("model_info") or {}).get("system_prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt
    return None


class SystemPromptHook(CustomLogger):
    def __init__(self):
        super().__init__()

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type not in CHAT_CALL_TYPES or not isinstance(data, dict):
            return data
        messages = data.get("messages")
        model = data.get("model")
        if not isinstance(messages, list) or not isinstance(model, str) or not model:
            return data
        if any(isinstance(m, dict) and m.get("role") in OWN_INSTRUCTION_ROLES for m in messages):
            return data
        try:
            prompt = _prompt_of(model)
        except Exception as exc:  # never fail a chat over the prompt; the text itself stays out of the log
            log.warning("system prompt of %s not applied: %s", model, type(exc).__name__)
            return data
        if prompt:
            data["messages"] = [{"role": "system", "content": prompt}, *messages]
        return data


handler = SystemPromptHook()
