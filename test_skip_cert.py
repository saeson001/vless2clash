"""v1.6.44 skip-cert-verify 测试: 自签 TLS 节点(8443 入站场景)"""
import sys, os
sys.path.insert(0, r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')
os.chdir(r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')

from app import parse_vless, generate_clash_yaml

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  PASS", name)
    else: FAIL += 1; print("  FAIL", name, detail)

# 8443 入站的真实链接格式 (sni 为空)
L8443 = ("vless://c8100fc5-1f64-497c-944c-f22b298b5c03@185.115.207.27:8443"
         "?alpn=http%2F1.1&encryption=none&security=tls&sni=&type=tcp#%E9%A6%99%E6%B8%AF-TLS")
# 域名 TLS (无 allowInsecure) — server 是域名才不触发 IP 自签启发式
LDM = ("vless://uuid-d@example.com:443?encryption=none&security=tls&sni=example.com&type=tcp#%E5%9F%9F%E5%90%8D-TLS")
# 域名 TLS + allowInsecure=1
LDMI = ("vless://uuid-d2@example.com:443?encryption=none&security=tls&sni=example.com&allowInsecure=1&type=tcp#%E5%9F%9F%E5%90%8D-INSEC")
# REALITY (对照)
LREAL = ("vless://uuid-r@185.115.207.27:443?type=tcp&encryption=none&security=reality"
         "&sni=www.microsoft.com&pbk=PBK1&sid=111111&fp=chrome#REAL")

print("=" * 25, "T1 8443 自签 TLS (IP 服务器)", "=" * 25)
p1 = parse_vless(L8443)
check("解析成功", p1 is not None)
check("skip-cert-verify=True", p1.get("skip-cert-verify") is True, str(p1.get("skip-cert-verify")))
check("空 sni 不产生 servername", "servername" not in p1, str(p1.get("servername")))
y1 = generate_clash_yaml([p1], {"rules_mode": "basic"})
check("YAML 输出 skip-cert-verify: true", "skip-cert-verify: true" in y1)

print("=" * 25, "T2 域名 TLS 无 allowInsecure", "=" * 25)
p2 = parse_vless(LDM)
check("无 skip-cert-verify", "skip-cert-verify" not in p2, str(p2.get("skip-cert-verify")))
y2 = generate_clash_yaml([p2], {"rules_mode": "basic"})
check("YAML 无 skip-cert-verify", "skip-cert-verify" not in y2)

print("=" * 25, "T3 域名 TLS + allowInsecure=1", "=" * 25)
p3 = parse_vless(LDMI)
check("skip-cert-verify=True", p3.get("skip-cert-verify") is True)

print("=" * 25, "T4 REALITY 对照", "=" * 25)
p4 = parse_vless(LREAL)
check("REALITY 无 skip-cert-verify", "skip-cert-verify" not in p4)

print("=" * 25, "T5 8443 节点 mihomo 可解析形态", "=" * 25)
y5 = generate_clash_yaml([p1], {"rules_mode": "basic", "mode": "global"})
check("tls: true", "tls: true" in y5)
check("无 reality-opts", "reality-opts" not in y5)
check("servername 缺省(用 server 地址)", "servername:" not in y5)

print()
print(f"===== 结果: {PASS} PASS / {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
