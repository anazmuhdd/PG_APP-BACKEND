from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID
import uuid
import os
import dotenv
from datetime import datetime

app = Flask(__name__)

# Load Supabase Postgres URL from .env
DATABASE_URL = dotenv.get_key(os.path.join(os.path.dirname(__file__), '.env'), 'POSTGRES_URL')
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL not set. Please add it in Render dashboard or .env file")

# Convert psql-compatible URI to SQLAlchemy-compatible
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_size": 10,
    "max_overflow": 5
}


db = SQLAlchemy(app)

# ---------- MODELS ----------
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
    canceled = db.Column(db.Boolean, default=False)  # NEW column for cancellation
    created_at = db.Column(db.DateTime, server_default=db.func.now())


# ---------- INIT DB ----------
with app.app_context():
    db.create_all()

# ---------- ROUTES ----------
@app.route("/")
def home():
    return "✅ Supabase DB connected and tables created!"


# Add a new user
@app.route("/users", methods=["POST"])
def add_user():
    data = request.get_json()
    whatsapp_id = data.get("whatsapp_id")
    username = data.get("username")
    age = data.get("age")
    address = data.get("address")

    if not whatsapp_id or not username:
        return jsonify({"error": "whatsapp_id and username required"}), 400

    # Check if already exists
    if User.query.filter_by(whatsapp_id=whatsapp_id).first():
        return jsonify({"error": "User already exists"}), 400

    new_user = User(
        whatsapp_id=whatsapp_id,
        username=username,
        age=age,
        address=address
    )
    db.session.add(new_user)
    db.session.commit()

    return jsonify({"message": "✅ User created", "user_id": str(new_user.id)})


# Add a booking (order)
@app.route("/orders", methods=["POST"])
def add_order():
    data = request.get_json()
    whatsapp_id = data.get("whatsapp_id")  # to identify user
    order_date = data.get("order_date")  # "YYYY-MM-DD"
    breakfast = data.get("breakfast", False)
    lunch = data.get("lunch", False)
    dinner = data.get("dinner", False)

    # Find user
    user = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Calculate total
    total = (40 if breakfast else 0) + (70 if lunch else 0) + (40 if dinner else 0)

    new_order = Order(
        user_id=user.id,
        order_date=datetime.strptime(order_date, "%Y-%m-%d").date(),
        breakfast=breakfast,
        lunch=lunch,
        dinner=dinner,
        total_amount=total
    )
    db.session.add(new_order)
    db.session.commit()

    return jsonify({
        "message": "✅ Order placed",
        "order_id": str(new_order.id),
        "total_amount": total
    })


# Get all orders of a user (including canceled)
@app.route("/orders/<whatsapp_id>", methods=["GET"])
def get_orders(whatsapp_id):
    user = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not user:
        return jsonify({"error": "User not found"}), 404

    orders = Order.query.filter_by(user_id=user.id).all()
    result = []
    for o in orders:
        result.append({
            "order_id": str(o.id),
            "order_date": o.order_date.isoformat(),
            "breakfast": o.breakfast,
            "lunch": o.lunch,
            "dinner": o.dinner,
            "total_amount": o.total_amount,
            "canceled": o.canceled
        })

    return jsonify({"username": user.username, "orders": result})


# Cancel an order by ID
@app.route("/orders/cancel/<order_id>", methods=["PUT"])
def cancel_order(order_id):
    order = Order.query.get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    if order.canceled:
        return jsonify({"message": "Order already canceled"}), 400

    order.canceled = True
    db.session.commit()

    return jsonify({"message": "❌ Order canceled", "order_id": str(order.id)})


# Get only active (non-canceled) orders for a user
@app.route("/orders/active/<whatsapp_id>", methods=["GET"])
def get_active_orders(whatsapp_id):
    user = User.query.filter_by(whatsapp_id=whatsapp_id).first()
    if not user:
        return jsonify({"error": "User not found"}), 404

    orders = Order.query.filter_by(user_id=user.id, canceled=False).all()
    result = []
    for o in orders:
        result.append({
            "order_id": str(o.id),
            "order_date": o.order_date.isoformat(),
            "breakfast": o.breakfast,
            "lunch": o.lunch,
            "dinner": o.dinner,
            "total_amount": o.total_amount
        })

    return jsonify({"username": user.username, "active_orders": result})


if __name__ == "__main__":
    app.run(port=5001, debug=True)
