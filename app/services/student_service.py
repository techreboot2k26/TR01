import os
import math
import uuid
import sqlite3
from datetime import datetime, timezone
from fastapi import HTTPException, status

def calculate_estimated_wait(people_ahead: int) -> int:
    """
    Calculates estimated wait time in minutes.
    """
    return people_ahead * 5

def book_token(
    db: sqlite3.Connection,
    user_id: str,
    user_name: str,
    user_email: str,
    service_id: str,
    counter_id: str | None = None,
    priority: str = "NORMAL"
) -> dict:
    """
    Atomically books a new queue token for a service.
    Supports intelligent multi-counter load balancing if counter_id is not specified.
    Prevents duplicate active tokens for the student.
    Runs inside a strict SQLite BEGIN IMMEDIATE transaction boundary.
    """
    cursor = db.cursor()
    try:
        # Enforce write lock immediately to prevent sequence and active booking races
        db.execute("BEGIN IMMEDIATE;")

        # 1. Verify service exists
        cursor.execute("SELECT name, code FROM services WHERE id = ?;", (service_id,))
        service = cursor.fetchone()
        if not service:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Service not found"
            )

        # 2. Verify counter if specified
        assigned_counter_id = counter_id
        if assigned_counter_id and assigned_counter_id.lower() != "auto":
            cursor.execute("SELECT status, name FROM counters WHERE id = ? AND service_id = ?;", (assigned_counter_id, service_id))
            counter = cursor.fetchone()
            if not counter:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Counter not found for this service"
                )
            
            if counter["status"] in ("CLOSED", "MAINTENANCE"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Counter is currently not accepting new tokens"
                )

        # 3. Check for any existing active token (prevents duplicate active tokens)
        cursor.execute("""
            SELECT id, token_number FROM tokens 
            WHERE student_id = ? AND status IN ('WAITING', 'SERVING', 'HELD')
            LIMIT 1;
        """, (user_id,))
        active_token = cursor.fetchone()
        if active_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You already have an active token. Complete or cancel it first."
            )

        # 4. If counter was not specified or auto, perform intelligent load balancing
        if not assigned_counter_id or assigned_counter_id.lower() == "auto":
            from app.services.queue_service import get_least_loaded_counter
            assigned_counter_id = get_least_loaded_counter(db, service_id)
            if not assigned_counter_id:
                # If no counters are open, pick first available counter or fallback
                cursor.execute("SELECT id FROM counters WHERE service_id = ? LIMIT 1;", (service_id,))
                c_row = cursor.fetchone()
                if not c_row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="No counters found for this service"
                    )
                assigned_counter_id = c_row["id"]

        # Validate priority
        allowed_priorities = ['NORMAL', 'HIGH', 'PRIORITY', 'URGENT']
        if priority not in allowed_priorities:
            priority = "NORMAL"

        # 5. Generate unique sequential token number (e.g. LP-042)
        cursor.execute("""
            SELECT token_number FROM tokens 
            WHERE service_id = ? AND token_number LIKE ?
            ORDER BY ROWID DESC LIMIT 1;
        """, (service_id, f"{service['code']}-%"))
        max_row = cursor.fetchone()
        
        next_num = 1
        if max_row and max_row["token_number"]:
            parts = max_row["token_number"].split("-")
            if len(parts) == 2 and parts[1].isdigit():
                next_num = int(parts[1]) + 1
        else:
            # Fallback to daily count
            cursor.execute("""
                SELECT COUNT(*) as count 
                FROM tokens 
                WHERE service_id = ? AND date(created_at) = date('now');
            """, (service_id,))
            next_num = cursor.fetchone()["count"] + 1

        seq_num = str(next_num).zfill(3)
        token_number = f"{service['code']}-{seq_num}"
        token_id = str(uuid.uuid4())

        # 6. Insert new token with high-precision timestamp to avoid same-second queue collisions in SQLite
        created_at_val = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            INSERT INTO tokens (id, token_number, student_id, student_name, student_email, service_id, counter_id, priority, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'WAITING', ?);
        """, (token_id, token_number, user_id, user_name, user_email, service_id, assigned_counter_id, priority, created_at_val))

        db.commit()

        # 7. Retrieve complete token details (including names) for response payload
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            LEFT JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        new_token = dict(cursor.fetchone())
        
        # Calculate real-time position details
        from app.services import queue_service
        pos_details = queue_service.get_token_position_details(db, token_id)
        if pos_details:
            new_token["people_ahead"] = pos_details["people_ahead"]
            new_token["estimated_wait_time"] = pos_details["estimated_wait_time"]
        else:
            new_token["people_ahead"] = 0
            new_token["estimated_wait_time"] = 0
        
        return new_token

    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def get_active_token(db: sqlite3.Connection, user_id: str) -> dict | None:
    """
    Retrieves the current active token (WAITING, SERVING, HELD) for a student,
    including real-time queue position and wait estimates.
    """
    cursor = db.cursor()
    try:
        # Get active token (prioritizing SERVING, then HELD, then WAITING)
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            LEFT JOIN counters c ON t.counter_id = c.id
            WHERE t.student_id = ? AND t.status IN ('WAITING', 'SERVING', 'HELD')
            ORDER BY 
              CASE t.status 
                WHEN 'SERVING' THEN 1 
                WHEN 'HELD' THEN 2 
                WHEN 'WAITING' THEN 3 
                ELSE 4 
              END ASC, 
              t.created_at DESC
            LIMIT 1;
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
            
        token = dict(row)
        
        # Calculate queue stats using unified queue engine logic
        from app.services import queue_service
        details = queue_service.get_token_position_details(db, token["id"])
        if details:
            token["people_ahead"] = details["people_ahead"]
            token["estimated_wait_time"] = details["estimated_wait_time"]
        else:
            token["people_ahead"] = 0
            token["estimated_wait_time"] = 0
            
        return token
        
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )

def get_token_history(
    db: sqlite3.Connection,
    user_id: str,
    page: int = 1,
    limit: int = 10,
    status_filter: str | None = None,
    service_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None
) -> dict:
    """
    Retrieves past completed, skipped, or cancelled tokens for a student with pagination and filtering.
    """
    cursor = db.cursor()
    try:
        conditions = ["t.student_id = ?"]
        params: list[any] = [user_id]

        if status_filter:
            conditions.append("t.status = ?")
            params.append(status_filter.upper())
        else:
            conditions.append("t.status IN ('COMPLETED', 'CANCELLED', 'SKIPPED')")

        if service_id:
            conditions.append("t.service_id = ?")
            params.append(service_id)

        if start_date:
            conditions.append("date(t.created_at) >= date(?)")
            params.append(start_date)

        if end_date:
            conditions.append("date(t.created_at) <= date(?)")
            params.append(end_date)

        where_clause = " AND ".join(conditions)

        # Count total matching records
        cursor.execute(f"SELECT COUNT(*) as total FROM tokens t WHERE {where_clause};", tuple(params))
        total = cursor.fetchone()["total"]

        # Fetch paginated tokens
        offset = max(0, (page - 1) * limit) if page > 0 else 0
        query_params = list(params) + [limit, offset]

        cursor.execute(f"""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            LEFT JOIN counters c ON t.counter_id = c.id
            WHERE {where_clause}
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?;
        """, tuple(query_params))
        
        tokens = [dict(row) for row in cursor.fetchall()]
        total_pages = math.ceil(total / limit) if limit > 0 else 1

        return {
            "tokens": tokens,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages
        }
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )

def cancel_token(db: sqlite3.Connection, user_id: str, token_id: str) -> dict:
    """
    Cancels a student's active waiting or held token.
    Enforces ownership and state transitions.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")

        # Fetch token to verify existence and check details
        cursor.execute("""
            SELECT student_id, status, counter_id, service_id, token_number
            FROM tokens 
            WHERE id = ?;
        """, (token_id,))
        token = cursor.fetchone()
        
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Token not found"
            )
            
        # Verify ownership
        if token["student_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You do not own this token"
            )
            
        # Verify state transition: cannot cancel SERVING, COMPLETED, SKIPPED, or CANCELLED
        if token["status"] not in ("WAITING", "HELD"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot cancel token with status '{token['status']}'. Must be 'WAITING' or 'HELD'."
            )
            
        # Update token state to CANCELLED
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens 
            SET status = 'CANCELLED', completed_at = ?
            WHERE id = ? AND status IN ('WAITING', 'HELD');
        """, (now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Token status changed during cancellation."
            )

        db.commit()
        
        return {
            "success": True,
            "message": "Token cancelled successfully",
            "token": {
                "id": token_id,
                "token_number": token["token_number"],
                "service_id": token["service_id"],
                "counter_id": token["counter_id"]
            }
        }
        
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database mutation error: {str(e)}"
        )
