#!/usr/bin/env bash
# Печатает токен подключения нод (токен агента k3s) в виде K10<хэш CA>::node:<токен>: его передают run-node --token.
# Серверный токен k3s не печатается никогда: пока k3s не перезапущен с --agent-token, файл agent-token остаётся
# ссылкой на server/token, и скрипт завершается с кодом 1 (run-main повторяет попытку, пока k3s запускается).
set -euo pipefail

SECRETS_FILE=/var/lib/llm/secrets.env
AGENT_TOKEN_FILE=/var/lib/rancher/k3s/server/agent-token

agent="$(sed -n 's/^AGENT_TOKEN=//p' "$SECRETS_FILE" 2>/dev/null | tail -n1)"
[ -n "$agent" ] || { echo "Токен агента ещё не создан: главный сервер не запускался с этим образом." >&2; exit 1; }
full=""
if [ -f "$AGENT_TOKEN_FILE" ] && [ ! -L "$AGENT_TOKEN_FILE" ]; then full="$(tr -d '\r\n' <"$AGENT_TOKEN_FILE")"; fi
case "$full" in
  K10*"::node:$agent") printf '%s\n' "$full" ;;
  *) echo "Токен агента ещё не записан: k3s запускается." >&2; exit 1 ;;
esac
