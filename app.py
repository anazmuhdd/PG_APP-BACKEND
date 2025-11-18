import os, dotenv
from flask import Flask
from models import db
from routes import bp as routes_bp
from webroutes import bp as webroutesbp
from flask_cors import CORS
dotenv.load_dotenv()
POSTGRES_URL = dotenv.get_key(os.path.join(os.path.dirname(__file__), ".env"), "POSTGRES_URL")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = POSTGRES_URL
CORS(app)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
with app.app_context():
    db.create_all()

# register blueprint
app.register_blueprint(routes_bp)
app.register_blueprint(webroutesbp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
