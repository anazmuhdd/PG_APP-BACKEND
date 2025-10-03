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


bp = Blueprint("webroutes", __name__)

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
# Get all users
@bp.route("/users", methods=["GET"])
def get_users():
    users = User.query.all()
    user_list = []
    for u in users:
        user_list.append({
            "id": str(u.id),
            "whatsapp_id": u.whatsapp_id,
            "username": u.username,
            "age": u.age,
            "address": u.address
        })
    return jsonify({"users": user_list})

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
    "canceled": bool(data.get("canceled", False))   
    }
    print("Direct order object:", order_obj)

    saved = upsert_order_for_user(u, order_obj)
    return jsonify({"message": "✅ Order recorded", "order": saved.as_dict()})

# List all orders for a user
@bp.route("/orders/<whatsapp_id>", methods=["GET"])
def list_orders_for_user(whatsapp_id):
    u = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not u:
        return jsonify({"error": "User not found"}), 404
    orders = [o.as_dict() for o in Order.query.filter_by(user_id=u.id).all()]
    return jsonify({"username": u.username,"whatsapp_id": u.whatsapp_id, "orders": orders})
from datetime import datetime, date, timedelta
from flask import jsonify
from sqlalchemy import and_

@bp.route("/orders/<whatsapp_id>/<month>", methods=["GET"])
def list_orders_for_user_by_month(whatsapp_id, month):
    u = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not u:
        return jsonify({"error": "User not found"}), 404

    try:
        # Expect month in format YYYY-MM
        year, month_num = map(int, month.split("-"))
        start_date = date(year, month_num, 1)
        print(start_date)
        # If it's the current month, end_date = today, else = last day of the month
        if year == date.today().year and month_num == date.today().month:
            end_date = date.today()
        else:
            # Trick: get the 1st of the next month, then subtract 1 day
            if month_num == 12:
                end_date = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = date(year, month_num + 1, 1) - timedelta(days=1)
        print(end_date)
    except ValueError:
        return jsonify({"error": "Invalid month format. Use YYYY-MM"}), 400

    # Query orders within that date range
    orders = (
        Order.query.filter_by(user_id=u.id)
        .filter(and_(Order.order_date >= start_date, Order.order_date <= end_date))
        .all()
    )

    return jsonify({
        "username": u.username,
        "whatsapp_id": u.whatsapp_id,
        "orders": [o.as_dict() for o in orders]
    })

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

    orders = Order.query.filter_by(order_date=dt).all()
    order_list = []
    for o in orders:
        user = User.query.get(o.user_id)
        order_list.append({
            "username": user.username,
            "whatsapp_id": user.whatsapp_id,
            "breakfast": bool(o.breakfast),
            "lunch": bool(o.lunch),
            "dinner": bool(o.dinner),
            "total_amount": o.total_amount,
            "canceled": o.canceled
        })

    return jsonify({
        "date": date_s,
        "orders": order_list,
        "total_orders": len(order_list)
    })


# --- Cron-friendly endpoint to combine summary + missing ---
@bp.route("/daily_report", methods=["GET"])
def daily_report():
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
