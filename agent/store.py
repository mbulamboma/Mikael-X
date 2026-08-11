# -*- coding: utf-8 -*-
"""Persistance SQLite — journal + etats, resistants a un arret brutal.

Pourquoi SQLite plutot que des fichiers JSON : un `write_text()` n'est PAS atomique.
Si le process est tue (VPS qui redemarre, Ctrl-C, crash MT5) pendant l'ecriture de
`open_meta.json`, on perd la correspondance ticket -> strategie/risque, donc le
suivi du trailing, l'attribution du R et le post-mortem. Avec SQLite :

  - chaque ecriture est une TRANSACTION (tout ou rien),
  - le mode WAL survit a une coupure et permet de lire pendant qu'on ecrit,
  - au redemarrage, l'etat est exactement celui d'avant l'arret,
  - le journal devient interrogeable en SQL (audit, statistiques, post-mortem).

Tables :
  events    : journal horodate (kind + payload JSON) — ordres, vetos, clotures, secours...
  lessons   : lecons apprises (apprentissage)
  open_meta : ticket -> metadonnees de la position (strategie, risque, trailing, MFE/MAE)
  kv        : etats divers (session FTMO, mode secours, cache news) en JSON

Migration : au premier demarrage, les anciens fichiers JSON/JSONL sont importes puis
renommes en `*.migrated` (rien n'est supprime).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import STATE_DIR

log = logging.getLogger("store")

DB_PATH = Path(os.environ.get("AGENT_DB", "")) if os.environ.get("AGENT_DB") \
    else STATE_DIR / "agent.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id);

CREATE TABLE IF NOT EXISTS lessons (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    symbol  TEXT,
    outcome TEXT,
    text    TEXT NOT NULL,
    tags    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS open_meta (
    ticket  TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Acces SQLite unique du process (thread-safe, autocommit, WAL)."""

    def __init__(self, path: Path | str | None = None, migrate: bool = True):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")      # survit a une coupure
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA busy_timeout=30000")
            self.db.executescript(_SCHEMA)
        if migrate:
            self._migrate_json_files()

    # ------------------------------------------------------------------ interne
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, args)

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ journal
    def log_event(self, kind: str, payload: dict):
        self._exec("INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                   (_now(), str(kind), json.dumps(payload, ensure_ascii=False, default=str)))

    def events(self, kind: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT ts, kind, payload FROM events"
        args: tuple = ()
        if kind:
            sql += " WHERE kind = ?"
            args = (kind,)
        sql += " ORDER BY id"
        if limit:
            sql = (f"SELECT * FROM ({sql} DESC LIMIT {int(limit)}) ORDER BY ts")
        out = []
        for r in self._rows(sql, args):
            try:
                data = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                data = {}
            out.append({"ts": r["ts"], "kind": r["kind"], **(data if isinstance(data, dict) else {})})
        return out

    # ------------------------------------------------------------------ lecons
    def add_lesson(self, symbol: str, outcome: str, text: str, tags: list[str] | None = None):
        self._exec("INSERT INTO lessons (ts, symbol, outcome, text, tags) VALUES (?, ?, ?, ?, ?)",
                   (_now(), symbol, outcome, text.strip(),
                    json.dumps(tags or [], ensure_ascii=False)))

    def lessons(self) -> list[dict]:
        out = []
        for r in self._rows("SELECT ts, symbol, outcome, text, tags FROM lessons ORDER BY id"):
            try:
                tags = json.loads(r["tags"])
            except (json.JSONDecodeError, TypeError):
                tags = []
            out.append({"ts": r["ts"], "symbol": r["symbol"], "outcome": r["outcome"],
                        "text": r["text"], "tags": tags if isinstance(tags, list) else []})
        return out

    # ------------------------------------------------------------------ positions
    def meta_all(self) -> dict:
        out = {}
        for r in self._rows("SELECT ticket, data FROM open_meta"):
            try:
                out[r["ticket"]] = json.loads(r["data"])
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def meta_replace(self, meta: dict):
        """Remplace l'ensemble des metadonnees en UNE transaction (tout ou rien)."""
        payload = [(str(k), json.dumps(v, ensure_ascii=False, default=str), _now())
                   for k, v in meta.items()]
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("DELETE FROM open_meta")
                self.db.executemany(
                    "INSERT INTO open_meta (ticket, data, updated) VALUES (?, ?, ?)", payload)
                self.db.execute("COMMIT")
            except sqlite3.Error as e:
                self.db.execute("ROLLBACK")
                log.error("Ecriture open_meta impossible: %s", e)

    # ------------------------------------------------------------------ etats (kv)
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM kv WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def kv_set(self, key: str, value: Any):
        self._exec("INSERT INTO kv (key, value, updated) VALUES (?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                   (key, json.dumps(value, ensure_ascii=False, default=str), _now()))

    def kv_delete(self, key: str):
        self._exec("DELETE FROM kv WHERE key = ?", (key,))

    # ------------------------------------------------------------------ migration
    def _migrate_json_files(self):
        """Importe l'ancien etat fichier (une seule fois) puis le met de cote."""
        d = self.path.parent
        jobs = (
            ("trades.jsonl", self._import_trades),
            ("lessons.json", self._import_lessons),
            ("session.json", lambda data: self.kv_set("session", data)),
            ("open_meta.json", lambda data: self.meta_replace(data)),
            ("news_cache.json", lambda data: self.kv_set("news_cache", data)),
            ("safe_mode.json", lambda data: self.kv_set("safe_mode", data)),
        )
        for name, importer in jobs:
            path = d / name
            if not path.exists():
                continue
            try:
                if name.endswith(".jsonl"):
                    importer(path.read_text(encoding="utf-8").splitlines())
                else:
                    importer(json.loads(path.read_text(encoding="utf-8")))
                path.rename(path.with_suffix(path.suffix + ".migrated"))
                log.info("Etat migre vers SQLite: %s -> %s", name, self.path.name)
            except (OSError, json.JSONDecodeError, ValueError, sqlite3.Error) as e:
                log.warning("Migration de %s impossible (ignore): %s", name, e)

    def _import_trades(self, lines: list[str]):
        rows = []
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            ts = r.pop("ts", _now())
            kind = r.pop("kind", "event")
            rows.append((ts, kind, json.dumps(r, ensure_ascii=False, default=str)))
        with self._lock:
            self.db.executemany("INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)", rows)

    def _import_lessons(self, data: list):
        if not isinstance(data, list):
            return
        rows = [(l.get("ts", _now()), l.get("symbol", ""), l.get("outcome", ""),
                 str(l.get("text", "")).strip(),
                 json.dumps(l.get("tags", []), ensure_ascii=False))
                for l in data if isinstance(l, dict) and l.get("text")]
        with self._lock:
            self.db.executemany(
                "INSERT INTO lessons (ts, symbol, outcome, text, tags) VALUES (?, ?, ?, ?, ?)", rows)


_DEFAULT: Optional[Store] = None


def default_store() -> Store:
    """Store partage du process (cree a la premiere utilisation)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Store()
    return _DEFAULT


def set_default_store(store: Store):
    """Utilise par les tests pour pointer vers une base temporaire."""
    global _DEFAULT
    _DEFAULT = store
