import asyncio
import random
import time
import json
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, MessageHandler, ChatMemberHandler, filters
from telegram.request import HTTPXRequest
import requests

load_dotenv()

# --- CONFIG ---
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Only allow the bot in these groups/channels
AUTHORIZED_GROUP_IDS = os.getenv("AUTHORIZED_GROUP_IDS").split(
    ","
)  # Add other allowed chat IDs here

# --- GLOBAL VARIABLES ---
# Use /tmp directory for session file in Lambda (writable location)
client = TelegramClient("/tmp/session", API_ID, API_HASH)
# Bot will be initialized in lambda_handler
bot = None


# --- FETCH THE LATEST MESSAGES BEFORE DAILY SEND ---
async def fetch_recent_messages(bot_id, chat_id, limit=1000):
    temp_messages = []

    await client.start()
    async for msg in client.iter_messages(chat_id, limit=limit):
        if not msg.text:
            continue
        sender = await msg.get_sender()
        if not sender or getattr(sender, "id", None) == bot_id:
            continue

        # Build sender name
        name_parts = []
        if getattr(sender, "first_name", None):
            name_parts.append(sender.first_name)
        if getattr(sender, "last_name", None):
            name_parts.append(sender.last_name)
        if getattr(sender, "username", None):
            sender_name = (
                f"{' '.join(name_parts)} ({sender.username})"
                if name_parts
                else f"({sender.username})"
            )
        else:
            sender_name = " ".join(name_parts) if name_parts else "Unknown"

        temp_messages.append({"text": msg.text, "sender_name": sender_name})

    await client.disconnect()
    print(f"Fetched {len(temp_messages)} messages from chat {chat_id}")
    return temp_messages


# --- SEND RANDOM MESSAGE + PUBLIC POLL ---
async def send_random_with_poll(chat_id, messages):
    if not messages:
        print(f"No messages to send in chat {chat_id}.")
        return

    chosen = random.choice(messages)
    text = chosen["text"]
    correct_name = chosen["sender_name"]

    # Pick 4 other random names
    other_names = list(
        {m["sender_name"] for m in messages if m["sender_name"] != correct_name}
    )
    options = random.sample(other_names, min(4, len(other_names)))
    options.append(correct_name)
    random.shuffle(options)

    await bot.send_message(chat_id=chat_id, text=text)
    await bot.send_poll(
        chat_id=chat_id,
        question="Who sent this message?",
        options=options,
        type="quiz",
        correct_option_id=options.index(correct_name),
        is_anonymous=False,
    )
    print(
        f"Sent message and public poll to chat {chat_id}: correct answer = {correct_name}"
    )


# --- HANDLE NEW MESSAGES IN CHATS ---
async def track_message(update: Update, context):
    chat_id = update.message.chat_id
    if chat_id not in AUTHORIZED_GROUP_IDS:
        try:
            await bot.leave_chat(chat_id)
            print(f"Left unauthorized chat {chat_id} (new message detected)")
        except Exception as e:
            print(f"Error leaving chat {chat_id}: {e}")


# --- HANDLE BOT ADDED TO NEW GROUP/CHANNEL ---
async def handle_new_chat(update: Update, context):
    chat_id = update.my_chat_member.chat.id
    chat_type = update.my_chat_member.chat.type  # 'group', 'supergroup', 'channel'
    status = update.my_chat_member.new_chat_member.status

    # If bot is a member/admin and chat is NOT authorized
    if status in ["member", "administrator"] and chat_id not in AUTHORIZED_GROUP_IDS:
        try:
            await bot.leave_chat(chat_id)
            print(
                f"Left unauthorized {chat_type} {chat_id} immediately upon being added"
            )
        except Exception as e:
            print(f"Error leaving {chat_type} {chat_id}: {e}")


# # --- DAILY SCHEDULER ---
async def send_poll():
    me = await bot.get_me()
    bot_id = me.id

    for chat_id in AUTHORIZED_GROUP_IDS:
        # Fetch last 1000 messages right before sending
        messages = await fetch_recent_messages(bot_id, chat_id)
        if messages:
            await send_random_with_poll(chat_id, messages, bot)
        # Delete messages list to free memory
        del messages


# --- SAMPLE HTTP REQUEST USING REQUESTS ---
def sample_http_request():
    url = "https://www.google.com"
    try:
        response = requests.get(url, timeout=10)
        print("Response status code:", response.status_code)
        print(
            "Response body:", response.text[:200]
        )  # Print first 200 characters of the response body
    except Exception as e:
        print("Error during HTTP request:", e)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        # close the loop to release resources
        # ensure pending tasks are cancelled (if any)
        for t in asyncio.all_tasks(loop):
            t.cancel()
        loop.run_until_complete(asyncio.sleep(0))  # give cancellations a chance
        loop.close()


# --- MAIN FUNCTION ---
async def process_update(event, request_obj):
    """Process a single update from webhook"""
    # Build application with shorter timeouts

    app = ApplicationBuilder().token(BOT_TOKEN).request(request_obj).build()
    app.add_handler(MessageHandler(filters.ALL, track_message))
    app.add_handler(ChatMemberHandler(handle_new_chat))
    await app.initialize()

    # Process the update
    update = Update.de_json(event, app.bot)
    await app.process_update(update)


def lambda_handler(event, context):
    """AWS Lambda handler for webhook"""
    # confirm that the call is from telegram
    print("Received event:", event)

    sample_http_request()

    request_obj = HTTPXRequest(
        connection_pool_size=10, connect_timeout=5.0, read_timeout=5.0
    )

    global bot
    bot = Bot(token=BOT_TOKEN, request=request_obj)

    # Check if this is a scheduled event to send daily poll (from EventBridge)
    if event.get("source") == "aws.events" or "detail-type" in event:
        _run_async(send_poll())
        return {"statusCode": 200, "body": "Poll check completed"}

    # Otherwise, process webhook update from API Gateway
    body = json.loads(event["body"])
    _run_async(process_update(body, request_obj))

    return {"statusCode": 200, "body": "OK"}
