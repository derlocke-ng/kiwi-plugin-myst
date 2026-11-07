# Changelog

All notable changes to kiwi-plugin-myst are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kiwi-updater installs the
latest tag matching `^v?[0-9]+(\.[0-9]+){0,3}$`.

## [Unreleased]

## [0.5.2] - 2026-11-07

- A lease is a *node*: the module says so (`lease_noun`), and kiwi-proxy 0.7.2's
  picker, editor and `module leases` call it that ("137 nodes to choose from",
  "Node id, 0xcbb6…eb78 for example"). Older kiwi-proxy ignores the attribute.
- The modules dialog shows the configured identity in full (`identity 0x…`) so it
  can be copied; it was shortened to 0x88994e…af55.

## [0.5.1] - 2026-11-07

### Fixed
- **A session that ends no longer leaves a dead exit.** Providers end sessions;
  the node did not notice and sat NotConnected while the kill switch dropped
  everything, so an exit looked up and refused every connection ("network
  unreachable"). The node now watches its session and, when it ends, fails closed
  and exits — it cannot reconnect itself: the kill switch blocks its own dial to a
  provider, and lifting it would leak a browser's traffic meanwhile. kiwi-proxy
  0.6.1 sees the provider gone and starts a fresh node and adapter behind the
  same gateway (`gateway.heal`, from the app's poll and `kiwi-proxy reap`), which
  connects anew; the exit is back within a minute, closed in between.

## [0.5.0] - 2026-11-07

### Fixed
- **The IP leak.** When the WireGuard tunnel dropped, the node's traffic fell back
  to its bridge/default route — which reaches the host — so a browser on the exit
  silently left through the host's own connection (the user's Mullvad). The node
  now installs a routing kill switch once connected: `blackhole 0.0.0.0/1` and
  `128.0.0.0/1` at a worse metric than the tunnel's own routes, so while the tunnel
  is up they are dormant and when it drops they drop consumer traffic instead of
  leaking. Mysterium's infra keeps its per-host routes, so the node still
  reconnects. (myst's own `kill_switch` cannot set up iptables in kiwi-proxy's
  cap-dropped, no-new-privileges container, and does not block this fallback.)

### Fixed (continued)
- **TLS through the tunnel hung on many providers** — DNS and plain HTTP passed, a
  TLS handshake never completed, bytes went out and little came back. The node's
  default WireGuard MTU of 1420 is too large when the path is itself a tunnel (a
  host on Mullvad, a container network behind one): large packets were dropped
  silently. The node now runs with `--wireguard.mtu 1280` (`KF_MYST_MTU` to raise
  it). Those "slow nodes" were this.

### Changed
- Readiness is proven with a neutral IP echo (ipinfo.io), not Mysterium's own
  hosts: the node allowlists those to bypass the tunnel, so they answered even when
  the tunnel carried nothing. With the kill switch on, a third party answers only
  through the tunnel — real proof, and the exit IP it reports is logged.
- Provider-by-country uses the node's `location_country` parameter. With no
  provider pinned, the node tries the best few in the country in turn and keeps the
  first that actually carries traffic, each on a short budget — some nodes advertise
  well but connect slowly, so a slow one is dropped for another instead of waited
  on. A provider the user pinned in the app is honoured with a generous budget and,
  if it carries nothing, a clear failure naming it.

## [0.4.0] - 2026-11-07

### Changed
- **Every provider, with what the picker needs.** `leases` returns every provider
  in the country (hundreds) instead of the best sixty by quality — which were all
  hosting IPs — and fills kiwi-proxy 0.5's lease fields: `kind` (residential,
  hosting, cellular, business, cdn), `quality` and `isp`, with latency and
  bandwidth in the detail. The Kiwi Proxy app's picker filters by IP type,
  searches any part of an id, a city or an ISP, and takes a typed id; a provider
  can be changed on an existing exit. On kiwi-proxy 0.4 the extra fields are
  left out.

## [0.3.0] - 2026-11-07

### Added
- **Countries from discovery.** The module tells kiwi-proxy 0.4 where Mysterium
  has providers and how many (`kiwi-proxy module countries mysterium`); the exit
  editor offers them as a searchable list and asks for that country's providers.
- **Identity import.** The module declares what it needs (kiwi-proxy 0.4's
  settings): the keystore JSON Mysterium exported and its passphrase. The Kiwi
  Proxy app asks with a file chooser and a password field; the CLI with
  `kiwi-proxy module config mysterium keystore=… --ask passphrase`. The keystore
  is copied into the module's data directory, the passphrase kept as a podman
  secret the node reads at start.
- **Providers by country.** `leases` queries Mysterium's discovery service —
  `kiwi-proxy module leases mysterium --country DE`, and the exit editor as the
  country changes — best quality first, sixty at most, with city, IP type,
  quality and ISP.

### Changed
- **The connect flow is real.** The entrypoint drives the node's API with curl:
  unlock, registration and balance (logged), the provider given or the best in
  the country, `PUT /connection`, wait for Connected; every step logs a
  `kf-myst:` line and a failure says why (`kf-myst: FATAL`), which kiwi-proxy
  reports instead of a timeout.
- One identity, one connection: a second provider while an exit still goes
  through the first is refused; an idle node on another provider is replaced.

### Fixed
- kiwi-proxy's readiness check took the node's "connected to <provider>" line
  for the final `kf-myst: connected` and started the gateway into a tunnel that
  carried nothing yet; it now wants that exact line. A session that carries no
  traffic within 75 s is dropped for a fresh one, once, before giving up.
- "Connected" is reported a few seconds before the tunnel passes traffic; a gateway
  measured in that window saw a TLS EOF. The entrypoint now says `connected` only
  once a request through the tunnel to Mysterium's location oracle has answered,
  and logs the exit it sees.
- The node is started with `--script-dir=/etc/mysterium-node`, where the image
  keeps `update-resolv-conf`; without it the node looks beside its binary and the
  connection dies with "could not set DNS: … no such file or directory".
- The node prefixes `ip` and `iptables` with sudo even as root, and sudo cannot
  run under no-new-privileges with the caps dropped ("setresuid: Operation not
  permitted"), so the tunnel interface never got its address. The image now
  carries a stand-in sudo that runs the command; the node is root with NET_ADMIN.
- The node reports its balance only after asking the hermes for it; the cached
  value a fresh node holds is 0 whatever the identity has.
- Under SELinux the node could not open `/dev/net/tun` ("failed to create TUN
  device: permission denied"); kiwi-proxy 0.4.0 runs tunnel containers unlabelled.

## [0.2.3] - 2026-11-07

### Fixed
- The adapter image did not build either: the same `apk add microsocks` as
  kiwi-plugin-vpn's adapter, and microsocks is in no stable Alpine repository.
  Built from its v1.0.5 release tag in a builder stage, as kiwi-plugin-vpn 0.2.2
  does. 0.2.2 fixed the node image and missed this one.

## [0.2.2] - 2026-11-07

### Fixed
- The node image did not build: it was `FROM docker.io/mysterium/myst`, which does
  not exist on Docker Hub. The official image is `mysteriumnetwork/myst`; it has
  `myst` and busybox `wget`, which the entrypoint uses. Nothing in CI builds the
  module's images, so this is the first time it was built.

## [0.2.1] - 2026-10-29

### Changed
- Wording: kiwi-proxy works per exit, not per kiwi-fox profile, and the module's
  texts now say so.

## [0.2.0] - 2026-10-29

### Changed
- Now a **kiwi-proxy** provider module: kiwi-fox's proxy engine moved to
  [kiwi-proxy](https://github.com/derlocke-ng/kiwi-proxy), which loads this module
  for every consumer (kiwi-fox, kiwi-pentesting, exits published on the host). The
  module imports `kiwi_proxy.models` / `kiwi_proxy.providers.base` (was
  `kiwi_fox.core.…`), installs into `~/.local/share/kiwi-proxy/modules/`, labels its
  containers `kiwi-proxy.*`, builds `kiwi-proxy/…` images, and depends on kiwi-proxy.
- `install.sh` removes the copy under kiwi-fox's old modules directory and takes its
  module state along.

## [0.1.0] - 2026-10-28

### Added
- Initial Mysterium provider module for kiwi-fox: a `TunnelProvider` that runs a
  Mysterium node on the `kf-providers` bridge and a microsocks adapter in its
  network namespace, exposing SOCKS5 that rides the tunnel.
- Node image built `FROM mysterium/myst` with a thin entrypoint that starts the node
  and connects a consumer session over tequilapi; identity/keystore persists under
  `modules-state/mysterium/myst/`. `--lease` selects a provider identity.
- `module/provider.py` (`MysteriumProvider`, `MANIFEST`), the tunnel and adapter
  images (`module/containers/`), `kiwi.manifest`, `install.sh`, and unit tests
  against the kiwi-fox provider contract.
