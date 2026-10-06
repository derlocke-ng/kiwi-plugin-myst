"""The mysterium provider against the real kiwi-fox contract."""

from __future__ import annotations

from kiwi_fox.core import paths, podman
from kiwi_fox.core.providers.base import Provider, TunnelProvider


def test_manifest(manifest):
    assert manifest.name == "mysterium"
    assert manifest.socks_port == 1080
    assert manifest.auth == "none"
    assert manifest.needs_account is True
    # image is built locally, so the manifest names none to pull
    assert manifest.image is None


def test_provider_is_a_tunnel_provider(provider):
    assert isinstance(provider, Provider)
    assert isinstance(provider, TunnelProvider)


def test_image_is_the_locally_built_name(ctx):
    assert ctx.image == "kiwi-fox/mysterium:latest"


def test_tunnel_spec_mounts_identity_and_has_tun(provider, ctx):
    spec = provider.tunnel_spec(ctx, lease="0xprovider")
    assert spec.network == "kf-providers"
    assert spec.image == "kiwi-fox/mysterium:latest"
    assert "/dev/net/tun" in spec.devices
    assert spec.cap_add == ["NET_ADMIN"]
    assert spec.env["KF_MYST_PROVIDER"] == "0xprovider"
    assert any(dst == "/var/lib/mysterium-node" for _src, dst, _opts in spec.volumes)


def test_adapter_rides_the_node_netns(provider, ctx):
    tunnel = ctx.container_name("x")
    adapter = provider.adapter_spec(ctx, tunnel, lease="x")
    assert adapter.network == f"container:{tunnel}"
    assert adapter.image == "kiwi-fox/mysterium-adapter:latest"
    assert adapter.args == ["1080"]
    assert "--privileged" not in podman.spec_args(adapter)


def test_container_names(ctx):
    assert ctx.container_name() == paths.provider_container_name("mysterium")
    assert ctx.adapter_name("x") == paths.provider_container_name("mysterium-adapter", "x")
