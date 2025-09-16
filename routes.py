import json, re, os, dotenv
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
from openai import OpenAI  # NVIDIA uses OpenAI-compatible API

# --- Load NVIDIA API key ---
dotenv.load_dotenv()
NVIDIA_API_KEY = dotenv.get_key(os.path.join(os.path.dirname(__file__), ".env"), "nvidia_api_key")

# --- LLM Setup ---
client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_API_KEY,
)

template = """
You are "PG Bot", handling food orders for a PG group on WhatsApp.

Context:
- Messages arrive from a WhatsApp Baileys connection with a timestamp (message_time).
- A daily reminder trigger runs at 9:00 PM asking for next-day orders.
- Meals available: breakfast, lunch, dinner.
- Users may place new orders, update existing ones, or cancel.
- Orders can be in English, Malayalam, or mixed language.
- Check the langauge as it will be in manglish fromat, so undertand it respond accurately
- You also receive the last 2 previous orders of this user (`previous_orders`).
- Use these previous orders to make smarter decisions:
   * If the user already has an order for a date and only wants to add a meal, just update that date instead of overwriting.
   * If the user tries to order again for tomorrow but already has lunch and dinner, only add missing meals like breakfast.
   * If nothing exists for tomorrow, create a new order normally.
Every reply must include the sender’s name (user_name) in a friendly tone inthe format for whatsapp text format too. For example:

"OK {user_name}, your order has been confirmed"

"Got it {user_name}, I’ve updated your dinner order"

"Hey {user_name}, do you mean today or tomorrow?"

Date & Time Rules (very important):
1. If user explicitly specifies a date (like "on Sep 8" or "for today"), use that date.
2. If no date is specified:
   - then check the time right now in India and the message_time that you will be recieving and think precisely and then decide the order.
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
- If no order for a particular day is recieved, mark all meals as false and , return JSON like:
  {{
    "reply": "<short confirmation>",
    "counter": 1,
    "order": {{"breakfast": 0, "lunch": 0, "dinner": 0, "date": "YYYY-MM-DD"}}
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
- previous_orders: {previous_orders}  # last 2 orders with meals and dates

New message from {user_name} ({user_id}) at {message_time}:
{message}
"""

prompt = ChatPromptTemplate.from_template(template)

bp = Blueprint("routes", __name__)

# --- Routes ---
@bp.route("/")
def home():
    return "PG backend running with Qwen 🎉"

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
    # Fetch last 2 orders for this user (most recent first)
    previous_orders = []
    if user_id:
        u = User.query.filter_by(whatsapp_id=user_id).first()
        if u:
            recent_orders = (
                Order.query.filter_by(user_id=u.id)
                .order_by(Order.order_date.desc())
                .limit(2)
                .all()
            )
            previous_orders = [
                {
                    "date": o.order_date.isoformat(),
                    "breakfast": o.breakfast,
                    "lunch": o.lunch,
                    "dinner": o.dinner
                }
                for o in recent_orders
            ]
    if not message or not user_id:
        return jsonify({"error": "Missing message or user_id"}), 400

    if not date_in:
        date_in = (datetime.utcnow().date() + timedelta(days=1)).isoformat()

    key = f"{user_id}_{date_in}"
    history, _ = chat_histories.get(key, ([], datetime.utcnow()))
    history.append(f"User: {message}")
    chat_histories[key] = (history, datetime.utcnow())
    history_string = "\n".join(history)
    message_time = datetime.utcnow().isoformat()

    print(f"Processing message from {user_name} ({user_id}) at {message_time} for date {date_in}")

    # Call NVIDIA Qwen LLM
    try:
        filled_prompt = prompt.format(
            history=history_string,
            message=message,
            user_id=user_id,
            user_name=user_name or "Unknown",
            date=date_in,
            message_time=message_time,
            previous_orders=json.dumps(previous_orders)
        )

        completion = client.chat.completions.create(
            model="qwen/qwen3-coder-480b-a35b-instruct",
            messages=[{"role": "user", "content": filled_prompt}],
            temperature=0.2,
            top_p=0.7,
            max_tokens=512,
        )
        result = completion.choices[0].message.content
    except Exception:
        app.logger.exception("LLM invocation failed")
        return jsonify({"reply": "Sorry, LLM error", "counter": 0}), 500

    # Clean LLM output
    clean = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
    cleaned_json_str = re.sub(r"^```json\s*|\s*```$", "", clean, flags=re.DOTALL).strip()

    print("LLM Output (cleaned):\n", cleaned_json_str)

    parsed, reply, counter, order_obj, action = None, None, 0, None, None
    try:
        parsed = json.loads(cleaned_json_str)
        print("Parsed JSON:\n", parsed)
    except json.JSONDecodeError:
        reply, counter = cleaned_json_str, 0

    if parsed:
        reply = parsed.get("reply", "")
        counter = int(parsed.get("counter", 0))
        order_obj = parsed.get("order")
        action = parsed.get("action")

        if order_obj and order_obj.get("date"):
            date_in = order_obj["date"]
        elif parsed.get("date"):
            date_in = parsed["date"]

    user = get_or_create_user(user_id, user_name)
    print(f"Determined order date: {date_in}")
    print("Order object:", order_obj)
    print("Action:", action)
    print("Reply:", reply)
    print("User:", user)
    print("Counter:", counter)

    response_payload = {"reply": reply or "", "counter": counter}

    if counter == 1:
        if action == "cancel":
            cancel_date = parsed.get("date") or date_in
            canceled_order = cancel_order_by_user_date(user, cancel_date)
            if canceled_order:
                response_payload["reply"] = parsed.get(
                    "reply", f"✅ Order for {cancel_date} canceled."
                )
                response_payload["canceled_order"] = canceled_order.as_dict()
            else:
                response_payload["reply"] = parsed.get(
                    "reply", f"No active order found for {cancel_date}."
                )
        elif order_obj:
            order_obj.setdefault("date", date_in)
            order_obj["breakfast"] = int(order_obj.get("breakfast", 0))
            order_obj["lunch"] = int(order_obj.get("lunch", 0))
            order_obj["dinner"] = int(order_obj.get("dinner", 0))
            saved = upsert_order_for_user(user, order_obj)
            response_payload["reply"] = parsed.get("reply", "✅ Order recorded.")
            response_payload["order"] = saved.as_dict()

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
@bp.route("/ping", methods=["GET"])
def ping():
    return {"status": "ok", "message": "server alive"}
# --- Missing Orders for a Date ---
@bp.route("/missing_orders", methods=["GET"])
def missing_orders():
    """
    Returns list of users who have NOT placed an order for the given date.
    Example: /missing_orders?date=2025-09-15
    """
    date_s = request.args.get("date")
    if not date_s:
        date_s = (datetime.utcnow().date() + timedelta(days=1)).isoformat()

    try:
        dt = datetime.fromisoformat(date_s).date()
    except:
        return jsonify({"error": "invalid date"}), 400

    # All active users
    users = User.query.all()
    ordered_users = {
        o.user_id
        for o in Order.query.filter_by(order_date=dt, canceled=False).all()
    }

    missing = [
        {"whatsapp_id": u.whatsapp_id, "username": u.username}
        for u in users if u.id not in ordered_users
    ]

    return jsonify({
        "date": date_s,
        "missing_count": len(missing),
        "missing_users": missing
    })


# --- Detailed Orders Summary (who ordered what) ---
@bp.route("/detailed_summary", methods=["GET"])
def detailed_summary():
    """
    Returns full breakdown of orders per user for a given date.
    Example: /detailed_summary?date=2025-09-15
    """
    date_s = request.args.get("date")
    if not date_s:
        date_s = (datetime.utcnow().date() + timedelta(days=1)).isoformat()

    try:
        dt = datetime.fromisoformat(date_s).date()
    except:
        return jsonify({"error": "invalid date"}), 400

    orders = Order.query.filter_by(order_date=dt, canceled=False).all()
    order_list = []
    for o in orders:
        user = User.query.get(o.user_id)
        order_list.append({
            "username": user.username,
            "whatsapp_id": user.whatsapp_id,
            "breakfast": bool(o.breakfast),
            "lunch": bool(o.lunch),
            "dinner": bool(o.dinner)
        })

    return jsonify({
        "date": date_s,
        "orders": order_list,
        "total_orders": len(order_list)
    })


# --- Cron-friendly endpoint to combine summary + missing ---
@bp.route("/daily_report", methods=["GET"])
def daily_report():
    """
    Combined report of orders + missing users.
    Example: /daily_report?date=2025-09-15
    """
    date_s = request.args.get("date")
    if not date_s:
        date_s = (datetime.utcnow().date() + timedelta(days=1)).isoformat()

    try:
        dt = datetime.fromisoformat(date_s).date()
    except:
        return jsonify({"error": "invalid date"}), 400

    # Orders
    orders = Order.query.filter_by(order_date=dt, canceled=False).all()
    users = User.query.all()
    ordered_ids = {o.user_id for o in orders}

    order_details = []
    for o in orders:
        user = User.query.get(o.user_id)
        order_details.append({
            "username": user.username,
            "whatsapp_id": user.whatsapp_id,
            "breakfast": bool(o.breakfast),
            "lunch": bool(o.lunch),
            "dinner": bool(o.dinner),
        })

    missing_users = [
        {"username": u.username, "whatsapp_id": u.whatsapp_id}
        for u in users if u.id not in ordered_ids
    ]

    return jsonify({
        "date": date_s,
        "orders": order_details,
        "missing_users": missing_users,
        "total_orders": len(order_details),
        "missing_count": len(missing_users)
    })
