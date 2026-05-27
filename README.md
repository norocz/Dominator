# Domain Manager — Dominator

Centrální správa Samba AD domény, DHCP, Pi-hole, monitoringu a klientů
z jednoho Python prostředí. Cíleno na Ubuntu 26.04 LTS.

## Co to dělá

- Postaví dvojici Samba AD řadičů domény (DC1 primary, DC2 secondary)
- Spravuje DHCP (ISC Kea) s MAC → IP rezervacemi a one-click rezervací z aktivních lease
- Provozuje Pi-hole DNS filter — správa adlistů, skupin, blokování klientů
- Sjednocuje monitoring (Prometheus + Grafana + Zabbix + SNMP)
- Web UI s plánkem budovy, na kterém vidíte rozmístěné PC
- CRUD správa počítačů, uživatelů a skupin přímo v AD
- Hromadný import z CSV přes web i CLI (počítače, uživatelé, skupiny)
- Politiky s dědičností: `computer_group → computer → user_group → user`
- Volání Ansible playbooků pro vzdálenou konfiguraci klientů
- Audit log všech akcí provedených přes webové rozhraní
- Nápověda se sekcí připojení klientů do domény (Windows + Linux)

## Topologie

```
                          ┌────────────────────┐
                          │   DC1 (primary)    │
                          │  192.168.10.10     │
                          │                    │
                          │  • Samba AD DC     │
                          │  • Kea DHCP        │
                          │  • Pi-hole #1      │
                          │  • PostgreSQL pri  │
                          │  • Manager web     │
                          │  • Ansible control │
                          └────────┬───────────┘
                                   │
                                   │ AD replikace (DRS)
                                   │ DB streaming
                                   │ Pi-hole gravity-sync
                                   │
                          ┌────────┴───────────┐
                          │   DC2 (secondary)  │
                          │  192.168.10.11     │
                          │                    │
                          │  • Samba AD DC     │
                          │  • Kea DHCP HA     │
                          │  • Pi-hole #2      │
                          │  • PostgreSQL std  │
                          │  • Prometheus      │
                          │  • Grafana         │
                          │  • Zabbix          │
                          └────────────────────┘
```

## Instalace

### 1) Příprava obou serverů

Čistá instalace Ubuntu 26.04 LTS. Statická IP nastaví instalátor podle `config.yaml`.

> **Důležité při instalaci přes SSH:** Instalátor při `netplan apply` změní IP
> z DHCP na statickou — SSH spojení na starou adresu se přeruší. Instalátor
> před tím zobrazí varování a 10 sekund čeká. **Samba AD DC nespouští DHCP**
> — stávající DHCP server nevypínejte, dokud nespustíte Kea.

### 2) Bootstrap

Na **obou** strojích (jako root):

```bash
git clone <tento-repo> /tmp/domain-manager
cd /tmp/domain-manager
sudo ./bootstrap.sh
```

Bootstrap: nainstaluje Python, vytvoří virtualenv, nainstaluje balíček,
vytvoří `/etc/domain-manager/config.yaml` ze vzoru, symlink `/usr/local/bin/dm`.

### 3) Konfigurace

Upravte na **obou** serverech identicky:

```bash
sudo nano /etc/domain-manager/config.yaml
sudo dm config validate
```

### 4) Instalace DC1 (primární stroj)

```bash
sudo dm install dc1
```

- Nastaví hostname, `/etc/hosts`, netplan
- Zakáže `systemd-resolved` stub listener (port 53 pro Samba DNS)
- Zastaví a maskuje konfliktní služby (smbd, nmbd, winbind)
- Nainstaluje `samba-ad-dc` a závislosti
- Spustí `samba-tool domain provision` s parametry z configu
- Nakonfiguruje Kerberos, chrony, DNS forwarder
- Spustí `samba-ad-dc` systemd unit

Smoke test: `samba-tool domain info 127.0.0.1`

### 5) Instalace DC2 (sekundární stroj)

DC1 musí být dostupný a běžet. Pak:

```bash
sudo dm install dc2
```

- Ověří ping a Kerberos port 88 na DC1 před joinem
- Nastaví statickou DNS na DC1 (nutné pro `samba-tool domain join`)
- Nakonfiguruje krb5.conf s explicitním KDC (bez DNS SRV lookup)
- Spustí `samba-tool domain join` a ověří DRS replikaci

### 6) Doplňkové komponenty

```bash
# DC1:
sudo dm install dhcp
sudo dm install postgres --role primary
sudo dm install docker
sudo dm install pihole --instance 1
sudo dm install firewall
sudo dm web install-service

# DC2:
sudo dm install postgres --role standby
sudo dm install docker
sudo dm install pihole --instance 2
sudo dm install monitoring   # Prometheus + Grafana + Zabbix
sudo dm install firewall
```

### 7) Spuštění webu

```bash
sudo systemctl start domain-manager
# nebo pro vývoj:
DM_DEMO=1 dm web start --reload
```

Otevřete `http://<dc1_ip>:8000/`

## Přihlášení do webového rozhraní

Používá **Active Directory přihlašovací údaje** — žádná vlastní databáze uživatelů neexistuje.

| Pole | Hodnota |
|------|---------|
| Uživatelské jméno | `Administrator` (nebo jiný člen Domain Admins / DM-Admins) |
| Heslo | hodnota `domain.admin_password` z `/etc/domain-manager/config.yaml` |

Heslo si připomenete přímo ze serveru:

```bash
sudo grep admin_password /etc/domain-manager/config.yaml
```

Přihlásit se mohou pouze členové skupiny `Domain Admins` nebo `DM-Admins`.
Pro delegovaný přístup bez plných AD admin práv:

```bash
samba-tool group add DM-Admins
samba-tool group addmembers DM-Admins <username>
```

Demo režim — přijme jakékoliv přihlašovací údaje, pracuje s ukázkovými daty:

```bash
DM_DEMO=1 dm web start
```

## Připojení klientů do domény

**Předpoklad:** klient musí mít jako primární DNS nastavenou IP DC1.

### Windows

1. Systém → Název počítače → Změnit → Doména: `<REALM>` (např. `SKOLA.LOCAL`)
2. Zadat přihlašovací údaje Domain Admins
3. Restart

### Linux (Ubuntu/Debian)

```bash
sudo apt install -y realmd sssd sssd-tools adcli
sudo realm discover SKOLA.LOCAL
sudo realm join SKOLA.LOCAL -U Administrator
```

## Inventář počítačů

Model `Computer` v DB má 59 sloupců. Filtrovatelná tabulka `/computers` podporuje:

- Fulltext (`?q=ThinkPad`)
- Rovnost, porovnání, substring, boolean, řazení, volba sloupců, stránkování
- Operátory: `__eq` `__ne` `__lt` `__le` `__gt` `__ge` `__in` `__notin`
  `__contains` `__starts` `__ends` `__is_null` `__not_null`

## Import z CSV

```bash
# CLI:
sudo dm import groups groups.csv
sudo dm import users users.csv --apply
sudo dm import computers computers.csv --apply
```

Bez `--apply` je dry-run. Import funguje i přes web UI (tlačítko **↑ Import CSV**).

### Formát CSV — počítače

Povinný sloupec: `hostname`. Ostatní volitelné.

```
hostname,mac,ip,department,building,floor,room,os_family,primary_user,warranty_until
pc-01,aa:bb:cc:dd:ee:01,192.168.10.101,Učebny,A,0,U01,windows,novak,2027-06-30
```

### Formát CSV — uživatelé

Povinné: `username`, `first_name`, `last_name`.

```
username,first_name,last_name,email,phone,department,job_title,manager
novakj,Jan,Novák,novak@skola.cz,+420123456789,Učitelé,Učitel,rednik
```

### Formát CSV — skupiny

```
name,kind,description
Ucitele,user,Skupina učitelů
PC-Ucebna1,computer,Počítače v učebně 1
```

Záhlaví lze psát česky i anglicky. Existující záznamy se aktualizují (upsert).

## Adresářová struktura

```
/opt/domain-manager/
├── bootstrap.sh
├── pyproject.toml
├── config.yaml.example
├── .venv/
└── domain_manager/
    ├── cli.py                   # `dm` příkaz
    ├── config.py                # validace config.yaml
    ├── runner.py                # spouštění shell + logy
    ├── installers/              # DC1, DC2, Kea, Pi-hole, PG, Docker, FW...
    ├── ad/                      # AD klient (ldap3 + samba-tool) + importéři
    ├── db/                      # SQLAlchemy modely (Computer 59 sl., User 27 sl.)
    ├── web/
    │   ├── app.py               # FastAPI aplikace
    │   ├── _templates.py        # sdílená Jinja2Templates instance
    │   ├── _audit.py            # audit log helper
    │   ├── routes/              # endpoints (computers, users, groups, dhcp, ...)
    │   ├── templates/           # Jinja2 šablony
    │   └── static/              # vendor JS/CSS (htmx, CodeMirror) — vše lokální
    └── ansible/                 # playbooky pro klienty

/etc/domain-manager/
└── config.yaml                  # (chmod 600, obsahuje hesla)

/var/lib/domain-manager/
├── uploads/                     # nahrané plánky budov
└── ansible/                     # inventáře + playbooky

/var/log/domain-manager/
└── dm.log
```

## Roadmap

### Fáze 1 — Kostra ✅

- [x] Bootstrap, CLI, config validace
- [x] Instalátor DC1 (Samba AD primary) + oprava port 53 / SSH disconnect
- [x] Instalátor DC2 (Samba AD join) + oprava kinit / KDC lookup
- [x] Instalátory Docker, Pi-hole, monitoring, Kea, FW, PostgreSQL
- [x] AD klient (samba-tool + ldap3)
- [x] CSV importéři (users, computers, groups) s upsert do AD i DB
- [x] DB modely (SQLAlchemy) — Computer 59 sl., User 27 sl.
- [x] Engine pro dědičnost politik (computer_group → computer → user_group → user)
- [x] Kostra FastAPI webu (login + dashboard)
- [x] Filtrovatelná tabulka inventáře s HTMX (10+ operátorů, řazení, volba sloupců)
- [x] Detail počítače s editačními poli
- [x] Ukázková data v `examples/`

### Fáze 2 — Web UI ✅

- [x] CRUD uživatelé — vytvoření, editace, smazání, přiřazení do skupin
- [x] CRUD skupiny — vytvoření, smazání, AD sync přes samba-tool
- [x] CSV import přes web UI — počítače, uživatelé, skupiny (tlačítko v záhlaví)
- [x] Upload a editace plánků budov (SVG/PNG/JPG)
- [x] Drag & drop ikon zařízení na plánek, kontextové menu, info panel
- [x] Blokování internetu počítači / skupinám (Ansible + Pi-hole)
- [x] Editor politik (formuláře per kind, dědičnost v UI)

### Fáze 3 — Integrace ✅

- [x] DHCP rezervace přes Kea Control Agent API (list, add, delete)
- [x] One-click "Rezervovat" z tabulky aktivních lease
- [x] Sync Kea rezervací → DB počítačů
- [x] Pi-hole API klient — skupiny, klienti, blokování/odblokování
- [x] Pi-hole adlisty — přidání, odebrání, zapnutí/vypnutí, preset knihovna (14 listů)
- [x] Gravity update s HTMX in-page statusem
- [x] Ansible runner s real-time výstupem jobů v UI
- [x] Editor playbooků (CodeMirror + YAML, snippety, save/delete)
- [x] SNMP klient pro switche a routerboard (monitoring stavu portů)

### Fáze 4 — Polish ✅ (částečně)

- [x] Audit log v UI — filtrování, export CSV
- [x] Backup/restore tlačítko (DB dump + config)
- [x] Zdraví systému — ping, porty, latence (DC1, DC2, Pi-hole, DHCP)
- [x] Správa TLS certifikátů — přehled, platnost, upozornění
- [x] Nápověda `/help` — 17 sekcí, sticky TOC, scroll tracking
- [x] Přihlašování přes AD s kontrolou skupiny (Domain Admins / DM-Admins)
- [ ] Live HW info z Prometheus v detailu počítače (CPU, RAM, disk)
- [ ] Proklik do Grafany pro detailní grafy
- [ ] PostgreSQL hot standby kompletně automaticky
- [ ] Automatický failover (Patroni nebo manuálně přes UI)
- [ ] Replikace Pi-hole přes gravity-sync

## Bezpečnostní poznámky

- `/etc/domain-manager/config.yaml` má chmod 600 a obsahuje hesla.
  Pro produkci doporučujeme načítat hesla z env nebo HashiCorp Vault.
- Web UI poslouchá na `0.0.0.0:8000` — omezte přes firewall jen na admin síť.
- Přihlášení vyžaduje členství v `Domain Admins` nebo `DM-Admins`.
  Řadoví AD uživatelé se přihlásit nemohou.
- Hesla v CSV při importu jsou nouzové řešení — lepší je vygenerovat silné
  heslo automaticky a předat ho uživateli odděleně.
- Jinja2 `cache_size=0` je workaround pro bug v Jinja2 3.2.x na Python 3.14
  (dict v LRU cache key). Pin závislosti: `jinja2>=3.1,<3.2`.

## Co tu schválně NENÍ

- **Žádný Bash kromě `bootstrap.sh`.** Vše ostatní řídí Python.
- **Žádný JavaScript framework.** Web je server-rendered Jinja2 + HTMX.
  Všechny vendor závislosti jsou lokální (`static/vendor/`) — funguje offline.
- **Žádná abstraktní pluginová architektura.** Konkrétní instalátory,
  konkrétní modely. Snazší na pochopení a ladění.

## Vývojové prostředí

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Demo bez AD serveru:
DM_DEMO=1 dm web start --reload

ruff check .
pytest
```
