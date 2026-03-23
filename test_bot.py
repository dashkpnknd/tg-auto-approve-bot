from pyrogram import Client, filters, idle
from config_local import API_ID, API_HASH, BOT_TOKEN

app = Client(
    "test_bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    print("ПОЛУЧЕН /start")
    await message.reply("Бот работает")

async def main():
    await app.start()
    me = await app.get_me()
    print("БОТ:", me.username, me.id)
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())