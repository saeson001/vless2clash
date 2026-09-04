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
import yaml  # PyYAML: validate admin-edited YAML before saving

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
APP_VERSION = "v1.6.19"

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

# Admin global config file — shared defaults applied to every new subscription
# and to every token when the admin clicks "保存并应用到所有 Token".
GLOBAL_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "global_config.json")

# Default global config. All keys are safe to expose publicly (no secrets).
DEFAULT_GLOBAL_CONFIG = {
    "ai_routing": False,        # master switch for AI 智能分流
    "ai_preference": "jp_hk",   # "jp_hk" = AI→日本优先 / 默认→香港优先; "hk_jp" = 反过来
    "rules_mode": "basic",      # basic | remote | none
    "group_name": "节点选择",
    "port": 7890,
    "allow_lan": True,
    "log_level": "info",
    # Health-check used by fallback proxy groups (node failover)
    "hc_url": "https://cp.cloudflare.com/digest204",
    "hc_interval": 300,
    "hc_tolerance": 50,
    "hc_timeout": 5000,
}


def load_global_config():
    """Load global config, falling back to defaults for missing keys."""
    try:
        with open(GLOBAL_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULT_GLOBAL_CONFIG)
    merged = dict(DEFAULT_GLOBAL_CONFIG)
    for k, v in data.items():
        if k in DEFAULT_GLOBAL_CONFIG:
            merged[k] = v
    return merged


def save_global_config(cfg):
    """Persist provided keys (validated against DEFAULT_GLOBAL_CONFIG) to disk."""
    merged = dict(DEFAULT_GLOBAL_CONFIG)
    for k, v in (cfg or {}).items():
        if k in DEFAULT_GLOBAL_CONFIG:
            merged[k] = v
    # Coerce types to match defaults so downstream code stays simple
    merged["ai_routing"] = bool(merged["ai_routing"])
    merged["allow_lan"] = bool(merged["allow_lan"])
    merged["port"] = int(merged["port"] or 7890)
    merged["hc_interval"] = int(merged["hc_interval"] or 300)
    merged["hc_tolerance"] = int(merged["hc_tolerance"] or 50)
    merged["hc_timeout"] = int(merged["hc_timeout"] or 5000)
    os.makedirs(os.path.dirname(GLOBAL_CONFIG_FILE), exist_ok=True)
    with open(GLOBAL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def global_basic_config(gcfg):
    """Extract the basic + health-check keys consumed by generate_clash_yaml."""
    return {
        "port": gcfg["port"],
        "allow_lan": gcfg["allow_lan"],
        "log_level": gcfg["log_level"],
        "group_name": gcfg["group_name"],
        "rules_mode": gcfg["rules_mode"],
        "ai_preference": gcfg["ai_preference"],
        "hc_url": gcfg["hc_url"],
        "hc_interval": gcfg["hc_interval"],
        "hc_tolerance": gcfg["hc_tolerance"],
        "hc_timeout": gcfg["hc_timeout"],
    }


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
            node_count INTEGER DEFAULT 0,
            config_name TEXT DEFAULT '',
            update_count INTEGER DEFAULT 0
        )
    """)
    # Migration: add config_name column for existing databases
    try:
        conn.execute("SELECT config_name FROM conversion_records LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE conversion_records ADD COLUMN config_name TEXT DEFAULT ''")
        conn.commit()

    # Migration: add update_count column for existing databases
    try:
        conn.execute("SELECT update_count FROM conversion_records LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE conversion_records ADD COLUMN update_count INTEGER DEFAULT 0")
        conn.commit()

    # Migration: add AI smart-routing columns
    for col, ctype in [
        ("ai_routing", "INTEGER DEFAULT 0"),
        ("ai_japan", "TEXT DEFAULT ''"),
        ("ai_hongkong", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"SELECT {col} FROM conversion_records LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE conversion_records ADD COLUMN {col} {ctype}")
            conn.commit()

    # Migration: add updated_at column (last time the token's YAML was regenerated)
    try:
        conn.execute("SELECT updated_at FROM conversion_records LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE conversion_records ADD COLUMN updated_at TEXT DEFAULT ''")
        conn.commit()

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


def record_conversion(original_links, subscription_urls, yaml_content, client_ip, token, filename, node_count, config_name="", ai_routing=False, ai_japan="", ai_hongkong=""):
    """Insert a conversion record into the database."""
    now = datetime.datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        """INSERT INTO conversion_records
           (created_at, updated_at, original_links, subscription_urls, yaml_content, client_ip, token, filename, node_count, config_name, ai_routing, ai_japan, ai_hongkong)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            now,
            original_links,
            subscription_urls,
            yaml_content,
            client_ip,
            token,
            filename,
            node_count,
            config_name,
            1 if ai_routing else 0,
            ai_japan,
            ai_hongkong
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

    # Flow: use the value from the URL if explicitly present
    if "flow" in params and params["flow"]:
        proxy["flow"] = params["flow"]

    # Reality options
    if security == "reality":
        # v1.6.10: reverted the v1.6.8 "default Vision flow" behavior.
        # The inbound on the server has no Vision flow configured, so
        # adding flow=xtls-rprx-vision to links that omit it makes the
        # server reject the request at the VLESS layer (connection
        # closed / timeout). Output flow ONLY when the URL carries it.
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


def parse_vmess(vmess_url):
    """Parse a VMess URL (base64-encoded JSON) into a proxy config dict."""
    vmess_url = vmess_url.strip()
    if not vmess_url.lower().startswith("vmess://"):
        return None

    try:
        b64_str = vmess_url[8:]
        # Add padding if needed
        b64_str += "=" * (4 - len(b64_str) % 4) if len(b64_str) % 4 else ""
        decoded = base64.b64decode(b64_str).decode("utf-8")
        cfg = json.loads(decoded)
    except Exception:
        return None

    name = cfg.get("ps", "") or "未命名节点"
    server = cfg.get("add", "")
    port = int(cfg.get("port", 443))
    uuid = cfg.get("id", "")
    if not server or not uuid:
        return None

    network = cfg.get("net", "tcp").lower()
    tls_val = cfg.get("tls", "").lower()
    tls = tls_val in ("tls", "1", "true")

    proxy = {
        "name": name,
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": uuid,
        "alterId": int(cfg.get("aid", 0)),
        "network": network,
        "tls": tls,
        "udp": True,
    }

    if "scy" in cfg and cfg["scy"]:
        proxy["cipher"] = cfg["scy"]

    if tls:
        sni = cfg.get("sni", "")
        if sni:
            proxy["servername"] = sni
        if "alpn" in cfg and cfg["alpn"]:
            proxy["alpn"] = cfg["alpn"].split(",")
        if cfg.get("verify_cert", True) is False or cfg.get("allowInsecure") in (1, "1", True):
            proxy["skip-cert-verify"] = True

    if network == "ws":
        ws_opts = {"path": cfg.get("path", "/")}
        if cfg.get("host"):
            ws_opts["headers"] = {"Host": cfg["host"]}
        proxy["ws-opts"] = ws_opts

    if network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": cfg.get("path", "")}

    if network == "h2":
        h2_opts = {"path": cfg.get("path", "/")}
        if cfg.get("host"):
            h2_opts["host"] = [cfg["host"]]
        proxy["h2-opts"] = h2_opts

    return proxy


def parse_ss(ss_url):
    """Parse a Shadowsocks URL into a proxy config dict.

    Supports both SIP002 and legacy formats.
    """
    ss_url = ss_url.strip()
    if not ss_url.lower().startswith("ss://"):
        return None

    rest = ss_url[5:]

    # Extract fragment (name)
    name = "未命名节点"
    if "#" in rest:
        rest, name_encoded = rest.rsplit("#", 1)
        name = unquote(name_encoded).strip() or "未命名节点"

    # SIP002 format: base64url(method:password)@server:port/?plugin=...
    if "@" in rest:
        userinfo, server_part = rest.rsplit("@", 1)
        try:
            # base64url decode
            userinfo += "=" * (4 - len(userinfo) % 4) if len(userinfo) % 4 else ""
            decoded = base64.urlsafe_b64decode(userinfo).decode("utf-8")
        except Exception:
            try:
                decoded = base64.b64decode(userinfo).decode("utf-8")
            except Exception:
                # Maybe plaintext method:password
                decoded = userinfo

        if ":" not in decoded:
            return None
        method, password = decoded.split(":", 1)

        # Parse server:port (strip query string)
        if "?" in server_part:
            server_part = server_part.split("?", 1)[0]
        if "/" in server_part:
            server_part = server_part.split("/", 1)[0]
    else:
        # Legacy format: ss://base64(method:password@server:port)
        try:
            b64_str = rest
            b64_str += "=" * (4 - len(b64_str) % 4) if len(b64_str) % 4 else ""
            decoded = base64.b64decode(b64_str).decode("utf-8")
        except Exception:
            return None
        if "@" not in decoded or ":" not in decoded:
            return None
        userinfo, server_part = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)

    # Parse server:port
    if server_port_parse := _parse_server_port(server_part):
        server, port = server_port_parse
    else:
        return None

    return {
        "name": name,
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def parse_ssr(ssr_url):
    """Parse an SSR URL into a proxy config dict."""
    ssr_url = ssr_url.strip()
    if not ssr_url.lower().startswith("ssr://"):
        return None

    try:
        b64_str = ssr_url[6:]
        b64_str += "=" * (4 - len(b64_str) % 4) if len(b64_str) % 4 else ""
        decoded = base64.b64decode(b64_str).decode("utf-8")
    except Exception:
        return None

    # Format: server:port:protocol:method:obfs:base64(password)/?params
    if "/?" in decoded:
        main_part, params_part = decoded.split("/?", 1)
    else:
        main_part = decoded
        params_part = ""

    parts = main_part.split(":")
    if len(parts) < 6:
        return None

    server = parts[0]
    port = int(parts[1])
    protocol = parts[2]
    method = parts[3]
    obfs = parts[4]
    password_b64 = parts[5]

    try:
        password_b64 += "=" * (4 - len(password_b64) % 4) if len(password_b64) % 4 else ""
        password = base64.b64decode(password_b64).decode("utf-8")
    except Exception:
        return None

    name = "未命名节点"
    protocol_param = ""
    obfs_param = ""

    if params_part:
        try:
            params_b64 = params_part
            params_b64 += "=" * (4 - len(params_b64) % 4) if len(params_b64) % 4 else ""
            params_str = base64.b64decode(params_b64).decode("utf-8")
        except Exception:
            params_str = params_part

        for pair in params_str.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k == "obfsparam":
                    obfs_param = unquote(v)
                elif k == "protoparam":
                    protocol_param = unquote(v)
                elif k == "remarks":
                    name = unquote(v) or "未命名节点"

    proxy = {
        "name": name,
        "type": "ssr",
        "server": server,
        "port": port,
        "cipher": method,
        "password": password,
        "protocol": protocol,
        "obfs": obfs,
        "udp": True,
    }
    if protocol_param:
        proxy["protocol-param"] = protocol_param
    if obfs_param:
        proxy["obfs-param"] = obfs_param

    return proxy


def parse_trojan(trojan_url):
    """Parse a Trojan URL into a proxy config dict."""
    trojan_url = trojan_url.strip()
    if not trojan_url.lower().startswith("trojan://"):
        return None

    rest = trojan_url[9:]

    # Extract fragment (name)
    name = "未命名节点"
    if "#" in rest:
        rest, name_encoded = rest.rsplit("#", 1)
        name = unquote(name_encoded).strip() or "未命名节点"

    # Split query string
    if "?" in rest:
        main_part, query_string = rest.split("?", 1)
    else:
        main_part = rest
        query_string = ""

    # Parse password@server:port
    if "@" not in main_part:
        return None

    password, server_port = main_part.rsplit("@", 1)
    password = password.strip()
    if not password:
        return None

    if server_port_parse := _parse_server_port(server_port):
        server, port = server_port_parse
    else:
        return None

    raw_params = parse_qs(query_string, keep_blank_values=True)
    params = {k: v[0] for k, v in raw_params.items()}

    proxy = {
        "name": name,
        "type": "trojan",
        "server": server,
        "port": port,
        "password": password,
        "sni": params.get("sni", server),
        "udp": True,
    }

    if "allowInsecure" in params and params["allowInsecure"] in ("1", "true"):
        proxy["skip-cert-verify"] = True

    network = params.get("type", "tcp").lower()
    if network == "ws":
        ws_opts = {"path": params.get("path", "/")}
        if "host" in params:
            ws_opts["headers"] = {"Host": params["host"]}
        proxy["ws-opts"] = ws_opts
        proxy["network"] = "ws"

    if network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": params.get("serviceName", "")}
        proxy["network"] = "grpc"

    return proxy


def _parse_server_port(server_port):
    """Parse 'server:port' or '[ipv6]:port' into (server, port). Returns None on failure."""
    if server_port.startswith("["):
        match = re.match(r"\[(.+)\]:(\d+)", server_port)
        if not match:
            return None
        return match.group(1), int(match.group(2))
    else:
        if ":" not in server_port:
            return None
        server, port_str = server_port.rsplit(":", 1)
        server = server.strip()
        try:
            return server, int(port_str)
        except ValueError:
            return None


def parse_proxy(link):
    """Generic proxy parser — dispatches to the correct parser based on protocol."""
    link = link.strip()
    lower = link.lower()
    if lower.startswith("vless://"):
        return parse_vless(link)
    elif lower.startswith("vmess://"):
        return parse_vmess(link)
    elif lower.startswith("ss://"):
        return parse_ss(link)
    elif lower.startswith("ssr://"):
        return parse_ssr(link)
    elif lower.startswith("trojan://"):
        return parse_trojan(link)
    return None


# ---------------------------------------------------------------------------
# AI Smart Routing — foreign AI services -> Japan node, everything else -> HK
# ---------------------------------------------------------------------------
# Node role is detected by the proxy name. When ambiguous (multiple or missing
# Japan/HK nodes) the generation page asks the user to pick interactively.
JAPAN_KEYWORDS = ["日本", "东京", "大阪", "tokyo", "osaka", "jp", "japan"]
HK_KEYWORDS = ["香港", "港", "hongkong", "hong kong", "hk"]

AI_GROUP_NAME = "AI 分流"
DEFAULT_GROUP_NAME = "默认分流"

# Foreign AI service domains routed to the Japan node.
# Chinese AI services (deepseek.com, qwen, zhipu, kimi, coze, etc.) are
# intentionally excluded — they fall through to GEO/CN direct rules.
AI_DOMAINS = [
    "openai.com", "chat.openai.com", "api.openai.com", "platform.openai.com",
    "chatgpt.com", "oaiusercontent.com",
    "anthropic.com", "claude.ai", "api.anthropic.com",
    "poe.com",
    "perplexity.ai",
    "character.ai",
    "huggingface.co",
    "midjourney.com",
    "you.com",
    "x.ai", "grok.x.ai", "api.x.ai",
    "mistral.ai", "api.mistral.ai", "codestral.ai",
    "cohere.com", "api.cohere.ai",
    "replicate.com",
    "stability.ai", "platform.stability.ai",
    "elevenlabs.io",
    "runwayml.com",
    "pi.ai",
    "gemini.google.com", "aistudio.google.com", "notebooklm.google.com",
    "llama.meta.com",
    "fireworks.ai", "api.fireworks.ai",
    "together.xyz", "api.together.xyz",
    "deepinfra.com", "api.deepinfra.com",
    "openrouter.ai",
    "groq.com", "api.groq.com",
    "cursor.com", "cursor.sh",
    "githubcopilot.com",
]


def classify_region_nodes(proxies):
    """Detect Japan / Hong Kong node names from proxy names.

    Returns {"japan": [...], "hongkong": [...]} of matched proxy display names.
    """
    japan, hongkong = [], []
    for p in proxies:
        name_l = p["name"].lower()
        if any(kw in name_l for kw in JAPAN_KEYWORDS):
            japan.append(p["name"])
        if any(kw in name_l for kw in HK_KEYWORDS):
            hongkong.append(p["name"])
    return {"japan": japan, "hongkong": hongkong}


def _emit_ai_rules(lines):
    """Emit DOMAIN-SUFFIX rules for foreign AI services -> AI 分流 group."""
    for d in AI_DOMAINS:
        lines.append(f"  - DOMAIN-SUFFIX,{d},{AI_GROUP_NAME}")


# Defensive: reject known placeholder / unreachable upstream IPs right before the
# MATCH fallback. A misconfigured downstream device (e.g. a secondary WiFi AP that
# hardcodes its DNS upstream to a dead address like 5.5.5.5:55555) would otherwise
# flood the proxy with doomed connections and clutter the connection list. REJECT
# makes them fail instantly without consuming a proxy node. Add more CIDRs here
# if other dead/placeholder upstreams show up.
DEFENSIVE_RULES = [
    "  - IP-CIDR,5.5.5.5/32,REJECT",
]


def _emit_rules(lines, group_name, rules_mode="basic", ai_routing=False, ai_japan="", ai_hongkong=""):
    """Emit routing rules.

    Without these rules, Mihomo / Clash Meta behaves like 'global' mode
    even when the UI shows 'rule' mode — every request gets routed through
    the proxy group, so Chinese domestic sites (Baidu, Bilibili, etc.)
    become unreachable when a foreign node is selected.

    rules_mode:
      - "basic"  : inline rules covering LAN/Private IPs, common CN
                   service domains and .cn TLD -> DIRECT. No external
                   dependencies, works offline. (default)
      - "remote" : rule-providers pointing at Loyalsoldier v2ray-rules-dat
                   (more complete coverage; requires internet on first
                   start to fetch rules)
      - "none"   : only MATCH fallback (legacy behavior, equivalent to
                   global mode)
    """
    if rules_mode == "none":
        lines.append("rules:")
        if ai_routing:
            _emit_ai_rules(lines)
            lines.extend(DEFENSIVE_RULES)
            lines.append(f"  - MATCH,{DEFAULT_GROUP_NAME}")
        else:
            lines.extend(DEFENSIVE_RULES)
            lines.append(f"  - MATCH,{group_name}")
        return

    if rules_mode == "remote":
        lines.append("rule-providers:")
        lines.append("  geosite-cn:")
        lines.append("    type: http")
        lines.append("    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geosite.dat\"")
        lines.append("    interval: 86400")
        lines.append("    format: binary")
        lines.append("    behavior: domain")
        lines.append("  geoip-cn:")
        lines.append("    type: http")
        lines.append("    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geoip.dat\"")
        lines.append("    interval: 86400")
        lines.append("    format: binary")
        lines.append("    behavior: ipcidr")
        lines.append("")
        lines.append("rules:")
        lines.append("  - RULE-SET,geosite-cn,DIRECT")
        lines.append("  - RULE-SET,geoip-cn,DIRECT")
        lines.append("  - GEOIP,LAN,DIRECT")
        lines.append("  - GEOIP,PRIVATE,DIRECT")
        if ai_routing:
            _emit_ai_rules(lines)
            lines.extend(DEFENSIVE_RULES)
            lines.append(f"  - MATCH,{DEFAULT_GROUP_NAME}")
        else:
            lines.extend(DEFENSIVE_RULES)
            lines.append(f"  - MATCH,{group_name}")
        return

    # default: basic inline rules
    lines.append("rules:")

    # LAN / Private IPs - direct (no-resolve skips DNS lookup)
    for cidr in [
        "0.0.0.0/8",          # current network
        "10.0.0.0/8",         # private
        "100.64.0.0/10",      # carrier-grade NAT
        "127.0.0.0/8",        # loopback
        "169.254.0.0/16",     # link-local
        "172.16.0.0/12",      # private
        "192.0.0.0/24",       # IETF protocol assignments
        "192.0.2.0/24",       # TEST-NET-1
        "192.88.99.0/24",     # 6to4 relay anycast
        "192.168.0.0/16",     # private
        "198.18.0.0/15",      # benchmark testing
        "198.51.100.0/24",    # TEST-NET-2
        "203.0.113.0/24",     # TEST-NET-3
        "224.0.0.0/4",        # multicast
        "240.0.0.0/4",        # reserved
        "255.255.255.255/32", # broadcast
        "::1/128",            # IPv6 loopback
        "fc00::/7",           # IPv6 unique local
        "fe80::/10",          # IPv6 link-local
    ]:
        lines.append(f"  - IP-CIDR,{cidr},DIRECT,no-resolve")

    # Chinese top-level domains
    lines.append("  - DOMAIN-SUFFIX,cn,DIRECT")
    lines.append("  - DOMAIN-SUFFIX,xn--fiqs8s,DIRECT")  # .中国 punycode
    lines.append("  - DOMAIN-SUFFIX,lan,DIRECT")
    lines.append("  - DOMAIN-SUFFIX,local,DIRECT")

    # Common Chinese service domains (covers the bulk of CN traffic)
    cn_domains = [
        # Internet / portals
        "baidu.com", "qq.com", "taobao.com", "weibo.com", "163.com",
        "126.com", "sohu.com", "ifeng.com", "sina.com.cn", "sina.cn",
        "bilibili.com", "bilibili.tv", "douyin.com", "kuaishou.com",
        "zhihu.com", "douban.com", "csdn.net", "jianshu.com",
        # E-commerce
        "jd.com", "tmall.com", "alipay.com", "taobaocdn.com",
        "alicdn.com", "alimama.com", "iqiyi.com", "youku.com",
        "tudou.com", "v.qq.com", "gtimg.cn", "qpic.cn",
        "bdimg.com", "bdstatic.com", "weixin.qq.com", "wechat.com",
        "wechatpay.com", "tenpay.com",
        # Tech companies
        "tencent.com", "aliyun.com", "alicloud.com", "aliyun.cn",
        "xiaomi.com", "mi.com", "huawei.com", "bytedance.com",
        "meituan.com", "dianping.com", "ctrip.com", "trip.com",
        "baidu.cn", "baiducontent.com",
        # Government / state
        "gov.cn", "miit.gov.cn", "miibeian.gov.cn",
    ]
    for d in cn_domains:
        lines.append(f"  - DOMAIN-SUFFIX,{d},DIRECT")

    # CN geo databases (geoip/geosite data bundled with Mihomo / Clash Party).
    # GEOIP is essential: many CN sites use .com/.net domains not in the list
    # above; without it they fall through to MATCH -> proxy and become
    # unreachable from a foreign node.
    lines.append("  - GEOSITE,cn,DIRECT")
    lines.append("  - GEOIP,CN,DIRECT")

    # Final fallback -> proxy
    if ai_routing:
        _emit_ai_rules(lines)
        lines.extend(DEFENSIVE_RULES)
        lines.append(f"  - MATCH,{DEFAULT_GROUP_NAME}")
    else:
        lines.extend(DEFENSIVE_RULES)
        lines.append(f"  - MATCH,{group_name}")


def generate_clash_yaml(proxies, config=None):
    """Generate Clash Meta / Mihomo Party compatible YAML.

    config keys:
      - port (int, default 7890)
      - allow_lan (bool, default True)
      - mode (str, default "rule")
      - log_level (str, default "info")
      - group_name (str, default "节点选择")
      - rules_mode (str, default "basic")
          "basic"  = inline domestic-direct routing rules
          "remote" = rule-providers via Loyalsoldier v2ray-rules-dat
          "none"   = MATCH-only (legacy behavior)
      - ai_routing (bool, default False)
          When True: foreign AI domains -> Japan node (AI 分流 group),
          all other traffic -> Hong Kong node (默认分流 group).
          Requires ai_japan / ai_hongkong proxy display names.
          Forces mode=rule (AI routing is meaningless in global/direct mode).
    """
    if config is None:
        config = {}

    port = config.get("port", 7890)
    allow_lan = config.get("allow_lan", True)
    log_level = config.get("log_level", "info")
    group_name = config.get("group_name", "节点选择")
    rules_mode = config.get("rules_mode", "basic")

    ai_routing = bool(config.get("ai_routing", False))
    ai_japan = config.get("ai_japan", "") or ""
    ai_hongkong = config.get("ai_hongkong", "") or ""

    # AI routing needs rule mode to have any effect
    mode = "rule" if ai_routing else config.get("mode", "rule")

    lines = []

    # Global settings
    lines.append(f"mixed-port: {port}")
    lines.append(f"allow-lan: {str(allow_lan).lower()}")
    lines.append(f"mode: {mode}")
    lines.append(f"log-level: {log_level}")
    lines.append("")

    # DNS — best-practice for China: domestic DoH (HTTPS-encrypted, GFW-proof)
    # so foreign domains (e.g. youtube.com) resolve correctly and fast. OpenClash
    # ignores the subscription's dns: and uses its own DNS settings; this block
    # makes the generated YAML self-sufficient for Clash Party / mihomo standalone
    # (where the YouTube-slow DNS issue would otherwise recur after reinstall).
    lines.append("dns:")
    lines.append("  enable: true")
    lines.append("  enhanced-mode: fake-ip")
    lines.append("  fake-ip-range: 198.18.0.1/16")
    lines.append("  use-hosts: true")
    lines.append("  nameserver:")
    lines.append("    - https://doh.pub/dns-query")
    lines.append("    - https://dns.alidns.com/dns-query")
    lines.append("  fallback:")
    lines.append("    - https://doh.pub/dns-query")
    lines.append("    - https://dns.alidns.com/dns-query")
    lines.append("  fallback-filter:")
    lines.append("    geoip: true")
    lines.append("    geoip-code: CN")
    lines.append("    ipcidr:")
    lines.append("      - 240.0.0.0/4")
    lines.append("      - 0.0.0.0/32")
    lines.append("  fake-ip-filter:")
    lines.append("    - '*.lan'")
    lines.append("    - '*.local'")
    lines.append("    - localhost")
    lines.append("    - '*.localhost'")
    lines.append("    - '*.example'")
    lines.append("    - '*.invalid'")
    lines.append("    - 'time.*.com'")
    lines.append("    - '*.music.163.com'")
    lines.append("    - '*.stun.*.*'")
    lines.append("")

    # Proxies
    lines.append("proxies:")
    for p in proxies:
        lines.append(f'  - name: "{p["name"]}"')
        lines.append(f'    type: {p["type"]}')
        lines.append(f'    server: {p["server"]}')
        lines.append(f'    port: {p["port"]}')

        ptype = p["type"]

        if ptype == "vless":
            _emit_vless(lines, p)
        elif ptype == "vmess":
            _emit_vmess(lines, p)
        elif ptype == "ss":
            _emit_ss(lines, p)
        elif ptype == "ssr":
            _emit_ssr(lines, p)
        elif ptype == "trojan":
            _emit_trojan(lines, p)

    lines.append("")

    # Proxy groups
    # Health-check used by fallback groups for automatic node failover
    # (e.g. Hong Kong VPS traffic exhausted -> auto switch to Japan). The test
    # traffic goes THROUGH the proxy node, so an overseas URL is fine from CN.
    hc_url = config.get("hc_url", "https://cp.cloudflare.com/digest204")
    hc_interval = config.get("hc_interval", 300)
    hc_tolerance = config.get("hc_tolerance", 50)
    hc_timeout = config.get("hc_timeout", 5000)
    HC_URL = hc_url
    HC_INTERVAL = hc_interval
    HC_TOLERANCE = hc_tolerance
    HC_TIMEOUT = hc_timeout

    lines.append("proxy-groups:")
    # Manual select group (user override) — always available
    lines.append(f'  - name: "{group_name}"')
    lines.append(f'    type: select')
    lines.append(f'    proxies:')
    for p in proxies:
        lines.append(f'      - "{p["name"]}"')
    lines.append(f'      - DIRECT')
    lines.append("")

    if ai_routing:
        # ai_preference decides which region is prioritized in each group:
        #   "jp_hk" (default): 默认分流→香港优先, AI分流→日本优先
        #   "hk_jp"           : 默认分流→日本优先, AI分流→香港优先
        ai_pref = config.get("ai_preference", "jp_hk")
        if ai_pref == "hk_jp":
            default_first, default_second = ai_japan, ai_hongkong   # 默认→日本优先
            ai_first, ai_second = ai_hongkong, ai_japan             # AI→香港优先
        else:
            default_first, default_second = ai_hongkong, ai_japan   # 默认→香港优先
            ai_first, ai_second = ai_japan, ai_hongkong             # AI→日本优先

        # 默认分流 group (fallback type => first node that passes the health check)
        default_members = []
        if default_first:
            default_members.append(default_first)
        if default_second:
            default_members.append(default_second)
        for p in proxies:
            if p["name"] not in default_members:
                default_members.append(p["name"])
        default_members.append("DIRECT")

        # AI 分流 group
        ai_members = []
        if ai_first:
            ai_members.append(ai_first)
        if ai_second:
            ai_members.append(ai_second)
        for p in proxies:
            if p["name"] not in ai_members:
                ai_members.append(p["name"])
        ai_members.append("DIRECT")

        lines.append(f'  - name: "{DEFAULT_GROUP_NAME}"')
        lines.append(f'    type: fallback')
        lines.append(f'    proxies:')
        for m in default_members:
            lines.append(f'      - "{m}"')
        lines.append(f'    url: {HC_URL}')
        lines.append(f'    interval: {HC_INTERVAL}')
        lines.append(f'    tolerance: {HC_TOLERANCE}')
        lines.append(f'    timeout: {HC_TIMEOUT}')
        lines.append("")

        lines.append(f'  - name: "{AI_GROUP_NAME}"')
        lines.append(f'    type: fallback')
        lines.append(f'    proxies:')
        for m in ai_members:
            lines.append(f'      - "{m}"')
        lines.append(f'    url: {HC_URL}')
        lines.append(f'    interval: {HC_INTERVAL}')
        lines.append(f'    tolerance: {HC_TOLERANCE}')
        lines.append(f'    timeout: {HC_TIMEOUT}')
        lines.append("")

    # Rules
    _emit_rules(lines, group_name, rules_mode, ai_routing, ai_japan, ai_hongkong)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# YAML emitters per proxy type
# ---------------------------------------------------------------------------

def _emit_vless(lines, p):
    """Emit VLESS-specific YAML fields."""
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
    _emit_network_opts(lines, p)


def _emit_vmess(lines, p):
    """Emit VMess-specific YAML fields."""
    lines.append(f'    uuid: {p["uuid"]}')
    lines.append(f'    alterId: {p.get("alterId", 0)}')
    lines.append(f'    network: {p["network"]}')
    lines.append(f'    tls: {str(p["tls"]).lower()}')
    lines.append(f'    udp: {str(p["udp"]).lower()}')

    if "cipher" in p:
        lines.append(f'    cipher: {p["cipher"]}')
    if "servername" in p:
        lines.append(f'    servername: {p["servername"]}')
    if "alpn" in p:
        alpn_str = ", ".join(p["alpn"])
        lines.append(f'    alpn: [{alpn_str}]')
    if "skip-cert-verify" in p:
        lines.append(f'    skip-cert-verify: {str(p["skip-cert-verify"]).lower()}')
    _emit_network_opts(lines, p)


def _emit_ss(lines, p):
    """Emit Shadowsocks-specific YAML fields."""
    lines.append(f'    cipher: {p["cipher"]}')
    lines.append(f'    password: "{p["password"]}"')
    lines.append(f'    udp: {str(p.get("udp", True)).lower()}')


def _emit_ssr(lines, p):
    """Emit SSR-specific YAML fields."""
    lines.append(f'    cipher: {p["cipher"]}')
    lines.append(f'    password: "{p["password"]}"')
    lines.append(f'    protocol: {p["protocol"]}')
    lines.append(f'    obfs: {p["obfs"]}')
    if "protocol-param" in p:
        lines.append(f'    protocol-param: {p["protocol-param"]}')
    if "obfs-param" in p:
        lines.append(f'    obfs-param: {p["obfs-param"]}')
    lines.append(f'    udp: {str(p.get("udp", True)).lower()}')


def _emit_trojan(lines, p):
    """Emit Trojan-specific YAML fields."""
    lines.append(f'    password: "{p["password"]}"')
    lines.append(f'    sni: {p.get("sni", "")}')
    lines.append(f'    udp: {str(p.get("udp", True)).lower()}')
    if "skip-cert-verify" in p:
        lines.append(f'    skip-cert-verify: {str(p["skip-cert-verify"]).lower()}')
    if "network" in p:
        lines.append(f'    network: {p["network"]}')
    _emit_network_opts(lines, p)


def _emit_network_opts(lines, p):
    """Emit network-specific options (ws-opts, grpc-opts, h2-opts) shared by vless/vmess/trojan."""
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
        if any(proto in decoded for proto in ("vless://", "vmess://", "trojan://", "ss://", "ssr://")):
            content = decoded
    except Exception:
        pass

    links = []
    for line in content.splitlines():
        line = line.strip()
        lower = line.lower()
        if lower.startswith(("vless://", "vmess://", "ss://", "ssr://", "trojan://")):
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

def _parse_links_and_subs(raw_links, sub_urls):
    """Parse raw proxy links and subscription URLs into a list of proxies.

    Shared by /api/convert, /api/admin/records/<id>/edit, and /refresh.
    Returns (proxies, errors).
    """
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

    SUPPORTED_PREFIXES = ("vless://", "vmess://", "ss://", "ssr://", "trojan://")

    if raw_links:
        for line in raw_links.splitlines():
            line = line.strip()
            if not line:
                continue
            for link in re.split(r"[,\s]+", line):
                link = link.strip()
                if not link or not link.lower().startswith(SUPPORTED_PREFIXES):
                    continue
                proxy = parse_proxy(link)
                if proxy:
                    add_proxy(proxy)
                else:
                    errors.append(f"解析失败: {link[:80]}...")

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
                    proxy = parse_proxy(link)
                    if proxy:
                        add_proxy(proxy)
                    else:
                        errors.append(f"订阅节点解析失败: {link[:80]}")
            except Exception as e:
                errors.append(f"订阅获取失败 ({url[:50]}): {str(e)}")

    return proxies, errors


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/version")
def version():
    """Return the current application version."""
    return jsonify({"version": APP_VERSION})


@app.route("/api/global-config-public")
def global_config_public():
    """Expose non-sensitive global defaults so the generate page can prefill."""
    g = load_global_config()
    return jsonify({
        "ai_routing": g["ai_routing"],
        "ai_preference": g["ai_preference"],
        "rules_mode": g["rules_mode"],
        "group_name": g["group_name"],
        "port": g["port"],
        "allow_lan": g["allow_lan"],
        "log_level": g["log_level"],
    })


@app.route("/api/admin/global-config", methods=["GET", "POST"])
def admin_global_config():
    """Read or update the global config (总体配置)."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401
    if request.method == "GET":
        return jsonify(load_global_config())
    data = request.get_json(silent=True) or {}
    saved = save_global_config(data)
    return jsonify({"success": True, "config": saved})


@app.route("/api/convert", methods=["POST"])
def convert():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    raw_links = data.get("links", "").strip()
    sub_urls = data.get("subscriptions", "").strip()
    config = data.get("config", {}) or {}
    custom_name = data.get("config_name", "").strip()

    # Inherit global config defaults so every new subscription uses the admin's
    # 总体配置 (basic settings + health-check + AI preference). The page may still
    # override per-generation; AI on/off + node names come from the request below.
    gcfg = load_global_config()
    for key in ("port", "allow_lan", "log_level", "group_name", "rules_mode",
                "ai_preference", "hc_url", "hc_interval", "hc_tolerance", "hc_timeout"):
        if key not in config or config.get(key) in (None, ""):
            config[key] = gcfg.get(key)

    proxies, errors = _parse_links_and_subs(raw_links, sub_urls)

    if not proxies:
        error_msg = "未找到有效的代理节点"
        if errors:
            error_msg += "。错误详情: " + "; ".join(errors[:5])
        return jsonify({"error": error_msg}), 400

    # --- AI smart routing: foreign AI -> Japan node, rest -> Hong Kong node ---
    ai_routing = bool(data.get("ai_routing", False))
    ai_japan = (data.get("ai_japan", "") or "").strip()
    ai_hongkong = (data.get("ai_hongkong", "") or "").strip()

    if ai_routing:
        proxy_names = {p["name"] for p in proxies}
        if ai_japan and ai_hongkong and ai_japan in proxy_names and ai_hongkong in proxy_names:
            # Explicit assignment from the interactive picker — already validated
            pass
        else:
            # Auto-detect by node name; if ambiguous, hand the choice back to the page
            cls = classify_region_nodes(proxies)
            if len(cls["japan"]) == 1 and len(cls["hongkong"]) == 1:
                ai_japan = cls["japan"][0]
                ai_hongkong = cls["hongkong"][0]
            else:
                return jsonify({
                    "ai_routing_ambiguous": True,
                    "candidates": [p["name"] for p in proxies],
                    "detected_japan": cls["japan"],
                    "detected_hongkong": cls["hongkong"],
                    "errors": errors,
                    "message": "无法自动判断日本/香港节点，请在生成页手动指定",
                })

    # Determine config_name (the name Clash shows when importing)
    if not custom_name:
        if len(proxies) == 1:
            # Single link: use the node name from the link
            config_name = proxies[0]["name"]
        else:
            # Multiple links: use token as fallback
            config_name = ""
    else:
        config_name = custom_name

    config["ai_routing"] = ai_routing
    if ai_routing:
        config["ai_japan"] = ai_japan
        config["ai_hongkong"] = ai_hongkong

    yaml_content = generate_clash_yaml(proxies, config)

    # Save to file with obfuscated random token
    token = create_obfuscated_file(yaml_content)
    filename = f"{token}.yaml"

    # If no custom name and multiple links, use token
    if not config_name:
        config_name = token

    # Record conversion in database
    client_ip = get_client_ip()
    record_conversion(
        original_links=raw_links,
        subscription_urls=sub_urls,
        yaml_content=yaml_content,
        client_ip=client_ip,
        token=token,
        filename=filename,
        node_count=len(proxies),
        config_name=config_name,
        ai_routing=ai_routing,
        ai_japan=ai_japan,
        ai_hongkong=ai_hongkong
    )

    # Build download URL — no extension, no sequential numbering
    download_url = f"/d/{token}"

    return jsonify({
        "yaml": yaml_content,
        "count": len(proxies),
        "errors": errors,
        "token": token,
        "config_name": config_name,
        "download_url": download_url,
        "ai_routing": ai_routing,
        "ai_japan": ai_japan,
        "ai_hongkong": ai_hongkong,
        "proxies": [{"name": p["name"], "server": p["server"], "port": p["port"]} for p in proxies],
    })


@app.route("/d/<token>")
def serve_by_token(token):
    """Serve a YAML file by its random token — URL shows no filename or extension.

    Sets Content-Disposition with the config_name so Clash shows a friendly name
    instead of the raw token when importing the subscription.
    """
    filename = resolve_token(token)
    if not filename:
        abort(404)

    # Look up config_name from database for a friendly display name
    display_name = token
    try:
        conn = get_db()
        cursor = conn.execute("SELECT config_name FROM conversion_records WHERE token = ?", (token,))
        row = cursor.fetchone()
        # Count this client pull as one subscription update
        # (Clash Party / Mihomo importing or refreshing the subscription URL)
        conn.execute(
            "UPDATE conversion_records SET update_count = update_count + 1 WHERE token = ?",
            (token,)
        )
        conn.commit()
        conn.close()
        if row and row["config_name"]:
            display_name = row["config_name"]
    except Exception:
        pass

    # Sanitize display_name: strip characters that break HTTP headers
    # (double quotes, backslashes, CR, LF — prevent header injection)
    safe_name = display_name.replace('"', '').replace('\\', '').replace('\r', '').replace('\n', '').strip()
    if not safe_name:
        safe_name = token

    # URL-encode for filename* parameter (RFC 5987) — preserves Chinese chars
    from urllib.parse import quote
    encoded_name = quote(f"{safe_name}.yaml")

    # ASCII-only fallback for filename= parameter (RFC 6266)
    # Chinese chars in filename= violate the spec and break Clash Party / Mihomo
    # Party's HTTP parser, causing import errors. Use ASCII fallback here and
    # rely on filename*= for the proper UTF-8 name.
    ascii_fallback = safe_name.encode('ascii', 'replace').decode('ascii').replace('?', '_')
    if not ascii_fallback or ascii_fallback.strip('_') == '':
        ascii_fallback = token

    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        yaml_text = f.read()

    response = Response(yaml_text, mimetype="text/yaml; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{ascii_fallback}.yaml"; '
        f"filename*=UTF-8''{encoded_name}"
    )
    response.headers["Subscription-Userinfo"] = "upload=0; download=0; total=0; expire=0"
    return response


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
    # update_count: how many times clients (Clash Party etc.) have pulled
    # this record's subscription URL /d/<token>
    cursor = conn.execute(
        f"""SELECT r.id, r.created_at, r.updated_at, r.original_links, r.subscription_urls, r.client_ip,
                  r.token, r.node_count, r.config_name, r.update_count,
                  length(r.yaml_content) as yaml_size
           FROM conversion_records r {where_sql}
           ORDER BY r.created_at DESC
           LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    )
    records = []
    for row in cursor.fetchall():
        records.append({
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or row["created_at"],
            "original_links": row["original_links"],
            "subscription_urls": row["subscription_urls"],
            "client_ip": row["client_ip"],
            "token": row["token"],
            "node_count": row["node_count"],
            "config_name": row["config_name"] or "",
            "yaml_size": row["yaml_size"],
            "update_count": row["update_count"],
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

    if not row:
        conn.close()
        return jsonify({"error": "记录不存在"}), 404

    # Count how many times this IP has converted
    ip_update_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM conversion_records WHERE client_ip = ?",
        (row["client_ip"],)
    ).fetchone()["cnt"]

    # Top 10 IPs for the detail view's IP stats panel
    ip_cursor = conn.execute(
        """SELECT client_ip, COUNT(*) as cnt, MAX(created_at) as last_seen
           FROM conversion_records
           GROUP BY client_ip
           ORDER BY cnt DESC
           LIMIT 10"""
    )
    top_ips = [{"ip": r["client_ip"], "count": r["cnt"], "last_seen": r["last_seen"]} for r in ip_cursor.fetchall()]

    conn.close()

    # Build download URL for the detail view
    download_url = f"{request.host_url.rstrip('/')}/d/{row['token']}"

    return jsonify({
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] if "updated_at" in row.keys() and row["updated_at"] else row["created_at"],
        "original_links": row["original_links"],
        "subscription_urls": row["subscription_urls"],
        "yaml_content": row["yaml_content"],
        "client_ip": row["client_ip"],
        "token": row["token"],
        "filename": row["filename"],
        "node_count": row["node_count"],
        "config_name": row["config_name"] if "config_name" in row.keys() else "",
        "download_url": download_url,
        "update_count": row["update_count"] if "update_count" in row.keys() else 0,
        "ip_update_count": ip_update_count,
        "top_ips": top_ips,
    })


@app.route("/api/admin/records/<int:record_id>/yaml")
def admin_record_yaml_get(record_id):
    """Get the raw YAML content of a record for the online editor."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    conn = get_db()
    row = conn.execute(
        "SELECT id, token, filename, config_name, yaml_content, updated_at FROM conversion_records WHERE id = ?",
        (record_id,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "记录不存在"}), 404

    return jsonify({
        "id": row["id"],
        "token": row["token"],
        "filename": row["filename"],
        "config_name": row["config_name"] if "config_name" in row.keys() else "",
        "yaml_content": row["yaml_content"] or "",
        "updated_at": row["updated_at"],
    })


@app.route("/api/admin/records/<int:record_id>/yaml", methods=["POST"])
def admin_record_yaml_save(record_id):
    """Validate and save an admin-edited YAML back to a record (DB + file).

    The token never changes; clients get the edited config on next refresh.
    Invalid YAML is rejected so a broken config can never go live.
    """
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    data = request.get_json(silent=True) or {}
    yaml_content = data.get("yaml_content")
    if yaml_content is None or not str(yaml_content).strip():
        return jsonify({"error": "YAML 内容为空，未保存"}), 400
    yaml_content = str(yaml_content).replace("\r\n", "\n")

    # Validate with PyYAML before persisting.
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return jsonify({"error": "YAML 语法错误，未保存：" + str(e)[:300]}), 400
    if not isinstance(parsed, dict):
        return jsonify({"error": "YAML 顶层必须是键值映射（如 mixed-port/proxies/rules），未保存"}), 400
    if not parsed.get("proxies"):
        return jsonify({"error": "缺少非空 proxies 段，订阅将没有可用节点，未保存"}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT id, filename FROM conversion_records WHERE id = ?",
        (record_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "记录不存在"}), 404

    now = datetime.datetime.now().isoformat()
    conn.execute(
        "UPDATE conversion_records SET yaml_content = ?, updated_at = ? WHERE id = ?",
        (yaml_content, now, record_id)
    )
    conn.commit()
    conn.close()

    # Keep the served file in sync so /d/<token> reflects the edit immediately.
    warning = ""
    try:
        filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write(yaml_content)
    except OSError as e:
        warning = "数据库已更新，但写入文件失败：" + str(e)[:120]

    result = {"success": True, "message": "YAML 已保存并生效（token 不变，客户端刷新订阅即生效）"}
    if warning:
        result["warning"] = warning
    return jsonify(result)


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


@app.route("/api/admin/records/<int:record_id>/edit", methods=["PUT"])
def admin_edit_record(record_id):
    """Edit a record's original links and regenerate YAML.

    The token and filename stay the same, so the subscription URL /d/<token>
    does not change — Clash will pull the updated config on next refresh.

    Accepts JSON body:
      - links: new raw proxy links (string)
      - subscriptions: new subscription URLs (string, optional)
      - config_name: new config name (string, optional)
      - rules_mode: "basic" | "remote" | "none" (default "basic")
    """
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "请求数据为空"}), 400

    raw_links = data.get("links", "").strip()
    sub_urls = data.get("subscriptions", "").strip()
    new_config_name = data.get("config_name", "").strip()
    rules_mode = data.get("rules_mode", "basic")
    ai_routing = bool(data.get("ai_routing", False))
    ai_japan = (data.get("ai_japan", "") or "").strip()
    ai_hongkong = (data.get("ai_hongkong", "") or "").strip()

    if not raw_links and not sub_urls:
        return jsonify({"error": "请输入代理链接或订阅地址"}), 400

    # Fetch the existing record
    conn = get_db()
    cursor = conn.execute("SELECT * FROM conversion_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "记录不存在"}), 404

    token = row["token"]
    filename = row["filename"]

    # Parse new links
    proxies, errors = _parse_links_and_subs(raw_links, sub_urls)

    if not proxies:
        error_msg = "未找到有效的代理节点"
        if errors:
            error_msg += "。错误详情: " + "; ".join(errors[:5])
        conn.close()
        return jsonify({"error": error_msg}), 400

    # Determine config_name
    if new_config_name:
        config_name = new_config_name
    elif row["config_name"]:
        config_name = row["config_name"]
    elif len(proxies) == 1:
        config_name = proxies[0]["name"]
    else:
        config_name = token

    # AI routing: validate explicit assignment or fall back to stored/existing
    if ai_routing:
        proxy_names = {p["name"] for p in proxies}
        if not (ai_japan in proxy_names and ai_hongkong in proxy_names):
            cls = classify_region_nodes(proxies)
            if len(cls["japan"]) == 1 and len(cls["hongkong"]) == 1:
                ai_japan, ai_hongkong = cls["japan"][0], cls["hongkong"][0]
            else:
                ai_japan = ai_japan if ai_japan in proxy_names else ""
                ai_hongkong = ai_hongkong if ai_hongkong in proxy_names else ""

    # Generate new YAML with selected rules_mode + AI routing
    yaml_content = generate_clash_yaml(
        proxies,
        {"rules_mode": rules_mode, "ai_routing": ai_routing, "ai_japan": ai_japan, "ai_hongkong": ai_hongkong}
    )

    # Overwrite the YAML file on disk (same filename, same token)
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    # Update database record
    now = datetime.datetime.now().isoformat()
    conn.execute(
        """UPDATE conversion_records
           SET original_links = ?, subscription_urls = ?, yaml_content = ?,
               node_count = ?, config_name = ?, ai_routing = ?, ai_japan = ?, ai_hongkong = ?, updated_at = ?
           WHERE id = ?""",
        (raw_links, sub_urls, yaml_content, len(proxies), config_name,
         1 if ai_routing else 0, ai_japan, ai_hongkong, now, record_id)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": "记录已更新",
        "node_count": len(proxies),
        "errors": errors,
        "config_name": config_name,
        "ai_routing": ai_routing,
        "ai_japan": ai_japan,
        "ai_hongkong": ai_hongkong,
    })


@app.route("/api/admin/records/<int:record_id>/refresh", methods=["POST"])
def admin_refresh_record(record_id):
    """One-click refresh: regenerate YAML for an existing record using the
    latest routing rules, without changing links or token.

    Reads original_links + subscription_urls from the DB, re-parses them,
    and regenerates the YAML with the specified (or default "basic") rules_mode.
    The token and download URL stay the same — Clash just needs a subscription
    refresh to pick up the new config.

    Accepts JSON body (all optional):
      - rules_mode: "basic" | "remote" | "none" (default "basic")
    """
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    data = request.get_json(silent=True) or {}
    rules_mode = data.get("rules_mode", "basic")

    conn = get_db()
    cursor = conn.execute("SELECT * FROM conversion_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "记录不存在"}), 404

    token = row["token"]
    filename = row["filename"]
    raw_links = row["original_links"] or ""
    sub_urls = row["subscription_urls"] or ""

    # Preserve stored AI routing settings across refresh
    ai_routing = bool(row["ai_routing"])
    ai_japan = row["ai_japan"] or ""
    ai_hongkong = row["ai_hongkong"] or ""

    if not raw_links and not sub_urls:
        conn.close()
        return jsonify({"error": "该记录没有原始链接数据，无法刷新"}), 400

    # Re-parse using stored links
    proxies, errors = _parse_links_and_subs(raw_links, sub_urls)

    if not proxies:
        error_msg = "重新解析失败，未找到有效节点"
        if errors:
            error_msg += "。错误: " + "; ".join(errors[:3])
        conn.close()
        return jsonify({"error": error_msg}), 400

    # Regenerate YAML with new rules (AI routing preserved). Basic + health-check
    # settings are pulled from the global config so every token stays consistent.
    gcfg = load_global_config()
    single_cfg = global_basic_config(gcfg)
    single_cfg["rules_mode"] = rules_mode
    single_cfg["ai_routing"] = ai_routing
    single_cfg["ai_japan"] = ai_japan
    single_cfg["ai_hongkong"] = ai_hongkong
    yaml_content = generate_clash_yaml(proxies, single_cfg)

    # Overwrite file on disk
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    # Update DB — update_count is NOT touched here: it counts client pulls
    # of /d/<token>, not admin-side refreshes
    now = datetime.datetime.now().isoformat()
    conn.execute(
        "UPDATE conversion_records SET yaml_content = ?, node_count = ?, updated_at = ? WHERE id = ?",
        (yaml_content, len(proxies), now, record_id)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"配置已刷新（{rules_mode} 模式{'，AI 分流' if ai_routing else ''}），节点数 {len(proxies)}。客户端下次拉取订阅时生效。",
        "node_count": len(proxies),
        "errors": errors,
        "rules_mode": rules_mode,
        "ai_routing": ai_routing,
    })


@app.route("/api/admin/records/refresh-all", methods=["POST"])
def admin_refresh_all_records():
    """One-click refresh ALL records: regenerate YAML for every record using
    the current conversion logic.

    Useful after a converter upgrade (e.g. v1.6.8 REALITY default Vision flow)
    to batch-regenerate stored YAML without touching each record manually.
    Token, links and download URLs stay unchanged — clients just refresh
    their subscription to pick up the new config.

    Accepts JSON body (all optional):
      - rules_mode: "basic" | "remote" | "none" (default "basic")

    Note: update_count is NOT touched — it counts client pulls of /d/<token>.
    """
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    data = request.get_json(silent=True) or {}
    # NOTE: refresh-all now follows the global config (总体配置) as the
    # regeneration policy. The legacy per-call `rules_mode` override is ignored
    # in favour of the admin's global settings.
    gcfg = load_global_config()
    basic_cfg = global_basic_config(gcfg)

    conn = get_db()
    rows = conn.execute("SELECT * FROM conversion_records").fetchall()

    success = 0
    ai_enabled = 0
    skipped = []
    now = datetime.datetime.now().isoformat()
    for row in rows:
        record_id = row["id"]
        raw_links = row["original_links"] or ""
        sub_urls = row["subscription_urls"] or ""
        try:
            if not raw_links and not sub_urls:
                skipped.append({"id": record_id, "error": "无原始链接数据"})
                continue
            proxies, _errors = _parse_links_and_subs(raw_links, sub_urls)
            if not proxies:
                skipped.append({"id": record_id, "error": "重新解析失败，无有效节点"})
                continue

            # Apply the global config policy. AI node names are re-detected per
            # record (or kept from the stored assignment if still valid).
            cfg = dict(basic_cfg)
            cfg["ai_routing"] = False
            cfg["ai_japan"] = ""
            cfg["ai_hongkong"] = ""
            if gcfg["ai_routing"]:
                names = {p["name"] for p in proxies}
                stored_jp = row["ai_japan"] if (row.get("ai_japan") in names) else ""
                stored_hk = row["ai_hongkong"] if (row.get("ai_hongkong") in names) else ""
                cls = classify_region_nodes(proxies)
                ai_japan = stored_jp or (cls["japan"][0] if cls["japan"] else "")
                ai_hongkong = stored_hk or (cls["hongkong"][0] if cls["hongkong"] else "")
                if ai_japan and ai_hongkong:
                    cfg["ai_routing"] = True
                    cfg["ai_japan"] = ai_japan
                    cfg["ai_hongkong"] = ai_hongkong
                else:
                    skipped.append({
                        "id": record_id,
                        "error": "全局已开启 AI 分流，但此记录找不到日本/香港节点，已按非 AI 重算",
                    })
            yaml_content = generate_clash_yaml(proxies, cfg)
            filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(yaml_content)
            conn.execute(
                """UPDATE conversion_records
                   SET yaml_content = ?, node_count = ?, ai_routing = ?, ai_japan = ?,
                       ai_hongkong = ?, updated_at = ?
                   WHERE id = ?""",
                (yaml_content, len(proxies), 1 if cfg["ai_routing"] else 0,
                 cfg["ai_japan"], cfg["ai_hongkong"], now, record_id)
            )
            success += 1
            if cfg["ai_routing"]:
                ai_enabled += 1
        except Exception as e:  # noqa: BLE001 - keep batch going on single failure
            skipped.append({"id": record_id, "error": str(e)[:100]})

    conn.commit()
    conn.close()

    msg = f"已批量刷新 {success} 条记录（{gcfg['rules_mode']} 模式，其中 {ai_enabled} 条已启用 AI 分流）"
    if skipped:
        msg += f"，{len(skipped)} 条跳过（ID: "
        msg += ", ".join(str(s["id"]) for s in skipped) + "）"

    return jsonify({
        "success": True,
        "message": msg,
        "refreshed": success,
        "ai_enabled": ai_enabled,
        "skipped": skipped,
        "rules_mode": gcfg["rules_mode"],
    })


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


@app.route("/api/admin/daily-stats")
def admin_daily_stats():
    """Get daily conversion counts for the last N days."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    days = int(request.args.get("days", 7))

    conn = get_db()

    # Today's count
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    cursor = conn.execute(
        "SELECT COUNT(*) as cnt FROM conversion_records WHERE date(created_at) = ?",
        (today_str,)
    )
    today_count = cursor.fetchone()["cnt"]

    # This week's count (Monday to Sunday)
    now = datetime.datetime.now()
    monday = now - datetime.timedelta(days=now.weekday())
    monday_str = monday.strftime("%Y-%m-%d")
    cursor = conn.execute(
        "SELECT COUNT(*) as cnt FROM conversion_records WHERE date(created_at) >= ?",
        (monday_str,)
    )
    week_count = cursor.fetchone()["cnt"]

    # Daily breakdown for last N days
    daily = []
    for i in range(days - 1, -1, -1):
        day = now - datetime.timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(node_count), 0) as nodes FROM conversion_records WHERE date(created_at) = ?",
            (day_str,)
        )
        row = cursor.fetchone()
        daily.append({
            "date": day_str,
            "count": row["cnt"],
            "nodes": row["nodes"],
            "is_today": day_str == today_str,
        })

    # Total records (all time)
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM conversion_records")
    total_count = cursor.fetchone()["cnt"]

    conn.close()

    return jsonify({
        "total_count": total_count,
        "today_count": today_count,
        "week_count": week_count,
        "daily": daily,
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
