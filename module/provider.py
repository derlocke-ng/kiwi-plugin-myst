"""Mysterium provider: exit through the Mysterium dVPN.

A Mysterium node in consumer mode connects to a provider and routes over a
WireGuard tunnel, but does not expose SOCKS5, so this is a TunnelProvider: the
myst node runs on the providers bridge and a microsocks adapter runs in its
network namespace, exposing SOCKS5 that rides the tunnel and is reachable on the
bridge at the node's address.

What the user brings: a registered consumer identity — the keystore JSON Mysterium
exported (``0x….json``) and its passphrase. ``configure`` copies the keystore into
the module's own data directory (mounted into the node) and keeps the passphrase
as a podman secret the node reads at start; nothing is typed into a container.

An exit's ``--lease`` is a provider identity (``0x…``); ``leases`` lists them from
Mysterium's discovery service, best quality first, for one country when asked.
An exit with a country and no lease lets the node pick the best provider there
at connect time. One node, one connection: the identity connects to one provider
at a time, so a second exit on another provider waits for the first to go down.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path

from kiwi_proxy.models import ContainerSpec, Lease, ProviderManifest
from kiwi_proxy.providers.base import ProviderError, TunnelProvider

try:  # kiwi-proxy ≥ 0.4 asks modules what they need and where they exit
    from kiwi_proxy import countries as _countries
    from kiwi_proxy.providers.base import Country, Setting
except ImportError:  # pragma: no cover - kiwi-proxy 0.3
    _countries = None
    Country = None  # type: ignore[assignment,misc]
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Setting:  # type: ignore[no-redef]
        key: str
        title: str
        kind: str = "text"
        help: str = ""
        required: bool = True


SOCKS_PORT = 1080
# Built locally FROM mysteriumnetwork/myst (the entrypoint starts the node and connects),
# so the manifest names no pullable image: ctx.image becomes kiwi-proxy/mysterium:latest.
MYST_DATA = "/var/lib/mysterium-node"
DISCOVERY = "https://discovery.mysterium.network/api/v4"
SERVICE_TYPE = "wireguard"
# The passphrase travels as a podman secret, read by the entrypoint; never env.
PASSPHRASE_SECRET = "passphrase"
PASSPHRASE_TARGET = "/run/secrets/kf-myst-passphrase"
ID_FILE = "kf-consumer-id"
# A dropdown of hundreds of providers helps nobody: the best so many, by quality.
MAX_LEASES = 60

MANIFEST = ProviderManifest(
    name="mysterium",
    title="Mysterium",
    description="exit through the Mysterium dVPN",
    version="0.3.0",
    image=None,
    socks_port=SOCKS_PORT,
    auth="none",
    needs_account=True,
    requires=[],
    notes="needs a registered Mysterium identity: configure the module with your keystore, "
    "then pick a country and a provider",
)

SETTINGS = [
    Setting(
        "keystore",
        "Identity keystore (0x….json)",
        kind="file",
        help="The wallet file Mysterium exported. It is copied into the module's own data "
        "directory; the original stays where it is.",
    ),
    Setting(
        "passphrase",
        "Keystore passphrase",
        kind="password",
        help="Empty for most identities the node made itself.",
        required=False,
    ),
]


def _fetch_json(url: str, timeout: float = 20) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


class MysteriumProvider(TunnelProvider):
    manifest = MANIFEST
    ready_timeout = 300.0  # connect ≤150s, traffic proven ≤75s, once more if not

    # ------------------------------------------------------------------ identity
    def settings(self, ctx):
        return list(SETTINGS)

    def _data_dir(self, ctx) -> Path:
        data = ctx.state_dir / "myst"
        data.mkdir(parents=True, exist_ok=True)
        return data

    def configure(self, ctx, values):
        source = values.get("keystore", "").strip()
        if not source:
            raise ProviderError("mysterium: the keystore file is needed")
        path = Path(source).expanduser()
        try:
            raw = path.read_text()
            data = json.loads(raw)
        except OSError as exc:
            raise ProviderError(f"mysterium: cannot read {path}: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"mysterium: {path} is not a keystore JSON: {exc}") from exc
        address = str(data.get("address", "")).lower().removeprefix("0x")
        if len(address) != 40 or any(c not in "0123456789abcdef" for c in address):
            raise ProviderError(f"mysterium: {path} carries no identity address")
        if "crypto" not in data:
            raise ProviderError(f"mysterium: {path} has no encrypted key — not a keystore")
        identity = f"0x{address}"

        keystore = self._data_dir(ctx) / "keystore"
        keystore.mkdir(parents=True, exist_ok=True)
        keystore.chmod(0o700)
        # The same identity once: an older copy of it goes.
        for old in keystore.iterdir():
            if old.is_file() and old.name.endswith(address):
                old.unlink()
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H-%M-%S.000000000Z")
        dest = keystore / f"UTC--{stamp}--{address}"
        dest.write_text(raw)
        dest.chmod(0o600)
        (self._data_dir(ctx) / ID_FILE).write_text(identity + "\n")
        # Stored with a newline, so an empty passphrase is still a secret with
        # content; the entrypoint's $(cat …) drops it again.
        ctx.set_secret(PASSPHRASE_SECRET, values.get("passphrase", "") + "\n")
        return [
            f"identity {identity} imported",
            "passphrase kept as a podman secret",
            "registration and balance are checked when the node starts "
            "(podman logs of the kf-prov-mysterium container say)",
        ]

    def configured(self, ctx):
        id_file = ctx.state_dir / "myst" / ID_FILE
        if not id_file.exists():
            return None
        identity = id_file.read_text().strip()
        return f"identity {identity[:8]}…{identity[-4:]}" if identity else None

    # ------------------------------------------------------------------ providers
    def countries(self, ctx):
        """Where Mysterium has providers, with how many: the discovery service's
        own count per country."""
        if Country is None:  # pragma: no cover - kiwi-proxy 0.3 never asks
            return []
        try:
            found = _fetch_json(f"{DISCOVERY}/countries?service_type={SERVICE_TYPE}")
        except Exception as exc:  # noqa: BLE001 - the service, the network
            raise ProviderError(f"mysterium: discovery service not reachable: {exc}") from exc
        if not isinstance(found, dict):
            return []
        return [
            Country(cc.upper(), _countries.name(cc), int(n))
            for cc, n in found.items()
            if cc and int(n or 0) > 0
        ]

    def leases(self, ctx, *, country=None):
        """Providers from the discovery service, best first; one country's when
        asked (every provider in a country is often hundreds)."""
        query = {"service_type": SERVICE_TYPE}
        if country:
            query["location_country"] = country.upper()
        try:
            found = _fetch_json(f"{DISCOVERY}/proposals?{urllib.parse.urlencode(query)}")
        except Exception as exc:  # noqa: BLE001 - the service, the network
            raise ProviderError(f"mysterium: discovery service not reachable: {exc}") from exc
        proposals = found if isinstance(found, list) else found.get("proposals", [])

        def score(p) -> tuple[float, float]:
            q = p.get("quality") or {}
            return (-(q.get("quality") or 0), q.get("latency") or 9999)

        out = []
        for p in sorted(proposals, key=score)[:MAX_LEASES]:
            pid = p.get("provider_id")
            if not pid:
                continue
            loc = p.get("location") or {}
            q = p.get("quality") or {}
            bits = [loc.get("city"), loc.get("ip_type")]
            detail = f"quality {q.get('quality', '?')}"
            if loc.get("isp"):
                detail += f" · {loc['isp']}"
            out.append(
                Lease(
                    id=pid,
                    label=" · ".join(b for b in bits if b) or None,
                    country=loc.get("country"),
                    city=loc.get("city"),
                    detail=detail,
                )
            )
        return out

    # --------------------------------------------------------------------- node
    def tunnel_spec(self, ctx, *, lease=None, country=None):
        from kiwi_proxy import podman

        env = {}
        if lease:
            env["KF_MYST_PROVIDER"] = str(lease)
        if country:
            env["KF_MYST_COUNTRY"] = str(country).upper()
        data = self._data_dir(ctx)
        secrets = []
        secret = ctx.secret_name(PASSPHRASE_SECRET)
        if getattr(podman, "secret_exists", lambda _n: False)(secret):
            secrets.append((secret, PASSPHRASE_TARGET))
        return ContainerSpec(
            name=ctx.container_name(lease),
            image=ctx.image,  # kiwi-proxy/mysterium:latest, built by setup()
            network=ctx.network,
            env=env,
            volumes=[(str(data), MYST_DATA, "rw,z")],  # identity / keystore persists here
            devices=["/dev/net/tun"],
            secrets=secrets,
            cap_drop=["all"],
            cap_add=["NET_ADMIN"],
            security_opt=["no-new-privileges"],
            # Container-root, so the node can configure the tun device — and so the
            # adapter (keep-id) joining this netns matches the proven kiwi-proxy
            # gateway(None)+browser(keep-id) namespace pairing.
            userns=None,
            tmpfs=["/tmp"],
            labels={"kiwi-proxy.module": self.name, "kiwi-proxy.role": "tunnel"},
        )

    def tunnel_ready(self, ctx, container):
        from kiwi_proxy import podman

        logs = podman.logs(container, tail=200)
        if "kf-myst: FATAL" in logs:
            raise ProviderError(
                "mysterium: the node gave up — " + logs.split("kf-myst: FATAL:")[-1].strip()
            )
        # The final line, whole: "kf-myst: connected to <provider> …" comes
        # earlier, before traffic through the tunnel is proven.
        return any(line.strip() == "kf-myst: connected" for line in logs.splitlines())

    def up(self, ctx, *, lease=None, country=None):
        """One identity, one connection: a node already up through another
        provider is reused only when nothing exits through it any more."""
        from kiwi_proxy import paths, podman

        wanted = ctx.container_name(lease)
        for c in podman.ps(labels={paths.MODULE_LABEL: self.name, paths.ROLE_LABEL: "tunnel"}):
            name = (c.get("Names") or [""])[0]
            if name == wanted or c.get("State") != "running":
                continue
            other = (c.get("Labels") or {}).get(paths.LEASE_LABEL)
            if self._gateways_through(other):
                raise ProviderError(
                    f"mysterium is up through {other or 'its default provider'} for another exit; "
                    "one provider at a time — bring that exit down first"
                )
            self.down(ctx, lease=other)
        return super().up(ctx, lease=lease, country=country)

    def _gateways_through(self, lease: str | None) -> bool:
        from kiwi_proxy import paths, podman

        for c in podman.ps(labels={paths.ROLE_LABEL: "gateway", paths.MODULE_LABEL: self.name}):
            lab = c.get("Labels") or {}
            if c.get("State") == "running" and lab.get(paths.LEASE_LABEL) == lease:
                return True
        return False

    def setup(self, ctx):
        # Both images are built locally: the node (FROM mysteriumnetwork/myst, with a
        # consumer-connect entrypoint) and the microsocks adapter.
        from kiwi_proxy import podman

        labels = {"app": "kiwi-proxy", "kiwi-proxy.module": self.name}
        tunnel_cf = ctx.containerfile("tunnel")
        if not tunnel_cf.exists():
            raise ProviderError(f"{self.name}: no tunnel Containerfile at {tunnel_cf}")
        podman.build(ctx.image, str(tunnel_cf), str(tunnel_cf.parent), labels=labels)

        adapter_cf = ctx.containerfile("adapter")
        if not adapter_cf.exists():
            raise ProviderError(f"{self.name}: no adapter Containerfile at {adapter_cf}")
        podman.build(ctx.adapter_image, str(adapter_cf), str(adapter_cf.parent), labels=labels)


PROVIDER = MysteriumProvider()
