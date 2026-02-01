import sqlite3
import time


class DB:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path, check_same_thread=False)

    def _init(self):
        con = self._conn()
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at INTEGER,
            last_seen INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet(
            user_id INTEGER PRIMARY KEY,
            credits INTEGER DEFAULT 0,
            validity_start INTEGER,
            validity_expire INTEGER,
            free_claimed INTEGER DEFAULT 0,
            videos_made INTEGER DEFAULT 0
        )
        """)

        con.commit()
        con.close()

    # ---------- users ----------
    def upsert_user(self, u):
        now = int(time.time())
        con = self._conn()
        cur = con.cursor()

        cur.execute(
            "INSERT OR IGNORE INTO users(id, username, first_name, joined_at, last_seen) VALUES(?,?,?,?,?)",
            (u.id, u.username, u.first_name, now, now),
        )
        cur.execute(
            "UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?",
            (u.username, u.first_name, now, u.id),
        )

        cur.execute("INSERT OR IGNORE INTO wallet(user_id) VALUES(?)", (u.id,))
        con.commit()
        con.close()

    def ensure_user(self, user_id: int, username: str = None):
        now = int(time.time())
        con = self._conn()
        cur = con.cursor()

        cur.execute(
            "INSERT OR IGNORE INTO users(id, username, first_name, joined_at, last_seen) VALUES(?,?,?,?,?)",
            (user_id, username, "", now, now),
        )
        cur.execute("INSERT OR IGNORE INTO wallet(user_id) VALUES(?)", (user_id,))

        con.commit()
        con.close()

    def count_users(self) -> int:
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        n = int(cur.fetchone()[0] or 0)
        con.close()
        return n

    def list_users(self, offset=0, limit=10):
        con = self._conn()
        cur = con.cursor()
        cur.execute("""
            SELECT u.id, u.username, IFNULL(w.credits,0)
            FROM users u
            LEFT JOIN wallet w ON w.user_id=u.id
            ORDER BY u.joined_at DESC
            LIMIT ? OFFSET ?
        """, (int(limit), int(offset)))
        rows = cur.fetchall()
        con.close()

        out = []
        for r in rows:
            out.append({"id": int(r[0]), "username": r[1], "credits": int(r[2])})
        return out

    def list_user_ids(self):
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT id FROM users")
        rows = cur.fetchall()
        con.close()
        return [int(r[0]) for r in rows]

    # ---------- credits / validity ----------
    def get_credit(self, user_id: int):
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT credits, validity_start, validity_expire FROM wallet WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        con.close()
        if not row:
            return 0, None, None
        return int(row[0] or 0), row[1], row[2]

    def add_credits(self, user_id: int, amount: int):
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("UPDATE wallet SET credits = credits + ? WHERE user_id=?", (int(amount), user_id))
        con.commit()
        con.close()

    def remove_credits(self, user_id: int, amount: int):
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT credits FROM wallet WHERE user_id=?", (user_id,))
        c = int(cur.fetchone()[0] or 0)
        c2 = max(0, c - int(amount))
        cur.execute("UPDATE wallet SET credits=? WHERE user_id=?", (c2, user_id))
        con.commit()
        con.close()

    def deduct_for_video(self, user_id: int, cost: int) -> bool:
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT credits FROM wallet WHERE user_id=?", (user_id,))
        c = int(cur.fetchone()[0] or 0)
        if c < cost:
            con.close()
            return False
        cur.execute("UPDATE wallet SET credits = credits - ? WHERE user_id=?", (int(cost), user_id))
        con.commit()
        con.close()
        return True

    def set_validity(self, user_id: int, days: int):
        self.ensure_user(user_id)
        now = int(time.time())
        exp = now + int(days) * 86400
        con = self._conn()
        cur = con.cursor()
        cur.execute(
            "UPDATE wallet SET validity_start=?, validity_expire=? WHERE user_id=?",
            (now, exp, user_id),
        )
        con.commit()
        con.close()

    def remove_validity(self, user_id: int):
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute(
            "UPDATE wallet SET validity_start=NULL, validity_expire=NULL WHERE user_id=?",
            (user_id,),
        )
        con.commit()
        con.close()

    def list_premium(self, limit=50):
        now = int(time.time())
        con = self._conn()
        cur = con.cursor()
        cur.execute("""
            SELECT u.id, IFNULL(w.credits,0), w.validity_start, w.validity_expire
            FROM users u
            JOIN wallet w ON w.user_id=u.id
            WHERE w.validity_expire IS NOT NULL AND w.validity_expire > ?
            ORDER BY w.validity_expire DESC
            LIMIT ?
        """, (now, int(limit)))
        rows = cur.fetchall()
        con.close()

        out = []
        for r in rows:
            out.append({"id": int(r[0]), "credits": int(r[1]), "vfrom": r[2], "exp": r[3]})
        return out

    # ---------- free claim ----------
    def free_claimed(self, user_id: int) -> bool:
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT free_claimed FROM wallet WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        con.close()
        return bool(row and int(row[0] or 0) == 1)

    def mark_free_claimed(self, user_id: int):
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("UPDATE wallet SET free_claimed=1 WHERE user_id=?", (user_id,))
        con.commit()
        con.close()

    # ---------- usage ----------
    def inc_videos(self, user_id: int):
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("UPDATE wallet SET videos_made = videos_made + 1 WHERE user_id=?", (user_id,))
        con.commit()
        con.close()

    def get_usage(self, user_id: int) -> int:
        self.ensure_user(user_id)
        con = self._conn()
        cur = con.cursor()
        cur.execute("SELECT videos_made FROM wallet WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        con.close()
        return int(row[0] or 0) if row else 0
