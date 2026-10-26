"""v1.6.43 panel-clients 自动发现端点测试(mock 面板) + deploy.sh 版本检查"""
import sys, os, json
sys.path.insert(0, r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')
os.chdir(r'C:\Users\Administrator\WorkBuddy\2026-08-08-23-49-43\vless2clash')

import app as v2c

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  PASS", name)
    else:
        FAIL += 1; print("  FAIL", name, detail)

# --- mock 面板数据 ---
PANEL_A = "http://185.0.0.1:8284"
PANEL_B = "http://140.0.0.2:8284"

def fake_inbounds(url, user, pwd):
    if url == PANEL_A:
        return {"success": True, "obj": [{
            "id": 1, "port": 443, "remark": "香港",
            "settings": json.dumps({"clients": [
                {"email": "客户A", "subId": "aaaa1111", "enable": True, "totalGB": 53687091200,
                 "expiryTime": 1801326552885, "id": "uuid-a1"},
                {"email": "停用B", "subId": "bbbb2222", "enable": False, "totalGB": 0, "id": "uuid-b1"},
            ]}),
            "clientStats": [{"email": "客户A", "up": 100, "down": 200, "total": 300}],
        }]}
    if url == PANEL_B:
        return {"success": True, "obj": [{
            "id": 2, "port": 443, "remark": "香港",
            "settings": {"clients": [   # dict 形式(不同面板版本)
                {"email": "客户A", "subId": "aaaa2222", "enable": True, "totalGB": 53687091200, "id": "uuid-a2"},
            ]},
            "clientStats": [],
        }]}
    return None

def fake_defaults(url, user, pwd):
    if url == PANEL_A:
        return {"subURI": "http://185.0.0.1:2096/sub/"}
    if url == PANEL_B:
        return {"subURI": "http://140.0.0.2:2096/sub/"}
    return None

orig_inb = v2c._fetch_panel_inbounds
orig_ds = v2c._fetch_panel_default_settings
v2c._fetch_panel_inbounds = fake_inbounds
v2c._fetch_panel_default_settings = fake_defaults

# --- 全局配置注入两个面板(剔除 38 的场景) ---
cfg_file = v2c.GLOBAL_CONFIG_FILE
backup = open(cfg_file, encoding="utf-8").read() if os.path.exists(cfg_file) else None
gcfg = v2c.load_global_config()
gcfg["vps_traffic_sources"] = [
    {"url": PANEL_A, "username": "saeson", "password": "x"},
    {"url": PANEL_B, "username": "admin", "password": "x"},
    {"url": "http://38.9.9.9:8284/dead", "username": "a", "password": "b"},  # 已退役面板 -> None
]
v2c.save_global_config(gcfg)

try:
    client = v2c.app.test_client()

    print("=" * 25, "T1 未登录 401", "=" * 25)
    r = client.post("/api/admin/panel-clients")
    check("未授权返回 401", r.status_code == 401, r.status_code)

    print("=" * 25, "T2 登录后自动发现", "=" * 25)
    r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
    check("v2c 管理员登录", r.status_code == 200 and r.get_json().get("success"), r.get_data(as_text=True)[:100])
    r = client.post("/api/admin/panel-clients")
    d = r.get_json()
    check("HTTP 200", r.status_code == 200)
    cl = d.get("clients") or []
    check("停用客户端被过滤", all(c["email"] != "停用B" for c in cl), str(cl))
    check("发现 2 个启用客户端", len(cl) == 2, json.dumps(cl, ensure_ascii=False)[:300])
    a = next((c for c in cl if c["panel"] == PANEL_A), None)
    b = next((c for c in cl if c["panel"] == PANEL_B), None)
    check("A: sub_url = subURI + subId", a and a["sub_url"] == "http://185.0.0.1:2096/sub/aaaa1111", a)
    check("A: 带流量统计", a and a["up"] == 100 and a["down"] == 200, a)
    check("B: dict 形式 settings 兼容", b and b["sub_url"] == "http://140.0.0.2:2096/sub/aaaa2222", b)
    check("退役面板进 errors", any("38.9.9.9" in e for e in (d.get("errors") or [])), str(d.get("errors")))

    print("=" * 25, "T3 选中客户端 -> convert 生成合并订阅", "=" * 25)
    def fake_fetch_sub(url):
        if "aaaa1111" in url:
            return [("vless://uuid-a1@185.0.0.1:443?type=tcp&encryption=none&security=reality"
                     "&sni=www.microsoft.com&pbk=74_1_uEKuRxiUbawJhnxTQo-vwZrJT1MsmytyA1HbCo"
                     "&sid=dfe1f4db389e5ce8&fp=chrome#%E5%AE%A2%E6%88%B7A")]
        if "aaaa2222" in url:
            return [("vless://uuid-a2@140.0.0.2:443?type=tcp&encryption=none&security=reality"
                     "&sni=www.microsoft.com&pbk=74_1_uEKuRxiUbawJhnxTQo-vwZrJT1MsmytyA1HbCo"
                     "&sid=dfe1f4db389e5ce8&fp=chrome#%E5%AE%A2%E6%88%B7A")]
        return []
    orig_fs = v2c.fetch_subscription
    v2c.fetch_subscription = fake_fetch_sub
    try:
        r = client.post("/api/convert", json={
            "xui_sub_url": a["sub_url"] + "\n" + b["sub_url"],
            "config": {"rules_mode": "basic"},
            "config_name": "客户A-双VPS",
        })
        d2 = r.get_json()
        check("convert 200", r.status_code == 200, str(d2)[:200])
        check("两源同名合并", d2.get("merge_groups") == {"客户A": ["客户A", "客户A_1"]},
              json.dumps(d2.get("merge_groups"), ensure_ascii=False))
        y = d2.get("yaml") or ""
        check("YAML 含合并组", '- name: "客户A"\n    type: select' in y)
        check("token 下载路由生成", bool(d2.get("download_url", "").startswith("/d/")))
    finally:
        v2c.fetch_subscription = orig_fs
finally:
    v2c._fetch_panel_inbounds = orig_inb
    v2c._fetch_panel_default_settings = orig_ds
    if backup is not None:
        open(cfg_file, "w", encoding="utf-8").write(backup)
    elif os.path.exists(cfg_file):
        os.remove(cfg_file)

print()
print(f"===== 结果: {PASS} PASS / {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
