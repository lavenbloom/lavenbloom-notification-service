import os
import time
import json
import threading
import redis
from app.database import SessionLocal
from app.models import NotificationLog

REDIS_URI = os.getenv("REDIS_URI", "redis://localhost:6379/0")

def process_queue():
    try:
        redis_client = redis.from_url(REDIS_URI)
    except Exception as e:
        print(f"Failed to connect to redis: {e}")
        return

    while True:
        try:
            item = redis_client.blpop("missed_habits_queue", timeout=0)
            if item:
                _, data_bytes = item
                data = json.loads(data_bytes)
                
                user_id = data.get("user_id")
                habit_name = data.get("habit_name")
                date = data.get("date")
                
                message = f"Reminder: You missed your habit '{habit_name}' on {date}."
                print(f"NOTIFICATION SENT TO USER {user_id}: {message}", flush=True)
                
                db = SessionLocal()
                try:
                    log_entry = NotificationLog(user_id=user_id, habit_name=habit_name, message=message, success=True)
                    db.add(log_entry)
                    db.commit()
                except Exception as e:
                    print(f"Failed to log notification: {e}")
                finally:
                    db.close()
        except Exception as e:
            print(f"Error processing queue: {e}")
            time.sleep(5)

def start_worker_thread():
    thread = threading.Thread(target=process_queue, daemon=True)
    thread.start()
