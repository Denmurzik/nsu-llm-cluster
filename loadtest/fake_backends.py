#!/usr/bin/env python3
"""Starts K fake OpenAI backends (portal/tests/fakes/openai_backend.py) on consecutive ports of the load host.

    python fake_backends.py --count 4 --port-base 28100 --delay-ms 50 --advertise-ip 10.0.0.7

Prints the `--api-base` URLs for `prepare.py register-backends`; stops all backends on Ctrl-C.
Needs `starlette` and `uvicorn` (loadtest/requirements.txt). The main node must reach host:port,
so open 28100.. for it in the load host's firewall.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "portal" / "tests" / "fakes" / "openai_backend.py"


def build_commands(count: int, port_base: int, host: str, delay_ms: float, model_id: str,
                   fill_max_tokens: bool) -> list[list[str]]:
    commands = []
    for i in range(count):
        cmd = [sys.executable, str(BACKEND), "--host", host, "--port", str(port_base + i),
               "--model-id", model_id, "--delay-ms", f"{delay_ms:g}"]
        if fill_max_tokens:
            cmd.append("--fill-max-tokens")
        commands.append(cmd)
    return commands


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Тестовые OpenAI-бэкенды для сценария A")
    p.add_argument("--count", type=int, default=4)
    p.add_argument("--port-base", type=int, default=28100)
    p.add_argument("--host", default="0.0.0.0", help="адрес, на котором слушают бэкенды")
    p.add_argument("--advertise-ip", default="127.0.0.1", help="адрес хоста, как его видит main (для печати URL)")
    p.add_argument("--delay-ms", type=float, default=50.0, help="пауза на токен; поток длится около (токенов + 1) * delay")
    p.add_argument("--model-id", default="lt-fake")
    p.add_argument("--no-fill-max-tokens", action="store_true", help="не дополнять ответ до max_tokens слов")
    args = p.parse_args(argv)
    commands = build_commands(args.count, args.port_base, args.host, args.delay_ms, args.model_id,
                              not args.no_fill_max_tokens)
    procs = [subprocess.Popen(cmd) for cmd in commands]
    print("Бэкенды запущены. Для prepare.py register-backends:")
    print(" ".join(f"--api-base http://{args.advertise_ip}:{args.port_base + i}/v1" for i in range(args.count)))
    print("Ctrl-C останавливает все бэкенды.", flush=True)
    try:
        while all(proc.poll() is None for proc in procs):
            time.sleep(1)
        print("Один из бэкендов завершился, останавливаю остальные.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
