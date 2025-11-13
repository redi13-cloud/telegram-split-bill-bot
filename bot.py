import os
import logging
import google.generativeai as genai
import io
import threading
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from flask import Flask

# --- Setup ---
# Set up logging to see errors
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask Web Server (to keep Render alive) ---
# We need to run a simple web server to respond to Render's health checks.
# This keeps our "Web Service" (on the free plan) from going to sleep.
app = Flask(__name__)

@app.route('/')
def home():
    # This is just a simple page to show that the bot is running.
    return "Hello! Your SplitBill AI Bot is alive and running."

def run_flask():
    # Get the port from the environment, default to 8080
    port = int(os.environ.get('PORT', 8080))
    # We run this on 0.0.0.0 to make it accessible in the Render container
    app.run(host='0.0.0.0', port=port)

logger.info("Flask server configured.")

# --- Load API Keys ---
# We load the keys from the environment (the host) instead of hard-coding them
# This is MUCH safer!
try:
    TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
    GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
except KeyError:
    logger.error("API keys not found! Set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY as environment variables.")
    exit()

# Configure the Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
# Use a model that supports vision
model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025')
logger.info("Gemini Model loaded")

# --- Bot Command Functions ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    welcome_message = (
        f"Hi {user.first_name}! I'm your AI-powered Split Bill Bot.\n\n"
        "Here's what I can do:\n\n"
        "1.  **Read a bill:**\n"
        "    Just send me a **photo** of your receipt and I'll find the total amount.\n\n"
        "2.  **Split a bill:**\n"
        "    Use: `/split [total_amount] [num_people]`\n"
        "    Example: `/split 120 4`\n\n"
        "3.  **Ask my AI brain anything:**\n"
        "    Use: `/gemini [your_question]`\n"
        "    Example: `/gemini What's a good tip percentage?`\n"
    )
    await update.message.reply_text(welcome_message)

async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Splits the bill based on user input."""
    try:
        # context.args is a list of the words after the command
        # e.g., /split 100 4 -> context.args = ['100', '4']
        total_amount = float(context.args[0])
        num_people = int(context.args[1])

        if num_people <= 0:
            await update.message.reply_text("Number of people must be at least 1!")
            return

        split_amount = total_amount / num_people
        
        # Format the result to 2 decimal places
        result_message = f"Total: ${total_amount:,.2f}\n"
        result_message += f"Split between {num_people} people:\n"
        result_message += f"Each person pays: ${split_amount:,.2f}"

        await update.message.reply_text(result_message)

    except (IndexError, ValueError):
        # This catches errors if the user types it wrong
        await update.message.reply_text(
            "Oops! That's not right.\n"
            "Please use the format: `/split [total] [people]`\n"
            "Example: `/split 150.75 3`"
        )
    except Exception as e:
        logger.error(f"Error in /split: {e}")
        await update.message.reply_text("Sorry, I had trouble calculating that.")

async def gemini_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the user's question to the Gemini AI."""
    # Combine all the text after /gemini into one question
    question = ' '.join(context.args)

    if not question:
        await update.message.reply_text("Please ask a question after /gemini.\n"
                                        "Example: `/gemini How to save money?`")
        return
    
    await update.message.reply_text("Asking my AI brain... 🧠")

    try:
        # Send the question to Gemini
        response = model.generate_content(question)
        
        # Send the AI's answer back to the user
        await update.message.reply_text(response.text)

    except Exception as e:
        logger.error(f"Error calling Gemini: {e}")
        await update.message.reply_text("Sorry, my AI brain is a bit foggy. Please try again.")

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles when the user sends a photo to read a bill."""
    await update.message.reply_text("Scanning your bill with AI... 📸")

    try:
        # Get the highest quality photo
        photo_file = await update.message.photo[-1].get_file()
        
        # Download the photo into memory
        file_bytes_io = io.BytesIO()
        await photo_file.download_to_memory(file_bytes_io)
        file_bytes_io.seek(0)
        
        # Open the image using Pillow
        img = Image.open(file_bytes_io)

        # Send the image to Gemini
        prompt = [
            "You are a receipt scanner. Look at this image of a receipt and find the final, total amount due. "
            "Only return the numerical value (e.g., '123.45'). If you can't find a total, say 'Error'.",
            img
        ]
        
        response = model.generate_content(prompt)
        raw_text = response.text.strip().replace('$', '').replace(',', '')

        # Try to convert the response to a number
        try:
            total = float(raw_text)
            await update.message.reply_text(
                f"I found the total: **${total:.2f}**\n\n"
                f"Now, you can use:\n`/split {total} [num_people]`",
                parse_mode='Markdown'
            )
        except ValueError:
            # The AI returned text that wasn't a number
            logger.warning(f"Gemini OCR response was not a number: {raw_text}")
            await update.message.reply_text(
                f"My AI brain said: '{response.text}'.\n\n"
                "I couldn't find a clear number. Please try again or enter the total manually with `/split`."
            )

    except Exception as e:
        logger.error(f"Error in photo_handler: {e}")
        await update.message.reply_text("Sorry, I had trouble reading that image. Please try again.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles any command that the bot doesn't recognize."""
    await update.message.reply_text("Sorry, I don't understand that command. Type /start to see what I can do!")

# --- Main Bot Setup ---

def main():
    """Start the bot."""
    
    # Start the Flask web server in a separate thread
    # The 'daemon=True' means this thread will close when the main program exits.
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server starting in a background thread.")

    # Create the Telegram Bot Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    logger.info("Bot application built")

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("split", split_command))
    application.add_handler(CommandHandler("gemini", gemini_command))

    # Add a handler for photos
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Add a handler for all other unknown commands
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Run the bot until the user presses Ctrl-C
    # This runs in the main thread
    logger.info("Starting bot polling...")
    application.run_polling()


if __name__ == '__main__':
    main()
