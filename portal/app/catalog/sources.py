"""Where a deployment's model comes from: Hugging Face (through the mirror) or own files on main (`local/<name>`).

A deployment keeps its model as `repo` + `file` either way; own files are the repository `local/<name>`, which the
mirror on main serves to the nodes like any other repository. So the builders, the spec hash and the standalone
config need no source field, and every caller of the Hugging Face client only has to get this router instead.
"""

from app.localmodels.store import LocalStore, is_local


class ModelSources:
    """The HfClient calls, sent to the local store for `local/...` repositories and to Hugging Face otherwise."""

    def __init__(self, hf, local: LocalStore | None):
        self.hf = hf
        self.local = local

    def _for(self, repo: str):
        return self.local if self.local is not None and is_local(repo) else self.hf

    async def list_files(self, repo: str):
        return await self._for(repo).list_files(repo)

    async def commit(self, repo: str) -> str | None:
        """The commit to pin; None for an own model (`local/...`): its files are checked by the portal itself."""
        if is_local(repo):
            return None
        return await self.hf.commit(repo)

    async def gguf_files(self, repo: str):
        return await self._for(repo).gguf_files(repo)

    async def gguf_meta(self, repo: str, file: str):
        return await self._for(repo).gguf_meta(repo, file)

    async def gguf_inspect(self, repo: str, shards):
        return await self._for(repo).gguf_inspect(repo, shards)

    async def vllm_info(self, repo: str):
        return await self._for(repo).vllm_info(repo)


def model_sources(state) -> ModelSources:
    """The router over the state of one process (API worker or controllers)."""
    return ModelSources(state.hf, getattr(state, "local_models", None))
