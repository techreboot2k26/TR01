from pydantic import BaseModel, Field
from typing import List, Optional

class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str
    created_at: str

class CounterBase(BaseModel):
    id: str
    service_id: str
    name: str
    status: str

class CounterWithWait(CounterBase):
    queue_size: int
    estimated_wait_time: int

class ServiceBase(BaseModel):
    id: str
    name: str
    code: str
    description: Optional[str] = None

class ServiceWithCounters(ServiceBase):
    counters: List[CounterWithWait]

class ServicesListResponse(BaseModel):
    services: List[ServiceWithCounters]

class CounterDiscoveryResponse(CounterBase):
    service_name: str
    service_code: str

# Token schemas for Phase 2
class TokenBookRequest(BaseModel):
    service_id: str
    counter_id: Optional[str] = None
    priority: Optional[str] = "NORMAL"

class TokenResponseDetail(BaseModel):
    id: str
    token_number: str
    student_id: Optional[str] = None
    student_name: str
    student_email: Optional[str] = None
    service_id: str
    counter_id: Optional[str] = None
    priority: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    skipped_at: Optional[str] = None
    held_at: Optional[str] = None
    notes: Optional[str] = None
    
    # Joined fields expected by frontend contracts
    service_name: Optional[str] = None
    counter_name: Optional[str] = None
    people_ahead: Optional[int] = None
    estimated_wait_time: Optional[int] = None

class TokenBookResponse(BaseModel):
    token: TokenResponseDetail

class ActiveTokenResponse(BaseModel):
    token: Optional[TokenResponseDetail] = None

class TokenHistoryListResponse(BaseModel):
    tokens: List[TokenResponseDetail]
    total: Optional[int] = None
    page: Optional[int] = None
    limit: Optional[int] = None
    total_pages: Optional[int] = None

