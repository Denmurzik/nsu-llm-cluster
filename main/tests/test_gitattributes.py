"""Line endings: scripts and templates stay LF on any checkout (core.autocrlf=true on Windows included).

A CRLF entrypoint.sh in an image built from a Windows clone fails with `bash\r` and restarts forever.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LF_SUFFIXES = (".sh", ".py", ".tmpl", ".conf")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def lf_files():
    files = [f for f in git("ls-files").splitlines() if f.endswith(LF_SUFFIXES) or Path(f).name == "Dockerfile"]
    assert any(f == "main/entrypoint.sh" for f in files)
    return files


def test_gitattributes_forces_lf_for_scripts_and_templates():
    files = lf_files()
    out = git("check-attr", "eol", "--", *files)
    not_lf = [line for line in out.splitlines() if not line.endswith(": eol: lf")]
    assert not_lf == []


def test_scripts_and_templates_are_lf_in_the_index():
    eol = {}
    for line in git("ls-files", "--eol", "--", *lf_files()).splitlines():
        info, path = line.split("\t", 1)
        eol[path] = info.split()[0]
    # i/none: an empty file
    assert {p: e for p, e in eol.items() if e not in ("i/lf", "i/none")} == {}


@pytest.mark.parametrize("name", ["main/entrypoint.sh", "node/rootfs/opt/llm/bin/entrypoint.sh"])
def test_autocrlf_checkout_keeps_lf(name):
    # what a Windows clone with core.autocrlf=true writes to the working tree for this path
    blob = git("show", f":{name}")
    out = subprocess.run(
        ["git", "-c", "core.autocrlf=true", "checkout-index", "--temp", "--", name],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    tmp = ROOT / out.split("\t", 1)[0]
    try:
        data = tmp.read_bytes()
    finally:
        tmp.unlink()
    assert blob and b"\r\n" not in data
