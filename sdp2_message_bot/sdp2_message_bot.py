import asyncio
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from logging.handlers import TimedRotatingFileHandler

import aiohttp
from dotenv import load_dotenv
from telegram.ext import Application, MessageHandler, filters

load_dotenv()

# ===============================
# Настройка логгера
# ===============================
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

# ===============================
# Конфигурация
# ===============================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))

# --- Сеть / watchdog ---
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


WATCHDOG_ENABLED = _env_bool("WATCHDOG_ENABLED", True)
INTERNET_CHECK_URL = os.getenv(
    "INTERNET_CHECK_URL",
    "https://1.1.1.1/cdn-cgi/trace",
)
INTERNET_CHECK_URL_ALT = os.getenv("INTERNET_CHECK_URL_ALT", "").strip() or None
INTERNET_CHECK_INTERVAL_SEC = int(os.getenv("INTERNET_CHECK_INTERVAL_SEC", "120"))
CHECK_TIMEOUT_SEC = float(os.getenv("INTERNET_CHECK_TIMEOUT_SEC", "5"))
NETWORK_IFACE = os.getenv("NETWORK_IFACE", "").strip()
NIC_RESET_METHOD = os.getenv("NIC_RESET_METHOD", "ip").strip().lower()
POST_REBOOT_NOTIFY_FLAG = os.getenv("POST_REBOOT_NOTIFY_FLAG", "").strip() or os.path.join(
    SCRIPT_DIR, "notify_after_reboot.flag"
)
DIAG_LOG_MAX_CHARS = int(os.getenv("DIAG_LOG_MAX_CHARS", "5000"))
WATCHDOG_FAST_RETRY_SEC = int(os.getenv("WATCHDOG_FAST_RETRY_SEC", "20"))
WATCHDOG_FAST_RETRY_COUNT = int(os.getenv("WATCHDOG_FAST_RETRY_COUNT", "3"))
WATCHDOG_HEARTBEAT_SEC = int(os.getenv("WATCHDOG_HEARTBEAT_SEC", str(5 * 3600)))
POST_NIC_WAIT_SEC = int(os.getenv("POST_NIC_WAIT_SEC", "45"))
STARTUP_NOTIFY_RETRIES = int(os.getenv("STARTUP_NOTIFY_RETRIES", "3"))
STARTUP_NOTIFY_RETRY_DELAY_SEC = int(os.getenv("STARTUP_NOTIFY_RETRY_DELAY_SEC", "30"))

MSG_AFTER_NIC_RESET = os.getenv(
    "TELEGRAM_MSG_AFTER_NIC_RESET",
    "Привет, сеть снова в норме после сброса интерфейса.",
)
MSG_AFTER_REBOOT = os.getenv(
    "TELEGRAM_MSG_AFTER_REBOOT",
    "Привет, снова онлайн после перезагрузки сервера.",
)
MSG_AFTER_MANUAL_REBOOT = os.getenv(
    "TELEGRAM_MSG_AFTER_MANUAL_REBOOT",
    "По твоей команде перезагрузился, всё чётко.",
)
POST_MANUAL_REBOOT_NOTIFY_FLAG = os.getenv("POST_MANUAL_REBOOT_NOTIFY_FLAG", "").strip() or (
    os.path.join(SCRIPT_DIR, "notify_after_manual_reboot.flag")
)
MATRIX_REBOOT_TRIGGER = os.getenv(
    "MATRIX_REBOOT_TRIGGER",
    "матрица перезагрузка",
).strip().lower()
MATRIX_REBOOT_SUBSTITUTE = os.getenv(
    "MATRIX_REBOOT_SUBSTITUTE_TEXT",
    "Кассета жрет магнитную ленту",
)

# Сброс NIC и reboot под обычным пользователем: sudo -n + абсолютные пути (см. sudoers).
PRIVILEGED_USE_SUDO = _env_bool("PRIVILEGED_USE_SUDO", True)
IP_BIN = os.getenv("IP_BIN", "/usr/bin/ip").strip() or "/usr/bin/ip"
REBOOT_BIN = os.getenv("REBOOT_BIN", "/usr/sbin/reboot").strip() or "/usr/sbin/reboot"
NMCLI_BIN = os.getenv("NMCLI_BIN", "/usr/bin/nmcli").strip() or "/usr/bin/nmcli"


def _privileged_argv(argv: list[str]) -> list[str]:
    if PRIVILEGED_USE_SUDO:
        return ["sudo", "-n", *argv]
    return argv


# ===============================
# Переменные для пересылки сообщений
# ===============================
media_groups = defaultdict(list)
media_timers = {}


# ===============================
# Пересылка сообщений
# ===============================
def _message_has_matrix_reboot(msg) -> bool:
    t = (msg.text or msg.caption or "").lower()
    return bool(MATRIX_REBOOT_TRIGGER and MATRIX_REBOOT_TRIGGER in t)


async def forward_album(media_group_id, context):
    messages = media_groups.pop(media_group_id, [])
    if messages and any(_message_has_matrix_reboot(m) for m in messages):
        media_timers.pop(media_group_id, None)
        await _matrix_reboot_sequence(context)
        return

    for msg in messages:
        try:
            await msg.forward(chat_id=TARGET_CHAT_ID)
            logger.info("✅ Переслано сообщение из альбома")
        except Exception as e:
            logger.error(f"❗ Ошибка при пересылке сообщения из альбома: {e}")

    media_timers.pop(media_group_id, None)


async def schedule_forward_album(media_group_id, context, delay=3):
    await asyncio.sleep(delay)
    if media_group_id in media_groups:
        await forward_album(media_group_id, context)


async def forward_message(update, context):
    message = update.message
    if not message or message.chat_id == TARGET_CHAT_ID:
        return

    text_lower = (message.text or message.caption or "").lower()
    if (
        MATRIX_REBOOT_TRIGGER
        and text_lower
        and MATRIX_REBOOT_TRIGGER in text_lower
    ):
        await _matrix_reboot_sequence(context)
        return

    mgid = message.media_group_id
    if mgid:
        media_groups[mgid].append(message)
        logger.info(
            f"📸 Альбом {mgid}: сообщение {len(media_groups[mgid])}"
        )

        if mgid not in media_timers:
            media_timers[mgid] = asyncio.create_task(
                schedule_forward_album(mgid, context)
            )
        return

    try:
        await message.forward(chat_id=TARGET_CHAT_ID)
        logger.info("✅ Переслано сообщение")
    except Exception as e:
        logger.error(f"❗ Ошибка при пересылке: {e}")


# ===============================
# Watchdog: проверка интернета, диагностика, NIC, reboot
# ===============================
async def _run_exec(argv: list[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace").strip()
        return proc.returncode or 0, text
    except Exception as e:
        return -1, str(e)


async def check_internet_reachable(session: aiohttp.ClientSession) -> bool:
    urls = [INTERNET_CHECK_URL]
    if INTERNET_CHECK_URL_ALT:
        urls.append(INTERNET_CHECK_URL_ALT)

    timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT_SEC)
    for url in urls:
        try:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                if 200 <= resp.status < 400:
                    return True
        except Exception:
            continue
    return False


async def _default_gateway() -> str | None:
    code, out = await _run_exec([IP_BIN, "route", "show", "default"])
    if code != 0:
        return None
    for line in out.splitlines():
        if line.startswith("default via "):
            parts = line.split()
            if len(parts) >= 3 and re.match(r"^\d", parts[2]):
                return parts[2]
    return None


async def collect_network_diag() -> str:
    parts: list[str] = []
    for label, argv in (
        ("ip -br addr", [IP_BIN, "-br", "addr"]),
        ("ip route", [IP_BIN, "route"]),
    ):
        code, out = await _run_exec(argv)
        parts.append(f"=== {label} (rc={code}) ===\n{out or '(пусто)'}")

    gw = await _default_gateway()
    if gw:
        code, out = await _run_exec(["ping", "-c", "1", "-W", "2", gw])
        parts.append(f"=== ping gateway {gw} (rc={code}) ===\n{out or '(пусто)'}")

    text = "\n\n".join(parts)
    if len(text) > DIAG_LOG_MAX_CHARS:
        text = text[:DIAG_LOG_MAX_CHARS] + "\n... [обрезано по DIAG_LOG_MAX_CHARS]"
    return text


async def reset_network_interface() -> None:
    if not NETWORK_IFACE:
        logger.error("NET_WATCHDOG: NETWORK_IFACE не задан, сброс интерфейса пропущен.")
        return

    if NIC_RESET_METHOD == "nmcli":
        logger.warning(
            f"NET_WATCHDOG: nmcli сброс {NETWORK_IFACE} (disconnect/connect)..."
        )
        c1, o1 = await _run_exec(_privileged_argv([NMCLI_BIN, "device", "disconnect", NETWORK_IFACE]))
        logger.info(f"NET_WATCHDOG nmcli disconnect rc={c1} {o1[:200]}")
        await asyncio.sleep(2)
        c2, o2 = await _run_exec(_privileged_argv([NMCLI_BIN, "device", "connect", NETWORK_IFACE]))
        logger.info(f"NET_WATCHDOG nmcli connect rc={c2} {o2[:200]}")
        return

    logger.warning(f"NET_WATCHDOG: ip link set {NETWORK_IFACE} down/up...")
    c1, o1 = await _run_exec(
        _privileged_argv([IP_BIN, "link", "set", NETWORK_IFACE, "down"])
    )
    logger.info(f"NET_WATCHDOG ip down rc={c1} {o1[:200]}")
    await asyncio.sleep(2)
    c2, o2 = await _run_exec(
        _privileged_argv([IP_BIN, "link", "set", NETWORK_IFACE, "up"])
    )
    logger.info(f"NET_WATCHDOG ip up rc={c2} {o2[:200]}")


def _write_reboot_flag() -> None:
    try:
        with open(POST_REBOOT_NOTIFY_FLAG, "w", encoding="utf-8") as f:
            f.write("pending\n")
        logger.warning(
            f"NET_WATCHDOG: создан флаг уведомления после ребута: {POST_REBOOT_NOTIFY_FLAG}"
        )
    except OSError as e:
        logger.error(f"NET_WATCHDOG: не удалось записать флаг ребута: {e}")


def _write_manual_reboot_flag() -> None:
    try:
        with open(POST_MANUAL_REBOOT_NOTIFY_FLAG, "w", encoding="utf-8") as f:
            f.write("pending\n")
        logger.warning(
            f"Создан флаг уведомления после ручной перезагрузки: {POST_MANUAL_REBOOT_NOTIFY_FLAG}"
        )
    except OSError as e:
        logger.error(f"Не удалось записать флаг ручного ребута: {e}")


def _trigger_reboot() -> None:
    try:
        subprocess.Popen(
            _privileged_argv([REBOOT_BIN]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.error("Вызван reboot хоста")
    except Exception as e:
        logger.error(f"Не удалось вызвать reboot: {e}")


async def _matrix_reboot_sequence(context) -> None:
    try:
        await context.application.bot.send_message(TARGET_CHAT_ID, MATRIX_REBOOT_SUBSTITUTE)
        logger.info(
            f"Команда «{MATRIX_REBOOT_TRIGGER}»: в целевой чат отправлен подменённый текст, "
            "через паузу — перезагрузка сервера."
        )
    except Exception as e:
        logger.error(f"❗ Не удалось отправить подменённый текст перед ребутом: {e}")
        return
    _write_manual_reboot_flag()
    await asyncio.sleep(2)
    _trigger_reboot()


async def send_watchdog_telegram(application: Application, text: str) -> None:
    try:
        await application.bot.send_message(TARGET_CHAT_ID, text)
        logger.info(f"NET_WATCHDOG: отправлено в Telegram: {text[:80]}...")
    except Exception as e:
        logger.error(f"NET_WATCHDOG: ошибка отправки в Telegram: {e}")


async def internet_watchdog_loop(application: Application) -> None:
    if not WATCHDOG_ENABLED:
        logger.info("NET_WATCHDOG отключён (WATCHDOG_ENABLED=0).")
        return

    if not NETWORK_IFACE:
        logger.warning(
            "NET_WATCHDOG: NETWORK_IFACE пуст — проверка интернета активна, "
            "сброс интерфейса и ребут при сбое отключены."
        )

    connector = aiohttp.TCPConnector(ssl=True)
    last_ok_heartbeat = time.monotonic()
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                ok = await check_internet_reachable(session)
                if ok:
                    now = time.monotonic()
                    if now - last_ok_heartbeat >= WATCHDOG_HEARTBEAT_SEC:
                        logger.info(
                            "NET_WATCHDOG: работаем, с интернетом всё хорошо "
                            f"(прошло ≥{WATCHDOG_HEARTBEAT_SEC // 3600} ч без сбоев)."
                        )
                        last_ok_heartbeat = now
                    await asyncio.sleep(INTERNET_CHECK_INTERVAL_SEC)
                    continue

                last_ok_heartbeat = time.monotonic()

                logger.warning(
                    f"NET_WATCHDOG: нет связи (основная проверка раз в "
                    f"{INTERNET_CHECK_INTERVAL_SEC} с), url={INTERNET_CHECK_URL!r}; "
                    f"ещё {WATCHDOG_FAST_RETRY_COUNT} проверки каждые {WATCHDOG_FAST_RETRY_SEC} с..."
                )
                recovered = False
                for n in range(1, WATCHDOG_FAST_RETRY_COUNT + 1):
                    await asyncio.sleep(WATCHDOG_FAST_RETRY_SEC)
                    if await check_internet_reachable(session):
                        logger.info(
                            f"NET_WATCHDOG: связь восстановилась на быстрой проверке {n}/"
                            f"{WATCHDOG_FAST_RETRY_COUNT}."
                        )
                        recovered = True
                        break

                if recovered:
                    await asyncio.sleep(INTERNET_CHECK_INTERVAL_SEC)
                    continue

                logger.error(
                    "NET_WATCHDOG: нет связи после основной проверки и "
                    f"{WATCHDOG_FAST_RETRY_COUNT} быстрых повторов — диагностика."
                )
                diag = await collect_network_diag()
                logger.error(f"NET_WATCHDOG диагностика:\n{diag}")

                if NETWORK_IFACE:
                    await reset_network_interface()
                    await asyncio.sleep(POST_NIC_WAIT_SEC)
                    ok = await check_internet_reachable(session)
                    if ok:
                        await send_watchdog_telegram(application, MSG_AFTER_NIC_RESET)
                        await asyncio.sleep(INTERNET_CHECK_INTERVAL_SEC)
                        continue
                    logger.error(
                        "NET_WATCHDOG: после сброса интерфейса связи нет — перезагрузка хоста."
                    )
                else:
                    logger.error(
                        "NET_WATCHDOG: NETWORK_IFACE не задан — перезагрузка хоста без сброса NIC."
                    )
                _write_reboot_flag()
                await asyncio.sleep(1)
                _trigger_reboot()
                await asyncio.sleep(3600)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"NET_WATCHDOG: непойманная ошибка цикла: {e}")
                await asyncio.sleep(INTERNET_CHECK_INTERVAL_SEC)


async def _send_pending_notice_for_flag(
    application: Application,
    flag_path: str,
    message_text: str,
    log_label: str,
) -> None:
    if not os.path.isfile(flag_path):
        return
    for attempt in range(STARTUP_NOTIFY_RETRIES):
        try:
            await application.bot.send_message(TARGET_CHAT_ID, message_text)
            try:
                os.remove(flag_path)
            except OSError as e:
                logger.error(f"Не удалось удалить флаг ({log_label}): {e}")
            logger.info(f"Уведомление после перезагрузки ({log_label}) отправлено в Telegram.")
            return
        except Exception as e:
            logger.error(
                f"Уведомление ({log_label}), попытка {attempt + 1}/{STARTUP_NOTIFY_RETRIES}: {e}"
            )
        await asyncio.sleep(STARTUP_NOTIFY_RETRY_DELAY_SEC)
    logger.warning(
        f"Флаг {flag_path!r} ({log_label}) сохранён: повторим отправку при следующем старте."
    )


async def send_pending_reboot_notice(application: Application) -> None:
    await _send_pending_notice_for_flag(
        application,
        POST_MANUAL_REBOOT_NOTIFY_FLAG,
        MSG_AFTER_MANUAL_REBOOT,
        "ручной ребут",
    )
    await _send_pending_notice_for_flag(
        application,
        POST_REBOOT_NOTIFY_FLAG,
        MSG_AFTER_REBOOT,
        "watchdog",
    )


# ===============================
# Запуск бота
# ===============================
def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS | filters.ChatType.CHANNEL,
            forward_message,
        )
    )

    async def on_startup(app: Application) -> None:
        logger.info(
            "Пересылка: все сообщения из групп и каналов → "
            f"чат {TARGET_CHAT_ID}"
        )

        await send_pending_reboot_notice(app)

        if WATCHDOG_ENABLED:
            asyncio.create_task(internet_watchdog_loop(app))
            logger.info(
                f"NET_WATCHDOG: интервал {INTERNET_CHECK_INTERVAL_SEC} с, iface={NETWORK_IFACE or '—'}"
            )

    application.post_init = on_startup

    logger.info("🚀 Бот запущен. Свободная пересылка из групп и каналов.")
    application.run_polling()


if __name__ == "__main__":
    main()
