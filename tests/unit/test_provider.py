"""The mysterium provider against the real kiwi-proxy contract."""

from __future__ import annotations

import pytest
from kiwi_proxy import paths, podman
from kiwi_proxy.providers.base import Provider, TunnelProvider


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
    assert ctx.image == "kiwi-proxy/mysterium:latest"


def test_tunnel_spec_mounts_identity_and_has_tun(provider, ctx):
    spec = provider.tunnel_spec(ctx, lease="0xprovider")
    assert spec.network == "kf-providers"
    assert spec.image == "kiwi-proxy/mysterium:latest"
    assert "/dev/net/tun" in spec.devices
    assert spec.cap_add == ["NET_ADMIN"]
    assert spec.env["KF_MYST_PROVIDER"] == "0xprovider"
    assert any(dst == "/var/lib/mysterium-node" for _src, dst, _opts in spec.volumes)


def test_adapter_rides_the_node_netns(provider, ctx):
    tunnel = ctx.container_name("x")
    adapter = provider.adapter_spec(ctx, tunnel, lease="x")
    assert adapter.network == f"container:{tunnel}"
    assert adapter.image == "kiwi-proxy/mysterium-adapter:latest"
    assert adapter.args == ["1080"]
    assert "--privileged" not in podman.spec_args(adapter)


def test_container_names(ctx):
    assert ctx.container_name() == paths.provider_container_name("mysterium")
    assert ctx.adapter_name("x") == paths.provider_container_name("mysterium-adapter", "x")


def test_tunnel_runs_as_container_root_for_the_tun_device(provider, ctx):
    assert provider.tunnel_spec(ctx, lease="0xp").userns is None


# ------------------------------------------------------------------ identity
KEYSTORE = (
    '{"address":"88994eb3a02f606bd134cca0cabac6e1ba51af55","crypto":{"cipher":"aes-128-ctr"},'
    '"id":"x","version":3}'
)


def test_settings_ask_for_the_keystore_and_a_passphrase(provider, ctx):
    assert [(s.key, s.kind, s.required) for s in provider.settings(ctx)] == [
        ("keystore", "file", True),
        ("passphrase", "password", False),
    ]
    assert provider.configured(ctx) is None


def test_configure_copies_the_keystore_and_keeps_the_passphrase_as_a_secret(
    provider, ctx, tmp_path, monkeypatch
):
    stored = {}
    monkeypatch.setattr(type(ctx), "set_secret", lambda self, k, v: stored.__setitem__(k, v))
    wallet = tmp_path / "0x8899.json"
    wallet.write_text(KEYSTORE)
    lines = provider.configure(ctx, {"keystore": str(wallet), "passphrase": "s3cret"})
    assert lines[0] == "identity 0x88994eb3a02f606bd134cca0cabac6e1ba51af55 imported"
    data = ctx.state_dir / "myst"
    assert (data / "kf-consumer-id").read_text() == "0x88994eb3a02f606bd134cca0cabac6e1ba51af55\n"
    copies = list((data / "keystore").iterdir())
    assert len(copies) == 1 and copies[0].name.endswith("88994eb3a02f606bd134cca0cabac6e1ba51af55")
    assert copies[0].read_text() == KEYSTORE and oct(copies[0].stat().st_mode)[-3:] == "600"
    assert stored == {"passphrase": "s3cret\n"}, "a newline, so an empty one is still a secret"
    assert provider.configured(ctx) == "identity 0x88994eb3a02f606bd134cca0cabac6e1ba51af55"
    # Again: one copy of the identity, not two.
    provider.configure(ctx, {"keystore": str(wallet)})
    assert len(list((data / "keystore").iterdir())) == 1
    assert stored["passphrase"] == "\n"


def test_configure_refuses_what_is_not_a_keystore(provider, ctx, tmp_path, monkeypatch):
    from kiwi_proxy.providers.base import ProviderError

    monkeypatch.setattr(type(ctx), "set_secret", lambda self, k, v: None)
    bad = tmp_path / "bad.json"
    bad.write_text('{"address": "nope"}')
    with pytest.raises(ProviderError, match="no identity address"):
        provider.configure(ctx, {"keystore": str(bad)})
    with pytest.raises(ProviderError, match="cannot read"):
        provider.configure(ctx, {"keystore": str(tmp_path / "absent.json")})
    with pytest.raises(ProviderError, match="keystore file is needed"):
        provider.configure(ctx, {})


def test_the_node_gets_the_passphrase_secret_when_it_exists(provider, ctx, monkeypatch):
    from kiwi_proxy import podman

    monkeypatch.setattr(podman, "secret_exists", lambda name: False)
    assert provider.tunnel_spec(ctx, lease="0xp").secrets == []
    monkeypatch.setattr(podman, "secret_exists", lambda name: name == ctx.secret_name("passphrase"))
    spec = provider.tunnel_spec(ctx, lease="0xp", country="de")
    assert spec.secrets == [(ctx.secret_name("passphrase"), "/run/secrets/kf-myst-passphrase")]
    assert spec.env["KF_MYST_COUNTRY"] == "DE"


# ----------------------------------------------------------------- providers
PROPOSALS = {
    "proposals": [
        {"provider_id": "0xlow", "location": {"country": "DE", "city": "Bonn", "ip_type": "hosting", "isp": "A"}, "quality": {"quality": 1.2, "latency": 50}},
        {"provider_id": "0xbest", "location": {"country": "DE", "city": "Berlin", "ip_type": "residential", "isp": "Telekom"}, "quality": {"quality": 2.9, "latency": 20}},
        {"provider_id": "", "location": {}, "quality": {}},
    ]
}  # fmt: skip


def test_leases_come_from_discovery_best_first_for_a_country(module, provider, ctx, monkeypatch):
    seen = []

    def fetch(url, timeout=20):
        seen.append(url)
        return PROPOSALS

    monkeypatch.setattr(module, "_fetch_json", fetch)
    leases = provider.leases(ctx, country="de")
    assert seen == [
        "https://discovery.mysterium.network/api/v4/proposals?service_type=wireguard&location_country=DE"
    ]
    assert [lease.id for lease in leases] == ["0xbest", "0xlow"]
    best = leases[0]
    assert (best.label, best.country, best.city) == ("Berlin · residential", "DE", "Berlin")
    assert best.detail == "quality 2.9 · 20 ms"
    assert (best.kind, best.quality, best.isp) == ("residential", 2.9, "Telekom")
    assert best.short_id == "0xbest" and best.matches("telekom") and not best.matches("bonn")
    provider.leases(ctx)
    assert seen[-1].endswith("proposals?service_type=wireguard")


def test_countries_are_discoverys_counts(module, provider, ctx, monkeypatch):
    from kiwi_proxy.providers.base import Country

    asked = []

    def fetch(url, timeout=20):
        asked.append(url)
        return {"DE": 785, "nl": 120, "XX": 0}

    monkeypatch.setattr(module, "_fetch_json", fetch)
    found = provider.countries(ctx)
    assert all(isinstance(c, Country) for c in found)
    assert [(c.code, c.name, c.count) for c in found] == [
        ("DE", "Germany", 785),
        ("NL", "Netherlands", 120),
    ]
    assert asked == [f"{module.DISCOVERY}/countries?service_type=wireguard"]


def test_countries_need_discovery(module, provider, ctx, monkeypatch):
    from kiwi_proxy.providers.base import ProviderError

    def down(url, timeout=20):
        raise OSError("no route")

    monkeypatch.setattr(module, "_fetch_json", down)
    with pytest.raises(ProviderError, match="discovery service not reachable"):
        provider.countries(ctx)


def test_leases_are_every_provider_best_first(module, provider, ctx, monkeypatch):
    # Not a best-sixty: the top of a country by quality is all hosting, and the
    # picker is what narrows — by IP type, by a piece of an id, by city or ISP.
    many = {
        "proposals": [
            {
                "provider_id": f"0x{i}",
                "quality": {"quality": i % 7},
                "location": {"ip_type": "residential" if i % 2 else "hosting"},
            }
            for i in range(700)
        ]
    }
    monkeypatch.setattr(module, "_fetch_json", lambda url, timeout=20: many)
    leases = provider.leases(ctx)
    assert len(leases) == 700 and all(int(lease.id[2:]) % 7 == 6 for lease in leases[:100])
    assert {lease.kind for lease in leases} == {"residential", "hosting"}


def test_discovery_being_away_is_a_provider_error(module, provider, ctx, monkeypatch):
    from kiwi_proxy.providers.base import ProviderError

    def down(url, timeout=20):
        raise OSError("no route")

    monkeypatch.setattr(module, "_fetch_json", down)
    with pytest.raises(ProviderError, match="discovery service not reachable"):
        provider.leases(ctx, country="de")


# ---------------------------------------------------------- one node at a time
def _running_tunnel(ctx, lease):
    return {
        "Names": [ctx.container_name(lease)],
        "State": "running",
        "Labels": {"kiwi-proxy.module": "mysterium", "kiwi-proxy.role": "tunnel", "kiwi-proxy.lease": lease},
    }  # fmt: skip


def test_up_refuses_a_second_provider_while_an_exit_uses_the_first(provider, ctx, monkeypatch):
    from kiwi_proxy import podman
    from kiwi_proxy.providers.base import ProviderError, TunnelProvider

    gateway = {"Names": ["kf-gw-1"], "State": "running",
               "Labels": {"kiwi-proxy.role": "gateway", "kiwi-proxy.module": "mysterium", "kiwi-proxy.lease": "0xa"}}  # fmt: skip

    def ps(all_=False, labels=None):
        labels = labels or {}
        if labels.get("kiwi-proxy.role") == "tunnel":
            return [_running_tunnel(ctx, "0xa")]
        if labels.get("kiwi-proxy.role") == "gateway":
            return [gateway]
        return []

    monkeypatch.setattr(podman, "ps", ps)
    monkeypatch.setattr(
        TunnelProvider, "up", lambda self, ctx, *, lease=None, country=None: ("up", lease)
    )
    with pytest.raises(ProviderError, match="one provider at a time"):
        provider.up(ctx, lease="0xb")
    assert provider.up(ctx, lease="0xa") == ("up", "0xa"), "the same provider is simply reused"


def test_up_replaces_an_idle_node_on_another_provider(provider, ctx, monkeypatch):
    from kiwi_proxy import podman
    from kiwi_proxy.providers.base import TunnelProvider

    downed = []

    def ps(all_=False, labels=None):
        if (labels or {}).get("kiwi-proxy.role") == "tunnel":
            return [_running_tunnel(ctx, "0xa")]
        return []  # no gateway exits through it any more

    monkeypatch.setattr(podman, "ps", ps)
    monkeypatch.setattr(
        type(provider), "down", lambda self, ctx, *, lease=None: downed.append(lease)
    )
    monkeypatch.setattr(
        TunnelProvider, "up", lambda self, ctx, *, lease=None, country=None: ("up", lease)
    )
    assert provider.up(ctx, lease="0xb") == ("up", "0xb")
    assert downed == ["0xa"]


def test_ready_reads_the_entrypoints_lines(provider, ctx, monkeypatch):
    from kiwi_proxy import podman
    from kiwi_proxy.providers.base import ProviderError

    monkeypatch.setattr(podman, "logs", lambda name, tail=200: "kf-myst: connecting to 0xa\n")
    assert provider.tunnel_ready(ctx, "c") is False
    monkeypatch.setattr(
        podman, "logs", lambda name, tail=200: "kf-myst: connected to 0xa\nkf-myst: connected\n"
    )
    assert provider.tunnel_ready(ctx, "c") is True
    monkeypatch.setattr(
        podman, "logs", lambda name, tail=200: "kf-myst: FATAL: could not unlock 0x1 (HTTP 403)\n"
    )
    with pytest.raises(ProviderError, match="could not unlock"):
        provider.tunnel_ready(ctx, "c")


def test_the_node_image_runs_as_root_with_a_sudo_stand_in():
    """The node calls `sudo ip …` even as root; sudo cannot work with the caps
    dropped and no-new-privileges, so the image replaces it with a runner."""
    import os
    import subprocess
    from pathlib import Path

    tunnel = Path(__file__).resolve().parents[2] / "module" / "containers" / "tunnel"
    containerfile = (tunnel / "Containerfile").read_text()
    assert "USER root" in containerfile and "COPY sudo /usr/bin/sudo" in containerfile
    shim = tunnel / "sudo"
    assert os.access(shim, os.X_OK)
    out = subprocess.run(
        ["sh", str(shim), "-n", "-u", "root", "--", "echo", "ran"], capture_output=True, text=True
    )
    assert out.stdout.strip() == "ran" and out.returncode == 0


def test_the_entrypoint_drives_the_node_the_way_the_tests_assume():
    """The script is the integration point; these are the calls the rest relies on."""
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[2] / "module" / "containers" / "tunnel" / "entrypoint.sh"
    ).read_text()
    for needle in (
        "--script-dir=/etc/mysterium-node",  # update-resolv-conf lives there
        "--wireguard.mtu=",  # 1280 by default: the tunnel may ride inside another tunnel
        "--tequilapi.address=127.0.0.1",
        "--tequilapi.port=4050",
        "/identities/$CONSUMER/unlock",
        "/identities/$CONSUMER/balance/refresh",
        "api PUT /connection",
        "api GET /connection",
        'DELETE "$API/connection"',  # on TERM
        "killswitch_on",  # the routing kill switch: blackhole 0/1 + 128/1
        "blackhole 0.0.0.0/1",
        "blackhole 128.0.0.0/1",
        "ipinfo.io",  # a neutral echo proves traffic through the tunnel (not an allowlisted host)
        'log "connected"',
    ):
        assert needle in script, needle


def test_ready_is_the_final_line_whole_not_a_prefix(provider, ctx, monkeypatch):
    """ "kf-myst: connected to 0x… — …" comes before traffic is proven; a readiness
    that matched it as a substring started gateways into a tunnel still dead."""
    early = "kf-myst: connecting to 0xa\nkf-myst: session with 0xa established — waiting for traffic to flow\n"
    monkeypatch.setattr(podman, "logs", lambda name, tail=200: early)
    assert provider.tunnel_ready(ctx, "c") is False
    monkeypatch.setattr(podman, "logs", lambda name, tail=200: "kf-myst: connected to 0xa — x\n")
    assert provider.tunnel_ready(ctx, "c") is False
    monkeypatch.setattr(
        podman,
        "logs",
        lambda name, tail=200: (
            early + "kf-myst: traffic flows: exit 1.2.3.4 DE\nkf-myst: connected\n"
        ),
    )
    assert provider.tunnel_ready(ctx, "c") is True
