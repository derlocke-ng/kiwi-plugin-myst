"""Wallets: import, the registry, which node an exit gets, the wallet tasks'
answers, auto-registration after a top-up, export and remove — without a node."""

from __future__ import annotations

import json

import pytest

A = "88994eb3a02f606bd134cca0cabac6e1ba51af55"
B = "1234567890abcdef1234567890abcdef12345678"


def _keystore(tmp_path, address):
    path = tmp_path / f"{address[:6]}.json"
    path.write_text(
        json.dumps({"address": address, "crypto": {"cipher": "aes-128-ctr"}, "version": 3})
    )
    return path


@pytest.fixture
def secrets(ctx, monkeypatch):
    stored = {}
    monkeypatch.setattr(type(ctx), "set_secret", lambda self, k, v: stored.__setitem__(k, v))
    return stored


@pytest.fixture
def two(provider, ctx, tmp_path, secrets):
    provider.configure(ctx, {"keystore": str(_keystore(tmp_path, A)), "passphrase": "a"})
    provider.configure(ctx, {"keystore": str(_keystore(tmp_path, B)), "name": "Spare"})
    return provider


def test_the_first_wallet_keeps_the_old_place_the_next_get_their_own(two, ctx, secrets):
    wallets = two._wallets(ctx)
    assert [(w["id"], w["name"], w["dir"], w["secret"]) for w in wallets] == [
        (f"0x{A}", "Wallet 1", "myst", "passphrase"),
        (f"0x{B}", "Spare", f"wallets/0x{B}", f"wallet-{B[:10]}"),
    ]
    assert (ctx.state_dir / f"wallets/0x{B}/kf-consumer-id").read_text() == f"0x{B}\n"
    assert secrets == {"passphrase": "a\n", f"wallet-{B[:10]}": "\n"}
    assert two.configured(ctx) == "2 wallets: Wallet 1, Spare"


def test_a_setup_from_0_5_is_read_as_its_first_wallet(provider, ctx):
    (ctx.state_dir / "myst").mkdir()
    (ctx.state_dir / "myst" / "kf-consumer-id").write_text(f"0x{A}\n")
    assert provider._wallets(ctx) == [
        {"id": f"0x{A}", "name": "Wallet 1", "dir": "myst", "secret": "passphrase"}
    ]
    assert provider.configured(ctx) == f"identity 0x{A}"


def test_an_exit_picks_a_wallet_and_gets_a_node_of_that_wallet(two, ctx):
    assert two.account_of(ctx, {}) == f"0x{A}", "the first wallet when none is chosen"
    assert two.account_of(ctx, {"wallet": "Spare"}) == f"0x{B}"
    assert two.account_of(ctx, {"wallet": B[:6]}) == f"0x{B}"
    assert two.lease_key(ctx, lease="0xprov", options={}) == f"w{A[:8]}-0xprov"
    assert two.lease_key(ctx, country="DE", options={"wallet": f"0x{B}"}) == f"w{B[:8]}-de"
    assert two.lease_key(ctx, options={"wallet": f"0x{B}"}) == f"w{B[:8]}"

    spec = two.tunnel_spec(ctx, lease=f"w{B[:8]}-0xprov", country="nl")
    assert spec.volumes[0][0] == str(ctx.state_dir / f"wallets/0x{B}")
    assert spec.env == {"KF_MYST_PROVIDER": "0xprov", "KF_MYST_COUNTRY": "NL"}
    assert spec.name == ctx.container_name(f"w{B[:8]}-0xprov")
    # A bare provider id (an exit from before wallets) is the first wallet's.
    old = two.tunnel_spec(ctx, lease="0xprov")
    assert old.volumes[0][0] == str(ctx.state_dir / "myst")
    assert old.env == {"KF_MYST_PROVIDER": "0xprov"}


def test_one_connection_per_wallet_other_wallets_side_by_side(two, ctx, monkeypatch):
    from kiwi_proxy import podman
    from kiwi_proxy.providers.base import ProviderError, TunnelProvider

    up_a = f"w{A[:8]}-0xa"
    tunnel = {"Names": [ctx.container_name(up_a)], "State": "running",
              "Labels": {"kiwi-proxy.module": "mysterium", "kiwi-proxy.role": "tunnel", "kiwi-proxy.lease": up_a}}  # fmt: skip
    gateway = {"Names": ["kf-gw-1"], "State": "running",
               "Labels": {"kiwi-proxy.role": "gateway", "kiwi-proxy.module": "mysterium", "kiwi-proxy.lease": up_a}}  # fmt: skip

    def ps(all_=False, labels=None):
        role = (labels or {}).get("kiwi-proxy.role")
        return [tunnel] if role == "tunnel" else [gateway] if role == "gateway" else []

    monkeypatch.setattr(podman, "ps", ps)
    monkeypatch.setattr(podman, "is_running", lambda name: False)
    monkeypatch.setattr(type(two), "_ensure_image", lambda self, ctx: None)
    monkeypatch.setattr(
        TunnelProvider, "up", lambda self, ctx, *, lease=None, country=None: ("up", lease)
    )
    with pytest.raises(ProviderError, match="one connection per wallet"):
        two.up(ctx, lease=f"w{A[:8]}-0xb")
    assert two.up(ctx, lease=f"w{B[:8]}-0xb") == ("up", f"w{B[:8]}-0xb")


def test_a_task_answer_is_its_last_result_line(module):
    from kiwi_proxy.providers.base import ProviderError

    text = 'noise\nkf-myst-result: {"id": "0x1"}\nkf-myst-result: {"balance": "1.5"}\n'
    assert module._result(text, "info") == {"balance": "1.5"}
    with pytest.raises(ProviderError, match="wrong passphrase"):
        module._result('kf-myst-result: {"error": "could not unlock — wrong passphrase?"}', "info")
    with pytest.raises(ProviderError, match="info: the node's API did not come up"):
        module._result("kf-myst: FATAL: the node's API did not come up in 90s", "info")
    with pytest.raises(ProviderError, match="no answer"):
        module._result("", "info")


def test_a_top_up_is_noted_and_the_wallet_registers_itself(two, ctx, monkeypatch):
    calls = []
    answers = iter([
        {"id": f"0x{B}", "balance": "0", "status": "Unregistered", "registered": False,
         "channel": "0xchan", "fee": "0.1", "free": False},
        {"id": f"0x{B}", "balance": "1.0", "status": "Unregistered", "registered": False,
         "channel": "0xchan", "fee": "0.1", "free": False},
    ])  # fmt: skip

    def task(self, ctx, wallet, name):
        calls.append(name)
        return next(answers) if name == "info" else {"registering": True}

    monkeypatch.setattr(type(two), "_task", task)
    acc = two.accounts(ctx, refresh=True, only=f"0x{B}")[0]
    assert acc.state == "waiting for its first top-up"
    assert acc.address == "0xchan" and "Polygon" in acc.address_note
    assert acc.watch and not acc.actions
    assert calls == ["info"], "nothing to register with yet"

    acc = two.accounts(ctx, refresh=True, only=f"0x{B}")[0]
    assert calls == ["info", "info", "register"]
    assert acc.state == "1.0 MYST · registering"
    ledger = two._registry(ctx)["ledger"][f"0x{B}"]
    assert [e["what"] for e in ledger] == ["Top-up arrived", "Registration started"]
    assert ledger[0]["amount"] == "+1.0000 MYST"


def test_history_merges_what_was_seen_with_the_nodes_sessions(two, ctx, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    import time

    time.tzset()
    reg = two._registry(ctx)
    reg["ledger"][f"0x{A}"] = [
        {"when": "2026-11-20 05:00", "what": "Top-up arrived", "amount": "+1 MYST"}
    ]
    two._save(ctx, reg)
    monkeypatch.setattr(type(two), "_task", lambda self, ctx, w, name: {
        "sessions": [{"when": "2026-11-20T06:00:00Z", "provider": "0x0aae6773d197eb6a27034e0725f845805a06fbd2",
                      "country": "DE", "seconds": 600, "bytes": 5_000_000, "tokens": str(2 * 10**15)}],
        "orders": [{"status": "paid", "gateway": "coingate", "myst": "10"},
                   {"status": "failed", "gateway": "coingate", "myst": "20"}]})  # fmt: skip
    items = two.account_history(ctx, f"0x{A}")
    assert [(i.when, i.what, i.amount) for i in items] == [
        ("2026-11-20 06:00", "Session · DE · 0x0aae…fbd2", "−0.0020 MYST"),
        ("2026-11-20 05:00", "Top-up arrived", "+1 MYST"),
        ("", "Top-up order · coingate · paid", "+10 MYST"),
        ("", "Top-up order · coingate · failed", ""),
    ]
    assert items[-1].detail == "20 MYST ordered"
    assert items[0].detail == "10 min · 5.0 MB"


def test_export_and_remove_keep_the_key(two, ctx, tmp_path, monkeypatch):
    from kiwi_proxy.providers.base import ProviderError

    out = tmp_path / "out"
    out.mkdir()
    first = two.account_export(ctx, "Spare", str(out))
    again = two.account_export(ctx, "Spare", str(out))
    assert first.name == f"0x{B}.json" and again.name == f"0x{B}-2.json"
    assert json.loads(first.read_text())["address"] == B

    monkeypatch.setattr(type(two), "_node_of", lambda self, ctx, w: "kf-prov-x")
    with pytest.raises(ProviderError, match="node running"):
        two.account_remove(ctx, "Spare")
    monkeypatch.setattr(type(two), "_node_of", lambda self, ctx, w: None)
    lines = two.account_remove(ctx, "Spare")
    assert "kept in" in lines[0]
    assert [w["name"] for w in two._wallets(ctx)] == ["Wallet 1"]
    kept = list((ctx.state_dir / "removed").glob(f"0x{B}-*/keystore/*"))
    assert len(kept) == 1


def test_a_new_wallet_is_made_by_a_node_in_a_directory_of_its_own(two, ctx, secrets, monkeypatch):
    removed = []
    monkeypatch.setattr("kiwi_proxy.podman.secret_rm", lambda name: removed.append(name))

    def task(self, ctx, wallet, name):
        assert name == "create" and wallet["id"].startswith("new-")
        home = ctx.state_dir / wallet["dir"]
        assert home.is_dir()
        (home / "kf-consumer-id").write_text("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd\n")
        return {"id": "0xABCDEFabcdefabcdefabcdefabcdefabcdefabcd", "channel": "0xchan",
                "status": "Unregistered", "balance": "0", "fee": "0.1"}  # fmt: skip

    monkeypatch.setattr(type(two), "_task", task)
    lines = two.account_new(ctx, {"name": "Test 1", "passphrase": "pw"})
    new = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    assert lines[0] == f"wallet Test 1 created: {new}"
    assert "0xchan" in lines[1] and "Polygon" in lines[1]
    assert (ctx.state_dir / f"wallets/{new}/kf-consumer-id").exists()
    assert not list((ctx.state_dir / "wallets").glob("new-*"))
    assert secrets[f"wallet-{new[2:12]}"] == "pw\n"
    assert removed and removed[0].endswith(next(k for k in secrets if k.startswith("wallet-new-")))
    assert [w["name"] for w in two._wallets(ctx)] == ["Wallet 1", "Spare", "Test 1"]
