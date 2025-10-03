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
if __name__ == "__main__":
    app.run(port=5001, debug=True)
