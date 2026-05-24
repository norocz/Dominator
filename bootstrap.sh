#!/usr/bin/env bash
# ============================================================================
# Domain Manager - bootstrap
# ----------------------------------------------------------------------------
# JEDINÝ bash skript v projektu. Jeho úkol je dostat systém do stavu, kdy
# existuje funkční Python + tento projekt nainstalovaný. Pak už všechno
# (instalaci Samby, DHCP, Pi-hole, FW...) řídí `dm` v Pythonu.
#
# Použití:
#   sudo ./bootstrap.sh
#
# Po doběhnutí:
#   sudo dm --help
#   sudo dm install dc1     # nebo dc2
# ============================================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERR: Spusťte jako root (sudo)." >&2
    exit 1
fi

# --- 1. Ověření systému ----------------------------------------------------
if ! grep -q "Ubuntu" /etc/os-release; then
    echo "ERR: Tento projekt cílí na Ubuntu 26.04 LTS." >&2
    exit 1
fi
. /etc/os-release
echo "[*] Systém: $PRETTY_NAME"

# --- 2. Instalace systémových závislostí Pythonu --------------------------
# Záměrně minimální - zbytek doinstalujeme z Pythonu skrze apt přes náš runner.
echo "[*] Instaluji Python a základní systémové balíčky..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    build-essential \
    git \
    ca-certificates \
    curl

# --- 3. Cílový adresář -----------------------------------------------------
INSTALL_DIR="/opt/domain-manager"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$PROJECT_DIR" != "$INSTALL_DIR" ]]; then
    echo "[*] Kopíruji projekt do $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    cp -r "$PROJECT_DIR/." "$INSTALL_DIR/"
fi
cd "$INSTALL_DIR"

# --- 4. Virtuální prostředí ------------------------------------------------
echo "[*] Vytvářím virtualenv v $INSTALL_DIR/.venv..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip setuptools wheel

echo "[*] Instaluji Domain Manager a závislosti..."
pip install --quiet -e .

# --- 5. Symlink na `dm` ----------------------------------------------------
ln -sf "$INSTALL_DIR/.venv/bin/dm" /usr/local/bin/dm

# --- 6. Data adresáře ------------------------------------------------------
mkdir -p /var/lib/domain-manager/uploads
mkdir -p /var/lib/domain-manager/ansible/inventory
mkdir -p /var/lib/domain-manager/ansible/playbooks
mkdir -p /var/log/domain-manager
mkdir -p /etc/domain-manager

# --- 7. Konfigurační soubor ------------------------------------------------
if [[ ! -f /etc/domain-manager/config.yaml ]]; then
    cp "$INSTALL_DIR/config.yaml.example" /etc/domain-manager/config.yaml
    chmod 600 /etc/domain-manager/config.yaml
    echo
    echo "============================================================"
    echo " Konfigurace zkopírována do /etc/domain-manager/config.yaml"
    echo " UPRAVTE JI před spuštěním instalace:"
    echo "   sudo nano /etc/domain-manager/config.yaml"
    echo "============================================================"
fi

echo
echo "[OK] Bootstrap dokončen."
echo "    Další kroky:"
echo "      1) sudo nano /etc/domain-manager/config.yaml"
echo "      2) sudo dm config validate"
echo "      3) sudo dm install dc1   # na primárním serveru"
echo "         sudo dm install dc2   # na sekundárním serveru"
