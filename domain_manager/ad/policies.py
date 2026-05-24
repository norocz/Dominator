"""Vyhodnocování efektivních politik podle hierarchie dědičnosti.

Hierarchie (od nejnižší k nejvyšší prioritě):
    1) computer_group  (počítačová skupina)
    2) computer        (konkrétní počítač)
    3) user_group      (uživatelská skupina, do které patří přihlášený user)
    4) user            (konkrétní uživatel)

Merge logika (pro každý kind politiky):
    - 'firewall'  : pravidla se sčítají (union), specifičtější deny vyhrává
    - 'pihole'    : blocklisty union, allowlisty union (přidání vyhrává nad blokem)
    - 'software'  : seznam install se sčítá, remove vyhrává
    - 'settings'  : per-klíč přepis (vyšší úroveň vyhrává)

Použití:
    effective = PolicyEngine(session).evaluate(computer_id=42, user_id=7)
    # vrací dict {kind: merged_spec}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import (
    Computer, ComputerGroupMembership, Group, Policy, PolicyAssignment,
    User, UserGroupMembership,
)


HIERARCHY_ORDER = ["computer_group", "computer", "user_group", "user"]


@dataclass
class EffectivePolicy:
    kind: str
    merged: dict[str, Any]
    sources: list[tuple[str, int, int]] = field(default_factory=list)
    # sources: list of (target_type, target_id, policy_id) - pro audit/debug


class PolicyEngine:
    def __init__(self, session: Session):
        self.session = session

    def evaluate(self, *, computer_id: int | None = None, user_id: int | None = None) -> dict[str, EffectivePolicy]:
        """Vrátí efektivní politiky pro danou kombinaci PC + uživatel.

        Pokud zadán jen computer_id, vyhodnotí jen úrovně 1-2.
        Pokud zadán jen user_id, jen úrovně 3-4.
        """
        # 1) Posbírat všechny relevantní (target_type, target_id) na všech úrovních
        targets: list[tuple[str, int]] = []  # v pořadí HIERARCHY_ORDER

        if computer_id is not None:
            # computer_group: skupiny, do kterých PC patří
            cg = self.session.query(ComputerGroupMembership.group_id).filter_by(
                computer_id=computer_id
            ).all()
            for (gid,) in cg:
                targets.append(("computer_group", gid))
            # computer
            targets.append(("computer", computer_id))

        if user_id is not None:
            ug = self.session.query(UserGroupMembership.group_id).filter_by(
                user_id=user_id
            ).all()
            for (gid,) in ug:
                targets.append(("user_group", gid))
            targets.append(("user", user_id))

        # 2) Pro každý target najít přiřazené politiky a uspořádat dle priority
        all_assignments = []
        for (ttype, tid) in targets:
            rows = self.session.query(PolicyAssignment, Policy).join(
                Policy, Policy.id == PolicyAssignment.policy_id
            ).filter(
                PolicyAssignment.target_type == ttype,
                PolicyAssignment.target_id == tid,
            ).all()
            for (assign, policy) in rows:
                all_assignments.append((ttype, tid, assign.priority, policy))

        # 3) Třídění: nejdřív podle pozice v hierarchii, pak podle priority (vyšší vyhrává)
        def sort_key(item):
            ttype, tid, prio, policy = item
            return (HIERARCHY_ORDER.index(ttype), prio)

        all_assignments.sort(key=sort_key)

        # 4) Postupný merge per kind
        effective: dict[str, EffectivePolicy] = {}
        for (ttype, tid, prio, policy) in all_assignments:
            if policy.kind not in effective:
                effective[policy.kind] = EffectivePolicy(kind=policy.kind, merged={})
            ep = effective[policy.kind]
            ep.merged = self._merge(policy.kind, ep.merged, policy.spec)
            ep.sources.append((ttype, tid, policy.id))

        return effective

    # --- merge per kind ---------------------------------------------------

    def _merge(self, kind: str, base: dict, new: dict) -> dict:
        if kind == "firewall":
            return self._merge_firewall(base, new)
        if kind == "pihole":
            return self._merge_pihole(base, new)
        if kind == "software":
            return self._merge_software(base, new)
        # 'settings' a default: per-klíč přepis
        return {**base, **new}

    @staticmethod
    def _merge_firewall(base: dict, new: dict) -> dict:
        """firewall spec: {'rules': [{'action': 'accept|drop', 'direction': 'in|out', 'port': N, 'proto': 'tcp|udp'}]}
        Sjednocení pravidel; deny ve vyšší úrovni přebije allow z nižší."""
        rules = list(base.get("rules", []))
        for r in new.get("rules", []):
            # Pokud existuje opačné pravidlo na stejném portu/protokolu, nahradíme ho
            rules = [
                x for x in rules
                if not (x.get("port") == r.get("port")
                        and x.get("proto") == r.get("proto")
                        and x.get("direction") == r.get("direction"))
            ]
            rules.append(r)
        return {"rules": rules}

    @staticmethod
    def _merge_pihole(base: dict, new: dict) -> dict:
        block = set(base.get("blocklists", [])) | set(new.get("blocklists", []))
        allow = set(base.get("allowlists", [])) | set(new.get("allowlists", []))
        # Specifické blokované domény pro tohoto klienta (vyšší úroveň vyhrává nad nižší)
        custom_block = set(base.get("custom_block", []))
        custom_allow = set(base.get("custom_allow", []))
        for d in new.get("custom_block", []):
            custom_allow.discard(d)
            custom_block.add(d)
        for d in new.get("custom_allow", []):
            custom_block.discard(d)
            custom_allow.add(d)
        return {
            "blocklists": sorted(block),
            "allowlists": sorted(allow),
            "custom_block": sorted(custom_block),
            "custom_allow": sorted(custom_allow),
        }

    @staticmethod
    def _merge_software(base: dict, new: dict) -> dict:
        install = set(base.get("install", [])) | set(new.get("install", []))
        remove = set(base.get("remove", [])) | set(new.get("remove", []))
        # remove vyhrává nad install
        install -= remove
        return {"install": sorted(install), "remove": sorted(remove)}
