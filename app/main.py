from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from app import models, schemas, database
from app.worker import start_worker_thread

models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="Notification Service")

@app.on_event("startup")
def startup_event():
    start_worker_thread()

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/logs", response_model=list[schemas.NotificationLogResponse])
def get_logs(db: Session = Depends(database.get_db)):
    return db.query(models.NotificationLog).order_by(models.NotificationLog.sent_at.desc()).all()
