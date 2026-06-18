from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


class FTSIndex:
    def __init__(self, db_path: Path, *, chunk_chars: int = 800):
        self.db_path = Path(db_path)
        self.chunk_chars = max(100, int(chunk_chars))

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    title TEXT,
                    folder TEXT,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    heading TEXT,
                    text TEXT NOT NULL,
                    chunk_hash TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_hash UNINDEXED,
                    path UNINDEXED,
                    heading,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def stats(self) -> dict:
        with self._connect() as conn:
            sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"db_path": str(self.db_path), "sources": sources, "chunks": chunks}

    def remove_page(self, relpath: str) -> None:
        with self._connect() as conn:
            hashes = [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_hash FROM chunks WHERE source_id = (SELECT id FROM sources WHERE path = ?)",
                    (relpath,),
                ).fetchall()
            ]
            conn.execute("DELETE FROM chunks WHERE source_id = (SELECT id FROM sources WHERE path = ?)", (relpath,))
            conn.execute("DELETE FROM sources WHERE path = ?", (relpath,))
            for chunk_hash in hashes:
                conn.execute("DELETE FROM chunks_fts WHERE chunk_hash = ?", (chunk_hash,))

    def index_page(self, relpath: str, content: str, chunks: list[dict]) -> None:
        self.remove_page(relpath)
        folder = Path(relpath).parts[0] if len(Path(relpath).parts) > 1 else ""
        title = Path(relpath).stem
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO sources(path, title, folder) VALUES (?, ?, ?)", (relpath, title, folder))
            source_id = cur.lastrowid
            for chunk_index, chunk in enumerate(chunks):
                text = chunk["text"]
                for piece_no, start in enumerate(range(0, max(len(text), 1), self.chunk_chars)):
                    piece = text[start : start + self.chunk_chars].strip()
                    if not piece:
                        continue
                    chunk_hash = hashlib.sha256(
                        f"{relpath}:{chunk_index}:{piece_no}:{piece}".encode("utf-8")
                    ).hexdigest()
                    conn.execute(
                        "INSERT INTO chunks(source_id, chunk_index, heading, text, chunk_hash) VALUES (?, ?, ?, ?, ?)",
                        (source_id, chunk_index * 100 + piece_no, chunk["heading"], piece, chunk_hash),
                    )
                    conn.execute(
                        "INSERT INTO chunks_fts(chunk_hash, path, heading, text) VALUES (?, ?, ?, ?)",
                        (chunk_hash, relpath, chunk["heading"], piece),
                    )

    def reindex_missing(self, pages: list[str], read_page, chunk_page) -> None:
        with self._connect() as conn:
            indexed = {row[0] for row in conn.execute("SELECT path FROM sources").fetchall()}
        for rel in pages:
            if rel not in indexed:
                content = read_page(rel)
                self.index_page(rel, content, chunk_page(rel, content))

    def search(self, query: str, *, top_k: int = 5) -> list[dict]:
        tokens = [token.strip() for token in query.lower().split() if token.strip()]
        if not tokens:
            return []
        match_query = " OR ".join(tokens)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT path, heading, text, bm25(chunks_fts) AS score
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (match_query, int(top_k)),
            ).fetchall()
        return [
            {
                "path": row["path"],
                "heading": row["heading"],
                "snippet": row["text"][:240],
                "score": row["score"],
            }
            for row in rows
        ]
