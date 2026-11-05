"""Tests of the admin API of main/hf_mirror.py (task 2-29): the cache listing, deleting, prefetch jobs."""

import http.client
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MAIN))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_mirror  # noqa: E402
from test_hf_mirror import AUTH, COMMIT, FakeHub, Running, _disk_reserve, blob  # noqa: E402,F401  (_disk_reserve: autouse)


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.close()


@pytest.fixture
def start(tmp_path):
    started = []

    def _start(upstream, **kw):
        kw = {"root": tmp_path / "data", "upstream": upstream, "upstream_token": "hf_up", "mirror_token": AUTH["Authorization"][7:],
              "max_bytes": 10**9, "revalidate_s": 3600, **kw}
        r = Running(hf_mirror.Mirror(**kw))
        started.append(r)
        return r

    yield _start
    for r in started:
        r.mirror.stop()
        r.close()


def call(m, method, path, body=None, headers=None, auth=True):
    conn = http.client.HTTPConnection("127.0.0.1", m.port, timeout=30)
    hdrs = {**(AUTH if auth else {}), **(headers or {})}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, (json.loads(raw) if raw else None)


def wait(pred, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def jobs(m) -> list[dict]:
    return call(m, "GET", "/admin/prefetch")[1]["jobs"]


def job_by_path(m, path) -> dict:
    return next(j for j in jobs(m) if j["path"] == path)


def prefetch(m, repo, files, **extra):
    status, body = call(m, "POST", "/admin/prefetch", {"repo": repo, "files": files, **extra})
    assert status == 202, body
    return body["jobs"]


def finished(m, path) -> dict:
    return wait(lambda: (j := job_by_path(m, path))["state"] in ("done", "failed", "cancelled") and j)


def cached(m, repo, path) -> bool:
    return (m.mirror.root / "cache" / repo / path).is_file()


# ---- access ----------------------------------------------------------------------------------------------------------


def test_admin_api_needs_the_token_and_is_closed_to_the_proxy(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    for method, path, body in (("GET", "/admin/cache", None), ("GET", "/admin/prefetch", None),
                               ("POST", "/admin/prefetch", {"repo": "org/m", "files": ["model.gguf"]}),
                               ("DELETE", "/admin/cache/org/m", None), ("DELETE", "/admin/prefetch/abc", None)):
        assert call(m, method, path, body, auth=False)[0] == 401
        # Caddy sets X-Forwarded-*: the nodes hold the mirror key too, but are not the portal
        assert call(m, method, path, body, {"X-Forwarded-For": "192.168.3.7"})[0] == 403
        assert call(m, method, path, body, {"X-Forwarded-Proto": "https"})[0] == 403
        assert call(m, method, path.replace("/admin", "/c/node1/admin", 1), body)[0] == 403
    assert call(m, "GET", "/admin/cache")[0] == 200
    assert jobs(m) == []


def test_a_repository_named_admin_cache_is_still_a_normal_file_request(hub, start):
    data = blob(5000)
    hub.add("admin/cache", "model.gguf", data)
    m = start(hub.url)
    resp, body = m.request("GET", "/admin/cache/resolve/main/model.gguf")
    assert resp.status == 200 and body == data


def test_other_methods_are_refused(hub, start):
    m = start(hub.url)
    assert call(m, "POST", "/org/m/resolve/main/model.gguf", {})[0] == 405
    assert call(m, "DELETE", "/api/models/org/m", None)[0] == 405


# ---- the listing -----------------------------------------------------------------------------------------------------


def test_the_listing_shows_every_cached_file_with_its_state_and_the_totals(hub, start):
    a, b = blob(300_000, 1), blob(120_000, 2)
    hub.add("org/m", "q4/model-00001-of-00002.gguf", a)
    hub.add("org/other", "x.gguf", b)
    m = start(hub.url, max_bytes=5 * 10**8)
    assert m.request("GET", "/org/m/resolve/main/q4/model-00001-of-00002.gguf")[0].status == 200
    assert m.request("GET", "/org/other/resolve/main/x.gguf")[0].status == 200
    m.settle()
    status, body = call(m, "GET", "/admin/cache")
    assert status == 200
    files = {(f["repo"], f["path"]): f for f in body["files"]}
    assert list(files) == [("org/m", "q4/model-00001-of-00002.gguf"), ("org/other", "x.gguf")]  # sorted
    first = files[("org/m", "q4/model-00001-of-00002.gguf")]
    assert first["size"] == first["total"] == len(a) and first["revision"] == COMMIT
    assert abs(first["mtime"] - time.time()) < 60 and abs(first["last_served"] - time.time()) < 60
    assert first["downloading"] is False and first["partial"] is False and first["busy"] is False
    assert body["used_bytes"] == len(a) + len(b)
    assert body["limit_bytes"] == 5 * 10**8
    disk = shutil.disk_usage(m.mirror.root)
    assert body["disk_total_bytes"] == disk.total and 0 < body["disk_free_bytes"] <= disk.total
    assert body["disk_reserve_bytes"] > 0
    before = first["last_served"]
    time.sleep(0.05)
    m.request("GET", "/org/m/resolve/main/q4/model-00001-of-00002.gguf")
    after = {(f["repo"], f["path"]): f for f in call(m, "GET", "/admin/cache")[1]["files"]}[("org/m", "q4/model-00001-of-00002.gguf")]
    assert after["last_served"] > before  # the last time a node was served


def test_the_listing_shows_a_download_in_progress_and_a_part_a_dead_one_left(hub, start):
    data = blob(1_500_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.03
    m = start(hub.url)
    t = threading.Thread(target=lambda: m.request("GET", "/org/m/resolve/main/model.gguf"))
    t.start()
    entry = wait(lambda: next((f for f in call(m, "GET", "/admin/cache")[1]["files"] if f["downloading"] and f["written"] > 0), None))
    assert entry["partial"] is True and entry["total"] == len(data) and entry["size"] == entry["written"] < len(data)
    t.join(60)
    m.settle()
    done = call(m, "GET", "/admin/cache")[1]["files"]
    assert [(f["path"], f["downloading"], f["partial"]) for f in done] == [("model.gguf", False, False)]
    # a part left by a download that died: listed as partial, counted in the volume
    hub.chunk_delay = 0.0
    hub.add("org/m", "second.gguf", blob(400_000, 3))
    hub.cut_after = 150_000
    m.request("GET", "/org/m/resolve/main/second.gguf")
    m.settle()
    listing = call(m, "GET", "/admin/cache")[1]
    part = next(f for f in listing["files"] if f["path"] == "second.gguf")
    assert part["partial"] is True and part["downloading"] is False and 0 < part["size"] < 400_000 and part["total"] == 400_000
    assert listing["used_bytes"] == len(data) + part["size"]


# ---- deleting --------------------------------------------------------------------------------------------------------


def test_delete_removes_a_file_with_its_state_and_a_repository_with_everything(hub, start):
    for p in ("a.gguf", "sub/b.gguf"):
        hub.add("org/m", p, blob(50_000))
    hub.add("org/keep", "k.gguf", blob(10_000))
    m = start(hub.url)
    for repo, p in (("org/m", "a.gguf"), ("org/m", "sub/b.gguf"), ("org/keep", "k.gguf")):
        m.request("GET", f"/{repo}/resolve/main/{p}")
    m.settle()
    root = m.mirror.root
    status, body = call(m, "DELETE", "/admin/cache/org/m/sub/b.gguf")
    assert status == 200 and body == {"deleted": [{"repo": "org/m", "path": "sub/b.gguf", "size": 50_000}], "freed_bytes": 50_000}
    assert not (root / "cache/org/m/sub").exists() and not (root / ".mirror/state/org/m/sub").exists()
    assert (root / "cache/org/m/a.gguf").is_file()
    assert call(m, "DELETE", "/admin/cache/org/m/sub/b.gguf")[0] == 404
    status, body = call(m, "DELETE", "/admin/cache/org/m")
    assert status == 200 and body["freed_bytes"] == 50_000 and [d["path"] for d in body["deleted"]] == ["a.gguf"]
    assert not (root / "cache/org/m").exists() and not (root / ".mirror/state/org/m").exists()
    assert (root / "cache/org/keep/k.gguf").is_file() and (root / ".mirror/state/org/keep/k.gguf.json").is_file()
    assert call(m, "DELETE", "/admin/cache/org/m")[0] == 404
    assert [f["path"] for f in call(m, "GET", "/admin/cache")[1]["files"]] == ["k.gguf"]


def test_delete_refuses_a_file_being_served_and_deletes_nothing_of_the_repository(hub, start):
    for p in ("a.gguf", "b.gguf"):
        hub.add("org/m", p, blob(50_000))
    m = start(hub.url)
    for p in ("a.gguf", "b.gguf"):
        m.request("GET", f"/org/m/resolve/main/{p}")
    m.settle()
    served = m.mirror.cache_dir / "org/m/b.gguf"
    m.mirror.reading(served, +1)  # what a request that sends the file does
    try:
        listing = {f["path"]: f for f in call(m, "GET", "/admin/cache")[1]["files"]}
        assert listing["b.gguf"]["busy"] is True and listing["a.gguf"]["busy"] is False
        status, body = call(m, "DELETE", "/admin/cache/org/m/b.gguf")
        assert status == 409 and "org/m/b.gguf" in body["error"]
        assert call(m, "DELETE", "/admin/cache/org/m")[0] == 409
        assert (m.mirror.cache_dir / "org/m/a.gguf").is_file() and served.is_file()  # not even the free one
        assert call(m, "DELETE", "/admin/cache/org/m/a.gguf")[0] == 200
    finally:
        m.mirror.reading(served, -1)
    assert call(m, "DELETE", "/admin/cache/org/m/b.gguf")[0] == 200


def test_delete_refuses_a_file_being_downloaded_and_removes_a_dead_downloads_part(hub, start):
    hub.add("org/m", "model.gguf", blob(1_800_000))
    hub.chunk_delay = 0.03
    m = start(hub.url)
    t = threading.Thread(target=lambda: m.request("GET", "/org/m/resolve/main/model.gguf"))
    t.start()
    wait(lambda: any(f["downloading"] for f in call(m, "GET", "/admin/cache")[1]["files"]))
    assert call(m, "DELETE", "/admin/cache/org/m/model.gguf")[0] == 409
    assert call(m, "DELETE", "/admin/cache/org/m")[0] == 409
    t.join(60)
    m.settle()
    assert call(m, "DELETE", "/admin/cache/org/m/model.gguf")[0] == 200
    hub.chunk_delay = 0.0
    hub.cut_after = 100_000
    m.request("GET", "/org/m/resolve/main/model.gguf")
    m.settle()
    parts = m.mirror.parts_dir
    assert list(parts.glob("*.part"))
    assert call(m, "DELETE", "/admin/cache/org/m/model.gguf")[0] == 200  # the abandoned part is a file of the cache too
    assert not list(parts.iterdir())


def test_delete_checks_the_names(hub, start):
    m = start(hub.url)
    assert call(m, "DELETE", "/admin/cache/org")[0] == 404
    assert call(m, "DELETE", "/admin/cache/org/m/..%2f..%2fx")[0] == 400
    assert call(m, "DELETE", "/admin/cache/org/m/.mirror")[0] == 400
    assert call(m, "DELETE", "/admin/cache/o%20x/m/a")[0] == 400


# ---- prefetch ----------------------------------------------------------------------------------------------------------


def test_prefetch_downloads_into_the_cache_as_a_nodes_request_would(hub, start):
    data = blob(900_000)
    hub.add("org/m", "q4/model.gguf", data)
    hub.chunk_delay = 0.02
    m = start(hub.url)
    started = prefetch(m, "org/m", ["q4/model.gguf"])
    assert len(started) == 1 and started[0]["state"] in ("queued", "running") and started[0]["revision"] == "main"
    assert started[0]["repo"] == "org/m" and started[0]["path"] == "q4/model.gguf" and started[0]["error"] is None
    seen = []
    done = wait(lambda: (seen.append(job_by_path(m, "q4/model.gguf")) or seen[-1])["state"] == "done" and seen[-1])
    assert done["size"] == done["written"] == len(data) and done["cached"] is False and done["error"] is None
    assert done["speed_bps"] > 0 and done["started"] <= done["finished"]
    assert any(j["state"] == "running" for j in seen)
    cached_file = m.mirror.root / "cache/org/m/q4/model.gguf"
    assert cached_file.read_bytes() == data
    state = m.mirror.read_state("org/m", "q4/model.gguf")
    assert state["commit"] == COMMIT and state["size"] == len(data) and state["accessed"]
    entry = call(m, "GET", "/admin/cache")[1]["files"][0]
    assert entry["revision"] == COMMIT and entry["partial"] is False
    resp, body = m.request("GET", "/org/m/resolve/main/q4/model.gguf")  # a node: from the cache
    assert body == data and len(hub.cdn_gets) == 1


def test_prefetch_takes_the_requested_revision(hub, start):
    pinned = "d" * 40
    data = blob(40_000, 7)
    hub.at_commit[("org/m", pinned, "model.gguf")] = data
    m = start(hub.url)
    prefetch(m, "org/m", ["model.gguf"], revision=pinned)
    assert finished(m, "model.gguf")["state"] == "done"
    assert (m.mirror.cache_dir / "org/m/model.gguf").read_bytes() == data
    assert m.mirror.read_state("org/m", "model.gguf")["commit"] == pinned
    assert [p for _, p, _ in hub.hub_requests if "resolve" in p][0].startswith(f"/org/m/resolve/{pinned}/")


def test_prefetch_of_a_file_already_here_downloads_nothing(hub, start):
    data = blob(60_000)
    hub.add("org/m", "model.gguf", data)
    m = start(hub.url)
    m.request("GET", "/org/m/resolve/main/model.gguf")
    m.settle()
    assert len(hub.cdn_gets) == 1
    before = m.mirror.read_state("org/m", "model.gguf")["accessed"]
    time.sleep(0.05)
    prefetch(m, "org/m", ["model.gguf"])
    job = finished(m, "model.gguf")
    assert job["state"] == "done" and job["cached"] is True and job["size"] == len(data)
    assert len(hub.cdn_gets) == 1
    assert m.mirror.read_state("org/m", "model.gguf")["accessed"] > before  # asked for: used again for the LRU
    # a file the admin put by hand counts as here too
    by_hand = m.mirror.offline_dir / "org/m/hand.gguf"
    by_hand.parent.mkdir(parents=True)
    by_hand.write_bytes(b"x" * 1000)
    prefetch(m, "org/m", ["hand.gguf"])
    assert finished(m, "hand.gguf")["cached"] is True and not cached(m, "org/m", "hand.gguf")


def test_prefetch_downloads_a_file_that_changed_upstream_again(hub, start):
    hub.add("org/m", "model.gguf", blob(30_000, 1))
    m = start(hub.url, revalidate_s=0)
    m.request("GET", "/org/m/resolve/main/model.gguf")
    m.settle()
    hub.add("org/m", "model.gguf", blob(30_000, 2))
    prefetch(m, "org/m", ["model.gguf"])
    assert finished(m, "model.gguf")["cached"] is False
    assert (m.mirror.cache_dir / "org/m/model.gguf").read_bytes() == blob(30_000, 2)


def test_a_file_hugging_face_does_not_have_fails_with_its_message(hub, start, tmp_path):
    m = start(hub.url)
    prefetch(m, "org/m", ["nothing.gguf"])
    job = finished(m, "nothing.gguf")
    assert job["state"] == "failed" and "Repository not found" in job["error"] and job["finished"]
    other = start(DEAD_HUB, root=tmp_path / "dead")
    prefetch(other, "org/m", ["a.gguf"])
    assert "unreachable" in finished(other, "a.gguf")["error"]
    # asking again replaces the old result
    hub.add("org/m", "nothing.gguf", blob(1000))
    prefetch(m, "org/m", ["nothing.gguf"])
    assert finished(m, "nothing.gguf")["state"] == "done" and len(jobs(m)) == 1


DEAD_HUB = "http://127.0.0.1:9"


def test_a_file_bigger_than_the_limit_fails_without_evicting_anything(hub, start):
    hub.add("org/m", "small.gguf", blob(40_000))
    hub.add("org/m", "huge.gguf", blob(300_000))
    m = start(hub.url, max_bytes=100_000)
    prefetch(m, "org/m", ["small.gguf"])
    assert finished(m, "small.gguf")["state"] == "done"
    prefetch(m, "org/m", ["huge.gguf"])
    job = finished(m, "huge.gguf")
    assert job["state"] == "failed" and "HF_MIRROR_MAX_GIB" in job["error"]
    assert cached(m, "org/m", "small.gguf")


def test_prefetch_checks_the_request(hub, start):
    m = start(hub.url)
    ok = {"repo": "org/m", "files": ["a.gguf"]}
    for bad in ({**ok, "repo": "nonsense"}, {**ok, "repo": "org/m/extra"}, {**ok, "repo": 5}, {**ok, "files": []},
                {**ok, "files": "a.gguf"}, {**ok, "files": ["../a"]}, {**ok, "files": [".mirror/x"]}, {**ok, "files": [3]},
                {**ok, "revision": "../x"}, {**ok, "revision": 7}, {**ok, "files": [f"f{i}" for i in range(201)]},
                {"files": ["a"]}, {**ok, "repo": "local/mine"}):
        status, body = call(m, "POST", "/admin/prefetch", bad)
        assert status == 400 and body["error"], bad
    assert call(m, "POST", "/admin/prefetch", b"not json")[0] == 400
    assert call(m, "POST", "/admin/prefetch", b"[1]")[0] == 400
    assert call(m, "POST", "/admin/prefetch", headers={"Content-Length": "0"})[0] == 400
    assert jobs(m) == []
    # the connection is not poisoned by a refused body
    conn = http.client.HTTPConnection("127.0.0.1", m.port, timeout=10)
    conn.request("POST", "/admin/prefetch", body=b'{"repo": "x"}', headers={**AUTH, "X-Forwarded-For": "1.2.3.4"})
    assert conn.getresponse().status == 403
    conn.close()


def test_the_same_file_asked_twice_is_one_job(hub, start):
    hub.add("org/m", "model.gguf", blob(800_000))
    hub.chunk_delay = 0.03
    m = start(hub.url)
    first = prefetch(m, "org/m", ["model.gguf", "model.gguf"])
    second = prefetch(m, "org/m", ["model.gguf"])
    assert len(first) == 1 and second[0]["id"] == first[0]["id"] and len(jobs(m)) == 1
    assert finished(m, "model.gguf")["state"] == "done" and len(hub.cdn_gets) == 1


def test_at_most_two_files_download_at_once_in_the_order_given(hub, start):
    names = [f"part-{i}.gguf" for i in range(5)]
    for n in names:
        hub.add("org/m", n, blob(500_000))
    hub.chunk_delay = 0.03
    m = start(hub.url)
    prefetch(m, "org/m", names)
    peak = 0
    order = []
    while True:
        current = jobs(m)
        running = [j for j in current if j["state"] == "running"]
        peak = max(peak, len(running))
        for j in running:
            if j["path"] not in order:
                order.append(j["path"])
        if all(j["state"] == "done" for j in current):
            break
        time.sleep(0.02)
    assert peak == 2 and order == names
    assert call(m, "GET", "/admin/prefetch")[1]["concurrency"] == 2
    assert sorted(f["path"] for f in call(m, "GET", "/admin/cache")[1]["files"]) == names


def test_cancel_stops_a_running_job_keeps_the_part_and_a_new_request_resumes_it(hub, start):
    data = blob(3_000_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.03
    m = start(hub.url)
    job = prefetch(m, "org/m", ["model.gguf"])[0]
    wait(lambda: job_by_path(m, "model.gguf")["written"] > 100_000)
    status, body = call(m, "DELETE", f"/admin/prefetch/{job['id']}")
    assert status == 200 and body["state"] == "cancelled" and body["error"]
    m.settle()
    assert not cached(m, "org/m", "model.gguf") and not m.mirror.downloads
    part = next(f for f in call(m, "GET", "/admin/cache")[1]["files"] if f["path"] == "model.gguf")
    assert part["partial"] is True and part["downloading"] is False and part["size"] > 0
    hub.chunk_delay = 0.0
    prefetch(m, "org/m", ["model.gguf"])
    assert finished(m, "model.gguf")["state"] == "done"
    assert (m.mirror.cache_dir / "org/m/model.gguf").read_bytes() == data
    assert hub.cdn_gets[-1]["Range"].startswith("bytes=") and not hub.cdn_gets[-1]["Range"].startswith("bytes=0-")


def test_cancel_takes_a_queued_job_out_before_it_starts_and_a_finished_one_off_the_list(hub, start):
    names = ["a.gguf", "b.gguf", "c.gguf"]
    for n in names:
        hub.add("org/m", n, blob(600_000))
    hub.chunk_delay = 0.03
    m = start(hub.url)
    ids = {j["path"]: j["id"] for j in prefetch(m, "org/m", names)}
    wait(lambda: job_by_path(m, "b.gguf")["state"] == "running")
    status, body = call(m, "DELETE", f"/admin/prefetch/{ids['c.gguf']}")
    assert status == 200 and body["state"] == "cancelled" and body["started"] is None
    assert finished(m, "a.gguf")["state"] == "done" and finished(m, "b.gguf")["state"] == "done"
    time.sleep(0.2)
    assert job_by_path(m, "c.gguf")["state"] == "cancelled" and not cached(m, "org/m", "c.gguf")
    assert call(m, "DELETE", f"/admin/prefetch/{ids['a.gguf']}")[0] == 200  # a finished job: off the list
    assert sorted(j["path"] for j in jobs(m)) == ["b.gguf", "c.gguf"]
    assert call(m, "DELETE", f"/admin/prefetch/{ids['a.gguf']}")[0] == 404
    assert call(m, "DELETE", "/admin/prefetch/not-an-id")[0] == 404
    status, body = call(m, "DELETE", "/admin/prefetch")  # all the finished ones
    assert status == 200 and body == {"removed": 2} and jobs(m) == []


def test_cancelling_a_prefetch_does_not_stop_a_download_a_node_waits_for(hub, start):
    data = blob(1_500_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.03
    m = start(hub.url)
    result = []
    t = threading.Thread(target=lambda: result.append(m.request("GET", "/org/m/resolve/main/model.gguf")))
    t.start()
    wait(lambda: m.mirror.readers)  # the node is being served from the growing file
    job = prefetch(m, "org/m", ["model.gguf"])[0]
    wait(lambda: job_by_path(m, "model.gguf")["state"] == "running")
    assert call(m, "DELETE", f"/admin/prefetch/{job['id']}")[1]["state"] == "cancelled"
    t.join(60)
    assert result[0][0].status == 200 and result[0][1] == data and len(hub.cdn_gets) == 1


def test_cancelling_a_prefetch_does_not_stop_a_download_a_node_has_joined_but_not_yet_read(hub, start):
    data = blob(1_500_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.03
    m = start(hub.url)
    job = prefetch(m, "org/m", ["model.gguf"])[0]
    wait(lambda: job_by_path(m, "model.gguf")["state"] == "running")
    download = wait(lambda: m.mirror.downloads.get(("org/m", "model.gguf")))
    m.mirror.join(download, 1)  # a node request waiting in wait_ready(): no reader yet
    assert call(m, "DELETE", f"/admin/prefetch/{job['id']}")[1]["state"] == "cancelled"
    time.sleep(0.3)
    assert download.cancelled is False and download.error is None
    m.mirror.join(download, -1)
    wait(lambda: download.done)
    assert (m.mirror.cache_dir / "org/m/model.gguf").read_bytes() == data


def test_a_pinned_prefetch_does_not_take_a_running_download_of_main_for_its_file(hub, start):
    main_data, pinned = blob(1_500_000, 1), blob(40_000, 2)
    pin = "d" * 40
    hub.add("org/m", "model.gguf", main_data)
    hub.at_commit[("org/m", pin, "model.gguf")] = pinned
    hub.chunk_delay = 0.03
    m = start(hub.url)
    result = []
    t = threading.Thread(target=lambda: result.append(m.request("GET", "/org/m/resolve/main/model.gguf")))
    t.start()
    wait(lambda: m.mirror.downloads)
    prefetch(m, "org/m", ["model.gguf"], revision=pin)
    t.join(60)
    assert finished(m, "model.gguf")["state"] == "done"
    assert (m.mirror.cache_dir / "org/m/model.gguf").read_bytes() == pinned  # the pinned file, not the one of main


def test_a_prefetch_shares_the_download_with_a_node(hub, start):
    data = blob(1_500_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.03
    m = start(hub.url)
    result = []
    t = threading.Thread(target=lambda: result.append(m.request("GET", "/org/m/resolve/main/model.gguf")))
    t.start()
    wait(lambda: m.mirror.downloads)
    prefetch(m, "org/m", ["model.gguf"])
    t.join(60)
    assert finished(m, "model.gguf")["state"] == "done"
    assert result[0][1] == data and len(hub.cdn_gets) == 1


# ---- restart -----------------------------------------------------------------------------------------------------------


def test_finished_jobs_stay_in_the_list_after_a_restart(hub, start):
    hub.add("org/m", "ok.gguf", blob(20_000))
    m = start(hub.url)
    prefetch(m, "org/m", ["ok.gguf", "missing.gguf"])
    finished(m, "ok.gguf")
    finished(m, "missing.gguf")
    before = {j["path"]: j for j in jobs(m)}
    m.mirror.stop()
    m2 = start(hub.url)
    assert {j["path"]: j for j in jobs(m2)} == before


def test_queued_and_running_jobs_continue_after_a_restart(hub, start, tmp_path):
    data = {n: blob(300_000, i) for i, n in enumerate(("run.gguf", "wait.gguf"))}
    for n, d in data.items():
        hub.add("org/m", n, d)
    root = tmp_path / "data"
    (root / ".mirror").mkdir(parents=True)
    now = time.time()
    # what a mirror killed in the middle of its work leaves behind
    (root / ".mirror/prefetch.json").write_text(json.dumps({"jobs": [
        {"id": "aaaaaaaaaaaa", "repo": "org/m", "revision": "main", "path": "run.gguf", "state": "running", "created": now,
         "started": now, "written": 123},
        {"id": "bbbbbbbbbbbb", "repo": "org/m", "revision": "main", "path": "wait.gguf", "state": "queued", "created": now},
        {"id": "cccccccccccc", "repo": "../x", "revision": "main", "path": "bad", "state": "queued"},
    ]}), encoding="utf-8")
    m = start(hub.url)
    assert finished(m, "run.gguf")["state"] == "done" and finished(m, "wait.gguf")["state"] == "done"
    assert len(jobs(m)) == 2
    for n, d in data.items():
        assert (m.mirror.cache_dir / "org/m" / n).read_bytes() == d


def test_a_stopped_mirror_keeps_the_job_and_the_next_one_resumes_the_part(hub, start):
    data = blob(2_000_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.03
    m = start(hub.url)
    prefetch(m, "org/m", ["model.gguf"])
    wait(lambda: job_by_path(m, "model.gguf")["written"] > 200_000)
    m.mirror.stop()
    saved = json.loads((m.mirror.root / ".mirror/prefetch.json").read_text(encoding="utf-8"))["jobs"]
    assert [j["state"] for j in saved] == ["queued"]  # not cancelled, not failed
    hub.chunk_delay = 0.0
    m2 = start(hub.url)
    assert finished(m2, "model.gguf")["state"] == "done"
    assert (m2.mirror.cache_dir / "org/m/model.gguf").read_bytes() == data
    assert hub.cdn_gets[-1]["Range"].startswith("bytes=") and not hub.cdn_gets[-1]["Range"].startswith("bytes=0-")
