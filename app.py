#!/usr/bin/env python3
"""
VLESS to Clash Meta YAML Converter
Flask Web Application for converting VLESS links to Clash/Mihomo Party compatible YAML
"""

import re
import os
import glob
import json
import time
import base64
import string
import secrets
import hashlib
import sqlite3
import datetime
import threading
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
APP_VERSION = "v1.6.38"

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
    # Put 「自动选择」(url-test) first in the main select group so a freshly
    # imported subscription auto-picks the fastest node instead of DIRECT.
    "default_to_auto": True,
    # Health-check used by fallback proxy groups (node failover)
    "hc_url": "https://cp.cloudflare.com/digest204",
    "hc_interval": 300,
    "hc_tolerance": 50,
    "hc_timeout": 5000,
    # Scheduled auto-update: re-fetch & regenerate records that carry a
    # subscription (xui_sub_url / subscription_urls) so 3x-ui node changes are
    # synced without a manual 「更新」 click.
    "auto_update_enabled": False,
    "auto_update_interval_hours": 6,
    # VPS traffic display: 3x-ui panel URLs for fetching inbound traffic stats.
    # Format: JSON array of {"url":"http://IP:PORT","username":"...","password":"..."}
    # Leave empty to disable (shows 0/0).
    "vps_traffic_sources": [],
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
    merged["default_to_auto"] = bool(merged.get("default_to_auto", True))
    merged["port"] = int(merged["port"] or 7890)
    merged["hc_interval"] = int(merged["hc_interval"] or 300)
    merged["hc_tolerance"] = int(merged["hc_tolerance"] or 50)
    merged["hc_timeout"] = int(merged["hc_timeout"] or 5000)
    merged["auto_update_enabled"] = bool(merged.get("auto_update_enabled", False))
    merged["auto_update_interval_hours"] = int(merged.get("auto_update_interval_hours", 6) or 6)
    # vps_traffic_sources: JSON array of 3x-ui panel credentials
    src = merged.get("vps_traffic_sources")
    if isinstance(src, list):
        merged["vps_traffic_sources"] = src
    else:
        merged["vps_traffic_sources"] = []
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
        # .get() guards configs written by older versions (key may be absent)
        "default_to_auto": gcfg.get("default_to_auto", True),
        "hc_url": gcfg["hc_url"],
        "hc_interval": gcfg["hc_interval"],
        "hc_tolerance": gcfg["hc_tolerance"],
        "hc_timeout": gcfg["hc_timeout"],
    }


# ---------------------------------------------------------------------------
# Global template (v1.6.21): the SHARED part of the generated Clash config,
# editable as raw YAML in the admin backend and appliable to every token.
# Per-record `proxies` / `proxy-groups` are always taken from the record itself.
# Placeholders: {{DEFAULT_GROUP}} / {{AI_GROUP}} resolve per record.
# ---------------------------------------------------------------------------
GLOBAL_TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "global_template.yaml")

DEFAULT_GLOBAL_TEMPLATE = """\
# v2c 全局配置模板 —— 所有订阅共享的部分
# · 这里只写「公共配置」：基础设置、DNS、规则等
# · 每条记录的 proxies（节点）和 proxy-groups（策略组）会自动保留，不要在这里写
# · 占位符：{{DEFAULT_GROUP}} = 该记录的主策略组，{{AI_GROUP}} = 该记录的 AI 策略组（没有则等于主策略组）
# · 编辑完点「保存并应用到所有 Token」，即可一次性铺到全部订阅（token 不变）

mixed-port: 7890
allow-lan: true
mode: rule
log-level: warning

dns:
  enable: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  use-hosts: true
  nameserver:
    - https://doh.pub/dns-query
    - https://dns.alidns.com/dns-query
  fallback:
    - https://doh.pub/dns-query
    - https://dns.alidns.com/dns-query
  fallback-filter:
    geoip: true
    geoip-code: CN
    ipcidr:
      - 240.0.0.0/4
      - 0.0.0.0/32
  fake-ip-filter:
    - '*.lan'
    - '*.local'
    - localhost
    - '*.localhost'
    - '*.example'
    - '*.invalid'
    - 'time.*.com'
    - '*.music.163.com'
    - '*.stun.*.*'

rule-providers:
  geosite-cn:
    type: http
    url: "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geosite.dat"
    interval: 86400
    format: binary
    behavior: domain
  geoip-cn:
    type: http
    url: "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geoip.dat"
    interval: 86400
    format: binary
    behavior: ipcidr

rules:
  - RULE-SET,geosite-cn,DIRECT
  - RULE-SET,geoip-cn,DIRECT
  - GEOIP,LAN,DIRECT
  - GEOIP,PRIVATE,DIRECT
  - IP-CIDR,5.5.5.5/32,REJECT
  # 下面这行是「国外 AI 走 AI 策略组」的示例；不需要就删掉
  # - GEOSITE,openai,{{AI_GROUP}}
  - MATCH,{{DEFAULT_GROUP}}
"""


def load_global_template():
    """Return the raw global template text (defaults if never saved)."""
    try:
        with open(GLOBAL_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return DEFAULT_GLOBAL_TEMPLATE


def save_global_template(text):
    """Persist the raw global template text."""
    os.makedirs(os.path.dirname(GLOBAL_TEMPLATE_FILE), exist_ok=True)
    with open(GLOBAL_TEMPLATE_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# Keys never taken from the template (they are per-record)
_TEMPLATE_RESERVED_KEYS = ("proxies", "proxy-groups")
# Keys that must come after proxies / proxy-groups in the output
_TEMPLATE_TAIL_KEYS = ("rule-providers", "rules")


def _tpl_placeholders(value, default_group, ai_group):
    """Recursively resolve {{DEFAULT_GROUP}} / {{AI_GROUP}} in template values."""
    if isinstance(value, str):
        return value.replace("{{DEFAULT_GROUP}}", default_group).replace("{{AI_GROUP}}", ai_group)
    if isinstance(value, list):
        return [_tpl_placeholders(v, default_group, ai_group) for v in value]
    if isinstance(value, dict):
        return {k: _tpl_placeholders(v, default_group, ai_group) for k, v in value.items()}
    return value


def apply_template_to_record(template_text, record_yaml):
    """Merge the global template into one record's YAML.

    Returns the regenerated YAML text. The record keeps its own proxies and
    proxy-groups; everything else comes from the template.
    """
    tpl = yaml.safe_load(template_text) or {}
    if not isinstance(tpl, dict):
        raise ValueError("模板顶层必须是键值映射")

    doc = yaml.safe_load(record_yaml) or {}
    if not isinstance(doc, dict):
        raise ValueError("该记录的 YAML 无法解析，已跳过")

    proxies = doc.get("proxies") or []
    groups = doc.get("proxy-groups") or []
    if not proxies:
        raise ValueError("该记录没有节点（proxies 为空），已跳过")

    # Group names are resolved per record so the rules point at real groups.
    default_group = "DIRECT"
    if groups and isinstance(groups[0], dict):
        default_group = groups[0].get("name") or default_group
    elif proxies and isinstance(proxies[0], dict):
        default_group = proxies[0].get("name") or default_group
    ai_group = default_group
    if len(groups) > 1 and isinstance(groups[1], dict):
        ai_group = groups[1].get("name") or default_group

    merged = {}
    for key, value in tpl.items():
        if key in _TEMPLATE_RESERVED_KEYS or key in _TEMPLATE_TAIL_KEYS or key == "dns":
            continue
        merged[key] = _tpl_placeholders(value, default_group, ai_group)
    if "dns" in tpl:
        merged["dns"] = _tpl_placeholders(tpl["dns"], default_group, ai_group)
    merged["proxies"] = proxies
    merged["proxy-groups"] = groups
    for key in _TEMPLATE_TAIL_KEYS:
        if key in tpl:
            merged[key] = _tpl_placeholders(tpl[key], default_group, ai_group)
    if "rules" not in merged:
        merged["rules"] = []

    return yaml.safe_dump(
        merged, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4096
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    """Get a SQLite connection (row factory for dict-like access).

    timeout=30 raises the busy-wait from the 5s default: concurrent writers
    (auto-migrate thread, /d/<token> pull counters, admin refresh) now wait
    for each other instead of raising "database is locked" (which used to
    bubble up as an HTML 500 → client-side "not valid JSON").
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
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

    # Migration: add xui_sub_url column (3x-ui subscription source for auto-sync)
    try:
        conn.execute("SELECT xui_sub_url FROM conversion_records LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE conversion_records ADD COLUMN xui_sub_url TEXT DEFAULT ''")
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


def record_conversion(original_links, subscription_urls, yaml_content, client_ip, token, filename, node_count, config_name="", ai_routing=False, ai_japan="", ai_hongkong="", xui_sub_url=""):
    """Insert a conversion record into the database."""
    now = datetime.datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        """INSERT INTO conversion_records
           (created_at, updated_at, original_links, subscription_urls, xui_sub_url, yaml_content, client_ip, token, filename, node_count, config_name, ai_routing, ai_japan, ai_hongkong)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            now,
            original_links,
            subscription_urls,
            xui_sub_url,
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
VIDEO_GROUP_NAME = "视频分流"
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

# Foreign video / streaming service domains routed to the 视频分流 group.
# Chinese video services (bilibili, youku, iqiyi, etc.) are intentionally
# excluded — they fall through to GEO/CN direct rules.
VIDEO_DOMAINS = [
    # YouTube
    "youtube.com", "youtu.be", "googlevideo.com", "ytimg.com",
    "gstatic.com", "ggpht.com",
    # Netflix
    "netflix.com", "nflximg.net", "nflxext.com", "nflxso.net",
    "nflxvideo.net", "netflix.net",
    # Disney+
    "disneyplus.com", "disney-plus.net", "disneyplus.net",
    # TikTok
    "tiktok.com", "tiktokv.com",
    # Spotify
    "spotify.com", "scdn.co",
    # Twitch
    "twitch.tv", "ttvnw.net", "jtvnw.net",
    # Prime Video
    "primevideo.com", "amazon.com",
    # HBO / Max
    "hbo.com", "hbonow.com", "max.com",
    # Apple TV+
    "icloud.com",  # apple TV+ uses icloud CDN
    # Dailymotion
    "dailymotion.com",
    # Vimeo
    "vimeo.com",
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


def _emit_video_rules(lines):
    """Emit DOMAIN-SUFFIX rules for foreign video/streaming -> 视频分流 group."""
    for d in VIDEO_DOMAINS:
        lines.append(f"  - DOMAIN-SUFFIX,{d},{VIDEO_GROUP_NAME}")


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
            _emit_video_rules(lines)
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
            _emit_video_rules(lines)
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
        _emit_video_rules(lines)
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
    if config.get("default_to_auto", True):
        # 默认选中「自动选择」分组（url-test 自动挑最快节点），而非固定节点或直连
        lines.append(f'      - "自动选择"')
    for p in proxies:
        lines.append(f'      - "{p["name"]}"')
    lines.append(f'      - DIRECT')
    lines.append("")

    if ai_routing:
        # ai_preference decides which region is prioritized in each group:
        #   "jp_hk" (default): 默认分流→香港优先, AI分流→日本优先, 视频分流→香港优先
        #   "hk_jp"           : 默认分流→日本优先, AI分流→香港优先, 视频分流→日本优先
        ai_pref = config.get("ai_preference", "jp_hk")
        if ai_pref == "hk_jp":
            default_first, default_second = ai_japan, ai_hongkong   # 默认→日本优先
            ai_first, ai_second = ai_hongkong, ai_japan             # AI→香港优先
            video_first, video_second = ai_japan, ai_hongkong       # 视频→日本优先
        else:
            default_first, default_second = ai_hongkong, ai_japan   # 默认→香港优先
            ai_first, ai_second = ai_japan, ai_hongkong             # AI→日本优先
            video_first, video_second = ai_hongkong, ai_japan       # 视频→香港优先

        # Build member list helper: [region_priority_nodes, all_other_nodes, 节点选择, DIRECT]
        def _build_smart_members(first, second):
            members = []
            if first:
                members.append(first)
            if second:
                members.append(second)
            for p in proxies:
                if p["name"] not in members:
                    members.append(p["name"])
            # Fallback to main select group so manual node selection still works
            # as ultimate fallback when all smart-routing nodes are down.
            members.append(group_name)
            members.append("DIRECT")
            return members

        # 默认分流 group (fallback => first healthy node by health-check)
        default_members = _build_smart_members(default_first, default_second)

        # AI 分流 group (fallback => AI domains → JP/HK smart pick → 节点选择 → DIRECT)
        ai_members = _build_smart_members(ai_first, ai_second)

        # 视频分流 group (fallback => streaming → HK/JP smart pick → 节点选择 → DIRECT)
        video_members = _build_smart_members(video_first, video_second)

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

        lines.append(f'  - name: "{VIDEO_GROUP_NAME}"')
        lines.append(f'    type: fallback')
        lines.append(f'    proxies:')
        for m in video_members:
            lines.append(f'      - "{m}"')
        lines.append(f'    url: {HC_URL}')
        lines.append(f'    interval: {HC_INTERVAL}')
        lines.append(f'    tolerance: {HC_TOLERANCE}')
        lines.append(f'    timeout: {HC_TIMEOUT}')
        lines.append("")

    # Auto-select group (url-test): automatically picks the fastest node.
    # Placed last so it does not shift the proxy-groups index order relied on by
    # the global-template merge (groups[0]=主策略组, groups[1]=AI 策略组).
    lines.append(f'  - name: "自动选择"')
    lines.append(f'    type: url-test')
    lines.append(f'    proxies:')
    for p in proxies:
        lines.append(f'      - "{p["name"]}"')
    lines.append(f'      - DIRECT')
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

def _normalize_xui_sub_url(url):
    """3x-ui convenience rewrite: /clash/<subid> -> /sub/<subid>.

    Users naturally copy the Clash subscription link shown in the 3x-ui
    panel. But the /clash/ endpoint (a) only exists when subClashEnable=true
    and (b) returns Clash YAML, which fetch_subscription cannot parse into
    share links. The /sub/<subid> endpoint on the SAME host:port returns
    base64 share links and is available whenever the subscription service is
    enabled (subEnable, default on) — same subid, zero config. Rewrite so
    pasted /clash/ links just work.
    """
    if "/clash/" in url:
        return url.replace("/clash/", "/sub/", 1)
    return url


def _parse_links_and_subs(raw_links, sub_urls, xui_sub_url=""):
    """Parse raw proxy links and subscription URLs into a list of proxies.

    Shared by /api/convert, /api/admin/records/<id>/edit, and /refresh.
    Returns (proxies, errors).

    If `xui_sub_url` is provided (a 3x-ui subscription link), it is used as the
    sole source of node links — v2c auto-fetches whatever the user configured /
    modified in 3x-ui, so manual re-pasting is no longer needed.
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

    # ---- 3x-ui 订阅优先：自动获取 3x-ui 中已配置/修改的链接 ----
    if xui_sub_url and xui_sub_url.strip():
        for url_line in xui_sub_url.splitlines():
            url = url_line.strip()
            if not url:
                continue
            if not url.startswith("http://") and not url.startswith("https://"):
                errors.append(f"无效 3x-ui 订阅地址: {url[:80]}")
                continue
            try:
                fetch_url = _normalize_xui_sub_url(url)
                links = fetch_subscription(fetch_url)
                if not links:
                    hint = "（已自动把 /clash/ 改写为 /sub/ 仍为空；请检查面板「启用订阅服务」是否开启、该客户端订阅是否有效）" if fetch_url != url else ""
                    errors.append(f"3x-ui 订阅未返回任何节点: {fetch_url[:60]}{hint}")
                for link in links:
                    proxy = parse_proxy(link)
                    if proxy:
                        add_proxy(proxy)
                    else:
                        errors.append(f"3x-ui 节点解析失败: {link[:80]}")
            except Exception as e:
                errors.append(f"3x-ui 订阅获取失败 ({fetch_url[:50]}): {str(e)}")
        return proxies, errors

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
    # version is injected so static assets can be cache-busted with ?v=<version>
    return render_template("index.html", version=APP_VERSION)


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


@app.route("/api/admin/global-template", methods=["GET"])
def admin_global_template_get():
    """Return the raw global template text for the admin editor."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401
    return jsonify({"template": load_global_template()})


@app.route("/api/admin/global-template", methods=["POST"])
def admin_global_template_save():
    """Validate and save the global template (shared part of the config)."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    data = request.get_json(silent=True) or {}
    template = data.get("template")
    if template is None or not str(template).strip():
        return jsonify({"error": "模板内容为空，未保存"}), 400
    template = str(template).replace("\r\n", "\n")

    try:
        parsed = yaml.safe_load(template)
    except yaml.YAMLError as e:
        return jsonify({"error": "YAML 语法错误，未保存：" + str(e)[:300]}), 400
    if not isinstance(parsed, dict):
        return jsonify({"error": "YAML 顶层必须是键值映射，未保存"}), 400
    reserved = [k for k in _TEMPLATE_RESERVED_KEYS if k in parsed]
    if reserved:
        return jsonify({
            "error": "模板里不能写 " + " / ".join(reserved) +
                     "（节点与策略组由每条记录各自保留，自动拼装），未保存"
        }), 400
    if "rules" not in parsed:
        return jsonify({"error": "模板缺少 rules 段（没有规则等同于全局代理），未保存"}), 400
    if not parsed["rules"]:
        return jsonify({"error": "rules 段为空（没有规则等同于全局代理），未保存"}), 400

    save_global_template(template)
    return jsonify({"success": True, "message": "总体配置模板已保存"})


@app.route("/api/admin/global-template/apply", methods=["POST"])
def admin_global_template_apply():
    """Apply the saved global template to every record (token unchanged)."""
    if not is_admin_logged_in():
        return jsonify({"error": "未授权"}), 401

    template = load_global_template()
    try:
        yaml.safe_load(template)  # fail fast on a broken stored template
    except yaml.YAMLError as e:
        return jsonify({"error": "已保存的模板有语法错误，未应用：" + str(e)[:200]}), 400

    conn = get_db()
    rows = conn.execute(
        "SELECT id, filename, yaml_content FROM conversion_records"
    ).fetchall()

    now = datetime.datetime.now().isoformat()
    success, skipped = 0, []
    for row in rows:
        try:
            new_yaml = apply_template_to_record(template, row["yaml_content"] or "")
        except (ValueError, yaml.YAMLError) as e:
            skipped.append({"id": row["id"], "error": str(e)[:100]})
            continue
        try:
            conn.execute(
                "UPDATE conversion_records SET yaml_content = ?, updated_at = ? WHERE id = ?",
                (new_yaml, now, row["id"])
            )
            filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
            with open(filepath, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_yaml)
            success += 1
        except (sqlite3.Error, OSError) as e:
            skipped.append({"id": row["id"], "error": "写入失败：" + str(e)[:100]})

    conn.commit()
    conn.close()

    msg = f"总体配置已应用到 {success} 条记录（节点与策略组保留，token 不变）"
    if skipped:
        msg += f"，{len(skipped)} 条跳过（ID: " + ", ".join(str(s["id"]) for s in skipped) + "）"
    return jsonify({
        "success": True,
        "message": msg,
        "applied": success,
        "skipped": skipped,
    })


@app.route("/api/convert", methods=["POST"])
def convert():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    raw_links = data.get("links", "").strip()
    sub_urls = data.get("subscriptions", "").strip()
    xui_sub_url = data.get("xui_sub_url", "").strip()
    config = data.get("config", {}) or {}
    custom_name = data.get("config_name", "").strip()

    # Inherit global config defaults so every new subscription uses the admin's
    # 总体配置 (basic settings + health-check + AI preference). The page may still
    # override per-generation; AI on/off + node names come from the request below.
    gcfg = load_global_config()
    for key in ("port", "allow_lan", "log_level", "group_name", "rules_mode",
                "ai_preference", "default_to_auto", "hc_url", "hc_interval", "hc_tolerance", "hc_timeout"):
        if key not in config or config.get(key) in (None, ""):
            config[key] = gcfg.get(key)

    proxies, errors = _parse_links_and_subs(raw_links, sub_urls, xui_sub_url=xui_sub_url)

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
        xui_sub_url=xui_sub_url,
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
        cursor = conn.execute("SELECT config_name, xui_sub_url FROM conversion_records WHERE token = ?", (token,))
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
    # Fetch VPS traffic from the 3x-ui panel this config was generated from,
    # so the displayed allocated/used traffic matches *this* subscription.
    # ⚠️ This MUST be best-effort: traffic stats are cosmetic (used/total shown
    # in the client), while the YAML itself is the whole point of the request.
    # Any failure here (panel down, unexpected payload, header encoding issue)
    # must degrade to "no traffic info" instead of breaking the subscription —
    # a Chinese inbound remark used to crash the gunicorn worker mid-response
    # (latin-1 header encode) and every client import failed with an empty reply.
    try:
        gcfg = load_global_config()
        scope_base = row["xui_sub_url"] if (row and "xui_sub_url" in row.keys()) else None
        traffic = _fetch_vps_traffic(gcfg, scope_base=scope_base, yaml_content=yaml_text)
        response.headers["Subscription-Userinfo"] = _ascii_header(
            _format_subscription_userinfo(traffic))
        # Diagnostics: per-node contribution, so you can tell at a glance which
        # VPS/clients were counted (and which were skipped because no panel was
        # configured for them).  Visible via: curl -I http://host/d/<token>
        detail = _ascii_header(_format_traffic_detail(traffic))
        if detail:
            response.headers["X-Traffic-Detail"] = detail
    except Exception:
        try:
            app.logger.exception("[vps-traffic] failed; serving YAML without traffic info")
        except Exception:
            pass
        try:
            response.headers["Subscription-Userinfo"] = "upload=0; download=0; total=0; expire=0"
        except Exception:
            pass
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
        f"""SELECT r.id, r.created_at, r.updated_at, r.original_links, r.subscription_urls, r.xui_sub_url, r.client_ip,
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
        "xui_sub_url": row["xui_sub_url"] if "xui_sub_url" in row.keys() else "",
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
    xui_sub_url = data.get("xui_sub_url", "").strip()
    new_config_name = data.get("config_name", "").strip()
    rules_mode = data.get("rules_mode", "basic")
    ai_routing = bool(data.get("ai_routing", False))
    ai_japan = (data.get("ai_japan", "") or "").strip()
    ai_hongkong = (data.get("ai_hongkong", "") or "").strip()

    if not raw_links and not sub_urls and not xui_sub_url:
        return jsonify({"error": "请输入代理链接、订阅地址或 3x-ui 订阅链接"}), 400

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
    proxies, errors = _parse_links_and_subs(raw_links, sub_urls, xui_sub_url=xui_sub_url)

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
           SET original_links = ?, subscription_urls = ?, xui_sub_url = ?, yaml_content = ?,
               node_count = ?, config_name = ?, ai_routing = ?, ai_japan = ?, ai_hongkong = ?, updated_at = ?
           WHERE id = ?""",
        (raw_links, sub_urls, xui_sub_url, yaml_content, len(proxies), config_name,
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
    xui_sub_url = row["xui_sub_url"] or ""

    # Preserve stored AI routing settings across refresh
    ai_routing = bool(row["ai_routing"])
    ai_japan = row["ai_japan"] or ""
    ai_hongkong = row["ai_hongkong"] or ""

    if not raw_links and not sub_urls and not xui_sub_url:
        conn.close()
        return jsonify({"error": "该记录没有原始链接数据，无法刷新"}), 400

    # Re-parse using stored links
    proxies, errors = _parse_links_and_subs(raw_links, sub_urls, xui_sub_url=xui_sub_url)

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
    # Retry the initial read: if another writer (auto-migrate thread, client
    # pull counter) holds the DB, wait it out instead of returning an HTML 500.
    rows = None
    for attempt in range(3):
        try:
            rows = conn.execute("SELECT * FROM conversion_records").fetchall()
            break
        except sqlite3.OperationalError:
            if attempt == 2:
                conn.close()
                return jsonify({"success": False, "error": "数据库忙，请稍后重试"}), 503
            time.sleep(1)

    success = 0
    ai_enabled = 0
    skipped = []
    now = datetime.datetime.now().isoformat()
    for row in rows:
        record_id = row["id"]
        raw_links = row["original_links"] or ""
        sub_urls = row["subscription_urls"] or ""
        xui_sub_url = row["xui_sub_url"] or ""
        try:
            if not raw_links and not sub_urls and not xui_sub_url:
                skipped.append({"id": record_id, "error": "无原始链接数据"})
                continue
            proxies, _errors = _parse_links_and_subs(raw_links, sub_urls, xui_sub_url=xui_sub_url)
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
                stored_jp = row["ai_japan"] if (row["ai_japan"] in names) else ""
                stored_hk = row["ai_hongkong"] if (row["ai_hongkong"] in names) else ""
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
            # Commit PER RECORD (short transaction). The old code held one
            # write transaction across the whole loop — including network
            # fetches of xui subscriptions — which collided with the
            # auto-migrate thread / pull counters and raised
            # "database is locked" at the final commit → HTML 500.
            conn.commit()
            success += 1
            if cfg["ai_routing"]:
                ai_enabled += 1
        except Exception as e:  # noqa: BLE001 - keep batch going on single failure
            skipped.append({"id": record_id, "error": str(e)[:100]})

    # Per-record commits above mean this is a no-op unless a late error path
    # left pending writes; keep it guarded so a lock can never turn into HTML.
    try:
        conn.commit()
    except sqlite3.OperationalError:
        pass
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


# ---------------------------------------------------------------------------
# Automatic post-upgrade YAML migration
# ---------------------------------------------------------------------------

AUTO_MIGRATE_MARKER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def regenerate_record_yaml(record_id, gcfg=None):
    """Regenerate ONE record's YAML from its stored links using current logic.

    Shared by 「一键全部更新」 and the automatic post-upgrade migration.
    Returns (ok: bool, error_message: str). Token / links / URL unchanged.
    """
    if gcfg is None:
        gcfg = load_global_config()
    basic_cfg = global_basic_config(gcfg)

    conn = get_db()
    row = conn.execute("SELECT * FROM conversion_records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        conn.close()
        return False, "记录不存在"

    raw_links = row["original_links"] or ""
    sub_urls = row["subscription_urls"] or ""
    xui_sub_url = row["xui_sub_url"] if "xui_sub_url" in row.keys() else ""
    xui_sub_url = xui_sub_url or ""

    if not raw_links and not sub_urls and not xui_sub_url:
        conn.close()
        return False, "无原始链接数据"

    proxies, _errors = _parse_links_and_subs(raw_links, sub_urls, xui_sub_url=xui_sub_url)
    if not proxies:
        conn.close()
        return False, "重新解析失败，无有效节点"

    cfg = dict(basic_cfg)
    cfg["ai_routing"] = False
    cfg["ai_japan"] = ""
    cfg["ai_hongkong"] = ""
    if gcfg.get("ai_routing"):
        names = {p["name"] for p in proxies}
        stored_jp = row["ai_japan"] if (row["ai_japan"] in names) else ""
        stored_hk = row["ai_hongkong"] if (row["ai_hongkong"] in names) else ""
        cls = classify_region_nodes(proxies)
        ai_japan = stored_jp or (cls["japan"][0] if cls["japan"] else "")
        ai_hongkong = stored_hk or (cls["hongkong"][0] if cls["hongkong"] else "")
        if ai_japan and ai_hongkong:
            cfg["ai_routing"] = True
            cfg["ai_japan"] = ai_japan
            cfg["ai_hongkong"] = ai_hongkong

    yaml_content = generate_clash_yaml(proxies, cfg)
    filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    now = datetime.datetime.now().isoformat()
    conn.execute(
        """UPDATE conversion_records
           SET yaml_content = ?, node_count = ?, ai_routing = ?, ai_japan = ?,
               ai_hongkong = ?, updated_at = ?
           WHERE id = ?""",
        (yaml_content, len(proxies), 1 if cfg["ai_routing"] else 0,
         cfg["ai_japan"], cfg["ai_hongkong"], now, record_id)
    )
    conn.commit()
    conn.close()
    return True, ""


def _auto_migrate_after_upgrade():
    """Background one-time migration run after a v2c upgrade.

    Stored YAML is generated once and served as-is from /d/<token>, so a newly
    added feature (e.g. the 「自动选择」 group) does NOT appear in existing
    subscriptions until each record is regenerated. Instead of forcing the
    admin to click 「一键全部更新」, detect records generated by an older
    converter (missing 自动选择) and regenerate them automatically in the
    background shortly after startup.

    Runs at most once per APP_VERSION (marker file in data/).
    """
    try:
        time.sleep(3)  # let the web server finish binding first

        marker = os.path.join(AUTO_MIGRATE_MARKER_DIR, f".yaml_migrated_{APP_VERSION}")
        if os.path.exists(marker):
            return

        gcfg = load_global_config()
        if not gcfg.get("default_to_auto", True):
            # 自动选择 is off — nothing to migrate, but remember we checked.
            with open(marker, "w", encoding="utf-8") as f:
                f.write(datetime.datetime.now().isoformat() + "\n")
            return

        conn = get_db()
        rows = conn.execute("SELECT id, yaml_content FROM conversion_records").fetchall()
        conn.close()

        stale = [r["id"] for r in rows if "自动选择" not in (r["yaml_content"] or "")]
        for rid in stale:
            try:
                regenerate_record_yaml(rid, gcfg)
            except Exception:  # noqa: BLE001 - keep going on single failure
                pass

        with open(marker, "w", encoding="utf-8") as f:
            f.write(datetime.datetime.now().isoformat() + "\n")

        try:
            app.logger.info(
                "v2c %s: auto-migrated %d record(s) to the new YAML layout",
                APP_VERSION, len(stale)
            )
        except Exception:
            pass
    except Exception:  # noqa: BLE001 - never break startup
        pass


# ---------------------------------------------------------------------------
# VPS traffic fetching (for Subscription-Userinfo header)
# ---------------------------------------------------------------------------

_traffic_cache = {"data": None, "ts": 0, "ttl": 300}  # cache 5 minutes


def _parse_quota(s):
    """Parse a human quota string like '1TB','500 GB','1024' into bytes. None if invalid."""
    if not s:
        return None
    s = str(s).strip().upper().replace(" ", "")
    for suffix, m in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)]) * m)
            except ValueError:
                return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _normalize_panel_base(u):
    """Strip /sub/, /clash/, /json/ tails + trailing slash + fragment/query,
    lowercase, to compare a 3x-ui panel URL with a subscription link base."""
    import urllib.parse as _up
    u = (u or "").strip()
    if not u:
        return ""
    u = u.split("#", 1)[0].split("?", 1)[0]
    for tail in ("/sub/", "/clash/", "/json/"):
        idx = u.find(tail)
        if idx != -1:
            u = u[:idx]
    return u.rstrip("/").lower()


def _panel_identity(url):
    """Return a port-agnostic panel identity string (scheme + host + path).

    3x-ui exposes the same panel on two ports: *webPort* (UI/login) and
    *subPort* (subscriptions).  A subscription link uses subPort while the
    traffic-source config naturally uses webPort.  Stripping the port lets
    them match as the same panel.
    """
    u = _normalize_panel_base(url)
    if not u:
        return ""
    try:
        p = __import__("urllib.parse", fromlist=[""]).urlparse(u)
        # Rebuild without port: scheme://hostname/path
        base = "%s://%s" % (p.scheme or "http", p.hostname or "")
        if p.path and p.path != "/":
            base += p.path.rstrip("/")
        return base.lower()
    except Exception:
        return u


def _extract_subid(url):
    """Extract the 3x-ui subscription id (subId) from a /sub/, /clash/ or
    /json/ link. e.g. http://host:8284/base/sub/ABC123 -> 'ABC123'."""
    u = (url or "").strip().split("#", 1)[0].split("?", 1)[0]
    for tail in ("/sub/", "/clash/", "/json/"):
        idx = u.find(tail)
        if idx != -1:
            sid = u[idx + len(tail):].strip("/")
            return sid or None
    return None


# --- Panel query cache -------------------------------------------------------
# /d/<token> queries every configured 3x-ui panel synchronously. Without a
# cache, one slow/unreachable panel adds up to 3 x timeout seconds to EVERY
# subscription fetch, and Clash Party aborts long downloads with
# "failed to fetch remote profile". Cache successful panel payloads for
# PANEL_CACHE_TTL and remember failures briefly (negative cache) so a dead
# panel costs at most one short timeout per window instead of per request.
_PANEL_CACHE = {}  # (url, user, pwd) -> (timestamp, payload_or_None)
_PANEL_CACHE_TTL = 90  # seconds for successful queries
_PANEL_CACHE_TTL_FAIL = 30  # seconds for failed queries
_PANEL_REQ_TIMEOUT = 4  # per-HTTP-request timeout (was 10s x3 per panel)


def _fetch_panel_inbounds(url, user, pwd):
    """Log into a 3x-ui panel (CSRF token + session) and return the parsed
    inbounds-list payload dict, or None on any failure.

    Results are cached: success for _PANEL_CACHE_TTL, failure for
    _PANEL_CACHE_TTL_FAIL, so subscription fetches never block on panels.
    Admin actions that need fresh data can call _fetch_panel_inbounds_fresh().
    """
    import time as _time
    key = (url, user, pwd)
    now = _time.time()
    hit = _PANEL_CACHE.get(key)
    if hit is not None:
        ts, payload = hit
        ttl = _PANEL_CACHE_TTL if payload is not None else _PANEL_CACHE_TTL_FAIL
        if now - ts < ttl:
            return payload
    data = _fetch_panel_inbounds_fresh(url, user, pwd)
    _PANEL_CACHE[key] = (now, data)
    return data


def _fetch_panel_inbounds_fresh(url, user, pwd):
    """Uncached panel login + inbounds list (the real worker)."""
    import json as _json
    import urllib.request as _urllib_req
    try:
        _jar = {}

        def _save_cookies(resp):
            for h in (resp.headers.get_all("Set-Cookie") or []):
                try:
                    kv = h.split(";", 1)[0]
                    k, v = kv.split("=", 1)
                    _jar[k.strip()] = v.strip()
                except Exception:
                    pass

        def _cookie_header():
            return "; ".join("%s=%s" % (k, v) for k, v in _jar.items())

        req_csrf = _urllib_req.Request("%s/csrf-token" % url, method="GET")
        resp_csrf = _urllib_req.urlopen(req_csrf, timeout=_PANEL_REQ_TIMEOUT)
        _save_cookies(resp_csrf)
        csrf_body = _json.loads(resp_csrf.read())
        csrf_token = (csrf_body.get("obj") or "") if isinstance(csrf_body, dict) else ""
        if not csrf_token:
            return None
        login_data = _json.dumps({"username": user, "password": pwd}).encode()
        req_login = _urllib_req.Request("%s/login" % url, data=login_data, method="POST")
        req_login.add_header("Content-Type", "application/json")
        req_login.add_header("X-CSRF-Token", csrf_token)
        if _cookie_header():
            req_login.add_header("Cookie", _cookie_header())
        resp_login = _urllib_req.urlopen(req_login, timeout=_PANEL_REQ_TIMEOUT)
        _save_cookies(resp_login)
        login_body = _json.loads(resp_login.read())
        if not isinstance(login_body, dict) or not login_body.get("success"):
            return None
        req_list = _urllib_req.Request("%s/panel/api/inbounds/list" % url, method="GET")
        if _cookie_header():
            req_list.add_header("Cookie", _cookie_header())
        resp_list = _urllib_req.urlopen(req_list, timeout=_PANEL_REQ_TIMEOUT)
        data = _json.loads(resp_list.read())
        if not isinstance(data, dict) or not data.get("success"):
            return None
        return data
    except Exception:
        return None


def _ib_settings(ib):
    """Return the parsed `settings` dict of a 3x-ui inbound.

    The inbounds-list API returns `settings` as a JSON-serialized STRING
    (gorm JSON column), not a dict — parse it defensively.
    """
    import json as _json
    s = ib.get("settings")
    if isinstance(s, str) and s.strip():
        try:
            v = _json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return s if isinstance(s, dict) else {}


def _extract_yaml_proxy_targets(yaml_text):
    """Parse a Clash/Mihomo YAML and return {(server, port): set(uuids)}.

    Used for client-level traffic matching without subscription links:
    each proxy's uuid equals the 3x-ui client `id` in settings.clients, so we
    can resolve the exact client (and its clientStats) for every node.
    """
    import yaml as _yaml
    targets = {}
    try:
        cfg = _yaml.safe_load(yaml_text)
        if not isinstance(cfg, dict):
            return targets
        for p in (cfg.get("proxies") or []):
            if not isinstance(p, dict):
                continue
            s = str(p.get("server", "") or "")
            port = p.get("port")
            uuid = str(p.get("uuid", "") or "")
            if not s or port is None:
                continue
            try:
                key = (s, int(port))
            except (ValueError, TypeError):
                continue
            targets.setdefault(key, set())
            if uuid:
                targets[key].add(uuid)
    except Exception:
        pass
    return targets


def _fetch_vps_traffic(gcfg, scope_base=None, yaml_content=None):
    """Aggregate traffic for the subscription this config was generated from.

    3x-ui v3.4.2 needs a CSRF token for POST /login, so we fetch /csrf-token
    then POST /login with the X-CSRF-Token header.

    `scope_base` is the record's `xui_sub_url`, which may hold MULTIPLE
    subscription links (one per VPS / inbound). Each link is matched to its
    panel by host:port+webBasePath, and to its specific client by the subId
    embedded in the link. Only that client's up/down/total count, so a token
    spanning three 200G clients shows 600G -- not the whole panels' 2T.

    When scope_base is empty (vless-link tokens have no xui_sub_url) and
    `yaml_content` is provided, we extract each proxy's (server, port) from
    the YAML and match against inbound listen addresses/ports instead of
    falling back to summing every configured panel's inbounds.

    Returns dict {"upload", "download", "total"} (bytes) or None on failure.
    Results are cached for `ttl` seconds keyed by scope_base.
    """
    import time as _time
    import re as _re
    now = _time.time()
    _cache_key = scope_base
    if yaml_content and not _cache_key:
        # For vless-link tokens (empty scope_base), hash YAML content so
        # different subscriptions don't share the same cache entry.
        import hashlib as _hashlib
        _cache_key = "yaml:" + _hashlib.md5(yaml_content.encode("utf-8", errors="replace")).hexdigest()[:12]
    if (_traffic_cache["data"] is not None and now - _traffic_cache["ts"] < _traffic_cache["ttl"]
            and _traffic_cache.get("scope") == _cache_key):
        return _traffic_cache["data"]

    sources = gcfg.get("vps_traffic_sources") or []
    if not sources:
        return None

    # Resolve which (panel, subId) pairs this subscription covers.
    targets = []  # list of (source_dict, subid_or_None)
    if scope_base:
        links = [l.strip() for l in _re.split(r"[\r\n,;]+", scope_base) if l.strip()]
        for link in links:
            pb = _normalize_panel_base(link)
            sid = _extract_subid(link)
            link_identity = _panel_identity(link)
            for src in sources:
                if _panel_identity(src.get("url")) == link_identity:
                    targets.append((src, sid))
                    break
        if not targets:
            app.logger.warning(
                "[vps-traffic] no panel matched for scope (trying YAML proxy match): "
                "links=%s source_identities=%s",
                links[:3], [_panel_identity(s.get("url")) for s in sources])
            # Don't fall back to sum-all (gives misleading 1.85T for vless tokens).
            # Instead, pass empty targets — the inbound loop below will use
            # yaml_content-based (server,port) matching if available.
            targets = []
        else:
            app.logger.info(
                "[vps-traffic] matched %d link(s) for this token", len(targets))
    else:
        # No xui_sub_url (vless-link token) — will try YAML proxy matching below
        targets = []

    total_up = 0
    total_down = 0
    panel_alloc = 0
    quota_override = 0
    saw_any = False
    _matched_nodes = []  # per-node breakdown for diagnostics

    # When we have no subId links (vless-link tokens), try YAML proxy matching
    _yaml_targets = None
    if not targets and yaml_content:
        _yaml_targets = _extract_yaml_proxy_targets(yaml_content)
        if _yaml_targets:
            app.logger.info(
                "[vps-traffic] YAML proxy match: %d unique (server,port) extracted",
                len(_yaml_targets))
            # Query all panels — we'll filter inbounds by (server,port) below
            targets = [(s, None) for s in sources]
        else:
            app.logger.warning("[vps-traffic] YAML has no parseable proxies")

    if not targets:  # Nothing to do at all
        return None

    try:
        _queried = {}  # panel url -> inbounds payload (de-dup queries)
        _counted = set()  # (panel_url, inbound_id, client_email) guard vs double count
        for src, sid in targets:
            url = (src.get("url") or "").rstrip("/")
            user = src.get("username", "")
            pwd = src.get("password", "")
            if not url:
                continue
            if url in _queried:
                data = _queried[url]
            else:
                data = _fetch_panel_inbounds(url, user, pwd)
                _queried[url] = data
            if data is None:
                continue
            saw_any = True
            q = _parse_quota(src.get("quota"))
            if q:
                quota_override += q

            from urllib.parse import urlparse as _uparse
            panel_host = ""
            try:
                panel_host = _uparse(url).hostname or ""
            except Exception:
                pass

            for ib in (data.get("obj") or []):
                if not isinstance(ib, dict):
                    continue
                ib_settings = _ib_settings(ib)
                clients = [c for c in (ib_settings.get("clients") or [])
                           if isinstance(c, dict)]
                stats = [c for c in (ib.get("clientStats") or [])
                         if isinstance(c, dict)]
                ib_key = str(ib.get("id") or ib.get("port") or id(ib))

                # ---- Accumulation helpers (nonlocal into this function) ----
                # We collect (up, down, total) contributions and add at the end
                # of each inbound iteration.

                contrib_up = contrib_down = contrib_tot = 0
                counted_any = False

                def _add_client_stats(clist):
                    nonlocal contrib_up, contrib_down, contrib_tot, counted_any  # noqa
                    got = False
                    for c in clist:
                        key = (url, ib_key, c.get("email") or c.get("subId") or id(c))
                        if key in _counted:
                            continue
                        _counted.add(key)
                        contrib_up += c.get("up", 0) or 0
                        contrib_down += c.get("down", 0) or 0
                        contrib_tot += c.get("total", 0) or 0
                        got = True
                    if got:
                        counted_any = True

                def _add_alloc_only(clist):
                    """Client known but no clientStats row yet — still show its
                    configured quota (totalGB) so the allocation isn't lost."""
                    nonlocal contrib_tot, counted_any  # noqa
                    got = False
                    for c in clist:
                        key = (url, ib_key, c.get("email") or c.get("subId") or id(c))
                        if key in _counted:
                            continue
                        _counted.add(key)
                        contrib_tot += c.get("totalGB", 0) or 0
                        got = True
                    if got:
                        counted_any = True

                def _add_inbound_level():
                    nonlocal contrib_up, contrib_down, contrib_tot, counted_any  # noqa
                    key = (url, ib_key, "__inbound__")
                    if key in _counted:
                        return
                    _counted.add(key)
                    contrib_up += ib.get("up", 0) or 0
                    contrib_down += ib.get("down", 0) or 0
                    contrib_tot += ib.get("total", 0) or 0
                    for c in stats:
                        ck = (url, ib_key, c.get("email") or c.get("subId") or id(c))
                        if ck in _counted:
                            continue
                        _counted.add(ck)
                        contrib_up += c.get("up", 0) or 0
                        contrib_down += c.get("down", 0) or 0
                        contrib_tot += c.get("total", 0) or 0
                    counted_any = True

                # ---- Path 1: subId from subscription links ----
                if sid is not None:
                    matched = [c for c in stats if c.get("subId") == sid]
                    if matched:
                        _add_client_stats(matched)
                    else:
                        # sid -> settings.clients -> email -> clientStats
                        via_clients = [c for c in clients if c.get("subId") == sid]
                        if via_clients:
                            emails = {c.get("email") for c in via_clients}
                            m2 = [c for c in stats if c.get("email") in emails]
                            if m2:
                                _add_client_stats(m2)
                            else:
                                _add_alloc_only(via_clients)
                        else:
                            ib_sub = ib_settings.get("subId") or ib.get("subId")
                            if ib_sub == sid:
                                _add_inbound_level()
                            # else: fall through to uuid matching below

                # ---- Path 2: YAML proxy uuid matching (client-level) ----
                if not counted_any and _yaml_targets:
                    ib_port = ib.get("port")
                    if ib_port is not None:
                        try:
                            p = int(ib_port)
                        except (ValueError, TypeError):
                            p = None
                        if p is not None:
                            match_key = None
                            # exact (listen/stream host, port)
                            real_host = ib.get("listen", "") or ""
                            ss = ib_settings  # settings may hold streamSettings
                            stream = (ss.get("streamSettings") or {}) if isinstance(ss, dict) else {}
                            if isinstance(stream, dict):
                                for net_key in ("ws", "grpc", "http", "tcp"):
                                    net_cfg = stream.get(net_key) or {}
                                    if isinstance(net_cfg, dict):
                                        h = net_cfg.get("server") or net_cfg.get("dest") or ""
                                        if h:
                                            real_host = str(h)
                                            break
                            for key in ((str(real_host), p), (panel_host, p)):
                                if key in _yaml_targets:
                                    match_key = key
                                    break
                            if match_key is not None:
                                uuids = _yaml_targets[match_key]
                                # clientStats may carry uuid directly
                                direct = [c for c in stats
                                          if c.get("uuid") and c.get("uuid") in uuids]
                                # resolve via settings.clients: id(uuid) -> email/subId
                                via_uuid = [c for c in clients if c.get("id") in uuids]
                                if direct or via_uuid:
                                    emails = ({c.get("email") for c in direct} |
                                              {c.get("email") for c in via_uuid})
                                    subids = {c.get("subId") for c in via_uuid}
                                    m2 = [c for c in stats
                                          if c.get("email") in emails or c.get("subId") in subids]
                                    if m2:
                                        _add_client_stats(m2)
                                    else:
                                        _add_alloc_only(via_uuid or direct)
                                else:
                                    # Can't pin the exact client — inbound-level
                                    # fallback (best we can do without identity)
                                    _add_inbound_level()

                if counted_any:
                    total_up += contrib_up
                    total_down += contrib_down
                    panel_alloc += contrib_tot
                    # Per-node breakdown for diagnostics (X-Traffic-Detail header)
                    _matched_nodes.append({
                        "panel": url,
                        "node": ib.get("remark") or "",
                        # Fallback label for the diagnostics header: remarks are
                        # often Chinese, and gunicorn latin-1 encodes headers.
                        "host": panel_host or "",
                        "port": ib.get("port"),
                        "up": contrib_up,
                        "down": contrib_down,
                        "total": contrib_tot,
                    })
                # neither sid nor yaml matched this inbound -> skip entirely

        # Warn about YAML proxies whose (server, port) never matched a panel —
        # this is why a token spanning 3 VPS used to report only 1 node's quota.
        if _yaml_targets and _matched_nodes:
            _hit = {(n.get("port")) for n in _matched_nodes}
            _miss = [f"{h}:{p}" for (h, p) in _yaml_targets if p not in _hit]
            if _miss:
                app.logger.warning(
                    "[vps-traffic] %d YAML proxy(s) had NO configured panel: %s "
                    "-> add them to 总体配置/ VPS 流量源", len(_miss), _miss[:6])

        result = {
            "upload": total_up,
            "download": total_down,
            "total": (panel_alloc if panel_alloc > 0 else quota_override),
            "nodes": _matched_nodes,
        } if saw_any else None
        _traffic_cache["data"] = result
        _traffic_cache["ts"] = now
        _traffic_cache["scope"] = _cache_key
        return result
    except Exception:  # noqa: BLE001
        return None


def _human_bytes(n):
    """Compact human readable size for diagnostics output."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.2f}{unit}"
        n /= 1024.0
    return f"{n:.2f}PB"


def _ascii_header(s):
    """Make a string safe for an HTTP header value.

    gunicorn writes response headers with `.encode('latin-1')`
    (gunicorn/http/wsgi.py). Any non-ASCII character — e.g. a Chinese 3x-ui
    inbound remark — raises UnicodeEncodeError while the headers are being
    written, the worker dies mid-response and the client sees
    "Empty reply from server" (curl 52) => Clash import failure.
    Also strip CR/LF to prevent header injection.
    """
    s = (s or "").replace("\r", " ").replace("\n", " ")
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return s.encode("ascii", "replace").decode("ascii")


def _format_traffic_detail(traffic):
    """Build the X-Traffic-Detail header: which node contributed how much.

    Example:
      HK:39999 used=10.00GB quota=200.00GB; JP:29214 used=1.00GB quota=200.00GB
    Lets you verify with `curl -I` that every node in the YAML is counted
    (a missing node means its panel isn't in 总体配置 → VPS 流量源).

    ⚠️ Must stay ASCII-only: gunicorn latin-1 encodes header values, so a
    Chinese inbound remark here would kill the worker (empty reply).
    """
    if not traffic:
        return ""
    nodes = traffic.get("nodes") or []
    if not nodes:
        return ""
    parts = []
    for n in nodes:
        label = n.get("node") or ""
        try:
            label.encode("latin-1")
        except UnicodeEncodeError:
            # Chinese remark — fall back to the panel host so the entry stays
            # readable in `curl -I` output instead of "??".
            label = n.get("host") or ""
        label = _ascii_header(label)
        port = n.get("port")
        name = f"{label}:{port}" if label else str(port or "?")
        used = (n.get("up") or 0) + (n.get("down") or 0)
        parts.append(f"{name} used={_human_bytes(used)} quota={_human_bytes(n.get('total'))}")
    return _ascii_header("; ".join(parts))


def _format_subscription_userinfo(traffic):
    """Format traffic dict into Subscription-Userinfo header value.

    `total` is the panel-configured traffic limit (bytes); 0 means no limit was
    set on the 3x-ui panel, so we do NOT fake a total from the used amount.
    """
    if traffic is None:
        return "upload=0; download=0; total=0; expire=0"
    up = traffic.get("upload", 0) or 0
    down = traffic.get("download", 0) or 0
    total = traffic.get("total", 0) or 0
    return f"upload={up}; download={down}; total={total}; expire=0"


def _run_auto_update_once(gcfg):
    """Re-fetch & regenerate every record that carries a subscription."""
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT id FROM conversion_records
               WHERE (xui_sub_url IS NOT NULL AND xui_sub_url != '')
                  OR (subscription_urls IS NOT NULL AND subscription_urls != '')"""
        ).fetchall()
        conn.close()
        updated = 0
        for r in rows:
            try:
                ok, _err = regenerate_record_yaml(r["id"], gcfg)
                if ok:
                    updated += 1
            except Exception:  # noqa: BLE001
                pass
        try:
            app.logger.info(
                "v2c %s: scheduled auto-update refreshed %d subscription record(s)",
                APP_VERSION, updated
            )
        except Exception:
            pass
    except Exception:  # noqa: BLE001
        pass


def _scheduled_auto_update():
    """Background scheduler for subscription records.

    Respects global config keys auto_update_enabled / auto_update_interval_hours.
    Only one worker/master runs it (exclusive file lock); config changes are
    picked up on the next cycle (sliced sleep, so no full restart needed).
    """
    try:
        import fcntl
    except ImportError:
        return  # non-Linux (e.g. local Windows smoke test) — skip scheduler
    lock_fd = None
    try:
        lock_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", ".autoupdate_scheduler.lock"
        )
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another worker already holds the lock

    global _auto_update_first_done
    try:
        while True:
            try:
                gcfg = load_global_config()
                if gcfg.get("auto_update_enabled"):
                    _run_auto_update_once(gcfg)
                interval_h = max(1, int(gcfg.get("auto_update_interval_hours", 6) or 6))
                total = interval_h * 3600
                if not _auto_update_first_done:
                    total = min(total, 120)  # first pass ~2 min after start
                    _auto_update_first_done = True
                slices = max(1, int(total // 300))
                for _ in range(slices):
                    time.sleep(300)
            except Exception:  # noqa: BLE001
                time.sleep(60)
    finally:
        try:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:
            pass


_auto_update_first_done = False


# Kick off the migration in a daemon thread so it never blocks startup.
# (gunicorn --preload imports this module once; the thread runs right after.)
try:
    threading.Thread(target=_auto_migrate_after_upgrade, daemon=True).start()
except Exception:  # noqa: BLE001
    pass


# Kick off the scheduled auto-update in a daemon thread (single instance).
try:
    threading.Thread(target=_scheduled_auto_update, daemon=True).start()
except Exception:  # noqa: BLE001
    pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reset-admin":
        reset_admin()
        print("Admin credentials reset to: admin / admin123")
        print(f"Config file: {ADMIN_CONFIG_FILE}")
    else:
        app.run(host="0.0.0.0", port=5000, debug=False)
