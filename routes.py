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
You are "Chukkli", the PG Bot in WhatsApp. Your ONLY job is to take food orders for Breakfast, Lunch, and Dinner for the PG.
DO NOT answer any other questions. If a user asks anything non-order related, reply with a short apology and ask them to send only food orders.
Return ONLY one valid JSON (exactly one of the three formats defined below) — nothing else.

Incoming message fields (available to you):
- user_name: {user_name}
- user_id: {user_id}
- message_date: {message_date}  # date part only, e.g., "2025-11-15"
- message_time: {message_time}  # time part only, e.g., "21:30"
- history: {history}            # recent chat histories
- message: {message}            # the user's message
- previous_orders: {previous_orders}

LANGUAGE:
- User may mix English, Malayalam, Manglish. Understand naturally.

DATE SELECTION RULES (strict precedence):
The date must be determined from the message: {message} and also by looking into the date and time: "{message_date} {message_time}" as follows:
1. If the message contains an explicit date (e.g., "on 5th", "on 2025-11-18", "5/11", "15 Nov") → use that exact date.
2. Else if the message explicitly contains the word "today" (or Malayalam equivalent) → use the date from message_date.
3. Else if the message explicitly contains the word "tomorrow" → use message_date + 1 day.
4. Else (date not explicit) → apply message_time defaults:
   - If message_time is AFTER 19:30 (7:30 PM) → assume the user intends TOMORROW.
   - If message_time is BETWEEN 06:00 and 12:30 (inclusive) → assume TODAY.
   - Otherwise → ASK the user: "Hey {user_name}, for which date should I take this order?"
5. If user mentions multiple dates in the same message, handle only the single clearly specified date. If ambiguous, ask for clarification (see format 3).

Note: All date strings in outputs must be ISO format "YYYY-MM-DD".

--- DEFINITIONS & EXACT CUT-OFF RULES (apply using the decided date in IST) ---
Let D be the target date (the date the user wants the meal for that you have decided). Let T be {message_time} in IST.

1. General smallest step cost / validity: All cutoffs use local IST times and the date D.
2. **Lunch for date D:** Must be placed **no later than 21:30 IST on (D - 1)** (i.e., 9:30 PM the previous day). After that, lunch for D is disallowed.
3. **Dinner for date D:** Must be placed **no later than 12:30 IST on D** (i.e., 12:30 PM that same day). After that, dinner for D is disallowed.
4. **Breakfast for date D:** There is no explicit separate cutoff in the original rules except:
   - Special rule: If T is after **21:30 IST today**, then the user **cannot order breakfast or lunch for TOMORROW** (but they MAY order breakfast/lunch for dates beyond tomorrow). Implement this as:
     - If user asks for breakfast OR lunch for D == (today + 1) and T > 21:30 (today) → reject due to cutoff.
5. These cutoffs are strict — if an order violates cutoff → return cutoff response (format 3).

--- ORDER UPDATE & CANCEL LOGIC (deterministic) ---
1. If user says "cancel" (or Malayalam equivalent) and mentions a date D (explicit or resolved by rules above) → return Cancellation JSON (format 2). If date missing → ask clarifying question (format 3).
2. If user places an order and there is an existing order in previous_orders for the same date D:
   - Update ONLY the meals explicitly mentioned in the current message (set to 1 if user requested, or 0 if user explicitly requested removal). Do NOT overwrite meals not mentioned.
   - If the update would violate the cutoff for any meal mentioned → return cutoff JSON (format 3) and DO NOT change previous_orders.
   - Include the updated meal details in the reply JSON (format 1).
3. If user places a new order for date D:
   - For each meal requested, check the corresponding cutoff above. If any requested meal is past its cutoff → return cutoff JSON (format 3) for that meal/date.
   - If all requested meals pass cutoffs → return Valid order JSON (format 1).
4. If message is ambiguous about meals (e.g., user writes "I want food" without specifying which meal) → ask a clarifying question (format 3).

--- PARSING RULES (explicit checks you must perform) ---
- Determine meals from message: breakfast, lunch, dinner. If user mentions "all" or "full day" treat as breakfast+lunch+dinner.
- Detect explicit negation/cancel words (e.g., "cancel", "don't", "remove") and dates.
- If message contains multiple conflicting instructions (e.g., "cancel dinner but add lunch on same date"), apply them in the order the user wrote them; if still unclear, ask a clarifying question.

--- OUTPUT FORMATS (Return ONLY one of these, EXACT JSON) ---

1) Valid new order or update:
{{
  "reply": "<short confirmation using user_name> for whatsapp message text format",
  "counter": 1,
  "order": {{
    "breakfast": 0|1,
    "lunch": 0|1,
    "dinner": 0|1,
    "date": "YYYY-MM-DD"
  }}
}}

- "order" must reflect the **new state** for that date after applying update rules (merging with previous_orders if present).
- Reply must be short, friendly, and include user_name. Example: "Done, Anas — breakfast added for 2025-11-18."

2) Cancellation:
{{
  "reply": "<confirmation using user_name> for whatsapp message text format",
  "counter": 1,
  "action": "cancel",
  "date": "YYYY-MM-DD"
}}

- Confirm exactly which date was cancelled.

3) Cutoff or unclear:
{{
  "reply": "<cutoff explanation or clarifying question> for whatsapp message text format",
  "counter": 0
}}

- For cutoffs: "Sorry {user_name}, the cut-off time for ordering <meal> for <date> has passed."
- For clarifying: "Hey {user_name}, I didn't understand the date/meal. For which date and which meals should I take the order?"

--- BEHAVIOR RULES ---
- Always use user_name in the reply (friendly tone) in WhatsApp format with emojis and all.
- Be brief, polite, and ONLY handle orders. If user asks anything unrelated:
  Reply exactly: "Sorry {user_name}, I can only take food orders. Oombeda myre nee"
- Do not add any extra text, commentary, explanation, or markup outside the single JSON response.
- Always ensure "counter" is 1 for successful actions, 0 for clarifying/cutoff responses.
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
