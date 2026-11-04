"""In-memory stand-in for HfClient, driven by dict fixtures."""

from app.catalog.gguf import GgufLayout, GgufMeta
from app.catalog.hf import GgufChoice, HfFile, HfNotFound, HfUnavailable, VllmInfo, group_gguf


class FakeHf:
    """files: repo -> list[HfFile]; metas: (repo, file) -> GgufMeta; layouts: (repo, first file) -> GgufLayout;
    vllm: repo -> VllmInfo; commits: repo -> the commit sha of main.

    Set `unavailable = True` to make every call raise HfUnavailable.
    """

    def __init__(
        self,
        files: dict[str, list[HfFile]] | None = None,
        metas: dict[tuple[str, str], GgufMeta] | None = None,
        vllm: dict[str, VllmInfo] | None = None,
        layouts: dict[tuple[str, str], GgufLayout] | None = None,
        commits: dict[str, str] | None = None,
    ):
        self.files = files or {}
        self.metas = metas or {}
        self.layouts = layouts or {}
        self.vllm = vllm or {}
        self.commits = commits or {}
        self.unavailable = False
        self.commit_unavailable = False
        self.calls: list[tuple] = []

    def _guard(self, *call) -> None:
        self.calls.append(call)
        if self.unavailable:
            raise HfUnavailable("fake Hugging Face is unavailable")

    async def list_files(self, repo: str) -> list[HfFile]:
        self._guard("list_files", repo)
        if repo not in self.files:
            raise HfNotFound(repo)
        return list(self.files[repo])

    async def commit(self, repo: str) -> str | None:
        """The pinned commit of `repo`; None (no pin, as before 2-15) for a repo not in `commits`."""
        self._guard("commit", repo)
        if self.commit_unavailable:
            raise HfUnavailable("fake Hugging Face is unavailable")
        return self.commits.get(repo)

    async def gguf_files(self, repo: str) -> list[GgufChoice]:
        return group_gguf(await self.list_files(repo))

    async def gguf_meta(self, repo: str, file: str) -> GgufMeta:
        self._guard("gguf_meta", repo, file)
        if (repo, file) not in self.metas:
            raise HfNotFound(f"{repo}/{file}")
        return self.metas[(repo, file)]

    async def gguf_inspect(self, repo: str, shards: list[HfFile]) -> tuple[GgufMeta, GgufLayout | None]:
        self._guard("gguf_inspect", repo, tuple(f.path for f in shards))
        first = shards[0].path
        if (repo, first) not in self.metas:
            raise HfNotFound(f"{repo}/{first}")
        return self.metas[(repo, first)], self.layouts.get((repo, first))

    async def vllm_info(self, repo: str) -> VllmInfo:
        self._guard("vllm_info", repo)
        if repo not in self.vllm:
            raise HfNotFound(repo)
        return self.vllm[repo]
