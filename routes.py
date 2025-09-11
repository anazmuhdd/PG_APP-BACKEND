import json, re
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, current_app as app
from models import db, User, Order
from helpers import (
    chat_histories,
    get_or_create_user,
    upsert_order_for_user,
    cancel_order_by_user_date,
    MEAL_PRICES
)
from langchain_core.prompts import ChatPromptTemplate
from geminillm import GeminiLLM
import os, dotenv

# LLM setup
dotenv.load_dotenv()
API_KEY = dotenv.get_key(os.path.join(os.path.dirname(__file__), ".env"), "api_key")
llm = GeminiLLM(api_key=API_KEY)

template = """
You are "PG Bot", handling food orders for a PG group on WhatsApp.

Context:
- Messages arrive from a WhatsApp Baileys connection with a timestamp (message_time).
- A daily reminder trigger runs at 9:00 PM asking for next-day orders.
- Meals available: breakfast, lunch, dinner.
- Users may place new orders, update existing ones, or cancel.
- Orders can be in English, Malayalam, or mixed language.

Date & Time Rules (very important):
1. If user explicitly specifies a date (like "on Sep 8" or "for today"), use that date.
2. If no date is specified:
   - then check the time right now and the message_time that you will be recieving and think precisely and then decide the order.
   -if you are unsure with it ask it as a query again as yu are holding chat history.
3. If the message says "tomorrow", map it to the next calendar date after message_time.
4. If the user says "change today's order" or similar, map it to today's date.
5. If you are unsure about which date the user intended, ask a clarifying question instead of guessing.
6. You are able to cancel an order too, If the user prompts to do so!

Instructions:
- Analyze the message and history to detect meals (breakfast, lunch, dinner) and intended date.
- If the message is a valid order (new or update), return JSON like:
  {{
    "reply": "<short confirmation>",
    "counter": 1,
    "order": {{"breakfast": 1|0, "lunch": 1|0, "dinner": 1|0, "date": "YYYY-MM-DD"}}
  }}
- If the message cancels an order, return JSON like:
  {{
    "reply": "<short confirmation>",
    "counter": 1,
    "action": "cancel",
    "date": "YYYY-MM-DD"
  }}
- If the message is unclear or the date is ambiguous, return JSON like:
  {{
    "reply": "<clarifying question>",
    "counter": 0
  }}

Guidelines:
- Be polite, concise, and user-friendly.
- Always prefer explicit user instructions over assumptions.
- Use the chat history if the user is clarifying an earlier order.
- Ensure the "date" is in ISO format (YYYY-MM-DD).

Variables available:
- user_name: {user_name}
- user_id: {user_id}
- date: {date}  # default candidate date (from server-side logic, e.g. tomorrow)
- message_time: {message_time}  # timestamp when user message was received
- history: {history}

New message from {user_name} ({user_id}) at {message_time}:
{message}
"""


prompt = ChatPromptTemplate.from_template(template)
chain = prompt | llm

bp = Blueprint("routes", __name__)

# --- Routes ---
@bp.route("/")
def home():
    return "PG backend running"

# Add a new user
@bp.route("/users", methods=["POST"])
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

# Direct order creation
@bp.route("/orders", methods=["POST"])
def add_order_direct():
    data = request.get_json() or {}
    w = data.get("whatsapp_id")
    if not w:
        return jsonify({"error": "whatsapp_id required"}), 400
    u = User.query.filter_by(whatsapp_id=w).first()
    if not u:
        return jsonify({"error": "User not found"}), 404

    try:
        datetime.fromisoformat(data["date"]).date()
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

# List all orders for a user
@bp.route("/orders/<whatsapp_id>", methods=["GET"])
def list_orders_for_user(whatsapp_id):
    u = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not u:
        return jsonify({"error": "User not found"}), 404
    orders = [o.as_dict() for o in Order.query.filter_by(user_id=u.id).all()]
    return jsonify({"username": u.username, "orders": orders})
# --- Main process route ---
@bp.route("/process", methods=["POST"])
def process():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    user_id = data.get("user_id")
    user_name = data.get("user_name")
    date_in = data.get("date")

    if not message or not user_id:
        return jsonify({"error": "Missing message or user_id"}), 400

    # Default to tomorrow if no date provided
    if not date_in:
        date_in = (datetime.utcnow().date() + timedelta(days=1)).isoformat()

    key = f"{user_id}_{date_in}"
    history, _ = chat_histories.get(key, ([], datetime.utcnow()))
    history.append(f"User: {message}")
    chat_histories[key] = (history, datetime.utcnow())
    history_string = "\n".join(history)
    message_time = datetime.utcnow().isoformat()

    print(f"Processing message from {user_name} ({user_id}) at {message_time} for date {date_in}")

    # Call LLM
    try:
        result = chain.invoke({
            "history": history_string,
            "message": message,
            "user_id": user_id,
            "user_name": user_name or "Unknown",
            "date": date_in,
            "message_time": message_time
        })
    except Exception:
        app.logger.exception("LLM invocation failed")
        return jsonify({"reply": "Sorry, LLM error", "counter": 0}), 500

    # Clean LLM output: remove <think> tags
    clean = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()

    # Strip ```json and ``` if present
    cleaned_json_str = re.sub(r"^```json\s*|\s*```$", "", clean, flags=re.DOTALL).strip()

    print("LLM Output (cleaned):\n", cleaned_json_str)

    parsed, reply, counter, order_obj, action = None, None, 0, None, None
    try:
        parsed = json.loads(cleaned_json_str)
        print("Parsed JSON:\n", parsed)
    except json.JSONDecodeError:
        # fallback if JSON parsing fails
        reply, counter = cleaned_json_str, 0

    if parsed:
        reply = parsed.get("reply", "")
        counter = int(parsed.get("counter", 0))
        order_obj = parsed.get("order")
        action = parsed.get("action")

        # Trust LLM's date if provided
        if order_obj and order_obj.get("date"):
            date_in = order_obj["date"]
        elif parsed.get("date"):
            date_in = parsed["date"]

    # Get or create user
    user = get_or_create_user(user_id, user_name)
    print(f"Determined order date: {date_in}")
    print("Order object:", order_obj)
    print("Action:", action)
    print("Reply:", reply)
    print("User:", user)
    print("Counter:", counter)

    response_payload = {"reply": reply or "", "counter": counter}

    # Handle orders / cancel
    if counter == 1:
        if action == "cancel":
            cancel_date = parsed.get("date") or date_in
            canceled_order = cancel_order_by_user_date(user, cancel_date)
            if canceled_order:
                response_payload["reply"] = parsed.get(
                    "reply", f"✅ Order for {cancel_date} canceled."
                )
                response_payload["canceled_order"] = canceled_order.as_dict()
                print("Canceled order:", canceled_order)
            else:
                response_payload["reply"] = parsed.get(
                    "reply", f"No active order found for {cancel_date}."
                )
        elif order_obj:
            # Normalize order
            order_obj.setdefault("date", date_in)
            order_obj["breakfast"] = int(order_obj.get("breakfast", 0))
            order_obj["lunch"] = int(order_obj.get("lunch", 0))
            order_obj["dinner"] = int(order_obj.get("dinner", 0))
            saved = upsert_order_for_user(user, order_obj)
            print("Saved order:", saved)
            response_payload["reply"] = parsed.get("reply", "✅ Order recorded.")
            response_payload["order"] = saved.as_dict()

    # Update chat history
    hist, _ = chat_histories.get(key, ([], datetime.utcnow()))
    hist.append(f"Bot: {response_payload['reply']}")
    chat_histories[key] = (hist, datetime.utcnow())

    print("Response:\n", response_payload)
    return jsonify(response_payload)

# Cancel order by date
@bp.route("/orders/cancel_by_date", methods=["POST"])
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

# Summary for a date
@bp.route("/summary", methods=["GET"])
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
    total = (breakfast_count*MEAL_PRICES["breakfast"] +
             lunch_count*MEAL_PRICES["lunch"] +
             dinner_count*MEAL_PRICES["dinner"])

    return jsonify({
        "date": date_s,
        "breakfast_count": breakfast_count,
        "lunch_count": lunch_count,
        "dinner_count": dinner_count,
        "total_amount": total
    })