#!/usr/bin/env bash
# Подписывает корневой сертификат главного (то, что Caddy отдаёт по /ca.crt) ключом CA сервера k3s: OUT_DIR/ca.crt.p7s.
# Использование: sign-ca.sh CA_FILE OUT_DIR [K3S_TLS_DIR]   (K3S_TLS_DIR по умолчанию /var/lib/rancher/k3s/server/tls)
#
# Зачем. Токен подключения ноды K10<H>::node:<секрет> несёт H = sha256 ответа https://<главный>:6443/cacerts (CA сервера k3s):
# так k3s agent проверяет главный при первом подключении. Корень Caddy (https://<главный>/ca.crt) другой, и раньше нода брала
# его без проверки (curl -k). Теперь нода проверяет CA k3s по H, а /ca.crt по этой подписи
# (node/rootfs/opt/llm/bin/fetch-main-ca.sh):
#   ca.crt.p7s: CMS (DER), отсоединённая подпись sha256 точных байтов CA_FILE, сертификат подписавшего внутри; подписавший это
#   первый сертификат server-ca.crt k3s (нода требует совпадения именно с ним: цепочки к нему недостаточно, CA k3s выпускает и
#   сертификаты, ключи которых лежат на нодах).
# Подпись пересчитывается, только если CA_FILE или CA k3s изменились (OUT_DIR/ca.crt.p7s.src: их sha256). Пока какого-то из
# файлов нет (CA k3s появляется при первом старте k3s), ничего не делает и завершается с кодом 0.
set -euo pipefail
umask 022

CA_FILE="${1:?CA_FILE}"
OUT_DIR="${2:?OUT_DIR}"
TLS_DIR="${3:-/var/lib/rancher/k3s/server/tls}"
K3S_CRT="$TLS_DIR/server-ca.crt"
K3S_KEY="$TLS_DIR/server-ca.key"

[ -s "$CA_FILE" ] && [ -s "$K3S_CRT" ] && [ -s "$K3S_KEY" ] || exit 0

src="$(cat "$CA_FILE" "$K3S_CRT" | sha256sum | cut -c1-64)"
sig="$OUT_DIR/ca.crt.p7s"
if [ -s "$sig" ] && [ "$(cat "$sig.src" 2>/dev/null)" = "$src" ]; then exit 0; fi

mkdir -p "$OUT_DIR"
tmp="$(mktemp "$OUT_DIR/.sign.XXXXXX")"
signer="$(mktemp "$OUT_DIR/.signer.XXXXXX")"
trap 'rm -f "$tmp" "$signer"' EXIT
# только первый сертификат server-ca.crt: им и подписывается (ключ server-ca.key)
sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/{p;/-----END CERTIFICATE-----/q}' "$K3S_CRT" >"$signer"
openssl cms -sign -binary -nosmimecap -md sha256 -outform DER -in "$CA_FILE" -signer "$signer" -inkey "$K3S_KEY" -out "$tmp" \
  || { echo "sign-ca.sh: не удалось подписать $CA_FILE ключом CA k3s" >&2; exit 1; }
chmod 644 "$tmp"
mv -f "$tmp" "$sig"
printf '%s\n' "$src" >"$sig.src"
echo "sign-ca.sh: $sig обновлена"
