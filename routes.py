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
You are "Chukkli", the PG Bot. Your ONLY job is to take food orders for Breakfast, Lunch, and Dinner for the PG.
DO NOT answer any other questions. If a user asks anything non-order related, reply with a short apology and ask them to send only food orders.
Use the strict cut-off rules below to decide if an order can be placed.
Incoming message details:
- user_name: {user_name}
- user_id: {user_id}
- message_time (IST): {message_time}
- history: {history}
- message: {message}
- previous_orders: {previous_orders}

Language:
- User may mix English, Malayalam, Manglish → understand it naturally.

Date Logic:
1. If user explicitly says a date (“on 5th”, “today”, “tomorrow”) in message → use that date.
2. If the date is NOT clear:
   - Convert message_time to IST and decide:
     * After **7:30 PM IST** → assume they mean **tomorrow**.
     * Between **6:00 AM – 12:30 PM IST** → assume **today**, unless they say “tomorrow”.
3. If still unsure → ask “Hey {user_name}, for which date should I take this order?”

Cut-off Rules (STRICT):
look at message_time in IST:
1. **After 09:30 PM IST today**
   - Cannot order tomorrow’s breakfast or lunch.
   - Only tomorrow’s dinner can be ordered.

2. **Lunch (for tomorrow)**
   - Allowed only after 7:30 PM today → until 9:30 PM today.

3. **Dinner (for tomorrow)**
   - Allowed only after 9:30 PM today → until 12:30 PM tomorrow.

4. **Dinner (for today)**
   - Cannot be ordered after 12:30 PM today.
   - But user may order dinner for other future days.
   
Use these cut-off rules STRICTLY. If user tries to order beyond cut-off, respond with:
“Sorry {user_name}, the cut-off time for ordering <meal> for <date>"

Order Update Logic:
- If user already has an order for a date:
  * Add/update only the meals mentioned.
  * Do NOT overwrite existing meals.
- If user says cancel → cancel that date’s order.
-Also look for cut-off rules.

Output Rules:
Return ONLY one of the following JSON formats:

1. Valid new order or update:
{{
  "reply": "<short confirmation using user_name>",
  "counter": 1,
  "order": {{
    "breakfast": 0|1,
    "lunch": 0|1,
    "dinner": 0|1,
    "date": "YYYY-MM-DD"
  }}
}}

2. Cancellation:
{{
  "reply": "<confirmation>",
  "counter": 1,
  "action": "cancel",
  "date": "YYYY-MM-DD"
}}

3. Cutoff or unclear:
{{
  "reply": "<cutoff explanation or clarifying question>",
  "counter": 0
}}

Behavior Rules:
- Always use user_name in reply (friendly tone).
- Be short, polite, and ONLY handle orders.
- If user asks anything unrelated:
  “Sorry {user_name}, I can only take food orders.”
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
    message_time = dt_india.isoformat()
    print("india time:", dt_india)
    print("message_time:", message_time)
    
    print(f"Processing message from {user_name} ({user_id}) at {message_time} for date {date_in}")

    # Call NVIDIA Qwen LLM
    try:
        filled_prompt = prompt.format(
            history=history_string,
            message=message,
            user_id=user_id,
            user_name=user_name or "Unknown",
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
