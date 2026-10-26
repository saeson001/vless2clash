"""v1.6.42/43 同名合并 + 多源拉取功能测试（本地，不依赖网络；v1.6.43 起地区前缀剥离）"""
import sys, os
sys.path.insert(0, r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')
os.chdir(r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')

from app import _parse_links_and_subs, generate_clash_yaml

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")

L1 = ("vless://uuid-1111@1.1.1.1:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK1&sid=111111&fp=chrome#%E9%A6%99%E6%B8%AF-%E8%B6%85%E6%8B%BD")  # 香港-超拽
L2 = ("vless://uuid-2222@2.2.2.2:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK2&sid=222222&fp=chrome#%E9%A6%99%E6%B8%AF-%E8%B6%85%E6%8B%BD")  # 香港-超拽 (同名, 不同 VPS)
L3 = ("vless://uuid-3333@3.3.3.3:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK3&sid=333333&fp=chrome#%E6%97%A5%E6%9C%AC-%E4%B8%9C%E4%BA%AC")    # 日本-东京

raw = L1 + "\n" + L2 + "\n" + L3

print("=" * 30, "T1 同名合并: parse 返回 merge_groups", "=" * 30)
proxies, errors, merge_groups = _parse_links_and_subs(raw, "")
check("解析 3 节点", len(proxies) == 3, f"got {len(proxies)}")
check("无错误", not errors, str(errors))
check("旧式重命名保留", proxies[1]["name"] == "香港-超拽_1", proxies[1]["name"])
check("地区前缀剥离后归组到 超拽", merge_groups == {"超拽": ["香港-超拽", "香港-超拽_1"]}, str(merge_groups))

print("=" * 30, "T2 合并开启: YAML 组结构", "=" * 30)
cfg = {"merge_groups": merge_groups, "merge_same_name": True}
y = generate_clash_yaml(proxies, dict(cfg))
check("成员保留完整名", '- name: "香港-超拽"' in y and '- name: "香港-超拽_1"' in y)
check("存在合并组(组名=剥离地区后的主体名)", '  - name: "超拽"\n    type: select' in y)
check("组内含两个变体", '- "香港-超拽"' in y and '- "香港-超拽_1"' in y)
check("节点选择引用组名", '- "超拽"' in y)
check("合并组在自动选择之后", y.find('- name: "自动选择"') < y.find('  - name: "超拽"\n    type: select'))
check("独立节点仍在节点选择", '- "日本-东京"' in y)

print("=" * 30, "T3 合并关闭: 行为回退", "=" * 30)
proxies2, _, mg2 = _parse_links_and_subs(raw, "")
y2 = generate_clash_yaml(proxies2, {"merge_groups": mg2, "merge_same_name": False})
check("关闭时保留 _1 平铺", '- name: "香港-超拽_1"' in y2)
check("关闭时无合并组", '- name: "超拽"\n    type: select' not in y2)

print("=" * 30, "T4 不同名: 无合并", "=" * 30)
proxies3, _, mg3 = _parse_links_and_subs(L1 + "\n" + L3, "")
check("无重名无分组", mg3 == {}, str(mg3))
y3 = generate_clash_yaml(proxies3, {"merge_groups": mg3, "merge_same_name": True})
check("名字保持原样", '- name: "香港-超拽"' in y3 and '- name: "日本-东京"' in y3)

print("=" * 30, "T5 三个同名: 三个成员", "=" * 30)
proxies4, _, mg4 = _parse_links_and_subs(L1 + "\n" + L2 + "\n" + L1.replace("uuid-1111", "uuid-9999").replace("1.1.1.1", "9.9.9.9").replace("PBK1", "PBK9").replace("111111", "999999"), "")
check("3 变体归组", mg4.get("超拽") == ["香港-超拽", "香港-超拽_1", "香港-超拽_2"], str(mg4))
y4 = generate_clash_yaml(proxies4, {"merge_groups": mg4, "merge_same_name": True})
check("组内 3 成员", '- "香港-超拽_2"' in y4)
check("组名仍为 超拽", '  - name: "超拽"\n    type: select' in y4)

print("=" * 30, "T6 AI 分流与合并共存", "=" * 30)
proxies5, _, mg5 = _parse_links_and_subs(raw, "")
cfg5 = {"merge_groups": mg5, "merge_same_name": True, "ai_routing": True,
        "ai_japan": "日本-东京", "ai_hongkong": "香港-超拽_1"}  # 选了变体名
y5 = generate_clash_yaml(proxies5, cfg5)
check("AI 香港成员映射到合并组", '- "超拽"' in y5)
check("默认分流组存在", '- name: "默认分流"' in y5)
check("AI 分流组存在", '- name: "AI 分流"' in y5)

print("=" * 30, "T7 多行 xui_sub_url 解析路径(mock)", "=" * 30)
import app
calls = []
def fake_fetch(url):
    calls.append(url)
    if "185." in url:
        return [L1]
    return [L2]
orig = app.fetch_subscription
app.fetch_subscription = fake_fetch
try:
    p6, e6, m6 = _parse_links_and_subs("", "", xui_sub_url="http://185.1.1.1:2096/sub/aaa\nhttp://38.9.9.9:2096/sub/bbb")
finally:
    app.fetch_subscription = orig
check("两个源都被拉取", calls == ["http://185.1.1.1:2096/sub/aaa", "http://38.9.9.9:2096/sub/bbb"], str(calls))
check("两源同名进 merge_groups", m6 == {"超拽": ["香港-超拽", "香港-超拽_1"]}, str(m6))
check("无错误", not e6, str(e6))

print("=" * 30, "T8 真实面板风格: |流量|天数 后缀", "=" * 30)
N1 = "香港-超拽|📊50.00GB|⏳95D"
N2 = "香港-超拽|📊50.00GB|⏳96D"
RA = ("vless://uuid-ra@11.11.11.11:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK1&sid=111111&fp=chrome#")
RB = ("vless://uuid-rb@22.22.22.22:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK2&sid=222222&fp=chrome#")
import urllib.parse
proxies8, e8, mg8 = _parse_links_and_subs(
    RA + urllib.parse.quote(N1) + "\n" + RB + urllib.parse.quote(N2), "")
check("主体名归组", mg8 == {"超拽": [N1, N2]}, str(mg8))
y8 = generate_clash_yaml(proxies8, {"merge_groups": mg8, "merge_same_name": True})
check("组名=主体名", '  - name: "超拽"\n    type: select' in y8)
check("成员保留完整名(含各自流量)", '- "香港-超拽|📊50.00GB|⏳95D"' in y8 and '- "香港-超拽|📊50.00GB|⏳96D"' in y8)
check("节点选择引用主体名", '- "超拽"' in y8)
check("主体名不再作为独立节点出现两次", y8.count('- name: "超拽"\n    type: vless') == 0)

print("=" * 30, "T9 地区前缀不同: 香港-X 与 日本-X 合并", "=" * 30)
NA = "香港-rz|📊50.00GB|⏳95D"
NB = "日本-rz|📊50.00GB|⏳95D"
PA = ("vless://uuid-pa@11.11.11.11:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK1&sid=111111&fp=chrome#")
PB = ("vless://uuid-pb@22.22.22.22:443?type=tcp&encryption=none&security=reality"
      "&sni=www.microsoft.com&pbk=PBK2&sid=222222&fp=chrome#")
proxies9, e9, mg9 = _parse_links_and_subs(
    PA + urllib.parse.quote(NA) + "\n" + PB + urllib.parse.quote(NB), "")
check("地区前缀剥离后归组", mg9 == {"rz": [NA, NB]}, str(mg9))
y9 = generate_clash_yaml(proxies9, {"merge_groups": mg9, "merge_same_name": True})
check("组名=rz", '  - name: "rz"\n    type: select' in y9)
check("组内含香港/日本两个出口", '- "香港-rz|📊50.00GB|⏳95D"' in y9 and '- "日本-rz|📊50.00GB|⏳95D"' in y9)

print("=" * 30, "T10 国外域名规则先于 GEOIP (google.com 修复)", "=" * 30)
y10 = generate_clash_yaml(proxies3, {"merge_groups": {}, "merge_same_name": True, "rules_mode": "basic"})
r10 = [ln.strip()[2:] for ln in y10.splitlines() if ln.strip().startswith("- ")]
i_geo = next((i for i, r in enumerate(r10) if r.startswith("GEOSITE,geolocation-!cn")), -1)
i_cn = next((i for i, r in enumerate(r10) if r == "GEOSITE,cn,DIRECT"), -1)
i_gip = next((i for i, r in enumerate(r10) if r.startswith("GEOIP,CN")), -1)
check("规则存在", "GEOSITE,geolocation-!cn,节点选择" in y10)
check("顺序: cn直连 < 国外代理 < GEOIP", 0 <= i_cn < i_geo < i_gip, f"{i_cn},{i_geo},{i_gip}")
if i_geo == -1:
    print("[DEBUG] rules 段尾部:")
    for ln in y10.splitlines():
        if ln.strip().startswith(("- GEOSITE", "- GEOIP", "- MATCH")):
            print("   ", ln.strip())

print("=" * 30, "T11 AI 模式下国外域名走默认分流", "=" * 30)
y11 = generate_clash_yaml(proxies3, {"merge_groups": {}, "merge_same_name": True, "rules_mode": "basic",
                                     "ai_routing": True, "ai_japan": "日本-东京", "ai_hongkong": "香港-超拽"})
check("geolocation-!cn -> 默认分流", "GEOSITE,geolocation-!cn,默认分流" in y11)

print("=" * 30, "T12 remote 模式 rule-provider 修复", "=" * 30)
y12 = generate_clash_yaml(proxies3, {"merge_groups": {}, "merge_same_name": True, "rules_mode": "remote"})
check("provider geosite-not-cn 存在", "  geosite-not-cn:" in y12)
check("RULE-SET 顺序正确", y12.find("RULE-SET,geosite-cn,DIRECT") < y12.find("RULE-SET,geosite-not-cn,节点选择") < y12.find("RULE-SET,geoip-cn,DIRECT"))

print()
print(f"===== 结果: {PASS} PASS / {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
