"""v1.6.48 节点地址覆写 (server_overrides) 测试.

场景: 源站 IP 被 GFW 干扰, 通过 Cloudflare 域名中转.
关键约束(来自 2026-10-28 实测):
  - CF 免费版不支持自定义回源端口, 客户端连哪个端口 CF 就回源哪个端口
  - 连 443 -> 回源 185:443 (REALITY 入站) -> WS 握手 400
  - 连 8443 -> 回源 185:8443 (WS+TLS 入站) -> 通
所以覆写必须同时改 server/port/sni/host, 只改 server 会打回 REALITY 入站.
"""
import sys, os
sys.path.insert(0, r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')
os.chdir(r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')

from app import apply_server_overrides, parse_vless, generate_clash_yaml, _parse_links_and_subs

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  PASS", name)
    else: FAIL += 1; print("  FAIL", name, detail)

# 真实 8443 入站链接 (ws + tls, 自签证书 CN=IP)
L8443 = ("vless://c8100fc5-1f64-497c-944c-f22b298b5c03@185.115.207.27:8443"
         "?alpn=http%2F1.1&encryption=none&security=tls&sni=&type=ws&path=%2Fws"
         "&host=&fp=chrome#%E9%A6%99%E6%B8%AF-TLS")
# 真实 443 REALITY 链接 (对照: 不应被 8443 规则命中)
L443 = ("vless://93ba80ff-5db8-466b-bf33-5385b4915132@185.115.207.27:443"
        "?encryption=none&security=reality&sni=www.microsoft.com"
        "&pbk=74_1_uEKuRxiUbawJhnxTQo-vwZrJT1MsmytyA1HbCo&sid=dfe1f4db389e5ce8"
        "&type=tcp&fp=chrome#REAL")

CF_RULE = {
    "from_server": "185.115.207.27",
    "from_port": 8443,
    "to_server": "3x.820804.xyz",
    "to_port": 8443,
    "sni": "3x.820804.xyz",
    "host": "3x.820804.xyz",
    "skip_cert_verify": False,
}

print("=" * 25, "T1 命中规则: server/port 全改", "=" * 25)
p = parse_vless(L8443)
check("解析成功", p is not None)
check("改前 server=IP", p["server"] == "185.115.207.27", p["server"])
check("改前 port=8443", p["port"] == 8443, str(p["port"]))
p2 = apply_server_overrides(dict(p), [CF_RULE])
check("server -> CF 域名", p2["server"] == "3x.820804.xyz", p2["server"])
check("port 保持 8443 (不能变 443)", p2["port"] == 8443, str(p2["port"]))
check("sni -> CF 域名", p2.get("servername") == "3x.820804.xyz", str(p2.get("servername")))
check("ws Host -> CF 域名", p2.get("ws-opts", {}).get("headers", {}).get("Host") == "3x.820804.xyz",
      str(p2.get("ws-opts")))
check("skip-cert-verify 被移除 (False -> 不输出该键)", "skip-cert-verify" not in p2,
      str(p2.get("skip-cert-verify")))

print("=" * 25, "T2 REALITY 节点不受影响 (端口不匹配)", "=" * 25)
r = parse_vless(L443)
r2 = apply_server_overrides(dict(r), [CF_RULE])
check("REALITY server 未改", r2["server"] == "185.115.207.27", r2["server"])
check("REALITY port 未改", r2["port"] == 443, str(r2["port"]))
check("REALITY sni 未改", r2.get("servername") == "www.microsoft.com", str(r2.get("servername")))

print("=" * 25, "T3 空规则 / 空列表 = 不改写", "=" * 25)
p3 = apply_server_overrides(dict(p), [])
check("空列表不改 server", p3["server"] == "185.115.207.27", p3["server"])
p4 = apply_server_overrides(dict(p), None)
check("None 不改 server", p4["server"] == "185.115.207.27", p4["server"])
p5 = apply_server_overrides(dict(p), [{"to_server": "x.example.com"}])
check("缺 from_server 不命中", p5["server"] == "185.115.207.27", p5["server"])
p6 = apply_server_overrides(dict(p), [{"from_server": "185.115.207.27"}])
check("缺 to_server 不命中", p6["server"] == "185.115.207.27", p6["server"])

print("=" * 25, "T4 首条命中即生效", "=" * 25)
r1 = {"from_server": "185.115.207.27", "to_server": "first.example.com"}
r2b = {"from_server": "185.115.207.27", "to_server": "second.example.com"}
p7 = apply_server_overrides(dict(p), [r1, r2b])
check("取第一条规则", p7["server"] == "first.example.com", p7["server"])

print("=" * 25, "T5 只写 from_server (不限端口) 也可命中", "=" * 25)
p8 = apply_server_overrides(dict(p), [{"from_server": "185.115.207.27", "to_server": "any.example.com"}])
check("server 被改", p8["server"] == "any.example.com", p8["server"])
check("port 未指定则保留", p8["port"] == 8443, str(p8["port"]))

print("=" * 25, "T5b skip_cert_verify=true 时保留该键", "=" * 25)
p8b = apply_server_overrides(dict(p), [{"from_server": "185.115.207.27",
                                        "to_server": "any.example.com",
                                        "skip_cert_verify": True}])
check("skip-cert-verify=True 保留", p8b.get("skip-cert-verify") is True,
      str(p8b.get("skip-cert-verify")))
y8b = generate_clash_yaml([p8b], {"rules_mode": "basic"})
check("YAML 输出 skip-cert-verify: true", "skip-cert-verify: true" in y8b)

print("=" * 25, "T6 YAML 输出正确", "=" * 25)
y = generate_clash_yaml([p2], {"rules_mode": "basic"})
check("YAML server 是 CF 域名", "server: 3x.820804.xyz" in y, y[:400])
check("YAML port 8443", "port: 8443" in y)
check("YAML servername CF 域名", "servername: 3x.820804.xyz" in y)
check("YAML Host CF 域名", "3x.820804.xyz" in y)
check("YAML 无 skip-cert-verify", "skip-cert-verify" not in y, y[:400])

print("=" * 25, "T7 走完整解析链路 (add_proxy 应用覆写)", "=" * 25)
proxies, errors, groups = _parse_links_and_subs(L8443, "", overrides=[CF_RULE])
check("解析出 1 个节点", len(proxies) == 1, str(len(proxies)))
if proxies:
    check("链路内已覆写 server", proxies[0]["server"] == "3x.820804.xyz", proxies[0]["server"])
    check("链路内已覆写 sni", proxies[0].get("servername") == "3x.820804.xyz")
proxies2, _, _ = _parse_links_and_subs(L8443, "", overrides=[])
check("无规则时保持原样", proxies2[0]["server"] == "185.115.207.27", proxies2[0]["server"])

print()
print(f"===== 结果: {PASS} PASS / {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
