from telegram.ext import Application, MessageHandler, filters
from dotenv import load_dotenv
import logging
import os
from pytz import utc  # Используем UTC как часовой пояс

load_dotenv()

# Настройка логов
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    filename='bot.log'
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))  # через этот бот узнаем @getidsbot

async def forward_message(update, context): # не копировать сообщения из целевого чата
    if update.message.chat_id != TARGET_CHAT_ID:
        await update.message.forward(chat_id=TARGET_CHAT_ID)

def main():
    application = Application.builder() \
        .token(TOKEN) \
        .arbitrary_callback_data(True) \
        .build()
    
    application.add_handler(MessageHandler(filters.ALL, forward_message))
    
    print("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
