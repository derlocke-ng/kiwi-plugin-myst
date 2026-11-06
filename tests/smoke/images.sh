#!/usr/bin/env bash
# Build the module's two images the way `kiwi-proxy module setup mysterium` does
# and run each once. 0.2.1 shipped a node image FROM a base that does not exist and
# an adapter image whose `apk add microsocks` fails, and nothing noticed until a
# desktop built them — the unit tests never touch a Containerfile. A build can also
# succeed and leave an image with nothing in it, so each binary is run; a tunnel
# module has two images, and both are checked.
#
#   tests/smoke/images.sh [podman build flags]      images.yml passes --no-cache
#
# Distinct tags: the :latest images kiwi-proxy runs are left alone.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

TUNNEL=kiwi-proxy/mysterium:smoke
ADAPTER=kiwi-proxy/mysterium-adapter:smoke
NAME=kf-smoke-mysterium-adapter
PORT=1080
fail() { printf 'smoke: FAIL: %s\n' "$*" >&2; podman logs "$NAME" >&2 2>&1 || true; exit 1; }
cleanup() { podman rm -f -t 0 "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== build $TUNNEL (the node)"
podman build "$@" -t "$TUNNEL" -f module/containers/tunnel/Containerfile module/containers/tunnel

echo "== myst is in the node image and runs; the entrypoint is executable, parses, and has wget"
podman run --rm --entrypoint myst "$TUNNEL" version | head -2
podman run --rm --entrypoint sh "$TUNNEL" -c \
    'test -x /usr/local/bin/kf-myst && sh -n /usr/local/bin/kf-myst && command -v wget >/dev/null' \
    || fail "the node image lacks the entrypoint or wget"

echo "== build $ADAPTER"
podman build "$@" -t "$ADAPTER" -f module/containers/adapter/Containerfile module/containers/adapter

echo "== microsocks is in the adapter image and runs"
podman run --rm --entrypoint sh "$ADAPTER" -c 'microsocks -h 2>&1 | grep "^usage: microsocks"' \
    || fail "microsocks does not run in the adapter image"

echo "== the adapter's entrypoint listens on the port kiwi-proxy passes"
podman run --detach --name "$NAME" "$ADAPTER" "$PORT" >/dev/null
hex="$(printf '%04X' "$PORT")"
for _ in $(seq 1 20); do
    # A listener (state 0A) on any address at that port, read from procfs: the
    # image has no tools beyond busybox and needs none.
    if podman exec "$NAME" grep -q ":$hex 00000000:0000 0A" /proc/net/tcp; then
        echo "ok: microsocks listens on 0.0.0.0:$PORT"
        exit 0
    fi
    [ "$(podman inspect -f '{{.State.Running}}' "$NAME")" = true ] || fail "the adapter exited during startup"
    sleep 0.5
done
fail "nothing listens on :$PORT within 10 s"
