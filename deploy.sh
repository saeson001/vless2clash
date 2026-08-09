#!/bin/bash
# ============================================================
# VLESS to Clash YAML 转换工具 - 多发行版部署/更新/卸载脚本
# 版本: v1.5.1
# 作者: saeson
# 支持: Debian 11/12/13, Ubuntu 20.04+, CentOS 7/8/9, RHEL 8/9,
#       Rocky Linux, AlmaLinux, Fedora, openSUSE, Arch Linux
# 用法:
#   交互菜单:   sudo bash deploy.sh
#   直接部署:   sudo bash deploy.sh install
#   更新:       sudo bash deploy.sh update
#   卸载:       sudo bash deploy.sh uninstall
#   查看版本:   bash deploy.sh version
#   查看管理员: sudo bash deploy.sh show-admin
#   重置管理员: sudo bash deploy.sh reset-admin
# ============================================================

set -e

VERSION="v1.5.1"
APP_NAME="vless2clash"
APP_DIR="/opt/vless2clash"
APP_USER="vless2clash"
APP_PORT=5000
PYTHON_VERSION="python3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================
# 操作系统检测
# ============================================================
detect_os() {
    if [ ! -f /etc/os-release ]; then
        echo "[错误] 无法检测操作系统（缺少 /etc/os-release）"
        exit 1
    fi
    # Save our VERSION before sourcing os-release (which may define its own VERSION)
    _SAVED_APP_VERSION="$VERSION"
    . /etc/os-release
    VERSION="$_SAVED_APP_VERSION"
    OS_ID="${ID:-unknown}"
    OS_VERSION="${VERSION_ID:-unknown}"
    OS_PRETTY="${PRETTY_NAME:-$OS_ID $OS_VERSION}"
    OS_FAMILY="unknown"
    PKG_MANAGER=""
    PKG_INSTALL=""
    NOLOGIN_PATH=""

    case "$OS_ID" in
        debian|ubuntu|linuxmint|pop)
            OS_FAMILY="debian"
            PKG_MANAGER="apt-get"
            PKG_INSTALL="apt-get install -y -qq"
            NOLOGIN_PATH="/usr/sbin/nologin"
            ;;
        centos|rhel|rocky|almalinux|ol|fedora|amzn)
            OS_FAMILY="rhel"
            if command -v dnf &>/dev/null; then
                PKG_MANAGER="dnf"
                PKG_INSTALL="dnf install -y -q"
            else
                PKG_MANAGER="yum"
                PKG_INSTALL="yum install -y -q"
            fi
            NOLOGIN_PATH="/sbin/nologin"
            ;;
        opensuse*|suse|sles)
            OS_FAMILY="suse"
            PKG_MANAGER="zypper"
            PKG_INSTALL="zypper --non-interactive --quiet install"
            NOLOGIN_PATH="/sbin/nologin"
            ;;
        arch|manjaro|endeavouros)
            OS_FAMILY="arch"
            PKG_MANAGER="pacman"
            PKG_INSTALL="pacman --noconfirm --needed -S"
            NOLOGIN_PATH="/sbin/nologin"
            ;;
        alpine)
            OS_FAMILY="alpine"
            PKG_MANAGER="apk"
            PKG_INSTALL="apk add --quiet"
            NOLOGIN_PATH="/sbin/nologin"
            ;;
        *)
            OS_FAMILY="unknown"
            echo "[警告] 未识别的发行版: $OS_ID ($OS_PRETTY)"
            echo "        尝试使用 apt-get 作为默认包管理器..."
            if command -v apt-get &>/dev/null; then
                OS_FAMILY="debian"
                PKG_MANAGER="apt-get"
                PKG_INSTALL="apt-get install -y -qq"
                NOLOGIN_PATH="/usr/sbin/nologin"
            elif command -v dnf &>/dev/null; then
                OS_FAMILY="rhel"
                PKG_MANAGER="dnf"
                PKG_INSTALL="dnf install -y -q"
                NOLOGIN_PATH="/sbin/nologin"
            elif command -v yum &>/dev/null; then
                OS_FAMILY="rhel"
                PKG_MANAGER="yum"
                PKG_INSTALL="yum install -y -q"
                NOLOGIN_PATH="/sbin/nologin"
            else
                echo "[错误] 找不到支持的包管理器 (apt/dnf/yum/zypper/pacman/apk)"
                exit 1
            fi
            ;;
    esac

    echo "[信息] 检测到系统: $OS_PRETTY ($OS_FAMILY)"
}

# ============================================================
# 安装系统依赖（跨发行版）
# ============================================================
install_system_deps() {
    echo "[依赖] 使用包管理器: $PKG_MANAGER"

    case "$OS_FAMILY" in
        debian)
            $PKG_MANAGER update -qq 2>/dev/null || true
            $PKG_INSTALL python3 python3-venv python3-pip curl wget unzip > /dev/null 2>&1
            ;;
        rhel)
            # CentOS 7 需要 EPEL 和额外包
            if [ "$OS_ID" = "centos" ] && [[ "$OS_VERSION" == 7* ]]; then
                $PKG_INSTALL epel-release > /dev/null 2>&1 || true
            fi
            $PKG_INSTALL python3 python3-pip curl wget unzip > /dev/null 2>&1
            # RHEL 系不需要单独的 venv 包，python3 自带
            # 但确保 venv 模块可用
            if ! $PYTHON_VERSION -c "import venv" 2>/dev/null; then
                $PKG_INSTALL python3-devel > /dev/null 2>&1 || true
            fi
            ;;
        suse)
            $PKG_INSTALL python3 python3-pip curl wget unzip > /dev/null 2>&1
            ;;
        arch)
            $PKG_INSTALL python python-pip curl wget unzip > /dev/null 2>&1
            PYTHON_VERSION="python"
            ;;
        alpine)
            $PKG_INSTALL python3 py3-pip curl wget unzip bash > /dev/null 2>&1
            # Alpine 需要 ensurepip 来创建 venv
            $PKG_INSTALL py3-virtualenv > /dev/null 2>&1 || true
            ;;
    esac

    echo "  -> 系统依赖安装完成"
}

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
# 查看管理员账号密码
# ============================================================
do_show_admin() {
    echo "========================================"
    echo "  管理员账号信息"
    echo "========================================"
    echo ""

    if [ ! -d "$APP_DIR" ]; then
        echo "  [错误] 应用未安装，请先执行安装"
        exit 1
    fi

    if [ -f "${APP_DIR}/data/admin_config.json" ]; then
        ADMIN_USER=$(python3 -c "import json; print(json.load(open('${APP_DIR}/data/admin_config.json'))['username'])" 2>/dev/null || echo "admin")
        ADMIN_PASS=$(python3 -c "import json; print(json.load(open('${APP_DIR}/data/admin_config.json'))['password'])" 2>/dev/null || echo "admin123")
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║        管理员账号信息                ║"
        echo "  ╠══════════════════════════════════════╣"
        echo "  ║  用户名: $ADMIN_USER"
        echo "  ║  密码:   $ADMIN_PASS"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        echo "  管理后台地址: http://<服务器IP>:${APP_PORT}/manage"
        echo ""
        echo "  ⚠️  以上为初始账号密码"
        echo "  如果已在 Web 管理后台修改过密码，此处显示的仍是旧密码"
        echo "  如需重置为默认密码，请执行: sudo bash deploy.sh reset-admin"
        echo "========================================"
    else
        echo "  [提示] 未找到管理员配置文件"
        echo "  默认账号: admin"
        echo "  默认密码: admin123"
        echo ""
        echo "  如果服务已启动，管理员账号会在首次启动时自动创建"
        echo "  请重启服务后再次查看: systemctl restart $APP_NAME && sudo bash deploy.sh show-admin"
        echo "========================================"
    fi
}

# ============================================================
# 重置管理员账号密码
# ============================================================
do_reset_admin() {
    echo "========================================"
    echo "  重置管理员账号密码"
    echo "========================================"
    echo ""

    if [ ! -d "$APP_DIR" ]; then
        echo "  [错误] 应用未安装，请先执行安装"
        exit 1
    fi

    if [ "$EUID" -ne 0 ]; then
        echo "[错误] 请使用 root 用户或 sudo 运行此命令"
        exit 1
    fi

    echo "[1/3] 停止服务..."
    systemctl stop "$APP_NAME" 2>/dev/null || true
    echo "  -> 服务已停止"

    echo "[2/3] 重置管理员账号..."
    if [ -f "$APP_DIR/venv/bin/python" ]; then
        sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" "$APP_DIR/app.py" reset-admin 2>/dev/null || \
        "$APP_DIR/venv/bin/python" "$APP_DIR/app.py" reset-admin
    else
        echo "  [错误] 找不到 Python 虚拟环境"
        exit 1
    fi
    echo "  -> 管理员账号已重置"

    echo "[3/3] 启动服务..."
    systemctl start "$APP_NAME"
    sleep 2
    echo "  -> 服务已启动"

    echo ""
    echo "========================================"
    echo "  重置完成!"
    echo "========================================"
    echo ""
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║        管理员账号信息                ║"
    echo "  ╠══════════════════════════════════════╣"
    echo "  ║  用户名: admin"
    echo "  ║  密码:   admin123"
    echo "  ╚══════════════════════════════════════╝"
    echo ""
    echo "  管理后台地址: http://<服务器IP>:${APP_PORT}/manage"
    echo "  ⚠️  请登录后及时修改密码！"
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

    detect_os

    echo "[1/6] 停止旧服务..."
    systemctl stop "$APP_NAME" 2>/dev/null || true
    echo "  -> 服务已停止"

    echo "[2/6] 备份已生成的 YAML 文件和数据库..."
    BACKUP_DIR="${APP_DIR}/backup_$(date +%Y%m%d%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    if [ -d "${APP_DIR}/downloads" ]; then
        cp -r "${APP_DIR}/downloads" "$BACKUP_DIR/downloads"
        echo "  -> YAML 文件已备份到 $BACKUP_DIR/downloads"
    else
        echo "  -> 无 downloads 目录，跳过"
    fi
    if [ -d "${APP_DIR}/data" ]; then
        cp -r "${APP_DIR}/data" "$BACKUP_DIR/data"
        echo "  -> 数据库已备份到 $BACKUP_DIR/data"
    fi

    echo "[3/6] 更新应用文件..."
    cp -r "$SCRIPT_DIR"/app.py "$SCRIPT_DIR"/templates "$SCRIPT_DIR"/static "$SCRIPT_DIR"/requirements.txt "$SCRIPT_DIR"/deploy.sh "$APP_DIR/"
    mkdir -p "$APP_DIR/downloads" "$APP_DIR/data"
    # Preserve existing database and admin config (do not overwrite)
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
    echo ""

    # Display admin credentials
    echo "  ────────────────────────────────────────"
    echo "  管理后台:  http://<服务器IP>:${APP_PORT}/manage"
    if [ -f "${APP_DIR}/data/admin_config.json" ]; then
        ADMIN_USER=$(python3 -c "import json; print(json.load(open('${APP_DIR}/data/admin_config.json'))['username'])" 2>/dev/null || echo "admin")
        ADMIN_PASS=$(python3 -c "import json; print(json.load(open('${APP_DIR}/data/admin_config.json'))['password'])" 2>/dev/null || echo "admin123")
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║        管理员账号信息                ║"
        echo "  ╠══════════════════════════════════════╣"
        echo "  ║  用户名: $ADMIN_USER"
        echo "  ║  密码:   $ADMIN_PASS"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        echo "  ⚠️  以上为初始账号密码，如已在后台修改过请使用新密码"
        echo "  重置密码: sudo bash deploy.sh reset-admin"
    else
        echo "  默认账号: admin / admin123"
        echo "  查看密码: sudo bash deploy.sh show-admin"
    fi
    echo "  ────────────────────────────────────────"
    echo "========================================"
}

# ============================================================
# 部署函数
# ============================================================
do_install() {
    echo "========================================"
    echo "  VLESS to Clash YAML 部署脚本 $VERSION"
    echo "  支持多 Linux 发行版"
    echo "========================================"
    echo ""

    if [ "$EUID" -ne 0 ]; then
        echo "[错误] 请使用 root 用户或 sudo 运行此脚本"
        exit 1
    fi

    detect_os

    echo "[1/7] 安装系统依赖..."
    install_system_deps

    echo "[2/7] 创建运行用户..."
    if ! id "$APP_USER" &>/dev/null; then
        useradd --system --no-create-home --shell "$NOLOGIN_PATH" "$APP_USER"
        echo "  -> 用户 $APP_USER 已创建 (shell: $NOLOGIN_PATH)"
    else
        echo "  -> 用户 $APP_USER 已存在"
    fi

    echo "[3/7] 部署应用文件..."
    mkdir -p "$APP_DIR"
    cp -r "$SCRIPT_DIR"/app.py "$SCRIPT_DIR"/templates "$SCRIPT_DIR"/static "$SCRIPT_DIR"/requirements.txt "$SCRIPT_DIR"/deploy.sh "$APP_DIR/"
    mkdir -p "$APP_DIR/downloads" "$APP_DIR/data"
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
    echo "  系统: $OS_PRETTY"
    echo "========================================"
    echo ""
    echo "  WebUI 地址:  http://<服务器IP>:${APP_PORT}"
    echo "  本机访问:    http://localhost:${APP_PORT}"
    echo ""
    echo "  管理后台:    http://<服务器IP>:${APP_PORT}/manage"
    echo "  ────────────────────────────────────────"
    echo "  ⚠️  默认管理员账号密码如下"
    echo "  登录后请立即修改密码！"
    echo "  ────────────────────────────────────────"

    # Display admin credentials if available
    sleep 3
    if [ -f "${APP_DIR}/data/admin_config.json" ]; then
        echo ""
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║        管理员账号信息                ║"
        echo "  ╠══════════════════════════════════════╣"
        ADMIN_USER=$(python3 -c "import json; print(json.load(open('${APP_DIR}/data/admin_config.json'))['username'])" 2>/dev/null || echo "admin")
        ADMIN_PASS=$(python3 -c "import json; print(json.load(open('${APP_DIR}/data/admin_config.json'))['password'])" 2>/dev/null || echo "admin123")
        echo "  ║  用户名: $ADMIN_USER"
        echo "  ║  密码:   $ADMIN_PASS"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        echo "  ⚠️  请妥善保存！登录后建议立即修改密码。"
    else
        echo ""
        echo "  默认账号: admin"
        echo "  默认密码: admin123"
        echo ""
        echo "  查看密码: sudo bash deploy.sh show-admin"
    fi

    echo ""
    echo "  常用命令:"
    echo "    查看状态:    systemctl status ${APP_NAME}"
    echo "    重启服务:    systemctl restart ${APP_NAME}"
    echo "    停止服务:    systemctl stop ${APP_NAME}"
    echo "    查看日志:    journalctl -u ${APP_NAME} -f"
    echo "    更新服务:    sudo bash ${APP_DIR}/deploy.sh update"
    echo "    卸载服务:    sudo bash ${APP_DIR}/deploy.sh uninstall"
    echo "    查看管理员:  sudo bash ${APP_DIR}/deploy.sh show-admin"
    echo "    重置管理员:  sudo bash ${APP_DIR}/deploy.sh reset-admin"
    echo ""
    echo "  生成的 YAML 文件保存在: ${APP_DIR}/downloads/"
    echo "  转换记录数据库:         ${APP_DIR}/data/records.db"
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
    echo "  支持多 Linux 发行版"
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
    echo "  5) 查看管理员账号"
    echo "  6) 重置管理员密码"
    echo "  0) 退出"
    echo ""
    read -p "请选择 [0-6]: " choice

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
        5)
            echo ""
            do_show_admin
            ;;
        6)
            echo ""
            do_reset_admin
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
    show-admin)
        do_show_admin
        ;;
    reset-admin)
        do_reset_admin
        ;;
    "")
        show_menu
        ;;
    *)
        echo "用法: $0 {install|update|uninstall|version|show-admin|reset-admin}"
        echo "  或直接运行 $0 进入交互菜单"
        exit 1
        ;;
esac
