import base64
import io
import json
import math
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image, UnidentifiedImageError


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def cosine(left, right):
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))


class Store:
    def __init__(self, config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.data_dir / "steamlab.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS streams (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, archived_at TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT);
            CREATE TABLE IF NOT EXISTS faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT, embedding TEXT NOT NULL,
                thumbnail BLOB NOT NULL, quality REAL NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                sightings INTEGER NOT NULL, detection_confidence REAL NOT NULL,
                match_similarity REAL);
            CREATE TABLE IF NOT EXISTS face_sessions (
                face_id INTEGER REFERENCES faces(id) ON DELETE CASCADE,
                session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, sightings INTEGER NOT NULL,
                PRIMARY KEY(face_id, session_id));
            CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id),
                started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                error TEXT);
            CREATE INDEX IF NOT EXISTS face_last_seen ON faces(last_seen);
            CREATE INDEX IF NOT EXISTS face_session_id ON face_sessions(session_id);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO streams VALUES ('stream', 'Stream 1', ?, NULL)", (utcnow(),))
            # Add ownership without rebuilding tables or changing existing row IDs,
            # keys, thumbnail blobs, or UUID recording filenames.
            for table in ("sessions", "faces", "recordings"):
                columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if "stream_id" not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN stream_id TEXT NOT NULL DEFAULT 'stream'")
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {table}_stream_id ON {table}(stream_id)")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('stream_key', ?)",
                            (secrets.token_urlsafe(32),))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('catalog_version', '0')")
            # Require a fresh opt-in after a backend restart.
            for stream in self.streams():
                self.set("analysis_enabled", "0", stream["id"])
            self.db.execute("UPDATE sessions SET ended_at=? WHERE ended_at IS NULL", (utcnow(),))
            self.db.execute("""UPDATE recordings SET ended_at=?, status='interrupted',
                            error='Backend restarted before recording was finalized'
                            WHERE status='recording'""", (utcnow(),))
            self.db.execute("PRAGMA user_version=1")

    def streams(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM streams ORDER BY created_at, id")]

    def stream(self, stream_id):
        row = self.db.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
        return dict(row) if row else None

    def add_stream(self, name):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT COUNT(*) FROM streams WHERE archived_at IS NULL").fetchone()[0] >= 4:
                raise ValueError("At most four active streams are allowed")
            stream_id = "stream-" + uuid.uuid4().hex
            self.db.execute("INSERT INTO streams VALUES (?, ?, ?, NULL)", (stream_id, name, utcnow()))
            self.set("stream_key", secrets.token_urlsafe(32), stream_id)
            self.set("catalog_version", 0, stream_id)
            self.set("analysis_enabled", 0, stream_id)
        return self.stream(stream_id)

    def get(self, key, stream_id="stream"):
        if stream_id != "stream":
            key = f"stream:{stream_id}:{key}"
        return self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]

    def set(self, key, value, stream_id="stream"):
        if stream_id != "stream":
            key = f"stream:{stream_id}:{key}"
        self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def invalidate_catalog(self, stream_id="stream"):
        self.set("catalog_version", int(self.get("catalog_version", stream_id)) + 1, stream_id)

    def prune(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.config.face_retention_days)).isoformat()
        with self.db:
            affected = [row[0] for row in self.db.execute("SELECT DISTINCT stream_id FROM faces WHERE last_seen < ?", (cutoff,))]
            self.db.execute("DELETE FROM faces WHERE last_seen < ?", (cutoff,))
            self.db.execute("DELETE FROM face_sessions WHERE last_seen < ?", (cutoff,))
            for stream_id in affected:
                self.invalidate_catalog(stream_id)
        # secure_delete also needs a checkpoint before old pages leave the WAL.
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def observations(self, batch):
        stream_id = batch.stream_id
        captured = batch.captured_at.astimezone(timezone.utc)
        timestamp = captured.isoformat()
        normalized = []
        for face in batch.faces:
            if face.confidence < self.config.detection_threshold:
                continue
            norm = math.sqrt(sum(x * x for x in face.embedding))
            if not math.isfinite(norm) or not 0.99 <= norm <= 1.01:
                raise ValueError("Embeddings must be finite unit vectors")
            try:
                jpeg = base64.b64decode(face.thumbnail, validate=True)
                if len(jpeg) > 100_000:
                    raise ValueError("Face image too large")
                with Image.open(io.BytesIO(jpeg)) as image:
                    if image.format != "JPEG" or not 16 <= image.width <= 512 or not 16 <= image.height <= 512:
                        raise ValueError("Invalid face JPEG dimensions")
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise ValueError("Invalid face JPEG") from exc
            normalized.append((face, [x / norm for x in face.embedding], jpeg))

        # One assignment per group per frame prevents two simultaneously visible
        # people from collapsing into the same group. Keep fixed anchor embeddings
        # rather than allowing a mistaken match to drift the group's identity.
        groups = [{**dict(row), "vector": json.loads(row["embedding"])} for row in
                  self.db.execute("SELECT id, embedding, last_seen, quality FROM faces WHERE stream_id=?", (stream_id,))]
        total_faces = self.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        used = set()
        accepted = 0
        with self.db:
            for face, vector, jpeg in sorted(normalized, key=lambda item: item[0].confidence, reverse=True):
                candidates = [(cosine(vector, group["vector"]), group) for group in groups if group["id"] not in used]
                score, group = max(candidates, key=lambda pair: pair[0]) if candidates else (-1, None)
                if group is None or score < self.config.match_threshold:
                    if total_faces >= self.config.max_faces:
                        continue
                    cursor = self.db.execute("""INSERT INTO faces
                        (embedding, thumbnail, quality, first_seen, last_seen, sightings, detection_confidence, stream_id)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                        (json.dumps(vector), jpeg, face.quality, timestamp, timestamp, face.confidence, stream_id))
                    total_faces += 1
                    face_id = cursor.lastrowid
                    group = {"id": face_id, "vector": vector, "last_seen": timestamp, "quality": face.quality}
                    groups.append(group)
                else:
                    face_id = group["id"]
                    if timestamp <= group["last_seen"]:
                        used.add(face_id)
                        continue
                    previous = self.db.execute("SELECT last_seen FROM face_sessions WHERE face_id=? AND session_id=?",
                                               (face_id, batch.session_id)).fetchone()
                    new_sighting = previous is None or (captured - datetime.fromisoformat(previous[0])).total_seconds() >= 10
                    self.db.execute("""UPDATE faces SET last_seen=?, sightings=sightings+?,
                        detection_confidence=?, match_similarity=? WHERE id=?""",
                        (timestamp, int(new_sighting), face.confidence, score, face_id))
                    if face.quality > group["quality"]:
                        self.db.execute("UPDATE faces SET thumbnail=?, quality=? WHERE id=?", (jpeg, face.quality, face_id))
                previous = self.db.execute("SELECT last_seen FROM face_sessions WHERE face_id=? AND session_id=?",
                                           (face_id, batch.session_id)).fetchone()
                count = int(previous is None or (captured - datetime.fromisoformat(previous[0])).total_seconds() >= 10)
                self.db.execute("""INSERT INTO face_sessions VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(face_id, session_id) DO UPDATE SET
                    last_seen=excluded.last_seen, sightings=face_sessions.sightings+?""",
                    (face_id, batch.session_id, timestamp, timestamp, count))
                used.add(face_id)
                accepted += 1
        return accepted

    def faces(self, session_id, limit, offset, stream_id="stream"):
        if session_id:
            source = "faces f JOIN face_sessions s ON f.id=s.face_id WHERE f.stream_id=? AND s.session_id=?"
            params = [stream_id, session_id]
            fields = "f.id, s.first_seen, s.last_seen, s.sightings, f.detection_confidence, f.match_similarity"
            order = "s.last_seen"
        else:
            source, params = "faces f WHERE f.stream_id=?", [stream_id]
            fields = "f.id, f.first_seen, f.last_seen, f.sightings, f.detection_confidence, f.match_similarity"
            order = "f.last_seen"
        total = self.db.execute(f"SELECT COUNT(*) FROM {source}", params).fetchone()[0]
        rows = self.db.execute(f"SELECT {fields} FROM {source} ORDER BY {order} DESC, f.id DESC LIMIT ? OFFSET ?",
                               [*params, limit, offset])
        return {"items": [{**dict(row), "stream_id": stream_id, "label": f"Face {row['id']:03d}",
                            "thumbnail_url": f"/api/faces/{row['id']}/thumbnail?stream_id={stream_id}"} for row in rows], "total": total}
