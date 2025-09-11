# app.py
import os
import json
import dotenv
import threading
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID
import uuid
from langchain_core.prompts import ChatPromptTemplate
from geminillm import GeminiLLM

# -------------------------
# Config & init
# -------------------------
dotenv.load_dotenv()  # loads .env in project dir

POSTGRES_URL = dotenv.get_key(os.path.join(os.path.dirname(__file__), ".env"), "POSTGRES_URL")
API_KEY = dotenv.get_key(os.path.join(os.path.dirname(__file__), ".env"), "api_key")

if not POSTGRES_URL:
    raise RuntimeError("POSTGRES_URL missing in .env")
if not API_KEY:
    raise RuntimeError("api_key missing in .env")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = POSTGRES_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------
# DB Models
# -------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    whatsapp_id = db.Column(db.String(255), unique=True, nullable=False)
    username = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=True)
    address = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    orders = db.relationship("Order", backref="user", lazy=True)


class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    order_date = db.Column(db.Date, nullable=False)
    breakfast = db.Column(db.Boolean, default=False)
    lunch = db.Column(db.Boolean, default=False)
    dinner = db.Column(db.Boolean, default=False)
    total_amount = db.Column(db.Integer, nullable=False)
    canceled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def as_dict(self):
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "order_date": self.order_date.isoformat(),
            "breakfast": self.breakfast,
            "lunch": self.lunch,
            "dinner": self.dinner,
            "total_amount": self.total_amount,
            "canceled": self.canceled,
        }


# Create tables (first run)
with app.app_context():
    db.create_all()

# -------------------------
# LLM prompt (template)
# -------------------------
template = """
You are "PG Bot", handling food orders for a PG group on WhatsApp.

Context:
- There is a WhatsApp Baileys connection sending messages to this endpoint.
- Automated trigger runs each evening to ask for next-day orders.
- Messages may be in English, Malayalam, or mixed language.
- Users may submit, update, or cancel orders. Orders are for a specific date.

Instructions:
- Determine whether the user's message is a valid order instruction.
- If valid, output JSON exactly like:
  {"reply": "<text to send back>", "counter": 1, "order": {"breakfast": 1|0, "lunch": 1|0, "dinner": 1|0, "date": "YYYY-MM-DD"}}
- If the message is NOT a valid order (needs clarification), output JSON like:
  {"reply": "<clarifying question>", "counter": 0}
- If the message explicitly cancels, you may output:
  {"reply": "<confirmation>", "counter": 1, "action":"cancel", "date":"YYYY-MM-DD"}
- Always prefer explicit date in the JSON "order.date" if user specified one. If user didn't specify, use the provided default date (tomorrow).
- Keep replies short and user-friendly.

Variables available:
- user_name: {user_name}
- user_id: {user_id}
- date: {date}
- history: {history}

New message:
{message}
"""

prompt = ChatPromptTemplate.from_template(template)
llm = GeminiLLM(api_key=API_KEY)
chain = prompt | llm

# -------------------------
# Chat history store and cleaner
# -------------------------
chat_histories = {}  # key -> (list_of_lines, timestamp)

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

# -------------------------
# Helpers
# -------------------------
MEAL_PRICES = {"breakfast": 40, "lunch": 70, "dinner": 40}

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

# -------------------------
# Flask routes
# -------------------------
@app.route("/")
def home():
    return "PG backend running"

# --- NEW direct endpoints ---
@app.route("/users", methods=["POST"])
def add_user():
    data = request.get_json() or {}
    w = data.get("whatsapp_id")
    name = data.get("username")
    if not w or not name:
        return jsonify({"error": "whatsapp_id and username required"}), 400

    if User.query.filter_by(whatsapp_id=w).first():
        return jsonify({"error": "User already exists"}), 400

    u = User(
        whatsapp_id=w,
        username=name,
        age=data.get("age"),
        address=data.get("address")
    )
    db.session.add(u)
    db.session.commit()
    return jsonify({"message": "✅ User created", "user": {"id": str(u.id), "whatsapp_id": u.whatsapp_id, "username": u.username}})


@app.route("/orders", methods=["POST"])
def add_order_direct():
    data = request.get_json() or {}
    w = data.get("whatsapp_id")
    if not w:
        return jsonify({"error": "whatsapp_id required"}), 400
    u = User.query.filter_by(whatsapp_id=w).first()
    if not u:
        return jsonify({"error": "User not found"}), 404

    try:
        date_obj = datetime.fromisoformat(data["date"]).date()
    except Exception:
        return jsonify({"error": "Invalid date"}), 400

    order_obj = {
        "date": data["date"],
        "breakfast": 1 if data.get("breakfast") else 0,
        "lunch": 1 if data.get("lunch") else 0,
        "dinner": 1 if data.get("dinner") else 0,
    }

    saved = upsert_order_for_user(u, order_obj)
    return jsonify({"message": "✅ Order recorded", "order": saved.as_dict()})


@app.route("/orders/<whatsapp_id>", methods=["GET"])
def list_orders_for_user(whatsapp_id):
    u = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not u:
        return jsonify({"error": "User not found"}), 404
    orders = [o.as_dict() for o in Order.query.filter_by(user_id=u.id).all()]
    return jsonify({"username": u.username, "orders": orders})


# --- existing LLM process + admin endpoints remain ---
@app.route("/process", methods=["POST"])
def process():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    user_id = data.get("user_id")
    user_name = data.get("user_name")
    date_in = data.get("date")

    if not message or not user_id:
        return jsonify({"error": "Missing message or user_id"}), 400

    if not date_in:
        d = datetime.utcnow().date() + timedelta(days=1)
        date_in = d.isoformat()

    key = f"{user_id}_{date_in}"
    history, _ = chat_histories.get(key, ([], datetime.utcnow()))
    history_string = "\n".join(history)
    history.append(f"User: {message}")
    chat_histories[key] = (history, datetime.utcnow())

    try:
        result = chain.invoke({
            "history": history_string,
            "message": message,
            "user_id": user_id,
            "user_name": user_name or "Unknown",
            "date": date_in
        })
    except Exception:
        app.logger.exception("LLM invocation failed")
        return jsonify({"reply": "Sorry, LLM error", "counter": 0}), 500

    clean = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()

    parsed, reply, counter, order_obj, action = None, None, 0, None, None
    try:
        parsed = json.loads(clean)
    except Exception:
        reply, counter = clean, 0

    if parsed:
        reply = parsed.get("reply", "")
        counter = int(parsed.get("counter", 0))
        order_obj = parsed.get("order")
        action = parsed.get("action")
        if order_obj and order_obj.get("date"):
            date_in = order_obj.get("date")

    user = get_or_create_user(user_id, user_name)
    response_payload = {"reply": reply or "", "counter": counter}

    if counter == 1:
        if action == "cancel":
            cancel_date = parsed.get("date") or date_in
            canceled_order = cancel_order_by_user_date(user, cancel_date)
            if canceled_order:
                response_payload["reply"] = parsed.get("reply", f"✅ Order for {cancel_date} canceled.")
                response_payload["canceled_order"] = canceled_order.as_dict()
            else:
                response_payload["reply"] = parsed.get("reply", f"No active order found for {cancel_date}.")
                response_payload["canceled_order"] = None
        elif order_obj:
            if not order_obj.get("date"):
                order_obj["date"] = date_in
            try:
                saved = upsert_order_for_user(user, order_obj)
                response_payload["reply"] = parsed.get("reply", "✅ Order recorded.")
                response_payload["order"] = saved.as_dict()
            except Exception:
                app.logger.exception("DB upsert error")
                return jsonify({"error": "DB error"}), 500
        else:
            response_payload["reply"] = parsed.get("reply", reply or "✅ Done.")
    else:
        response_payload["reply"] = reply or parsed.get("reply", "")

    hist, _ = chat_histories.get(key, ([], datetime.utcnow()))
    hist.append(f"Bot: {response_payload['reply']}")
    chat_histories[key] = (hist, datetime.utcnow())

    return jsonify(response_payload)


@app.route("/orders/cancel_by_date", methods=["POST"])
def cancel_by_date():
    data = request.get_json() or {}
    w = data.get("whatsapp_id")
    date_s = data.get("date")
    if not w or not date_s:
        return jsonify({"error": "whatsapp_id and date required"}), 400
    user = User.query.filter_by(whatsapp_id=w).first()
    if not user:
        return jsonify({"error": "User not found"}), 404
    canceled = cancel_order_by_user_date(user, date_s)
    if canceled:
        return jsonify({"message": "Canceled", "order": canceled.as_dict()})
    return jsonify({"message": "No active order to cancel"}), 404


@app.route("/summary", methods=["GET"])
def summary():
    date_s = request.args.get("date")
    if not date_s:
        date_s = (datetime.utcnow().date() + timedelta(days=1)).isoformat()
    try:
        dt = datetime.fromisoformat(date_s).date()
    except:
        return jsonify({"error": "invalid date"}), 400

    orders = Order.query.filter_by(order_date=dt, canceled=False).all()
    breakfast_count = sum(1 for o in orders if o.breakfast)
    lunch_count = sum(1 for o in orders if o.lunch)
    dinner_count = sum(1 for o in orders if o.dinner)
    total = breakfast_count*MEAL_PRICES["breakfast"] + lunch_count*MEAL_PRICES["lunch"] + dinner_count*MEAL_PRICES["dinner"]

    return jsonify({
        "date": date_s,
        "breakfast_count": breakfast_count,
        "lunch_count": lunch_count,
        "dinner_count": dinner_count,
        "total_amount": total
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
