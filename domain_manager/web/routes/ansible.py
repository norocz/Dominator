"""Administrace Ansible — seznam playbooků, editor, spuštění, real-time výstup.

Architektura:
  GET  /ansible                        přehled: playbook list + historie jobů
  GET  /ansible/playbooks/new          editor — nový playbook
  GET  /ansible/playbooks/{name}/edit  editor — existující playbook
  POST /ansible/playbooks/save         uložit playbook na disk
  POST /ansible/playbooks/{name}/delete smazat playbook
  POST /ansible/run                    spustí job (redirect na /ansible/jobs/{id})
  GET  /ansible/jobs/{id}              detail jobu (full stránka)
  GET  /ansible/jobs/{id}/output       HTMX fragment - výstup + status (polling)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...ansible.runner import AnsibleRunner
from ...db.models import AnsibleAction, Computer, ComputerGroupMembership, Group, User, UserGroupMembership, get_session
from .._audit import log_action

# ── Přednastavené akce ────────────────────────────────────────────────────────
_BUILTIN_ACTIONS: list[dict] = [
    # ── Windows ──────────────────────────────────────────────────────────────
    {
        "name": "Win: Mapovat síťový disk",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Připojí síťový SMB disk na Windows klienty přes win_mapped_drive.",
        "playbook": """\
---
# Nastavte: drive_letter (výchozí Z), share_path (UNC cesta ke sdílené složce)
- name: Mapovat síťový disk
  hosts: all
  gather_facts: no
  vars:
    drive_letter: Z
    share_path: '\\\\dc1\\Dokumenty'
  tasks:
    - name: Mapovat disk {{ drive_letter }} na {{ share_path }}
      community.windows.win_mapped_drive:
        letter: "{{ drive_letter }}"
        path: "{{ share_path }}"
        state: present
""",
    },
    {
        "name": "Win: Odpojit síťový disk",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Odpojí síťový disk (podle písmene disku) ze Windows klientů.",
        "playbook": """\
---
# Nastavte: drive_letter (písmeno disku k odpojení)
- name: Odpojit síťový disk
  hosts: all
  gather_facts: no
  vars:
    drive_letter: Z
  tasks:
    - name: Odpojit disk {{ drive_letter }}
      community.windows.win_mapped_drive:
        letter: "{{ drive_letter }}"
        state: absent
""",
    },
    {
        "name": "Win: Nainstalovat MSI / EXE",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Nainstaluje aplikaci ze sdílené cesty (MSI nebo EXE) přes win_package.",
        "playbook": """\
---
# Nastavte: app_name, installer_path (UNC nebo lokální), installer_args
- name: Nainstalovat aplikaci
  hosts: all
  gather_facts: no
  vars:
    app_name: MojeAplikace
    installer_path: '\\\\dc1\\Software\\aplikace.msi'
    installer_args: '/quiet /norestart'
  tasks:
    - name: Nainstalovat {{ app_name }}
      ansible.windows.win_package:
        path: "{{ installer_path }}"
        arguments: "{{ installer_args }}"
        state: present
""",
    },
    {
        "name": "Win: Instalovat přes Chocolatey",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Nainstaluje balíčky přes Chocolatey package manager (automaticky instaluje choco).",
        "playbook": """\
---
# Nastavte: packages (seznam Chocolatey balíčků)
- name: Instalovat přes Chocolatey
  hosts: all
  gather_facts: no
  vars:
    packages:
      - 7zip
      - vlc
      - googlechrome
  tasks:
    - name: Zajistit Chocolatey
      ansible.windows.win_shell: |
        if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
          Set-ExecutionPolicy Bypass -Scope Process -Force
          [System.Net.ServicePointManager]::SecurityProtocol = 3072
          iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))
        }
      changed_when: false
    - name: Instalovat balíčky
      chocolatey.chocolatey.win_chocolatey:
        name: "{{ packages }}"
        state: present
""",
    },
    {
        "name": "Win: Aktualizace Windows",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Nainstaluje bezpečnostní a kritické aktualizace Windows přes win_updates.",
        "playbook": """\
---
# auto_reboot: true = restartovat automaticky po aktualizacích
- name: Aktualizace Windows
  hosts: all
  gather_facts: no
  vars:
    auto_reboot: false
  tasks:
    - name: Nainstalovat Windows Updates
      ansible.windows.win_updates:
        category_names:
          - SecurityUpdates
          - CriticalUpdates
          - UpdateRollups
        state: installed
        reboot: "{{ auto_reboot }}"
      register: update_result
    - name: Výsledek aktualizací
      ansible.builtin.debug:
        msg: "Nainstalováno {{ update_result.installed_update_count }} aktualizací"
""",
    },
    {
        "name": "Win: Spustit .bat / .cmd skript",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Spustí BAT nebo CMD skript na Windows stanicích přes cmd.exe.",
        "playbook": """\
---
# Nastavte: script_path (lokální cesta nebo UNC ke skriptu na stanici)
- name: Spustit batch skript
  hosts: all
  gather_facts: no
  vars:
    script_path: 'C:\\Scripts\\update.bat'
  tasks:
    - name: Spustit {{ script_path }}
      ansible.windows.win_shell: '"{{ script_path }}"'
      args:
        executable: cmd
      register: result
    - name: Výstup skriptu
      ansible.builtin.debug:
        var: result.stdout_lines
""",
    },
    {
        "name": "Win: Spustit PowerShell příkaz",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Spustí libovolný PowerShell příkaz nebo blok kódu na Windows stanicích.",
        "playbook": """\
---
# Nastavte: ps_command (PowerShell kód k spuštění)
- name: Spustit PowerShell příkaz
  hosts: all
  gather_facts: no
  vars:
    ps_command: |
      Get-ComputerInfo | Select-Object WindowsProductName, TotalPhysicalMemory
      Get-Process | Sort-Object CPU -Descending | Select-Object -First 10
  tasks:
    - name: Spustit PS příkaz
      ansible.windows.win_shell: "{{ ps_command }}"
      register: result
    - ansible.builtin.debug:
        var: result.stdout_lines
""",
    },
    {
        "name": "Win: Nastavit hodnotu v registru",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Zapíše nebo aktualizuje hodnotu ve Windows registru přes win_regedit.",
        "playbook": """\
---
# reg_type: string | dword | binary | expandstring | multistring | qword
- name: Nastavit hodnotu v registru
  hosts: all
  gather_facts: no
  vars:
    reg_path: 'HKLM:\\SOFTWARE\\Policies\\Example'
    reg_name: EnableFeature
    reg_data: 1
    reg_type: dword
  tasks:
    - name: Zapsat {{ reg_name }} = {{ reg_data }}
      ansible.windows.win_regedit:
        path: "{{ reg_path }}"
        name: "{{ reg_name }}"
        data: "{{ reg_data }}"
        type: "{{ reg_type }}"
        state: present
""",
    },
    {
        "name": "Win: Smazat klíč / hodnotu registru",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Odstraní hodnotu nebo celý klíč z Windows registru přes win_regedit.",
        "playbook": """\
---
- name: Smazat hodnotu z registru
  hosts: all
  gather_facts: no
  vars:
    reg_path: 'HKLM:\\SOFTWARE\\Policies\\Example'
    reg_name: EnableFeature
  tasks:
    - name: Smazat {{ reg_name }}
      ansible.windows.win_regedit:
        path: "{{ reg_path }}"
        name: "{{ reg_name }}"
        state: absent
""",
    },
    {
        "name": "Win: Firewall — povolit port",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Přidá pravidlo Windows Firewall pro povolení příchozího TCP/UDP portu.",
        "playbook": """\
---
# fw_protocol: tcp | udp
- name: Firewall — povolit příchozí port
  hosts: all
  gather_facts: no
  vars:
    fw_port: '8080'
    fw_protocol: tcp
  tasks:
    - name: Povolit port {{ fw_port }}/{{ fw_protocol }}
      community.windows.win_firewall_rule:
        name: 'DM Allow {{ fw_protocol }} {{ fw_port }}'
        localport: "{{ fw_port }}"
        protocol: "{{ fw_protocol }}"
        direction: in
        action: allow
        state: present
        enabled: yes
        profiles: domain,private
""",
    },
    {
        "name": "Win: Firewall — blokovat port",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Přidá pravidlo Windows Firewall pro blokování odchozího TCP/UDP portu.",
        "playbook": """\
---
- name: Firewall — blokovat odchozí port
  hosts: all
  gather_facts: no
  vars:
    fw_port: '445'
    fw_protocol: tcp
  tasks:
    - name: Blokovat odchozí {{ fw_port }}/{{ fw_protocol }}
      community.windows.win_firewall_rule:
        name: 'DM Block {{ fw_protocol }} {{ fw_port }}'
        remoteport: "{{ fw_port }}"
        protocol: "{{ fw_protocol }}"
        direction: out
        action: block
        state: present
        enabled: yes
""",
    },
    {
        "name": "Win: Firewall — pravidlo pro aplikaci",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Přidá nebo odebrá firewall pravidlo podle cesty k EXE souboru.",
        "playbook": """\
---
# fw_action: allow | block  |  fw_direction: in | out
- name: Firewall — pravidlo pro aplikaci
  hosts: all
  gather_facts: no
  vars:
    fw_rule_name: 'DM App Rule'
    fw_program: 'C:\\Program Files\\MojeAplikace\\app.exe'
    fw_action: allow
    fw_direction: in
  tasks:
    - name: Firewall pravidlo pro {{ fw_program }}
      community.windows.win_firewall_rule:
        name: "{{ fw_rule_name }}"
        program: "{{ fw_program }}"
        action: "{{ fw_action }}"
        direction: "{{ fw_direction }}"
        state: present
        enabled: yes
        profiles: domain
""",
    },
    {
        "name": "Win: Spravovat Windows službu",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Spustí, zastaví nebo restartuje Windows službu; nastaví start_mode.",
        "playbook": """\
---
# service_state: started | stopped | restarted | absent
# start_mode: auto | manual | disabled
- name: Spravovat Windows službu
  hosts: all
  gather_facts: no
  vars:
    service_name: Spooler
    service_state: restarted
    start_mode: auto
  tasks:
    - name: "{{ service_state | capitalize }}: {{ service_name }}"
      ansible.windows.win_service:
        name: "{{ service_name }}"
        state: "{{ service_state }}"
        start_mode: "{{ start_mode }}"
""",
    },
    {
        "name": "Win: Kopírovat soubor na klienta",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Zkopíruje soubor z DC (Ansible hostu) na Windows klienty přes win_copy.",
        "playbook": """\
---
# src: cesta na Ansible hostu (DC); dest: cíl na Windows stanici
- name: Kopírovat soubor na Windows klienta
  hosts: all
  gather_facts: no
  vars:
    src: /opt/domain-manager/files/config.ini
    dest: 'C:\\ProgramData\\MojeAplikace\\config.ini'
  tasks:
    - name: Kopírovat {{ src }} → {{ dest }}
      ansible.windows.win_copy:
        src: "{{ src }}"
        dest: "{{ dest }}"
        backup: yes
""",
    },
    {
        "name": "Win: Vytvořit lokálního uživatele",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Vytvoří nebo upraví lokálního uživatele Windows přes win_user.",
        "playbook": """\
---
# user_state: present | absent
- name: Lokální uživatel Windows
  hosts: all
  gather_facts: no
  vars:
    local_username: servisni
    local_password: 'ZmenteHeslo123!'
    local_fullname: 'Servisní účet'
    local_group: Administrators
    user_state: present
  tasks:
    - name: Uživatel {{ local_username }} ({{ user_state }})
      ansible.windows.win_user:
        name: "{{ local_username }}"
        password: "{{ local_password }}"
        fullname: "{{ local_fullname }}"
        groups:
          - "{{ local_group }}"
        state: "{{ user_state }}"
        password_never_expires: yes
        account_disabled: no
      no_log: true
""",
    },
    {
        "name": "Win: Naplánovaný úkol při startu",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Vytvoří naplánovaný úkol spouštěný při startu Windows jako SYSTEM.",
        "playbook": """\
---
- name: Naplánovaný úkol při startu
  hosts: all
  gather_facts: no
  vars:
    task_name: 'DM Startup Task'
    task_executable: PowerShell.exe
    task_arguments: '-NonInteractive -File C:\\Scripts\\startup.ps1'
  tasks:
    - name: Naplánovaný úkol {{ task_name }}
      community.windows.win_scheduled_task:
        name: "{{ task_name }}"
        description: Spravováno přes Dominator
        executable: "{{ task_executable }}"
        arguments: "{{ task_arguments }}"
        triggers:
          - type: boot
        run_level: highest
        logon_type: service_account
        username: SYSTEM
        state: present
        enabled: yes
""",
    },
    {
        "name": "Win: Nastavit proměnnou prostředí",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Nastaví systémovou nebo uživatelskou proměnnou prostředí Windows.",
        "playbook": """\
---
# env_level: machine (systémová) | user (aktuální uživatel)
- name: Nastavit proměnnou prostředí
  hosts: all
  gather_facts: no
  vars:
    env_name: APP_CONFIG_PATH
    env_value: 'C:\\ProgramData\\MojeAplikace'
    env_level: machine
  tasks:
    - name: Nastavit {{ env_name }}
      ansible.windows.win_environment:
        name: "{{ env_name }}"
        value: "{{ env_value }}"
        state: present
        level: "{{ env_level }}"
""",
    },
    {
        "name": "Win: Sdílená složka SMB",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Vytvoří nebo odstraní SMB share (sdílenou složku) na Windows klientech.",
        "playbook": """\
---
# share_state: present | absent
- name: Spravovat SMB share
  hosts: all
  gather_facts: no
  vars:
    share_name: Dokumenty
    share_path: 'C:\\Sdileni\\Dokumenty'
    share_description: 'Sdílené dokumenty'
    share_state: present
  tasks:
    - name: SMB share {{ share_name }} ({{ share_state }})
      ansible.windows.win_share:
        name: "{{ share_name }}"
        path: "{{ share_path }}"
        description: "{{ share_description }}"
        state: "{{ share_state }}"
        full: Domain Admins
        read: 'Domain Users'
""",
    },
    {
        "name": "Win: Restartovat Windows",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Restartuje Windows stanice a čeká na opětovné připojení přes WinRM.",
        "playbook": """\
---
# pre_reboot_delay: sekund upozornění před restartem
- name: Restartovat Windows
  hosts: all
  gather_facts: no
  vars:
    pre_reboot_delay: 30
    post_reboot_delay: 60
  tasks:
    - name: Restart stanice
      ansible.windows.win_reboot:
        msg: Restart naplánován přes Dominator
        pre_reboot_delay: "{{ pre_reboot_delay }}"
        post_reboot_delay: "{{ post_reboot_delay }}"
        reboot_timeout: 300
""",
    },
    {
        "name": "Win: Info o systému",
        "category": "Windows",
        "targets": "computer,computer_group",
        "description": "Shromáždí a zobrazí základní HW/SW informace o Windows stanicích.",
        "playbook": """\
---
- name: Info o Windows systému
  hosts: all
  gather_facts: yes
  tasks:
    - name: Zobrazit informace o stanici
      ansible.builtin.debug:
        msg:
          - "Stanice: {{ ansible_hostname }}"
          - "OS: {{ ansible_os_name | default('N/A') }}"
          - "RAM: {{ (ansible_memtotal_mb | default(0) / 1024) | round(1) }} GB"
          - "CPU jader: {{ ansible_processor_count | default('N/A') }}"
          - "IP: {{ ansible_ip_addresses | default([]) | join(', ') }}"
""",
    },

    # ── Linux ────────────────────────────────────────────────────────────────
    {
        "name": "Linux: Nainstalovat apt balíček",
        "category": "Linux",
        "targets": "computer,computer_group",
        "description": "Nainstaluje nebo odinstaluje apt balíčky na Debian/Ubuntu klientech.",
        "playbook": """\
---
# pkg_state: present | absent | latest
- name: Spravovat apt balíčky
  hosts: all
  gather_facts: no
  become: yes
  vars:
    packages:
      - htop
      - curl
    pkg_state: present
  tasks:
    - name: apt {{ pkg_state }}: {{ packages | join(', ') }}
      ansible.builtin.apt:
        name: "{{ packages }}"
        state: "{{ pkg_state }}"
        update_cache: yes
""",
    },
    {
        "name": "Linux: Aktualizace systému",
        "category": "Linux",
        "targets": "computer,computer_group",
        "description": "Spustí apt upgrade + autoremove na Linux klientech.",
        "playbook": """\
---
- name: Aktualizace systémových balíčků
  hosts: all
  gather_facts: no
  become: yes
  tasks:
    - name: apt update + safe upgrade
      ansible.builtin.apt:
        upgrade: safe
        update_cache: yes
    - name: Autoremove nepoužívaných balíčků
      ansible.builtin.apt:
        autoremove: yes
""",
    },
    {
        "name": "Linux: Spustit shell příkaz",
        "category": "Linux",
        "targets": "computer,computer_group",
        "description": "Spustí libovolný shell příkaz na Linux klientech (volitelně jako root).",
        "playbook": """\
---
# use_become: true = spustit jako root
- name: Spustit shell příkaz
  hosts: all
  gather_facts: no
  vars:
    shell_command: 'systemctl status sshd | head -20'
    use_become: false
  tasks:
    - name: Spustit příkaz
      ansible.builtin.shell: "{{ shell_command }}"
      become: "{{ use_become }}"
      register: result
    - ansible.builtin.debug:
        var: result.stdout_lines
""",
    },
    {
        "name": "Linux: Spravovat systemd službu",
        "category": "Linux",
        "targets": "computer,computer_group",
        "description": "Spustí, zastaví, restartuje nebo reloaduje systemd službu na Linux klientech.",
        "playbook": """\
---
# service_state: started | stopped | restarted | reloaded
- name: Spravovat systemd službu
  hosts: all
  gather_facts: no
  become: yes
  vars:
    service_name: nginx
    service_state: restarted
    service_enabled: true
  tasks:
    - name: "{{ service_state | capitalize }}: {{ service_name }}"
      ansible.builtin.systemd:
        name: "{{ service_name }}"
        state: "{{ service_state }}"
        enabled: "{{ service_enabled }}"
        daemon_reload: yes
""",
    },
    {
        "name": "Linux: Kopírovat soubor",
        "category": "Linux",
        "targets": "computer,computer_group",
        "description": "Zkopíruje soubor z DC (Ansible hostu) na Linux klienty.",
        "playbook": """\
---
- name: Kopírovat soubor na Linux klienty
  hosts: all
  gather_facts: no
  become: yes
  vars:
    src: /opt/domain-manager/files/config.conf
    dest: /etc/app/config.conf
    file_owner: root
    file_mode: '0644'
  tasks:
    - name: Kopírovat {{ src }} → {{ dest }}
      ansible.builtin.copy:
        src: "{{ src }}"
        dest: "{{ dest }}"
        owner: "{{ file_owner }}"
        mode: "{{ file_mode }}"
        backup: yes
""",
    },
    {
        "name": "Linux: Restartovat systém",
        "category": "Linux",
        "targets": "computer,computer_group",
        "description": "Restartuje Linux klienty a čeká na opětovné připojení přes SSH.",
        "playbook": """\
---
- name: Restartovat Linux systém
  hosts: all
  gather_facts: no
  become: yes
  vars:
    pre_reboot_delay: 30
  tasks:
    - name: Restart
      ansible.builtin.reboot:
        msg: Restart naplánován přes Dominator
        pre_reboot_delay: "{{ pre_reboot_delay }}"
        reboot_timeout: 300
""",
    },

    # ── Active Directory ─────────────────────────────────────────────────────
    {
        "name": "AD: Resetovat heslo uživatele",
        "category": "Active Directory",
        "targets": "user,user_group",
        "description": "Resetuje heslo AD účtu přes samba-tool (spouští se na DC).",
        "playbook": """\
---
# username a new_password jsou předány jako extra_vars z kontextu
- name: Resetovat heslo AD uživatele
  hosts: localhost
  gather_facts: no
  vars:
    username: novak
    new_password: 'NoveHeslo123!'
  tasks:
    - name: Resetovat heslo pro {{ username }}
      ansible.builtin.shell: |
        samba-tool user setpassword "{{ username }}" --newpassword="{{ new_password }}"
      no_log: true
    - ansible.builtin.debug:
        msg: "Heslo pro {{ username }} bylo úspěšně resetováno"
""",
    },
    {
        "name": "AD: Deaktivovat AD účet",
        "category": "Active Directory",
        "targets": "user,user_group",
        "description": "Deaktivuje AD účet — uživatel se nebude moci přihlásit (spouští se na DC).",
        "playbook": """\
---
# username je předán jako extra_var z kontextu
- name: Deaktivovat AD účet
  hosts: localhost
  gather_facts: no
  vars:
    username: novak
  tasks:
    - name: Deaktivovat {{ username }}
      ansible.builtin.shell: |
        samba-tool user disable "{{ username }}"
    - ansible.builtin.debug:
        msg: "Účet {{ username }} byl deaktivován"
""",
    },
    {
        "name": "AD: Aktivovat AD účet",
        "category": "Active Directory",
        "targets": "user,user_group",
        "description": "Aktivuje (povolí) AD účet — uživatel se bude moci přihlásit (spouští se na DC).",
        "playbook": """\
---
# username je předán jako extra_var z kontextu
- name: Aktivovat AD účet
  hosts: localhost
  gather_facts: no
  vars:
    username: novak
  tasks:
    - name: Aktivovat {{ username }}
      ansible.builtin.shell: |
        samba-tool user enable "{{ username }}"
    - ansible.builtin.debug:
        msg: "Účet {{ username }} byl aktivován"
""",
    },
    {
        "name": "AD: Přidat do skupiny",
        "category": "Active Directory",
        "targets": "user,user_group",
        "description": "Přidá uživatele do AD skupiny přes samba-tool (spouští se na DC).",
        "playbook": """\
---
# username předán z kontextu; ad_group = cílová AD skupina
- name: Přidat uživatele do AD skupiny
  hosts: localhost
  gather_facts: no
  vars:
    username: novak
    ad_group: Ucitele
  tasks:
    - name: Přidat {{ username }} do {{ ad_group }}
      ansible.builtin.shell: |
        samba-tool group addmembers "{{ ad_group }}" "{{ username }}"
    - ansible.builtin.debug:
        msg: "{{ username }} přidán do skupiny {{ ad_group }}"
""",
    },
    {
        "name": "AD: Odebrat ze skupiny",
        "category": "Active Directory",
        "targets": "user,user_group",
        "description": "Odebere uživatele ze AD skupiny přes samba-tool (spouští se na DC).",
        "playbook": """\
---
# username předán z kontextu; ad_group = AD skupina k odebrání
- name: Odebrat uživatele ze AD skupiny
  hosts: localhost
  gather_facts: no
  vars:
    username: novak
    ad_group: Ucitele
  tasks:
    - name: Odebrat {{ username }} ze {{ ad_group }}
      ansible.builtin.shell: |
        samba-tool group removemembers "{{ ad_group }}" "{{ username }}"
    - ansible.builtin.debug:
        msg: "{{ username }} odebrán ze skupiny {{ ad_group }}"
""",
    },
    {
        "name": "AD: Zobrazit info o uživateli",
        "category": "Active Directory",
        "targets": "user",
        "description": "Zobrazí detailní informace o AD účtu přes samba-tool show.",
        "playbook": """\
---
# username je předán jako extra_var z kontextu
- name: Zobrazit info o AD uživateli
  hosts: localhost
  gather_facts: no
  vars:
    username: novak
  tasks:
    - name: Info o {{ username }}
      ansible.builtin.shell: |
        samba-tool user show "{{ username }}"
      register: result
    - ansible.builtin.debug:
        var: result.stdout_lines
""",
    },
    {
        "name": "AD: Hromadný reset hesel skupiny",
        "category": "Active Directory",
        "targets": "user_group",
        "description": "Resetuje hesla všem uživatelům v AD skupině (spouští se na DC).",
        "playbook": """\
---
# group_name a usernames jsou předány z kontextu skupiny uživatelů
- name: Hromadný reset hesel skupiny
  hosts: localhost
  gather_facts: no
  vars:
    group_name: Ucitele
    new_password: 'SkolicniHeslo2024!'
    usernames: []
  tasks:
    - name: Resetovat hesla členů skupiny {{ group_name }}
      ansible.builtin.shell: |
        samba-tool user setpassword "{{ item }}" --newpassword="{{ new_password }}"
      loop: "{{ usernames }}"
      no_log: true
      when: item != ''
    - ansible.builtin.debug:
        msg: "Resetováno hesel: {{ usernames | length }}"
""",
    },
]

router = APIRouter(prefix="/ansible")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

_STARTER_TEMPLATE = """\
---
# Popis: Co tento playbook dělá
- name: Název playbooku
  hosts: all
  become: yes

  vars:
    # Definujte proměnné zde
    example_var: hodnota

  tasks:
    - name: Hello world
      ansible.builtin.debug:
        msg: "Spouštím na {{ inventory_hostname }}"
"""

_ALLOWED_SUFFIXES = {".yml", ".yaml"}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


def _runner(request: Request) -> AnsibleRunner:
    return AnsibleRunner(request.app.state.config)


@router.get("", response_class=HTMLResponse)
async def ansible_page(request: Request, user: str = Depends(_require_user)):
    runner = _runner(request)
    with get_session() as session:
        actions = session.query(AnsibleAction).order_by(AnsibleAction.category, AnsibleAction.name).all()
        actions_data = [a.as_dict() for a in actions]
    return templates.TemplateResponse(request, "ansible.html", {
        "user": user,
        "playbooks": runner.list_playbooks(),
        "groups": runner.list_groups(),
        "jobs": [j.as_dict() for j in runner.list_jobs()],
        "actions": actions_data,
    })


@router.post("/run")
async def ansible_run(
    request: Request,
    user: str = Depends(_require_user),
    playbook: str = Form(...),
    limit: str = Form("all"),
):
    runner = _runner(request)
    job_id = runner.start(playbook, limit=limit, demo=_DEMO_MODE)
    return RedirectResponse(f"/ansible/jobs/{job_id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def ansible_job(
    job_id: str, request: Request, user: str = Depends(_require_user)
):
    runner = _runner(request)
    job = runner.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job nenalezen")
    return templates.TemplateResponse(request, "ansible_job.html", {
        "user": user,
        "job": job.as_dict(),
        "output": job.output_lines,
    })


@router.get("/actions/panel", response_class=HTMLResponse)
async def action_panel(
    request: Request,
    user: str = Depends(_require_user),
    target: str = "computer",
    group_id: int = 0,
    hostname: str = "",
    username: str = "",
    prefix: str = "p",
):
    """HTMX fragment — seznam akcí filtrovatelných podle target type."""
    with get_session() as session:
        all_actions = (
            session.query(AnsibleAction)
            .order_by(AnsibleAction.category, AnsibleAction.name)
            .all()
        )
        filtered = [a for a in all_actions if target in a.targets_list()]

        context_label = ""
        if group_id:
            g = session.get(Group, group_id)
            context_label = g.name if g else f"#{group_id}"
        elif hostname:
            context_label = hostname
        elif username:
            context_label = username

        actions_data = [a.as_dict() for a in filtered]

    target_label = {
        "computer": "počítač",
        "computer_group": "skupinu počítačů",
        "user": "uživatele",
        "user_group": "skupinu uživatelů",
    }.get(target, target)

    return templates.TemplateResponse(request, "ansible_action_panel.html", {
        "actions": actions_data,
        "target": target,
        "group_id": group_id,
        "hostname": hostname,
        "username": username,
        "context_label": context_label,
        "target_label": target_label,
        "prefix": prefix,
    })


@router.post("/actions/{action_id}/run", response_class=HTMLResponse)
async def action_run(
    action_id: int,
    request: Request,
    user: str = Depends(_require_user),
    target_type: str = Form(...),
    group_id: str = Form("0"),
    hostname: str = Form(""),
    username: str = Form(""),
    extra_vars: str = Form(""),
):
    """Spustí DB akci s kontextem cíle (počítač / skupina / uživatel)."""
    with get_session() as session:
        action = session.get(AnsibleAction, action_id)
        if not action:
            raise HTTPException(404, "Akce nenalezena")
        action_name = action.name
        playbook_content = action.playbook

    gid = int(group_id) if group_id and group_id.isdigit() else 0
    limit = "all"
    ev: dict | None = None

    if target_type == "computer":
        limit = hostname.strip() or "all"
        ev = {"hostname": limit}

    elif target_type == "computer_group" and gid:
        with get_session() as session:
            mems = (
                session.query(ComputerGroupMembership)
                .filter(ComputerGroupMembership.group_id == gid)
                .all()
            )
            comp_ids = [m.computer_id for m in mems]
            comps = (
                session.query(Computer).filter(Computer.id.in_(comp_ids)).all()
                if comp_ids else []
            )
            hostnames = [c.hostname for c in comps if c.hostname]
        limit = ",".join(hostnames) if hostnames else "all"

    elif target_type == "user":
        limit = "localhost"
        ev = {"username": username.strip()}

    elif target_type == "user_group" and gid:
        with get_session() as session:
            g = session.get(Group, gid)
            group_name = g.name if g else ""
            mems = (
                session.query(UserGroupMembership)
                .filter(UserGroupMembership.group_id == gid)
                .all()
            )
            user_ids = [m.user_id for m in mems]
            users = (
                session.query(User).filter(User.id.in_(user_ids)).all()
                if user_ids else []
            )
            unames = [u.username for u in users]
        limit = "localhost"
        ev = {"group_name": group_name, "usernames": unames, "username": unames[0] if unames else ""}

    # Extra vars override (JSON nebo key=val)
    if extra_vars.strip():
        import json as _json
        try:
            ev_override = _json.loads(extra_vars)
            ev = {**(ev or {}), **ev_override}
        except Exception:
            parsed: dict = {}
            for pair in extra_vars.split():
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    parsed[k.strip()] = v.strip()
            if parsed:
                ev = {**(ev or {}), **parsed}

    runner = _runner(request)
    job_id = runner.start_content(
        action_name, playbook_content, limit=limit, extra_vars=ev, demo=_DEMO_MODE
    )
    log_action(user, "run_ansible_action", "ansible_action", action_id, {
        "action": action_name, "target_type": target_type, "limit": limit,
    })

    return HTMLResponse(
        f'<span style="color:var(--success);">&#10003; Job spuštěn</span> — '
        f'<a href="/ansible/jobs/{job_id}" style="color:var(--accent);">#{job_id}</a>'
        f' <span style="color:var(--text-dim);font-size:11px;">({action_name})'
        f' &nbsp;<a href="/ansible">Ansible →</a></span>'
    )


@router.get("/actions/new", response_class=HTMLResponse)
def action_new(request: Request, user: str = Depends(_require_user)):
    return templates.TemplateResponse(request, "ansible_editor.html", {
        "user": user,
        "filename": "", "content": _STARTER_TEMPLATE,
        "is_new": True, "saved": False,
        "is_action": True,
        "action": {
            "id": None, "name": "", "description": "",
            "category": "Windows", "targets": "computer,computer_group",
            "targets_list": ["computer", "computer_group"],
        },
    })


@router.get("/actions/{action_id}/edit", response_class=HTMLResponse)
def action_edit(
    action_id: int, request: Request,
    user: str = Depends(_require_user),
    saved: int = 0,
):
    with get_session() as session:
        action = session.get(AnsibleAction, action_id)
        if not action:
            raise HTTPException(404, "Akce nenalezena")
        ad = action.as_dict()
    return templates.TemplateResponse(request, "ansible_editor.html", {
        "user": user,
        "filename": "", "content": ad["playbook"],
        "is_new": False, "saved": bool(saved),
        "is_action": True,
        "action": ad,
    })


@router.post("/actions/save")
async def action_save(
    request: Request,
    user: str = Depends(_require_user),
    action_id: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form("Obecné"),
    targets: str = Form("computer,computer_group"),
    content: str = Form(...),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Název akce nesmí být prázdný")
    with get_session() as session:
        if action_id.strip():
            action = session.get(AnsibleAction, int(action_id))
            if not action:
                raise HTTPException(404)
            action.name = name
            action.description = description.strip()
            action.category = category.strip() or "Obecné"
            action.targets = targets.strip() or "computer"
            action.playbook = content
            session.commit()
            log_action(user, "update_ansible_action", "ansible_action", action.id, {"name": name})
            aid = action.id
        else:
            existing = session.query(AnsibleAction).filter(AnsibleAction.name == name).first()
            if existing:
                return RedirectResponse(f"/ansible/actions/{existing.id}/edit?saved=1", status_code=303)
            action = AnsibleAction(
                name=name, description=description.strip(),
                category=category.strip() or "Obecné",
                targets=targets.strip() or "computer",
                playbook=content, created_by=user,
            )
            session.add(action)
            session.commit()
            session.refresh(action)
            log_action(user, "create_ansible_action", "ansible_action", action.id, {"name": name})
            aid = action.id
    return RedirectResponse(f"/ansible/actions/{aid}/edit?saved=1", status_code=303)


@router.post("/actions/{action_id}/delete")
def action_delete(
    action_id: int, request: Request, user: str = Depends(_require_user),
):
    with get_session() as session:
        action = session.get(AnsibleAction, action_id)
        if not action:
            raise HTTPException(404)
        name = action.name
        session.delete(action)
        session.commit()
    log_action(user, "delete_ansible_action", "ansible_action", action_id, {"name": name})
    return RedirectResponse("/ansible", status_code=303)


@router.post("/actions/seed", response_class=HTMLResponse)
async def action_seed(request: Request, user: str = Depends(_require_user)):
    """Vloží výchozí akce do DB (přeskočí existující)."""
    created = skipped = 0
    with get_session() as session:
        for data in _BUILTIN_ACTIONS:
            existing = session.query(AnsibleAction).filter(AnsibleAction.name == data["name"]).first()
            if existing:
                skipped += 1
                continue
            session.add(AnsibleAction(
                name=data["name"],
                description=data.get("description", ""),
                category=data.get("category", "Obecné"),
                targets=data.get("targets", "computer,computer_group"),
                playbook=data["playbook"],
                is_builtin=True,
                created_by=user,
            ))
            created += 1
        session.commit()
    log_action(user, "seed_ansible_actions", "ansible_action", None, {"created": created, "skipped": skipped})
    suffix = f", {skipped} přeskočeno" if skipped else ""
    return HTMLResponse(
        f'<span style="color:var(--success);">&#10003; Načteno {created} akcí{suffix}.</span>'
        f'<script>setTimeout(()=>location.reload(),600)</script>'
    )


@router.get("/playbooks/new", response_class=HTMLResponse)
def playbook_new(request: Request, user: str = Depends(_require_user)):
    return templates.TemplateResponse(request, "ansible_editor.html", {
        "user": user,
        "filename": "", "content": _STARTER_TEMPLATE,
        "is_new": True, "saved": False, "is_action": False, "action": {},
    })


@router.get("/playbooks/{name}/edit", response_class=HTMLResponse)
def playbook_edit(
    name: str, request: Request,
    user: str = Depends(_require_user),
    saved: int = 0,
):
    safe = Path(name).name
    if Path(safe).suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(400, "Neplatný typ souboru")
    runner = _runner(request)
    path = runner.playbooks_path / safe
    if not path.exists():
        raise HTTPException(404, f"Playbook {safe!r} nenalezen")
    return templates.TemplateResponse(request, "ansible_editor.html", {
        "user": user,
        "filename": safe, "content": path.read_text(encoding="utf-8"),
        "is_new": False, "saved": bool(saved), "is_action": False, "action": {},
    })


@router.post("/playbooks/save")
async def playbook_save(
    request: Request,
    user: str = Depends(_require_user),
    name: str = Form(...),
    content: str = Form(...),
):
    safe = Path(name.strip()).name
    if not safe:
        raise HTTPException(400, "Název playbooku nesmí být prázdný")
    if Path(safe).suffix not in _ALLOWED_SUFFIXES:
        safe += ".yml"
    runner = _runner(request)
    runner.playbooks_path.mkdir(parents=True, exist_ok=True)
    (runner.playbooks_path / safe).write_text(content, encoding="utf-8")
    log_action(user, "save_playbook", "playbook", None, {"name": safe})
    return RedirectResponse(f"/ansible/playbooks/{safe}/edit?saved=1", status_code=303)


@router.post("/playbooks/{name}/delete")
def playbook_delete(
    name: str, request: Request,
    user: str = Depends(_require_user),
):
    safe = Path(name).name
    runner = _runner(request)
    path = runner.playbooks_path / safe
    if path.exists():
        path.unlink()
    log_action(user, "delete_playbook", "playbook", None, {"name": safe})
    return RedirectResponse("/ansible", status_code=303)


@router.get("/jobs/{job_id}/output", response_class=HTMLResponse)
async def ansible_job_output(
    job_id: str, request: Request, user: str = Depends(_require_user)
):
    """HTMX fragment — výstup + status badge. Polling se zastaví sám když job skončí."""
    runner = _runner(request)
    job = runner.get_job(job_id)
    if not job:
        return HTMLResponse("<div>Job nenalezen</div>")
    return templates.TemplateResponse(request, "ansible_output_fragment.html", {
        "job": job.as_dict(),
        "output": job.output_lines,
    })
