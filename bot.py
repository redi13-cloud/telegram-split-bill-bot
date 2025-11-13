import os
import logging
import google.generativai as genai
import io
import json
from PIL import Image
from telegram import Update, Bot
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    ContextTypes, 
    filters,
    ConversationHandler,
    ApplicationBuilder
)
from flask import Flask, request as flask_request, Response

# --- Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Load API Keys ---
# Vercel uses Environment Variables.
try:
    TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
    GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
except KeyError:
    logger.error("API keys not found! Set them in Vercel 'Environment Variables'.")
    # Don't exit, as Vercel needs the app to be defined.
    # We will just log the error.
    TELEGRAM_BOT_TOKEN = None
    GEMINI_API_KEY = None

# --- Bot & AI Setup ---
if TELEGRAM_BOT_TOKEN and GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025')
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    application = ApplicationBuilder().bot(bot).build()
    logger.info("Gemini Model loaded and Bot Application built")
else:
    logger.error("Bot cannot start due to missing API keys.")
    # We create a dummy 'app' so Vercel can at least build.
    app = Flask(__name__)
    @app.route('/')
    def error_home():
        return "Bot is OFFLINE. Missing API keys in Vercel Environment Variables.", 500


# --- Conversation States ---
RECEIVE_PHOTO, RECEIVE_ASSIGNMENTS = range(2)

# --- Bot Command Functions (Same as before) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    welcome_message = (
        f"Hi {user.first_name}! I'm your AI-powered Split Bill Bot.\n\n"
        "**Here's the new, powerful way to split a bill:**\n\n"
        "1.  **Send me a photo** of your itemized receipt.\n"
        "2.  I'll read all the items, tax, and service charge.\n"
        "3.  I'll ask you who ate what.\n"
        "4.  You reply with who had which items (e.g., `Alice: Burger. Bob: Salad, Fries`)\n"
        "5.  I'll calculate the *exact* amount each person owes, including their share of tax & service!\n\n"
        "**Other commands:**\n"
        "*/split [total] [people]* - Quick manual split.\n"
        "*/gemini [question]* - Ask my AI brain anything.\n"
        "*/cancel* - Cancel the current bill splitting conversation."
    )
    await update.message.reply_text(welcome_message)

async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Splits the bill based on user input (the simple, manual way)."""
    try:
        total_amount = float(context.args[0])
        num_people = int(context.args[1])

        if num_people <= 0:
            await update.message.reply_text("Number of people must be at least 1!")
            return

        split_amount = total_amount / num_people
        result_message = f"Total: ${total_amount:,.2f}\n"
        result_message += f"Split between {num_people} people:\n"
        result_message += f"Each person pays: ${split_amount:,.2f}"
        await update.message.reply_text(result_message)

    except (IndexError, ValueError):
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
    question = ' '.join(context.args) # All text after the command
    if not question:
        await update.message.reply_text("Please ask a question after /gemini.\n"
                                        "Example: `/gemini How to save money?`")
        return
    
    await update.message.reply_text("Asking my AI brain... 🧠")
    try:
        response = model.generate_content(question)
        await update.message.reply_text(response.text)
    except Exception as e:
        logger.error(f"Error calling Gemini: {e}")
        await update.message.reply_text("Sorry, my AI brain is a bit foggy. Please try again.")

# --- Bill Splitting Conversation Functions (Same as before) ---

async def start_bill_split_convo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STARTS the conversation.
    This is triggered when a user sends a photo.
    """
    await update.message.reply_text("Got your photo! Reading the bill with AI... 📸")
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        file_bytes_io = io.BytesIO()
        await photo_file.download_to_memory(file_bytes_io)
        file_bytes_io.seek(0)
        img = Image.open(file_bytes_io)

        prompt = [
            "You are an expert receipt scanner. Analyze this image and extract all itemized items, their prices, "
            "and any tax and service charges. "
            "Respond *ONLY* with a valid JSON object in this exact format: "
            '{"items": [{"name": "Item Name", "price": 12.50}, {"name": "Another Item", "price": 8.00}], '
            '"tax": 1.50, "service_charge": 2.00, "subtotal": 20.50}'
            " If you cannot find items, tax, or service, set their value to 0.00. "
            "Do not include any other text before or after the JSON.",
            img
        ]
        
        response = model.generate_content(prompt)
        json_text = response.text.strip().lstrip("```json").rstrip("```")
        bill_data = json.loads(json_text)
        
        if "items" not in bill_data or not bill_data["items"]:
            await update.message.reply_text("Sorry, I couldn't find any items on that receipt. Please try a clearer photo.")
            return ConversationHandler.END

        context.user_data['bill_data'] = bill_data
        
        item_list = ""
        for i, item in enumerate(bill_data['items']):
            item_list += f"{i+1}. {item['name']} - ${item['price']:.2f}\n"
        
        summary_message = (
            "OK, I've read the bill! Here's what I found:\n\n"
            f"**Items:**\n{item_list}\n"
            f"**Tax:** ${bill_data.get('tax', 0.00):.2f}\n"
            f"**Service:** ${bill_data.get('service_charge', 0.00):.2f}\n\n"
            "---------------------------------\n"
            "**Now, please tell me who had what.**\n"
            "Send me a single message like this:\n\n"
            "Alice: Burger, Fries\n"
            "Bob: Salad\n"
            "Everyone: Tacos (to split an item)"
        )

        await update.message.reply_text(summary_message)
        return RECEIVE_ASSIGNMENTS

    except Exception as e:
        logger.error(f"Error in start_bill_split_convo: {e}")
        await update.message.reply_text("Sorry, I had trouble reading that receipt. Please try a clearer photo or type /cancel to stop.")
        return ConversationHandler.END

async def receive_assignments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    SECOND step in the conversation.
    Receives the text message of who ate what.
    """
    assignments_text = update.message.text
    bill_data = context.user_data.get('bill_data')

    if not bill_data:
        await update.message.reply_text("Oops! Something went wrong. Please send the photo again to start over.")
        return ConversationHandler.END

    await update.message.reply_text("Got it! Calculating the split... 🧮")
    
    calculation_prompt = (
        "You are an expert bill splitting calculator. I will give you a JSON of bill data and a text of assignments.\n\n"
        f"**Bill Data (JSON):**\n{json.dumps(bill_data)}\n\n"
        f"**Assignments (Text):**\n{assignments_text}\n\n"
        "**Your Task:**\n"
        "1.  Calculate the subtotal for each person based on the items they were assigned. Match item names fuzzily (e.g., 'Burger' matches 'burger').\n"
        "2.  If an item is assigned to 'Everyone' or 'Share', split its cost evenly among all people mentioned.\n"
        "3.  Calculate the total subtotal of all assigned items.\n"
        "4.  Calculate each person's *percentage* of this total subtotal.\n"
        "5.  Each person must pay their item subtotal, plus their *percentage* of the `tax` and `service_charge`.\n"
        "6.  Respond with a clear, final breakdown for each person.\n"
    )

    try:
        response = model.generate_content(calculation_prompt)
        await update.message.reply_text(response.text)

    except Exception as e:
        logger.error(f"Error in receive_assignments (calculation): {e}")
        await update.message.reply_text("Sorry, I had trouble with the final calculation. Please try again.")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels and ends the conversation."""
    await update.message.reply_text(
        "OK, I've cancelled the current bill split."
    )
    context.user_data.clear()
    return ConversationHandler.END

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles any command that the bot doesn't recognize."""
    await update.message.reply_text("Sorry, I don't understand that command. Type /start to see what I can do!")

# --- Flask Web Server ---
# This is the "app" that Vercel will run.
app = Flask(__name__)

if application: # Only set up handlers if the bot initialized correctly
    # --- Setup Handlers (No Polling) ---
    bill_split_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, start_bill_split_convo)],
        states={
            RECEIVE_ASSIGNMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_assignments)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(bill_split_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("split", split_command))
    application.add_handler(CommandHandler("gemini", gemini_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    @app.route('/')
    def home():
        """A simple page to show the bot is alive."""
        return "Hello! Your SplitBill AI Bot is alive and running."

    @app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
    async def webhook():
        """This is the main function that receives updates from Telegram."""
        update_data = flask_request.get_json()
        update = Update.de_json(data=update_data, bot=bot)
        
        logger.info(f"Received update: {update.update_id}")
        
        try:
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Error processing update: {e}")
            
        return Response(status=200)

# Vercel needs to know what 'app' is.
# This file will be run, and Vercel will find the 'app' object.
