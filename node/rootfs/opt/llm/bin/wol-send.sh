#!/usr/bin/env bash
# Sends a Wake-on-LAN magic packet (6 x 0xFF + 16 x MAC) over UDP to ports 9 and 7 of ADDR, from this node: the relay
# for a PC of the same subnet whose packet from main did not arrive (portal: WOL_VIA_NODE). ADDR is the broadcast of
# the common subnet; the k8s relay Job runs with hostNetwork, so the packet leaves on the PC's own network.
# Usage: wol-send.sh MAC ADDR   (WOL_PORTS="9 7" by default). Needs only perl-base, an Essential package of Ubuntu.
set -euo pipefail

mac="${1:-}"
addr="${2:-}"
[[ "$mac" =~ ^[0-9A-Fa-f]{2}([-:]?[0-9A-Fa-f]{2}){5}$ ]] || { echo "wol-send: not a MAC address: $mac" >&2; exit 2; }
[[ "$addr" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "wol-send: not an IPv4 address: $addr" >&2; exit 2; }
hex="${mac//[-:]/}"
hex="${hex,,}"
read -r -a ports <<<"${WOL_PORTS:-9 7}"

exec perl -MSocket -e '
  my ($hex, $addr, @ports) = @ARGV;
  socket(my $s, PF_INET, SOCK_DGRAM, 17) or die "wol-send: socket: $!\n";
  setsockopt($s, SOL_SOCKET, SO_BROADCAST, 1) or die "wol-send: SO_BROADCAST: $!\n";
  my $ip = inet_aton($addr) or die "wol-send: bad address $addr\n";
  my $packet = ("\xff" x 6) . (pack("H12", $hex) x 16);
  my $ok = 0;
  for my $p (@ports) {
    if (send($s, $packet, 0, sockaddr_in($p, $ip))) { $ok++ } else { warn "wol-send: $addr:$p: $!\n" }
  }
  print "wol-send: magic packet for $hex to $addr (UDP @ports, $ok sent)\n";
  exit($ok ? 0 : 1);
' "$hex" "$addr" "${ports[@]}"
