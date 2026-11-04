#!/usr/bin/env python3
"""Admin driver for model deployments (what the admin UI does, over the API).

Connection flags go before the subcommand:
    admin.py --base https://localhost --cacert ca.pem create-deployment --name tiny-b ...

Exit codes: 0 success, 1 request rejected or condition not reached, 2 usage or connection error.
"""
import argparse
import json
import sys
import time

import common
from common import Fail


def csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def deployment_id(portal, name):
    for item in portal.json("GET", "/api/admin/deployments"):
        if item["name"] == name:
            return item["id"]
    raise Fail(f"развёртывание {name!r} не найдено")


def cmd_create(portal, args):
    accel = args.accel or ("nvidia" if args.engine == "vllm" else "any")
    body = {
        "name": args.name, "engine": args.engine, "repo": args.repo, "file": args.file, "mode": args.mode,
        "accel": accel, "ctx_per_request": args.ctx, "parallel": args.parallel,
    }
    if args.group_size is not None:
        body["group_size"] = args.group_size
    if args.nodes is not None:
        body["nodes"] = args.nodes
    if args.auto_assign:
        body["auto_assign"] = True
    emit(portal.json("POST", "/api/admin/deployments", json=body))


def cmd_patch(portal, args):
    fields = {"ctx_per_request": args.ctx, "parallel": args.parallel, "mode": args.mode,
              "group_size": args.group_size, "accel": args.accel, "nodes": args.nodes,
              "enabled": args.enabled, "auto_assign": args.auto_assign}
    body = {key: value for key, value in fields.items() if value is not None}
    if not body:
        raise Fail("patch-deployment: не указано ни одного изменяемого поля")
    emit(portal.json("PATCH", f"/api/admin/deployments/{deployment_id(portal, args.name)}", json=body))


def cmd_assign(portal, args):
    target = None if args.none else deployment_id(portal, args.deployment)
    emit(portal.json("PUT", f"/api/admin/nodes/{args.node}/assignment", json={"deployment_id": target}))


def cmd_wait_ready(portal, args):
    dep_id = deployment_id(portal, args.name)
    deadline = time.monotonic() + args.timeout
    while True:
        status = portal.json("GET", f"/api/admin/deployments/{dep_id}/status")
        ready = status.get("ready_backends") or 0
        if ready >= args.backends:
            emit(status)
            return
        if time.monotonic() >= deadline:
            raise Fail(f"{args.name}: ready_backends={ready}, ожидалось >= {args.backends} за {args.timeout:g} с")
        time.sleep(args.poll_interval)


def cmd_status(portal, args):
    dep_id = deployment_id(portal, args.name)
    emit({"deployment": portal.json("GET", f"/api/admin/deployments/{dep_id}"),
          "status": portal.json("GET", f"/api/admin/deployments/{dep_id}/status")})


def cmd_delete(portal, args):
    portal.request("DELETE", f"/api/admin/deployments/{deployment_id(portal, args.name)}")
    print(f"развёртывание {args.name} удалено")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_connection_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-deployment")
    create.add_argument("--name", required=True)
    create.add_argument("--repo", required=True)
    create.add_argument("--engine", choices=["llamacpp", "vllm"], default="llamacpp")
    create.add_argument("--file", help="GGUF file in the repository (llama.cpp only)")
    create.add_argument("--mode", choices=["replicas", "split"], default="replicas")
    create.add_argument("--accel", choices=["any", "cpu", "nvidia"], help="default: any, nvidia for vLLM")
    create.add_argument("--ctx", type=int, required=True, help="context per request")
    create.add_argument("--parallel", type=int, required=True)
    create.add_argument("--group-size", type=int)
    create.add_argument("--nodes", type=csv, help="comma-separated node names to assign")
    create.add_argument("--auto-assign", action="store_true")
    create.set_defaults(func=cmd_create)

    patch = sub.add_parser("patch-deployment")
    patch.add_argument("--name", required=True)
    patch.add_argument("--ctx", type=int)
    patch.add_argument("--parallel", type=int)
    patch.add_argument("--mode", choices=["replicas", "split"])
    patch.add_argument("--accel", choices=["any", "cpu", "nvidia"])
    patch.add_argument("--group-size", type=int)
    patch.add_argument("--nodes", type=csv)
    patch.add_argument("--enabled", action=argparse.BooleanOptionalAction, help="--enabled / --no-enabled")
    patch.add_argument("--auto-assign", action=argparse.BooleanOptionalAction, help="--auto-assign / --no-auto-assign")
    patch.set_defaults(func=cmd_patch)

    assign = sub.add_parser("assign")
    assign.add_argument("--node", required=True)
    target = assign.add_mutually_exclusive_group(required=True)
    target.add_argument("--deployment")
    target.add_argument("--none", action="store_true", help="leave the node idle")
    assign.set_defaults(func=cmd_assign)

    wait = sub.add_parser("wait-ready")
    wait.add_argument("--name", required=True)
    wait.add_argument("--backends", type=int, required=True)
    wait.add_argument("--timeout", type=float, required=True)
    wait.add_argument("--poll-interval", type=float, default=5)
    wait.set_defaults(func=cmd_wait_ready)

    status = sub.add_parser("status")
    status.add_argument("--name", required=True)
    status.set_defaults(func=cmd_status)

    delete = sub.add_parser("delete-deployment")
    delete.add_argument("--name", required=True)
    delete.set_defaults(func=cmd_delete)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    common.validate_connection_args(parser, args)
    if args.command == "create-deployment" and args.engine == "llamacpp" and not args.file:
        parser.error("create-deployment: --file is required for the llamacpp engine")

    def run():
        portal = common.Portal.from_args(args)
        try:
            args.func(portal, args)
        finally:
            portal.close()
        return 0

    return common.run_guarded(run)


if __name__ == "__main__":
    sys.exit(main())
