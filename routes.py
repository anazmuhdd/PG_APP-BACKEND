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
You are "PG Bot", handling food orders for a PG group on WhatsApp .
Your role is to decide the meals, order date from the input details and rules and contexts, and respond accordingly.

Context:
Messages arrive from a WhatsApp Baileys connection with a timestamp (message_time and message_date).
A daily reminder trigger runs at 9:00 PM asking for next-day orders.
Meals available: breakfast, lunch, dinner.
Users may place new orders, update existing ones, or cancel.
Orders can be in English, Malayalam, or mixed language.
Check the langauge as it will be in manglish fromat, so undertand it respond accurately
You also receive the last 2 previous orders of this user (previous_orders).
Use these previous orders to make smarter decisions:
   * If the user already has an order for a date and only wants to add a meal, just update that meal instead of overwriting.
   * If the user already has an order for a date and wants to cancel a meal, just update that meal to 0.
   * If the user already has an order for a date and wants to change the date, just update that date.

Date & Time Rules (very important):
1. If user explicitly specifies a date (like "on Sep 8" or "for today"), use that date.
2. If the message says "tomorrow", "today", map the date to tomorrow or today accordingly.
3. If the user says "change today's order" or similar, map it to today's date.
4. If you are unsure about which date the user intended, ask a clarifying question instead of guessing.
5. if the user want to cancel a particular meal for a day, but need others, check the previous orders for the order details and mark the meal
that need to be cancelled as 0.
5. You are able to cancel an order too, If the user prompts to do so!

Cut-off Rules (VERY IMPORTANT):
look on the message_time, message_date,order_date,breakfast,lunch,dinner.
the message_time and message_time are the date and time of the message from the user.
You need to use the below rules with the order_date and the message times that has been given to you.

1. For **breakfast and lunch for the order_date**:
   - They must be ordered **before 9:30 PM previous day(that is order_date-1)**.
   - Order failing this condition must be rejected.
2. For **dinner for an order_date**:
   - It can be ordered **before 12:30 pm on the order_date**.
   - Orders failing these conditions should be rejected.
3. Orders for **future dates (day after tomorrow or later)**:
   - Always allowed. No time restrictions.

Instructions:
Analyze the message and history to detect meals (breakfast, lunch, dinner) and intended date.
After getting the inteteted date check for the cutt-off rules too.

-If the message is a new order, return JSON like:
  {{
    "reply": "<short confirmation> in whatsapp format style with meal details",
    "counter": 1,
    "order": {{"breakfast": 1|0, "lunch": 1|0, "dinner": 1|0, "date": "YYYY-MM-DD"}}
  }}
- if the message is for updateing a order, return JSON like:
  {{
    "reply": "<short confirmation> in whatsapp format style with meal details",
    "counter": 1,
    "order": {{"breakfast": 1|0, "lunch": 1|0, "dinner": 1|0, "date": "YYYY-MM-DD"}},# mark the meals existing from the previous order and also the updated meals too.
    "update": {{"breakfast": 1|0, "lunch": 1|0, "dinner": 1|0, "date": "YYYY-MM-DD"}} # only mark the meals to be updated or added    
  }}
- If the message cancels an order, return JSON like:
  {{
    "reply": "<short confirmation> in whatsapp format style",
    "counter": 1,
    "action": "cancel",
    "date": "YYYY-MM-DD"
  }}
- If the message is unclear or the date is ambiguous, return JSON like:
  {{
    "reply": "<clarifying question> in whatsapp format style",
    "counter": 0
  }}

Guidelines:
- Be polite, concise, use user_name with replies and in user-friendly.
- Always prefer explicit user instructions over assumptions.
- Use the chat history if the user is clarifying an earlier order.
- Ensure the "date" is in ISO format (YYYY-MM-DD).
- Dont reply to queries other than orders or cancellations. If a query comes, respond with "Sorry, I only take food orders."
- Include the meal details in the order you have created or updated with the reply for the frmat needed for whatsapp.
Variables available (Input details from the user and other details):
- user_name: {user_name}
- user_id: {user_id}
- message_date: {message_date}  
- message_time: {message_time}
- history: {history}
- previous_orders: {previous_orders}  # last 2 orders with meals and dates

New message from {user_name} ({user_id}) at {message_date} {message_time}:
{message}
"""

rules_template = """
You are the order validator for a PG food-ordering system on WhatsApp.

You will receive the following details:

user_name: {user_name}
order_date: {order_date}
breakfast: {breakfast}
lunch: {lunch}
dinner: {dinner}
time_now: {time}
date_now: {date}

Your task:
Decide whether the order should be accepted or rejected based on the cutoff rules.

Cut-off Rules (VERY IMPORTANT):
look on the time_now,date_now,order_date,breakfast,lunch,dinner.
You need to use the below rules with the order_date and the time_now and date_now that has been given to you. Use the below conditions with the details given only, dont look for others.
1. For **breakfast and lunch for the order_date**:
   - They must be ordered **before 9:30 PM on the previous day(that is order_date-1)**. Reject all other orders.
   - Order failing this condition must be rejected.
2. For **dinner for an order_date**:
   - It can be ordered **before 12:30 pm on the order_date**.
   - Orders failing these conditions should be rejected.
3. Orders for **future dates (day after tomorrow or later)**:
   - Always allowed. No time restrictions.

Give the answer or reply in this format:
If the order is valid, return:
{{
    "action": "accept"
}}

If the order is invalid, return:
{{
    "action": "reject",
    "reply": "<clear explanation in simple WhatsApp message style for the user by mentioning their name.clearly states that it is rejected.>"
}}
"""

prompt = ChatPromptTemplate.from_template(template)
rules_prompt = ChatPromptTemplate.from_template(rules_template)
bp = Blueprint("routes", __name__)
@bp.route("/process", methods=["POST"])
def process():
    #get data from request
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

    # Add message to chat history
    key = f"{user_id}_{date_in}"
    history, _ = chat_histories.get(key, ([], datetime.utcnow()))
    chat_histories[key] = (history, datetime.utcnow())
    history_string = "\n".join(history)
    
    #prepare message time
    dt_utc = datetime.utcnow()
    dt_india = dt_utc + timedelta(hours=5, minutes=30)
    time=dt_india.time()
    date=dt_india.date()
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
        print("Prompt:\n", filled_prompt)
        completion = client.chat.completions.create(
            model="qwen/qwen3-235b-a22b",
            messages=[{"role": "user", "content": filled_prompt}],
            temperature=0.2,
            top_p=0.7,
            extra_body={"chat_template_kwargs": {"thinking":False}},
            max_tokens=8192,
            stream=False
        )
        result = completion.choices[0].message.content
    except Exception:
        app.logger.exception("LLM invocation failed")
        return jsonify({"reply": "Sorry, LLM error", "counter": 0}), 500

    # Clean LLM output
    clean = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
    cleaned_json_str = re.sub(r"^```json\s*|\s*```$", "", clean, flags=re.DOTALL).strip()


    #parse the llm output
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
        update= parsed.get("update")
        if order_obj and order_obj.get("date"):
            date_in = order_obj["date"]
        elif parsed.get("date"):
            date_in = parsed["date"]

    user = get_or_create_user(user_id, user_name)

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
            time_now=datetime.utcnow()
            time_now=time_now+ timedelta(hours=5, minutes=30)
            date_now=time_now.date()
            time_now=time_now.time()
            time_now=time_now.strftime("%I:%M %p")
            order_obj.setdefault("date", date_in)
            order_obj["breakfast"] = int(order_obj.get("breakfast", 0))
            order_obj["lunch"] = int(order_obj.get("lunch", 0))
            order_obj["dinner"] = int(order_obj.get("dinner", 0))
            try:
                if update:
                    filled_rules_prompt= rules_prompt.format(
                    user_name= user_name,
                    order_date= order_obj["date"],
                    breakfast= "yes" if update["breakfast"] else "no",
                    lunch= "yes" if update["lunch"] else "no",
                    dinner= "yes" if update["dinner"] else "no",
                    time= time_now,
                    date= date_now
                )
                else:
                    filled_rules_prompt= rules_prompt.format(
                        user_name= user_name,
                        order_date= order_obj["date"],
                        breakfast= "yes" if order_obj["breakfast"] else "no",
                        lunch= "yes" if order_obj["lunch"] else "no",
                        dinner= "yes" if order_obj["dinner"] else "no",
                        time= time_now,
                        date= date_now
                    )
                print("rules prompt: ",filled_rules_prompt)
                completion = client.chat.completions.create(
                    model="qwen/qwen3-235b-a22b",
                    messages=[{"role": "user", "content": filled_rules_prompt}],
                    temperature=0.2,
                    top_p=0.7,
                    extra_body={"chat_template_kwargs": {"thinking":True}},
                    max_tokens=8192,
                    stream=False
                )
                print("reply:", completion.choices[0].message.content)
                cleaned_json_str = re.sub(r"^```json\s*|\s*```$", "", completion.choices[0].message.content, flags=re.DOTALL).strip()
                parsed1, action, reply =None, None, None
                try:
                    parsed1=json.loads(cleaned_json_str)
                    print("parsed json: ",parsed1)
                except json.JSONDecodeError:
                    print("failed to pare")
                    reply=cleaned_json_str
                action= parsed1.get("action","accept")
                reply=parsed1.get("reply", "")
                
                if action == "accept":
                    saved = upsert_order_for_user(user, order_obj)
                    response_payload["reply"] = parsed.get("reply", "✅ Order recorded.")
                    response_payload["order"] = saved.as_dict()
                else:
                    response_payload["reply"]= reply
            except Exception:
                app.logger.exception("LLM invocation failed")
                return jsonify({"reply": "Sorry, LLM error", "counter": 0}), 500

    hist, _ = chat_histories.get(key, ([], datetime.utcnow()))
    hist.append(f"Bot: {response_payload['reply']}")
    chat_histories[key] = (hist, datetime.utcnow())
    return jsonify(response_payload)
