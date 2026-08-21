import sqlite3
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.database import get_db
from app.auth.jwt import verify_jwt_token

security = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """
    Extracts Bearer token, verifies via local JWT validation (with mock fallback),
    resolves identity from SQLite database, and auto-syncs user profile if missing.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Authentication credentials are required"
        )
    token = credentials.credentials

    # Validate token and extract properties
    decoded = verify_jwt_token(token)
    uid = decoded.get("uid")
    email = decoded.get("email")
    name = decoded.get("name")

    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Token is missing user identification credentials"
        )

    cursor = db.cursor()
    # 1. Search SQLite by user ID
    cursor.execute("SELECT id, name, email, role, created_at FROM users WHERE id = ?", (uid,))
    user = cursor.fetchone()

    # 2. Try searching by email as fallback
    if not user and email:
        cursor.execute("SELECT id, name, email, role, created_at FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

    # 3. Auto-sync: Create the user in SQLite database if they are authenticated via JWT but not registered in database
    if not user:
        # Strictly default role to STUDENT to prevent privilege escalation vulnerabilities
        role = "STUDENT"
        display_name = name or (email.split("@")[0] if email else "User")
        
        try:
            cursor.execute(
                "INSERT INTO users (id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
                (uid, display_name, email or "", "PBKDF2_MOCK_HASH", role)
            )
            db.commit()
            
            cursor.execute("SELECT id, name, email, role, created_at FROM users WHERE id = ?", (uid,))
            user = cursor.fetchone()
        except sqlite3.Error as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database synchronization error: {str(e)}"
            )

    return dict(user)

def require_role(allowed_roles: list[str]):
    """
    Returns a dependency function that asserts the current user has one of the allowed roles.
    """
    def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Access restricted to {', '.join(allowed_roles)} users"
            )
        return current_user
    return dependency

# Reusable role dependency markers
require_student = require_role(["STUDENT"])
require_staff = require_role(["STAFF"])
require_admin = require_role(["ADMIN"])

def get_assigned_counter(
    current_user: dict = Depends(require_staff),
    db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """
    Enforces staff role and retrieves the counter assigned to the staff member.
    """
    cursor = db.cursor()
    cursor.execute("""
        SELECT c.*, s.name as service_name, s.code as service_code
        FROM counters c
        JOIN services s ON c.service_id = s.id
        WHERE c.assigned_staff_id = ?;
    """, (current_user["id"],))
    counter = cursor.fetchone()
    if not counter:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Staff member is not assigned to any active counter"
        )
    return dict(counter)

