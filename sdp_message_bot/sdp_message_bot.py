from telegram.ext import Application, MessageHandler, filters
from dotenv import load_dotenv
import logging
import os
from logging.handlers import TimedRotatingFileHandler

load_dotenv()

file_handler = TimedRotatingFileHandler(
    filename="bot.log",
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        file_handler,
        logging.StreamHandler(),
    ],
    force=True,
)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))


async def forward_message(update, context):
    message = update.effective_message
    if not message:
        return

    preview = (message.text or message.caption or "").strip() or "(без текста)"

    if message.chat_id == TARGET_CHAT_ID:
        logger.info(f"❌ Сообщение не было перенаправлено: {preview}")
        return

    try:
        await message.forward(chat_id=TARGET_CHAT_ID)
        logger.info(f"✅ Сообщение было перенаправлено: {preview}")
    except Exception as e:
        logger.error(f"❗ Ошибка при пересылке: {e}")


def main():
    application = Application.builder() \
        .token(TOKEN) \
        .arbitrary_callback_data(True) \
        .build()

    # Только группы, супергруппы и каналы — не пересылаем из лички
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS | filters.ChatType.CHANNEL,
            forward_message,
        )
    )

    logger.info("Бот запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()
