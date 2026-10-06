# Changelog

All notable changes to kiwi-plugin-myst are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kiwi-updater installs the
latest tag matching `^v?[0-9]+(\.[0-9]+){0,3}$`.

## [Unreleased]

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
