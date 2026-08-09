#!/bin/bash
# ============================================================
# VLESS to Clash YAML 转换工具 - 远程一键安装脚本
# 作者: saeson
# 仓库: https://github.com/saeson001/vless2clash
# 支持: Debian 11/12/13, Ubuntu, CentOS 7/8/9, RHEL 8/9,
#       Rocky Linux, AlmaLinux, Fedora, openSUSE, Arch Linux
#
# 用法 (在 VPS 上执行一行命令即可):
#
#   方式一 (推荐):
#     bash <(curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh)
#
#   方式二:
#     curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh -o install.sh && bash install.sh
#
#   更新:
#     bash <(curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh) -- update
#
#   卸载:
#     bash <(curl -sL https://raw.githubusercontent.com/saeson001/vless2clash/main/install.sh) -- uninstall
#
# ============================================================

set -e

# 配置
GITHUB_REPO="saeson001/vless2clash"
GITHUB_BRANCH="main"
APP_NAME="vless2clash"
APP_DIR="/opt/vless2clash"
TEMP_DIR="/tmp/vless2clash_install_$$"

# 解析参数
ACTION="auto"
for arg in "$@"; do
    case "$arg" in
        install)   ACTION="install" ;;
        update)    ACTION="update" ;;
        uninstall) ACTION="uninstall" ;;
        --)        ;;
        *)         ;;
    esac
done

# ============================================================
# 前置检查
# ============================================================

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then
    echo "[错误] 请使用 root 用户或 sudo 运行此脚本"
    echo "  示例: sudo bash install.sh"
    exit 1
fi

# 检查操作系统
if [ ! -f /etc/os-release ]; then
    echo "[错误] 无法检测操作系统（缺少 /etc/os-release）"
    exit 1
fi

. /etc/os-release
OS_ID="${ID:-unknown}"
OS_VERSION="${VERSION_ID:-unknown}"
OS_PRETTY="${PRETTY_NAME:-$OS_ID $OS_VERSION}"
echo "[信息] 检测到系统: $OS_PRETTY"

# 检测发行版家族
OS_FAMILY="unknown"
case "$OS_ID" in
    debian|ubuntu|linuxmint|pop)
        OS_FAMILY="debian"
        ;;
    centos|rhel|rocky|almalinux|ol|fedora|amzn)
        OS_FAMILY="rhel"
        ;;
    opensuse*|suse|sles)
        OS_FAMILY="suse"
        ;;
    arch|manjaro|endeavouros)
        OS_FAMILY="arch"
        ;;
    alpine)
        OS_FAMILY="alpine"
        ;;
    *)
        # 尝试通过可用包管理器推断
        if command -v apt-get &>/dev/null; then
            OS_FAMILY="debian"
        elif command -v dnf &>/dev/null; then
            OS_FAMILY="rhel"
        elif command -v yum &>/dev/null; then
            OS_FAMILY="rhel"
        elif command -v zypper &>/dev/null; then
            OS_FAMILY="suse"
        elif command -v pacman &>/dev/null; then
            OS_FAMILY="arch"
        elif command -v apk &>/dev/null; then
            OS_FAMILY="alpine"
        fi
        ;;
esac

if [ "$OS_FAMILY" = "unknown" ]; then
    echo "[错误] 不支持的发行版: $OS_ID"
    echo "  支持的发行版: Debian, Ubuntu, CentOS, RHEL, Rocky, AlmaLinux, Fedora, openSUSE, Arch, Alpine"
    exit 1
fi

echo "[信息] 发行版家族: $OS_FAMILY"

# ============================================================
# 安装系统依赖 (跨发行版)
# ============================================================
echo ""
echo "[1/4] 安装系统依赖..."

install_deps() {
    case "$OS_FAMILY" in
        debian)
            apt-get update -qq 2>/dev/null || true
            apt-get install -y -qq curl wget unzip git > /dev/null 2>&1
            ;;
        rhel)
            if command -v dnf &>/dev/null; then
                dnf install -y -q curl wget unzip git > /dev/null 2>&1
            else
                yum install -y -q curl wget unzip git > /dev/null 2>&1
            fi
            # CentOS 7 可能需要 EPEL
            if [ "$OS_ID" = "centos" ] && [[ "$OS_VERSION" == 7* ]]; then
                yum install -y -q epel-release > /dev/null 2>&1 || true
            fi
            ;;
        suse)
            zypper --non-interactive --quiet install curl wget unzip git > /dev/null 2>&1
            ;;
        arch)
            pacman --noconfirm --needed -S curl wget unzip git > /dev/null 2>&1
            ;;
        alpine)
            apk add --quiet curl wget unzip git bash > /dev/null 2>&1
            ;;
    esac
}

install_deps
echo "  -> 系统依赖已就绪"

# ============================================================
# 检测已安装状态，决定动作
# ============================================================
if [ "$ACTION" = "auto" ]; then
    if [ -d "$APP_DIR" ] && [ -f "${APP_DIR}/VERSION" ]; then
        INSTALLED_VER=$(cat "${APP_DIR}/VERSION")
        echo "  -> 检测到已安装版本: $INSTALLED_VER"
        ACTION="update"
    else
        echo "  -> 未检测到已安装版本，将执行全新部署"
        ACTION="install"
    fi
fi

# ============================================================
# 卸载模式 - 直接下载 deploy.sh 执行卸载
# ============================================================
if [ "$ACTION" = "uninstall" ]; then
    echo ""
    echo "[2/4] 下载卸载脚本..."
    mkdir -p "$TEMP_DIR"
    wget -q -O "${TEMP_DIR}/deploy.sh" \
        "https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/deploy.sh" 2>/dev/null || \
    curl -sL -o "${TEMP_DIR}/deploy.sh" \
        "https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/deploy.sh"

    if [ ! -s "${TEMP_DIR}/deploy.sh" ]; then
        echo "[错误] 下载 deploy.sh 失败"
        rm -rf "$TEMP_DIR"
        exit 1
    fi
    echo "  -> 卸载脚本已下载"

    echo ""
    echo "[3/4] 执行卸载..."
    bash "${TEMP_DIR}/deploy.sh" uninstall

    rm -rf "$TEMP_DIR"
    exit 0
fi

# ============================================================
# 下载最新代码
# ============================================================
echo ""
echo "[2/4] 从 GitHub 下载最新代码..."

# 方式一: 下载 zip (不需要 git)
ZIP_URL="https://github.com/${GITHUB_REPO}/archive/refs/heads/${GITHUB_BRANCH}.zip"
ZIP_FILE="${TEMP_DIR}/${APP_NAME}.zip"

mkdir -p "$TEMP_DIR"

# 尝试下载
echo "  -> 下载地址: $ZIP_URL"
wget -q -O "$ZIP_FILE" "$ZIP_URL" 2>/dev/null || \
    curl -sL -o "$ZIP_FILE" "$ZIP_URL"

if [ ! -s "$ZIP_FILE" ]; then
    echo "[错误] 下载失败，请检查网络连接"
    echo "  手动下载: $ZIP_URL"
    rm -rf "$TEMP_DIR"
    exit 1
fi

# 验证是否为有效 zip
if ! unzip -tq "$ZIP_FILE" > /dev/null 2>&1; then
    echo "[错误] 下载的文件不是有效的 zip 压缩包"
    echo "  可能是 GitHub 限流或网络问题，请稍后重试"
    rm -rf "$TEMP_DIR"
    exit 1
fi

echo "  -> 下载完成 ($(du -h "$ZIP_FILE" | cut -f1))"

# ============================================================
# 解压
# ============================================================
echo ""
echo "[3/4] 解压文件..."
unzip -qo "$ZIP_FILE" -d "$TEMP_DIR"

# GitHub zip 解压后目录名格式: vless2clash-main
EXTRACTED_DIR="${TEMP_DIR}/${APP_NAME}-${GITHUB_BRANCH}"

if [ ! -d "$EXTRACTED_DIR" ]; then
    echo "[错误] 解压目录不存在: $EXTRACTED_DIR"
    ls -la "$TEMP_DIR"
    rm -rf "$TEMP_DIR"
    exit 1
fi

echo "  -> 解压完成"
echo "  -> 文件列表:"
find "$EXTRACTED_DIR" -type f -not -path "*/.git/*" | while read -r f; do
    echo "     $(basename "$f")"
done

# ============================================================
# 执行 deploy.sh
# ============================================================
echo ""
echo "[4/4] 执行部署脚本 ($ACTION)..."

# 将解压目录中的所有文件复制到 TEMP_DIR，确保 deploy.sh 的 SCRIPT_DIR 能找到 app.py 等文件
cp -r "$EXTRACTED_DIR"/* "$TEMP_DIR/" 2>/dev/null || true
cp -r "$EXTRACTED_DIR"/.gitignore "$TEMP_DIR/" 2>/dev/null || true
chmod +x "$TEMP_DIR/deploy.sh"

# 执行对应的动作
bash "$TEMP_DIR/deploy.sh" "$ACTION"

# 清理临时文件
rm -rf "$TEMP_DIR"

echo ""
echo "========================================"
echo "  远程安装流程结束"
echo "========================================"
echo ""
echo "  默认管理员账号: admin / admin123"
echo "  管理后台地址:   http://<服务器IP>:5000/manage"
echo ""
echo "  常用命令:"
echo "    更新:        bash <(curl -sL https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/install.sh) -- update"
echo "    卸载:        bash <(curl -sL https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/install.sh) -- uninstall"
echo "    查看管理员:  sudo bash ${APP_DIR}/deploy.sh show-admin"
echo "    重置管理员:  sudo bash ${APP_DIR}/deploy.sh reset-admin"
echo "========================================"
