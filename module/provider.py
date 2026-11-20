"""Mysterium provider: exit through the Mysterium dVPN.

A Mysterium node in consumer mode connects to a provider and routes over a
WireGuard tunnel, but does not expose SOCKS5, so this is a TunnelProvider: the
myst node runs on the providers bridge and a microsocks adapter runs in its
network namespace, exposing SOCKS5 that rides the tunnel and is reachable on the
bridge at the node's address.

Wallets (kiwi-proxy ≥ 0.27 shows them under Provider modules → Mysterium →
Wallets…): each is a Mysterium identity with its keystore, a passphrase kept as a
podman secret, and a node data directory of its own. One is imported (the keystore
JSON Mysterium exported, ``0x….json``) or made new; a new one gets its top-up
address (the identity's channel on Polygon) and registers itself once its first
top-up arrives, as Mysterium's own app does. Each exit picks a wallet (its
``wallet`` option; the first wallet when none is chosen) and runs a node of that
wallet: exits on different wallets run side by side, one connection per wallet.

An exit's ``--lease`` is a provider identity (``0x…``); ``leases`` lists them from
Mysterium's discovery service, best quality first, for one country when asked.
An exit with a country and no lease lets the node pick the best provider there
at connect time. One connection per wallet: a second exit on another provider
with the same wallet waits for the first to go down.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import re
import secrets as _random
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from kiwi_proxy.models import ContainerSpec, Lease, ProviderManifest
from kiwi_proxy.providers.base import ProviderError, TunnelProvider

try:  # kiwi-proxy ≥ 0.27 shows a module's accounts: the wallets
    from kiwi_proxy.providers.base import Account, AccountAction, HistoryItem
except ImportError:  # pragma: no cover - older kiwi-proxy shows none
    Account = AccountAction = HistoryItem = None  # type: ignore[assignment,misc]

try:  # kiwi-proxy labels images with their source, to rebuild stale ones
    from kiwi_proxy.providers.base import SOURCE_LABEL, source_hash
except ImportError:  # pragma: no cover
    SOURCE_LABEL, source_hash = None, None  # type: ignore[assignment]

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
# The wallets: their names, data directories and secrets, what was last read from
# their node, and what the module saw happen (top-ups, registrations).
WALLETS_FILE = "wallets.json"
# The first wallet keeps the module's original place (0.5 had one identity in
# myst/ with the secret "passphrase"), so setups and snapshots from then work.
FIRST_DIR = "myst"
WALLETS_DIR = "wallets"
REMOVED_DIR = "removed"
WEI = 10**18
TOPUP_NOTE = "Top-up address: send MYST on Polygon only"
_KEY = re.compile(r"^w([0-9a-f]{8})(?:-(.+))?$")
# A dropdown of hundreds of providers helps nobody: the best so many, by quality.
MAX_LEASES = 5000  # every provider in a country (hundreds); the picker filters and searches
_LEASE_HAS_KIND = "kind" in getattr(Lease, "model_fields", {})

MANIFEST = ProviderManifest(
    name="mysterium",
    title="Mysterium",
    description="exit through the Mysterium dVPN",
    version="0.6.2",
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
    Setting("name", "Name", help="What the app calls this wallet.", required=False),
]

NEW_WALLET_SETTINGS = [
    Setting("name", "Name", help="What the app calls this wallet.", required=False),
    Setting(
        "passphrase",
        "Passphrase",
        kind="password",
        help="Locks the wallet's key file. Kept as a podman secret; you need it to "
        "import the wallet anywhere else.",
        required=False,
    ),
]


def _fetch_json(url: str, timeout: float = 20) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


class MysteriumProvider(TunnelProvider):
    manifest = MANIFEST
    lease_noun = "node"  # a lease is a Mysterium node: its id is an 0x… address
    ready_timeout = 360.0  # entrypoint worst case: 4 providers × (connect ≤45s + traffic ≤25s)
    log_marker = "kf-myst:"  # kiwi-proxy quotes these lines, not the node's debug noise
    # Left out of kiwi-proxy's snapshots: the node's database, which the node writes
    # while it runs (a copy taken then could be torn) and rebuilds when it is gone.
    # The keystore, the identity and the passphrase are what travels.
    snapshot_exclude = (
        "myst/mainnet",
        "wallets/*/mainnet*",
        "wallets/new-*",
        "removed/*/mainnet*",
        ".locks",
    )

    # ------------------------------------------------------------------ wallets
    account_noun = "wallet"
    account_option = "wallet"

    def settings(self, ctx):
        """What importing a wallet asks: its keystore, passphrase and a name."""
        return list(SETTINGS)

    def _data_dir(self, ctx) -> Path:
        data = ctx.state_dir / FIRST_DIR
        data.mkdir(parents=True, exist_ok=True)
        return data

    def _registry(self, ctx) -> dict:
        path = ctx.state_dir / WALLETS_FILE
        try:
            reg = json.loads(path.read_text())
        except (OSError, ValueError):
            reg = {}
        reg.setdefault("wallets", [])
        reg.setdefault("cache", {})
        reg.setdefault("ledger", {})
        if not reg["wallets"]:
            # 0.5: one identity in myst/, its passphrase the secret "passphrase".
            first = ctx.state_dir / FIRST_DIR / ID_FILE
            if first.exists() and (identity := first.read_text().strip()):
                reg["wallets"].append(
                    {"id": identity.lower(), "name": "Wallet 1", "dir": FIRST_DIR,
                     "secret": PASSPHRASE_SECRET}
                )  # fmt: skip
        return reg

    def _save(self, ctx, reg: dict) -> None:
        path = ctx.state_dir / WALLETS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps(reg, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)

    def _wallets(self, ctx) -> list[dict]:
        return self._registry(ctx)["wallets"]

    def _wallet(self, ctx, ref: str | None) -> dict:
        """A wallet by its id, the start of it, or its name."""
        found = self._wallets(ctx)
        if not ref:
            if not found:
                raise ProviderError("mysterium: no wallet yet; import or create one")
            return found[0]
        low = ref.lower()
        for w in found:
            if w["id"] == low or w["name"] == ref:
                return w
        hits = [w for w in found if w["id"].removeprefix("0x").startswith(low.removeprefix("0x"))]
        if len(hits) == 1:
            return hits[0]
        raise ProviderError(
            f"mysterium: no wallet {ref!r}" if not hits else f"{ref!r} fits several wallets"
        )

    def _dir(self, ctx, wallet: dict) -> Path:
        return ctx.state_dir / wallet["dir"]

    def configure(self, ctx, values):
        """Import a wallet: copy its keystore into a data directory of its own and
        keep the passphrase as a podman secret. Importing one that is there
        already replaces its keystore and passphrase."""
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

        reg = self._registry(ctx)
        wallet = next((w for w in reg["wallets"] if w["id"] == identity), None)
        if wallet is None:
            first = not reg["wallets"]
            wallet = {
                "id": identity,
                "name": "",
                "dir": FIRST_DIR if first else f"{WALLETS_DIR}/{identity}",
                "secret": PASSPHRASE_SECRET if first else f"wallet-{address[:10]}",
            }
            reg["wallets"].append(wallet)
        wallet["name"] = (
            (values.get("name") or "").strip()
            or wallet["name"]
            or (f"Wallet {len(reg['wallets'])}")
        )
        home = self._dir(ctx, wallet)
        keystore = home / "keystore"
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
        (home / ID_FILE).write_text(identity + "\n")
        # Stored with a newline, so an empty passphrase is still a secret with
        # content; the entrypoint's $(cat …) drops it again.
        ctx.set_secret(wallet["secret"], values.get("passphrase", "") + "\n")
        self._save(ctx, reg)
        return [
            f"identity {identity} imported",
            f"wallet {wallet['name']}; its passphrase kept as a podman secret",
            "its balance shows under Wallets (and kiwi-proxy module accounts mysterium)",
        ]

    def configured(self, ctx):
        wallets = self._wallets(ctx)
        if not wallets:
            return None
        if len(wallets) == 1:
            # In full: the app offers to copy it.
            return f"identity {wallets[0]['id']}"
        return f"{len(wallets)} wallets: " + ", ".join(w["name"] for w in wallets)

    def exit_options(self, ctx):  # noqa: ARG002
        return [
            Setting(
                "wallet",
                "Wallet",
                kind="choice",
                help="Which Mysterium wallet pays for this exit; the first when none is chosen.",
                required=False,
            )
        ]

    def option_choices(self, ctx, key, country=None):  # noqa: ARG002
        if key != "wallet":
            return []
        reg = self._registry(ctx)
        return [
            Lease(id=w["id"], label=w["name"], detail=self._state(reg["cache"].get(w["id"])))
            for w in reg["wallets"]
        ]

    def account_of(self, ctx, options):
        """The wallet an exit with these options pays from: its own, else the first."""
        wanted = str((options or {}).get(self.account_option, "")).strip()
        try:
            return self._wallet(ctx, wanted or None)["id"]
        except ProviderError:
            return wanted.lower() or None

    def lease_key(self, ctx, *, lease=None, country=None, options=None):
        """Names the node: the wallet, then the provider (or the country), so
        exits on different wallets get nodes of their own."""
        wallet = self.account_of(ctx, options or {})
        if not wallet:
            return lease
        key = f"w{wallet.removeprefix('0x')[:8]}"
        if lease:
            return f"{key}-{lease}"
        return f"{key}-{country.lower()}" if country else key

    def _split(self, ctx, key: str | None) -> tuple[dict | None, str | None]:
        """A node's key → (its wallet, its provider): "w88994eb3-0xabc…"; a bare
        provider id (an exit from before wallets) is the first wallet's."""
        found = _KEY.match(key or "")
        wallets = self._wallets(ctx)
        if found:
            short, rest = found.group(1), found.group(2) or ""
            wallet = next((w for w in wallets if w["id"][2:10] == short), None)
            return wallet, rest if rest.startswith("0x") else None
        return (wallets[0] if wallets else None), key

    # ----------------------------------------------------------- wallet state
    @staticmethod
    def _float(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _state(self, info: dict | None) -> str:
        if not info:
            return "not checked yet"
        balance = info.get("balance")
        money = f"{balance} MYST" if balance is not None else "balance unknown"
        status = info.get("status") or "?"
        if info.get("registered") or status == "Registered":
            return money
        if status == "InProgress":
            return f"{money} · registering"
        if (self._float(balance) or 0) > 0:
            return f"{money} · not registered yet"
        return "waiting for its first top-up"

    def _account(self, ctx, reg: dict, wallet: dict):
        info = reg["cache"].get(wallet["id"]) or {}
        registered = bool(info.get("registered")) or info.get("status") == "Registered"
        fee = info.get("fee")
        actions = ()
        payable = info.get("free") or (self._float(info.get("balance")) or 0) > 0
        if info and not registered and info.get("status") != "InProgress" and payable:
            cost = "nothing (free)" if info.get("free") else f"{fee or 'a small fee in'} MYST"
            actions = (
                AccountAction(
                    "register",
                    "Register now",
                    confirm=f"Registering costs {cost} from this wallet, once. "
                    "Mysterium needs it before the wallet can pay for a connection.",
                ),
            )
        if not info:
            detail = "Not checked yet: refresh to ask its node."
        elif registered:
            detail = "Registered."
        else:
            fee_note = f" (fee {fee} MYST, once)" if fee else ""
            detail = f"Registers itself after the first top-up{fee_note}."
        if checked := info.get("checked"):
            detail += f" Checked {checked}."
        return Account(
            id=wallet["id"],
            name=wallet["name"],
            state=self._state(info),
            address=info.get("channel") or "",
            address_note=TOPUP_NOTE if info.get("channel") else "",
            detail=detail,
            actions=actions,
            watch=bool(info) and not registered,
        )

    def accounts(self, ctx, *, refresh=False, only=None):
        reg = self._registry(ctx)
        wanted = [w for w in reg["wallets"] if not only or w["id"] == str(only).lower()]
        if refresh:
            for wallet in wanted:
                self._refresh(ctx, reg, wallet)
            reg = self._registry(ctx)
        return [self._account(ctx, reg, w) for w in wanted]

    def _refresh(self, ctx, reg: dict, wallet: dict) -> dict:
        """Ask the wallet's node; note a top-up; register once it can pay for it,
        as Mysterium's app does after a top-up."""
        info = self._task(ctx, wallet, "info")
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        info["checked"] = now
        before = reg["cache"].get(wallet["id"]) or {}
        old, new = self._float(before.get("balance")), self._float(info.get("balance"))
        ledger = reg["ledger"].setdefault(wallet["id"], [])
        if old is not None and new is not None and new > old + 1e-9:
            ledger.append(
                {"when": now, "what": "Top-up arrived", "amount": f"+{new - old:.4f} MYST"}
            )
        registered = info.get("registered") or info.get("status") == "Registered"
        fee = self._float(info.get("fee"))
        can_pay = info.get("free") or (new is not None and new > 0 and (fee is None or new > fee))
        if not registered and info.get("status") != "InProgress" and can_pay:
            try:
                self._task(ctx, wallet, "register")
                info["status"] = "InProgress"
                ledger.append(
                    {"when": now, "what": "Registration started",
                     "amount": f"−{fee:.4f} MYST" if fee and not info.get("free") else ""}
                )  # fmt: skip
            except ProviderError as exc:
                ledger.append({"when": now, "what": f"Registration failed: {exc}", "amount": ""})
        if before.get("status") not in (None, "Registered") and info.get("status") == "Registered":
            ledger.append({"when": now, "what": "Registered", "amount": ""})
        reg["cache"][wallet["id"]] = info
        self._save(ctx, reg)
        return info

    def account_new_settings(self, ctx):  # noqa: ARG002
        return list(NEW_WALLET_SETTINGS)

    def account_new(self, ctx, values):
        """A new identity, made by a node in a data directory of its own."""
        reg = self._registry(ctx)
        name = (values.get("name") or "").strip() or f"Wallet {len(reg['wallets']) + 1}"
        token = _random.token_hex(4)
        work = ctx.state_dir / WALLETS_DIR / f"new-{token}"
        work.mkdir(parents=True, exist_ok=True)
        temp = {"id": f"new-{token}", "name": name, "dir": f"{WALLETS_DIR}/new-{token}",
                "secret": f"wallet-new-{token}"}  # fmt: skip
        ctx.set_secret(temp["secret"], values.get("passphrase", "") + "\n")
        try:
            info = self._task(ctx, temp, "create")
            identity = str(info.get("id") or "").lower()
            if not re.fullmatch(r"0x[0-9a-f]{40}", identity):
                raise ProviderError(f"mysterium: the node made no identity ({info})")
            wallet = {"id": identity, "name": name, "dir": f"{WALLETS_DIR}/{identity}",
                      "secret": f"wallet-{identity[2:12]}"}  # fmt: skip
            ctx.set_secret(wallet["secret"], values.get("passphrase", "") + "\n")
            work.rename(self._dir(ctx, wallet))
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        finally:
            _secret_rm(ctx.secret_name(temp["secret"]))
        info["checked"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        reg = self._registry(ctx)
        reg["wallets"].append(wallet)
        reg["cache"][identity] = info
        reg["ledger"].setdefault(identity, []).append(
            {"when": info["checked"], "what": "Wallet created", "amount": ""}
        )
        self._save(ctx, reg)
        lines = [f"wallet {name} created: {identity}"]
        if info.get("channel"):
            lines.append(f"top-up address (MYST on Polygon only): {info['channel']}")
        lines.append("it registers itself once the first top-up arrives; export it to keep a copy")
        return lines

    def account_history(self, ctx, account):
        wallet = self._wallet(ctx, account)
        reg = self._registry(ctx)
        items = [
            (
                e.get("when", ""),
                HistoryItem(e.get("when", ""), e.get("what", ""), e.get("amount", "")),
            )
            for e in reg["ledger"].get(wallet["id"], [])
        ]
        try:
            found = self._task(ctx, wallet, "history")
        except ProviderError as exc:
            found = {}
            items.append(("", HistoryItem("", f"Sessions not read: {exc}")))
        for o in found.get("orders") or []:
            # Mysterium's orders carry no date: listed after everything dated.
            status = str(o.get("status") or "?")
            myst = o.get("myst")
            what = f"Top-up order · {o.get('gateway') or '?'} · {status}"
            amount = f"+{myst} MYST" if myst and status in ("paid", "completed") else ""
            detail = f"{myst} MYST ordered" if myst and not amount else ""
            if o.get("paid") and o.get("currency"):
                detail = " · ".join(b for b in (detail, f"{o['paid']} {o['currency']}") if b)
            items.append(("", HistoryItem("", what, amount, detail)))
        for s in found.get("sessions") or []:
            when = _local_time(str(s.get("when") or ""))
            tokens = int(str(s.get("tokens") or "0") or 0) / WEI
            seconds = int(s.get("seconds") or 0)
            took = f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"
            mb = int(s.get("bytes") or 0) / 1e6
            provider = str(s.get("provider") or "")
            short = f"{provider[:6]}…{provider[-4:]}" if len(provider) > 12 else provider
            what = " · ".join(b for b in ("Session", s.get("country"), short) if b)
            items.append(
                (
                    when,
                    HistoryItem(when, what, f"−{tokens:.4f} MYST", f"{took} · {mb:.1f} MB"),
                )
            )
        items.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _when, item in items]

    def account_act(self, ctx, account, action):
        wallet = self._wallet(ctx, account)
        if action != "register":
            raise ProviderError(f"mysterium: no action {action!r}")
        self._task(ctx, wallet, "register")
        reg = self._registry(ctx)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        reg["cache"].setdefault(wallet["id"], {})["status"] = "InProgress"
        reg["ledger"].setdefault(wallet["id"], []).append(
            {"when": now, "what": "Registration started", "amount": ""}
        )
        self._save(ctx, reg)
        return [f"{wallet['name']}: registration started; it takes a few minutes"]

    def account_rename(self, ctx, account, new_name):
        reg = self._registry(ctx)
        wallet = next(w for w in reg["wallets"] if w["id"] == self._wallet(ctx, account)["id"])
        wallet["name"] = new_name.strip() or wallet["name"]
        self._save(ctx, reg)

    def _keystore_file(self, ctx, wallet: dict) -> Path:
        address = wallet["id"].removeprefix("0x")
        keystore = self._dir(ctx, wallet) / "keystore"
        found = sorted(
            p for p in keystore.glob("*") if p.is_file() and p.name.lower().endswith(address)
        )
        if not found:
            raise ProviderError(f"mysterium: no key file for {wallet['id']} in {keystore}")
        return found[-1]

    def account_export(self, ctx, account, dest):
        """The wallet's key file, as Mysterium's apps import it (0x….json)."""
        wallet = self._wallet(ctx, account)
        source = self._keystore_file(ctx, wallet)
        target = Path(dest).expanduser()
        if target.is_dir():
            target = target / f"{wallet['id']}.json"
        n = 2
        while target.exists():
            target = target.with_name(f"{wallet['id']}-{n}.json")
            n += 1
        shutil.copyfile(source, target)
        target.chmod(0o600)
        return target

    def account_remove(self, ctx, account):
        """Move the wallet's directory aside (removed/), never delete it: a key
        that is gone takes the wallet's coins with it. Its passphrase secret stays."""
        wallet = self._wallet(ctx, account)
        if self._node_of(ctx, wallet):
            raise ProviderError(
                f"mysterium: {wallet['name']} has a node running; bring its exit down"
            )
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = ctx.state_dir / REMOVED_DIR / f"{wallet['id']}-{stamp}"
        aside.parent.mkdir(parents=True, exist_ok=True)
        source = self._dir(ctx, wallet)
        if source.exists():
            source.rename(aside)
        reg = self._registry(ctx)
        reg["wallets"] = [w for w in reg["wallets"] if w["id"] != wallet["id"]]
        self._save(ctx, reg)
        return [f"{wallet['name']} removed; its key file is kept in {aside / 'keystore'}"]

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
        """Providers from the discovery service, best first — every one of them,
        with its IP type (residential, hosting, cellular, …), quality, latency,
        bandwidth and ISP, so a picker can filter and search; one country's when
        asked (hundreds per country)."""
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
            kind = (loc.get("ip_type") or "").replace("content_delivery_network", "cdn") or None
            quality = q.get("quality")
            bits = [f"quality {quality:.1f}" if isinstance(quality, (int, float)) else None]
            if isinstance(q.get("latency"), (int, float)):
                bits.append(f"{q['latency']:.0f} ms")
            if isinstance(q.get("bandwidth"), (int, float)):
                bits.append(f"{q['bandwidth']:.0f} Mbit/s")
            fields = dict(
                id=pid,
                label=" · ".join(b for b in (loc.get("city"), kind) if b) or None,
                country=loc.get("country"),
                city=loc.get("city"),
                detail=" · ".join(b for b in bits if b) or None,
            )
            if _LEASE_HAS_KIND:  # kiwi-proxy ≥ 0.5 filters and sorts a picker by these
                fields.update(
                    kind=kind,
                    quality=float(quality) if isinstance(quality, (int, float)) else None,
                    isp=loc.get("isp") or None,
                )
            out.append(Lease(**fields))
        return out

    # --------------------------------------------------------------------- node
    def tunnel_spec(self, ctx, *, lease=None, country=None):
        from kiwi_proxy import podman

        wallet, provider = self._split(ctx, lease)
        env = {}
        if provider:
            env["KF_MYST_PROVIDER"] = str(provider)
        if country:
            env["KF_MYST_COUNTRY"] = str(country).upper()
        data = self._dir(ctx, wallet) if wallet else self._data_dir(ctx)
        secrets = []
        secret = ctx.secret_name(wallet["secret"] if wallet else PASSPHRASE_SECRET)
        if getattr(podman, "secret_exists", lambda _n: False)(secret):
            secrets.append((secret, PASSPHRASE_TARGET))
        return ContainerSpec(
            name=ctx.container_name(lease),
            image=ctx.image,  # kiwi-proxy/mysterium:latest, built by setup()
            network=ctx.network,
            env=env,
            volumes=[(str(data), MYST_DATA, "rw,z")],  # the wallet's identity and database
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

    def _wallet_tag(self, ctx, key: str | None) -> str:
        """Which wallet a node key belongs to, for "one connection per wallet"
        ("" when there are no wallets: every node is the one identity's)."""
        wallet, _provider = self._split(ctx, key)
        return wallet["id"] if wallet else ""

    def _tunnels(self):
        from kiwi_proxy import paths, podman

        return podman.ps(labels={paths.MODULE_LABEL: self.name, paths.ROLE_LABEL: "tunnel"})

    def _node_of(self, ctx, wallet: dict) -> str | None:
        """The running node of this wallet, if one is up."""
        from kiwi_proxy import paths

        for c in self._tunnels():
            key = (c.get("Labels") or {}).get(paths.LEASE_LABEL)
            if c.get("State") == "running" and self._wallet_tag(ctx, key) == wallet["id"]:
                return (c.get("Names") or [""])[0]
        return None

    def up(self, ctx, *, lease=None, country=None, options=None):  # noqa: ARG002
        """One wallet, one connection — Mysterium cancels a second session of the
        same identity (seen 2026-11-20: a second node on the wallet got
        "Connection cancelled" from every provider until the first was gone).
        A node of the same wallet through another provider is reused only when
        nothing exits through it any more; other wallets run side by side."""
        from kiwi_proxy import paths

        self._ensure_image(ctx)
        wanted = ctx.container_name(lease)
        mine = self._wallet_tag(ctx, lease)
        self._await_task(ctx, mine)
        for c in self._tunnels():
            name = (c.get("Names") or [""])[0]
            if name == wanted or c.get("State") != "running":
                continue
            other = (c.get("Labels") or {}).get(paths.LEASE_LABEL)
            if self._wallet_tag(ctx, other) != mine:
                continue
            if self._gateways_through(other):
                raise ProviderError(
                    f"mysterium: this wallet is up through {other or 'its default provider'} "
                    "for another exit; one connection per wallet — bring that exit down, "
                    "or give this exit another wallet"
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

    # ---------------------------------------------------------------- tasks
    def _task_name(self, ctx, wallet_id: str) -> str:
        return ctx.container_name(f"task-{wallet_id.removeprefix('0x')[:12]}")

    def _await_task(self, ctx, wallet_id: str) -> None:
        """A wallet task holds the wallet's node database; a node started beside
        it could not open it. Wait for the task (they take seconds)."""
        from kiwi_proxy import podman

        if wallet_id and podman.is_running(name := self._task_name(ctx, wallet_id)):
            podman.wait(name, timeout=200)

    def _task(self, ctx, wallet: dict, task: str) -> dict:
        """Run task.sh for a wallet: in its node when one is up, else in a node
        started for that alone (no tunnel: it only talks to Mysterium and Polygon).
        One task per wallet at a time — the app asks for a wallet's balance and
        history together, the CLI may ask meanwhile, and two task nodes would
        share a container name and the wallet's database."""
        locks = ctx.state_dir / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        with (locks / f"{wallet['id']}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._task_locked(ctx, wallet, task)

    def _task_locked(self, ctx, wallet: dict, task: str) -> dict:
        from kiwi_proxy import podman

        running = self._node_of(ctx, wallet) if not wallet["id"].startswith("new-") else None
        if running:
            proc = podman.exec_(running, ["kf-myst-task", task], timeout=200)
            text = (proc.stdout or "") + (proc.stderr or "")
        else:
            self._ensure_image(ctx)
            secrets = []
            secret = ctx.secret_name(wallet["secret"])
            if getattr(podman, "secret_exists", lambda _n: False)(secret):
                secrets.append((secret, PASSPHRASE_TARGET))
            data = self._dir(ctx, wallet)
            data.mkdir(parents=True, exist_ok=True)
            name = self._task_name(ctx, wallet["id"])
            spec = ContainerSpec(
                name=name,
                image=ctx.image,
                network=ctx.network,
                env={"KF_MYST_TASK": task},
                volumes=[(str(data), MYST_DATA, "rw,z")],
                secrets=secrets,
                cap_drop=["all"],
                security_opt=["no-new-privileges"],
                userns=None,
                tmpfs=["/tmp"],
                labels={"kiwi-proxy.module": self.name, "kiwi-proxy.role": "task"},
            )
            from kiwi_proxy import paths

            podman.network_ensure(
                ctx.network, subnet=paths.PROVIDERS_SUBNET, gateway=paths.PROVIDERS_GATEWAY_IP
            )
            podman.rm(name)  # one a crash left behind
            try:
                podman.start(spec)
                podman.wait(name, timeout=240)
                text = podman.logs(name, tail=400)
            finally:
                podman.rm(name)
        return _result(text, task)

    def _ensure_image(self, ctx) -> None:
        """Build the node image when it is missing or older than the module (a
        node image from 0.5 has no wallet tasks)."""
        from kiwi_proxy import podman

        cf = ctx.containerfile("tunnel")
        if not cf.exists():
            return
        if SOURCE_LABEL and source_hash and podman.image_exists(ctx.image):
            proc = podman._run(
                [
                    "image",
                    "inspect",
                    "--format",
                    f'{{{{index .Labels "{SOURCE_LABEL}"}}}}',
                    ctx.image,
                ],
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip() == source_hash(cf.parent):
                return
        elif podman.image_exists(ctx.image):
            return
        try:
            self.setup(ctx)
        except Exception as exc:  # noqa: BLE001 - a build, a network
            raise ProviderError(
                f"mysterium: the node image could not be built ({exc}); "
                "`kiwi-proxy module setup mysterium`"
            ) from exc

    def setup(self, ctx):
        # Both images are built locally: the node (FROM mysteriumnetwork/myst, with a
        # consumer-connect entrypoint and the wallet tasks) and the microsocks adapter.
        from kiwi_proxy import podman

        labels = {"app": "kiwi-proxy", "kiwi-proxy.module": self.name}
        tunnel_cf = ctx.containerfile("tunnel")
        if not tunnel_cf.exists():
            raise ProviderError(f"{self.name}: no tunnel Containerfile at {tunnel_cf}")
        node_labels = dict(labels)
        if SOURCE_LABEL and source_hash:
            node_labels[SOURCE_LABEL] = source_hash(tunnel_cf.parent)
        podman.build(ctx.image, str(tunnel_cf), str(tunnel_cf.parent), labels=node_labels)

        adapter_cf = ctx.containerfile("adapter")
        if not adapter_cf.exists():
            raise ProviderError(f"{self.name}: no adapter Containerfile at {adapter_cf}")
        podman.build(ctx.adapter_image, str(adapter_cf), str(adapter_cf.parent), labels=labels)


def _result(text: str, task: str):
    """The "kf-myst-result: <json>" line of a wallet task; its error, or what the
    node said when it gave up, as a ProviderError."""
    found = None
    for line in text.splitlines():
        if line.startswith("kf-myst-result: "):
            found = line.removeprefix("kf-myst-result: ")
    if found is None:
        fatal = [ln for ln in text.splitlines() if "kf-myst: FATAL" in ln]
        why = fatal[-1].split("FATAL:", 1)[-1].strip() if fatal else "no answer from the node"
        raise ProviderError(f"mysterium: {task}: {why}")
    try:
        data = json.loads(found)
    except ValueError as exc:
        raise ProviderError(f"mysterium: {task}: unreadable answer {found[:120]!r}") from exc
    if isinstance(data, dict) and data.get("error"):
        raise ProviderError(f"mysterium: {data['error']}")
    return data


def _local_time(stamp: str) -> str:
    """The node's UTC time ("2026-11-20T06:24:09Z") in this computer's time."""
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:16].replace("T", " ")
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


def _secret_rm(name: str) -> None:
    from kiwi_proxy import podman

    with contextlib.suppress(Exception):  # tidying a temporary secret
        podman.secret_rm(name)


PROVIDER = MysteriumProvider()
