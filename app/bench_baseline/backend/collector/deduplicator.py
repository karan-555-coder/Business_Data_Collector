"""Cross-category deduplication on normalized domain, company name, email and
phone. The registry persists in the checkpoint so a company found again in a
later run or another category is recognized as a duplicate."""

from __future__ import annotations

import threading

from .normalize import norm_domain, norm_email, norm_name, norm_phone


class DedupRegistry:
    def __init__(self):
        self.domains: dict[str, str] = {}   # key -> "Category|Company"
        self.names: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        self.phones: dict[str, str] = {}
        self.lock = threading.Lock()

    def _keys(self, record: dict) -> tuple[str, str, list[str], list[str]]:
        dom = norm_domain(record.get("Official Website", ""))
        if not dom and record.get("_cid"):
            dom = f"cid:{record['_cid']}"
        nm = norm_name(record.get("Company Name", ""))
        emails = [norm_email(e) for e in record.get("Business Email", "").split(";") if norm_email(e)]
        phones = [norm_phone(p) for p in record.get("Business Phone", "").split(";") if norm_phone(p)]
        return dom, nm, emails, phones

    def peek(self, record: dict) -> str:
        """Non-mutating duplicate probe: the owner of an existing record that
        matches on domain/cid, name, email or phone, else "". Lets the engine
        skip crawling a business it already has."""
        dom, nm, emails, phones = self._keys(record)
        with self.lock:
            if dom and dom in self.domains:
                return self.domains[dom]
            if nm and nm in self.names:
                return self.names[nm]
            for e in emails:
                if e in self.emails:
                    return self.emails[e]
            for p in phones:
                if p in self.phones:
                    return self.phones[p]
        return ""

    def check_and_add(self, record: dict, category: str) -> tuple[bool, str]:
        """Atomically test for duplicates and register the record.
        Returns (added, duplicate_of) — duplicate_of names the earlier record."""
        dom, nm, emails, phones = self._keys(record)
        owner = f"{category}|{record.get('Company Name', '')[:60]}"
        with self.lock:
            if dom and dom in self.domains:
                return False, self.domains[dom]
            if nm and nm in self.names:
                return False, self.names[nm]
            for e in emails:
                if e in self.emails:
                    return False, self.emails[e]
            for p in phones:
                if p in self.phones:
                    return False, self.phones[p]
            if dom:
                self.domains[dom] = owner
            if nm:
                self.names[nm] = owner
            for e in emails:
                self.emails[e] = owner
            for p in phones:
                self.phones[p] = owner
            return True, ""

    def ensure(self, record: dict, category: str) -> int:
        """Register any missing keys of an already-stored record (checkpoint
        repair: the registry snapshot can predate the newest records).
        Existing owners are never overwritten. Returns keys added."""
        dom, nm, emails, phones = self._keys(record)
        owner = f"{category}|{record.get('Company Name', '')[:60]}"
        added = 0
        with self.lock:
            for table, keys in ((self.domains, [dom]), (self.names, [nm]),
                                (self.emails, emails), (self.phones, phones)):
                for k in keys:
                    if k and k not in table:
                        table[k] = owner
                        added += 1
        return added

    # -- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        with self.lock:   # copies: serialised outside the lock
            return {"domains": dict(self.domains), "names": dict(self.names),
                    "emails": dict(self.emails), "phones": dict(self.phones)}

    @classmethod
    def from_dict(cls, data: dict) -> "DedupRegistry":
        reg = cls()
        reg.domains = dict(data.get("domains", {}))
        reg.names = dict(data.get("names", {}))
        reg.emails = dict(data.get("emails", {}))
        reg.phones = dict(data.get("phones", {}))
        return reg
