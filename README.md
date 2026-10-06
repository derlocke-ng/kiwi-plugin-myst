# kiwi-plugin-myst

A [kiwi-fox](https://github.com/derlocke-ng/kiwi-fox) provider module: exit a
browser identity through the **[Mysterium](https://mysterium.network/) dVPN**.

A Mysterium node in consumer mode routes over a tunnel but does not expose SOCKS5,
so this module runs the node on the `kf-providers` bridge and a **microsocks adapter
in the node's network namespace** — SOCKS5 that rides the tunnel and is reachable on
the bridge at the node's address. (kiwi-fox's `TunnelProvider` pattern, shared with
kiwi-plugin-vpn.)

Mysterium needs a **registered consumer identity** (free to create; connecting
spends a small balance).

## Install

```sh
kiwi install kiwi-plugin-myst
kiwi-fox module setup mysterium    # builds kiwi-fox/mysterium:latest and the adapter
```

Both images are built locally: the node image is `FROM mysterium/myst` with a thin
entrypoint that starts the node and brings a consumer connection up; the adapter is
`alpine + microsocks`.

## Identity (once)

The node's data directory — identity and keystore — lives in the module's state
directory and is mounted into the node, so it persists:

```
~/.local/share/kiwi-fox/modules-state/mysterium/myst/
    kf-consumer-id      a text file holding your registered consumer identity (0x…)
    ...                 the node's keystore
```

Create and register a consumer identity with the Mysterium node, then write its
address into `kf-consumer-id`. The tunnel entrypoint connects over the node's local
API (tequilapi on `127.0.0.1:4050`); see `module/containers/tunnel/entrypoint.sh` —
it is the one place to adjust for your myst version if the connect flow differs.

## Use

```sh
kiwi-fox new work --module mysterium --lease 0x<provider-id>   # a specific provider
kiwi-fox new work --module mysterium                           # let the node pick
kiwi-fox run work
```

`--lease` is a Mysterium **provider identity**; provider ids come from the discovery
service at runtime, so none are hard-coded.

## How it fits

```
myst node container ─────── kf-providers bridge ── kiwi-fox gateway ── browser
  tunnel up, /dev/net/tun                           permits only <node-ip>:1080
     ▲ shares netns
microsocks adapter  (SOCKS5 0.0.0.0:1080, rides the tunnel)
```

The adapter is removed before the node on teardown. A shared node serves every
profile on the same lease and is torn down once no running gateway still uses it.

## Status

The wiring, two-container lifecycle, and both images are complete and unit-tested
against the kiwi-fox contract. The consumer-connect step depends on your myst
version and a registered, funded identity, so validate `module up mysterium` on your
own machine; the entrypoint's tequilapi calls are the documented integration point
to adjust if needed.

## Development

```sh
make setup && make test     # needs a kiwi-fox checkout beside this repo (or KIWI_FOX_SRC)
make lint
```

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
