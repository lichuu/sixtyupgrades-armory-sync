#!/usr/bin/env python3
"""Keep Sixty Upgrades gear sets in sync with the WoW: Forever armory.

Reads each character's armory page (public, no login), converts gear, enchants,
random suffixes, talents and level into Sixty Upgrades' format, and writes them
to an existing gear set through the site's GraphQL API. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REGION = "us-west-2"
CLIENT_ID = "14a1p5tkpv3b5u9c4u7gv28p09"
USER_POOL = "us-west-2_LciI88NwD"
IDENTITY_POOL = "us-west-2:8257c7cf-9665-49d6-82ad-bdd3acc34b32"
API_HOST = "api.sixtyupgrades.com"
GAME = "forever"
USER_AGENT = "sixtyupgrades-armory-sync/1.0"

STATE_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sixtyupgrades-armory-sync"
TOKEN_FILE = STATE_DIR / "refresh_token"
ENCHANT_CACHE = STATE_DIR / "enchant_cache.json"

# Blizzard slot type -> Sixty Upgrades slot index (WoW inventory slot IDs)
SLOT_IDS = {
    "HEAD": 1, "NECK": 2, "SHOULDER": 3, "SHIRT": 4, "CHEST": 5, "WAIST": 6,
    "LEGS": 7, "FEET": 8, "WRIST": 9, "HANDS": 10, "FINGER_1": 11, "FINGER_2": 12,
    "TRINKET_1": 13, "TRINKET_2": 14, "BACK": 15, "MAIN_HAND": 16, "OFF_HAND": 17,
    "RANGED": 18, "TABARD": 19,
}
NUM_SLOTS = 20

VARIATION_FIELDS = ("skinColor face hair hairColor facialHair mustache jawFeatures eyeGlow markingsColor "
                    "facePaint facePaintColor noseRing beard eyeColor ears earrings")

FLICKR58 ="123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
TALENT_LETTERS = "abcdefghijklmnopqrstuvwxyz"


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- HTTP helpers

def http(url: str, body: bytes | None = None, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SyncError(f"{url} -> HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from None


def aws_json(host: str, target: str, payload: dict) -> dict:
    raw = http(f"https://{host}/", json.dumps(payload).encode(), {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": target,
    })
    return json.loads(raw)


# ---------------------------------------------------------------- auth

def load_refresh_token() -> str:
    tok = os.environ.get("SIXTY_REFRESH_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    raise SyncError(f"no refresh token: run `{sys.argv[0]} login` or set SIXTY_REFRESH_TOKEN")


def save_refresh_token(tok: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok.strip() + "\n")
    TOKEN_FILE.chmod(0o600)


def get_aws_credentials(refresh_token: str) -> dict:
    try:
        auth = aws_json(f"cognito-idp.{REGION}.amazonaws.com",
                        "AWSCognitoIdentityProviderService.InitiateAuth", {
                            "AuthFlow": "REFRESH_TOKEN_AUTH",
                            "ClientId": CLIENT_ID,
                            "AuthParameters": {"REFRESH_TOKEN": refresh_token},
                        })
    except SyncError as e:
        raise SyncError(f"login failed, the refresh token is probably expired; run `login` again ({e})") from None
    result = auth["AuthenticationResult"]
    if result.get("RefreshToken") and "SIXTY_REFRESH_TOKEN" not in os.environ:
        save_refresh_token(result["RefreshToken"])  # rotated token
    logins = {f"cognito-idp.{REGION}.amazonaws.com/{USER_POOL}": result["IdToken"]}
    ident = aws_json(f"cognito-identity.{REGION}.amazonaws.com", "AWSCognitoIdentityService.GetId",
                     {"IdentityPoolId": IDENTITY_POOL, "Logins": logins})
    creds = aws_json(f"cognito-identity.{REGION}.amazonaws.com",
                     "AWSCognitoIdentityService.GetCredentialsForIdentity",
                     {"IdentityId": ident["IdentityId"], "Logins": logins})
    return creds["Credentials"]


def sigv4_headers(creds: dict, host: str, path: str, body: bytes) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    service = "execute-api"
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-security-token": creds["SessionToken"],
    }
    signed = ";".join(sorted(headers))
    canonical = "\n".join([
        "POST", path, "",
        "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
        signed,
        hashlib.sha256(body).hexdigest(),
    ])
    scope = f"{date}/{REGION}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = h(h(h(h(("AWS4" + creds["SecretKey"]).encode(), date), REGION), service), "aws4_request")
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds['AccessKeyId']}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    del headers["host"]
    return headers


class Sixty:
    def __init__(self, creds: dict):
        self.creds = creds
        self.path = f"/{GAME}/graphql"

    def gql(self, query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        raw = http(f"https://{API_HOST}{self.path}", body, sigv4_headers(self.creds, API_HOST, self.path, body))
        data = json.loads(raw)
        if data.get("errors"):
            raise SyncError(f"GraphQL error: {data['errors']}")
        return data["data"]

    def get_set(self, set_id: str) -> dict:
        return self.gql("query($id: ID!) { getSet(id: $id) { id characterId name gameClass "
                        "itemIds enchantIds talents } }", {"id": set_id})["getSet"]

    def get_character(self, char_id: str) -> dict:
        return self.gql(
            "query($id: ID!) { getCharacter(id: $id) { id name level race gender gameClass faction realm "
            f"variations {{ {VARIATION_FIELDS} }} connectedSets {{ id region realm name points {{ pointId }} }} }} }}",
            {"id": char_id})["getCharacter"]

    def update_set(self, set_id: str, data: dict) -> dict:
        return self.gql("mutation($id: ID!, $input: SetUpdateInput!) { updateSet(id: $id, input: $input) "
                        "{ id itemIds enchantIds talents } }", {"id": set_id, "input": data})["updateSet"]

    def update_level(self, char: dict, level: int) -> None:
        # updateCharacter replaces the whole record, so send every field back unchanged (as the site does).
        data = {k: char[k] for k in ("name", "realm", "faction", "race", "gender", "gameClass")}
        data["level"] = level
        data["variations"] = char.get("variations")
        data["connectedSets"] = [
            {**cs, "points": [{"type": "preset", "pointId": p["pointId"]} for p in cs.get("points") or []]}
            for cs in char.get("connectedSets") or []
        ]
        self.gql("mutation($id: ID!, $input: CharacterInput!) { updateCharacter(id: $id, input: $input) { id level } }",
                 {"id": char["id"], "input": data})

    def talent_trees(self, game_class: str) -> list:
        q = "query($c: String!) { getTalentData(gameClass: $c) { trees { name talents { id name ranks { rank } } } } }"
        trees = self.gql(q, {"c": game_class})["getTalentData"]["trees"]
        for tree in trees:
            for t in tree["talents"]:
                t["maxRank"] = len(t.pop("ranks") or [])
        return trees

    def find_enchant(self, slot_id: int, name: str, game_class: str) -> str | None:
        q = ("query($q: ItemQueryInput!) { searchItems(from: 0, size: 25, query: $q) "
             "{ items { id name } } }")
        query = {"type": "enchant", "slotId": slot_id, "search": name, "gameClass": game_class, "phase": 1}
        items = self.gql(q, {"q": query})["searchItems"]["items"]
        for it in items:
            if it["name"].lower() == name.lower():
                return it["id"]
        return None


# ---------------------------------------------------------------- armory

def armory_url(name: str, region: str) -> str:
    if name.startswith("http"):
        return name
    slug = re.sub(r"\s+", "-", name.strip().lower())
    return f"https://worldofwarcraft.blizzard.com/en-us/{GAME}/{region}/armory/character/{slug}"


def fetch_armory(name: str, region: str) -> dict:
    html = http(armory_url(name, region)).decode()
    marker = "characterProfileInitialState = "
    i = html.find(marker)
    if i < 0:
        raise SyncError(f"armory page for {name!r} had no profile data (wrong name or region?)")
    state, _ = json.JSONDecoder().raw_decode(html, i + len(marker))
    return state["character"]


# ---------------------------------------------------------------- conversion

def short_to_uuid(short: str) -> str:
    n = 0
    for ch in short:
        n = n * 58 + FLICKR58.index(ch)
    h = f"{n:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def parse_set_url(url: str) -> str:
    m = re.search(r"/set/([A-Za-z0-9]+)", url)
    if not m:
        raise SyncError(f"not a Sixty Upgrades set URL: {url}")
    sid = m.group(1)
    return sid if "-" in sid else short_to_uuid(sid)


def convert_items(char: dict) -> tuple[list, list]:
    """Return (itemIds, pending enchants as (slot, name)) from armory gear."""
    item_ids: list = [None] * NUM_SLOTS
    enchants: list = []
    for piece in char.get("gear", {}).values():
        slot = SLOT_IDS.get((piece.get("slot") or {}).get("type"))
        if slot is None:
            continue
        item_id = str(piece["id"])
        bonus = piece.get("bonus_list") or []
        if bonus:
            item_id = f"{item_id}_{bonus[0]}"  # random suffix ("of the Owl" etc.)
        item_ids[slot] = item_id
        for ench in piece.get("enchantments") or []:
            if (ench.get("enchantment_slot") or {}).get("type") != "PERMANENT":
                continue
            src = (ench.get("source_item") or {}).get("name")
            enchants.append((slot, src or ench.get("display_string", "")))
    return item_ids, enchants


def encode_talents(trees: list, specs: list) -> str:
    """Encode armory talents in Sixty Upgrades' talent-string format."""
    ranks_by_tree = {s["name"]: {t["name"]: t["rank"] for t in s.get("talents", [])} for s in specs}
    out = ""
    for tree_idx, tree in enumerate(trees):
        picked = ranks_by_tree.get(tree["name"], {})
        if not picked:
            continue
        current_tree = None
        for idx, talent in enumerate(tree["talents"]):
            rank = picked.pop(talent["name"], 0)
            if not rank:
                continue
            t_idx, tr = (idx - 26, tree_idx + 6) if idx > 25 else (idx, tree_idx)
            if tr != current_tree:
                out += str(tr)
                current_tree = tr
            letter = TALENT_LETTERS[t_idx]
            if rank == talent["maxRank"]:
                out += letter.upper()
            elif rank <= 2:
                out += letter * rank
            else:
                out += f"{letter}{rank}"
        for missing in picked:
            print(f"  ! talent {missing!r} not found in {tree['name']} tree", file=sys.stderr)
    return out


def decode_talents(trees: list, s: str | None) -> dict:
    """Talent string -> {talent_id: rank}; mirrors the site's decoder. Order-insensitive."""
    ranks: dict = {}
    tree, offset, prev = None, 0, None
    for ch in s or "":
        if ch.isdigit():
            n = int(ch)
            if 3 <= n <= 5:  # "b3" = previous talent has 3 ranks
                if prev is not None:
                    ranks[prev] += n - 1
            elif n < len(trees) or n > 5:
                tree, offset = trees[n - 6 if n > 5 else n], 26 if n > 5 else 0
            continue
        if tree is None or ch.lower() not in TALENT_LETTERS:
            continue
        idx = TALENT_LETTERS.index(ch.lower()) + offset
        if idx >= len(tree["talents"]):
            continue
        talent = tree["talents"][idx]
        ranks[talent["id"]] = ranks.get(talent["id"], 0) + (talent["maxRank"] if ch.isupper() else 1)
        prev = talent["id"]
    return ranks


# ---------------------------------------------------------------- sync

def load_enchant_cache() -> dict:
    try:
        return json.loads(ENCHANT_CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def sync_one(api: Sixty, armory_name: str, set_url: str, region: str, dry_run: bool) -> None:
    char = fetch_armory(armory_name, region)
    set_id = parse_set_url(set_url)
    current = api.get_set(set_id)
    if current is None:
        raise SyncError(f"set {set_url} not found (is it on this account?)")
    sixty_char = api.get_character(current["characterId"])
    game_class = current["gameClass"]
    full_name = " ".join(filter(None, [char.get("name"), char.get("surname")]))
    print(f"{full_name} (lvl {char['level']} {char['race']['name']} {char['class']['name']}) "
          f"-> {sixty_char['name']} / {current['name']}")

    if char["class"]["slug"] != game_class:
        raise SyncError(f"class mismatch: armory {char['class']['slug']} vs set {game_class}")

    item_ids, pending_enchants = convert_items(char)

    cache = load_enchant_cache()
    enchant_ids: list = [None] * NUM_SLOTS
    for slot, name in pending_enchants:
        key = f"{slot}|{name}"
        if key not in cache:
            cache[key] = api.find_enchant(slot, name, game_class)
        if cache[key]:
            enchant_ids[slot] = cache[key]
        else:
            print(f"  ! couldn't match enchant {name!r} (slot {slot})", file=sys.stderr)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ENCHANT_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))

    trees = api.talent_trees(game_class)
    talents = encode_talents(trees, char.get("specs") or [])

    changes = {}
    for key, want in (("itemIds", item_ids), ("enchantIds", enchant_ids)):
        have = current.get(key) or []
        have = have + [None] * (NUM_SLOTS - len(have))
        if have != want:
            changes[key] = want
            describe_change(key, have, want)
    if decode_talents(trees, current.get("talents")) != decode_talents(trees, talents):
        changes["talents"] = talents
        describe_change("talents", current.get("talents"), talents)
    level_change = sixty_char["level"] != char["level"]
    if level_change:
        print(f"  level: {sixty_char['level']} -> {char['level']}")

    if not changes and not level_change:
        print("  up to date")
        return
    if dry_run:
        print("  (dry run, nothing written)")
        return
    if level_change:
        api.update_level(sixty_char, char["level"])
    if changes:
        result = api.update_set(set_id, changes)
        rejected = [i for i, (a, b) in enumerate(zip(item_ids, result["itemIds"] or [])) if a and not b]
        if rejected:
            print(f"  ! site rejected items in slots {rejected}", file=sys.stderr)
    print("  synced")


def describe_change(key: str, have, want) -> None:
    if key == "talents":
        print(f"  talents: {have!r} -> {want!r}")
        return
    for slot, (a, b) in enumerate(zip(have, want)):
        if a != b:
            print(f"  {key[:-3]} slot {slot}: {a} -> {b}")


# ---------------------------------------------------------------- CLI

LOGIN_HELP = """\
1. Log in at https://sixtyupgrades.com in your browser.
2. Open DevTools (F12) -> Console and run:

   copy(Object.entries(localStorage).find(([k]) => k.endsWith('.refreshToken'))[1])

   (that copies your refresh token to the clipboard)
3. Paste it below.
"""


def cmd_login(_args) -> None:
    print(LOGIN_HELP)
    tok = input("refresh token: ").strip()
    if not tok:
        raise SyncError("no token entered")
    get_aws_credentials(tok)  # validate before saving
    save_refresh_token(tok)
    print(f"saved to {TOKEN_FILE}")


def cmd_sync(args) -> None:
    if args.armory and args.set:
        targets = [{"armory": args.armory, "set": args.set}]
        region = args.region
    else:
        cfg = tomllib.loads(Path(args.config).read_text())
        targets = cfg.get("character", [])
        region = cfg.get("region", args.region)
        if not targets:
            raise SyncError(f"no [[character]] entries in {args.config}")
    api = Sixty(get_aws_credentials(load_refresh_token()))
    failed = 0
    for t in targets:
        try:
            sync_one(api, t["armory"], t["set"], t.get("region", region), args.dry_run)
        except SyncError as e:
            failed += 1
            print(f"  ERROR: {e}", file=sys.stderr)
    if failed:
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="save your Sixty Upgrades refresh token").set_defaults(fn=cmd_login)
    s = sub.add_parser("sync", help="sync characters from the armory")
    s.add_argument("-c", "--config", default="config.toml")
    s.add_argument("--armory", help="armory character name/slug or URL (instead of config)")
    s.add_argument("--set", help="Sixty Upgrades set URL (with --armory)")
    s.add_argument("--region", default="us")
    s.add_argument("-n", "--dry-run", action="store_true", help="show changes without writing")
    s.set_defaults(fn=cmd_sync)
    args = p.parse_args()
    try:
        args.fn(args)
    except SyncError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
