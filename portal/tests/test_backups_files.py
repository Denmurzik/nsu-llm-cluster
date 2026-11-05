"""The dump files of BACKUP_DIR as the admin «Резервные копии» lists, downloads and deletes them."""

import os
from datetime import UTC, datetime

import pytest

from app import backups

T1, T2, T3 = "20261101T031700Z", "20261102T031700Z", "20261103T211700Z"


def put(path, size=1, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else b"x" * size)


def make_set(directory, ts, portal=10, litellm=20):
    put(directory / f"portal-{ts}.dump", portal)
    put(directory / f"litellm-{ts}.dump", litellm)


def test_sets_are_listed_newest_first_with_sizes_and_the_restore_command(tmp_path):
    make_set(tmp_path, T1, 10, 20)
    make_set(tmp_path, T2, 30, 40)
    entries = backups.list_entries(tmp_path)
    assert [e.name for e in entries] == [T2, T1]
    new = entries[0]
    assert (new.kind, new.complete, new.size_bytes) == ("set", True, 70)
    assert new.at == datetime(2026, 11, 2, 3, 17, tzinfo=UTC)
    assert [(f.name, f.db, f.size_bytes) for f in new.files] == [
        (f"litellm-{T2}.dump", "litellm", 40),
        (f"portal-{T2}.dump", "portal", 30),
    ]
    assert new.restore_command == f"docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh {T2}"


def test_an_incomplete_set_is_listed_but_cannot_be_restored(tmp_path):
    put(tmp_path / f"portal-{T1}.dump", 5)
    (entry,) = backups.list_entries(tmp_path)
    assert (entry.complete, entry.restore_command, entry.size_bytes) == (False, None, 5)


def test_partial_files_foreign_files_and_symlinks_are_not_listed(tmp_path):
    make_set(tmp_path, T1)
    put(tmp_path / f"portal-{T2}.dump.123.part", 5)  # a run in progress
    put(tmp_path / "notes.txt", 5)
    put(tmp_path / f"other-{T2}.dump", 5)  # not one of our databases
    outside = tmp_path.parent / "outside.dump"
    put(outside, 5)
    try:
        os.symlink(outside, tmp_path / f"portal-{T3}.dump")
    except (OSError, NotImplementedError):
        pass
    assert [e.name for e in backups.list_entries(tmp_path)] == [T1]


def test_the_safety_copy_of_a_restore_is_listed_with_its_own_kind(tmp_path):
    make_set(tmp_path, T1)
    put(tmp_path / f"pre-restore-{T3}" / "portal.dump", 7)
    put(tmp_path / f"pre-restore-{T3}" / "litellm.dump", 8)
    entries = backups.list_entries(tmp_path)
    assert [(e.name, e.kind) for e in entries] == [(f"pre-restore-{T3}", "pre-restore"), (T1, "set")]
    safety = entries[0]
    assert (safety.size_bytes, safety.complete, safety.restore_command) == (15, True, None)
    assert [f.name for f in safety.files] == [f"pre-restore-{T3}.litellm.dump", f"pre-restore-{T3}.portal.dump"]


def test_a_checksum_beside_a_dump_is_shown_and_garbage_is_ignored(tmp_path):
    make_set(tmp_path, T1)
    digest = "ab" * 32
    (tmp_path / f"portal-{T1}.dump.sha256").write_text(f"{digest.upper()}  portal-{T1}.dump\n", encoding="utf-8")
    (tmp_path / f"litellm-{T1}.dump.sha256").write_text("not a digest\n", encoding="utf-8")
    (entry,) = backups.list_entries(tmp_path)
    assert {f.db: f.sha256 for f in entry.files} == {"portal": digest, "litellm": None}


def test_a_missing_directory_is_none(tmp_path):
    assert backups.list_entries(tmp_path / "nope") is None
    assert backups.disk_usage(tmp_path / "nope") is None


def test_disk_usage_reports_free_and_total(tmp_path):
    usage = backups.disk_usage(tmp_path)
    assert usage["total_bytes"] >= usage["free_bytes"] > 0


@pytest.mark.parametrize(
    "name",
    [
        "../portal-20261101T031700Z.dump",
        "..",
        "/etc/passwd",
        "portal-20261101T031700Z.dump/../../x",
        "portal-20261101T031700Z.dump\x00.png",
        "portal-20261101T031700Z.dump.sha256",
        "portal-20261101T031700Z.dump.1.part",
        "postgres-20261101T031700Z.dump",
        "pre-restore-20261103T211700Z/portal.dump",
        "pre-restore-20261103T211700Z.portal.dump.x",
        "..%2fportal-20261101T031700Z.dump",
        "C:\\portal-20261101T031700Z.dump",
        "",
    ],
)
def test_resolve_refuses_names_that_are_not_dump_files(tmp_path, name):
    make_set(tmp_path, T1)
    put(tmp_path / f"pre-restore-{T3}" / "portal.dump", 7)
    assert backups.resolve_file(tmp_path, name) is None


def test_resolve_finds_set_files_and_safety_copies(tmp_path):
    make_set(tmp_path, T1)
    put(tmp_path / f"pre-restore-{T3}" / "portal.dump", 7)
    assert backups.resolve_file(tmp_path, f"portal-{T1}.dump") == tmp_path / f"portal-{T1}.dump"
    assert backups.resolve_file(tmp_path, f"pre-restore-{T3}.portal.dump") == tmp_path / f"pre-restore-{T3}" / "portal.dump"
    assert backups.resolve_file(tmp_path, f"portal-{T2}.dump") is None  # well-formed, but not there
    assert backups.resolve_file(tmp_path, f"pre-restore-{T3}.litellm.dump") is None


def test_resolve_does_not_follow_a_symlink_out_of_the_directory(tmp_path):
    outside = tmp_path.parent / "secret.dump"
    put(outside, 5)
    d = tmp_path / "b"
    d.mkdir()
    try:
        os.symlink(outside, d / f"portal-{T1}.dump")
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    assert backups.resolve_file(d, f"portal-{T1}.dump") is None


def test_delete_removes_the_whole_set_with_its_checksums_and_nothing_else(tmp_path):
    make_set(tmp_path, T1)
    make_set(tmp_path, T2)
    (tmp_path / f"portal-{T1}.dump.sha256").write_text("x", encoding="utf-8")
    removed = backups.delete_entry(tmp_path, T1)
    assert sorted(removed) == [f"litellm-{T1}.dump", f"portal-{T1}.dump"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"litellm-{T2}.dump", f"portal-{T2}.dump"]


def test_delete_of_a_safety_copy_removes_its_directory(tmp_path):
    put(tmp_path / f"pre-restore-{T3}" / "portal.dump", 7)
    put(tmp_path / f"pre-restore-{T3}" / "litellm.dump", 7)
    make_set(tmp_path, T1)
    removed = backups.delete_entry(tmp_path, f"pre-restore-{T3}")
    assert removed == [f"pre-restore-{T3}.litellm.dump", f"pre-restore-{T3}.portal.dump"]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("pre-restore")] == []
    assert (tmp_path / f"portal-{T1}.dump").exists()


@pytest.mark.parametrize(
    "name",
    ["..", "../x", "/", f"portal-{T1}.dump", T2, "pre-restore-..", "pre-restore-20261103T211700Z/..", "", "20261101T031700Z/../"],
)
def test_delete_refuses_unknown_and_malformed_names(tmp_path, name):
    make_set(tmp_path, T1)
    (tmp_path / "keep").mkdir()
    assert backups.delete_entry(tmp_path, name) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep", f"litellm-{T1}.dump", f"portal-{T1}.dump"]
