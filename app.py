#!/usr/bin/env python3
"""
VLESS to Clash Meta YAML Converter
Flask Web Application for converting VLESS links to Clash/Mihomo Party compatible YAML
"""

import re
import os
import glob
import base64
import requests
from urllib.parse import unquote, parse_qs
from flask import Flask, render_template, request, jsonify, Response, send_from_directory

app = Flask(__name__)

# Directory for saving generated YAML files
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


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
        # Remove whitespace/newlines for base64 decoding
        b64_content = content.replace("\n", "").replace("\r", "").replace(" ", "")
        decoded = base64.b64decode(b64_content).decode("utf-8")
        # Check if decoded content looks like proxy links
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
# File management
# ---------------------------------------------------------------------------

def get_next_filename():
    """Get the next incrementing filename: 00001.yaml, 00002.yaml, ..."""
    existing = glob.glob(os.path.join(DOWNLOADS_DIR, "*.yaml"))
    max_num = 0
    for f in existing:
        basename = os.path.basename(f)
        name_part = basename.rsplit(".", 1)[0]
        try:
            num = int(name_part)
            if num > max_num:
                max_num = num
        except ValueError:
            continue
    return f"{max_num + 1:05d}.yaml"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


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

    # Save to file with incrementing name
    filename = get_next_filename()
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    # Build download URL
    download_url = f"/files/{filename}"

    return jsonify({
        "yaml": yaml_content,
        "count": len(proxies),
        "errors": errors,
        "filename": filename,
        "download_url": download_url,
        "proxies": [{"name": p["name"], "server": p["server"], "port": p["port"]} for p in proxies],
    })


@app.route("/files/<path:filename>")
def serve_file(filename):
    """Serve a saved YAML file (also works as Clash subscription import URL)."""
    return send_from_directory(DOWNLOADS_DIR, filename, mimetype="text/yaml")


@app.route("/api/files")
def list_files():
    """List all saved YAML files."""
    files = sorted(glob.glob(os.path.join(DOWNLOADS_DIR, "*.yaml")))
    result = []
    for f in files:
        basename = os.path.basename(f)
        result.append({
            "filename": basename,
            "url": f"/files/{basename}",
            "size": os.path.getsize(f),
        })
    return jsonify({"files": result})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
