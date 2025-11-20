# PG Food Order Agent (Backend)

A lightweight **LLM-powered decision engine** for managing daily PG (paying guest) food orders. This backend acts as the **brain/agent** behind a WhatsApp automation system.

It does not handle WhatsApp sessions directly. Instead, it receives structured triggers from a Baileys-based frontend running on AWS.

---

## 🌟 Project Overview

This project represents the **AI / Agent layer** of a production PG food ordering system.

### What this backend does

- Receives WhatsApp message events from the Baileys frontend (AWS EC2)
- Uses an **LLM agent** to interpret user intent

  - New orders
  - Updates
  - Cancellations
  - Replacement meals
  - Multilingual messages (Malayalam + English / Manglish)

- Enforces **cut‑off rules** for breakfast, lunch, dinner
- Reads/writes orders from the database
- Sends structured decisions back to the Baileys worker

This backend is optimized to run on **Render**, while the WhatsApp client runs on **AWS EC2**.

---

## 🧠 System Architecture

Your system consists of two main components:

### 1. **Frontend Worker (AWS EC2)**

- Runs Baileys (WhatsApp WebSocket client)
- Listens to group messages 24/7
- Extracts user ID, timestamp, raw message
- Sends structured JSON → backend

### 2. **Backend Agent (Render)**

- Receives `/process` requests
- Uses an LLM to understand intent + meals + date
- Checks cut-off rules
- Interacts with DB (upsert, cancel, update)
- Returns the final decision

**Flow:**

```
WhatsApp → Baileys (EC2) → Backend Agent (Render) → Decision → EC2 → WhatsApp Reply
```

This backend is **not a traditional full-stack Flask app** — it is an **AI-driven agent** that exposes minimal HTTP endpoints.

---

## 🛠 Repository Layout

```
app.py          → Bootstraps Flask (API surface only)
routes.py       → /process endpoint logic
webroutes.py    → Optional admin views
models.py       → SQLAlchemy: User, Order
helpers.py      → LLM prompts, chat history, order helpers
db.py           → DB initialisation
requirements.txt→ Dependencies
```

---

## 🚀 Quickstart (Development)

### Prerequisites

- Python 3.10+
- Virtual environment recommended
- SQLAlchemy‑compatible DB (SQLite for development)
- `.env` file containing:

  ```
  nvidia_api_key=YOUR_API_KEY
  ```

### Installation

```powershell
git clone <repo-url> .
cd "c:\Users\anasm\Documents\PG_APP_BACKEND"
```

Create virtual environment:

```powershell
python -m venv myenv
& .\myenv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create `.env`:

```text
nvidia_api_key=your_api_key_here
```

### Run in development

```powershell
$env:FLASK_APP = "app.py"
$env:FLASK_ENV = "development"
flask run
```

Or:

```powershell
python app.py
```

---

## 🧪 API Usage

The frontend sends JSON to:

```
POST /process
```

Example request:

```json
{
  "user_id": "whatsapp-123",
  "user_name": "Rahul",
  "message": "I want breakfast and lunch tomorrow",
  "date": "2025-11-21"
}
```

Example response:

```json
{
  "counter": 1,
  "reply": "Order updated for 21 Nov: Breakfast + Lunch",
  "order": {
    "breakfast": 1,
    "lunch": 1,
    "dinner": 0
  }
}
```

---

## ⚙️ Configuration

- `.env` must contain the LLM API key
- Database URI configured in `models.py` or Render environment vars

---

## 🧪 Testing

```powershell
python test.py
```

Recommend upgrading to `pytest` and GitHub Actions.

---

## 🔐 Security

- Never commit `.env` or secrets
- Backend should ideally accept authenticated requests from EC2

---

## 📌 Next Steps

- Add `.env.example`
- Add `LICENSE` + `CONTRIBUTING.md`
- Add tests with mocked LLM responses
- Add admin dashboard for daily summaries

---

## 🧑‍💻 Author

- Anas Mohammed.
