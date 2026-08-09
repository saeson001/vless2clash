#!/bin/bash
# ============================================================
# VLESS to Clash YAML 转换工具 - Debian 12 部署/更新/卸载脚本
# 版本: v1.3.0
# 作者: saeson
# 用法:
#   交互菜单: sudo bash deploy.sh
#   直接部署: sudo bash deploy.sh install
#   更新:     sudo bash deploy.sh update
#   卸载:     sudo bash deploy.sh uninstall
#   查看版本: bash deploy.sh version
# ============================================================

set -e

VERSION="v1.3.0"
APP_NAME="vless2clash"
APP_DIR="/opt/vless2clash"
APP_USER="vless2clash"
APP_PORT=5000
PYTHON_VERSION="python3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================
# 工具函数
# ============================================================

get_installed_version() {
    if [ -f "${APP_DIR}/VERSION" ]; then
        cat "${APP_DIR}/VERSION"
    else
        echo "未知(旧版本)"
    fi
}

# ============================================================
# 卸载函数
# ============================================================
do_uninstall() {
    echo "========================================"
    echo "  卸载 VLESS to Clash YAML"
    echo "========================================"
    echo ""

    read -p "确认要完全卸载 $APP_NAME？此操作不可逆 (y/N) " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "已取消卸载"
        exit 0
    fi

    echo ""
    echo "[1/5] 停止服务..."
    systemctl stop "$APP_NAME" 2>/dev/null || true
    echo "  -> 服务已停止"

    echo "[2/5] 禁用服务..."
    systemctl disable "$APP_NAME" 2>/dev/null || true
    echo "  -> 服务已禁用"

    echo "[3/5] 删除 systemd 服务文件..."
    rm -f "/etc/systemd/system/${APP_NAME}.service"
    systemctl daemon-reload
    echo "  -> 服务文件已删除"

    echo "[4/5] 删除应用目录..."
    rm -rf "$APP_DIR"
    echo "  -> $APP_DIR 已删除"

    echo "[5/5] 删除用户..."
    if id "$APP_USER" &>/dev/null; then
        userdel "$APP_USER" 2>/dev/null || true
        echo "  -> 用户 $APP_USER 已删除"
    else
        echo "  -> 用户 $APP_USER 不存在，跳过"
    fi

    echo ""
    echo "========================================"
    echo "  卸载完成!"
    echo "========================================"
    echo ""
    echo "  所有相关文件、服务、用户已清除"
    echo "========================================"
}

# ============================================================
# 更新函数
# ============================================================
do_update() {
    echo "========================================"
    echo "  更新 VLESS to Clash YAML"
    echo "  当前版本: $(get_installed_version) -> 新版本: $VERSION"
    echo "========================================"
    echo ""

    if [ ! -d "$APP_DIR" ]; then
        echo "[提示] 未检测到已安装的版本，将执行全新部署..."
        echo ""
        do_install
        return
    fi

    if [ "$EUID" -ne 0 ]; then
        echo "[错误] 请使用 root 用户或 sudo 运行此脚本"
        exit 1
    fi

    echo "[1/6] 停止旧服务..."
    systemctl stop "$APP_NAME" 2>/dev/null || true
    echo "  -> 服务已停止"

    echo "[2/6] 备份已生成的 YAML 文件..."
    BACKUP_DIR="${APP_DIR}/downloads_backup_$(date +%Y%m%d%H%M%S)"
    if [ -d "${APP_DIR}/downloads" ]; then
        cp -r "${APP_DIR}/downloads" "$BACKUP_DIR"
        echo "  -> 已备份到 $BACKUP_DIR"
    else
        echo "  -> 无 downloads 目录，跳过备份"
    fi

    echo "[3/6] 更新应用文件..."
    cp -r "$SCRIPT_DIR"/app.py "$SCRIPT_DIR"/templates "$SCRIPT_DIR"/static "$SCRIPT_DIR"/requirements.txt "$APP_DIR/"
    mkdir -p "$APP_DIR/downloads"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    echo "  -> 应用文件已更新"

    echo "[4/6] 写入版本号..."
    echo "$VERSION" > "${APP_DIR}/VERSION"
    chown "$APP_USER:$APP_USER" "${APP_DIR}/VERSION"
    echo "  -> 版本号: $VERSION"

    echo "[5/6] 更新 Python 依赖..."
    if [ -f "$APP_DIR/venv/bin/pip" ]; then
        "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
        echo "  -> 依赖已更新"
    else
        $PYTHON_VERSION -m venv "$APP_DIR/venv"
        "$APP_DIR/venv/bin/pip" install --upgrade pip -q
        "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
        echo "  -> 虚拟环境已重建，依赖已安装"
    fi

    echo "[6/6] 重启服务..."
    systemctl daemon-reload
    systemctl restart "$APP_NAME"
    sleep 2

    if systemctl is-active --quiet "$APP_NAME"; then
        echo "  -> 服务已重启"
    else
        echo "  -> [警告] 服务可能未正常启动，请检查: journalctl -u $APP_NAME -f"
    fi

    echo ""
    echo "========================================"
    echo "  更新完成!  $VERSION"
    echo "========================================"
    echo ""
    echo "  WebUI 地址:  http://<服务器IP>:${APP_PORT}"
    echo "  之前生成的 YAML 文件保留在 downloads/ 目录中"
    echo "  备份位于:    $BACKUP_DIR"
    echo ""
    echo "  如需回滚，可从备份目录恢复:"
    echo "    cp -r $BACKUP_DIR/* ${APP_DIR}/downloads/"
    echo "    systemctl restart $APP_NAME"
    echo "========================================"
}

# ============================================================
# 部署函数
# ============================================================
do_install() {
    echo "========================================"
    echo "  VLESS to Clash YAML 部署脚本 $VERSION"
    echo "  目标系统: Debian 12 (Bookworm)"
    echo "========================================"
    echo ""

    if [ "$EUID" -ne 0 ]; then
        echo "[错误] 请使用 root 用户或 sudo 运行此脚本"
        exit 1
    fi

    echo "[1/7] 更新系统并安装依赖..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip curl > /dev/null 2>&1
    echo "  -> 系统依赖安装完成"

    echo "[2/7] 创建运行用户..."
    if ! id "$APP_USER" &>/dev/null; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
        echo "  -> 用户 $APP_USER 已创建"
    else
        echo "  -> 用户 $APP_USER 已存在"
    fi

    echo "[3/7] 部署应用文件..."
    mkdir -p "$APP_DIR"
    cp -r "$SCRIPT_DIR"/app.py "$SCRIPT_DIR"/templates "$SCRIPT_DIR"/static "$SCRIPT_DIR"/requirements.txt "$APP_DIR/"
    mkdir -p "$APP_DIR/downloads"
    echo "$VERSION" > "${APP_DIR}/VERSION"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    echo "  -> 文件已复制到 $APP_DIR"

    echo "[4/7] 创建 Python 虚拟环境并安装依赖..."
    $PYTHON_VERSION -m venv "$APP_DIR/venv"
    "$APP_DIR/venv/bin/pip" install --upgrade pip -q
    "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
    echo "  -> Python 依赖安装完成"

    echo "[5/7] 配置 systemd 服务..."
    cat > /etc/systemd/system/${APP_NAME}.service << EOF
[Unit]
Description=VLESS to Clash YAML Converter ($VERSION)
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/gunicorn --workers 2 --bind 0.0.0.0:${APP_PORT} --timeout 30 app:app
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
    echo "  -> systemd 服务已创建"

    echo "[6/7] 启动服务..."
    systemctl daemon-reload
    systemctl enable "$APP_NAME"
    systemctl restart "$APP_NAME"
    sleep 2

    if systemctl is-active --quiet "$APP_NAME"; then
        echo "  -> 服务已启动"
    else
        echo "  -> [警告] 服务可能未正常启动，请检查: journalctl -u $APP_NAME -f"
    fi

    echo "[7/7] 部署完成!"
    echo ""
    echo "========================================"
    echo "  部署成功!  $VERSION"
    echo "========================================"
    echo ""
    echo "  WebUI 地址:  http://<服务器IP>:${APP_PORT}"
    echo "  本机访问:    http://localhost:${APP_PORT}"
    echo ""
    echo "  常用命令:"
    echo "    查看状态:  systemctl status ${APP_NAME}"
    echo "    重启服务:  systemctl restart ${APP_NAME}"
    echo "    停止服务:  systemctl stop ${APP_NAME}"
    echo "    查看日志:  journalctl -u ${APP_NAME} -f"
    echo "    更新服务:  sudo bash deploy.sh update"
    echo "    卸载服务:  sudo bash deploy.sh uninstall"
    echo ""
    echo "  生成的 YAML 文件保存在: ${APP_DIR}/downloads/"
    echo "  下载链接格式: http://<服务器IP>:${APP_PORT}/d/<随机token>"
    echo "  可直接在 Mihomo Party 中作为订阅链接导入"
    echo ""
    echo "  如需使用域名 + HTTPS, 建议配置 Nginx 反向代理"
    echo "========================================"
}

# ============================================================
# 交互式菜单
# ============================================================
show_menu() {
    INSTALLED_VER=$(get_installed_version)

    echo "========================================"
    echo "  VLESS to Clash YAML 转换工具"
    echo "  脚本版本: $VERSION"
    echo "  目标系统: Debian 12 (Bookworm)"
    if [ -d "$APP_DIR" ]; then
        echo "  已装版本: $INSTALLED_VER"
    else
        echo "  安装状态: 未安装"
    fi
    echo "========================================"
    echo ""
    echo "  1) 部署 (全新安装)"
    echo "  2) 更新 (覆盖升级，保留 YAML 文件)"
    echo "  3) 卸载 (完全清除)"
    echo "  4) 查看版本"
    echo "  0) 退出"
    echo ""
    read -p "请选择 [0-4]: " choice

    case "$choice" in
        1)
            echo ""
            do_install
            ;;
        2)
            echo ""
            do_update
            ;;
        3)
            echo ""
            do_uninstall
            ;;
        4)
            echo ""
            echo "脚本版本: $VERSION"
            if [ -d "$APP_DIR" ]; then
                echo "已装版本: $(get_installed_version)"
            else
                echo "安装状态: 未安装"
            fi
            echo ""
            ;;
        0)
            echo "已退出"
            exit 0
            ;;
        *)
            echo "[错误] 无效选项: $choice"
            exit 1
            ;;
    esac
}

# ============================================================
# 主入口
# ============================================================
case "$1" in
    install)
        do_install
        ;;
    update)
        do_update
        ;;
    uninstall)
        do_uninstall
        ;;
    version)
        echo "脚本版本: $VERSION"
        if [ -d "$APP_DIR" ]; then
            echo "已装版本: $(get_installed_version)"
        else
            echo "安装状态: 未安装"
        fi
        ;;
    "")
        show_menu
        ;;
    *)
        echo "用法: $0 {install|update|uninstall|version}"
        echo "  或直接运行 $0 进入交互菜单"
        exit 1
        ;;
esac
