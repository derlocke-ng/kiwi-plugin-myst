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
myst --data-dir="$DATADIR" --config-dir="$DATADIR" --runtime-dir="$RUNDIR" \
    --script-dir=/etc/mysterium-node \
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
PROVIDER="${KF_MYST_PROVIDER:-}"
COUNTRY="${KF_MYST_COUNTRY:-}"
if [ -z "$PROVIDER" ]; then
    q="/proposals?service_type=wireguard"
    [ -n "$COUNTRY" ] && q="$q&country=$(printf '%s' "$COUNTRY" | tr 'a-z' 'A-Z')"
    api GET "$q"
    PROVIDER=$(jq -r '(.proposals // .) | sort_by(-(.quality.quality // 0)) | .[0].provider_id // empty' /tmp/api.out)
    [ -n "$PROVIDER" ] || die "no provider found${COUNTRY:+ in $COUNTRY}"
    log "picked provider $PROVIDER${COUNTRY:+ in $COUNTRY} (best quality)"
fi

body=$(jq -cn --arg c "$CONSUMER" --arg p "$PROVIDER" --arg h "$HERMES" \
    '{consumer_id:$c, provider_id:$p, hermes_id:$h, service_type:"wireguard", connect_options:{kill_switch:false, dns:"auto"}}')
connect() {  # PUT /connection, then wait for the node to say Connected
    api PUT /connection "$body"
    case "$CODE" in
        2*) log "connecting to $PROVIDER" ;;
        *) die "connection to $PROVIDER refused (HTTP $CODE: $(body))" ;;
    esac
    local i=0 state
    while :; do
        api GET /connection
        state=$(jq -r '.status // "?"' /tmp/api.out)
        case "$state" in
            Connected) break ;;
            NotConnected|Disconnecting|Canceled)
                [ "$i" -gt 8 ] && die "the connection to $PROVIDER ended ($state): $(body)" ;;
        esac
        i=$((i + 1)); [ "$i" -ge 150 ] && die "not connected after 150s (status $state)"
        sleep 1
    done
    log "session with $PROVIDER established — waiting for traffic to flow"
}

# The node says Connected before the tunnel passes traffic: the WireGuard
# handshake and the peer's NAT traversal finish seconds later — and now and then
# not at all for one session. A gateway measured in that window sees a TLS EOF,
# so the tunnel is proven with the node's own location oracle first, and a
# session that carries nothing is dropped for a fresh one, once.
ORACLE="${KF_MYST_ORACLE:-https://location.mysterium.network/api/v1/location}"
traffic() {  # up to $1 seconds
    local i=0
    while [ "$i" -lt $(( $1 / 2 )) ]; do
        if curl -4 -s -m 8 -o /tmp/where.out "$ORACLE"; then
            log "traffic flows: exit $(jq -r '[.ip, .country] | map(select(. != null)) | join(" ")' /tmp/where.out 2>/dev/null || cat /tmp/where.out)"
            return 0
        fi
        i=$((i + 1)); sleep 2
    done
    return 1
}

connect
if ! traffic 75; then
    api GET /connection
    log "nothing passes the tunnel after 75s (node says $(jq -r '.status // "?"' /tmp/api.out)); dropping the session for a new one"
    curl -s -X DELETE "$API/connection" >/dev/null 2>&1
    sleep 3
    connect
    traffic 75 || die "connected to $PROVIDER twice, but nothing passes the tunnel"
fi
log "connected"

wait "$NODE"
