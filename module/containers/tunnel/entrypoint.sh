#!/bin/sh
# kiwi-proxy mysterium provider — start the Mysterium node as a consumer and bring
# one connection up, so the microsocks adapter in this netns rides it.
#
# Everything goes through the node's local API (tequilapi, 127.0.0.1:4050), the
# same calls Mysterium's own apps make: unlock the identity, read its registration
# and balance, pick a provider, PUT /connection, wait for "Connected". Every step
# logs a "kf-myst:" line; kiwi-proxy waits for "kf-myst: connected" and treats
# "kf-myst: FATAL" as the reason when it does not come.
#
#   KF_MYST_PROVIDER   provider identity (0x…) to connect to; else the best in
#   KF_MYST_COUNTRY    this country (two letters); else the best anywhere
#   KF_MYST_HERMES     hermes to pay through (default: Mysterium's on Polygon)
#   /run/secrets/kf-myst-passphrase   the keystore passphrase (podman secret)
set -u

API=http://127.0.0.1:4050
DATADIR=/var/lib/mysterium-node
RUNDIR=/var/run/mysterium-node
ID_FILE="$DATADIR/kf-consumer-id"
HERMES="${KF_MYST_HERMES:-0x80ed28d84792d8b153bf2f25f0c4b7a1381de4ab}"

log() { printf 'kf-myst: %s\n' "$*"; }
die() { printf 'kf-myst: FATAL: %s\n' "$*" >&2; exit 1; }
CODE=000
api() {  # method path [json-body] -> status in $CODE, body in /tmp/api.out (body())
    # Not called in a command substitution: that would run it in a subshell and
    # lose $CODE, which is exactly the bug that once killed this script.
    local method="$1" path="$2" json="${3:-}"
    if [ -n "$json" ]; then
        CODE=$(curl -s -o /tmp/api.out -w '%{http_code}' -X "$method" \
            -H 'Content-Type: application/json' -d "$json" "$API$path" || echo 000)
    else
        CODE=$(curl -s -o /tmp/api.out -w '%{http_code}' -X "$method" "$API$path" || echo 000)
    fi
}
body() { cat /tmp/api.out 2>/dev/null; }

[ -s "$ID_FILE" ] || die "no identity: import your keystore first (kiwi-proxy module config mysterium keystore=…, or the app)"
CONSUMER=$(tr -d ' \t\r\n' < "$ID_FILE")
PASS=""
[ -f /run/secrets/kf-myst-passphrase ] && PASS=$(cat /run/secrets/kf-myst-passphrase)

mkdir -p "$RUNDIR"
log "starting the node (identity $CONSUMER)"
# --script-dir: the node sets the tunnel's DNS through update-resolv-conf, which
# it looks for beside its binary (/usr/bin/config) unless told; the image has it
# in /etc/mysterium-node.
# --wireguard.mtu: the node's default 1420 assumes a plain path. Nested inside
# another tunnel (a host on Mullvad/WireGuard, a container network behind one),
# packets that size are dropped and nothing says so: DNS and small HTTP pass, a TLS
# handshake never completes, bytes go out and little comes back. 1280 carries over
# any path (IPv6's minimum) at a small throughput cost; KF_MYST_MTU raises it.
myst --data-dir="$DATADIR" --config-dir="$DATADIR" --runtime-dir="$RUNDIR" \
    --script-dir=/etc/mysterium-node --wireguard.mtu="${KF_MYST_MTU:-1280}" \
    --local-service-discovery=false --ui.enable=false \
    --tequilapi.address=127.0.0.1 --tequilapi.port=4050 daemon &
NODE=$!

cleanup() {
    log "shutting down"
    curl -s -X DELETE "$API/connection" >/dev/null 2>&1
    kill "$NODE" 2>/dev/null
    wait "$NODE" 2>/dev/null
    exit 0
}
trap cleanup TERM INT

i=0
until curl -sf "$API/healthcheck" >/dev/null 2>&1; do
    kill -0 "$NODE" 2>/dev/null || die "the node exited during start"
    i=$((i + 1)); [ "$i" -ge 90 ] && die "the node's API did not come up in 90s"
    sleep 1
done

# ---------------------------------------------------------------- identity
api PUT "/identities/$CONSUMER/unlock" "$(jq -cn --arg p "$PASS" '{passphrase:$p}')"
case "$CODE" in
    2*) log "identity unlocked" ;;
    *) die "could not unlock $CONSUMER (HTTP $CODE: $(body)) — wrong passphrase, or no keystore for it under $DATADIR/keystore" ;;
esac
api PUT "/identities/current" "$(jq -cn --arg i "$CONSUMER" --arg p "$PASS" '{id:$i, passphrase:$p}')"
api GET "/identities/$CONSUMER"
hermes=$(jq -r '.hermes_id // empty' /tmp/api.out)
[ -n "$hermes" ] && HERMES="$hermes"
balance=$(jq -r '.balance_tokens.human // .balance // "?"' /tmp/api.out)
# The node's cached balance reads 0 until it has asked the hermes; ask now.
api PUT "/identities/$CONSUMER/balance/refresh"
case "$CODE" in
    2*) balance=$(jq -r '.balance_tokens.human // .balance // "?"' /tmp/api.out) ;;
    *) log "balance refresh answered HTTP $CODE; showing the cached value" ;;
esac
api GET "/identities/$CONSUMER/registration"
status=$(jq -r '.status // "?"' /tmp/api.out)
registered=$(jq -r '.registered // false' /tmp/api.out)
log "registration $status · balance $balance MYST · hermes $HERMES"
if [ "$registered" != "true" ]; then
    log "the identity is not registered: connecting will be refused until it is (myst account register / a top-up)"
fi

# ---------------------------------------------------------------- provider
# A pinned provider (the user chose one in the app) is honoured with a generous
# budget and a clear failure. With none pinned, try the best few in the country in
# turn and keep the first that actually carries traffic — some nodes advertise
# well but connect slowly or not at all, so a slow one is abandoned for another
# rather than waited on.
PINNED="${KF_MYST_PROVIDER:-}"
COUNTRY="${KF_MYST_COUNTRY:-}"
if [ -n "$PINNED" ]; then
    CANDIDATES="$PINNED"
else
    q="/proposals?service_type=wireguard"
    [ -n "$COUNTRY" ] && q="$q&location_country=$(printf '%s' "$COUNTRY" | tr 'a-z' 'A-Z')"
    api GET "$q"
    CANDIDATES=$(jq -r '(.proposals // .) | sort_by(-(.quality.quality // 0)) | .[0:4][].provider_id // empty' /tmp/api.out)  # 4 × (45+25)s stays under kiwi-proxy's wait
    [ -n "$CANDIDATES" ] || die "no provider found${COUNTRY:+ in $COUNTRY}"
fi

# $1 provider, $2 seconds to wait for Connected. Returns nonzero (no die) so the
# loop can move on to another node.
connect_to() {
    local prov="$1" wait_s="$2" body i=0 state
    body=$(jq -cn --arg c "$CONSUMER" --arg p "$prov" --arg h "$HERMES" \
        '{consumer_id:$c, provider_id:$p, hermes_id:$h, service_type:"wireguard", connect_options:{kill_switch:false, dns:"auto"}}')
    api PUT /connection "$body"
    case "$CODE" in 2*) ;; *) log "  $prov refused the connection (HTTP $CODE)"; return 1 ;; esac
    while :; do
        api GET /connection
        state=$(jq -r '.status // "?"' /tmp/api.out)
        [ "$state" = Connected ] && return 0
        case "$state" in NotConnected|Disconnecting|Canceled) [ "$i" -gt 6 ] && { log "  $prov ended ($state)"; return 1; } ;; esac
        i=$((i + 1)); [ "$i" -ge "$wait_s" ] && { log "  $prov not connected after ${wait_s}s ($state)"; return 1; }
        sleep 1
    done
}

# A leak-proof routing kill switch, independent of myst's own (which cannot set up
# iptables in this cap-dropped container): blackhole every unicast address (0/1 +
# 128/1) at a worse metric than myst's own 0/1+128/1 via the tunnel. While the
# tunnel is up myst's routes win and this is dormant; when it drops they vanish and
# the blackhole drops consumer traffic instead of letting it fall to the bridge's
# default route — which reaches the host, so the browser would silently exit
# through the host's own connection. Mysterium's infra keeps its own per-host
# routes via eth0, so the node still reaches the broker and can reconnect. Added
# only once the tunnel is up: before that, infra lives under 0/1 and blackholing it
# would block connecting.
killswitch_on() {
    ip route replace blackhole 0.0.0.0/1 metric 1000 2>/dev/null
    ip route replace blackhole 128.0.0.0/1 metric 1000 2>/dev/null
}
killswitch_off() {
    ip route del blackhole 0.0.0.0/1 metric 1000 2>/dev/null
    ip route del blackhole 128.0.0.0/1 metric 1000 2>/dev/null
}

# Prove traffic goes through the TUNNEL with a neutral IP echo — not Mysterium's own
# hosts, which the node allowlists to bypass the tunnel (a false "flows"). With the
# kill switch on, a third party answers only through the tunnel, so its answer is
# proof and the IP it reports is the exit the world sees.
ECHO="${KF_MYST_ECHO:-https://ipinfo.io/json}"
traffic() {  # up to $1 seconds
    local i=0 out ip cc
    while [ "$i" -lt $(( $1 / 2 )) ]; do
        if out=$(curl -4 -s -m 8 "$ECHO" 2>/dev/null) && [ -n "$out" ]; then
            ip=$(printf '%s' "$out" | jq -r '.ip // empty' 2>/dev/null)
            cc=$(printf '%s' "$out" | jq -r '.country // empty' 2>/dev/null)
            [ -n "$ip" ] && { log "traffic flows: exit $ip $cc"; return 0; }
        fi
        i=$((i + 1)); sleep 2
    done
    return 1
}

# Connect, arm the kill switch, prove traffic; tidy up on failure. $1 connect secs,
# $2 traffic secs. The kill switch is only off while no browser is attached (before
# the "connected" line kiwi-proxy waits for), so a failed attempt never leaks.
try_provider() {
    local prov="$1"
    connect_to "$prov" "$2" || return 1
    log "session with $prov established — proving traffic"
    killswitch_on
    traffic "$3" && return 0
    killswitch_off
    curl -s -X DELETE "$API/connection" >/dev/null 2>&1
    sleep 2
    return 1
}

if [ -n "$PINNED" ]; then
    # The user's own choice: a generous budget, and a clear failure naming the node.
    try_provider "$PINNED" 120 60 \
        || die "connected to $PINNED but it carried no traffic; pick another provider in the app"
else
    ok=""
    for prov in $CANDIDATES; do
        log "trying $prov"
        try_provider "$prov" 45 25 && { ok="$prov"; break; }
        log "  moving on from $prov"
    done
    [ -n "$ok" ] || die "no provider in ${COUNTRY:-any country} carried traffic; try again or pick one in the app"
fi
log "connected"

# ------------------------------------------------------------------ watchdog
# A session can end on the provider's side; the node does not reconnect by
# itself and would sit NotConnected while the kill switch drops everything — an
# exit that looks up and refuses every connection. Reconnecting from here is not
# possible either: the kill switch blocks the node's own dial to a provider, and
# lifting it would leak a browser's traffic to the host meanwhile. So this node
# fails closed and exits; kiwi-proxy sees the provider gone and starts a fresh
# node and adapter behind the same gateway (gateway.heal), which connects anew.
while kill -0 "$NODE" 2>/dev/null; do
    sleep 15
    api GET /connection
    state=$(jq -r '.status // "?"' /tmp/api.out)
    [ "$state" = Connected ] && continue
    die "the session ended (node says $state); the exit stays closed until kiwi-proxy restarts the provider"
done
wait "$NODE"
