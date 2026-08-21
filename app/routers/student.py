import sqlite3
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from app.database import get_db
from app.dependencies import require_student
from app.models.schemas import (
    ServicesListResponse, 
    CounterDiscoveryResponse,
    TokenBookRequest,
    TokenBookResponse,
    ActiveTokenResponse,
    TokenHistoryListResponse
)
from app.services import student_service

router = APIRouter()

@router.get("/services", response_model=ServicesListResponse)
def get_student_services(
    current_user: dict = Depends(require_student),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Retrieves all available services along with their operational counters,
    including current queue lengths and estimated wait times.
    """
    try:
        cursor = db.cursor()
        
        # Get all services
        cursor.execute("SELECT id, name, code, description FROM services ORDER BY name ASC;")
        services = [dict(row) for row in cursor.fetchall()]
        
        # Get all counters
        cursor.execute("SELECT id, service_id, name, status FROM counters;")
        counters = [dict(row) for row in cursor.fetchall()]
        
        # Fetch queue sizes for counters
        cursor.execute("""
            SELECT counter_id, COUNT(*) as count 
            FROM tokens 
            WHERE status IN ('WAITING', 'HELD') AND counter_id IS NOT NULL
            GROUP BY counter_id;
        """)
        queue_sizes = {row["counter_id"]: row["count"] for row in cursor.fetchall()}
        
        # Embed counters inside their respective parent services
        services_with_counters = []
        for service in services:
            service_id = service["id"]
            service_counters = []
            
            for counter in counters:
                if counter["service_id"] == service_id:
                    q_size = queue_sizes.get(counter["id"], 0)
                    service_counters.append({
                        "id": counter["id"],
                        "service_id": counter["service_id"],
                        "name": counter["name"],
                        "status": counter["status"],
                        "queue_size": q_size,
                        "estimated_wait_time": q_size * 5
                    })
            
            services_with_counters.append({
                **service,
                "counters": service_counters
            })
            
        return {"services": services_with_counters}
        
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )

@router.get("/counters", response_model=List[CounterDiscoveryResponse])
def get_student_counters(
    current_user: dict = Depends(require_student),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Retrieves a list of all counters mapped to their parent service definitions.
    """
    try:
        cursor = db.cursor()
        cursor.execute("""
            SELECT c.id, c.name, c.service_id, c.status, s.name as service_name, s.code as service_code
            FROM counters c
            JOIN services s ON c.service_id = s.id
            ORDER BY c.name ASC;
        """)
        counters = [dict(row) for row in cursor.fetchall()]
        return counters
        
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )

@router.post("/tokens/book", response_model=TokenBookResponse)
def book_new_token(
    payload: TokenBookRequest,
    db: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_student)
):
    """
    Creates a new queue token atomically for the authenticated student.
    """
    token = student_service.book_token(
        db,
        user_id=current_user["id"],
        user_name=current_user["name"],
        user_email=current_user["email"],
        service_id=payload.service_id,
        counter_id=payload.counter_id,
        priority=payload.priority or "NORMAL"
    )
    
    # Emit socket update after database commit
    from app.services import socket_service
    socket_service.emit_queue_updated(
        service_id=token["service_id"],
        payload={
            "action": "CREATE",
            "tokenId": token["id"],
            "tokenNumber": token["token_number"],
            "counterId": token.get("counter_id")
        }
    )
    
    return {"token": token}

@router.get("/tokens/active", response_model=ActiveTokenResponse)
def get_active_token(
    db: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_student)
):
    """
    Retrieves the current active token for the student.
    """
    token = student_service.get_active_token(db, user_id=current_user["id"])
    return {"token": token}

@router.get("/tokens/history", response_model=TokenHistoryListResponse)
def get_token_history(
    page: int = 1,
    limit: int = 10,
    status: str | None = None,
    service_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_student)
):
    """
    Retrieves past terminal tokens for the student with pagination and filtering.
    """
    return student_service.get_token_history(
        db,
        user_id=current_user["id"],
        page=page,
        limit=limit,
        status_filter=status,
        service_id=service_id,
        start_date=start_date,
        end_date=end_date
    )

@router.patch("/tokens/{token_id}/cancel")
@router.post("/tokens/{token_id}/cancel")
def cancel_active_token(
    token_id: str,
    db: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_student)
):
    """
    Cancels a student's active token. Supporting both PATCH and POST methods.
    """
    res = student_service.cancel_token(db, user_id=current_user["id"], token_id=token_id)
    
    # Emit socket update after database commit
    token = res.get("token")
    if token and token["service_id"]:
        from app.services import socket_service
        socket_service.emit_queue_updated(
            service_id=token["service_id"],
            payload={
                "action": "CANCEL",
                "tokenId": token["id"],
                "tokenNumber": token["token_number"],
                "counterId": token["counter_id"]
            }
        )
        
    return {"success": True, "message": res["message"]}

