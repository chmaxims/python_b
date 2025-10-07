# bot.py — финальная версия для Render (как твой main.py, но с PostgreSQL)
import os
import psycopg2
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Настройки
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

conn = None

def init_db():
    global conn
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
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

        # Миграция: добавляем photo_file_id, если отсутствует
        try:
            cur.execute("ALTER TABLE products ADD COLUMN photo_file_id TEXT;")
            conn.commit()
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()
        except Exception as e:
            conn.rollback()

        conn.commit()
    print("✅ База данных инициализирована")

# === Функции из main.py (адаптированы под PostgreSQL) ===

def get_notification_status(user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT notifications_enabled FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users (user_id, notifications_enabled) VALUES (%s, %s)", (user_id, True))
            conn.commit()
            return True
        return row[0]

def toggle_notifications(user_id):
    current = get_notification_status(user_id)
    new_status = not current
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (user_id, notifications_enabled)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET notifications_enabled = %s
        """, (user_id, new_status, new_status))
        conn.commit()
    return new_status

def get_subscribers(exclude_user_id=None):
    with conn.cursor() as cur:
        if exclude_user_id is not None:
            cur.execute("SELECT user_id FROM users WHERE notifications_enabled = TRUE AND user_id != %s", (exclude_user_id,))
        else:
            cur.execute("SELECT user_id FROM users WHERE notifications_enabled = TRUE")
        return [row[0] for row in cur.fetchall()]

def get_categories():
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM categories ORDER BY name")
        return cur.fetchall()

def add_category(name):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        cur.execute("SELECT id FROM categories WHERE name = %s", (name,))
        conn.commit()
        return cur.fetchone()[0]

def save_product(user_id, user_name, category_id, product_name, rating, photo_file_id=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO products (user_id, user_name, category_id, product_name, photo_file_id, rating)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, user_name, category_id, product_name, photo_file_id, rating))
        conn.commit()

def get_products_by_category_and_rating(category_id, rating):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_name, created_at, user_name, id, photo_file_id
            FROM products 
            WHERE category_id = %s AND rating = %s
            ORDER BY created_at DESC
        """, (category_id, rating))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

def get_all_products_with_categories():
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.product_name, c.name, p.created_at
            FROM products p 
            JOIN categories c ON p.category_id = c.id
            ORDER BY c.name, p.product_name
        """)
        return cur.fetchall()

def get_all_users():
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

def update_category_name(category_id, new_name):
    with conn.cursor() as cur:
        cur.execute("UPDATE categories SET name = %s WHERE id = %s", (new_name, category_id))
        conn.commit()

def move_product_to_category(product_id, new_category_id):
    with conn.cursor() as cur:
        cur.execute("UPDATE products SET category_id = %s WHERE id = %s", (new_category_id, product_id))
        conn.commit()

def delete_product(product_id):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
        conn.commit()

def clear_all_data():
    with conn.cursor() as cur:
        cur.execute("DELETE FROM products")
        cur.execute("DELETE FROM categories")
        conn.commit()

# === Глобальное состояние (как в main.py) ===
user_state = {}

def get_main_menu(user_id=None):
    if user_id is not None:
        notifications_on = get_notification_status(user_id)
        notify_icon = "🔔" if notifications_on else "🔕"
    else:
        notify_icon = "🔔"
    keyboard = [
        ["➕   Добавить товар", notify_icon],
        ["✅   Покупать", "❌   Не покупать"]
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

# === Обработчики команд ===

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🛠️ Админ-команды:\n\n"
        "/clear_all — очистить всю базу данных\n"
        "/change_cat — изменить название категории\n"
        "/change_list — переместить товар в другую категорию\n"
        "/del_position — удалить товар из списка\n"
        "/del_user <id> — удалить пользователя\n"
        "/ban_user <id> — забанить пользователя\n"
        "/unban_user <id> — разбанить пользователя\n"
    )
    await update.message.reply_text(help_text)

async def clear_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_all_data()
    await update.message.reply_text("🗑️ База данных очищена.")

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

async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE is_banned = TRUE")
        banned_ids = [row[0] for row in cur.fetchall()]
    if not banned_ids:
        await update.message.reply_text("Нет забаненных пользователей.")
        return
    users = []
    for uid in banned_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT user_name FROM products WHERE user_id = %s LIMIT 1", (uid,))
            name = cur.fetchone()
            users.append((uid, name[0] if name else "Неизвестно"))
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

async def show_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])
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
    await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_file_id, caption=caption)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("Привет!", reply_markup=get_main_menu(user_id))

# === Основной обработчик текста ===

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        return
    text = update.message.text.strip()
    user_id = update.effective_user.id

    with conn.cursor() as cur:
        cur.execute("SELECT is_banned FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    if row and row[0] and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return

    get_notification_status(user_id)
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
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM products WHERE user_id = %s", (target_id,))
                    cur.execute("DELETE FROM users WHERE user_id = %s", (target_id,))
                    conn.commit()
                await update.message.reply_text(f"Пользователь {target_id} удалён.")
            else:
                await update.message.reply_text("Неверный номер.")
        else:
            await update.message.reply_text("Введите номер пользователя.")
        if user_id in user_state:
            del user_state[user_id]
        return

            #=== Обработка изменения категории ===
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
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (user_id, is_banned)
                        VALUES (%s, TRUE)
                        ON CONFLICT (user_id) DO UPDATE SET is_banned = TRUE
                    """, (target_id,))
                    conn.commit()
                await update.message.reply_text(f"Пользователь {target_id} забанен.")
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
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (user_id, is_banned)
                        VALUES (%s, FALSE)
                        ON CONFLICT (user_id) DO UPDATE SET is_banned = FALSE
                    """, (target_id,))
                    conn.commit()
                await update.message.reply_text(f"Пользователь {target_id} разбанен.")
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

    if current_state.get('step') == 'adding_category':
        if text.strip():
            category_id = add_category(text.strip())
            user_state[user_id] = {
                'step': 'choosing_category_for_add',
                'mode': 'add'
            }
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
                    context.user_data['category_id'] = selected_category_id
                    await update.message.reply_text(
                        "Введите название товара:",
                        reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
                    )
                    user_state[user_id] = {'step': 'awaiting_product_name'}
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

        photo_file_id = None
        product_name = None

        if update.message.photo:
            if update.message.caption:
                photo_file_id = update.message.photo[-1].file_id
                product_name = update.message.caption.strip()
            else:
                await update.message.reply_text(
                    "Пожалуйста, укажите название товара (добавьте подпись к фото).",
                    reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
                )
                return
        else:
            product_name = text

        context.user_data['product_name'] = product_name
        context.user_data['photo_file_id'] = photo_file_id

        await update.message.reply_text(
            "Выберите оценку:",
            reply_markup=ReplyKeyboardMarkup([["Отлично", "Плохо"], ["Назад"]], resize_keyboard=True, one_time_keyboard=False)
        )
        user_state[user_id] = {'step': 'awaiting_rating'}
        return

    if current_state.get('step') == 'awaiting_rating':
        if text == "Назад":
            if user_id in user_state:
                del user_state[user_id]
            await update.message.reply_text("Выберите действие:", reply_markup=get_main_menu(user_id))
            return

        if text in ["Отлично", "Плохо"]:
            product_name = context.user_data.get('product_name', 'Неизвестно')
            category_id = context.user_data.get('category_id')
            user_name = update.effective_user.full_name or "Неизвестно"
            photo_file_id = context.user_data.get('photo_file_id')
            save_product(user_id, user_name, category_id, product_name, text, photo_file_id)

            with conn.cursor() as cur:
                cur.execute("SELECT name FROM categories WHERE id = %s", (category_id,))
                category_name = cur.fetchone()[0]

            subscribers = get_subscribers(exclude_user_id=user_id)
            for sub_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=sub_id,
                        text=f"🆕 Новый товар в категории «{category_name}»:\n• {product_name} — {text}\n(добавил: {user_name})"
                    )
                except Exception as e:
                    print(f"Не удалось отправить уведомление пользователю {sub_id}: {e}")

            await update.message.reply_text("Товар сохранён!", reply_markup=get_main_menu(user_id))
        else:
            await update.message.reply_text("Пожалуйста, выберите 'Отлично', 'Плохо' или 'Назад'.")
        if user_id in user_state:
            del user_state[user_id]
        return

    # Обработка главного меню
    if text == "я Лена":
        await update.message.reply_text("Бесишь. Не пиши мне.", reply_markup=get_main_menu(user_id))
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

    with conn.cursor() as cur:
        cur.execute("SELECT is_banned FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    if row and row[0] and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return

    get_notification_status(user_id)
    current_state = user_state.get(user_id, {})

    if current_state.get('step') == 'awaiting_product_name':
        if not update.message.caption:
            await update.message.reply_text(
                "Пожалуйста, укажите название товара (добавьте подпись к фото).",
                reply_markup=ReplyKeyboardMarkup([["Назад"]], resize_keyboard=True, one_time_keyboard=False)
            )
            return

        photo_file_id = update.message.photo[-1].file_id
        product_name = update.message.caption.strip()

        context.user_data['product_name'] = product_name
        context.user_data['photo_file_id'] = photo_file_id

        await update.message.reply_text(
            "Выберите оценку:",
            reply_markup=ReplyKeyboardMarkup([["Отлично", "Плохо"], ["Назад"]], resize_keyboard=True, one_time_keyboard=False)
        )
        user_state[user_id] = {'step': 'awaiting_rating'}
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
    app.add_handler(CallbackQueryHandler(show_photo_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    PORT = int(os.environ.get("PORT", 10000))
    PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-bot.onrender.com").strip()
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=PUBLIC_URL
    )

if __name__ == "__main__":
    main()