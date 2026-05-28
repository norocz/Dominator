#!/usr/bin/env bash
# ============================================================================
# Domain Manager — interaktivní bootstrap
# Spuštění: sudo ./bootstrap.sh
# ============================================================================
set -euo pipefail

# --- barvy -------------------------------------------------------------------
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

ok()  { echo -e "${GRN}[✓]${RST} $*"; }
inf() { echo -e "${CYN}[·]${RST} $*"; }
hdr() { echo; echo -e "${BLD}${YLW}══ $* ══${RST}"; echo; }
err() { echo -e "${RED}[✗] $*${RST}" >&2; exit 1; }

ask() {
    # ask "Popis" VAR_NAME "výchozí hodnota"
    local prompt="$1" var="$2" default="${3:-}"
    local hint=""
    [[ -n "$default" ]] && hint=" ${CYN}[${default}]${RST}"
    echo -ne "${BLD}${prompt}${RST}${hint}: "
    read -r _ans
    [[ -z "$_ans" && -n "$default" ]] && _ans="$default"
    printf -v "$var" '%s' "$_ans"
}

ask_pass() {
    local prompt="$1" var="$2"
    while true; do
        echo -ne "${BLD}${prompt}${RST}: "
        read -rs _p1; echo
        echo -ne "${BLD}Potvrďte heslo${RST}: "
        read -rs _p2; echo
        if [[ "$_p1" == "$_p2" && ${#_p1} -ge 8 ]]; then
            printf -v "$var" '%s' "$_p1"
            break
        elif [[ ${#_p1} -lt 8 ]]; then
            echo -e "${RED}Heslo musí mít alespoň 8 znaků.${RST}"
        else
            echo -e "${RED}Hesla se neshodují.${RST}"
        fi
    done
}

ask_yn() {
    # ask_yn "Otázka?" → vrací 0 (ano) nebo 1 (ne)
    local prompt="$1" default="${2:-y}"
    local hint="[A/n]"; [[ "$default" == "n" ]] && hint="[a/N]"
    echo -ne "${BLD}${prompt}${RST} ${CYN}${hint}${RST}: "
    read -r _yn
    [[ -z "$_yn" ]] && _yn="$default"
    [[ "$_yn" =~ ^[AaYy] ]]
}

# --- práva -------------------------------------------------------------------
[[ $EUID -ne 0 ]] && err "Spusťte jako root (sudo ./bootstrap.sh)"

# --- systém ------------------------------------------------------------------
hdr "Kontrola systému"
if ! grep -qi "ubuntu" /etc/os-release; then
    echo -e "${YLW}[!] Tenhle projekt cílí na Ubuntu. Na jiném systému může selhat.${RST}"
    ask_yn "Pokračovat přesto?" "n" || exit 1
fi
. /etc/os-release
ok "Systém: ${PRETTY_NAME:-$(uname -s)}"

# --- uvítání -----------------------------------------------------------------
clear
echo -e "${BLD}"
cat << 'LOGO'
  ____                        _
 |  _ \  ___  _ __ ___   ___(_)_ __   __ _
 | | | |/ _ \| '_ ` _ \ / _ \ | '_ \ / _` |
 | |_| | (_) | | | | | |  __/ | | | | (_| |
 |____/ \___/|_| |_| |_|\___|_|_| |_|\__, |
  __  __                              |___/
 |  \/  | __ _ _ __   __ _  __ _  ___ _ __
 | |\/| |/ _` | '_ \ / _` |/ _` |/ _ \ '__|
 | |  | | (_| | | | | (_| | (_| |  __/ |
 |_|  |_|\__,_|_| |_|\__,_|\__, |\___|_|
                             |___/
LOGO
echo -e "${RST}"
echo "  Interaktivní instalátor — Samba AD pro školy"
echo "  Odpovídejte na otázky níže. Prázdný vstup = výchozí hodnota v [závorkách]."
echo

# ============================================================================
hdr "1 / 6 — Role serveru"
# ============================================================================

# Auto-detect hostname
DETECTED_HOST=$(hostname -s 2>/dev/null || echo "dc1")

echo "  Primární server (dc1): Samba AD master, DHCP active, PostgreSQL primary,"
echo "                         Pi-hole #1, web rozhraní, Ansible control node."
echo "  Sekundární server (dc2): AD slave, DHCP standby, PostgreSQL hot-standby,"
echo "                           Pi-hole #2, Prometheus, Grafana, Zabbix, Ubiquiti."
echo
echo -ne "${BLD}Role tohoto serveru${RST} ${CYN}[dc1/dc2]${RST}: "
read -r ROLE_INPUT
ROLE_INPUT="${ROLE_INPUT:-dc1}"
[[ "$ROLE_INPUT" =~ ^dc[12]$ ]] || err "Platné hodnoty: dc1 nebo dc2"
SERVER_ROLE="$ROLE_INPUT"
ok "Role: ${SERVER_ROLE}"

# ============================================================================
hdr "2 / 6 — Doménové nastavení"
# ============================================================================

ask "FQDN domény (velká písmena)"    REALM     "SKOLA.LOCAL"
REALM="${REALM^^}"
# NetBIOS = první část realm, max 15 znaků
DEFAULT_NETBIOS="${REALM%%.*}"
DEFAULT_NETBIOS="${DEFAULT_NETBIOS:0:15}"
ask "NetBIOS název (max 15 znaků)"   NETBIOS   "$DEFAULT_NETBIOS"
NETBIOS="${NETBIOS^^}"

echo
echo -e "  ${YLW}Administrátorské heslo domény — používá ho Samba a dm CLI.${RST}"
echo -e "  ${YLW}Musí splňovat Windows complexity (velká+malá+číslice+special).${RST}"
ask_pass "Heslo správce domény" ADMIN_PASS

ok "Doména: ${REALM}  (NetBIOS: ${NETBIOS})"

# ============================================================================
hdr "3 / 6 — Síťová nastavení"
# ============================================================================

# Pokus o auto-detect IP a interface
DETECTED_IF=$(ip route | awk '/^default/ {print $5; exit}' 2>/dev/null || echo "ens18")
DETECTED_IP=$(ip -4 addr show "$DETECTED_IF" 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1 || echo "")

echo "  Detekovaný interface: ${DETECTED_IF}  IP: ${DETECTED_IP:-???}"
echo

ask "Síťový interface DC1"   IF_DC1   "${DETECTED_IF}"
ask "Síťový interface DC2"   IF_DC2   "${DETECTED_IF}"

# Subnet z IP
if [[ -n "$DETECTED_IP" ]]; then
    IFS='.' read -r o1 o2 o3 _ <<< "$DETECTED_IP"
    DEFAULT_SUBNET="${o1}.${o2}.${o3}.0/24"
    DEFAULT_GW="${o1}.${o2}.${o3}.1"
else
    DEFAULT_SUBNET="192.168.0.0/24"
    DEFAULT_GW="192.168.0.1"
fi

ask "Síťová podsíť"          SUBNET   "$DEFAULT_SUBNET"
ask "Výchozí brána"          GATEWAY  "$DEFAULT_GW"

# IP serverů
BASE="${SUBNET%.*}"   # 192.168.0
ask "IP adresa DC1"          IP_DC1   "${BASE}.2"
ask "IP adresa DC2"          IP_DC2   "${BASE}.3"

# DNS forwarder
ask "DNS forwarder (upstream)" DNS_FWD "1.1.1.1"

ok "Síť: ${SUBNET}  brána: ${GATEWAY}  DC1: ${IP_DC1}  DC2: ${IP_DC2}"

# ============================================================================
hdr "4 / 6 — DHCP a rezervace"
# ============================================================================

if ask_yn "Zapnout DHCP server (ISC Kea)?" "y"; then
    DHCP_ENABLED="true"
    ask "DHCP pool — začátek"  POOL_START "${BASE}.100"
    ask "DHCP pool — konec"    POOL_END   "${BASE}.200"
    ask "Doba výpůjčky (s)"    LEASE_TIME "86400"
    ok "DHCP: ${POOL_START} – ${POOL_END}"
else
    DHCP_ENABLED="false"
    POOL_START="${BASE}.100"
    POOL_END="${BASE}.200"
    LEASE_TIME="86400"
    inf "DHCP přeskočeno."
fi

# ============================================================================
hdr "5 / 6 — Služby a hesla"
# ============================================================================

# Pi-hole
if ask_yn "Zapnout Pi-hole (blokování reklam + DNS)?" "y"; then
    PIHOLE_ENABLED="true"
    ask_pass "Heslo Pi-hole webového rozhraní" PIHOLE_PASS
else
    PIHOLE_ENABLED="false"
    PIHOLE_PASS="ZmenMePihole!"
    inf "Pi-hole přeskočen."
fi

# Monitoring (jen na dc2)
if [[ "$SERVER_ROLE" == "dc2" ]] || ask_yn "Zapnout monitoring (Prometheus/Grafana/Zabbix)?" "y"; then
    MON_ENABLED="true"
    ask_pass "Heslo Grafana admin"  GRAFANA_PASS
    ask_pass "Heslo Zabbix DB"      ZABBIX_DB_PASS
else
    MON_ENABLED="false"
    GRAFANA_PASS="ZmenMeGrafana!"
    ZABBIX_DB_PASS="ZmenMeZabbixDb!"
    inf "Monitoring přeskočen."
fi

# PostgreSQL
ask_pass "Heslo PostgreSQL (domainmgr uživatel)" PG_PASS
ask_pass "Heslo PostgreSQL replikace"             PG_REPL_PASS

# Secret key pro session
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(36))" 2>/dev/null \
             || cat /proc/sys/kernel/random/uuid | tr -d '-')

# ============================================================================
hdr "6 / 6 — Shrnutí a zápis konfigurace"
# ============================================================================

echo -e "  ${BLD}Co se zapíše do /etc/domain-manager/config.yaml:${RST}"
echo
printf "    %-25s %s\n" "Doména:"    "${REALM}"
printf "    %-25s %s\n" "NetBIOS:"   "${NETBIOS}"
printf "    %-25s %s\n" "Role:"      "${SERVER_ROLE}"
printf "    %-25s %s\n" "DC1 IP:"    "${IP_DC1}"
printf "    %-25s %s\n" "DC2 IP:"    "${IP_DC2}"
printf "    %-25s %s\n" "Podsíť:"    "${SUBNET}"
printf "    %-25s %s\n" "Brána:"     "${GATEWAY}"
printf "    %-25s %s\n" "DHCP:"      "${DHCP_ENABLED} (${POOL_START}–${POOL_END})"
printf "    %-25s %s\n" "Pi-hole:"   "${PIHOLE_ENABLED}"
printf "    %-25s %s\n" "Monitoring:""${MON_ENABLED}"
echo
ask_yn "Pokračovat s instalací?" "y" || { inf "Instalace zrušena."; exit 0; }

# --- instalace Python --------------------------------------------------------
hdr "Instalace závislostí"
inf "apt update + python3 + git…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    build-essential git ca-certificates curl
ok "Systémové balíčky OK"

# --- cílový adresář ----------------------------------------------------------
INSTALL_DIR="/opt/domain-manager"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$PROJECT_DIR" != "$INSTALL_DIR" ]]; then
    inf "Kopíruji projekt do ${INSTALL_DIR}…"
    mkdir -p "$INSTALL_DIR"
    rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
          "$PROJECT_DIR/" "$INSTALL_DIR/" 2>/dev/null \
    || cp -r "$PROJECT_DIR/." "$INSTALL_DIR/"
fi
cd "$INSTALL_DIR"

# --- virtualenv + pip --------------------------------------------------------
inf "Vytvářím virtualenv…"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip setuptools wheel
inf "Instaluji Domain Manager…"
pip install --quiet -e .
ln -sf "$INSTALL_DIR/.venv/bin/dm" /usr/local/bin/dm
ok "Python balíčky OK"

# --- datové adresáře ---------------------------------------------------------
mkdir -p /var/lib/domain-manager/{uploads,ansible/inventory,ansible/playbooks}
mkdir -p /var/log/domain-manager
mkdir -p /etc/domain-manager
ok "Adresáře OK"

# --- zápis config.yaml -------------------------------------------------------
CONFIG_PATH="/etc/domain-manager/config.yaml"
if [[ -f "$CONFIG_PATH" ]]; then
    cp "$CONFIG_PATH" "${CONFIG_PATH}.bak.$(date +%Y%m%d%H%M%S)"
    inf "Stávající config zálohován."
fi

cat > "$CONFIG_PATH" << YAML
# Domain Manager — vygenerováno bootstrap.sh $(date '+%Y-%m-%d %H:%M')
# NECOMMITUJTE tento soubor do gitu (je v .gitignore).

domain:
  realm: "${REALM}"
  netbios: "${NETBIOS}"
  admin_password: "${ADMIN_PASS}"
  dns_forwarder: ${DNS_FWD}

network:
  subnet: ${SUBNET}
  gateway: ${GATEWAY}
  dns_servers:
    - ${IP_DC1}
    - ${IP_DC2}

servers:
  dc1:
    hostname: dc1
    ip: ${IP_DC1}
    interface: ${IF_DC1}
    role: primary
  dc2:
    hostname: dc2
    ip: ${IP_DC2}
    interface: ${IF_DC2}
    role: secondary

dhcp:
  enabled: ${DHCP_ENABLED}
  pool_start: ${POOL_START}
  pool_end: ${POOL_END}
  lease_time_seconds: ${LEASE_TIME}
  reservations: []

pihole:
  enabled: ${PIHOLE_ENABLED}
  webpassword: "${PIHOLE_PASS}"
  upstream_dns:
    - 1.1.1.1
    - 9.9.9.9
  dns_port: 5353
  web_port: 8081

monitoring:
  enabled: ${MON_ENABLED}
  prometheus:
    port: 9090
    retention_days: 30
  grafana:
    port: 3000
    admin_password: "${GRAFANA_PASS}"
  zabbix:
    port: 8082
    db_password: "${ZABBIX_DB_PASS}"

postgres:
  enabled: true
  db_name: domainmgr
  db_user: domainmgr
  db_password: "${PG_PASS}"
  replication_password: "${PG_REPL_PASS}"

manager:
  enabled: true
  bind_host: 0.0.0.0
  bind_port: 8000
  secret_key: "${SECRET_KEY}"
  session_timeout_minutes: 120
  uploads_dir: /var/lib/domain-manager/uploads

firewall:
  enabled: true
  default_policy: drop
  trusted_networks:
    - ${SUBNET}
  egress_always:
    - { proto: udp, port: 53 }
    - { proto: udp, port: 123 }
    - { proto: tcp, port: 443 }
    - { proto: tcp, port: 80 }

ansible:
  enabled: true
  inventory_path: /var/lib/domain-manager/ansible/inventory
  playbooks_path: /var/lib/domain-manager/ansible/playbooks
YAML

chmod 600 "$CONFIG_PATH"
ok "Konfigurace zapsána do ${CONFIG_PATH}"

# --- validace ----------------------------------------------------------------
inf "Validuji konfiguraci…"
dm config validate && ok "Konfigurace je validní." || {
    echo -e "${YLW}[!] Validace selhala — zkontrolujte ${CONFIG_PATH}${RST}"
}

# --- hotovo ------------------------------------------------------------------
echo
echo -e "${BLD}${GRN}╔══════════════════════════════════════════════════════╗${RST}"
echo -e "${BLD}${GRN}║  Bootstrap dokončen!                                 ║${RST}"
echo -e "${BLD}${GRN}╚══════════════════════════════════════════════════════╝${RST}"
echo
echo -e "  Další kroky pro ${BLD}${SERVER_ROLE}${RST}:"
echo
if [[ "$SERVER_ROLE" == "dc1" ]]; then
    echo -e "    ${CYN}sudo dm install dc1${RST}   # nainstaluje Sambu, DHCP, Pi-hole…"
    echo -e "    ${CYN}sudo dm web start${RST}     # spustí webové rozhraní (:8000)"
else
    echo -e "    ${CYN}sudo dm install dc2${RST}   # nainstaluje AD slave, monitoring…"
    echo -e "    ${CYN}sudo dm web start${RST}     # spustí webové rozhraní (:8000)"
fi
echo
echo -e "  Konfigurace: ${BLD}/etc/domain-manager/config.yaml${RST}"
echo -e "  Logy:        ${BLD}/var/log/domain-manager/${RST}"
echo
