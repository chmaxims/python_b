# bot.py — финальная версия для Render (все проблемы исправлены)
import os
import logging
import psycopg2
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import TelegramError

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Настройки
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")


# === Вспомогательные функции для работы с БД ===

def get_db_connection():
    """Создаёт новое соединение с БД."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def is_user_banned(user_id):
    """Проверяет, забанен ли пользователь (кроме админа)."""
    if user_id == ADMIN_USER_ID:
        return False
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_banned FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else False
    except Exception as e:
        logger.error(f"Ошибка при проверке бана пользователя {user_id}: {e}")
        return False


def ensure_user_exists(user_id):
    """Гарантирует, что пользователь есть в таблице users."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (user_id, notifications_enabled)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                """, (user_id, True))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при создании пользователя {user_id}: {e}")


# === Функции работы с БД ===

def init_db():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS categories (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS products (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        user_name TEXT NOT NULL,
                        product_name TEXT NOT NULL,
                        photo_file_id TEXT,
                        rating TEXT NOT NULL CHECK (rating IN ('Отлично', 'Плохо')),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        category_id INTEGER NOT NULL REFERENCES categories(id)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        notifications_enabled BOOLEAN DEFAULT TRUE,
                        is_banned BOOLEAN DEFAULT FALSE
                    );
                """)

                try:
                    cur.execute("ALTER TABLE products ADD COLUMN photo_file_id TEXT;")
                    conn.commit()
                except psycopg2.errors.DuplicateColumn:
                    pass
                except Exception as e:
                    logger.warning(f"Ошибка при миграции photo_file_id: {e}")
                    conn.rollback()

                conn.commit()
        print("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        raise


def get_notification_status(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT notifications_enabled FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute("INSERT INTO users (user_id, notifications_enabled) VALUES (%s, %s)", (user_id, True))
                    conn.commit()
                    return True
                return row[0]
    except Exception as e:
        logger.error(f"Ошибка при получении статуса уведомлений {user_id}: {e}")
        return True


def toggle_notifications(user_id):
    current = get_notification_status(user_id)
    new_status = not current
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (user_id, notifications_enabled)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET notifications_enabled = %s
                """, (user_id, new_status, new_status))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при переключении уведомлений {user_id}: {e}")
    return new_status


def get_subscribers(exclude_user_id=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if exclude_user_id is not None:
                    cur.execute("SELECT user_id FROM users WHERE notifications_enabled = TRUE AND user_id != %s",
                                (exclude_user_id,))
                else:
                    cur.execute("SELECT user_id FROM users WHERE notifications_enabled = TRUE")
                return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Ошибка при получении подписчиков: {e}")
        return []


def get_categories():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM categories ORDER BY name")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Ошибка при получении категорий: {e}")
        return []


def add_category(name):
    # Запрет системных имён
    if name.strip() in {"Назад", "Другое"}:
        return None
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
                cur.execute("SELECT id FROM categories WHERE name = %s", (name,))
                conn.commit()
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Ошибка при добавлении категории '{name}': {e}")
        return None


def save_product(user_id, user_name, category_id, product_name, rating, photo_file_id=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO products (user_id, user_name, category_id, product_name, photo_file_id, rating)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, user_name, category_id, product_name, photo_file_id, rating))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при сохранении товара: {e}")


def get_products_by_category_and_rating(category_id, rating):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT product_name, created_at, user_name, id, photo_file_id
                    FROM products 
                    WHERE category_id = %s AND rating = %s
                    ORDER BY created_at DESC
                """, (category_id, rating))
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Ошибка при получении товаров: {e}")
        return []


def get_all_products_with_categories():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.id, p.product_name, c.name, p.created_at
                    FROM products p 
                    JOIN categories c ON p.category_id = c.id
                    ORDER BY c.name, p.product_name
                """)
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Ошибка при получении всех товаров: {e}")
        return []

def get_user_products(user_id):
    """Получает список товаров текущего пользователя с категориями."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.id, p.product_name, c.name, p.created_at, p.photo_file_id
                    FROM products p
                    JOIN categories c ON p.category_id = c.id
                    WHERE p.user_id = %s
                    ORDER BY p.created_at DESC
                """, (user_id,))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Ошибка при получении товаров пользователя {user_id}: {e}")
        return []

def get_all_users():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.user_id, p.user_name
                    FROM users u
                    LEFT JOIN products p ON u.user_id = p.user_id
                    GROUP BY u.user_id, p.user_name
                    ORDER BY u.user_id
                """)
                users = cur.fetchall()
                unique_users = {}
                for user_id, name in users:
                    if user_id not in unique_users:
                        unique_users[user_id] = name or "Неизвестно"
                return list(unique_users.items())
    except Exception as e:
        logger.error(f"Ошибка при получении всех пользователей: {e}")
        return []


def update_category_name(category_id, new_name):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE categories SET name = %s WHERE id = %s", (new_name, category_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при переименовании категории {category_id}: {e}")


def move_product_to_category(product_id, new_category_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE products SET category_id = %s WHERE id = %s", (new_category_id, product_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при перемещении товара {product_id}: {e}")


def delete_product(product_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при удалении товара {product_id}: {e}")


def clear_all_data():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM products")
                cur.execute("DELETE FROM categories")
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при очистке БД: {e}")


# === Глобальное состояние ===
user_state = {}


def get_main_menu(user_id=None):
    if user_id is not None:
        notifications_on = get_notification_status(user_id)
        notify_icon = "🔔" if notifications_on else "🔕"
    else:
        notify_icon = "🔔"
    keyboard = [
        ["➕   Добавить товар", "❔   Справка"],
        ["✅   Покупать", "❌   Не покупать"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def get_category_keyboard(show_other=False, show_back=False):
    categories = get_categories()
    buttons = []
    row = []
    for i, (cat_id, name) in enumerate(categories, 1):
        row.append(str(i))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    extra_buttons = []
    if show_other:
        extra_buttons.append("Другое")
    if show_back:
        extra_buttons.append("Назад")
    if extra_buttons:
        buttons.append(extra_buttons)
    if not buttons:
        buttons = [["Назад"]]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def format_category_list(mode=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if mode == 'recommend':
                    cur.execute("""
                        SELECT c.id, c.name, COUNT(p.id) as product_count
                        FROM categories c
                        LEFT JOIN products p ON c.id = p.category_id AND p.rating = 'Отлично'
                        GROUP BY c.id, c.name
                        ORDER BY c.name
                    """)
                elif mode == 'avoid':
                    cur.execute("""
                        SELECT c.id, c.name, COUNT(p.id) as product_count
                        FROM categories c
                        LEFT JOIN products p ON c.id = p.category_id AND p.rating = 'Плохо'
                        GROUP BY c.id, c.name
                        ORDER BY c.name
                    """)
                else:
                    cur.execute("""
                        SELECT c.id, c.name, COUNT(p.id) as product_count
                        FROM categories c
                        LEFT JOIN products p ON c.id = p.category_id
                        GROUP BY c.id, c.name
                        ORDER BY c.name
                    """)
                categories = cur.fetchall()
                if not categories:
                    return "Нет категорий."
                lines = [f"{i}. {name} — [{count}]" for i, (cat_id, name, count) in enumerate(categories, 1)]
                return "Выберите категорию:\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"Ошибка при форматировании списка категорий: {e}")
        return "Ошибка при загрузке категорий."


# === Декоратор для проверки бана ===

def banned_user_check(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if is_user_banned(user_id):
            await update.message.reply_text("❌ Доступ запрещён.")
            return
        return await func(update, context)

    return wrapper


# === Обработчики команд ===

@banned_user_check
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🛠️ Админ-команды:\n\n"
        "/del_user <id> — удалить пользователя\n"
        "/ban_user <id> — забанить пользователя\n"
        "/unban_user <id> — разбанить пользователя\n"
        "/clear_all — очистить всю базу данных\n"
    )
    await update.message.reply_text(help_text)


@banned_user_check
async def clear_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    clear_all_data()
    await update.message.reply_text("🗑️ База данных очищена.")


@banned_user_check
async def change_cat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    categories = get_categories()
    if not categories:
        await update.message.reply_text("Нет категорий для редактирования.")
        return
    lines = [f"{i}. {name}" for i, (cat_id, name) in enumerate(categories, 1)]
    msg = "Выберите категорию для изменения:\n" + "\n".join(lines)
    await update.message.reply_text(msg)
    user_state[update.effective_user.id] = {'step': 'selecting_category_to_rename'}


@banned_user_check
async def change_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    products = get_all_products_with_categories()
    if not products:
        await update.message.reply_text("Нет товаров для перемещения.")
        return
    lines = [f"{i}. {name} → {cat}" for i, (pid, name, cat, _) in enumerate(products, 1)]
    msg = "Выберите товар для перемещения:\n" + "\n".join(lines)
    await update.message.reply_text(msg)
    user_state[update.effective_user.id] = {
        'step': 'selecting_product_to_move',
        'products': products
    }


@banned_user_check
async def del_position_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    products = get_all_products_with_categories()
    if not products:
        await update.message.reply_text("Нет товаров для удаления.")
        return
    lines = [f"{i}. {name} → {cat} — {date.strftime('%d.%m.%Y')}"
             for i, (pid, name, cat, date) in enumerate(products, 1)]
    msg = "Выберите товар для удаления:\n" + "\n".join(lines)
    await update.message.reply_text(msg)
    user_state[update.effective_user.id] = {
        'step': 'selecting_product_to_delete',
        'products': products
    }


@banned_user_check
async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    lines = [f"{i}. {name} (ID: {user_id})" for i, (user_id, name) in enumerate(users, 1)]
    msg = "Выберите пользователя для удаления:\n" + "\n".join(lines)
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
    )
    user_state[update.effective_user.id] = {
        'step': 'selecting_user_to_delete',
        'users': users
    }


@banned_user_check
async def ban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    lines = [f"{i}. {name} (ID: {user_id})" for i, (user_id, name) in enumerate(users, 1)]
    msg = "Выберите пользователя для блокировки:\n" + "\n".join(lines)
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
    )
    user_state[update.effective_user.id] = {
        'step': 'selecting_user_to_ban',
        'users': users
    }


@banned_user_check
async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE is_banned = TRUE")
                banned_ids = [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Ошибка при получении забаненных: {e}")
        banned_ids = []

    if not banned_ids:
        await update.message.reply_text("Нет забаненных пользователей.")
        return
    users = []
    for uid in banned_ids:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT user_name FROM products WHERE user_id = %s LIMIT 1", (uid,))
                    name = cur.fetchone()
                    users.append((uid, name[0] if name else "Неизвестно"))
        except Exception as e:
            logger.error(f"Ошибка при получении имени забаненного {uid}: {e}")
            users.append((uid, "Неизвестно"))
    lines = [f"{i}. {name} (ID: {user_id})" for i, (user_id, name) in enumerate(users, 1)]
    msg = "Выберите пользователя для разблокировки:\n" + "\n".join(lines)
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
    )
    user_state[update.effective_user.id] = {
        'step': 'selecting_user_to_unban',
        'users': users
    }

async def edit_product_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    products = get_user_products(user_id)
    if not products:
        await update.message.reply_text("У вас нет добавленных товаров.")
        return

    lines = [
        f"{i}. {name} → {cat} — {date.strftime('%d.%m.%Y')}"
        for i, (pid, name, cat, date, photo) in enumerate(products, 1)
    ]
    msg = "Выберите товар для редактирования:\n" + "\n".join(lines)
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
    )
    user_state[user_id] = {
        'step': 'selecting_product_to_edit',
        'products': products
    }

async def show_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        product_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.message.reply_text("Некорректный запрос.")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.product_name, p.created_at, p.user_name, p.rating, c.name as category_name, p.photo_file_id
                    FROM products p
                    JOIN categories c ON p.category_id = c.id
                    WHERE p.id = %s
                """, (product_id,))
                row = cur.fetchone()
        if not row or not row[5]:
            await query.message.reply_text("Фото не найдено.")
            return
        name, created_at, user_name, rating, category_name, photo_file_id = row
        date_display = created_at.strftime('%d.%m.%Y')
        caption = f"{name}\nКатегория: {category_name}\nОценка: {rating}\nДата: {date_display}\nАвтор: {user_name}"
        try:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_file_id, caption=caption)
        except TelegramError as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await query.message.reply_text("Не удалось загрузить фото.")
    except Exception as e:
        logger.error(f"Ошибка в show_photo_callback: {e}")
        await query.message.reply_text("Произошла ошибка при загрузке фото.")


@banned_user_check
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user_exists(user_id)
    await update.message.reply_text("Привет!", reply_markup=get_main_menu(user_id))

@banned_user_check
async def help_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🔸 Как пользоваться приложением?\n\n"
        "Бот работает по принципу 'запрос-ответ' посредством отправки команд в чат, либо нажатия кнопки в меню, которая отправит команду за Вас\n"
        "* Если хотите добавить новый товар, нажмите 'Добавить товар', выберите категорию, (если подходящая категория отсутствует, нажмите 'Другое', после чего введите название категории и отправьте боту), далее необходимо прикрепить фото продукта с описанием, либо просто название и отправить. Затем выберите оценку\n"
        "* Если хотите ознакомиться с хорошими товарами - нажмите 'Покупать', затем выберите интересующую категорию. Отобразиться список продуктов, под некоторым из них будет значок фотоаппарата - при нажатии на него бот пришлет фото продукта с описанием\n"
        "* Если хотите ознакомиться с плохими товарами - - нажмите 'Не покупать', затем выберите интересующую категорию. Отобразиться список продуктов, под некоторым из них будет значок фотоаппарата - при нажатии на него бот пришлет фото продукта с описанием\n\n"
        "Если во время работы в боте исчезли кнопки - 'достать' их можно, нажав на значок квадрата с четырьмя точками(кружками) внутри. Находится он в строке ввода сообщения\n\n"
        "🛠️ Команды:\n\n"
        "/change_cat — изменить название категории\n"
        "/change_list — переместить товар в другую категорию\n"
        "/edit_product — изменить свой товар\n"
        "/del_position — удалить товар из списка\n\n"
        # Добавь сюда другие пользовательские команды, если появятся
    )
    await update.message.reply_text(help_text, reply_markup=get_main_menu(update.effective_user.id))

# === Основной обработчик текста ===

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        return
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if is_user_banned(user_id) and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return

    ensure_user_exists(user_id)

    # === Сброс состояния при нажатии кнопок главного меню ===
    main_menu_triggers = ["➕   Добавить товар", "✅   Покупать", "❌   Не покупать", "🔔", "🔕", "я Лена"]
    if text in main_menu_triggers:
        if user_id in user_state:
            del user_state[user_id]

    # Получаем АКТУАЛЬНОЕ состояние ПОСЛЕ возможного сброса
    current_state = user_state.get(user_id, {})

    # Обработка кнопки уведомлений
    if text in ("🔔", "🔕"):
        new_status = toggle_notifications(user_id)
        icon = "🔔" if new_status else "🔕"
        status_text = "включены" if new_status else "отключены"
        await update.message.reply_text(f"{icon} Уведомления {status_text}.", reply_markup=get_main_menu(user_id))
        return

    # === Админ-состояния ===
    admin_steps = ['selecting_user_to_delete', 'selecting_user_to_ban', 'selecting_user_to_unban']
    if current_state.get('step') in admin_steps:
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

    if current_state.get('step') == 'selecting_user_to_delete':
        users = current_state.get('users', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(users):
                target_id, _ = users[idx - 1]
                try:
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM products WHERE user_id = %s", (target_id,))
                            cur.execute("DELETE FROM users WHERE user_id = %s", (target_id,))
                            conn.commit()
                    await update.message.reply_text(f"Пользователь {target_id} удалён.")
                except Exception as e:
                    logger.error(f"Ошибка удаления пользователя {target_id}: {e}")
                    await update.message.reply_text("Ошибка при удалении пользователя.")
            else:
                await update.message.reply_text("Неверный номер.")
        else:
            await update.message.reply_text("Введите номер пользователя.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'selecting_category_to_rename':
        categories = get_categories()
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(categories):
                cat_id, cat_name = categories[idx - 1]
                await update.message.reply_text(f"Текущее название: {cat_name}\nВведите новое название:")
                user_state[user_id] = {
                    'step': 'entering_new_category_name',
                    'category_id': cat_id
                }
            else:
                await update.message.reply_text("Неверный номер категории.")
        else:
            await update.message.reply_text("Введите номер категории.")
        return

    if current_state.get('step') == 'selecting_user_to_ban':
        users = current_state.get('users', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(users):
                target_id, _ = users[idx - 1]
                try:
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO users (user_id, is_banned)
                                VALUES (%s, TRUE)
                                ON CONFLICT (user_id) DO UPDATE SET is_banned = TRUE
                            """, (target_id,))
                            conn.commit()
                    await update.message.reply_text(f"Пользователь {target_id} забанен.")
                except Exception as e:
                    logger.error(f"Ошибка бана пользователя {target_id}: {e}")
                    await update.message.reply_text("Ошибка при блокировке пользователя.")
            else:
                await update.message.reply_text("Неверный номер.")
        else:
            await update.message.reply_text("Введите номер пользователя.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'selecting_user_to_unban':
        users = current_state.get('users', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(users):
                target_id, _ = users[idx - 1]
                try:
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO users (user_id, is_banned)
                                VALUES (%s, FALSE)
                                ON CONFLICT (user_id) DO UPDATE SET is_banned = FALSE
                            """, (target_id,))
                            conn.commit()
                    await update.message.reply_text(f"Пользователь {target_id} разбанен.")
                except Exception as e:
                    logger.error(f"Ошибка разбана пользователя {target_id}: {e}")
                    await update.message.reply_text("Ошибка при разблокировке пользователя.")
            else:
                await update.message.reply_text("Неверный номер.")
        else:
            await update.message.reply_text("Введите номер пользователя.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'entering_new_category_name':
        new_name = text.strip()
        if new_name:
            update_category_name(current_state['category_id'], new_name)
            await update.message.reply_text(f"Категория переименована в: {new_name}")
        else:
            await update.message.reply_text("Название не может быть пустым.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'selecting_product_to_move':
        products = current_state.get('products', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(products):
                product_id, _, _, _ = products[idx - 1]
                cat_list = get_categories()
                if not cat_list:
                    await update.message.reply_text("Нет категорий для перемещения.")
                    if user_id in user_state:
                        del user_state[user_id]
                    return
                lines = [f"{i}. {name}" for i, (cid, name) in enumerate(cat_list, 1)]
                msg = "Выберите новую категорию:\n" + "\n".join(lines)
                await update.message.reply_text(msg)
                user_state[user_id] = {
                    'step': 'selecting_new_category_for_product',
                    'product_id': product_id,
                    'categories': cat_list
                }
            else:
                await update.message.reply_text("Неверный номер товара.")
        else:
            await update.message.reply_text("Введите номер товара.")
        return

    if current_state.get('step') == 'selecting_product_to_delete':
        products = current_state.get('products', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(products):
                product_id, _, _, _ = products[idx - 1]
                delete_product(product_id)
                await update.message.reply_text("Товар удалён!")
            else:
                await update.message.reply_text("Неверный номер товара.")
        else:
            await update.message.reply_text("Введите номер товара.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'selecting_new_category_for_product':
        cat_list = current_state.get('categories', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(cat_list):
                new_cat_id, _ = cat_list[idx - 1]
                move_product_to_category(current_state['product_id'], new_cat_id)
                await update.message.reply_text("Товар перемещён!")
            else:
                await update.message.reply_text("Неверный номер категории.")
        else:
            await update.message.reply_text("Введите номер категории.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'selecting_product_to_edit':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        products = current_state.get('products', [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(products):
                product_id, _, _, _, _ = products[idx - 1]
                keyboard = [["Изменить название", "Изменить фото"], ["Назад"]]
                await update.message.reply_text(
                    "Что вы хотите изменить?",
                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
                )
                user_state[user_id] = {
                    'step': 'choosing_edit_field',
                    'product_id': product_id
                }
            else:
                await update.message.reply_text("Неверный номер товара.")
        else:
            await update.message.reply_text("Введите номер товара.")
        return

    if current_state.get('step') == 'choosing_edit_field':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        product_id = current_state.get('product_id')
        if text == "Изменить название":
            await update.message.reply_text(
                "Введите новое название товара:",
                reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
            )
            user_state[user_id] = {
                'step': 'editing_product_name',
                'product_id': product_id
            }
        elif text == "Изменить фото":
            await update.message.reply_text(
                "Отправьте новое фото (можно без подписи):",
                reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
            )
            user_state[user_id] = {
                'step': 'editing_product_photo',
                'product_id': product_id
            }
        else:
            await update.message.reply_text("Пожалуйста, выберите действие.")
        return

    if current_state.get('step') == 'editing_product_name':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        product_id = current_state.get('product_id')
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE products SET product_name = %s WHERE id = %s", (text.strip(), product_id))
                    conn.commit()
            await update.message.reply_text("✅ Название товара обновлено!")
        except Exception as e:
            logger.error(f"Ошибка обновления названия товара {product_id}: {e}")
            await update.message.reply_text("❌ Не удалось обновить название.")
        if user_id in user_state:
            del user_state[user_id]
        return

    if current_state.get('step') == 'adding_category':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        if text.strip():
            category_id = add_category(text.strip())
            if category_id is None:
                await update.message.reply_text("Недопустимое название категории. Попробуйте снова.")
                return
            user_state[user_id] = user_state.get(user_id, {})
            user_state[user_id]['step'] = 'choosing_category_for_add'
            user_state[user_id]['mode'] = 'add'
            msg = format_category_list()
            await update.message.reply_text(msg, reply_markup=get_category_keyboard(show_other=True, show_back=True))
        else:
            await update.message.reply_text("Название категории не может быть пустым. Попробуйте снова:")
        return

    if current_state.get('step') in ['choosing_category_for_add', 'choosing_category_for_view']:
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        categories = get_categories()
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(categories):
                selected_category_id = categories[idx - 1][0]
                mode = current_state['mode']

                if mode == 'add':
                    # Сохраняем category_id в user_state (не перезаписываем!)
                    user_state[user_id] = user_state.get(user_id, {})
                    user_state[user_id]['category_id'] = selected_category_id
                    user_state[user_id]['step'] = 'awaiting_product_name'
                    await update.message.reply_text(
                        "Прикрепите фотографию и введите название товара:",
                        reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
                    )
                elif mode in ['recommend', 'avoid']:
                    rating = 'Отлично' if mode == 'recommend' else 'Плохо'
                    products = get_products_by_category_and_rating(selected_category_id, rating)
                    if user_id in user_state:
                        del user_state[user_id]

                    if products:
                        for p in products:
                            name = p['product_name']
                            created_at = p['created_at']
                            date_display = created_at.strftime('%d.%m.%Y')
                            user_name = p['user_name']
                            product_id = p['id']
                            photo_exists = p.get('photo_file_id') is not None

                            text_msg = f"• {name} — {date_display} ({user_name})"
                            if photo_exists:
                                keyboard = [[InlineKeyboardButton("📸", callback_data=f"show_photo_{product_id}")]]
                                reply_markup = InlineKeyboardMarkup(keyboard)
                                await update.message.reply_text(text_msg, reply_markup=reply_markup)
                            else:
                                await update.message.reply_text(text_msg)
                    else:
                        response = f"В категории '{categories[idx - 1][1]}' нет позиций."
                        await update.message.reply_text(response)

                    await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
                return
            else:
                await update.message.reply_text("Неверный номер категории. Попробуйте снова.")
                return
        elif text == "Другое" and current_state['step'] == 'choosing_category_for_add':
            await update.message.reply_text("Введите название новой категории:")
            user_state[user_id] = {'step': 'adding_category'}
            return
        else:
            await update.message.reply_text("Пожалуйста, выберите номер категории, 'Другое' или 'Назад'.")
            return

    if current_state.get('step') == 'awaiting_product_name':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        # В handle_text мы получаем ТОЛЬКО текст — фото обрабатывается в handle_photo
        photo_file_id = current_state.get('photo_file_id')

        if photo_file_id is not None:
            # Фото уже загружено ранее — этот текст = название для него
            product_name = text
            user_state[user_id]['product_name'] = product_name
            # photo_file_id остаётся
        else:
            # Чисто текстовый товар
            product_name = text
            user_state[user_id]['product_name'] = product_name
            user_state[user_id]['photo_file_id'] = None

        await update.message.reply_text(
            "Выберите оценку:",
            reply_markup=ReplyKeyboardMarkup([["Отлично", "Плохо"], ["Назад"]], resize_keyboard=True,
                                             one_time_keyboard=False)
        )
        user_state[user_id]['step'] = 'awaiting_rating'
        return

    elif current_state.get('step') == 'editing_product_photo':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        photo_file_id = update.message.photo[-1].file_id
        product_id = current_state.get('product_id')
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE products SET photo_file_id = %s WHERE id = %s", (photo_file_id, product_id))
                    conn.commit()
            await update.message.reply_text("✅ Фото товара обновлено!")
        except Exception as e:
            logger.error(f"Ошибка обновления фото товара {product_id}: {e}")
            await update.message.reply_text("❌ Не удалось обновить фото.")
        if user_id in user_state:
            del user_state[user_id]

    if current_state.get('step') == 'awaiting_rating':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        if text in ["Отлично", "Плохо"]:
            product_name = current_state.get('product_name', 'Неизвестно')
            category_id = current_state.get('category_id')
            user_name = update.effective_user.full_name or "Неизвестно"
            photo_file_id = current_state.get('photo_file_id')

            if not category_id:
                await update.message.reply_text("Ошибка: категория не выбрана. Начните заново.")
                if user_id in user_state:
                    del user_state[user_id]
                return

            save_product(user_id, user_name, category_id, product_name, text, photo_file_id)

            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT name FROM categories WHERE id = %s", (category_id,))
                        category_name = cur.fetchone()[0]
            except Exception as e:
                logger.error(f"Ошибка получения имени категории {category_id}: {e}")
                category_name = "Неизвестно"

            subscribers = get_subscribers(exclude_user_id=user_id)
            for sub_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=sub_id,
                        text=f"🆕 Новый товар в категории «{category_name}»:\n• {product_name} — {text}\n(добавил: {user_name})"
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление пользователю {sub_id}: {e}")

            await update.message.reply_text("Товар сохранён!", reply_markup=get_main_menu(user_id))
        else:
            await update.message.reply_text("Пожалуйста, выберите 'Отлично', 'Плохо' или 'Назад'.")
        if user_id in user_state:
            del user_state[user_id]
        return

    # Обработка главного меню
    if text == "я Лена":
        await update.message.reply_text("Бесишь. Не пиши мне.", reply_markup=get_main_menu(user_id))
    elif text == "❔   Справка":
        await help_user_command(update, context)
        return  # Важно: не дублировать меню дважды
    elif text == "➕   Добавить товар":
        user_state[user_id] = {
            'step': 'choosing_category_for_add',
            'mode': 'add'
        }
        msg = format_category_list(mode=None)
        await update.message.reply_text(msg, reply_markup=get_category_keyboard(show_other=True, show_back=True))
    elif text == "✅   Покупать":
        user_state[user_id] = {
            'step': 'choosing_category_for_view',
            'mode': 'recommend'
        }
        msg = format_category_list(mode='recommend')
        await update.message.reply_text(msg, reply_markup=get_category_keyboard(show_other=False, show_back=True))
    elif text == "❌   Не покупать":
        user_state[user_id] = {
            'step': 'choosing_category_for_view',
            'mode': 'avoid'
        }
        msg = format_category_list(mode='avoid')
        await update.message.reply_text(msg, reply_markup=get_category_keyboard(show_other=False, show_back=True))
    else:
        await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))


# === Обработчик фото ===

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return
    user_id = update.effective_user.id

    if is_user_banned(user_id) and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return

    ensure_user_exists(user_id)
    current_state = user_state.get(user_id, {})

    if current_state.get('step') == 'awaiting_product_name':
        if not update.message.caption:
            # Сохраняем фото, но остаёмся в том же состоянии
            user_state[user_id] = user_state.get(user_id, {})
            user_state[user_id]['photo_file_id'] = update.message.photo[-1].file_id
            await update.message.reply_text(
                "Пожалуйста, укажите название товара (добавьте подпись к фото).",
                reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
            )
            return  # Не меняем шаг!

        # Фото с подписью — сохраняем всё сразу
        photo_file_id = update.message.photo[-1].file_id
        product_name = update.message.caption.strip()

        user_state[user_id] = user_state.get(user_id, {})
        user_state[user_id]['product_name'] = product_name
        user_state[user_id]['photo_file_id'] = photo_file_id
        user_state[user_id]['step'] = 'awaiting_rating'

        await update.message.reply_text(
            "Выберите оценку:",
            reply_markup=ReplyKeyboardMarkup([["Отлично", "Плохо"], ["Назад"]], resize_keyboard=True,
                                             one_time_keyboard=False)
        )
    else:
        await update.message.reply_text("Пожалуйста, используйте кнопки меню.")


# === Запуск ===

def main():
    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear_all", clear_all_command))
    app.add_handler(CommandHandler("change_cat", change_cat_command))
    app.add_handler(CommandHandler("change_list", change_list_command))
    app.add_handler(CommandHandler("del_position", del_position_command))
    app.add_handler(CommandHandler("del_user", del_user_command))
    app.add_handler(CommandHandler("ban_user", ban_user_command))
    app.add_handler(CommandHandler("unban_user", unban_user_command))
    app.add_handler(CommandHandler("edit_product", edit_product_command))
    app.add_handler(CallbackQueryHandler(show_photo_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    PORT = int(os.environ.get("PORT", 10000))
    PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not PUBLIC_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL not set")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=PUBLIC_URL
    )


if __name__ == "__main__":
    main()