#!/bin/sh
# kiwi-fox mysterium provider — start the Mysterium node and bring a consumer
# connection up, so a microsocks adapter in this netns can route over it.
#
# The node exposes a local HTTP API (tequilapi) on 127.0.0.1:4050. The consumer
# flow — create/unlock an identity, then POST a connection to a provider — is done
# through that API because it is more stable across versions than the CLI. Adjust
# to your myst version and registered identity if needed; this is the integration
# point the module README points at.
set -u

API=http://127.0.0.1:4050
DATADIR=/var/lib/mysterium-node

log() { printf 'kf-myst: %s\n' "$*"; }

# Start the node in the background. `--consumer` keeps it from advertising as a
# provider; the data directory (identity/keystore) is a mounted volume.
myst --data-dir="$DATADIR" --tequilapi.address=127.0.0.1 --tequilapi.port=4050 daemon &
NODE=$!

# Wait for the local API.
i=0
while [ "$i" -lt 60 ]; do
    if wget -qO- "$API/healthcheck" >/dev/null 2>&1; then break; fi
    i=$((i + 1))
    sleep 1
done

# Best-effort consumer connect. Requires a registered identity; if none is set up
# yet the node still runs and you can register/connect out of band. KF_MYST_PROVIDER
# is the target provider identity (0x…); when unset the node is left idle-connected.
ID_FILE="$DATADIR/kf-consumer-id"
if [ -s "$ID_FILE" ]; then
    CONSUMER="$(cat "$ID_FILE")"
    if [ -n "${KF_MYST_PROVIDER:-}" ]; then
        log "connecting $CONSUMER -> $KF_MYST_PROVIDER (wireguard)"
        wget -qO- --header='Content-Type: application/json' \
            --post-data="{\"consumer_id\":\"$CONSUMER\",\"provider_id\":\"$KF_MYST_PROVIDER\",\"service_type\":\"wireguard\"}" \
            "$API/connection" || log "connection request failed — check identity/balance/registration"
    else
        log "no KF_MYST_PROVIDER set; node is up, connect a provider via tequilapi"
    fi
else
    log "no consumer identity at $ID_FILE; create and register one, then write its address there"
fi

wait "$NODE"
