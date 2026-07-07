from app import app

if __name__ == "__main__":
    from config import SETTINGS
    app.run(host="0.0.0.0", port=SETTINGS.port, debug=False)
