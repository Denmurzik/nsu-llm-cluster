"""In-memory stand-in for app.deployments.progress.ProgressSource."""


class FakeProgress:
    def __init__(self, transfers: dict[str, list[dict]] | None = None, health: dict[str, int] | None = None):
        self.transfers_data = transfers
        self.health_codes = health or {}
        self.health_calls: list[str] = []

    async def transfers(self) -> dict[str, list[dict]] | None:
        return self.transfers_data

    async def health(self, url: str) -> int | None:
        self.health_calls.append(url)
        return self.health_codes.get(url)
