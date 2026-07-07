"""
PVH TOPUP — Python (Flask) backend
Replaces the original Netlify Functions (which needed Supabase) with a
single self-contained server that stores data in a local db.json file,
matching the same style as your other bots (JSON storage, no external DB).

Endpoints (same paths/behavior as the original netlify/functions/*.js):
  POST /api/create-payment
  POST /api/check-payment
  POST /api/expire-payment
  GET  /api/get-home-data
  GET  /api/get-topup-data?id=<game_code>
  POST /api/check-user
  GET  /api/get-stats?type=notifications
  POST /api/check-topup-status
  GET  /api/get-site-settings                (public)
  GET/PUT   /api/admin-settings              (admin, header x-admin-token)
  GET/POST/PUT/DELETE /api/admin-games       (admin)
  GET/POST/PUT/DELETE /api/admin-products    (admin)
  GET/POST/PUT/DELETE /api/admin-banners     (admin)
  GET/PATCH /api/admin-transactions          (admin)

Serves:
  GET /        -> index.html   (single-file frontend)
  GET /admin   -> admin/index.html

Run:
  pip install flask requests python-dotenv --break-system-packages
  python server.py
"""

import os
import json
import time
import hmac
import hashlib
import secrets
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, send_from_directory, g

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
# The frontend bundle calls CryptoJS.AES.decrypt(payloadString, PASSPHRASE) on
# several endpoints (get-home-data, get-topup-data, get-stats). CryptoJS's
# passphrase-based AES uses OpenSSL's "Salted__" format: MD5-based
# EVP_BytesToKey key/iv derivation, AES-256-CBC, PKCS7 padding, base64 output.
# This must be applied server-side or the frontend silently fails to decrypt
# and the games/products/notifications never render.
# ---------------------------------------------------------------------------
FRONTEND_PAYLOAD_KEY = os.environ.get("FRONTEND_PAYLOAD_KEY", "6Imhmam1ob2xienZ5a3l3c2hzbG9kIiwic")


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
        raise RuntimeError("The 'cryptography' package is required (pip install cryptography --break-system-packages)")
    plaintext = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    salt = secrets.token_bytes(8)
    key, iv = _evp_bytes_to_key(FRONTEND_PAYLOAD_KEY.encode("utf-8"), salt)
    pad_len = 16 - (len(plaintext) % 16)
    plaintext += bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    import base64
    return base64.b64encode(b"Salted__" + salt + ciphertext).decode("utf-8")

# ---------------------------------------------------------------------------
# Config (edit these, or set as real environment variables before running)
# ---------------------------------------------------------------------------
CAMRAPID_API_KEY = os.environ.get("CAMRAPID_API_KEY", "")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "sokii-secret-key-change-this")  # MUST match "Ct" in the frontend bundle
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_PANEL_TOKEN = os.environ.get("ADMIN_PANEL_TOKEN", "change-this-to-a-long-random-string")

TOPUP_PROVIDER_TOKEN = os.environ.get("TOPUP_PROVIDER_TOKEN", "")
TOPUP_PROVIDER_BASE_URL = os.environ.get("TOPUP_PROVIDER_BASE_URL", "https://cambotopup.com/api/reseller/")
TOPUP_ORDER_PATH = os.environ.get("TOPUP_ORDER_PATH", "order")
TOPUP_STATUS_PATH = os.environ.get("TOPUP_STATUS_PATH", "status")

CAMRAPID_CREATE_URL = "https://pay.camrapidpay.com/api/v1/khqr/create-payments"
CAMRAPID_CHECK_URL = "https://pay.camrapidpay.com/check-transaction-api"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)  # point this at a Render persistent disk mount in production
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "db.json")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "db_default.json")
STATIC_DIR = BASE_DIR  # index.html + admin/index.html live alongside this file

app = Flask(__name__)
_db_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Tiny JSON "database" (mirrors your usual db.json pattern)
# ---------------------------------------------------------------------------
def _load_db():
    if not os.path.exists(DB_PATH):
        with open(DEFAULT_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _save_db(data)
        return data
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_db(data):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def db_read():
    with _db_lock:
        return _load_db()


def db_write(mutate_fn):
    """mutate_fn(data) -> data; runs under lock, persists, returns result of mutate_fn"""
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


def verify_signature(user_id, amount, timestamp, signature):
    """HMAC-SHA256 check — must match the frontend: HmacSHA256(`${userId}${amount}${timestamp}`, SECRET)"""
    if not signature or not timestamp:
        return False
    try:
        age = time.time() * 1000 - float(timestamp)
    except (TypeError, ValueError):
        return False
    if age > 5 * 60 * 1000 or age < -60 * 1000:
        return False

    payload = f"{user_id}{amount}{timestamp}"
    expected = hmac.new(SIGNING_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


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


def create_topup_order(user_id, server_id, package_code, reference):
    if not TOPUP_PROVIDER_TOKEN:
        raise RuntimeError("TOPUP_PROVIDER_TOKEN is not set")
    res = requests.post(
        f"{TOPUP_PROVIDER_BASE_URL}{TOPUP_ORDER_PATH}",
        json={
            "token": TOPUP_PROVIDER_TOKEN,
            "user_id": user_id,
            "server_id": server_id,
            "package": package_code,
            "reference": reference,
        },
        timeout=15,
    )
    return res.json()


def check_topup_order(order_id):
    if not TOPUP_PROVIDER_TOKEN:
        raise RuntimeError("TOPUP_PROVIDER_TOKEN is not set")
    res = requests.post(
        f"{TOPUP_PROVIDER_BASE_URL}{TOPUP_STATUS_PATH}",
        json={"token": TOPUP_PROVIDER_TOKEN, "order_id": order_id},
        timeout=15,
    )
    return res.json()


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


def find_by_id(rows, id_value, key="id"):
    for row in rows:
        if str(row.get(key)) == str(id_value):
            return row
    return None


# ---------------------------------------------------------------------------
# Static file serving — single-file frontend + admin panel
# ---------------------------------------------------------------------------
@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/admin")
@app.route("/admin/")
def serve_admin():
    return send_from_directory(STATIC_DIR, "admin.html")


# ---------------------------------------------------------------------------
# Public API — payment flow
# ---------------------------------------------------------------------------
@app.route("/api/create-payment", methods=["POST", "OPTIONS"])
def create_payment():
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    user_id = body.get("userId")
    zone_id = body.get("zoneId")
    game_code = body.get("gameCode")
    product_id = body.get("productId")

    signature = request.headers.get("x-signature")
    timestamp = request.headers.get("x-timestamp")

    if not amount or not user_id or not game_code or not product_id:
        return json_response({"success": False, "error": "Missing required fields"}, 400)

    if not verify_signature(user_id, amount, timestamp, signature):
        return json_response({"success": False, "error": "Invalid signature"}, 401)

    trx_id = f"PVH{int(time.time() * 1000)}{secrets.randbelow(1000)}"
    reference = trx_id

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
            "provider_order_id": None,
            "created_at": now_iso(),
            "paid_at": None,
        })

    db_write(_mutate)

    return json_response({"success": True, "trx_id": trx_id, "qr_data": data.get("qr_code")})


@app.route("/api/check-payment", methods=["POST", "OPTIONS"])
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

    # Mark paid + run auto top-up + notify admin
    def _mutate(d):
        o = find_by_id(d["transactions"], trx_id, key="trx_id")
        o["status"] = "paid"
        o["paid_at"] = now_iso()

        product = find_by_id(d["products"], o["product_id"])
        delivery_status = "manual"
        delivery_error = None
        provider_order_id = None

        if product and product.get("provider_package") and TOPUP_PROVIDER_TOKEN:
            try:
                topup_res = create_topup_order(
                    user_id=o["user_id"], server_id=o.get("zone_id"),
                    package_code=product["provider_package"], reference=o["trx_id"],
                )
                if str(topup_res.get("status", "")).lower() == "success":
                    provider_order_id = topup_res.get("order_ID") or topup_res.get("order_id")
                    delivery_status = "processing"
                else:
                    delivery_status = "failed"
                    delivery_error = topup_res.get("message", "Unknown provider error")
            except Exception as e:  # noqa: BLE001
                print("Auto top-up order failed:", e)
                delivery_status = "failed"
                delivery_error = str(e)

        o["provider_order_id"] = provider_order_id
        o["delivery_status"] = delivery_status
        o["delivery_error"] = delivery_error
        return o, delivery_status, provider_order_id, delivery_error

    order_after, delivery_status, provider_order_id, delivery_error = db_write(_mutate)

    if delivery_status == "processing":
        delivery_line = f"⏳ Auto top-up submitted (provider order {provider_order_id}) — awaiting confirmation"
    elif delivery_status == "failed":
        delivery_line = f"⚠️ *AUTO TOP-UP FAILED*: {delivery_error}\n👉 Please deliver manually"
    else:
        delivery_line = "👤 No auto top-up mapping for this product — please deliver manually"

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
        result = check_topup_order(order["provider_order_id"])
    except Exception as e:  # noqa: BLE001
        print("check-topup-status error:", e)
        return json_response({"error": "Server error"}, 500)

    provider_status = str(result.get("topup_status", "")).upper()
    new_status = order["delivery_status"]
    new_error = order.get("delivery_error")

    if provider_status in ("SUCCESS", "COMPLETED", "DONE"):
        new_status = "delivered"
    elif provider_status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
        new_status = "failed"
        new_error = result.get("message", "Provider reported failure")

    if new_status != order["delivery_status"]:
        def _mutate(d):
            o = find_by_id(d["transactions"], trx_id, key="trx_id")
            o["delivery_status"] = new_status
            o["delivery_error"] = new_error

        db_write(_mutate)

        zone_part = f" ({order['zone_id']})" if order.get("zone_id") else ""
        if new_status == "delivered":
            notify_admin(
                f"💎 *AUTO TOP-UP DELIVERED*\n{order['game_code']} / {order['user_id']}{zone_part}\nRef: {order['reference']}"
            )
        elif new_status == "failed":
            notify_admin(
                f"⚠️ *AUTO TOP-UP FAILED* after processing\n{order['game_code']} / {order['user_id']}\n"
                f"Ref: {order['reference']}\nReason: {new_error}\n👉 Please deliver manually"
            )

    return json_response({"delivery_status": new_status, "delivery_error": new_error})


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


@app.route("/api/get-topup-data", methods=["GET", "OPTIONS"])
def get_topup_data():
    if request.method == "OPTIONS":
        return json_response({})
    game_code = request.args.get("id")
    if not game_code:
        return json_response({"success": False, "error": "Missing id"}, 400)
    data = db_read()
    game = next((g for g in data["games"] if g.get("code") == game_code), None)
    if game is None:
        return json_response({"success": False, "error": "Game not found"}, 404)
    products = [p for p in data["products"] if p.get("game_code") == game_code]
    payload = encrypt_payload({"game": game, "products": products})
    return json_response({"success": True, "payload": payload})


@app.route("/api/check-user", methods=["POST", "OPTIONS"])
def check_user():
    if request.method == "OPTIONS":
        return json_response({})
    body = request.get_json(silent=True) or {}
    game_code = body.get("gameCode")
    user_id = body.get("userId")
    if not game_code or not user_id:
        return json_response({"success": False, "error": "Missing fields"}, 400)
    # TODO: wire a real in-game-nickname-lookup provider here if you have one.
    return json_response({"success": True, "name": None})


@app.route("/api/get-stats", methods=["GET", "OPTIONS"])
def get_stats():
    if request.method == "OPTIONS":
        return json_response({})
    stat_type = request.args.get("type", "notifications")
    data = db_read()
    paid = [t for t in data["transactions"] if t.get("status") == "paid"]
    paid_sorted = sorted(paid, key=lambda t: t.get("created_at") or "", reverse=True)[:10]
    slim = [{"user_id": t["user_id"], "game_code": t["game_code"], "amount": t["amount"], "created_at": t["created_at"]} for t in paid_sorted]
    payload = encrypt_payload(slim)
    return json_response({"success": True, "type": stat_type, "payload": payload})


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
    return _admin_crud("games", ["name", "code", "image_url"], required_on_create=["name", "code"])


@app.route("/api/admin-products", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def admin_products():
    return _admin_crud("products", ["game_code", "name", "price", "provider_package"], required_on_create=["game_code", "name"])


@app.route("/api/admin-banners", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def admin_banners():
    return _admin_crud("banners", ["image_url", "link"], required_on_create=["image_url"])


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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-signature, x-timestamp, x-client-id, x-admin-token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"PVH TOPUP server running on http://0.0.0.0:{port}")
    print(f"  Site : http://localhost:{port}/")
    print(f"  Admin: http://localhost:{port}/admin  (token = ADMIN_PANEL_TOKEN)")
    app.run(host="0.0.0.0", port=port, debug=False)
