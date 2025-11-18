import json, re, os, dotenv
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, current_app as app
from models import db, User, Order
from sqlalchemy import extract
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
You are Chukkli, the "PG Bot", handling food orders for a PG group on WhatsApp.

Context:
- Messages arrive from a WhatsApp Baileys connection with a timestamp (message_date and message_time).
- A daily reminder trigger runs at 9:00 PM asking for next-day orders.
- Meals available: breakfast, lunch, dinner.
- Users may place new orders, update existing ones, or cancel.
- Orders can be in English, Malayalam, or mixed language.
- Check the langauge as it will be in manglish fromat, so undertand it respond accurately
- You also receive the last 2 previous orders of this user (`previous_orders`).
- Use these previous orders to make smarter decisions:
   * If the user already has an order for a date and only wants to add a meal, just update that meal instead of overwriting.
   * If the user tries to order again for tomorrow but already has lunch and dinner, only add missing meals like breakfast.
   * If nothing exists for tomorrow, create a new order normally.
Every reply must include the sender’s name (user_name) in a friendly tone inthe format for whatsapp text format too. For example:

"OK {user_name}, your order has been confirmed"

"Got it {user_name}, I’ve updated your dinner order"

"Hey {user_name}, do you mean today or tomorrow?"

Date & Time Rules (very important):
Use the message_time and message_date to determine the intended order date:
1. If user explicitly specifies a date (like "on Sep 8" or "for today", "tomorrow"), use that date.
2. If no date is specified:
   -Main logic is the breakfast and lunch for a day can only be allowed to order the previous day before 9:30 PM.
   -The dinner for a day can be allowed to order on the same day before 12:30 PM.
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
- message_date: {message_date}  # date when user message was received
- message_time: {message_time}  # timestamp when user message was received
- history: {history}
- previous_orders: {previous_orders}  # last 2 orders with meals and dates

New message from {user_name} ({user_id}) at {message_time}:
{message}
"""






prompt = ChatPromptTemplate.from_template(template)
bp = Blueprint("routes", __name__)
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
        date_in = "no date provided"

    key = f"{user_id}_{date_in}"
    history, _ = chat_histories.get(key, ([], datetime.utcnow()))
    history.append(f"User: {message}")
    chat_histories[key] = (history, datetime.utcnow())
    history_string = "\n".join(history)
    # Convert message_time to India time (IST, UTC+5:30)
    message_time_utc = data.get("message_time")
    if message_time_utc:
        try:
            dt_utc = datetime.fromisoformat(message_time_utc)
        except Exception:
            dt_utc = datetime.utcnow()
    else:
        dt_utc = datetime.utcnow()
    # Add 5 hours 30 minutes for IST
    dt_india = dt_utc + timedelta(hours=5, minutes=30)
    time=dt_india.time()
    date=dt_india.date()
    print("date_india:", date, "time_india:", time)
    print(f"Processing message from {user_name} ({user_id}) at {time} for date {date}:\n", message)

    # Call NVIDIA Qwen LLM
    try:
        filled_prompt = prompt.format(
            history=history_string,
            message=message,
            user_id=user_id,
            user_name=user_name or "Unknown",
            message_time=time,
            message_date=date,
            previous_orders=json.dumps(previous_orders)
        )
        completion = client.chat.completions.create(
            model="qwen/qwen3-235b-a22b",
            messages=[{"role": "user", "content": filled_prompt}],
            temperature=0.2,
            top_p=0.7,
            extra_body={"chat_template_kwargs": {"thinking":False}},
            max_tokens=512,
        )
        result = completion.choices[0].message.content
        print("LLM Output (raw):\n", result)
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
