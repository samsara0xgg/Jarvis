#!/usr/bin/env bash
# Jarvis Pi 一键安装脚本
# 用法: bash deploy/install.sh
set -euo pipefail

JARVIS_DIR="/home/pi/jarvis"
VENV_DIR="$JARVIS_DIR/.venv"

echo "=========================================="
echo "  Jarvis Pi 安装脚本"
echo "=========================================="

# 1. 系统依赖
echo "[1/6] 安装系统依赖..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    portaudio19-dev libatlas-base-dev \
    mosquitto mosquitto-clients \
    i2c-tools python3-smbus \
    ffmpeg mpv \
    git

# 2. 启用 I2C（OLED 需要）
echo "[2/6] 启用 I2C..."
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm=on" | sudo tee -a /boot/firmware/config.txt
    echo "  I2C 已启用（需要重启生效）"
fi
sudo modprobe i2c-dev 2>/dev/null || true

# 3. 配置 Mosquitto
echo "[3/6] 配置 MQTT Broker..."
if [ -f deploy/mosquitto.conf ]; then
    sudo cp deploy/mosquitto.conf /etc/mosquitto/conf.d/jarvis.conf
fi
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

# 4. Python 虚拟环境
echo "[4/6] 创建 Python 虚拟环境..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 5. 安装 systemd 服务
echo "[5/6] 安装 systemd 服务..."
sudo cp deploy/jarvis.service /etc/systemd/system/jarvis.service
sudo systemctl daemon-reload
sudo systemctl enable jarvis.service

# 6. 创建数据目录
echo "[6/6] 创建数据目录..."
mkdir -p data/conversations data/memory data/todos

echo ""
echo "=========================================="
echo "  安装完成！"
echo "=========================================="
echo ""
echo "  启动 Jarvis:  sudo systemctl start jarvis"
echo "  查看日志:     journalctl -u jarvis -f"
echo "  停止 Jarvis:  sudo systemctl stop jarvis"
echo ""
echo "  注意: 如果刚启用 I2C，请先重启: sudo reboot"
echo ""
