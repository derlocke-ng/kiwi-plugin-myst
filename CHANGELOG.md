# Changelog

All notable changes to kiwi-plugin-myst are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kiwi-updater installs the
latest tag matching `^v?[0-9]+(\.[0-9]+){0,3}$`.

## [Unreleased]

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
