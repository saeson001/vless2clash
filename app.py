#!/usr/bin/env python3
"""
VLESS to Clash Meta YAML Converter
Flask Web Application for converting VLESS links to Clash/Mihomo Party compatible YAML
"""

import re
import os
import glob
import json
import base64
import string
import secrets
import hashlib
import sqlite3
import datetime
import requests
from urllib.parse import unquote, parse_qs
from flask import (
    Flask, render_template, request, jsonify, Response,
    send_from_directory, abort, session, redirect, url_for
)

app = Flask(__name__)

# Persistent secret key — stored in a file so all gunicorn workers share
# the same key. Without this, each worker generates its own random key via
# secrets.token_hex(32), and session cookies signed by one worker are
# invalid in another, causing "未授权" errors after login.
SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "secret_key.txt")


def _load_or_create_secret_key():
    """Load the secret key from file, or generate and persist a new one."""
    os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
    try:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key and len(key) >= 32:
                return key
    except (FileNotFoundError, IOError):
        pass
    # Generate a new key and persist it
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _load_or_create_secret_key()

# Session config — ensure cookies work reliably across page reloads
app.config.update(
    SESSION_COOKIE_NAME="vless2clash_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_PATH="/",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
)

# Application version (sync with deploy.sh VERSION)
APP_VERSION = "v1.5.3"

# Directory for saving generated YAML files
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Token-to-filename mapping file (persists across restarts)
TOKEN_MAP_FILE = os.path.join(DOWNLOADS_DIR, "_token_map.json")

# SQLite database for conversion records
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "records.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Admin config file
ADMIN_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "admin_config.json")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    """Get a SQLite connection (row factory for dict-like access)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversion_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            original_links TEXT NOT NULL,
            subscription_urls TEXT DEFAULT '',
            yaml_content TEXT NOT NULL,
            client_ip TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            node_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)
    conn.commit()
    conn.close()


def init_admin():
    """Initialize default admin account if none exists.

    Default credentials: admin / admin123
    The password is written to admin_config.json for the deploy script to display.
    """
    DEFAULT_USERNAME = "admin"
    DEFAULT_PASSWORD = "admin123"

    conn = get_db()
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM admin_users")
    count = cursor.fetchone()["cnt"]

    if count == 0:
        salt = secrets.token_hex(16)
        password_hash = hashlib.sha256((salt + DEFAULT_PASSWORD).encode()).hexdigest()
        now = datetime.datetime.now().isoformat()

        conn.execute(
            "INSERT INTO admin_users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (DEFAULT_USERNAME, password_hash, salt, now)
        )
        conn.commit()

        # Save plaintext to config file (for display during install)
        config = {
            "username": DEFAULT_USERNAME,
            "password": DEFAULT_PASSWORD,
            "note": "Default credentials. Please change this password after first login."
        }
        with open(ADMIN_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.chmod(ADMIN_CONFIG_FILE, 0o600)

    conn.close()


def verify_admin(username, password):
    """Verify admin credentials. Returns True if valid."""
    conn = get_db()
    cursor = conn.execute(
        "SELECT * FROM admin_users WHERE username = ?",
        (username,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return False

    password_hash = hashlib.sha256((row["salt"] + password).encode()).hexdigest()
    if password_hash == row["password_hash"]:
        # Update last login
        conn = get_db()
        conn.execute(
            "UPDATE admin_users SET last_login = ? WHERE username = ?",
            (datetime.datetime.now().isoformat(), username)
        )
        conn.commit()
        conn.close()
        return True
    return False


def change_admin_password(username, old_password, new_password):
    """Change admin password. Returns (success, message)."""
    if not verify_admin(username, old_password):
        return False, "旧密码不正确"

    salt = secrets.token_hex(16)
    password_hash = hashlib.sha256((salt + new_password).encode()).hexdigest()
    conn = get_db()
    conn.execute(
        "UPDATE admin_users SET password_hash = ?, salt = ? WHERE username = ?",
        (password_hash, salt, username)
    )
    conn.commit()
    conn.close()
    return True, "密码修改成功"


def record_conversion(original_links, subscription_urls, yaml_content, client_ip, token, filename, node_count):
    """Insert a conversion record into the database."""
    conn = get_db()
    conn.execute(
        """INSERT INTO conversion_records
           (created_at, original_links, subscription_urls, yaml_content, client_ip, token, filename, node_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.datetime.now().isoformat(),
            original_links,
            subscription_urls,
            yaml_content,
            client_ip,
            token,
            filename,
            node_count
        )
    )
    conn.commit()
    conn.close()


def delete_record(record_id):
    """Delete a conversion record and its file from disk."""
    conn = get_db()
    cursor = conn.execute("SELECT token, filename FROM conversion_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False

    token = row["token"]
    filename = row["filename"]

    # Remove from token map
    token_map = _load_token_map()
    if token in token_map:
        del token_map[token]
        _save_token_map(token_map)

    # Remove file from disk
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    # Remove from database
    conn.execute("DELETE FROM conversion_records WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return True


# Initialize DB on import
init_db()
init_admin()


# ---------------------------------------------------------------------------
# VLESS Parser & YAML Generator
# ---------------------------------------------------------------------------

def parse_vless(vless_url):
    """Parse a VLESS URL into a proxy config dict.

    Handles:
      - security: none / tls / reality
      - network:  tcp / ws / grpc / h2
      - flow:     xtls-rprx-vision etc.
      - Reality:  pbk, sid, fp, sni
      - WebSocket: path, host
      - gRPC:      serviceName
    """
    vless_url = vless_url.strip()
    if not vless_url.lower().startswith("vless://"):
        return None

    # Remove vless:// prefix
    rest = vless_url[8:]

    # Split fragment (node name)
    if "#" in rest:
        main_part, name_encoded = rest.rsplit("#", 1)
        name = unquote(name_encoded).strip()
    else:
        main_part = rest
        name = "未命名节点"

    if not name:
        name = "未命名节点"

    # Split query string
    if "?" in main_part:
        user_server, query_string = main_part.split("?", 1)
    else:
        user_server = main_part
        query_string = ""

    # Parse uuid@server:port
    if "@" not in user_server:
        return None

    uuid_part, server_port = user_server.rsplit("@", 1)
    uuid = uuid_part.strip()
    if not uuid:
        return None

    # Handle IPv6 [::1]:port
    if server_port.startswith("["):
        match = re.match(r"\[(.+)\]:(\d+)", server_port)
        if not match:
            return None
        server = match.group(1)
        port = int(match.group(2))
    else:
        if ":" not in server_port:
            return None
        server, port_str = server_port.rsplit(":", 1)
        server = server.strip()
        try:
            port = int(port_str)
        except ValueError:
            return None

    if not server:
        return None

    # Parse query params (flatten single values)
    raw_params = parse_qs(query_string, keep_blank_values=True)
    params = {k: v[0] for k, v in raw_params.items()}

    network = params.get("type", "tcp").lower()
    security = params.get("security", "none").lower()

    proxy = {
        "name": name,
        "type": "vless",
        "server": server,
        "port": port,
        "uuid": uuid,
        "network": network,
        "tls": security in ("tls", "reality"),
        "udp": True,
    }

    # Flow: only use if explicitly present in the URL
    # Do NOT auto-add xtls-rprx-vision — if the server doesn't support it,
    # sending Vision flow will cause connection timeout
    if "flow" in params and params["flow"]:
        proxy["flow"] = params["flow"]

    # Reality options
    if security == "reality":
        proxy["servername"] = params.get("sni", params.get("peer", ""))
        reality_opts = {}
        if "pbk" in params or "public-key" in params:
            reality_opts["public-key"] = params.get("pbk", params.get("public-key", ""))
        if "sid" in params or "short-id" in params or "shortId" in params:
            reality_opts["short-id"] = params.get("sid", params.get("short-id", params.get("shortId", "")))
        if reality_opts:
            proxy["reality-opts"] = reality_opts
        if "fp" in params:
            proxy["client-fingerprint"] = params["fp"]

    elif security == "tls":
        if "sni" in params:
            proxy["servername"] = params["sni"]
        if "fp" in params:
            proxy["client-fingerprint"] = params["fp"]
        if "alpn" in params:
            proxy["alpn"] = params["alpn"].split(",")

    # WebSocket options
    if network == "ws":
        ws_opts = {}
        ws_opts["path"] = params.get("path", "/")
        if "host" in params or "sni" in params:
            ws_opts["headers"] = {"Host": params.get("host", params.get("sni", ""))}
        proxy["ws-opts"] = ws_opts

    # gRPC options
    if network == "grpc":
        proxy["grpc-opts"] = {
            "grpc-service-name": params.get("serviceName", params.get("servicename", ""))
        }

    # HTTP/2 network
    if network == "h2":
        h2_opts = {}
        h2_opts["path"] = params.get("path", "/")
        if "host" in params:
            h2_opts["host"] = params["host"]
        proxy["h2-opts"] = h2_opts

    return proxy


def generate_clash_yaml(proxies, config=None):
    """Generate Clash Meta / Mihomo Party compatible YAML.

    config keys:
      - port (int, default 7890)
      - allow_lan (bool, default True)
      - mode (str, default "rule")
      - log_level (str, default "info")
      - group_name (str, default "节点选择")
    """
    if config is None:
        config = {}

    port = config.get("port", 7890)
    allow_lan = config.get("allow_lan", True)
    mode = config.get("mode", "rule")
    log_level = config.get("log_level", "info")
    group_name = config.get("group_name", "节点选择")

    lines = []

    # Global settings
    lines.append(f"mixed-port: {port}")
    lines.append(f"allow-lan: {str(allow_lan).lower()}")
    lines.append(f"mode: {mode}")
    lines.append(f"log-level: {log_level}")
    lines.append("")

    # Proxies
    lines.append("proxies:")
    for p in proxies:
        lines.append(f'  - name: "{p["name"]}"')
        lines.append(f'    type: {p["type"]}')
        lines.append(f'    server: {p["server"]}')
        lines.append(f'    port: {p["port"]}')
        lines.append(f'    uuid: {p["uuid"]}')
        lines.append(f'    network: {p["network"]}')
        lines.append(f'    tls: {str(p["tls"]).lower()}')
        lines.append(f'    udp: {str(p["udp"]).lower()}')

        if "flow" in p:
            lines.append(f'    flow: {p["flow"]}')

        if "servername" in p:
            lines.append(f'    servername: {p["servername"]}')

        if "reality-opts" in p:
            lines.append(f'    reality-opts:')
            ro = p["reality-opts"]
            if "public-key" in ro:
                lines.append(f'      public-key: {ro["public-key"]}')
            if "short-id" in ro:
                lines.append(f'      short-id: {ro["short-id"]}')

        if "client-fingerprint" in p:
            lines.append(f'    client-fingerprint: {p["client-fingerprint"]}')

        if "alpn" in p:
            alpn_str = ", ".join(p["alpn"])
            lines.append(f'    alpn: [{alpn_str}]')

        if "ws-opts" in p:
            lines.append(f'    ws-opts:')
            wo = p["ws-opts"]
            lines.append(f'      path: "{wo.get("path", "/")}"')
            if "headers" in wo:
                lines.append(f'      headers:')
                for k, v in wo["headers"].items():
                    lines.append(f'        {k}: "{v}"')

        if "grpc-opts" in p:
            lines.append(f'    grpc-opts:')
            lines.append(f'      grpc-service-name: {p["grpc-opts"].get("grpc-service-name", "")}')

        if "h2-opts" in p:
            lines.append(f'    h2-opts:')
            ho = p["h2-opts"]
            lines.append(f'      path: "{ho.get("path", "/")}"')
            if "host" in ho:
                lines.append(f'      host: [{ho["host"]}]')

    lines.append("")

    # Proxy groups
    lines.append("proxy-groups:")
    lines.append(f'  - name: "{group_name}"')
    lines.append(f'    type: select')
    lines.append(f'    proxies:')
    for p in proxies:
        lines.append(f'      - "{p["name"]}"')
    lines.append(f'      - DIRECT')
    lines.append("")

    # Rules
    lines.append("rules:")
    lines.append(f'  - MATCH,{group_name}')

    return "\n".join(lines) + "\n"


def fetch_subscription(url):
    """Fetch subscription content from URL, auto-decode base64 if needed.

    Returns list of VLESS link strings.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ClashForWindows/0.20.39"
    }
    resp = requests.get(url, timeout=15, headers=headers)
    resp.raise_for_status()
    content = resp.text.strip()

    # Try base64 decode (common for v2ray subscriptions)
    try:
        b64_content = content.replace("\n", "").replace("\r", "").replace(" ", "")
        decoded = base64.b64decode(b64_content).decode("utf-8")
        if "vless://" in decoded or "vmess://" in decoded or "trojan://" in decoded:
            content = decoded
    except Exception:
        pass

    links = []
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("vless://"):
            links.append(line)

    return links


# ---------------------------------------------------------------------------
# File management — obfuscated random tokens instead of sequential numbers
# ---------------------------------------------------------------------------

def _load_token_map():
    """Load the token-to-filename mapping from disk."""
    try:
        with open(TOKEN_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_token_map(mapping):
    """Persist the token-to-filename mapping to disk."""
    with open(TOKEN_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)


def generate_random_token(length=16):
    """Generate a random alphanumeric token, e.g. 'k7m3xz9fqw2a8p1d'."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_obfuscated_file(yaml_content):
    """Save YAML content with a random token filename and return the token.

    Returns the token string (without extension). The actual file on disk is
    <token>.yaml, but the URL served to the user is /d/<token> — no extension,
    no sequential numbering, no guessable pattern.
    """
    token_map = _load_token_map()

    # Generate a unique token (retry on collision)
    token = generate_random_token()
    while token in token_map:
        token = generate_random_token()

    filename = f"{token}.yaml"
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    token_map[token] = filename
    _save_token_map(token_map)

    return token


def resolve_token(token):
    """Resolve a token to its actual filename on disk. Returns None if not found."""
    token_map = _load_token_map()
    return token_map.get(token)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def is_admin_logged_in():
    """Check if the current session has admin privileges."""
    return session.get("admin_user") is not None


def get_client_ip():
    """Get the real client IP, accounting for reverse proxy headers."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    if request.headers.get("X-Real-IP"):
        return request.headers.get("X-Real-IP").strip()
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Routes — Public
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/version")
def version():
    """Return the current application version."""
    return jsonify({"version": APP_VERSION})


@app.route("/api/convert", methods=["POST"])
def convert():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    raw_links = data.get("links", "").strip()
    sub_urls = data.get("subscriptions", "").strip()
    config = data.get("config", {})

    proxies = []
    errors = []
    seen_names = {}

    def add_proxy(proxy):
        name = proxy["name"]
        if name in seen_names:
            seen_names[name] += 1
            proxy["name"] = f"{name}_{seen_names[name]}"
        else:
            seen_names[name] = 0
        proxies.append(proxy)

    # Parse direct links
    if raw_links:
        for line in raw_links.splitlines():
            line = line.strip()
            if not line:
                continue
            # Handle comma-separated links too
            for link in re.split(r"[,\s]+", line):
                link = link.strip()
                if not link or not link.lower().startswith("vless://"):
                    continue
                proxy = parse_vless(link)
                if proxy:
                    add_proxy(proxy)
                else:
                    errors.append(f"解析失败: {link[:80]}...")

    # Fetch from subscription URLs
    if sub_urls:
        for url_line in sub_urls.splitlines():
            url = url_line.strip()
            if not url:
                continue
            if not url.startswith("http://") and not url.startswith("https://"):
                errors.append(f"无效订阅地址: {url[:80]}")
                continue
            try:
                links = fetch_subscription(url)
                for link in links:
                    proxy = parse_vless(link)
                    if proxy:
                        add_proxy(proxy)
                    else:
                        errors.append(f"订阅节点解析失败: {link[:80]}")
            except Exception as e:
                errors.append(f"订阅获取失败 ({url[:50]}): {str(e)}")

    if not proxies:
        error_msg = "未找到有效的 VLESS 节点"
        if errors:
            error_msg += "。错误详情: " + "; ".join(errors[:5])
        return jsonify({"error": error_msg}), 400

    yaml_content = generate_clash_yaml(proxies, config)

    # Save to file with obfuscated random token
    token = create_obfuscated_file(yaml_content)
    filename = f"{token}.yaml"

    # Record conversion in database
    client_ip = get_client_ip()
    record_conversion(
        original_links=raw_links,
        subscription_urls=sub_urls,
        yaml_content=yaml_content,
        client_ip=client_ip,
        token=token,
        filename=filename,
        node_count=len(proxies)
    )

    # Build download URL — no extension, no sequential numbering
    download_url = f"/d/{token}"

    return jsonify({
        "yaml": yaml_content,
        "count": len(proxies),
        "errors": errors,
        "token": token,
        "download_url": download_url,
        "proxies": [{"name": p["name"], "server": p["server"], "port": p["port"]} for p in proxies],
    })


@app.route("/d/<token>")
def serve_by_token(token):
    """Serve a YAML file by its random token — URL shows no filename or extension."""
    filename = resolve_token(token)
    if not filename:
        abort(404)
    return send_from_directory(DOWNLOADS_DIR, filename, mimetype="text/yaml")


@app.route("/files/<path:filename>")
def serve_file(filename):
    """Legacy route — still works for backward compatibility."""
    return send_from_directory(DOWNLOADS_DIR, filename, mimetype="text/yaml")


@app.route("/api/files")
def list_files():
    """List all saved YAML files (shows tokens, not real filenames)."""
    token_map = _load_token_map()
    result = []
    for token, filename in sorted(token_map.items()):
        filepath = os.path.join(DOWNLOADS_DIR, filename)
        if os.path.exists(filepath):
            result.append({
                "token": token,
                "url": f"/d/{token}",
                "size": os.path.getsize(filepath),
            })
    return jsonify({"files": result, "count": len(result)})


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------

@app.route("/manage")
def manage():
    """Admin entry point — shows login or dashboard depending on session."""
    if not is_admin_logged_in():
        return render_template("manage.html", logged_in=False, version=APP_VERSION)
    return render_template("manage.html", logged_in=True, version=APP_VERSION)


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    """Admin login endpoint."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请求数据为空"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    if verify_admin(username, password):
        session["admin_user"] = username
        session.permanent = True
        # Force session save by touching it
        session.modified = True
        return jsonify({"success": True, "message": "登录成功"})
    else:
        return jsonify({"error": "用户名或密码错误"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    """Admin logout endpoint."""
    session.pop("admin_user", None)
    return jsonify({"success": True, "message": "已退出登录"})


@app.route("/api/admin/check")
def admin_check():
    """Check if admin is logged in."""
    return jsonify({"logged_in": is_admin_logged_in(), "username": session.get("admin_user")})


@app.route("/api/admin/records")
def admin_records():
    """List conversion records with optional filtering.

    Query params:
      - page: page number (default 1)
      - per_page: items per page (default 20, max 100)
      - search: search in original_links, client_ip, token
      - ip: filter by IP
    """
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 20)), 100)
    search = request.args.get("search", "").strip()
    ip_filter = request.args.get("ip", "").strip()

    offset = (page - 1) * per_page

    conn = get_db()

    # Build query
    where_clauses = []
    params = []

    if search:
        where_clauses.append("(original_links LIKE ? OR client_ip LIKE ? OR token LIKE ?)")
        params.extend([f"%{search}%"] * 3)

    if ip_filter:
        where_clauses.append("client_ip LIKE ?")
        params.append(f"%{ip_filter}%")

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # Get total count
    cursor = conn.execute(f"SELECT COUNT(*) as cnt FROM conversion_records {where_sql}", params)
    total = cursor.fetchone()["cnt"]

    # Get records (exclude full yaml_content for list view)
    cursor = conn.execute(
        f"""SELECT id, created_at, original_links, subscription_urls, client_ip,
                  token, node_count,
                  length(yaml_content) as yaml_size
           FROM conversion_records {where_sql}
           ORDER BY created_at DESC
           LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    )
    records = []
    for row in cursor.fetchall():
        records.append({
            "id": row["id"],
            "created_at": row["created_at"],
            "original_links": row["original_links"],
            "subscription_urls": row["subscription_urls"],
            "client_ip": row["client_ip"],
            "token": row["token"],
            "node_count": row["node_count"],
            "yaml_size": row["yaml_size"],
        })

    conn.close()

    return jsonify({
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
    })


@app.route("/api/admin/records/<int:record_id>")
def admin_record_detail(record_id):
    """Get full detail of a single record (includes full YAML content)."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    conn = get_db()
    cursor = conn.execute(
        "SELECT * FROM conversion_records WHERE id = ?",
        (record_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "记录不存在"}), 404

    # Build download URL for the detail view
    download_url = f"{request.host_url.rstrip('/')}/d/{row['token']}"

    return jsonify({
        "id": row["id"],
        "created_at": row["created_at"],
        "original_links": row["original_links"],
        "subscription_urls": row["subscription_urls"],
        "yaml_content": row["yaml_content"],
        "client_ip": row["client_ip"],
        "token": row["token"],
        "filename": row["filename"],
        "node_count": row["node_count"],
        "download_url": download_url,
    })


@app.route("/api/admin/records/<int:record_id>", methods=["DELETE"])
def admin_delete_record(record_id):
    """Delete a conversion record and its file."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    success = delete_record(record_id)
    if success:
        return jsonify({"success": True, "message": "记录已删除"})
    else:
        return jsonify({"error": "记录不存在或删除失败"}), 404


@app.route("/api/admin/stats")
def admin_stats():
    """Get summary statistics for the admin dashboard."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    conn = get_db()

    # Total records
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM conversion_records")
    total_records = cursor.fetchone()["cnt"]

    # Total nodes converted
    cursor = conn.execute("SELECT COALESCE(SUM(node_count), 0) as total_nodes FROM conversion_records")
    total_nodes = cursor.fetchone()["total_nodes"]

    # Unique IPs
    cursor = conn.execute("SELECT COUNT(DISTINCT client_ip) as cnt FROM conversion_records")
    unique_ips = cursor.fetchone()["cnt"]

    # Records in last 24 hours
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat()
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM conversion_records WHERE created_at > ?", (cutoff,))
    recent_24h = cursor.fetchone()["cnt"]

    # Top 10 IPs by record count
    cursor = conn.execute(
        """SELECT client_ip, COUNT(*) as cnt, MAX(created_at) as last_seen
           FROM conversion_records
           GROUP BY client_ip
           ORDER BY cnt DESC
           LIMIT 10"""
    )
    top_ips = [{"ip": row["client_ip"], "count": row["cnt"], "last_seen": row["last_seen"]} for row in cursor.fetchall()]

    conn.close()

    return jsonify({
        "total_records": total_records,
        "total_nodes": total_nodes,
        "unique_ips": unique_ips,
        "recent_24h": recent_24h,
        "top_ips": top_ips,
    })


@app.route("/api/admin/change-password", methods=["POST"])
def admin_change_password():
    """Change admin password."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "请求数据为空"}), 400

    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if len(new_password) < 8:
        return jsonify({"error": "新密码至少 8 个字符"}), 400

    username = session.get("admin_user")
    success, message = change_admin_password(username, old_password, new_password)

    if success:
        return jsonify({"success": True, "message": message})
    else:
        return jsonify({"error": message}), 400


def reset_admin():
    """Reset admin account to default credentials (admin / admin123).

    Deletes all existing admin users and recreates the default one.
    Also rewrites admin_config.json with the default credentials.
    """
    DEFAULT_USERNAME = "admin"
    DEFAULT_PASSWORD = "admin123"

    conn = get_db()
    conn.execute("DELETE FROM admin_users")
    salt = secrets.token_hex(16)
    password_hash = hashlib.sha256((salt + DEFAULT_PASSWORD).encode()).hexdigest()
    now = datetime.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
        (DEFAULT_USERNAME, password_hash, salt, now)
    )
    conn.commit()
    conn.close()

    config = {
        "username": DEFAULT_USERNAME,
        "password": DEFAULT_PASSWORD,
        "note": "Default credentials. Please change this password after first login."
    }
    with open(ADMIN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.chmod(ADMIN_CONFIG_FILE, 0o600)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reset-admin":
        reset_admin()
        print("Admin credentials reset to: admin / admin123")
        print(f"Config file: {ADMIN_CONFIG_FILE}")
    else:
        app.run(host="0.0.0.0", port=5000, debug=False)
