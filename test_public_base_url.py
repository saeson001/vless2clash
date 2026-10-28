"""测试 public_base_url: 订阅链接生成优先使用配置的公开地址。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("=" * 25, "T1 save_global_config 归一化 public_base_url", "=" * 25)
import app as A  # noqa: E402

# 用临时文件避免污染真实配置
orig_file = A.GLOBAL_CONFIG_FILE
tmp = os.path.join(os.path.dirname(orig_file), "_test_pub_base.json")
A.GLOBAL_CONFIG_FILE = tmp

saved = A.save_global_config({"public_base_url": "http://3x.820804.xyz:2095/"})
check("去掉末尾斜杠", saved["public_base_url"] == "http://3x.820804.xyz:2095",
      saved["public_base_url"])
check("落盘成功", os.path.exists(tmp))

saved = A.save_global_config({"public_base_url": "  https://sub.example.com  "})
check("去掉首尾空白", saved["public_base_url"] == "https://sub.example.com",
      saved["public_base_url"])

saved = A.save_global_config({"public_base_url": "3x.820804.xyz:2095"})
check("无 scheme 则拒绝", saved["public_base_url"] == "", saved["public_base_url"])

saved = A.save_global_config({"public_base_url": "ftp://x.com"})
check("非 http(s) 则拒绝", saved["public_base_url"] == "", saved["public_base_url"])

saved = A.save_global_config({})
check("未提供则默认空", saved["public_base_url"] == "", saved["public_base_url"])

saved = A.save_global_config({"public_base_url": "http://a.com", "port": 7891})
check("不影响其他键", saved["port"] == 7891, str(saved["port"]))

print("=" * 25, "T2 load_global_config 读回", "=" * 25)
A.save_global_config({"public_base_url": "http://3x.820804.xyz:2095"})
loaded = A.load_global_config()
check("读回一致", loaded["public_base_url"] == "http://3x.820804.xyz:2095",
      loaded["public_base_url"])

# 老配置文件(无该键)应回落到默认
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"port": 7890}, fh)
loaded = A.load_global_config()
check("老配置缺键回落默认", loaded.get("public_base_url", "") == "",
      str(loaded.get("public_base_url")))

print("=" * 25, "T3 默认值存在于 DEFAULT_GLOBAL_CONFIG", "=" * 25)
check("默认键存在", "public_base_url" in A.DEFAULT_GLOBAL_CONFIG)
check("默认为空串", A.DEFAULT_GLOBAL_CONFIG["public_base_url"] == "")

# 清理
if os.path.exists(tmp):
    os.remove(tmp)
A.GLOBAL_CONFIG_FILE = orig_file

print("=" * 25, "T4 前端逻辑静态检查", "=" * 25)
js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "js", "manage.js")
js = open(js_path, encoding="utf-8").read()
check("manage.js 读取 public_base_url", "cfg.public_base_url" in js)
check("manage.js 保存 public_base_url", "public_base_url:" in js)
check("复制链接使用 __publicBaseUrl", "__publicBaseUrl" in js)

html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "templates", "manage.html")
html = open(html_path, encoding="utf-8").read()
check("manage.html 有输入框", 'id="gc-public-base-url"' in html)

print()
print("=" * 60)
print(f"结果: {PASS} 通过, {FAIL} 失败")
print("=" * 60)
sys.exit(1 if FAIL else 0)
