from pydantic import BaseModel
from datetime import datetime

class NotificationLogResponse(BaseModel):
    id: int
    user_id: int
    habit_name: str
    message: str
    sent_at: datetime
    success: bool
    class Config:
        from_attributes = True
