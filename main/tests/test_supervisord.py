"""supervisord.conf: multi-worker portal API and a single controllers process."""

import configparser
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "supervisord.conf"


def programs():
    cp = configparser.RawConfigParser(inline_comment_prefixes=None, comment_prefixes=(";",))
    cp.read(CONF, encoding="utf-8")
    return {s.split(":", 1)[1]: dict(cp[s]) for s in cp.sections() if s.startswith("program:")}


def test_programs():
    assert set(programs()) == {"caddy", "portal", "portal-controllers"}


def test_portal_api_runs_workers_without_controllers():
    portal = programs()["portal"]
    assert 'uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers "${PORTAL_WORKERS:-4}"' in portal["command"]
    assert 'CONTROLLERS_ENABLED="false"' in portal["environment"]
    # uvicorn's worker processes stop together with their supervisor
    assert portal["stopasgroup"] == "true" and portal["killasgroup"] == "true"


def test_controllers_run_once_in_their_own_process():
    ctl = programs()["portal-controllers"]
    assert ctl["command"].endswith("exec /opt/llm/portal/.venv/bin/python -m app.controllers'")
    assert ". /run/llm/portal.env" in ctl["command"]
    assert "CONTROLLERS_ENABLED" not in ctl.get("environment", "")
    assert "numprocs" not in ctl
    assert ctl["autorestart"] == "true"


def test_supervisorctl_reaches_supervisord_over_a_root_only_socket():
    # pg-restore.sh stops and starts the portal with it
    cp = configparser.RawConfigParser(inline_comment_prefixes=None, comment_prefixes=(";",))
    cp.read(CONF, encoding="utf-8")
    assert cp["unix_http_server"]["file"] == "/run/llm/supervisor.sock"
    assert cp["unix_http_server"]["chmod"] == "0700"
    assert cp["supervisorctl"]["serverurl"] == "unix:///run/llm/supervisor.sock"
    assert cp["rpcinterface:supervisor"]["supervisor.rpcinterface_factory"] == "supervisor.rpcinterface:make_main_rpcinterface"


def test_no_log_file_grows_without_a_limit():
    # everything goes to the container's stdout, which `docker run --log-opt max-size/max-file` rotates (run-main.sh);
    # a program that wrote to a regular file here would need its own stdout_logfile_maxbytes and backups
    cp = configparser.RawConfigParser(inline_comment_prefixes=None, comment_prefixes=(";",))
    cp.read(CONF, encoding="utf-8")
    assert cp["supervisord"]["logfile"] == "/dev/null"
    for name, program in programs().items():
        assert program["stdout_logfile"] == "/dev/fd/1", name
        assert program["redirect_stderr"] == "true", name
