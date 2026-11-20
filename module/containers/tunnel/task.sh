#!/bin/sh
# kiwi-plugin-myst — one wallet task against a running node's local API
# (tequilapi, 127.0.0.1:4050), the calls Mysterium's own app makes:
#
#   create     make a new identity in this data dir (an empty one)
#   info       unlock, refresh the balance, read the identity, its channel
#              (top-up) address, its registration and the registration fee
#   register   register the identity; the fee comes from its balance
#   history    the sessions this node paid for, and top-up orders
#
# Run inside the wallet's node while it is up (podman exec), or by the
# entrypoint in a short-lived node (KF_MYST_TASK). Prints one line,
# "kf-myst-result: <json>"; {"error": "…"} when the task failed.
set -u
API=http://127.0.0.1:4050
DATADIR=/var/lib/mysterium-node
ID_FILE="$DATADIR/kf-consumer-id"
PASS=""
[ -f /run/secrets/kf-myst-passphrase ] && PASS=$(cat /run/secrets/kf-myst-passphrase)

out() { printf 'kf-myst-result: %s\n' "$1"; }
fail() { out "$(jq -cn --arg e "$1" '{error:$e}')"; exit 1; }
# req METHOD PATH [JSON] -> prints the HTTP status; the body is in /tmp/task.out
req() {
    if [ -n "${3:-}" ]; then
        curl -s -m 90 -o /tmp/task.out -w '%{http_code}' -X "$1" \
            -H 'Content-Type: application/json' -d "$3" "$API$2" || echo 000
    else
        curl -s -m 90 -o /tmp/task.out -w '%{http_code}' -X "$1" "$API$2" || echo 000
    fi
}
message() { jq -r '.message // .error.message // .error // empty' /tmp/task.out 2>/dev/null | head -c 200; }
keep() { cp /tmp/task.out "/tmp/task.$1" 2>/dev/null || echo '{}' > "/tmp/task.$1"; }

unlock() {
    code=$(req PUT "/identities/$ID/unlock" "$(jq -cn --arg p "$PASS" '{passphrase:$p}')")
    case "$code" in 2*) ;; *) fail "could not unlock $ID (HTTP $code: $(message)) — wrong passphrase?" ;; esac
    req PUT /identities/current "$(jq -cn --arg i "$ID" --arg p "$PASS" '{id:$i, passphrase:$p}')" >/dev/null
}

info() {
    unlock
    req PUT "/identities/$ID/balance/refresh" >/dev/null
    code=$(req GET "/identities/$ID")
    case "$code" in 2*) keep ident ;; *) fail "the node does not know $ID (HTTP $code: $(message))" ;; esac
    req GET "/identities/$ID/registration" >/dev/null; keep reg
    req GET /transactor/fees >/dev/null; keep fees
    req GET "/identities/$ID/eligibility" >/dev/null; keep elig
    out "$(jq -cn --arg id "$ID" \
        --slurpfile i /tmp/task.ident --slurpfile r /tmp/task.reg \
        --slurpfile f /tmp/task.fees --slurpfile e /tmp/task.elig '
        {id: $id,
         balance: ($i[0].balance_tokens.human // null),
         balance_wei: (($i[0].balance_tokens.wei // $i[0].balance // null) | tostring),
         channel: ($i[0].channel_address // null),
         status: ($r[0].status // $i[0].registration_status // null),
         registered: ($r[0].registered // false),
         hermes: ($i[0].hermes_id // null),
         fee: ($f[0].registration_tokens.human // null),
         free: ($e[0].eligible // false)}')"
}

case "${1:-}" in
    create)
        [ -s "$ID_FILE" ] && fail "this data dir already has an identity"
        code=$(req POST /identities "$(jq -cn --arg p "$PASS" '{passphrase:$p}')")
        case "$code" in 2*) ;; *) fail "the node made no identity (HTTP $code: $(message))" ;; esac
        ID=$(jq -r '.id // empty' /tmp/task.out)
        [ -n "$ID" ] || fail "the node's answer had no identity"
        printf '%s\n' "$ID" > "$ID_FILE"
        info
        ;;
    info)
        ID=$(tr -d ' \t\r\n' < "$ID_FILE" 2>/dev/null)
        [ -n "$ID" ] || fail "no identity in this data dir"
        info
        ;;
    register)
        ID=$(tr -d ' \t\r\n' < "$ID_FILE" 2>/dev/null)
        [ -n "$ID" ] || fail "no identity in this data dir"
        unlock
        code=$(req POST "/identities/$ID/register" '{}')
        case "$code" in
            2*) out "$(jq -cn '{registering:true}')" ;;
            *) fail "registration refused (HTTP $code: $(message))" ;;
        esac
        ;;
    history)
        ID=$(tr -d ' \t\r\n' < "$ID_FILE" 2>/dev/null)
        [ -n "$ID" ] || fail "no identity in this data dir"
        unlock  # Mysterium's payment service answers only a signed (unlocked) identity
        code=$(req GET "/sessions?direction=Consumed&page_size=200")
        case "$code" in 2*) ;; *) fail "the node lists no sessions (HTTP $code: $(message))" ;; esac
        jq -c '[(.items // [])[] | {when: .created_at, provider: .provider_id,
            country: .provider_country, seconds: .duration, bytes: ((.bytes_received // 0) + (.bytes_sent // 0)),
            tokens: ((.tokens // 0) | tostring), status: .status}]' /tmp/task.out > /tmp/task.sessions
        # Top-ups bought through Mysterium's payment gateways (card, crypto); a
        # plain MYST transfer to the top-up address is not an order.
        code=$(req GET "/v2/identities/$ID/payment-order")
        case "$code" in 2*) ;; *) code=$(req GET "/identities/$ID/payment-order") ;; esac
        case "$code" in 2*) keep orders ;; *) echo '[]' > /tmp/task.orders ;; esac
        out "$(jq -cn --slurpfile s /tmp/task.sessions --slurpfile o /tmp/task.orders '
            {sessions: $s[0], orders: [($o[0] | if type == "array" then .[] else empty end) |
              {id: .id, status: .status, gateway: .gateway_name, myst: (.receive_myst // null),
               paid: (.pay_amount // null), currency: (.pay_currency // null)}]}')"
        ;;
    *) fail "unknown task ${1:-(none)}" ;;
esac
