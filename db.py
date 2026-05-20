import sqlite3
import threading
import hashlib
from pathlib import Path
import config

_conn = None
_lock = threading.Lock()
_last_backup_hash = None


def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.SQLITE_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    else:
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
    return _conn


def _write(fn):
    """Thread-safe wrapper for SQLite write operations."""
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapper


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS merchants (
            discord_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            trust_score INTEGER DEFAULT 50,
            auto_confirm_max REAL DEFAULT 0,
            trust_mode TEXT DEFAULT 'local',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id TEXT NOT NULL REFERENCES merchants(discord_id),
            name TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            code_value TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            order_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS buyers (
            discord_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            trust_score INTEGER DEFAULT 50,
            total_orders INTEGER DEFAULT 0,
            total_spent REAL DEFAULT 0,
            rejected_orders INTEGER DEFAULT 0,
            flagged INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id TEXT NOT NULL,
            buyer_id TEXT NOT NULL,
            buyer_name TEXT,
            product_name TEXT,
            amount REAL,
            tx_id TEXT,
            receipt_url TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            confirmed_at TEXT,
            delivered_at TEXT,
            UNIQUE(tx_id)
        );

        CREATE TABLE IF NOT EXISTS trust_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            event TEXT NOT NULL,
            delta INTEGER NOT NULL,
            order_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            details TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS buyer_merchant_relations (
            buyer_id TEXT NOT NULL,
            merchant_id TEXT NOT NULL,
            trust_score INTEGER DEFAULT 50,
            total_orders INTEGER DEFAULT 0,
            total_spent REAL DEFAULT 0,
            rejected_orders INTEGER DEFAULT 0,
            last_order_at TEXT,
            PRIMARY KEY (buyer_id, merchant_id)
        );

        CREATE TABLE IF NOT EXISTS server_config (
            guild_id TEXT PRIMARY KEY,
            merchant_role_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    try:
        conn.execute("ALTER TABLE merchants ADD COLUMN trust_mode TEXT DEFAULT 'local'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_tx_id ON orders(tx_id) WHERE tx_id IS NOT NULL AND tx_id != 'unknown'")
        conn.commit()
    except sqlite3.IntegrityError:
        # Existing duplicates — keep them, index won't enforce uniqueness retroactively
        pass
    except sqlite3.OperationalError:
        pass


@_write
def upsert_buyer(discord_id, name):
    conn = get_conn()
    conn.execute(
        """INSERT INTO buyers (discord_id, name) VALUES (?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET name = ?""",
        (discord_id, name, name),
    )
    conn.commit()


@_write
def update_buyer_stats(discord_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT COUNT(*) as total, COALESCE(SUM(amount), 0) as spent FROM orders WHERE buyer_id = ? AND status = 'delivered'",
        (discord_id,),
    ).fetchone()
    rejected = conn.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE buyer_id = ? AND status = 'rejected'",
        (discord_id,),
    ).fetchone()
    trust = get_trust_score_with_decay(discord_id)
    conn.execute(
        """UPDATE buyers SET total_orders = ?, total_spent = ?, rejected_orders = ?, trust_score = ?
           WHERE discord_id = ?""",
        (rows["total"], rows["spent"], rejected["cnt"], trust, discord_id),
    )
    conn.commit()


@_write
def toggle_buyer_flag(discord_id):
    conn = get_conn()
    conn.execute(
        "UPDATE buyers SET flagged = CASE WHEN flagged = 0 THEN 1 ELSE 0 END WHERE discord_id = ?",
        (discord_id,),
    )
    conn.commit()
    row = conn.execute("SELECT flagged FROM buyers WHERE discord_id = ?", (discord_id,)).fetchone()
    return row["flagged"] if row else None


def get_buyer(discord_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM buyers WHERE discord_id = ?", (discord_id,)).fetchone()
    return dict(row) if row else None


def get_all_buyers(merchant_id=None):
    conn = get_conn()
    if merchant_id:
        rows = conn.execute(
            """SELECT DISTINCT b.* FROM buyers b
               JOIN orders o ON o.buyer_id = b.discord_id
               WHERE o.merchant_id = ?
               ORDER BY b.trust_score ASC, b.total_orders DESC""",
            (merchant_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM buyers ORDER BY trust_score ASC, total_orders DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@_write
def create_merchant(discord_id, name):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO merchants (discord_id, name) VALUES (?, ?)",
        (discord_id, name),
    )
    conn.commit()


def get_merchant(discord_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM merchants WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    return dict(row) if row else None


MERCHANT_UPDATEABLE = {"auto_confirm_max", "trust_mode", "trust_score", "name"}


@_write
def update_merchant(discord_id, **kwargs):
    safe = {k: v for k, v in kwargs.items() if k in MERCHANT_UPDATEABLE}
    if not safe:
        return
    conn = get_conn()
    sets = ", ".join(f"{k} = ?" for k in safe)
    vals = list(safe.values()) + [discord_id]
    conn.execute(f"UPDATE merchants SET {sets} WHERE discord_id = ?", vals)
    conn.commit()


@_write
def set_merchant_role(guild_id, role_id):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO server_config (guild_id, merchant_role_id) VALUES (?, ?)",
        (guild_id, role_id),
    )
    conn.commit()


def get_merchant_role(guild_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT merchant_role_id FROM server_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    return row["merchant_role_id"] if row else None


@_write
def add_product(merchant_id, name, price, codes_list):
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO products (merchant_id, name, price) VALUES (?, ?, ?)",
        (merchant_id, name, price),
    )
    product_id = cursor.lastrowid
    for code in codes_list:
        conn.execute(
            "INSERT INTO codes (product_id, code_value) VALUES (?, ?)",
            (product_id, code.strip()),
        )
    conn.commit()
    return product_id


def get_products(merchant_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM codes WHERE product_id = p.id AND used = 0) as stock
           FROM products p WHERE p.merchant_id = ? ORDER BY p.created_at DESC""",
        (merchant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_product_by_id(product_id, merchant_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM products WHERE id = ? AND merchant_id = ?",
        (product_id, merchant_id),
    ).fetchone()
    return dict(row) if row else None


@_write
def assign_code(product_id, order_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, code_value FROM codes WHERE product_id = ? AND used = 0 ORDER BY id LIMIT 1",
            (product_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE codes SET used = 1, order_id = ? WHERE id = ? AND used = 0",
            (order_id, row["id"]),
        )
        if conn.total_changes == 0:
            return None
        conn.commit()
        return row["code_value"]
    except Exception:
        conn.rollback()
        return None


@_write
def restock_code(code_value, merchant_id):
    conn = get_conn()
    cursor = conn.execute(
        """UPDATE codes SET used = 0, order_id = NULL
           WHERE code_value = ? AND product_id IN
           (SELECT id FROM products WHERE merchant_id = ?)""",
        (code_value, merchant_id),
    )
    conn.commit()
    return cursor.rowcount > 0


@_write
def create_order(merchant_id, buyer_id, buyer_name, amount, tx_id, receipt_url, product_name=None):
    conn = get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO orders (merchant_id, buyer_id, buyer_name, product_name, amount, tx_id, receipt_url)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (merchant_id, buyer_id, buyer_name, product_name, amount, tx_id, receipt_url),
        )
        order_id = cursor.lastrowid
        conn.commit()
        return order_id
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


def get_order(order_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def get_orders(merchant_id, limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE merchant_id = ? ORDER BY created_at DESC LIMIT ?",
        (merchant_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_merchant_orders(merchant_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE merchant_id = ? ORDER BY created_at DESC",
        (merchant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_order_by_tx_id(tx_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM orders WHERE tx_id = ? AND status != 'rejected' LIMIT 1",
        (tx_id,),
    ).fetchone()
    return dict(row) if row else None


def get_all_orders():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@_write
def update_order_status(order_id, status):
    conn = get_conn()
    if status == "delivered":
        conn.execute(
            """UPDATE orders SET status = ?, confirmed_at = datetime('now'),
               delivered_at = datetime('now') WHERE id = ?""",
            (status, order_id),
        )
    elif status == "confirming":
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
    elif status == "rejected":
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
    else:
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
    conn.commit()


def get_trust_score(user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) as score FROM trust_events WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row["score"] + 50 if row else 50


def get_trust_score_with_decay(user_id):
    base = get_trust_score(user_id)
    if base <= config.TRUST_MAX_FLOOR:
        return base
    conn = get_conn()
    row = conn.execute(
        """SELECT MAX(created_at) as last_event,
                  (julianday('now') - julianday(MAX(created_at))) as days_since
           FROM trust_events WHERE user_id = ?""",
        (user_id,),
    ).fetchone()
    if not row or not row["last_event"] or row["days_since"] is None:
        return base
    days_since = row["days_since"]
    if days_since > config.TRUST_DECAY_DAYS:
        cycles = int(days_since // config.TRUST_DECAY_DAYS)
        decay = cycles * config.TRUST_DECAY_AMOUNT
        base = max(config.TRUST_MAX_FLOOR, base - decay)
    return base


@_write
def add_trust_event(user_id, event, delta, order_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trust_events (user_id, event, delta, order_id) VALUES (?, ?, ?, ?)",
        (user_id, event, delta, order_id),
    )
    conn.commit()


@_write
def log_audit(actor_id, action, target_type, target_id=None, details=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log (actor_id, action, target_type, target_id, details) VALUES (?, ?, ?, ?, ?)",
        (actor_id, action, target_type, target_id, details),
    )
    conn.commit()


@_write
def upsert_buyer_merchant(buyer_id, merchant_id):
    conn = get_conn()
    conn.execute(
        """INSERT INTO buyer_merchant_relations (buyer_id, merchant_id, trust_score, total_orders, total_spent, rejected_orders)
           VALUES (?, ?, 50, 0, 0, 0)
           ON CONFLICT(buyer_id, merchant_id) DO NOTHING""",
        (buyer_id, merchant_id),
    )
    conn.execute(
        "UPDATE buyer_merchant_relations SET last_order_at = datetime('now') WHERE buyer_id = ? AND merchant_id = ?",
        (buyer_id, merchant_id),
    )
    conn.commit()


@_write
def update_buyer_merchant_on_delivery(buyer_id, merchant_id, amount):
    conn = get_conn()
    conn.execute(
        """UPDATE buyer_merchant_relations
           SET total_orders = total_orders + 1,
               total_spent = total_spent + ?,
               trust_score = trust_score + ?,
               last_order_at = datetime('now')
           WHERE buyer_id = ? AND merchant_id = ?""",
        (amount, config.TRUST_CONFIRM_BONUS, buyer_id, merchant_id),
    )
    conn.commit()


@_write
def update_buyer_merchant_on_rejection(buyer_id, merchant_id, penalty):
    conn = get_conn()
    conn.execute(
        """UPDATE buyer_merchant_relations
           SET rejected_orders = rejected_orders + 1,
               trust_score = MAX(?, trust_score - ?),
               last_order_at = datetime('now')
           WHERE buyer_id = ? AND merchant_id = ?""",
        (config.TRUST_MAX_FLOOR, penalty, buyer_id, merchant_id),
    )
    conn.commit()


def get_merchant_buyers(merchant_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT bmr.*, b.name, b.flagged, b.trust_score as global_trust
           FROM buyer_merchant_relations bmr
           JOIN buyers b ON b.discord_id = bmr.buyer_id
           WHERE bmr.merchant_id = ?
           ORDER BY bmr.trust_score ASC, bmr.total_orders DESC""",
        (merchant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_buyer_network(buyer_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT bmr.merchant_id, bmr.trust_score, bmr.total_orders, bmr.total_spent, bmr.last_order_at
           FROM buyer_merchant_relations bmr
           WHERE bmr.buyer_id = ?
           ORDER BY bmr.total_orders DESC""",
        (buyer_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_buyer_merchant_trust(buyer_id, merchant_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM buyer_merchant_relations WHERE buyer_id = ? AND merchant_id = ?",
        (buyer_id, merchant_id),
    ).fetchone()
    return dict(row) if row else None


def get_merchant_rejection_rate(merchant_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected FROM orders WHERE merchant_id = ? AND status IN ('delivered', 'rejected')",
        (merchant_id,),
    ).fetchone()
    total = row["total"] if row else 0
    rejected = row["rejected"] if row else 0
    if total == 0:
        return 0.0
    return rejected / total


@_write
def delete_product(product_id, merchant_id):
    conn = get_conn()
    conn.execute("DELETE FROM codes WHERE product_id = ?", (product_id,))
    cursor = conn.execute(
        "DELETE FROM products WHERE id = ? AND merchant_id = ?",
        (product_id, merchant_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_unused_codes(product_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT code_value FROM codes WHERE product_id = ? AND used = 0 ORDER BY id",
        (product_id,),
    ).fetchall()
    return [r["code_value"] for r in rows]


@_write
def add_codes_to_product(product_id, codes_list):
    conn = get_conn()
    for code in codes_list:
        conn.execute(
            "INSERT INTO codes (product_id, code_value) VALUES (?, ?)",
            (product_id, code.strip()),
        )
    conn.commit()
    return len(codes_list)


def get_all_codes(product_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.*, o.status as order_status
           FROM codes c
           LEFT JOIN orders o ON o.id = c.order_id
           WHERE c.product_id = ?
           ORDER BY c.id""",
        (product_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@_write
def delete_code(code_id, merchant_id):
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM codes WHERE id = ? AND product_id IN (SELECT id FROM products WHERE merchant_id = ?)",
        (code_id, merchant_id),
    )
    conn.commit()
    return cursor.rowcount > 0


@_write
def update_product(product_id, merchant_id, name=None, price=None):
    conn = get_conn()
    fields = []
    vals = []
    if name:
        fields.append("name = ?")
        vals.append(name)
    if price is not None:
        fields.append("price = ?")
        vals.append(price)
    if not fields:
        return False
    vals.append(product_id)
    vals.append(merchant_id)
    conn.execute(
        f"UPDATE products SET {', '.join(fields)} WHERE id = ? AND merchant_id = ?",
        vals,
    )
    conn.commit()
    return True


def get_merchant_analytics_extended(merchant_id):
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE merchant_id = ?",
        (merchant_id,),
    ).fetchone()
    delivered = conn.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE merchant_id = ? AND status = 'delivered'",
        (merchant_id,),
    ).fetchone()
    rejected = conn.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE merchant_id = ? AND status = 'rejected'",
        (merchant_id,),
    ).fetchone()
    revenue = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM orders WHERE merchant_id = ? AND status = 'delivered'",
        (merchant_id,),
    ).fetchone()
    avg_time = conn.execute(
        """SELECT COALESCE(AVG(
            (julianday(COALESCE(confirmed_at, delivered_at)) - julianday(created_at)) * 24
        ), 0) as hours
        FROM orders WHERE merchant_id = ? AND status = 'delivered'""",
        (merchant_id,),
    ).fetchone()
    top_buyers = conn.execute(
        """SELECT buyer_id, buyer_name, COUNT(*) as cnt, SUM(amount) as spent
           FROM orders WHERE merchant_id = ? AND status = 'delivered'
           GROUP BY buyer_id ORDER BY cnt DESC LIMIT 3""",
        (merchant_id,),
    ).fetchall()
    buyer_count = conn.execute(
        "SELECT COUNT(DISTINCT buyer_id) as cnt FROM orders WHERE merchant_id = ?",
        (merchant_id,),
    ).fetchone()

    total_cnt = total["cnt"] if total else 0
    delivered_cnt = delivered["cnt"] if delivered else 0
    rejected_cnt = rejected["cnt"] if rejected else 0
    conversion = round(delivered_cnt / total_cnt * 100, 1) if total_cnt > 0 else 0
    rr = round(rejected_cnt / total_cnt * 100, 1) if total_cnt > 0 else 0

    return {
        "total_orders": total_cnt,
        "delivered": delivered_cnt,
        "rejected": rejected_cnt,
        "conversion_rate": conversion,
        "revenue": revenue["total"] if revenue else 0,
        "avg_confirm_hours": round(avg_time["hours"], 1) if avg_time else 0,
        "unique_buyers": buyer_count["cnt"] if buyer_count else 0,
        "top_buyers": [dict(r) for r in top_buyers],
        "rejection_rate": rr,
    }


def get_analytics(merchant_id=None):
    conn = get_conn()
    if merchant_id:
        rows = conn.execute(
            """SELECT status, COUNT(*) as count, COALESCE(SUM(amount), 0) as total
               FROM orders WHERE merchant_id = ? GROUP BY status""",
            (merchant_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT status, COUNT(*) as count, COALESCE(SUM(amount), 0) as total
               FROM orders GROUP BY status"""
        ).fetchall()
    return [dict(r) for r in rows]


def close_conn():
    global _conn
    if _conn:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


def backup_db():
    import shutil
    backup_dir = Path(config.DB_DIR)
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"eumenes_backup_{Path(config.SQLITE_DB_PATH).stem}.db"
    shutil.copy2(config.SQLITE_DB_PATH, backup_path)
    return str(backup_path)


@_write
def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_to_hf():
    global _last_backup_hash
    repo_id = config.HF_BACKUP_REPO
    token = config.HF_TOKEN
    if not repo_id or not token:
        return None
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
        conn = get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        db_path = Path(config.SQLITE_DB_PATH)
        current_hash = _file_hash(db_path)
        if current_hash == _last_backup_hash:
            return "unchanged"
        db_size = db_path.stat().st_size
        commit_info = api.upload_file(
            path_or_fileobj=config.SQLITE_DB_PATH,
            path_in_repo="eumenes.db",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"Backup {db_path.stem} ({db_size} bytes)",
        )
        _last_backup_hash = current_hash
        return commit_info.commit_url
    except Exception:
        return None


def restore_from_hf():
    repo_id = config.HF_BACKUP_REPO
    token = config.HF_TOKEN
    if not repo_id or not token:
        return False
    try:
        from huggingface_hub import HfApi, hf_hub_download
        api = HfApi(token=token)
        api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
        try:
            repo_info = api.repo_info(repo_id=repo_id, repo_type="dataset")
            files = [f for f in repo_info.siblings if f.rfilename == "eumenes.db"]
            if not files:
                return False
        except Exception:
            return False
        tmp_path = hf_hub_download(
            repo_id=repo_id,
            filename="eumenes.db",
            repo_type="dataset",
            token=token,
        )
        import shutil
        src = Path(tmp_path).resolve()
        dst = Path(config.SQLITE_DB_PATH).resolve()
        if src != dst:
            shutil.copy2(tmp_path, config.SQLITE_DB_PATH)
        return True
    except Exception:
        return False
