# VLESS to Clash YAML Converter

将 VLESS 链接一键转换为 Clash Meta / Mihomo Party 可导入的 YAML 配置文件。

## 功能

- 批量解析 VLESS 链接，生成 Clash YAML 配置
- 支持 Reality (pbk/sid/fp/sni) 完整参数
- 支持 TCP / WebSocket / gRPC / HTTP2 网络类型
- 支持订阅链接导入（自动拉取 + Base64 解码）
- 每次转换自动保存 YAML 文件，生成混淆随机下载链接
- 可自定义端口、局域网、模式、日志级别、策略组名称
- 复制链接 / 下载文件 / 复制配置
- 交互式部署脚本（部署 / 更新 / 卸载）

## 一键远程安装 (推荐)

在 VPS 上执行一行命令即可自动下载、解压、安装：

```bash
bash <(curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh)
```

脚本会自动：
1. 安装系统依赖 (curl, wget, unzip, git)
2. 从 GitHub 下载最新代码
3. 解压并执行部署
4. 自动检测：已安装则更新，未安装则全新部署

### 一键更新

```bash
bash <(curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh) -- update
```

### 一键卸载

```bash
bash <(curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh) -- uninstall
```

## 手动部署 (Debian 12)

如果一键安装不可用，也可手动操作：

```bash
# 下载并解压
wget https://github.com/saeson001/vless2clash/archive/refs/heads/main.zip
unzip main.zip
cd vless2clash-main

# 执行部署
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

生成的下载链接格式：`http://服务器IP:5000/d/<随机token>`

可将该链接直接粘贴到 Mihomo Party 的订阅导入中使用。

## 项目结构

```
vless2clash/
├── app.py                  # Flask 后端（VLESS 解析 + YAML 生成）
├── templates/index.html    # WebUI 页面
├── static/css/style.css    # 样式
├── static/js/app.js        # 前端交互逻辑
├── requirements.txt        # Python 依赖
├── install.sh              # 远程一键安装脚本
├── deploy.sh               # 部署/更新/卸载脚本
├── nginx-vless2clash.conf  # Nginx 反向代理配置参考
└── test_convert.py         # 测试脚本
```

## License

MIT
