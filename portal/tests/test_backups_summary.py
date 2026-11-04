"""What the «Состояние» page says about the daily Postgres dumps in BACKUP_DIR."""

from datetime import UTC, datetime, timedelta

from app.backups import summarize


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def touch(directory, name, size=1):
    (directory / name).write_bytes(b"x" * size)


def test_missing_directory_is_reported_as_unavailable(tmp_path):
    assert summarize(tmp_path / "nope", NOW) == {
        "available": False,
        "count": 0,
        "last_at": None,
        "last_age_s": None,
        "last_size_bytes": None,
    }


def test_empty_directory_has_no_backups(tmp_path):
    assert summarize(tmp_path, NOW) == {
        "available": True,
        "count": 0,
        "last_at": None,
        "last_age_s": None,
        "last_size_bytes": None,
    }


def test_sets_are_counted_by_timestamp_and_the_newest_complete_one_is_reported(tmp_path):
    touch(tmp_path, "portal-20261101T031700Z.dump", 10)
    touch(tmp_path, "litellm-20261101T031700Z.dump", 20)
    touch(tmp_path, "portal-20261102T031700Z.dump", 30)
    touch(tmp_path, "litellm-20261102T031700Z.dump", 40)
    last = datetime(2026, 11, 2, 3, 17, tzinfo=UTC)
    assert summarize(tmp_path, NOW) == {
        "available": True,
        "count": 2,
        "last_at": last.isoformat(),
        "last_age_s": int((NOW - last).total_seconds()),
        "last_size_bytes": 70,
    }


def test_a_half_written_set_and_foreign_files_do_not_count(tmp_path):
    touch(tmp_path, "portal-20261101T031700Z.dump", 10)
    touch(tmp_path, "litellm-20261101T031700Z.dump", 20)
    touch(tmp_path, "portal-20261102T031700Z.dump", 30)  # the litellm dump of this run is missing
    touch(tmp_path, "litellm-20261102T031700Z.dump.part", 40)  # still being written
    touch(tmp_path, "notes.txt")
    touch(tmp_path, "portal-yesterday.dump")
    result = summarize(tmp_path, NOW)
    assert result["count"] == 1
    assert result["last_at"] == datetime(2026, 11, 1, 3, 17, tzinfo=UTC).isoformat()
    assert result["last_size_bytes"] == 30
    assert result["last_age_s"] == int(timedelta(days=2, hours=8, minutes=43).total_seconds())
