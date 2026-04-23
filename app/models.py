from sqlalchemy import Column, Integer, String, DateTime, Boolean
from datetime import datetime, timezone
from app.database import Base

class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    habit_name = Column(String, nullable=False)
    message = Column(String, nullable=False)
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    success = Column(Boolean, default=True)
