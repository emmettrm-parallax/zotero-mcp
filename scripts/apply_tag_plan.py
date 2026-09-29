"""Add planned tags to Zotero items with ONE local-API write.

Usage:
    python3 apply_tag_plan.py PLAN.json [LOCAL_API_KEY]

PLAN.json maps an item key to a list of tag names. Existing tags stay.
A planned tag is added when it is missing. All items go in one POST, so a
single-use key ("Allow") is enough. Without a key argument the script reads
ZOTERO_LOCAL_API_KEY, then ~/.config/zotero-mcp/config.json.
"""
import json, os, sys, urllib.request

BASE = "http://localhost:23119/api"


def load_key(arg):
    if arg:
        return arg.strip()
    if os.environ.get("ZOTERO_LOCAL_API_KEY"):
        return os.environ["ZOTERO_LOCAL_API_KEY"].strip()
    cfg = os.path.expanduser("~/.config/zotero-mcp/config.json")
    if os.path.exists(cfg):
        local = json.load(open(cfg)).get("local_api") or {}
        if local.get("key"):
            return local["key"]
    sys.exit("No local API key. Run: zotero-mcp authorize-local  (click Always Allow)")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r), dict(r.headers)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    plan = json.load(open(sys.argv[1]))
    key = load_key(sys.argv[2] if len(sys.argv) > 2 else None)
    if len(plan) > 50:
        sys.exit("The local API accepts 50 items per write. Split the plan.")

    _, hdr = get("/users/0/items?limit=1")
    server_id = hdr.get("Zotero-Server-ID")
    batch = []
    for item_key, new_tags in plan.items():
        data, _ = get(f"/users/0/items/{item_key}")
        d = data["data"]
        have = {t["tag"] for t in d.get("tags", [])}
        merged = list(d.get("tags", [])) + [{"tag": t, "type": 0} for t in new_tags if t not in have]
        batch.append({"key": item_key, "version": d["version"], "tags": merged})

    req = urllib.request.Request(
        f"{BASE}/users/0/items", data=json.dumps(batch).encode(), method="POST",
        headers={"Content-Type": "application/json", "Zotero-API-Key": key, "Zotero-Server-ID": server_id},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")
    print("successful:", len(res.get("successful", {})), "unchanged:", len(res.get("unchanged", {})),
          "failed:", res.get("failed", {}))

    missing = {}
    for item_key, new_tags in plan.items():
        data, _ = get(f"/users/0/items/{item_key}")
        have = {t["tag"] for t in data["data"].get("tags", [])}
        m = [t for t in new_tags if t not in have]
        if m:
            missing[item_key] = m
    print("verify: all planned tags present" if not missing else f"verify: MISSING {missing}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
