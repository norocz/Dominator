# QUICKSTART — lokální vývoj

Pro iterování nad kódem nepotřebujete reálné Samba DC ani PostgreSQL.
Stačí Python 3.12+ a SQLite (v Pythonu už je).

## 1) Rozbalit a vytvořit venv

```bash
tar xzf domain-manager-v0.1.0.tar.gz
cd domain-manager
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2) Lokální dev konfigurace

Vytvořte `config.dev.yaml` (NEMĚŇTE config.yaml.example — ten je vzor):

```bash
cp config.yaml.example config.dev.yaml
# Hesla už splňují validátor, klidně to zatím nechte.
export DM_CONFIG=$(pwd)/config.dev.yaml
```

## 3) DB v SQLite (místo Postgresu)

Otevřete `domain_manager/db/models.py` a najděte funkci `get_engine` (úplně
dole). Pro lokální vývoj jí dočasně přepište na SQLite:

```python
def get_engine(cfg=None):
    global _engine
    if _engine is None:
        # PRODUKCE: PostgreSQL
        # from ..config import load_config
        # cfg = cfg or load_config()
        # pg = cfg.postgres
        # url = f"postgresql+psycopg://{pg.db_user}:{pg.db_password}@localhost/{pg.db_name}"
        # DEV: SQLite
        import os
        url = os.environ.get("DM_DB_URL", "sqlite:///./domain-manager.db")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine
```

Alternativně si rovnou udělejte v `models.py` switch podle env proměnné
(navrhnu rád v dalším kroku, jen mi to řekněte).

## 4) Inicializace DB a naplnění ukázkovými daty

```bash
python -c "from domain_manager.db.models import create_all; create_all()"
```

Pak naimportujte ukázková data. Ale pozor — importéři volají `ADClient`,
což jde k reálné Sambě. Pro lokál mock:

```bash
python <<'PY'
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from domain_manager.db import models as M
from domain_manager.ad import client

# Mock AD (bez Samby)
client.ADClient.__init__ = lambda self, cfg: None
client.ADClient.create_computer = lambda self, c: True
client.ADClient.create_group = lambda self, name, description='': True
client.ADClient.add_computer_to_group = lambda self, h, g: True
client.ADClient.create_user = lambda self, u: True
client.ADClient.add_user_to_group = lambda self, u, g: True

# Skupiny (importér je očekává jako existující)
session = M.get_session()
for gname in ['Ucetni','Marketing','IT','Admins','Vsichni',
              'ucetni-pc','marketing-pc','it-pc','sklad-pc','servery','vsechna-pc']:
    if not session.query(M.Group).filter_by(name=gname).first():
        kind = 'computer' if 'pc' in gname or gname == 'servery' else 'user'
        session.add(M.Group(name=gname, kind=kind))
session.commit()

# Import počítačů
from pathlib import Path
from domain_manager.ad.importers import ComputerImporter
class FakeCfg: pass
ComputerImporter(FakeCfg()).import_csv(Path('examples/computers.csv'), dry_run=False)
PY
```

## 5) Spuštění webu

```bash
# .venv aktivní, DM_CONFIG nastaveno
uvicorn domain_manager.web.app:app --reload --port 8000
```

Otevřete `http://localhost:8000`.

**Pozor — login:** routě `/login` validuje proti AD. Pro lokální iteraci
buď v `web/routes/auth.py` dočasně zakomentujte LDAP volání a rovnou
nastavte `request.session["user"] = username`, nebo si přihlášení obejděte
v dev konzoli prohlížeče (cookie session).

Nejjednodušší dev hack — v `web/routes/auth.py`:

```python
@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    import os
    if os.environ.get("DM_DEV_LOGIN") == "1":
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    # ... původní AD bind kód
```

A pak `export DM_DEV_LOGIN=1` před `uvicorn`.

## 6) Iterování

- `domain_manager/db/models.py` — když přidáte sloupec, smažte
  `domain-manager.db` a spusťte znovu `create_all()` (alembic migrace
  zatím neimplementuju, dev je přepisovat databázi).
- `domain_manager/web/templates/*.html` — uvicorn s `--reload` zachytí
  i změny v šablonách.
- `domain_manager/web/routes/computers.py` — hlavní soubor pro iteraci
  filtrovací tabulky.
- `domain_manager/db/queries.py` — kde se přidávají nové filtrovatelné
  sloupce do `ComputerQuery.fields`.

## 7) Test, že to běží

```bash
# Otestovat filtry bez webu:
python -c "
from domain_manager.db.queries import ComputerQuery
from domain_manager.db.models import get_session
s = get_session()
q = ComputerQuery(s).filter_from_params({'os_family': 'windows'}).paginate()
print(f'Windows: {q.total}')
for r in q.rows: print(' ', r.hostname, r.os_name)
"
```

## Struktura pro rychlou orientaci

```
domain-manager/
├── bootstrap.sh                # produkční instalace - dev ho nepotřebuje
├── config.yaml.example         # vzor; pro dev kopie do config.dev.yaml
├── pyproject.toml
├── examples/                   # ukázková CSV
│   ├── computers.csv
│   ├── users.csv
│   └── groups.csv
└── domain_manager/
    ├── cli.py                  # `dm` příkaz
    ├── config.py               # validace yaml
    ├── runner.py               # shell wrapper (pro instalátory)
    ├── installers/             # Samba, DHCP, Pi-hole... (produkce)
    ├── ad/
    │   ├── client.py           # samba-tool + ldap3
    │   ├── importers.py        # CSV → AD + DB
    │   └── policies.py         # dědičnost politik
    ├── db/
    │   ├── models.py           # ⭐ 59 sloupců v Computer, 27 v User
    │   └── queries.py          # ⭐ filtrovací engine
    └── web/
        ├── app.py
        ├── routes/
        │   ├── computers.py    # ⭐ filtrovatelná tabulka
        │   ├── auth.py
        │   └── ...
        └── templates/
            ├── base.html
            ├── computers.html         # ⭐ stránka s filtrem
            ├── computers_table.html   # ⭐ tabulkový fragment (HTMX)
            └── computer_detail.html   # ⭐ detail počítače
```

Hvězdičky ⭐ jsou soubory, do kterých se nejvíc sahá při iteraci.
