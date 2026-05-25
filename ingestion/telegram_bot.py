"""
telegram.py

Telegram bot listener for Dubai real estate matcher system.

Features:
- Reads TELEGRAM_BOT_TOKEN from .env
- Accepts text messages
- Accepts images/photos
- Sends data to existing FastAPI API
- Prints match results in console
- Keeps ngrok/API running separately
"""

import os
import tempfile
import requests

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────────────────────
# Load Environment Variables
# ─────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

API_KEY = os.getenv("API_KEY")

HEADERS = {
    "X-API-Key": API_KEY
}

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def print_matches(matches: list):
    """
    Pretty print matches to console.
    """

    if not matches:
        print("[Telegram] No matches found.")
        return

    print(f"\n[Telegram] {len(matches)} MATCH(ES) FOUND\n")

    for i, match in enumerate(matches, start=1):

        print("=" * 60)
        print(f"MATCH #{i}")
        print("=" * 60)

        print(f"Score: {match.get('score')}")
        print()

        print("Reasons:")
        for r in match.get("reasons", []):
            print(f" - {r}")

        print()

        buy = match.get("buy_snapshot", {})
        sell = match.get("sell_snapshot", {})

        print("BUY:")
        print(buy)

        print("\nSELL:")
        print(sell)

        print("\n")


# ─────────────────────────────────────────────────────────────
# Text Messages
# ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        message = update.message.text

        print("\n[Telegram] Text message received:")
        print(message)

        response = requests.post(
            f"{API_BASE_URL}/ingest/text",
            json={"message": message},
            headers=HEADERS,
            timeout=120,
        )

        data = response.json()

        print("\n[Telegram] API Response:")
        print(data)

        print_matches(data.get("matches", []))

    except Exception as e:
        print(f"[Telegram] Text handler error: {e}")


# ─────────────────────────────────────────────────────────────
# Photo Messages
# ─────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    temp_path = None

    try:

        photo = update.message.photo[-1]

        file = await context.bot.get_file(photo.file_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:

            temp_path = tmp.name

            await file.download_to_drive(temp_path)

        print(f"\n[Telegram] Image received: {temp_path}")

        with open(temp_path, "rb") as f:

            response = requests.post(
                f"{API_BASE_URL}/ingest/image",
                files={"file": f},
                headers=HEADERS,
                timeout=180,
            )

        data = response.json()

        print("\n[Telegram] API Response:")
        print(data)

        print_matches(data.get("matches", []))

    except Exception as e:
        print(f"[Telegram] Photo handler error: {e}")

    finally:

        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():

    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN missing in .env")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    print("[Telegram] Bot started...")

    app.run_polling()


if __name__ == "__main__":
    main()