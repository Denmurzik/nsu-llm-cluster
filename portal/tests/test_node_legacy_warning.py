from types import SimpleNamespace

from app.nodes import routes


def _request(app_state, host):
    return SimpleNamespace(app=SimpleNamespace(state=app_state), client=SimpleNamespace(host=host))


def test_legacy_warning_is_once_per_address_per_worker_process(caplog):
    caplog.set_level("WARNING", logger="app.nodes.routes")
    worker_a, worker_b = SimpleNamespace(), SimpleNamespace()  # app.state of two worker processes
    for state, host in ((worker_a, "10.0.0.5"), (worker_a, "10.0.0.5"), (worker_a, "10.0.0.6"), (worker_b, "10.0.0.5")):
        routes._warn_legacy(_request(state, host))
    assert [r.getMessage().split()[2] for r in caplog.records] == ["10.0.0.5", "10.0.0.6", "10.0.0.5"]


def test_legacy_warning_set_is_bounded(caplog, monkeypatch):
    caplog.set_level("WARNING", logger="app.nodes.routes")
    monkeypatch.setattr(routes, "LEGACY_WARNED_MAX", 2)
    state = SimpleNamespace()
    for host in ("a", "b", "c", "c"):
        routes._warn_legacy(_request(state, host))
    assert len(state.legacy_token_warned) <= 2
    assert [r.getMessage().split()[2] for r in caplog.records] == ["a", "b", "c"]
