import threading, re, json
from datetime import datetime, timedelta
from models import db, User, Order

MEAL_PRICES = {"breakfast": 40, "lunch": 70, "dinner": 40}
chat_histories = {}  # key -> (list_of_lines, timestamp)

# --- chat history cleaner thread ---
def clear_old_histories_loop():
    while True:
        now = datetime.utcnow()
        keys_to_remove = []
        for k, (_, ts) in list(chat_histories.items()):
            if now - ts > timedelta(hours=24):
                keys_to_remove.append(k)
        for k in keys_to_remove:
            del chat_histories[k]
        threading.Event().wait(3600)

threading.Thread(target=clear_old_histories_loop, daemon=True).start()


# --- DB helpers ---
def get_or_create_user(whatsapp_id: str, username: str = None):
    user = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if user:
        if username and user.username != username:
            user.username = username
            db.session.commit()
        return user
    user = User(whatsapp_id=whatsapp_id, username=username or "Unknown")
    db.session.add(user)
    db.session.commit()
    return user


def calculate_total_from_order_obj(order_obj: dict):
    total = 0
    if order_obj.get("breakfast"):
        total += MEAL_PRICES["breakfast"]
    if order_obj.get("lunch"):
        total += MEAL_PRICES["lunch"]
    if order_obj.get("dinner"):
        total += MEAL_PRICES["dinner"]
    return total


def upsert_order_for_user(user, order_obj: dict):
    date_str = order_obj.get("date")
    if not date_str:
        raise ValueError("order_obj must include date")
    order_date = datetime.fromisoformat(date_str).date()
    existing = Order.query.filter_by(user_id=user.id, order_date=order_date).first()
    b = bool(order_obj.get("breakfast"))
    l = bool(order_obj.get("lunch"))
    d = bool(order_obj.get("dinner"))
    total = calculate_total_from_order_obj(order_obj)
    if existing:
        existing.breakfast = b
        existing.lunch = l
        existing.dinner = d
        existing.total_amount = total
        existing.canceled = False
        db.session.commit()
        return existing
    new = Order(user_id=user.id, order_date=order_date,
                breakfast=b, lunch=l, dinner=d, total_amount=total)
    db.session.add(new)
    db.session.commit()
    return new


def cancel_order_by_user_date(user, date_str):
    try:
        order_date = datetime.fromisoformat(date_str).date()
    except Exception:
        return None
    existing = Order.query.filter_by(user_id=user.id, order_date=order_date, canceled=False).first()
    if not existing:
        return None
    existing.canceled = True
    db.session.commit()
    return existing
