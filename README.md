# Domain Manager

Centrální správa Samba AD domény, DHCP, Pi-hole, monitoringu a klientů
z jednoho Python prostředí. Cíleno na Ubuntu 26.04 LTS.

## Co to dělá

- Postaví dvojici Samba AD řadičů domény (DC1 primary, DC2 secondary)
- Spravuje DHCP (ISC Kea) s MAC → IP rezervacemi
- Provozuje Pi-hole DNS filter (2 instance s replikací)
- Sjednocuje monitoring (Prometheus + Grafana + Zabbix)
- Web UI s plánkem budovy, na kterém vidíte rozmístěné PC
- Hromadný import uživatelů, počítačů a skupin z CSV
- Politiky s dědičností: `computer_group → computer → user_group → user`
- Volání Ansible playbooků pro vzdálenou konfiguraci klientů

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

Čistá instalace Ubuntu 26.04 LTS, statická IP přes /etc/netplan nebo přes
náš instalátor (přepíše ji podle configu).

### 2) Bootstrap

Na **obou** strojích (jako root):

```bash
git clone <tento-repo> /tmp/domain-manager
cd /tmp/domain-manager
sudo ./bootstrap.sh
```

Bootstrap udělá pouze: nainstaluje Python, vytvoří virtualenv,
nainstaluje tento balíček, vytvoří `/etc/domain-manager/config.yaml`
ze vzoru a vytvoří symlink `/usr/local/bin/dm`.

### 3) Konfigurace

Upravte na **obou** serverech identicky:

```bash
sudo nano /etc/domain-manager/config.yaml
sudo dm config validate
```

`dm config validate` ověří, že je config konzistentní (IP v subnetu,
hesla dostatečně silná, FQDN správně, atd.).

### 4) Instalace DC1 (jen na primárním stroji)

```bash
sudo dm install dc1
```

Tohle:
- Nastaví hostname, /etc/hosts, netplan podle configu
- Zastaví konfliktní služby (smbd, nmbd, winbind)
- Nainstaluje `samba-ad-dc` a závislosti
- Spustí `samba-tool domain provision` s parametry z configu
- Nakonfiguruje Kerberos, chrony, DNS forwarder
- Spustí `samba-ad-dc` systemd unit

Smoke test: `samba-tool domain info 127.0.0.1`.

### 5) Instalace DC2 (jen na sekundárním stroji)

DC1 musí být dostupný a běžet. Pak:

```bash
sudo dm install dc2
```

Provede `samba-tool domain join` - připojí se do existující domény.

### 6) Doplňkové komponenty

V libovolném pořadí, podle potřeby:

```bash
# DC1:
sudo dm install dhcp         # ISC Kea
sudo dm install postgres --role primary
sudo dm install docker
sudo dm install pihole --instance 1
sudo dm install firewall
sudo dm web install-service  # systemd unit pro web

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
sudo dm web start --reload
```

Otevřete `http://<dc1_ip>:8000/`.

## Inventář počítačů

Model `Computer` v DB má 59 sloupců rozdělených do logických bloků
(identita, síť, hardware, OS, doména, lokace, životní cyklus, monitoring,
software, vlastní pole). Indexovány jsou sloupce, podle kterých se reálně
filtruje (hostname, MAC, IP, OS, RAM, oddělení, lokace, stav, atd.).

**Filtrovatelná tabulka** v `/computers` umí přes URL parametry:

- Fulltext (`?q=ThinkPad`) — hledá v hostname, S/N, modelu, primárním uživateli, poznámkách…
- Rovnost (`?os_family=windows&department=Účetní`)
- Porovnání (`?ram_mb__ge=16384`)
- Členství (`?form_factor__in=laptop,server`)
- Substring (`?manufacturer__contains=Lenovo`)
- Boolean (`?is_online=true`)
- Řazení (`?sort=-ram_mb`)
- Volba zobrazených sloupců (`?cols=hostname,os_family,ram_mb,department`)
- Stránkování (`?page=2&per_page=50`)

Operátory (jako suffix): `__eq` `__ne` `__lt` `__le` `__gt` `__ge`
`__in` `__notin` `__contains` `__starts` `__ends` `__is_null` `__not_null`.

UI v `/computers` je postavené nad HTMX — změna filtru způsobí refresh
jen těla tabulky, ne celé stránky, a URL se aktualizuje (bookmark/sdílení
odkazu funguje).

## Ukázková data

V `examples/`:

- `groups.csv` — 11 skupin (uživatelské + počítačové)
- `users.csv` — 5 uživatelů s kontakty
- `computers.csv` — 8 počítačů s plným inventářem (HW, OS, lokace, životní cyklus)

Postup nasazení ukázky:

```bash
sudo dm import groups examples/groups.csv
sudo dm import users examples/users.csv --apply
sudo dm import computers examples/computers.csv --apply
```



## Hromadný import z CSV

```bash
# Nejdřív skupiny:
sudo dm import groups groups.csv

# Pak uživatele a počítače (skupiny musí existovat):
sudo dm import users users.csv --apply
sudo dm import computers computers.csv --apply
```

Bez `--apply` běží v dry-run režimu - vypíše náhled, nic neuloží.

Formáty CSV viz `/opt/domain-manager/domain_manager/ad/importers.py`
(docstring na začátku souboru).

## Adresářová struktura

```
/opt/domain-manager/            # instalace
├── bootstrap.sh
├── pyproject.toml
├── config.yaml.example
├── .venv/                       # virtualenv (vytvoří bootstrap)
└── domain_manager/
    ├── cli.py                   # `dm` příkaz
    ├── config.py                # validace config.yaml
    ├── runner.py                # spouštění shell + logy
    ├── installers/              # instalátory komponent
    ├── ad/                      # AD klient + import + politiky
    ├── db/                      # SQLAlchemy modely
    ├── web/                     # FastAPI app + šablony
    └── ansible/                 # playbooky pro klienty

/etc/domain-manager/
└── config.yaml                  # běžící konfigurace (chmod 600)

/var/lib/domain-manager/
├── uploads/                     # nahrané plánky budov
└── ansible/                     # invertáře + playbooky

/var/log/domain-manager/
└── dm.log
```

## Roadmap

### Fáze 1 - Kostra (HOTOVO)
- [x] Bootstrap, CLI, config validace
- [x] Instalátor DC1 (Samba AD primary)
- [x] Instalátor DC2 (Samba AD join)
- [x] Instalátory Docker, Pi-hole, monitoring, Kea, FW, PostgreSQL
- [x] AD klient (samba-tool + LDAP3)
- [x] CSV importéři (users, computers, groups) s upsert do AD i DB
- [x] DB modely (SQLAlchemy) — Computer má 59 sloupců, User 27
- [x] Engine pro dědičnost politik (computer_group → computer → user_group → user)
- [x] Kostra FastAPI webu (login + dashboard)
- [x] **Filtrovatelná tabulka inventáře** s HTMX (10+ operátorů, řazení, volba sloupců)
- [x] **Detail počítače** s editačními poli
- [x] Ukázková data v `examples/`

### Fáze 2 - Web UI (zbývá)
- [ ] CRUD: uživatelé, skupiny (analogicky k computers)
- [ ] Upload a editace plánků budov
- [ ] Drag & drop ikon zařízení na plánek
- [ ] Live HW info z Prometheus (CPU, RAM, disk)
- [ ] Proklik do Grafany pro detailní graf
- [ ] Editor politik (formuláře per kind)

### Fáze 3 - Integrace
- [ ] DHCP rezervace přes Kea Control Agent API
- [ ] Pi-hole API klient (přidání/odebrání klientů, blocklistů)
- [ ] Prometheus query klient (HW info, last_seen)
- [ ] Ansible runner s real-time logy v UI
- [ ] Replikace Pi-hole přes gravity-sync

### Fáze 4 - Polish
- [ ] PostgreSQL hot standby kompletně automaticky
- [ ] Automatický failover (Patroni nebo manuálně přes UI)
- [ ] Audit log v UI
- [ ] Backup/restore tlačítko (DB dump + samba backup + pihole)
- [ ] SNMP klient pro switche a routerboard

## Bezpečnostní poznámky

- `/etc/domain-manager/config.yaml` má chmod 600 a obsahuje hesla.
  Pro produkční nasazení doporučuju načítat hesla z env nebo HashiCorp
  Vaultu (TODO).
- Web UI defaultně poslouchá na 0.0.0.0:8000 - omezte přes firewall
  jen na admin síť.
- Login do webu používá AD bind. Doporučuju vytvořit skupinu `DM-Admins`
  a kontrolovat členství (TODO v `routes/auth.py`).
- Hesla v CSV při importu jsou nouzové řešení. Lepší: prázdné heslo →
  importér vygeneruje silné a vypíše do souboru, který předáte uživatelům.

## Co tu schválně NENÍ

- **Žádný Bash kromě `bootstrap.sh`.** Vše ostatní řídí Python.
- **Žádný JavaScript framework.** Web je server-rendered Jinja + HTMX
  pro interaktivitu. Méně závislostí, méně problémů.
- **Žádná abstraktní pluginová architektura.** Konkrétní instalátory,
  konkrétní modely. Snazší na pochopení.

## Vývojové prostředí

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```
