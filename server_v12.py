"""
PVH TOPUP — Python (Flask) backend

Self-contained server that stores data in a local db.json file (JSON storage,
no external DB — matches the style of your other bots).

What changed in this version
-----------------------------
Auto top-up is now wired to FazerCards (https://api.fzr.cards/api/v2) instead of
a generic placeholder provider:

  - Auto CHECK ID   -> POST /topups/validate-id   (used by /api/check-user)
  - Auto PAYMENT    -> CamRapidPay KHQR            (unchanged: create-payment / check-payment)
  - Auto TOP-UP     -> POST /topups/order          (placed automatically the moment
                                                      CamRapidPay confirms payment)
  - Delivery status -> GET  /orders/:orderId        (polled by /api/check-topup-status)

To enable this per game:
  1. In the admin panel (or via PUT /api/admin-games), set `fazercards_category_id`
     on the game to the FazerCards topup category (e.g. "cat_ff_1", "cat_mlbb_1").
     Find this value from GET https://api.fzr.cards/api/v2/topups (X-API-Key header).
  2. On each product, set `provider_package` to the matching FazerCards `offer_id`
     from GET /topups/offers?category_id=<that category>.
  3. Set the FAZERCARDS_API_KEY environment variable (from the reseller hub -> Profile -> API).

If a game has no `fazercards_category_id` configured, or FAZERCARDS_API_KEY is unset,
top-ups for that game simply fall back to "manual" delivery (an admin fulfils it by hand) —
nothing breaks, it just doesn't auto-deliver.

Endpoints (same paths/behavior as the original netlify/functions/*.js):
  POST /api/create-payment
  POST /api/check-payment
  POST /api/expire-payment
  GET  /api/get-home-data
  GET  /api/get-topup-data?id=<game_code>
  POST /api/check-user                 <- now does a real auto ID-check via FazerCards
  GET  /api/get-stats?type=notifications
  POST /api/check-topup-status
  GET  /api/get-site-settings (public)
  GET/PUT             /api/admin-settings      (admin, header x-admin-token)
  GET/POST/PUT/DELETE /api/admin-games         (admin)
  GET/POST/PUT/DELETE /api/admin-products      (admin)
  GET/POST/PUT/DELETE /api/admin-banners       (admin)
  GET/PATCH           /api/admin-transactions  (admin)

Serves:
  GET /       -> index_v5.html (single-file frontend)
  GET /admin  -> admin_v3.html

Run:
  pip install flask requests python-dotenv cryptography --break-system-packages
  python server_v12.py
"""

import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import threading
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

try:
    from dotenv import load_dotenv
    load_dotenv()  # reads .env in this folder and loads it into os.environ
except ImportError:
    pass  # falls back to real environment variables if python-dotenv isn't installed


# ---------------------------------------------------------------------------
# CryptoJS-compatible AES encryption
#
# The frontend bundle calls CryptoJS.AES.decrypt(payloadString, PASSPHRASE) on
# several endpoints (get-home-data, get-topup-data, get-stats). CryptoJS's
# passphrase-based AES uses OpenSSL's "Salted__" format: MD5-based
# EVP_BytesToKey key/iv derivation, AES-256-CBC, PKCS7 padding, base64 output.
# This must be applied server-side or the frontend silently fails to decrypt
# and the games/products/notifications never render.
# ---------------------------------------------------------------------------

FRONTEND_PAYLOAD_KEY = os.environ.get(
    "FRONTEND_PAYLOAD_KEY", "6Imhmam1ob2xienZ5a3l3c2hzbG9kIiwic"
)


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len=32, iv_len=16):
    dtot = b""
    d = b""
    while len(dtot) < key_len + iv_len:
        d = hashlib.md5(d + password + salt).digest()
        dtot += d
    return dtot[:key_len], dtot[key_len:key_len + iv_len]


def encrypt_payload(obj) -> str:
    """Encrypt a JSON-serializable object the way CryptoJS.AES.decrypt(str, passphrase) expects."""
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "The 'cryptography' package is required (pip install cryptography --break-system-packages)"
        )
    plaintext = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    salt = secrets.token_bytes(8)
    key, iv = _evp_bytes_to_key(FRONTEND_PAYLOAD_KEY.encode("utf-8"), salt)
    pad_len = 16 - (len(plaintext) % 16)
    plaintext += bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return base64.b64encode(b"Salted__" + salt + ciphertext).decode("utf-8")


# ---------------------------------------------------------------------------
# Config (edit these, or set as real environment variables before running)
# ---------------------------------------------------------------------------

CAMRAPID_API_KEY = os.environ.get("CAMRAPID_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_PANEL_TOKEN = os.environ.get("ADMIN_PANEL_TOKEN", "change-this-to-a-long-random-string")

CAMRAPID_CREATE_URL = "https://pay.camrapidpay.com/api/v1/khqr/create-payments"
CAMRAPID_CHECK_URL = "https://pay.camrapidpay.com/check-transaction-api"

# FazerCards reseller API (auto ID-check + auto top-up)
FAZERCARDS_API_KEY = os.environ.get("FAZERCARDS_API_KEY", "")
FAZERCARDS_BASE_URL = os.environ.get("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)  # point this at a Render persistent disk mount in production
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "db.json")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "db_default.json")
STATIC_DIR = BASE_DIR  # index_v5.html + admin_v4.html live alongside this file

UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB per upload
_db_lock = threading.Lock()

# ---------------------------------------------------------------------------
# DDoS / abuse protection
#
# This site sits behind Cloudflare (see cf-ray / server: cloudflare on live
# responses), which already absorbs volumetric/network-layer DDoS traffic.
# This layer protects against application-layer abuse: someone hammering
# endpoints that cost real money per call (FazerCards check-user/place-order,
# CamRapidPay create/check-payment) or that are cheap to spam but expensive
# to read (admin-* without a valid token still does a full db_read()).
#
# Cloudflare terminates the real client IP into the CF-Connecting-IP header;
# request.remote_addr would otherwise just be Render's internal proxy IP,
# which would make every visitor share one rate-limit bucket. Prefer that
# header when present, else fall back to X-Forwarded-For, else remote_addr.
# ---------------------------------------------------------------------------
try:
    from flask_limiter import Limiter
    _HAS_LIMITER = True
except ImportError:
    _HAS_LIMITER = False


def _client_ip():
    return (
        request.headers.get("CF-Connecting-IP")
        or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


if _HAS_LIMITER:
    limiter = Limiter(
        key_func=_client_ip,
        app=app,
        default_limits=["200 per hour", "40 per minute"],
        storage_uri="memory://",  # single-instance app; swap for Redis if you scale to >1 dyno
    )

    @limiter.request_filter
    def _exempt_cors_preflight():
        return request.method == "OPTIONS"

    # -----------------------------------------------------------------------
    # Escalating auto-ban for repeat offenders
    #
    # Plain rate limiting alone just makes an attacker retry slightly slower —
    # a determined script keeps knocking forever. Anyone who keeps tripping
    # the rate limit gets banned outright for a growing period (10 min → 1
    # hour → 24 hours), checked in before_request so a banned IP is rejected
    # before touching db_read(), FazerCards, or CamRapidPay — the whole point
    # is that repeat offenders cost us ~0 CPU/IO per request once banned.
    #
    # Caveat: this state lives in each gunicorn worker's own memory (not
    # shared across workers/dynos). Fine for a single Starter-plan instance;
    # move to Redis if you ever scale past one instance.
    # -----------------------------------------------------------------------
    import time as _time

    _ban_lock = threading.Lock()
    _ban_store = {}  # ip -> {"strikes": int, "banned_until": epoch, "last": epoch}
    _STRIKE_RESET_SECONDS = 3600  # a clean hour of good behavior forgives past strikes
    _BAN_DURATIONS = [600, 3600, 86400]  # 10 min, 1 hour, 24 hours (caps here)

    def _is_banned(ip):
        info = _ban_store.get(ip)
        return bool(info) and _time.time() < info.get("banned_until", 0)

    def _register_violation(ip):
        now = _time.time()
        with _ban_lock:
            info = _ban_store.setdefault(ip, {"strikes": 0, "banned_until": 0, "last": 0})
            if now - info["last"] > _STRIKE_RESET_SECONDS:
                info["strikes"] = 0
            info["strikes"] += 1
            info["last"] = now
            idx = min(info["strikes"] - 1, len(_BAN_DURATIONS) - 1)
            info["banned_until"] = now + _BAN_DURATIONS[idx]
            if len(_ban_store) > 5000:
                cutoff = now - _STRIKE_RESET_SECONDS
                for k in [k for k, v in _ban_store.items() if v["last"] < cutoff and v["banned_until"] < now]:
                    del _ban_store[k]

    @app.before_request
    def _reject_banned_ips():
        if request.method == "OPTIONS":
            return None
        if _is_banned(_client_ip()):
            return json_response({"success": False, "error": "Temporarily blocked due to repeated rate-limit violations"}, 429)
        return None
else:
    # Lets the app boot even before `pip install Flask-Limiter` — but log loudly,
    # since running without this in production means no abuse protection at all.
    print("!! Flask-Limiter not installed — rate limiting is DISABLED. Run: pip install Flask-Limiter")

    class _NoopLimiter:
        def limit(self, *a, **kw):
            def deco(fn):
                return fn
            return deco

        def exempt(self, fn):
            return fn

    limiter = _NoopLimiter()

    def _register_violation(ip):
        pass  # no rate limiting installed, so nothing to escalate


# ---------------------------------------------------------------------------
# Tiny JSON "database" (mirrors your usual db.json pattern)
# ---------------------------------------------------------------------------

def _seed_from_defaults(data):
    """Merge any games/products/banners from db_default.json into an EXISTING db.json
    that are missing (matched by 'code' for games/products, 'image_url' for banners).
    This lets you add catalog items by editing db_default.json and redeploying, even
    if db.json already exists on disk (e.g. from earlier admin-panel entries) — so a
    stale/pre-existing db.json never silently blocks your new baked-in entries."""
    try:
        with open(DEFAULT_DB_PATH, "r", encoding="utf-8") as f:
            defaults = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    changed = False

    existing_game_codes = {g.get("code") for g in data.get("games", [])}
    for g in defaults.get("games", []):
        if g.get("code") not in existing_game_codes:
            new_row = dict(g)
            new_row["id"] = next_id(data, "games")
            data["games"].append(new_row)
            existing_game_codes.add(g.get("code"))
            changed = True

    existing_products = {(p.get("game_code"), p.get("name")) for p in data.get("products", [])}
    for p in defaults.get("products", []):
        key = (p.get("game_code"), p.get("name"))
        if key not in existing_products:
            new_row = dict(p)
            new_row["id"] = next_id(data, "products")
            data["products"].append(new_row)
            existing_products.add(key)
            changed = True

    existing_banner_urls = {b.get("image_url") for b in data.get("banners", [])}
    for b in defaults.get("banners", []):
        if b.get("image_url") not in existing_banner_urls:
            new_row = dict(b)
            new_row["id"] = next_id(data, "banners")
            data["banners"].append(new_row)
            existing_banner_urls.add(b.get("image_url"))
            changed = True

    return changed


def _load_db():
    if not os.path.exists(DB_PATH):
        with open(DEFAULT_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _save_db(data)
        return data
    with open(DB_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if _seed_from_defaults(data):
        _save_db(data)
    return data


def _save_db(data):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def db_read():
    with _db_lock:
        return _load_db()


def db_write(mutate_fn):
    """mutate_fn(data) -> result; runs under lock, persists, returns result of mutate_fn"""
    with _db_lock:
        data = _load_db()
        result = mutate_fn(data)
        _save_db(data)
        return result


def next_id(data, table):
    nid = data["next_ids"].get(table, 1)
    data["next_ids"][table] = nid + 1
    return nid


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers (mirror _utils.js)
# ---------------------------------------------------------------------------

def json_response(payload, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def notify_admin(text):
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except requests.RequestException as e:
        print("Telegram notify failed:", e)


def find_by_id(rows, id_value, key="id"):
    for row in rows:
        if str(row.get(key)) == str(id_value):
            return row
    return None


def find_game(data, game_code):
    return next((g for g in data.get("games", []) if g.get("code") == game_code), None)


def require_admin():
    """Returns None if authorized, else a Flask response to short-circuit with."""
    if not ADMIN_PANEL_TOKEN:
        return json_response({"success": False, "error": "ADMIN_PANEL_TOKEN is not configured on the server"}, 500)
    provided = request.headers.get("x-admin-token") or request.headers.get("X-Admin-Token")
    if not provided:
        return json_response({"success": False, "error": "Missing admin token"}, 401)
    if not hmac.compare_digest(str(provided), str(ADMIN_PANEL_TOKEN)):
        return json_response({"success": False, "error": "Invalid admin token"}, 401)
    return None


# ---------------------------------------------------------------------------
# FazerCards integration (auto ID-check + auto top-up)
# https://reseller.fazercards.com/en/docs
# ---------------------------------------------------------------------------

class FazerCardsError(Exception):
    pass


def _fazercards_headers(idempotency_key=None):
    headers = {"X-API-Key": FAZERCARDS_API_KEY, "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


# category_id (order/offers space, e.g. "cat_pubgm_1") -> list of required field keys.
# Cached in memory per process since a category's required fields never change at runtime.
_fazercards_fields_cache = {}


def fazercards_get_fields(category_id):
    if category_id in _fazercards_fields_cache:
        return _fazercards_fields_cache[category_id]
    res = requests.get(
        f"{FAZERCARDS_BASE_URL}/topups/offers",
        params={"category_id": category_id},
        headers=_fazercards_headers(),
        timeout=15,
    )
    data = res.json()
    fields = [f["key"] for f in data.get("fields", []) if f.get("key")]
    if not fields:
        fields = ["player_id"]  # safe default — every topup game has at least this
    _fazercards_fields_cache[category_id] = fields
    return fields


def fazercards_build_fields(category_id, user_id, zone_id=None):
    """Maps our (user_id, zone_id) onto whatever field keys FazerCards expects for this
    category — e.g. Free Fire only needs player_id; MLBB-style games need a 2nd field too."""
    keys = fazercards_get_fields(category_id)
    values = [v for v in (user_id, zone_id) if v not in (None, "")]
    return {k: v for k, v in zip(keys, values)}


# ---------------------------------------------------------------------------
# IMPORTANT: GET /topups/validate-id uses a COMPLETELY DIFFERENT category_id
# namespace than GET /topups / /topups/offers. e.g. the order catalog might call
# PUBG Mobile "cat_pubgm_1", while the validate-id catalog calls the very same
# game "pubg_mobile". Passing the order-catalog category_id into
# POST /topups/validate-id is why every ID check used to come back "invalid" —
# it was querying a category_id that doesn't exist in that catalog at all.
# So validate-id needs its OWN lookup, cached separately, keyed by game name
# (matched case-insensitively against our own game's "name" field), and it
# carries its own "fields" list directly — no need to call /topups/offers for it.
# ---------------------------------------------------------------------------

_fazercards_validate_catalog_cache = None  # {normalized_name: {"category_id":..., "fields":[keys]}}


def _fazercards_load_validate_catalog():
    global _fazercards_validate_catalog_cache
    if _fazercards_validate_catalog_cache is not None:
        return _fazercards_validate_catalog_cache
    res = requests.get(
        f"{FAZERCARDS_BASE_URL}/topups/validate-id",
        headers=_fazercards_headers(),
        timeout=15,
    )
    data = res.json()
    catalog = {}
    for item in data.get("items", []):
        name = (item.get("name") or "").strip().lower()
        keys = [f["key"] for f in item.get("fields", []) if f.get("key")] or ["player_id"]
        if name:
            catalog[name] = {"category_id": item.get("category_id"), "fields": keys}
    _fazercards_validate_catalog_cache = catalog
    return catalog


def fazercards_resolve_validate_category(game_name, fallback_category_id=None):
    """Looks up the validate-id-specific category_id + fields for a game by name.
    Falls back to fallback_category_id (the order-catalog id) only if no name match
    is found, so games we haven't matched by name yet don't hard-fail — though that
    fallback will likely still return invalid, same as before, until the name lines up."""
    catalog = _fazercards_load_validate_catalog()
    key = (game_name or "").strip().lower()
    match = catalog.get(key)
    if match:
        return match["category_id"], match["fields"]
    # Loose fallback: try substring match (e.g. our "Free Fire" vs their "Free Fire MAX")
    for name, entry in catalog.items():
        if key and (key in name or name in key):
            return entry["category_id"], entry["fields"]
    return fallback_category_id, None


def fazercards_validate_id(game_name, user_id, zone_id=None, fallback_category_id=None):
    """Auto CHECK ID: POST /topups/validate-id -> {ok, valid, player_name, region?}
    Resolves the correct validate-id category_id by game name first (see note above),
    since it is NOT the same category_id used for placing orders."""
    if not FAZERCARDS_API_KEY:
        raise FazerCardsError("FAZERCARDS_API_KEY is not set")
    category_id, keys = fazercards_resolve_validate_category(game_name, fallback_category_id)
    if not category_id:
        raise FazerCardsError(f"No FazerCards validate-id category found for game '{game_name}'")
    if keys:
        values = [v for v in (user_id, zone_id) if v not in (None, "")]
        fields = {k: v for k, v in zip(keys, values)}
    else:
        # Fell back to the order-catalog category_id — best effort via /topups/offers fields.
        fields = fazercards_build_fields(category_id, user_id, zone_id)
    res = requests.post(
        f"{FAZERCARDS_BASE_URL}/topups/validate-id",
        json={"category_id": category_id, "fields": fields},
        headers=_fazercards_headers(),
        timeout=15,
    )
    try:
        return res.json()
    except ValueError:
        raise FazerCardsError(f"Bad response from FazerCards ({res.status_code})")


def fazercards_place_order(category_id, offer_id, user_id, zone_id, idempotency_key):
    """Auto TOP-UP: POST /topups/order -> {ok, order:{id, kind, status}}
    idempotency_key should be your own order/trx id — retrying with the same key
    returns the original order instead of charging or delivering twice."""
    if not FAZERCARDS_API_KEY:
        raise FazerCardsError("FAZERCARDS_API_KEY is not set")
    fields = fazercards_build_fields(category_id, user_id, zone_id)
    res = requests.post(
        f"{FAZERCARDS_BASE_URL}/topups/order",
        json={"category_id": category_id, "offer_id": offer_id, "fields": fields},
        headers=_fazercards_headers(idempotency_key),
        timeout=20,
    )
    try:
        data = res.json()
    except ValueError:
        raise FazerCardsError(f"Bad response from FazerCards ({res.status_code})")
    if not data.get("ok"):
        raise FazerCardsError(data.get("error") or f"FazerCards order failed ({res.status_code})")
    return data


def fazercards_giftcard_order(category_id, card_id, idempotency_key, quantity=1):
    """Auto-buy a gift-card CODE (e.g. Roblox): POST /giftcards/order -> {ok, order:{id, kind, status}}
    Unlike /topups/order, this never delivers into a player account directly — the
    provider hands back a redemption code that the customer must redeem themselves
    on the brand's own site. idempotency_key = our trx_id, same double-charge safety
    as fazercards_place_order."""
    if not FAZERCARDS_API_KEY:
        raise FazerCardsError("FAZERCARDS_API_KEY is not set")
    res = requests.post(
        f"{FAZERCARDS_BASE_URL}/giftcards/order",
        json={"category_id": category_id, "card_id": card_id, "quantity": quantity},
        headers=_fazercards_headers(idempotency_key),
        timeout=20,
    )
    try:
        data = res.json()
    except ValueError:
        raise FazerCardsError(f"Bad response from FazerCards ({res.status_code})")
    if not data.get("ok"):
        raise FazerCardsError(data.get("error") or f"FazerCards gift card order failed ({res.status_code})")
    return data


def extract_giftcard_codes(order_obj):
    """FazerCards' exact field name for the redemption code(s) isn't nailed down in the
    summarized docs (order shape 'varies by kind/status') — try the common candidates
    defensively so a field-name mismatch degrades to 'show the raw payload' instead of
    silently losing the code the customer already paid for."""
    for key in ("codes", "code", "pins", "pin", "serials", "serial"):
        val = order_obj.get(key)
        if val:
            return val if isinstance(val, list) else [val]
    payload = order_obj.get("payload")
    if isinstance(payload, dict):
        for key in ("codes", "code", "pins", "pin", "serials", "serial"):
            val = payload.get(key)
            if val:
                return val if isinstance(val, list) else [val]
    return None


def fazercards_get_order(order_id):
    """Poll delivery status: GET /orders/:orderId -> {ok, order:{id, kind, status, ...}}"""
    if not FAZERCARDS_API_KEY:
        raise FazerCardsError("FAZERCARDS_API_KEY is not set")
    res = requests.get(f"{FAZERCARDS_BASE_URL}/orders/{order_id}", headers=_fazercards_headers(), timeout=15)
    try:
        return res.json()
    except ValueError:
        raise FazerCardsError(f"Bad response from FazerCards ({res.status_code})")


# ---------------------------------------------------------------------------
# Static file serving — single-file frontend + admin panel
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory(STATIC_DIR, "manifest.json")


@app.route("/icon-192.png")
def serve_icon_192():
    return send_from_directory(STATIC_DIR, "icon-192.png")


@app.route("/icon-512.png")
def serve_icon_512():
    return send_from_directory(STATIC_DIR, "icon-512.png")


@app.route("/api/admin-upload", methods=["POST", "OPTIONS"])
@limiter.limit("20 per minute")
def admin_upload():
    """Accepts a real image file (multipart/form-data, field name 'file') from the admin
    panel and returns a URL under /uploads/... — this is what lets the admin panel offer
    an actual upload button instead of requiring you to paste an image link."""
    if request.method == "OPTIONS":
        return json_response({})
    auth_err = require_admin()
    if auth_err:
        return auth_err

    if "file" not in request.files:
        return json_response({"success": False, "error": "No file provided"}, 400)
    file = request.files["file"]
    if not file or file.filename == "":
        return json_response({"success": False, "error": "No file selected"}, 400)

    ext = secure_filename(file.filename).rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return json_response(
            {"success": False, "error": f"File type not allowed (use: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))})"},
            400,
        )

    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, filename))
    url = f"/uploads/{filename}"
    return json_response({"success": True, "url": url})


@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index_v5.html")


@app.route("/admin")
@app.route("/admin/")
def serve_admin():
    return send_from_directory(STATIC_DIR, "admin_v4.html")


# ---------------------------------------------------------------------------
# Public API — payment flow
# ---------------------------------------------------------------------------

@app.route("/api/create-payment", methods=["POST", "OPTIONS"])
@limiter.limit("6 per minute")
def create_payment():
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    user_id = body.get("userId")
    zone_id = body.get("zoneId")
    game_code = body.get("gameCode")
    product_id = body.get("productId")

    if not game_code or not product_id:
        return json_response({"success": False, "error": "Missing required fields"}, 400)

    # Trust nothing the client says about price. A shared "signing secret" can't
    # protect this anyway — anything shipped in the frontend bundle is visible to
    # anyone who opens dev tools. The real protection is to never accept a client-
    # supplied amount at all: look the product up ourselves and charge exactly
    # what's stored in the database for it.
    data = db_read()
    product = find_by_id(data["products"], product_id)
    if product is None or _norm_code(product.get("game_code")) != _norm_code(game_code):
        return json_response({"success": False, "error": "Product not found"}, 404)

    game = find_game(data, game_code)
    is_giftcard = (game or {}).get("fulfillment_type") == "giftcard"
    # Gift-card products (e.g. Roblox) deliver a redemption code, not an account
    # top-up — there's no player ID to collect. Everything else still requires one.
    if not is_giftcard and not user_id:
        return json_response({"success": False, "error": "Missing required fields"}, 400)

    try:
        amount = float(product.get("price") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return json_response({"success": False, "error": "Invalid product price"}, 400)

    trx_id = f"PVH{int(time.time() * 1000)}{secrets.randbelow(1000)}"
    reference = trx_id
    if is_giftcard and not user_id:
        user_id = f"giftcard-{trx_id}"  # placeholder so every transaction row still has an identifier

    if not CAMRAPID_API_KEY:
        return json_response({"success": False, "error": "CAMRAPID_API_KEY is not configured on the server"}, 500)

    try:
        res = requests.post(
            CAMRAPID_CREATE_URL,
            json={
                "api_key": CAMRAPID_API_KEY,
                "amount": round(float(amount) * 100) / 100,
                "reference": reference,
                "webhook_url": f"{request.host_url.rstrip('/')}/api/webhook/{reference}",
            },
            timeout=15,
        )
        data = res.json()
    except requests.RequestException as e:
        print("camrapid_create request failed:", e)
        return json_response({"success": False, "error": "Failed to generate QR"}, 500)

    if not data.get("success"):
        print("camrapid_create failed:", data)
        return json_response({"success": False, "error": "Failed to generate QR"}, 500)

    def _mutate(d):
        d["transactions"].append({
            "trx_id": trx_id,
            "reference": reference,
            "user_id": user_id,
            "zone_id": zone_id,
            "game_code": game_code,
            "product_id": product_id,
            "amount": float(amount),
            "status": "pending",
            "delivery_status": None,
            "delivery_error": None,
            "delivery_code": None,
            "provider_order_id": None,
            "created_at": now_iso(),
            "paid_at": None,
        })

    db_write(_mutate)
    return json_response({"success": True, "trx_id": trx_id, "qr_data": data.get("qr_code")})


@app.route("/api/check-payment", methods=["POST", "OPTIONS"])
@limiter.limit("15 per minute")
def check_payment():
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    trx_id = body.get("trx_id")
    if not trx_id:
        return json_response({"paid": False, "error": "Missing trx_id"}, 400)

    data = db_read()
    order = find_by_id(data["transactions"], trx_id, key="trx_id")
    if not order:
        return json_response({"paid": False, "error": "Order not found"}, 404)
    if order["status"] == "paid":
        return json_response({"paid": True, "data": order})
    if order["status"] == "expired":
        return json_response({"paid": False, "expired": True})

    if not CAMRAPID_API_KEY:
        return json_response({"paid": False, "error": "CAMRAPID_API_KEY is not configured on the server"}, 500)

    try:
        res = requests.get(
            CAMRAPID_CHECK_URL,
            params={"api_key": CAMRAPID_API_KEY, "reference": order["reference"]},
            timeout=15,
        )
        result = res.json()
    except requests.RequestException as e:
        print("camrapid_check request failed:", e)
        return json_response({"paid": False, "error": "Server error"}, 500)

    is_paid = bool(result.get("success")) and str(result.get("status", "")).lower() in ("success", "paid")
    if not is_paid:
        return json_response({"paid": False})

    # Mark paid + run auto top-up (FazerCards) + notify admin
    def _mutate(d):
        o = find_by_id(d["transactions"], trx_id, key="trx_id")
        o["status"] = "paid"
        o["paid_at"] = now_iso()

        product = find_by_id(d["products"], o["product_id"])
        game = find_game(d, o["game_code"])
        fc_category = (game or {}).get("fazercards_category_id")
        fc_offer_id = (product or {}).get("provider_package")  # FazerCards offer_id / card_id lives here
        is_giftcard = (game or {}).get("fulfillment_type") == "giftcard"

        delivery_status = "manual"
        delivery_error = None
        provider_order_id = None
        delivery_code = None

        if fc_offer_id and fc_category and FAZERCARDS_API_KEY:
            try:
                # trx_id doubles as the Idempotency-Key: safe to retry this exact
                # call later (e.g. from the admin dashboard) without double-delivering.
                if is_giftcard:
                    # Gift-card products (e.g. Roblox) deliver a redemption CODE, not
                    # an account top-up — there is no user_id/zone_id to send.
                    fc_res = fazercards_giftcard_order(fc_category, fc_offer_id, idempotency_key=o["trx_id"])
                else:
                    fc_res = fazercards_place_order(
                        fc_category, fc_offer_id, o["user_id"], o.get("zone_id"), idempotency_key=o["trx_id"]
                    )
                fc_order = fc_res.get("order", {})
                provider_order_id = fc_order.get("id")
                fc_status = str(fc_order.get("status", "")).lower()
                delivery_status = "delivered" if fc_status in ("completed", "delivered") else "processing"
                if is_giftcard and delivery_status == "delivered":
                    codes = extract_giftcard_codes(fc_order)
                    if codes:
                        delivery_code = ", ".join(str(c) for c in codes)
                    else:
                        # Order completed but we couldn't find the code under any of the
                        # expected keys — don't lose it silently, surface the raw payload
                        # so the admin can hand-deliver while we confirm the real field name.
                        delivery_error = f"Order completed but code field not recognized — raw: {json.dumps(fc_order)[:500]}"
            except FazerCardsError as e:
                delivery_status = "failed"
                delivery_error = str(e)
            except Exception as e:  # noqa: BLE001
                print("FazerCards order failed:", e)
                delivery_status = "failed"
                delivery_error = str(e)

        o["provider_order_id"] = provider_order_id
        o["delivery_status"] = delivery_status
        o["delivery_error"] = delivery_error
        o["delivery_code"] = delivery_code
        return o, delivery_status, provider_order_id, delivery_error

    order_after, delivery_status, provider_order_id, delivery_error = db_write(_mutate)

    if delivery_status == "processing":
        delivery_line = f"⏳ Auto top-up submitted to FazerCards (order {provider_order_id}) — awaiting confirmation"
    elif delivery_status == "delivered":
        code_part = f"\nCode: {order_after.get('delivery_code')}" if order_after.get("delivery_code") else ""
        delivery_line = f"💎 Auto top-up delivered instantly (order {provider_order_id}){code_part}"
    elif delivery_status == "failed":
        delivery_line = f"⚠️ *AUTO TOP-UP FAILED*: {delivery_error}\n👉 Please deliver manually"
    else:
        delivery_line = "👤 No FazerCards mapping for this product — please deliver manually"

    zone_part = f" ({order_after['zone_id']})" if order_after.get("zone_id") else ""
    notify_admin(
        "✅ *PAYMENT CONFIRMED (CamRapidPay)*\n"
        "--------------------------\n"
        f"🎮 Game: {order_after['game_code']}\n"
        f"🆔 User ID: {order_after['user_id']}{zone_part}\n"
        f"💎 Product: {order_after['product_id']}\n"
        f"💰 Amount: ${order_after['amount']}\n"
        f"🧾 Ref: {order_after['reference']}\n"
        "--------------------------\n"
        f"{delivery_line}"
    )

    return json_response({"paid": True, "data": {**order_after, "status": "paid", "delivery_status": delivery_status}})


@app.route("/api/expire-payment", methods=["POST", "OPTIONS"])
def expire_payment():
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    trx_id = body.get("trx_id")
    if not trx_id:
        return json_response({"success": False, "error": "Missing trx_id"}, 400)

    def _mutate(d):
        o = find_by_id(d["transactions"], trx_id, key="trx_id")
        if o and o["status"] != "paid":
            o["status"] = "expired"

    db_write(_mutate)
    return json_response({"success": True})


@app.route("/api/check-topup-status", methods=["POST", "OPTIONS"])
@limiter.limit("15 per minute")
def check_topup_status():
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    trx_id = body.get("trx_id")
    if not trx_id:
        return json_response({"error": "Missing trx_id"}, 400)

    data = db_read()
    order = find_by_id(data["transactions"], trx_id, key="trx_id")
    if not order:
        return json_response({"error": "Order not found"}, 404)

    if order.get("delivery_status") != "processing" or not order.get("provider_order_id"):
        return json_response({
            "delivery_status": order.get("delivery_status"),
            "delivery_error": order.get("delivery_error"),
        })

    try:
        result = fazercards_get_order(order["provider_order_id"])
    except FazerCardsError as e:
        print("check-topup-status error:", e)
        return json_response({"error": "Server error"}, 500)
    except Exception as e:  # noqa: BLE001
        print("check-topup-status error:", e)
        return json_response({"error": "Server error"}, 500)

    fc_order = result.get("order", {})
    provider_status = str(fc_order.get("status", "")).lower()
    new_status = order["delivery_status"]
    new_error = order.get("delivery_error")

    new_code = order.get("delivery_code")
    if provider_status in ("completed", "delivered", "success"):
        new_status = "delivered"
        if not new_code:
            codes = extract_giftcard_codes(fc_order)
            if codes:
                new_code = ", ".join(str(c) for c in codes)
    elif provider_status in ("failed", "error", "cancelled", "canceled"):
        new_status = "failed"
        new_error = fc_order.get("error") or "FazerCards reported failure"

    if new_status != order["delivery_status"] or new_code != order.get("delivery_code"):
        def _mutate(d):
            o = find_by_id(d["transactions"], trx_id, key="trx_id")
            o["delivery_status"] = new_status
            o["delivery_error"] = new_error
            o["delivery_code"] = new_code

        db_write(_mutate)

        zone_part = f" ({order['zone_id']})" if order.get("zone_id") else ""
        if new_status == "delivered":
            code_part = f"\nCode: {new_code}" if new_code else ""
            notify_admin(
                f"💎 *AUTO TOP-UP DELIVERED*\n{order['game_code']} / {order['user_id']}{zone_part}\nRef: {order['reference']}{code_part}"
            )
        elif new_status == "failed":
            notify_admin(
                f"⚠️ *AUTO TOP-UP FAILED* after processing\n{order['game_code']} / {order['user_id']}\n"
                f"Ref: {order['reference']}\nReason: {new_error}\n👉 Please deliver manually"
            )

    return json_response({"delivery_status": new_status, "delivery_error": new_error, "delivery_code": new_code})


# ---------------------------------------------------------------------------
# Public API — page data
# ---------------------------------------------------------------------------

@app.route("/api/get-home-data", methods=["GET", "OPTIONS"])
def get_home_data():
    if request.method == "OPTIONS":
        return json_response({})
    data = db_read()
    payload = encrypt_payload({"games": data["games"], "banners": data["banners"]})
    return json_response({"success": True, "payload": payload})


def _norm_code(v):
    return str(v or "").strip().lower()


@app.route("/api/get-topup-data", methods=["GET", "OPTIONS"])
def get_topup_data():
    if request.method == "OPTIONS":
        return json_response({})

    game_code = request.args.get("id")
    if not game_code:
        return json_response({"success": False, "error": "Missing id"}, 400)

    data = db_read()
    target = _norm_code(game_code)
    game = next((g for g in data["games"] if _norm_code(g.get("code")) == target), None)
    if game is None:
        return json_response({"success": False, "error": "Game not found"}, 404)

    products = [p for p in data["products"] if _norm_code(p.get("game_code")) == target]

    # Defensive: coerce legacy string prices (saved before the numeric-price fix) so the
    # frontend's price.toFixed(2) doesn't crash and silently blank out the whole package list.
    for p in products:
        if p.get("price") not in (None, ""):
            try:
                p["price"] = float(p["price"])
            except (TypeError, ValueError):
                p["price"] = 0
        # Defensive: frontend hides any product whose "section" isn't exactly
        # "recommend" or "normal" — old rows (added before this field existed)
        # would otherwise vanish from the site even though they're in the DB.
        if p.get("section") not in ("recommend", "normal"):
            p["section"] = "normal"

    # Public-safe view only: cost_usd (wholesale cost, used for margin math in the
    # admin panel) and provider_package (the FazerCards offer_id) must never reach
    # the browser — the frontend's AES key/passphrase is public, so anything put
    # in this payload is effectively readable by anyone, not just "hidden" by
    # decryption. Whitelist exactly what the storefront needs to render a card.
    PUBLIC_PRODUCT_FIELDS = ("id", "game_code", "name", "price", "image_url", "section")
    public_products = [{f: p.get(f) for f in PUBLIC_PRODUCT_FIELDS} for p in products]

    payload = encrypt_payload({"game": game, "products": public_products})
    return json_response({"success": True, "payload": payload})


@app.route("/api/check-user", methods=["POST", "OPTIONS"])
@limiter.limit("10 per minute;100 per hour")
def check_user():
    """Auto CHECK ID — validates the player ID against FazerCards before checkout
    so the customer sees their in-game nickname and typos get caught early."""
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    game_code = body.get("gameCode")
    user_id = body.get("userId")
    zone_id = body.get("zoneId")
    if not game_code or not user_id:
        # DEBUG: log the raw body so we can see what keys the frontend actually sends.
        print("check-user MISSING FIELDS — raw body received:", body)
        return json_response({"success": False, "error": "Missing fields"}, 400)

    data = db_read()
    game = find_game(data, game_code)
    fc_category = (game or {}).get("fazercards_category_id")
    game_name = (game or {}).get("name")

    if not FAZERCARDS_API_KEY:
        # No FazerCards key configured yet — don't block checkout, just skip the auto-check.
        return json_response({"success": True, "name": None})

    try:
        # Pass the game's display NAME (not fc_category) — validate-id has its own
        # category_id namespace, resolved by name inside fazercards_validate_id().
        # fc_category is only passed as a last-resort fallback.
        result = fazercards_validate_id(game_name, user_id, zone_id, fallback_category_id=fc_category)
    except FazerCardsError as e:
        print("validate-id failed:", e)
        return json_response({"success": True, "name": None})
    except Exception as e:  # noqa: BLE001
        print("validate-id failed:", e)
        return json_response({"success": True, "name": None})

    if result.get("ok") and result.get("valid"):
        return json_response({"success": True, "name": result.get("player_name")})

    return json_response({"success": False, "error": "Player ID not found", "name": None})


@app.route("/api/get-stats", methods=["GET", "OPTIONS"])
def get_stats():
    if request.method == "OPTIONS":
        return json_response({})
    stat_type = request.args.get("type", "notifications")
    data = db_read()
    paid = [t for t in data["transactions"] if t.get("status") == "paid"]
    paid_sorted = sorted(paid, key=lambda t: t.get("created_at") or "", reverse=True)[:10]
    slim = [
        {"user_id": t["user_id"], "game_code": t["game_code"], "amount": t["amount"], "created_at": t["created_at"]}
        for t in paid_sorted
    ]
    payload = encrypt_payload(slim)
    return json_response({"success": True, "type": stat_type, "payload": payload})


@app.route("/api/my-orders", methods=["GET", "OPTIONS"])
@limiter.limit("20 per minute")
def my_orders():
    if request.method == "OPTIONS":
        return json_response({})

    user_id = (request.args.get("userId") or "").strip()
    if not user_id:
        return json_response({"success": False, "error": "Missing userId"}, 400)

    data = db_read()
    products_by_id = {p["id"]: p for p in data["products"]}
    games_by_code = {g["code"]: g for g in data["games"]}

    # Player ID is the only identifier this site has (no login system) — same trust
    # model as /api/check-user. Anyone who knows a player ID can see its order
    # history, same as anyone who knows it can already validate/target it for a
    # top-up. Rate-limited above so it can't be used to bulk-scrape all IDs.
    matches = [t for t in data["transactions"] if str(t.get("user_id")) == user_id]
    matches.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    matches = matches[:20]

    orders = []
    for t in matches:
        product = products_by_id.get(t.get("product_id"))
        game = games_by_code.get(t.get("game_code"))
        orders.append({
            "trx_id": t.get("trx_id"),
            "game_name": (game or {}).get("name") or t.get("game_code"),
            "product_name": (product or {}).get("name") or "",
            "amount": t.get("amount"),
            "status": t.get("status"),
            "delivery_status": t.get("delivery_status"),
            "delivery_code": t.get("delivery_code"),
            "created_at": t.get("created_at"),
            "paid_at": t.get("paid_at"),
        })
    return json_response({"success": True, "orders": orders})


@app.route("/api/get-site-settings", methods=["GET", "OPTIONS"])
def get_site_settings():
    if request.method == "OPTIONS":
        return json_response({})
    s = db_read()["site_settings"]
    return json_response({
        "success": True,
        "settings": {
            "SITE_NAME": s.get("site_name") or "PVH TOPUP",
            "FOOTER_NAME": s.get("footer_name") or "PVH TOPUP",
            "LOGO_URL": s.get("logo_url") or "",
            "ADMIN_TELEGRAM_LINK": s.get("admin_telegram_link") or "",
            "ADMIN_TELEGRAM_NAME": s.get("admin_telegram_name") or "",
            "FACEBOOK_LINK": s.get("facebook_link") or "",
            "TIKTOK_LINK": s.get("tiktok_link") or "",
            "FOOTER_DESC": s.get("footer_desc") or "",
            "COPYRIGHT": s.get("copyright") or "",
        },
    })


# ---------------------------------------------------------------------------
# Admin API (all require x-admin-token header == ADMIN_PANEL_TOKEN)
# ---------------------------------------------------------------------------

@app.route("/api/admin-settings", methods=["GET", "PUT", "OPTIONS"])
def admin_settings():
    if request.method == "OPTIONS":
        return json_response({})
    auth_err = require_admin()
    if auth_err:
        return auth_err

    if request.method == "GET":
        return json_response({"success": True, "settings": db_read()["site_settings"]})

    body = request.get_json(silent=True) or {}
    row = {
        "id": 1,
        "site_name": body.get("site_name"),
        "footer_name": body.get("footer_name"),
        "logo_url": body.get("logo_url"),
        "admin_telegram_link": body.get("admin_telegram_link"),
        "admin_telegram_name": body.get("admin_telegram_name"),
        "facebook_link": body.get("facebook_link"),
        "tiktok_link": body.get("tiktok_link"),
        "footer_desc": body.get("footer_desc"),
        "copyright": body.get("copyright"),
    }

    def _mutate(d):
        d["site_settings"] = row

    db_write(_mutate)
    return json_response({"success": True, "settings": row})


NUMERIC_FIELDS = {"products": {"price", "cost_usd"}}


def _coerce_fields(table_name, row):
    for f in NUMERIC_FIELDS.get(table_name, ()):
        if row.get(f) not in (None, ""):
            try:
                row[f] = float(row[f])
            except (TypeError, ValueError):
                pass
    # Frontend only renders products whose "section" is exactly "recommend" or
    # "normal" (anything else, including missing/blank, is silently invisible).
    # Default every product to "normal" unless the admin explicitly picked "recommend".
    if table_name == "products":
        if row.get("section") not in ("recommend", "normal"):
            row["section"] = "normal"


def _admin_crud(table_name, allowed_fields, required_on_create):
    """Generic GET/POST/PUT/DELETE handler for games / products / banners."""
    if request.method == "OPTIONS":
        return json_response({})
    auth_err = require_admin()
    if auth_err:
        return auth_err

    if request.method == "GET":
        data = db_read()
        rows = data[table_name]
        game_code = request.args.get("game_code")
        if game_code and table_name == "products":
            rows = [r for r in rows if r.get("game_code") == game_code]
        return json_response({"success": True, table_name: rows})

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if any(body.get(f) in (None, "") for f in required_on_create):
            return json_response({"success": False, "error": f"Missing {', '.join(required_on_create)}"}, 400)

        def _mutate(d):
            row = {"id": next_id(d, table_name)}
            for f in allowed_fields:
                row[f] = body.get(f)
            _coerce_fields(table_name, row)
            d[table_name].append(row)
            return row

        row = db_write(_mutate)
        singular = table_name[:-1] if table_name != "banners" else "banner"
        return json_response({"success": True, singular: row})

    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        row_id = body.get("id")
        if not row_id:
            return json_response({"success": False, "error": "Missing id"}, 400)

        def _mutate(d):
            row = find_by_id(d[table_name], row_id)
            if row is None:
                return None
            for f in allowed_fields:
                if f in body:
                    row[f] = body.get(f)
            _coerce_fields(table_name, row)
            return row

        row = db_write(_mutate)
        if row is None:
            return json_response({"success": False, "error": "Not found"}, 404)
        singular = table_name[:-1] if table_name != "banners" else "banner"
        return json_response({"success": True, singular: row})

    if request.method == "DELETE":
        row_id = request.args.get("id")
        if not row_id:
            return json_response({"success": False, "error": "Missing id"}, 400)

        def _mutate(d):
            d[table_name] = [r for r in d[table_name] if str(r.get("id")) != str(row_id)]

        db_write(_mutate)
        return json_response({"success": True})

    return json_response({"success": False, "error": "Method not allowed"}, 405)


@app.route("/api/admin-games", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def admin_games():
    # fazercards_category_id: the FazerCards topup category for this game (enables
    # auto ID-check + auto top-up). Get it from GET /topups on the FazerCards API.
    # fulfillment_type: "topup" (default, account-based, e.g. FF/ML diamonds) or
    # "giftcard" (code-based, e.g. Roblox — see GET /giftcards on FazerCards).
    # For "giftcard" games, fazercards_category_id must hold the /giftcards
    # category_id, and each product's provider_package must hold the card_id
    # from GET /giftcards/cards?category_id=... — NOT a topup offer_id.
    return _admin_crud(
        "games",
        ["name", "code", "image_url", "fazercards_category_id", "has_server_id", "fulfillment_type"],
        required_on_create=["name", "code"],
    )


@app.route("/api/admin-products", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def admin_products():
    # provider_package holds the FazerCards offer_id for this product (from
    # GET /topups/offers?category_id=<game's fazercards_category_id>).
    # cost_usd is the FazerCards wholesale cost (fill in manually from the offers
    # list) — used only to compute profit margin in the admin panel; it is never
    # shown to customers.
    return _admin_crud(
        "products",
        ["game_code", "name", "price", "cost_usd", "provider_package", "image_url", "section"],
        required_on_create=["game_code", "name"],
    )


@app.route("/api/admin-banners", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def admin_banners():
    # 'type' MUST be "main_slider" or "small_promo" — the frontend filters banners by
    # this field to decide where to render them (see u.banners.filter(S=>S.type===...)
    # in the site bundle). A banner with no/wrong type is silently invisible on the site.
    return _admin_crud("banners", ["image_url", "link", "type"], required_on_create=["image_url", "type"])


@app.route("/api/admin-transactions", methods=["GET", "PATCH", "OPTIONS"])
def admin_transactions():
    if request.method == "OPTIONS":
        return json_response({})
    auth_err = require_admin()
    if auth_err:
        return auth_err

    if request.method == "GET":
        status = request.args.get("status")
        limit = int(request.args.get("limit", 50))
        data = db_read()
        rows = data["transactions"]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda t: t.get("created_at") or "", reverse=True)[:limit]
        return json_response({"success": True, "transactions": rows})

    body = request.get_json(silent=True) or {}
    trx_id = body.get("trx_id")
    delivery_status = body.get("delivery_status")
    allowed = ["pending", "processing", "delivered", "failed", "manual"]
    if not trx_id or not delivery_status:
        return json_response({"success": False, "error": "Missing trx_id or delivery_status"}, 400)
    if delivery_status not in allowed:
        return json_response({"success": False, "error": f"delivery_status must be one of: {', '.join(allowed)}"}, 400)

    def _mutate(d):
        o = find_by_id(d["transactions"], trx_id, key="trx_id")
        if o is None:
            return None
        o["delivery_status"] = delivery_status
        o["delivery_error"] = body.get("delivery_error")
        return o

    row = db_write(_mutate)
    if row is None:
        return json_response({"success": False, "error": "Not found"}, 404)
    return json_response({"success": True, "transaction": row})


# ---------------------------------------------------------------------------
# CORS (kept permissive like the original functions, in case you split domains)
# ---------------------------------------------------------------------------

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-client-id, x-admin-token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return resp


# ---------------------------------------------------------------------------
# Global error handlers
#
# Without these, an unhandled exception in any view (a bad FazerCards
# response shape, a malformed request body, a JSON decode error, etc.) can
# either leak an internal stack trace to the client or, under some gunicorn
# worker classes, take the worker down entirely. One bad/malicious request
# should never be able to degrade service for everyone else.
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def _handle_payload_too_large(e):
    return json_response({"success": False, "error": "Payload too large"}, 413)


@app.errorhandler(429)
def _handle_rate_limited(e):
    _register_violation(_client_ip())
    return json_response({"success": False, "error": "Too many requests — please slow down"}, 429)


@app.errorhandler(404)
def _handle_not_found(e):
    return json_response({"success": False, "error": "Not found"}, 404)


@app.errorhandler(Exception)
def _handle_unexpected_error(e):
    import traceback
    print("UNHANDLED ERROR:", traceback.format_exc())
    return json_response({"success": False, "error": "Internal server error"}, 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"PVH TOPUP server running on http://0.0.0.0:{port}")
    print(f"  Site : http://localhost:{port}/")
    print(f"  Admin: http://localhost:{port}/admin (token = ADMIN_PANEL_TOKEN)")
    app.run(host="0.0.0.0", port=port, debug=False)
