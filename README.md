# kiwi-plugin-myst

A [kiwi-proxy](https://github.com/derlocke-ng/kiwi-proxy) provider module: send an
exit — a kiwi-fox browser identity, kiwi-pentesting's tools, a SOCKS5 port on
the host — through the **[Mysterium](https://mysterium.network/) dVPN**.

A Mysterium node in consumer mode routes over a tunnel but does not expose SOCKS5,
so this module runs the node on the `kf-providers` bridge and a **microsocks adapter
in the node's network namespace** — SOCKS5 that rides the tunnel and is reachable on
the bridge at the node's address. (kiwi-proxy's `TunnelProvider` pattern, shared with
kiwi-plugin-vpn.)

Mysterium needs a **registered consumer identity** (free to create; connecting
spends a small balance).

## Install

```sh
kiwi install kiwi-plugin-myst
kiwi-proxy module setup mysterium    # builds kiwi-proxy/mysterium:latest and the adapter
```

Both images are built locally: the node image is `FROM mysteriumnetwork/myst` with a thin
entrypoint that starts the node and brings a consumer connection up; the adapter is
`alpine + microsocks`.

## Wallets

A Mysterium exit pays from a **wallet**: a Mysterium identity with a balance in
MYST. The module keeps several; in the Kiwi Proxy app they are under
*Preferences → Modules → Wallets…* (kiwi-proxy 0.27 or newer).

```sh
kiwi-proxy module config mysterium keystore=/path/to/0x….json --ask passphrase name=main  # import one
kiwi-proxy module account mysterium new name=test1 --ask passphrase                       # make one
kiwi-proxy module accounts mysterium --refresh                                            # balances
kiwi-proxy module account mysterium show test1                                            # address, history
kiwi-proxy module account mysterium export test1 ~/Backups                                # the key file
```

- **New** makes the identity in a node of its own and shows its **top-up
  address**: send MYST there **on Polygon only**. Once the first top-up arrives
  the wallet registers itself (a small fee, once, from that top-up), as
  Mysterium's own app does. Sending on another chain, or to the identity
  address instead of the top-up address, is how coins get lost.
- **Export** the key file of a wallet you fund: without it and its passphrase
  the MYST in it is gone. **Remove** moves the wallet aside
  (`modules-state/mysterium/removed/`), never deletes it.
- **History**: the sessions it paid for, top-ups seen arriving, registrations,
  and top-ups bought through Mysterium's payment gateways.

Each wallet has its own data directory under
`~/.local/share/kiwi-proxy/modules-state/mysterium/` (the first one in `myst/`,
where 0.5 kept its one identity; the next in `wallets/0x…/`) and its passphrase
as a podman secret, never an environment variable.

## Use

Pick a country, then a provider in it — the exit editor's picker lists every one,
best quality first, with short id, city, IP type (residential, hosting, cellular,
…), quality and ISP; filter by IP type, search any part of an id, a city or an
ISP, or type an id to force one. The CLI does the same:

```sh
kiwi-proxy module leases mysterium --country DE --kind residential --search cbb6   # providers in Germany, best first
kiwi-proxy add myst-de --module mysterium --country de --lease 0x<provider-id> --opt wallet=main
kiwi-proxy add myst-any-de --module mysterium --country de  # no lease: the node picks the best
kiwi-proxy up myst-de && kiwi-proxy check myst-de
kiwi-fox new work --exit myst-de                           # a browser identity on it
```

Pick the wallet with `--opt wallet=NAME` (in the app: the *Wallet* list); an
exit without one uses the first wallet. A kiwi-fox profile is on one exit, so
one provider; another provider is another exit (`kiwi-fox set work --exit
myst-nl`). **One wallet holds one connection at a time**: Mysterium cancels a
second session of the same identity (tested 2026-11-20), so an exit on a second
provider with the same wallet refuses to come up while the first is up. For
exits side by side, give them different wallets.

## How it fits

```
myst node container ─────── kf-providers bridge ── kiwi-proxy gateway ── browser
  tunnel up, /dev/net/tun                           permits only <node-ip>:1080
     ▲ shares netns
microsocks adapter  (SOCKS5 0.0.0.0:1080, rides the tunnel)
```

The adapter is removed before the node on teardown. A shared node serves every
exit on the same lease and is torn down once no running gateway still uses it.

The tunnel's MTU is 1280 (`KF_MYST_MTU` in the node's environment to raise it):
the node's default of 1420 is too large when the path is itself a tunnel — a host
on Mullvad, say — and then DNS and small pages work while every TLS handshake hangs.

The node runs as container-root with `NET_ADMIN` only, under `no-new-privileges`
and rootless podman's user namespace. Two things the official image assumes are
not there: SELinux's container type may not open `/dev/net/tun`, so kiwi-proxy
0.4 runs tunnel containers unlabelled (`label=disable`); and the node prefixes
`ip`/`iptables` with sudo even as root, which cannot work with the caps dropped,
so the image carries a stand-in sudo that just runs the command.

## Status

Verified end to end on 2026-11-07 (Fedora, rootless podman, node image 1.39.9):
the identity is unlocked and its registration and balance read, the node connects
to the chosen provider, and a kiwi-proxy gateway exits through it — plain HTTP and
TLS from a member, and `kiwi-proxy check`, see the provider's address. Connecting
needs a registered identity with MYST on it; the entrypoint logs what it finds
(`podman logs kf-prov-mysterium-<provider>`), and its tequilapi calls are the
integration point to adjust when the node's API moves.

## Development

```sh
make setup && make test     # needs a kiwi-proxy checkout beside this repo (or KIWI_PROXY_SRC)
make lint
```

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
