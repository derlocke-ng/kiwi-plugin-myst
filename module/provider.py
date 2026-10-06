"""Mysterium provider: exit through the Mysterium dVPN.

A Mysterium node in consumer mode connects to a provider and routes over a tunnel,
but does not expose SOCKS5, so this is a TunnelProvider: the myst node runs on the
providers bridge and a microsocks adapter runs in its network namespace, exposing
SOCKS5 that rides the tunnel and is reachable on the bridge at the node's address.

Mysterium needs a registered consumer identity (free to create; connecting spends a
small balance). The identity/keystore is kept in the module's state directory and
mounted into the node, so it persists across launches and is not per profile.

A `--lease` is a provider identity (0x…) to connect to; omit it to let the node pick
from the discovery service. Because the exact consumer-connect flow depends on your
myst version and your registered identity, the tunnel entrypoint performs it over
the node's local API and logs clearly if it needs attention — see the README.
"""

from __future__ import annotations

from kiwi_fox.core.models import ContainerSpec, ProviderManifest
from kiwi_fox.core.providers.base import ProviderError, TunnelProvider

SOCKS_PORT = 1080
# Built locally FROM mysterium/myst (the entrypoint starts the node and connects),
# so the manifest names no pullable image: ctx.image becomes kiwi-fox/mysterium:latest.
MYST_DATA = "/var/lib/mysterium-node"

MANIFEST = ProviderManifest(
    name="mysterium",
    title="Mysterium",
    description="exit through the Mysterium dVPN",
    version="0.1.0",
    image=None,
    socks_port=SOCKS_PORT,
    auth="none",
    needs_account=True,
    requires=[],
    notes="needs a registered Mysterium identity; pass a provider id with --lease",
)


class MysteriumProvider(TunnelProvider):
    manifest = MANIFEST
    ready_timeout = 150.0

    def tunnel_spec(self, ctx, *, lease=None, country=None):
        env = {}
        if lease:
            env["KF_MYST_PROVIDER"] = str(lease)
        if country:
            env["KF_MYST_COUNTRY"] = str(country)
        data = ctx.state_dir / "myst"
        data.mkdir(parents=True, exist_ok=True)
        return ContainerSpec(
            name=ctx.container_name(lease),
            image=ctx.image,  # kiwi-fox/mysterium:latest, built by setup()
            network=ctx.network,
            env=env,
            volumes=[(str(data), MYST_DATA, "rw,z")],  # identity / keystore persists here
            devices=["/dev/net/tun"],
            cap_drop=["all"],
            cap_add=["NET_ADMIN"],
            security_opt=["no-new-privileges"],
            # Container-root, so the node can configure the tun device — and so the
            # adapter (keep-id) joining this netns matches the proven kiwi-fox
            # gateway(None)+browser(keep-id) namespace pairing.
            userns=None,
            tmpfs=["/tmp"],
            labels={"kiwi-fox.module": self.name, "kiwi-fox.role": "tunnel"},
        )

    def tunnel_ready(self, ctx, container):
        from kiwi_fox.core import podman

        logs = podman.logs(container, tail=200).lower()
        return "connection established" in logs or "status: connected" in logs

    def setup(self, ctx):
        # Both images are built locally: the node (FROM mysterium/myst, with a
        # consumer-connect entrypoint) and the microsocks adapter.
        from kiwi_fox.core import podman

        labels = {"app": "kiwi-fox", "kiwi-fox.module": self.name}
        tunnel_cf = ctx.containerfile("tunnel")
        if not tunnel_cf.exists():
            raise ProviderError(f"{self.name}: no tunnel Containerfile at {tunnel_cf}")
        podman.build(ctx.image, str(tunnel_cf), str(tunnel_cf.parent), labels=labels)

        adapter_cf = ctx.containerfile("adapter")
        if not adapter_cf.exists():
            raise ProviderError(f"{self.name}: no adapter Containerfile at {adapter_cf}")
        podman.build(ctx.adapter_image, str(adapter_cf), str(adapter_cf.parent), labels=labels)

    def leases(self, ctx):
        # Provider identities come from Mysterium's discovery service at runtime, so
        # none are hard-coded; pass one with --lease, or omit it to let the node pick.
        return []


PROVIDER = MysteriumProvider()
