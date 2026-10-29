# Changelog

All notable changes to kiwi-plugin-myst are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kiwi-updater installs the
latest tag matching `^v?[0-9]+(\.[0-9]+){0,3}$`.

## [Unreleased]

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
