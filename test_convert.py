#!/usr/bin/env python3
"""Test script: verify VLESS parsing and YAML generation"""

import sys
sys.path.insert(0, '.')
from app import parse_vless, generate_clash_yaml

# User's example VLESS link
vless_link = "vless://c164c8aa-3db8-49bb-9675-bdbfc2ecdb39@38.47.108.240:57613?type=tcp&encryption=none&security=reality&sni=apple.com&pbk=53eqWPu-fQR8tPXoSc5tLZ1wCgyIExpt04e3ZDMQ2i8&sid=a290181c&fp=chrome#%E5%88%98%E5%BC%A0%E8%88%AA%E7%88%B8%E7%88%B8"

# Expected YAML output
expected_yaml = """mixed-port: 7890
allow-lan: true
mode: rule
log-level: info

proxies:
  - name: "刘张航爸爸"
    type: vless
    server: 38.47.108.240
    port: 57613
    uuid: c164c8aa-3db8-49bb-9675-bdbfc2ecdb39
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: apple.com
    reality-opts:
      public-key: 53eqWPu-fQR8tPXoSc5tLZ1wCgyIExpt04e3ZDMQ2i8
      short-id: a290181c
    client-fingerprint: chrome

proxy-groups:
  - name: "节点选择"
    type: select
    proxies:
      - "刘张航爸爸"
      - DIRECT

rules:
  - MATCH,节点选择
"""

print("=" * 60)
print("Testing VLESS parsing...")
print("=" * 60)

proxy = parse_vless(vless_link)
print(f"\nParsed proxy: {proxy}")

print("\n" + "=" * 60)
print("Generating YAML...")
print("=" * 60)

config = {
    "port": 7890,
    "allow_lan": True,
    "mode": "rule",
    "log_level": "info",
    "group_name": "节点选择",
}

yaml_output = generate_clash_yaml([proxy], config)
print(f"\nGenerated YAML:\n")
print(yaml_output)

print("=" * 60)
print("Comparing with expected output...")
print("=" * 60)

if yaml_output.strip() == expected_yaml.strip():
    print("\n✅ PASS: Output matches expected YAML exactly!")
else:
    print("\n❌ FAIL: Output does not match expected YAML")
    print("\n--- Expected ---")
    print(expected_yaml)
    print("--- Got ---")
    print(yaml_output)
    
    # Show line-by-line diff
    expected_lines = expected_yaml.strip().split('\n')
    got_lines = yaml_output.strip().split('\n')
    print("\n--- Line-by-line diff ---")
    for i, (e, g) in enumerate(zip(expected_lines, got_lines)):
        if e != g:
            print(f"  Line {i+1}:")
            print(f"    Expected: {repr(e)}")
            print(f"    Got:      {repr(g)}")
    if len(expected_lines) != len(got_lines):
        print(f"  Line count: expected={len(expected_lines)}, got={len(got_lines)}")

# Test multiple links
print("\n" + "=" * 60)
print("Testing multiple VLESS links...")
print("=" * 60)

links = [
    "vless://c164c8aa-3db8-49bb-9675-bdbfc2ecdb39@38.47.108.240:57613?type=tcp&encryption=none&security=reality&sni=apple.com&pbk=53eqWPu-fQR8tPXoSc5tLZ1wCgyIExpt04e3ZDMQ2i8&sid=a290181c&fp=chrome#%E5%88%98%E5%BC%A0%E8%88%AA%E7%88%B8%E7%88%B8",
    "vless://aaaa-bbbb-cccc@1.2.3.4:443?type=tcp&security=reality&sni=www.google.com&pbk=SomePublicKey&sid=abcdef&fp=firefox#TestNode2",
    "vless://dddd-eeee-ffff@5.6.7.8:8080?type=ws&security=none&path=/ws&host=example.com#WebSocketNode",
]

proxies = []
for link in links:
    p = parse_vless(link)
    if p:
        proxies.append(p)
        print(f"  ✅ Parsed: {p['name']} -> {p['server']}:{p['port']} ({p['network']}/{p.get('tls', False)})")
    else:
        print(f"  ❌ Failed to parse: {link[:50]}...")

yaml_multi = generate_clash_yaml(proxies, config)
print(f"\nMulti-node YAML ({len(proxies)} nodes):")
print(yaml_multi)
