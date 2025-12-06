import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import json
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from telegram.constants import ParseMode
import asyncpg
from cryptography.fernet import Fernet
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ==================== CONFIGURATION ====================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")  # Neon PostgreSQL
    ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
    PORT = int(os.getenv("PORT", 8080))
    USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    
    # Paths
    HEADER_IMAGES_DIR = "head"
    
    # Injection limits
    FREE_DAILY_LIMIT = 1
    VIP_FEATURES = {
        "unlimited_injects": True,
        "all_cars": True,
        "all_liveries": True,
        "max_resources": 999999
    }

# ==================== DATABASE MODELS ====================
class Database:
    def __init__(self):
        self.pool = None
        
    async def connect(self):
        """Connect to PostgreSQL database"""
        self.pool = await asyncpg.create_pool(Config.DATABASE_URL)
        await self.init_tables()
        
    async def init_tables(self):
        """Initialize database tables"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_vip BOOLEAN DEFAULT FALSE,
                    vip_until TIMESTAMP,
                    free_used_today INTEGER DEFAULT 0,
                    last_reset_date DATE DEFAULT CURRENT_DATE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_tokens (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                    encrypted_token TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS injection_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    item_type TEXT,
                    item_id TEXT,
                    item_name TEXT,
                    success BOOLEAN,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS vip_packages (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    days INTEGER,
                    price REAL,
                    description TEXT,
                    is_active BOOLEAN DEFAULT TRUE
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS admin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
    async def get_user(self, user_id: int):
        """Get user data"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                'SELECT * FROM users WHERE user_id = $1',
                user_id
            )
            
    async def create_or_update_user(self, user_id: int, username: str, 
                                   first_name: str, last_name: str = ""):
        """Create or update user record"""
        async with self.pool.acquire() as conn:
            user = await self.get_user(user_id)
            if user:
                await conn.execute('''
                    UPDATE users 
                    SET username = $2, first_name = $3, last_name = $4,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                ''', user_id, username, first_name, last_name)
            else:
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name)
                    VALUES ($1, $2, $3, $4)
                ''', user_id, username, first_name, last_name)
                
    async def update_vip_status(self, user_id: int, days: int):
        """Update user VIP status"""
        async with self.pool.acquire() as conn:
            user = await self.get_user(user_id)
            if user and user['vip_until'] and user['vip_until'] > datetime.now():
                new_date = user['vip_until'] + timedelta(days=days)
            else:
                new_date = datetime.now() + timedelta(days=days)
                
            await conn.execute('''
                UPDATE users 
                SET is_vip = TRUE, vip_until = $2,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1
            ''', user_id, new_date)
            
    async def remove_vip(self, user_id: int):
        """Remove VIP status"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE users 
                SET is_vip = FALSE, vip_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1
            ''', user_id)
            
    async def get_vip_users(self):
        """Get all VIP users"""
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT * FROM users 
                WHERE is_vip = TRUE AND vip_until > CURRENT_TIMESTAMP
                ORDER BY vip_until DESC
            ''')
            
    async def can_inject_free(self, user_id: int):
        """Check if free user can inject today"""
        async with self.pool.acquire() as conn:
            user = await self.get_user(user_id)
            if not user:
                return False
                
            # Reset daily counter if new day
            if user['last_reset_date'] != datetime.now().date():
                await conn.execute('''
                    UPDATE users 
                    SET free_used_today = 0, last_reset_date = CURRENT_DATE
                    WHERE user_id = $1
                ''', user_id)
                return True
                
            return user['free_used_today'] < Config.FREE_DAILY_LIMIT
            
    async def increment_free_usage(self, user_id: int):
        """Increment free user daily usage"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE users 
                SET free_used_today = free_used_today + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1
            ''', user_id)
            
    async def log_injection(self, user_id: int, item_type: str, 
                           item_id: str, item_name: str, success: bool):
        """Log injection attempt"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO injection_logs 
                (user_id, item_type, item_id, item_name, success)
                VALUES ($1, $2, $3, $4, $5)
            ''', user_id, item_type, item_id, item_name, success)
            
    async def get_user_stats(self, user_id: int):
        """Get user statistics"""
        async with self.pool.acquire() as conn:
            stats = await conn.fetchrow('''
                SELECT 
                    COUNT(*) as total_injections,
                    SUM(CASE WHEN success THEN 1 ELSE 0 END) as successful_injections
                FROM injection_logs 
                WHERE user_id = $1
            ''', user_id)
            
            user = await self.get_user(user_id)
            return {
                'total_injections': stats['total_injections'] or 0,
                'successful_injections': stats['successful_injections'] or 0,
                'is_vip': user['is_vip'] if user else False,
                'vip_until': user['vip_until'] if user else None,
                'free_used_today': user['free_used_today'] if user else 0
            }

# ==================== ENCRYPTION SERVICE ====================
class EncryptionService:
    def __init__(self):
        self.cipher = Fernet(Config.ENCRYPTION_KEY.encode())
        
    def encrypt_token(self, token: str) -> str:
        """Encrypt user token"""
        return self.cipher.encrypt(token.encode()).decode()
        
    def decrypt_token(self, encrypted_token: str) -> str:
        """Decrypt user token"""
        return self.cipher.decrypt(encrypted_token.encode()).decode()

# ==================== INJECTION SERVICE ====================
class InjectionService:
    def __init__(self):
        self.cars_db = self.load_cars_database()
        self.liveries_db = self.load_liveries_database()
        self.encryption = EncryptionService()
        self.db = Database()
        
    def load_cars_database(self) -> Dict:
        """Load cars database from URL"""
        try:
            url = "https://gist.githubusercontent.com/R3XBASE/455316165066c65564121647db913f28/raw/oloCarDB.json"
            response = requests.get(url, timeout=10)
            return response.json()
        except:
            return {}
            
    def load_liveries_database(self) -> Dict:
        """Load liveries database from URL"""
        try:
            url = "https://gist.githubusercontent.com/R3XBASE/b0b9dcde1994d25a5257d8ccfa0c7939/raw/livery_db.json"
            response = requests.get(url, timeout=10)
            return response.json()
        except:
            return {}
            
    async def inject_item(self, user_id: int, item_id: str, item_type: str) -> Dict:
        """Inject item for user"""
        user = await self.db.get_user(user_id)
        if not user:
            return {"success": False, "message": "User not found"}
            
        # Check if free user can inject
        if not user['is_vip']:
            can_inject = await self.db.can_inject_free(user_id)
            if not can_inject:
                return {
                    "success": False, 
                    "message": "Daily free limit reached! Upgrade to VIP for unlimited injections."
                }
                
        # Get user token
        async with self.db.pool.acquire() as conn:
            token_data = await conn.fetchrow(
                'SELECT encrypted_token FROM user_tokens WHERE user_id = $1',
                user_id
            )
            
        if not token_data:
            return {"success": False, "message": "Please set your token first using /settoken"}
            
        # Decrypt token
        try:
            token = self.encryption.decrypt_token(token_data['encrypted_token'])
        except:
            return {"success": False, "message": "Invalid token format"}
            
        # Perform injection (simplified - adapt from original code)
        result = await self._perform_injection(token, item_id, item_type)
        
        # Log injection
        item_name = self.cars_db.get(item_id, {}).get('name', item_id)
        await self.db.log_injection(user_id, item_type, item_id, item_name, result['success'])
        
        # Increment free usage if not VIP
        if not user['is_vip'] and result['success']:
            await self.db.increment_free_usage(user_id)
            
        return result
        
    async def _perform_injection(self, token: str, item_id: str, item_type: str) -> Dict:
        """Actual injection logic (adapted from original)"""
        # This is a simplified version - you should adapt the full logic from fullInjects.py
        try:
            url = "https://be38c.playfabapi.com/Client/ExecuteCloudScript"
            headers = {
                'X-Authorization': token,
                'Content-Type': 'application/json'
            }
            
            payload = {
                "FunctionName": "ExecuteGrantItems",
                "FunctionParameter": {"itemIds": [item_id]},
                "GeneratePlayStreamEvent": False
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            
            if response.status_code == 200:
                return {"success": True, "message": "✅ Injection successful!"}
            else:
                return {"success": False, "message": "❌ Injection failed"}
                
        except Exception as e:
            return {"success": False, "message": f"❌ Error: {str(e)}"}

# ==================== TELEGRAM BOT ====================
class InjectionBot:
    def __init__(self):
        self.db = Database()
        self.injection_service = InjectionService()
        self.scheduler = AsyncIOScheduler()
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        user = update.effective_user
        await self.db.create_or_update_user(
            user.id, user.username, user.first_name, user.last_name or ""
        )
        
        # Send header image if exists
        header_path = self._get_header_image()
        if header_path and os.path.exists(header_path):
            with open(header_path, 'rb') as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=f"🚀 *Welcome to Injection Bot v4.0!*\n\n"
                           f"👤 *User:* {user.mention_markdown_v2()}\n"
                           f"🆔 *ID:* `{user.id}`\n\n"
                           f"💎 *Features:*\n"
                           f"• Free: {Config.FREE_DAILY_LIMIT} injection/day\n"
                           f"• VIP: Unlimited injections\n"
                           f"• All cars & liveries\n"
                           f"• Resource injection\n\n"
                           f"Use /menu to see all options!",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=self._get_main_keyboard(user.id)
                )
        else:
            await update.message.reply_text(
                f"🚀 *Welcome to Injection Bot v4.0!*\n\n"
                f"👤 *User:* {user.mention_markdown_v2()}\n"
                f"🆔 *ID:* `{user.id}`\n\n"
                f"💎 *Features:*\n"
                f"• Free: {Config.FREE_DAILY_LIMIT} injection/day\n"
                f"• VIP: Unlimited injections\n"
                f"• All cars & liveries\n"
                f"• Resource injection\n\n"
                f"Use /menu to see all options!",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=self._get_main_keyboard(user.id)
            )
            
    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show main menu"""
        keyboard = [
            [
                InlineKeyboardButton("🚗 Inject Cars", callback_data="menu_cars"),
                InlineKeyboardButton("🎨 Inject Liveries", callback_data="menu_liveries")
            ],
            [
                InlineKeyboardButton("💰 Inject Resources", callback_data="menu_resources"),
                InlineKeyboardButton("📊 My Stats", callback_data="my_stats")
            ],
            [
                InlineKeyboardButton("💎 VIP Features", callback_data="vip_info"),
                InlineKeyboardButton("⚙️ Settings", callback_data="settings")
            ]
        ]
        
        # Add admin button if user is admin
        if update.effective_user.id in Config.ADMIN_IDS:
            keyboard.append([
                InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")
            ])
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📱 *MAIN MENU*\n\n"
            "Select an option below:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
        
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback queries"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        if query.data == "menu_cars":
            await self.show_cars_menu(query)
        elif query.data == "menu_liveries":
            await self.show_liveries_menu(query)
        elif query.data == "menu_resources":
            await self.show_resources_menu(query)
        elif query.data == "my_stats":
            await self.show_user_stats(query)
        elif query.data == "vip_info":
            await self.show_vip_info(query)
        elif query.data == "settings":
            await self.show_settings(query)
        elif query.data == "admin_panel":
            await self.show_admin_panel(query)
        elif query.data.startswith("car_page_"):
            page = int(query.data.split("_")[2])
            await self.show_cars_page(query, page)
        elif query.data.startswith("inject_car_"):
            car_id = query.data.split("_")[2]
            await self.inject_car(query, car_id)
            
    async def show_cars_menu(self, query):
        """Show cars menu with pagination"""
        cars = list(self.injection_service.cars_db.items())
        total_pages = (len(cars) + 9) // 10
        
        keyboard = []
        for i in range(min(10, len(cars))):
            car_id, car_data = cars[i]
            keyboard.append([
                InlineKeyboardButton(
                    f"🚗 {car_data.get('name', car_id)}",
                    callback_data=f"inject_car_{car_id}"
                )
            ])
            
        # Pagination buttons
        pagination_buttons = []
        if total_pages > 1:
            pagination_buttons.append(
                InlineKeyboardButton("◀️ Prev", callback_data="car_page_0")
            )
            pagination_buttons.append(
                InlineKeyboardButton(f"1/{total_pages}", callback_data="page_info")
            )
            pagination_buttons.append(
                InlineKeyboardButton("Next ▶️", callback_data=f"car_page_1")
            )
            
        if pagination_buttons:
            keyboard.append(pagination_buttons)
            
        keyboard.append([
            InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
        ])
        
        await query.edit_message_text(
            "🚗 *Select a Car to Inject*\n\n"
            f"Total cars available: {len(cars)}\n"
            "Click on a car to inject it:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    async def show_cars_page(self, query, page):
        """Show specific page of cars"""
        cars = list(self.injection_service.cars_db.items())
        total_pages = (len(cars) + 9) // 10
        start_idx = page * 10
        end_idx = min(start_idx + 10, len(cars))
        
        keyboard = []
        for i in range(start_idx, end_idx):
            car_id, car_data = cars[i]
            keyboard.append([
                InlineKeyboardButton(
                    f"🚗 {car_data.get('name', car_id)}",
                    callback_data=f"inject_car_{car_id}"
                )
            ])
            
        # Pagination buttons
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(
                InlineKeyboardButton("◀️ Prev", callback_data=f"car_page_{page-1}")
            )
            
        pagination_buttons.append(
            InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="page_info")
        )
        
        if page < total_pages - 1:
            pagination_buttons.append(
                InlineKeyboardButton("Next ▶️", callback_data=f"car_page_{page+1}")
            )
            
        if pagination_buttons:
            keyboard.append(pagination_buttons)
            
        keyboard.append([
            InlineKeyboardButton("🔙 Back", callback_data="menu_cars")
        ])
        
        await query.edit_message_text(
            f"🚗 *Cars Page {page+1}/{total_pages}*\n\n"
            f"Showing {start_idx+1}-{end_idx} of {len(cars)} cars",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    async def inject_car(self, query, car_id):
        """Inject selected car"""
        user_id = query.from_user.id
        car_data = self.injection_service.cars_db.get(car_id, {})
        car_name = car_data.get('name', car_id)
        
        # Check if user can inject
        user = await self.db.get_user(user_id)
        if not user['is_vip']:
            can_inject = await self.db.can_inject_free(user_id)
            if not can_inject:
                await query.edit_message_text(
                    "❌ *Daily Limit Reached!*\n\n"
                    f"You've used your {Config.FREE_DAILY_LIMIT} free injection(s) today.\n"
                    "💎 Upgrade to VIP for unlimited injections!",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return
                
        await query.edit_message_text(
            f"🔄 *Injecting {car_name}...*\n\n"
            "Please wait while we process your request...",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        # Perform injection
        result = await self.injection_service.inject_item(user_id, car_id, "car")
        
        if result['success']:
            message = f"✅ *Success!*\n\n{car_name} has been injected successfully!"
            if not user['is_vip']:
                remaining = Config.FREE_DAILY_LIMIT - user['free_used_today'] - 1
                message += f"\n\n🆓 Free injections remaining today: {remaining}"
        else:
            message = f"❌ *Failed!*\n\n{result['message']}"
            
        keyboard = [[
            InlineKeyboardButton("🔙 Back to Cars", callback_data="menu_cars"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_menu")
        ]]
        
        await query.edit_message_text(
            message,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    async def show_user_stats(self, query):
        """Show user statistics"""
        user_id = query.from_user.id
        stats = await self.db.get_user_stats(user_id)
        
        vip_status = "💎 *VIP Status:* Active"
        if stats['vip_until']:
            vip_status += f" until {stats['vip_until'].strftime('%Y-%m-%d %H:%M')}"
        else:
            vip_status = "🆓 *VIP Status:* Not Active"
            
        message = (
            f"📊 *YOUR STATISTICS*\n\n"
            f"{vip_status}\n"
            f"📈 *Total Injections:* {stats['total_injections']}\n"
            f"✅ *Successful:* {stats['successful_injections']}\n"
            f"🆓 *Free used today:* {stats['free_used_today']}/{Config.FREE_DAILY_LIMIT}\n\n"
        )
        
        keyboard = [[
            InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
        ]]
        
        if not stats['is_vip']:
            keyboard[0].insert(0, InlineKeyboardButton("💎 Upgrade to VIP", callback_data="vip_info"))
            
        await query.edit_message_text(
            message,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    async def show_vip_info(self, query):
        """Show VIP information"""
        user_id = query.from_user.id
        user = await self.db.get_user(user_id)
        
        message = (
            "💎 *VIP SUBSCRIPTION*\n\n"
            "✨ *Benefits:*\n"
            "• ✅ Unlimited injections\n"
            "• 🚗 All cars unlocked\n"
            "• 🎨 All liveries unlocked\n"
            "• 💰 Max resource amounts\n"
            "• ⚡ Priority processing\n"
            "• 🔒 No daily limits\n\n"
        )
        
        if user['is_vip'] and user['vip_until']:
            message += f"*Your VIP expires:* {user['vip_until'].strftime('%Y-%m-%d %H:%M')}\n\n"
            
        message += "Contact admin @username to upgrade!"
        
        keyboard = [[
            InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
        ]]
        
        if query.from_user.id in Config.ADMIN_IDS:
            keyboard.append([
                InlineKeyboardButton("👑 Manage VIP Users", callback_data="admin_vip")
            ])
            
        await query.edit_message_text(
            message,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    async def show_admin_panel(self, query):
        """Show admin panel"""
        if query.from_user.id not in Config.ADMIN_IDS:
            await query.answer("❌ Access denied!", show_alert=True)
            return
            
        keyboard = [
            [
                InlineKeyboardButton("👥 User Management", callback_data="admin_users"),
                InlineKeyboardButton("💎 VIP Management", callback_data="admin_vip")
            ],
            [
                InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
                InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
            ]
        ]
        
        await query.edit_message_text(
            "👑 *ADMIN PANEL*\n\n"
            "Select an option to manage:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    async def set_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set user token"""
        if not context.args:
            await update.message.reply_text(
                "❌ *Usage:* `/settoken YOUR_TOKEN_HERE`\n\n"
                "Get your token from the game settings.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
            
        token = " ".join(context.args)
        user_id = update.effective_user.id
        
        # Encrypt and store token
        encrypted_token = self.injection_service.encryption.encrypt_token(token)
        
        async with self.db.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO user_tokens (user_id, encrypted_token)
                VALUES ($1, $2)
                ON CONFLICT (user_id) 
                DO UPDATE SET encrypted_token = $2, updated_at = CURRENT_TIMESTAMP
            ''', user_id, encrypted_token)
            
        await update.message.reply_text(
            "✅ *Token saved successfully!*\n\n"
            "Your authentication token has been encrypted and stored securely.\n"
            "You can now use the injection features.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
    # Admin commands
    async def add_vip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add VIP to user (admin only)"""
        if update.effective_user.id not in Config.ADMIN_IDS:
            await update.message.reply_text("❌ Admin only command!")
            return
            
        if len(context.args) != 2:
            await update.message.reply_text(
                "❌ *Usage:* `/addvip USER_ID DAYS`\n\n"
                "Example: `/addvip 123456789 30`",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
            
        try:
            user_id = int(context.args[0])
            days = int(context.args[1])
            
            await self.db.update_vip_status(user_id, days)
            
            await update.message.reply_text(
                f"✅ *VIP Added!*\n\n"
                f"User `{user_id}` now has VIP for {days} days.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID or days!")
            
    async def remove_vip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove VIP from user (admin only)"""
        if update.effective_user.id not in Config.ADMIN_IDS:
            await update.message.reply_text("❌ Admin only command!")
            return
            
        if not context.args:
            await update.message.reply_text(
                "❌ *Usage:* `/removevip USER_ID`",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
            
        try:
            user_id = int(context.args[0])
            await self.db.remove_vip(user_id)
            
            await update.message.reply_text(
                f"✅ *VIP Removed!*\n\n"
                f"User `{user_id}` no longer has VIP access.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID!")
            
    async def list_vip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all VIP users (admin only)"""
        if update.effective_user.id not in Config.ADMIN_IDS:
            await update.message.reply_text("❌ Admin only command!")
            return
            
        vip_users = await self.db.get_vip_users()
        
        if not vip_users:
            await update.message.reply_text("📭 No active VIP users found.")
            return
            
        message = "👑 *ACTIVE VIP USERS*\n\n"
        for user in vip_users:
            expires = user['vip_until'].strftime('%Y-%m-%d') if user['vip_until'] else "Unknown"
            message += f"• `{user['user_id']}` - {user['first_name']} - Expires: {expires}\n"
            
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)
        
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot statistics (admin only)"""
        if update.effective_user.id not in Config.ADMIN_IDS:
            await update.message.reply_text("❌ Admin only command!")
            return
            
        async with self.db.pool.acquire() as conn:
            total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
            total_vip = await conn.fetchval(
                'SELECT COUNT(*) FROM users WHERE is_vip = TRUE AND vip_until > CURRENT_TIMESTAMP'
            )
            total_injections = await conn.fetchval('SELECT COUNT(*) FROM injection_logs')
            successful_injections = await conn.fetchval(
                'SELECT COUNT(*) FROM injection_logs WHERE success = TRUE'
            )
            
        message = (
            "📊 *BOT STATISTICS*\n\n"
            f"👥 *Total Users:* {total_users}\n"
            f"💎 *Active VIP Users:* {total_vip}\n"
            f"🔄 *Total Injections:* {total_injections}\n"
            f"✅ *Successful Injections:* {successful_injections}\n"
            f"📈 *Success Rate:* {((successful_injections/total_injections)*100 if total_injections > 0 else 0):.1f}%\n\n"
            f"⚙️ *Bot Mode:* {'Webhook' if Config.USE_WEBHOOK else 'Polling'}"
        )
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)
        
    def _get_header_image(self) -> Optional[str]:
        """Get random header image from directory"""
        if not os.path.exists(Config.HEADER_IMAGES_DIR):
            return None
            
        images = [f for f in os.listdir(Config.HEADER_IMAGES_DIR) 
                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))]
        
        if not images:
            return None
            
        return os.path.join(Config.HEADER_IMAGES_DIR, random.choice(images))
        
    def _get_main_keyboard(self, user_id: int):
        """Get main keyboard based on user status"""
        keyboard = [
            [
                InlineKeyboardButton("🚗 Cars", callback_data="menu_cars"),
                InlineKeyboardButton("🎨 Liveries", callback_data="menu_liveries"),
                InlineKeyboardButton("💰 Resources", callback_data="menu_resources")
            ],
            [
                InlineKeyboardButton("📊 Stats", callback_data="my_stats"),
                InlineKeyboardButton("💎 VIP", callback_data="vip_info"),
                InlineKeyboardButton("⚙️ Settings", callback_data="settings")
            ]
        ]
        
        if user_id in Config.ADMIN_IDS:
            keyboard.append([
                InlineKeyboardButton("👑 Admin", callback_data="admin_panel")
            ])
            
        return InlineKeyboardMarkup(keyboard)
        
    async def reset_daily_limits(self):
        """Reset daily free limits for all users"""
        async with self.db.pool.acquire() as conn:
            await conn.execute('''
                UPDATE users 
                SET free_used_today = 0, last_reset_date = CURRENT_DATE
                WHERE last_reset_date < CURRENT_DATE
            ''')
        logging.info("Daily limits reset")
        
    async def check_vip_expiry(self):
        """Check and expire VIP users"""
        async with self.db.pool.acquire() as conn:
            expired_users = await conn.fetch('''
                SELECT user_id FROM users 
                WHERE is_vip = TRUE AND vip_until < CURRENT_TIMESTAMP
            ''')
            
            for user in expired_users:
                await conn.execute('''
                    UPDATE users 
                    SET is_vip = FALSE 
                    WHERE user_id = $1
                ''', user['user_id'])
                
        if expired_users:
            logging.info(f"Expired {len(expired_users)} VIP users")
            
    def setup_scheduler(self):
        """Setup scheduled tasks"""
        # Reset daily limits at midnight
        self.scheduler.add_job(
            self.reset_daily_limits,
            'cron',
            hour=0,
            minute=0
        )
        
        # Check VIP expiry every hour
        self.scheduler.add_job(
            self.check_vip_expiry,
            'interval',
            hours=1
        )
        
        self.scheduler.start()
        
    def setup_handlers(self, application: Application):
        """Setup bot handlers"""
        # Command handlers
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("menu", self.menu))
        application.add_handler(CommandHandler("settoken", self.set_token))
        
        # Admin commands
        application.add_handler(CommandHandler("addvip", self.add_vip))
        application.add_handler(CommandHandler("removevip", self.remove_vip))
        application.add_handler(CommandHandler("listvip", self.list_vip))
        application.add_handler(CommandHandler("stats", self.stats))
        
        # Callback query handler
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        
    async def run_webhook(self):
        """Run bot with webhook (for Vercel)"""
        application = Application.builder().token(Config.BOT_TOKEN).build()
        
        await self.db.connect()
        self.setup_scheduler()
        self.setup_handlers(application)
        
        # Set webhook
        await application.bot.set_webhook(
            url=f"{Config.WEBHOOK_URL}/webhook",
            drop_pending_updates=True
        )
        
        return application
        
    def run_polling(self):
        """Run bot with polling (for panel)"""
        application = Application.builder().token(Config.BOT_TOKEN).build()
        
        # Connect to database
        asyncio.get_event_loop().run_until_complete(self.db.connect())
        
        self.setup_scheduler()
        self.setup_handlers(application)
        
        # Start polling
        application.run_polling(allowed_updates=Update.ALL_TYPES)

# ==================== MAIN ENTRY POINT ====================
async def main():
    """Main entry point"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    bot = InjectionBot()
    
    if Config.USE_WEBHOOK:
        # For Vercel deployment
        from telegram.ext import Application
        application = await bot.run_webhook()
        return application
    else:
        # For local/panel deployment
        bot.run_polling()

# For Vercel deployment
app = None
if Config.USE_WEBHOOK:
    import asyncio
    app = asyncio.run(main())

# For Vercel webhook endpoint
if Config.USE_WEBHOOK:
    from flask import Flask, request
    import nest_asyncio
    
    nest_asyncio.apply()
    flask_app = Flask(__name__)
    
    @flask_app.route('/webhook', methods=['POST'])
    def webhook():
        update = Update.de_json(request.get_json(), app.bot)
        asyncio.create_task(app.process_update(update))
        return 'OK', 200
        
    @flask_app.route('/')
    def index():
        return 'Bot is running!'
        
    if __name__ == '__main__':
        flask_app.run(host='0.0.0.0', port=Config.PORT)

if __name__ == '__main__' and not Config.USE_WEBHOOK:
    asyncio.run(main())
