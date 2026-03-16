import logging
import os
import asyncio
import aiohttp
from logging.handlers import TimedRotatingFileHandler
from dotenv import load_dotenv
from telegram.ext import Application

# ===============================
# Настройка окружения и логгера
# ===============================
load_dotenv()

file_handler = TimedRotatingFileHandler(
    filename='bot.log',
    when='midnight',
    interval=1,
    backupCount=30,
    encoding='utf-8'
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        file_handler,
        logging.StreamHandler()
    ],
    force=True
)

logger = logging.getLogger(__name__)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

# ===============================
# Конфигурация
# ===============================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))
TARGET_NUMBERS = [
    number.strip()
    for number in os.getenv(
        "TARGET_NUMBERS",
        "827,818,828,854,3632,2191,3139,3355,2274",
    ).split(",")
    if number.strip()
]

# URL Django-функции
API_URL = os.getenv("API_URL_STATUS", "http://192.168.45.66:8000/controllers_status_check/")

# Храним последнее состояние и статусы для подтверждения
last_status = {}
pending_changes = {}  # Для хранения неподтвержденных изменений
IMPORTANT_STATUSES = {"На связи, работает", "Нет связи", "В ЖМ", "На чёрном"}

# ===============================
# Запрос к Django API
# ===============================
async def fetch_status():
    if not TARGET_NUMBERS:
        logger.warning("Список TARGET_NUMBERS пуст, запрос к API пропущен.")
        return None

    params = {"controllers": ",".join(TARGET_NUMBERS)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"Ошибка ответа API: {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"Ошибка запроса к API: {e}")
        return None

# ===============================
# Фоновая задача для проверки
# ===============================
async def check_status_loop(application):
    global last_status, pending_changes
    
    # Основной интервал проверки (150 секунд)
    MAIN_CHECK_INTERVAL = 150
    # Интервал для повторной проверки при обнаружении изменений (150 секунд)
    CONFIRMATION_INTERVAL = 150
    # Максимальное число проверок неподтвержденного изменения, чтобы избежать зацикливания
    MAX_PENDING_ATTEMPTS = 2
    
    while True:
        data = await fetch_status()
        
        if data and data.get("Success"):
            controllers = data.get("Controllers", [])
            current_status = {c["ControllerId"]: c["Status"] for c in controllers}
            
            # Проверяем, есть ли неподтвержденные изменения
            if pending_changes:
                logger.info("🔍 Проверка неподтвержденных изменений...")
                confirmed_changes = {}
                dropped_changes = []
                
                for ctrl_id, pending_info in pending_changes.items():
                    pending_status = pending_info["status"]
                    previous_status = pending_info["previous_status"]
                    attempts = pending_info["attempts"]
                    current_status_for_ctrl = current_status.get(ctrl_id)
                    
                    if current_status_for_ctrl == pending_status:
                        # Изменение подтверждено
                        confirmed_changes[ctrl_id] = pending_status
                        logger.info(f"✅ Изменение подтверждено: {ctrl_id} = {pending_status}")
                    elif current_status_for_ctrl == previous_status:
                        # Изменение откатилось к предыдущему состоянию, сбрасываем ожидание
                        logger.info(
                            f"↩️ Изменение откатилось: {ctrl_id} "
                            f"(ожидалось: {pending_status}, вернулось к: {previous_status})"
                        )
                        dropped_changes.append(ctrl_id)
                    else:
                        # Изменение не подтвердилось
                        attempts += 1
                        pending_info["attempts"] = attempts
                        logger.info(
                            f"❌ Изменение не подтвердилось: {ctrl_id} "
                            f"(ожидалось: {pending_status}, получено: {current_status_for_ctrl}, попытка: {attempts}/{MAX_PENDING_ATTEMPTS})"
                        )
                        if attempts >= MAX_PENDING_ATTEMPTS:
                            logger.warning(
                                f"🧹 Сброс неподтвержденного изменения: {ctrl_id} "
                                f"(достигнут лимит попыток {MAX_PENDING_ATTEMPTS})"
                            )
                            dropped_changes.append(ctrl_id)
                
                # Отправляем подтвержденные изменения
                for ctrl_id, status in confirmed_changes.items():
                    if status in IMPORTANT_STATUSES:
                        msg = f"{ctrl_id}: {status}"
                        try:
                            await application.bot.send_message(TARGET_CHAT_ID, msg)
                            logger.info(f"📤 Отправлено сообщение: {msg}")
                            # Обновляем последнее известное состояние
                            last_status[ctrl_id] = status
                        except Exception as e:
                            logger.error(f"❗ Ошибка при отправке в Telegram: {e}")
                
                # Очищаем обработанные изменения
                for ctrl_id in confirmed_changes.keys():
                    pending_changes.pop(ctrl_id, None)
                for ctrl_id in dropped_changes:
                    pending_changes.pop(ctrl_id, None)
            
            # Основная проверка изменений (каждые 150 секунд)
            else:
                logger.info("🔄 Основная проверка статусов...")
                changes_detected = False
                
                for ctrl_id, status in current_status.items():
                    old_status = last_status.get(ctrl_id)
                    
                    if old_status is None:
                        logger.info(f"📡 Первое состояние: {ctrl_id} = {status}")
                        # сохраняем только важные статусы
                        if status in IMPORTANT_STATUSES:
                            last_status[ctrl_id] = status
                    elif old_status != status:
                        if status in IMPORTANT_STATUSES:
                            logger.info(f"⚠️ Обнаружено изменение: {ctrl_id} ({old_status} → {status})")
                            pending_changes[ctrl_id] = {
                                "status": status,
                                "previous_status": old_status,
                                "attempts": 0,
                            }
                            changes_detected = True
                        else:
                            # любой другой статус — просто лог, last_status не меняем
                            logger.info(f"ℹ️ {ctrl_id} изменил статус на: {status}")
                
                if changes_detected:
                    logger.info(f"⏳ Изменения обнаружены. Повторная проверка через {CONFIRMATION_INTERVAL} секунд...")
                    await asyncio.sleep(CONFIRMATION_INTERVAL)
                    continue  # Сразу переходим к следующей проверке для подтверждения
        
        # Определяем интервал ожидания
        if pending_changes:
            # Если есть неподтвержденные изменения, ждем меньше для повторной проверки
            wait_interval = CONFIRMATION_INTERVAL
            logger.info(f"⏰ Ожидание {wait_interval} секунд до проверки неподтвержденных изменений...")
        else:
            # Обычный интервал ожидания
            wait_interval = MAIN_CHECK_INTERVAL
            logger.info(f"⏰ Ожидание {wait_interval} секунд до следующей проверки...")
        
        await asyncio.sleep(wait_interval)

# ===============================
# Точка входа
# ===============================
def main():
    application = Application.builder().token(TOKEN).build()

    # запускаем фоновый таск при старте
    async def on_startup(app):
        tracked_numbers = ", ".join(TARGET_NUMBERS) if TARGET_NUMBERS else "не задано"
        logger.info(f"Мониторинг запущен. Отслеживаю объекты: {tracked_numbers}")
        asyncio.create_task(check_status_loop(app))

    application.post_init = on_startup

    logger.info(f"🚀 Бот запущен! Отслеживает числа: {', '.join(TARGET_NUMBERS)}")
    logger.info("📊 Мониторинг статусов контроллеров активен с подтверждением изменений.")
    application.run_polling()

if __name__ == "__main__":
    main()