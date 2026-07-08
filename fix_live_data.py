"""
One-time migration script — fixes the LIVE db.json on your Render server
via the admin API, so you don't have to click through the admin panel by hand.

Run this from Termux:
    export ADMIN_TOKEN="your ADMIN_PANEL_TOKEN value from Render env vars"
    python3 fix_live_data.py

What it does:
  1. Sets fazercards_category_id on Free Fire, Mobile Legends, PUBG Mobile
     (the real values confirmed live against the FazerCards API on 2026-07-08).
  2. Deletes the Roblox game and all its products (FazerCards doesn't sell
     Roblox top-ups anyway, and you asked to remove it).
  3. Replaces the placeholder Free Fire products (FF_25_DIAMOND, $0.00 etc.)
     with the REAL FazerCards catalog (25_diamonds, 520_diamonds, ... with
     real wholesale prices x1.15 markup — adjust MARKUP below if you want a
     different margin).

Mobile Legends and PUBG Mobile products are NOT touched here — their real
FazerCards offer_id catalogs haven't been fetched yet. Ask Claude to fetch
those next (same pattern as Free Fire) and this script can be extended.
"""

import os
import sys
import json
import urllib.request
import urllib.error

BASE_URL = os.environ.get("PVH_BASE_URL", "https://pvhtopup.onrender.com")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

if not ADMIN_TOKEN:
    print("ERROR: set ADMIN_TOKEN env var first, e.g.")
    print('  export ADMIN_TOKEN="your-real-token"')
    sys.exit(1)

MARKUP = 1.15  # retail = wholesale * MARKUP — change this if you want a different margin

# Real Free Fire catalog confirmed live via
#   GET /topups/offers?category_id=free_fire_my_sg
# (offer_id, display name, wholesale price USD)
REAL_FF_OFFERS = [
    ("25_diamonds", "25 Diamonds", 0.22),
    ("100_diamonds", "100 Diamonds", 0.74),
    ("310_diamonds", "310 Diamonds", 2.60),
    ("520_diamonds", "520 Diamonds", 3.76),
    ("1060_diamonds", "1060 Diamonds", 7.40),
    ("2180_diamonds", "2180 Diamonds", 14.94),
    ("5600_diamonds", "5600 Diamonds", 36.98),
    ("11500_diamonds", "11500 Diamonds", 76.18),
    ("weekly_membership", "Weekly Membership", 1.47),
    ("monthly_membership", "Monthly Membership", 7.36),
]


def call(method, path, body=None, params=None):
    url = BASE_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("x-admin-token", ADMIN_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} on {method} {path}: {e.read().decode()[:300]}")
        return None


import urllib.parse

print("== 1. Reading current games/products ==")
games_resp = call("GET", "/api/admin-games")
products_resp = call("GET", "/api/admin-products")
if not games_resp or not products_resp:
    print("Could not read current data — check ADMIN_TOKEN and BASE_URL.")
    sys.exit(1)

games = {g["code"]: g for g in games_resp.get("games", [])}
products = products_resp.get("products", [])

print("== 2. Setting fazercards_category_id on Free Fire / ML / PUBG ==")
CATEGORY_MAP = {
    "ff": "free_fire_my_sg",
    "ml": "mobile_legends_global",
    "pubg": "pubg_mobile_auto",
}
for code, category_id in CATEGORY_MAP.items():
    g = games.get(code)
    if not g:
        print(f"  (skip: no game with code '{code}' found)")
        continue
    res = call("PUT", "/api/admin-games", {"id": g["id"], "fazercards_category_id": category_id})
    print(f"  {code} -> {category_id}: {'OK' if res and res.get('success') else 'FAILED'}")

print("== 3. Removing Roblox game + its products ==")
rbx = games.get("rbx")
if rbx:
    rbx_products = [p for p in products if p.get("game_code") == "rbx"]
    for p in rbx_products:
        res = call("DELETE", "/api/admin-products", params={"id": p["id"]})
        print(f"  deleted product '{p['name']}': {'OK' if res and res.get('success') else 'FAILED'}")
    res = call("DELETE", "/api/admin-games", params={"id": rbx["id"]})
    print(f"  deleted Roblox game: {'OK' if res and res.get('success') else 'FAILED'}")
else:
    print("  (no Roblox game found — already removed?)")

print("== 4. Replacing Free Fire products with real FazerCards catalog ==")
ff_products = [p for p in products if p.get("game_code") == "ff"]
for p in ff_products:
    res = call("DELETE", "/api/admin-products", params={"id": p["id"]})
    print(f"  removed old '{p['name']}': {'OK' if res and res.get('success') else 'FAILED'}")

for offer_id, name, wholesale in REAL_FF_OFFERS:
    retail = round(wholesale * MARKUP, 2)
    res = call("POST", "/api/admin-products", {
        "game_code": "ff",
        "name": name,
        "price": retail,
        "provider_package": offer_id,
        "image_url": "",
    })
    print(f"  added '{name}' (${retail}, offer_id={offer_id}): {'OK' if res and res.get('success') else 'FAILED'}")

print("\nDone. Reload the storefront — Free Fire should now show real packages/prices,")
print("and Check ID should return the real player name for a valid Free Fire ID.")
print("Mobile Legends / PUBG Mobile products are untouched — ask for those next.")
