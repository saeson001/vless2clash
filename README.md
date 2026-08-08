# VLESS to Clash YAML Converter

将 VLESS 链接一键转换为 Clash Meta / Mihomo Party 可导入的 YAML 配置文件。

## 功能

- 批量解析 VLESS 链接，生成 Clash YAML 配置
- 支持 Reality (pbk/sid/fp/sni) 完整参数
- 支持 TCP / WebSocket / gRPC / HTTP2 网络类型
- 支持订阅链接导入（自动拉取 + Base64 解码）
- 每次转换自动保存 YAML 文件，从 00001.yaml 递增命名
- 生成可复制下载链接，可直接作为 Mihomo Party 订阅地址
- 可自定义端口、局域网、模式、日志级别、策略组名称

## 快速部署 (Debian 12)

```bash
# 1. 上传项目到服务器
scp -r vless2clash/ root@你的IP:/root/

# 2. SSH 登录后执行部署
cd vless2clash
sudo bash deploy.sh
```

部署脚本提供交互式菜单：

```
1) 部署 (全新安装)
2) 更新 (覆盖升级，保留 YAML 文件)
3) 卸载 (完全清除)
4) 查看版本
0) 退出
```

也支持直接传参数：`deploy.sh install` / `deploy.sh update` / `deploy.sh uninstall`

## 使用

部署完成后访问 `http://服务器IP:5000`，粘贴 VLESS 链接，点击「一键转换」即可。

生成的下载链接格式：`http://服务器IP:5000/files/00001.yaml`

可将该链接直接粘贴到 Mihomo Party 的订阅导入中使用。

## 项目结构

```
vless2clash/
├── app.py                  # Flask 后端（VLESS 解析 + YAML 生成）
├── templates/index.html    # WebUI 页面
├── static/css/style.css    # 样式
├── static/js/app.js        # 前端交互逻辑
├── requirements.txt        # Python 依赖
├── deploy.sh               # 部署/更新/卸载脚本
├── nginx-vless2clash.conf  # Nginx 反向代理配置参考
└── test_convert.py         # 测试脚本
```

## License

MIT
