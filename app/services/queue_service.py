import os
import math
import sqlite3
from datetime import datetime, timezone
from fastapi import HTTPException, status

def parse_dt(dt_str: str | None) -> datetime:
    """
    Parses datetime string supporting ISO format, SQLite formats, and UTC offsets.
    """
    if not dt_str:
        return datetime.now(timezone.utc)
    s = dt_str.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            # Handle SQLite default format without timezone
            dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S.%f')
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.now(timezone.utc)

def get_average_service_time(db: sqlite3.Connection, service_id: str) -> float:
    """
    Calculates dynamic average service duration in minutes from historical COMPLETED tokens.
    """
    cursor = db.cursor()
    cursor.execute("""
        SELECT AVG((julianday(completed_at) - julianday(started_at)) * 24 * 60) as avg_mins
        FROM tokens
        WHERE service_id = ? AND status = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL;
    """, (service_id,))
    row = cursor.fetchone()
    if row and row["avg_mins"] is not None:
        return max(1.0, round(row["avg_mins"] * 10) / 10)
    return 4.5

def get_sorted_waiting_tokens(db: sqlite3.Connection, service_id: str) -> list[dict]:
    """
    Starvation-Prevention Sorting function.
    Sorts all WAITING tokens for a service by:
      1. Effective priority (Base priority + starvation boost)
      2. Original creation time (FIFO within effective priority)
      3. Token ID (stable tie-breaker)
    """
    cursor = db.cursor()
    cursor.execute("SELECT * FROM tokens WHERE service_id = ? AND status = 'WAITING';", (service_id,))
    rows = cursor.fetchall()
    if not rows:
        return []

    col_names = [col[0] for col in cursor.description]
    tokens = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            tokens.append(dict(row))
        elif isinstance(row, dict):
            tokens.append(row)
        else:
            tokens.append(dict(zip(col_names, row)))


    try:
        threshold = float(os.getenv("PRIORITY_WAIT_THRESHOLD_MINUTES", "15"))
    except ValueError:
        threshold = 15.0

    now = datetime.now(timezone.utc)

    def get_priority_val(p: str) -> int:
        if p == 'URGENT':
            return 3
        elif p in ('HIGH', 'PRIORITY'):
            return 2
        elif p == 'NORMAL':
            return 1
        return 1

    def get_effective_priority(token: dict) -> int:
        created = parse_dt(token.get("created_at"))
        elapsed_minutes = max(0.0, (now - created).total_seconds() / 60.0)
        base_val = get_priority_val(token.get("priority", "NORMAL"))
        boost = math.floor(elapsed_minutes / threshold) if threshold > 0 else 0
        return base_val + boost

    tokens_with_keys = []
    for t in tokens:
        eff_p = get_effective_priority(t)
        c_time = parse_dt(t.get("created_at")).timestamp()
        tokens_with_keys.append((eff_p, c_time, str(t["id"]), t))

    # Sort: highest effective priority first, then earliest created_at, then stable ID tie-breaker
    tokens_with_keys.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [x[3] for x in tokens_with_keys]

def get_waiting_queue(db: sqlite3.Connection, service_id: str) -> list[dict]:
    """
    Retrieves all WAITING tokens for a service, ordered by fair priority queue with starvation prevention.
    """
    return get_sorted_waiting_tokens(db, service_id)

def get_token_position_details(db: sqlite3.Connection, token_id: str) -> dict | None:
    """
    Computes a token's real-time queue position and intelligent estimated wait minutes.
    Considers open counters, active serving tokens, and starvation-aware queue order.
    Returns: {"position": int, "people_ahead": int, "estimated_wait_time": int}
    """
    cursor = db.cursor()
    cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
    row = cursor.fetchone()
    if not row:
        return None
        
    token = dict(row)
    if token["status"] == "SERVING":
        return {"position": 0, "people_ahead": 0, "estimated_wait_time": 0}
        
    if token["status"] != "WAITING":
        return {"position": -1, "people_ahead": 0, "estimated_wait_time": 0}
        
    sorted_queue = get_sorted_waiting_tokens(db, token["service_id"])
    idx = -1
    for i, t in enumerate(sorted_queue):
        if t["id"] == token_id:
            idx = i
            break
            
    if idx == -1:
        return None
        
    people_ahead = idx
    position = idx + 1
    avg_service_time = get_average_service_time(db, token["service_id"])

    cursor.execute("SELECT id FROM counters WHERE service_id = ? AND status = 'OPEN';", (token["service_id"],))
    open_counters = [dict(r) for r in cursor.fetchall()]
    num_counters = max(1, len(open_counters))

    active_remaining = 0.0
    now = datetime.now(timezone.utc)
    for c in open_counters:
        cursor.execute("SELECT started_at FROM tokens WHERE counter_id = ? AND status = 'SERVING' LIMIT 1;", (c["id"],))
        s_row = cursor.fetchone()
        if s_row and s_row["started_at"]:
            started = parse_dt(s_row["started_at"])
            elapsed = max(0.0, (now - started).total_seconds() / 60.0)
            active_remaining += max(0.0, avg_service_time - elapsed)

    wait_time = (people_ahead * avg_service_time) / num_counters + (active_remaining / num_counters)
    estimated_mins = int(round(wait_time))

    return {
        "position": position,
        "people_ahead": people_ahead,
        "estimated_wait_time": estimated_mins
    }

def get_least_loaded_counter(db: sqlite3.Connection, service_id: str) -> str | None:
    """
    Intelligent multi-counter load balancer.
    Identifies and returns the OPEN counter with the smallest active queue for a service.
    """
    cursor = db.cursor()
    cursor.execute("""
        SELECT id, name FROM counters 
        WHERE service_id = ? AND status = 'OPEN' 
        ORDER BY id ASC;
    """, (service_id,))
    open_counters = [dict(r) for r in cursor.fetchall()]
    if not open_counters:
        return None
    if len(open_counters) == 1:
        return open_counters[0]["id"]

    counter_loads = []
    for c in open_counters:
        cursor.execute("""
            SELECT COUNT(*) as count FROM tokens
            WHERE counter_id = ? AND status IN ('WAITING', 'SERVING', 'HELD');
        """, (c["id"],))
        cnt = cursor.fetchone()["count"]
        counter_loads.append((cnt, c["id"]))

    counter_loads.sort(key=lambda x: (x[0], x[1]))
    return counter_loads[0][1]

def call_next_token(db: sqlite3.Connection, counter_id: str, service_id: str) -> dict:
    """
    Enforces counter availability, active serving checks, and atomically assigns
    the next fair-priority waiting token.
    Runs inside a strict SQLite BEGIN IMMEDIATE transaction lock with concurrency safety.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        
        # 1. Verify counter exists and is OPEN
        cursor.execute("SELECT status FROM counters WHERE id = ?;", (counter_id,))
        counter = cursor.fetchone()
        if not counter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Counter not found")
        if counter["status"] != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot call next token: Counter is currently {counter['status']}"
            )
            
        # 2. Assert no token is currently SERVING at this counter
        cursor.execute("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,))
        active_serving = cursor.fetchone()
        if active_serving:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Counter already has active serving token {active_serving['token_number']}. Complete, hold, or skip it first."
            )
            
        # 3. Pull next eligible token using fair priority queue
        waiting = get_sorted_waiting_tokens(db, service_id)
        if not waiting:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Waiting queue is currently empty for this service."
            )
        
        # 4. Atomic conditional update loop to ensure safe concurrent staff operations
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        claimed_id = None
        for candidate in waiting:
            cursor.execute("""
                UPDATE tokens
                SET status = 'SERVING', counter_id = ?, started_at = ?
                WHERE id = ? AND status = 'WAITING';
            """, (counter_id, now, candidate["id"]))
            if cursor.rowcount == 1:
                claimed_id = candidate["id"]
                break

        if not claimed_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Waiting queue is currently empty for this service."
            )
        
        db.commit()
        
        # Get updated token
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (claimed_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def promote_next_token(db: sqlite3.Connection, service_id: str, counter_id: str) -> dict:
    """
    Automatic waitlist promotion with fair scheduling.
    Promotes the next eligible waiting token to SERVING at the target open counter.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        
        # 1. Verify counter status is OPEN
        cursor.execute("SELECT status FROM counters WHERE id = ?;", (counter_id,))
        counter = cursor.fetchone()
        if not counter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Counter not found")
        if counter["status"] != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Counter is not OPEN (currently {counter['status']})"
            )

        # 2. Check counter does not already have an active serving token
        cursor.execute("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,))
        active_serving = cursor.fetchone()
        if active_serving:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Counter already has an active serving token"
            )

        # 3. Pull next eligible token using fair priority queue
        waiting_queue = get_sorted_waiting_tokens(db, service_id)
        if not waiting_queue:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No eligible tokens found in waitlist"
            )

        # 4. Atomic conditional update to promote
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        promoted_id = None
        for candidate in waiting_queue:
            cursor.execute("""
                UPDATE tokens
                SET status = 'SERVING', counter_id = ?, started_at = ?
                WHERE id = ? AND status = 'WAITING';
            """, (counter_id, now, candidate["id"]))
            if cursor.rowcount == 1:
                promoted_id = candidate["id"]
                break

        if not promoted_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No eligible tokens found in waitlist"
            )

        db.commit()

        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (promoted_id,))
        return dict(cursor.fetchone())

    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def complete_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Marks a serving token as COMPLETED. Enforces valid state transitions and counter authorization.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] != "SERVING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot complete token with status '{token['status']}'. Must be 'SERVING'."
            )
            
        if token["counter_id"] != counter_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unauthorized: Token is assigned to a different counter"
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'COMPLETED', completed_at = ?
            WHERE id = ? AND status = 'SERVING';
        """, (now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Token status changed during operation."
            )
            
        db.commit()
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def hold_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Places a serving token on HELD. Enforces valid state transitions.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] != "SERVING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot hold token with status '{token['status']}'. Must be 'SERVING'."
            )
            
        if token["counter_id"] != counter_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unauthorized: Token is assigned to a different counter"
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'HELD', held_at = ?
            WHERE id = ? AND status = 'SERVING';
        """, (now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Token status changed during operation."
            )
            
        db.commit()
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def resume_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Resumes a held token back to SERVING. Enforces counter OPEN availability and serving exclusivity.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] != "HELD":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot resume token with status '{token['status']}'. Must be 'HELD'."
            )
            
        # Verify counter status is OPEN
        cursor.execute("SELECT status FROM counters WHERE id = ?;", (counter_id,))
        counter = cursor.fetchone()
        if not counter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Counter not found")
        if counter["status"] != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resume token: Counter is currently {counter['status']}"
            )

        # Assert no token is currently SERVING at this counter
        cursor.execute("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,))
        active_serving = cursor.fetchone()
        if active_serving:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resume token: Counter already has active serving token {active_serving['token_number']}."
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'SERVING', counter_id = ?, started_at = ?
            WHERE id = ? AND status = 'HELD';
        """, (counter_id, now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Token status changed during operation."
            )
            
        db.commit()
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def skip_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Skips a waiting, serving, or held token to state SKIPPED. Enforces terminal state protections.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] not in ("WAITING", "SERVING", "HELD"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot skip token with status '{token['status']}'."
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'SKIPPED', skipped_at = ?
            WHERE id = ? AND status IN ('WAITING', 'SERVING', 'HELD');
        """, (now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Token status changed during operation."
            )
            
        db.commit()
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            LEFT JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )
