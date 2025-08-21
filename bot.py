# bot.py
import os
import json
import asyncio
import sqlite3
from dotenv import load_dotenv
import discord
from discord.ext import tasks, commands
import asyncpraw
from pathlib import Path
import traceback

# -------------------------
# Настройки окружения
# -------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "script:RedditToDiscordBot:1.0 (by u/yourname)")

# Путь к каталогу с персистентным хранилищем (смонтируйте volume на /data в Railway)
PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")  # можно переопределить в Variables
SEEN_DB_FILENAME = os.getenv("SEEN_DB_FILENAME", "seen_posts.db")
SEEN_DB_PATH = os.path.join(PERSIST_DIR, SEEN_DB_FILENAME)

# Для совместимости: при отсутствии volume будет использоваться локальный JSON
LOCAL_SEEN_PATH = "seen_posts.json"

# Конфиги
CONFIG_PATH = "config.json"

# -------------------------
# Загрузка конфигурации
# -------------------------
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

CHECK_INTERVAL = CONFIG.get("check_interval_minutes", 60)

# -------------------------
# Helpers: SQLite-backed seen storage
# -------------------------
use_sqlite = False
sqlite_conn = None
SEEN = set()

def init_sqlite_db(db_path: str):
    global sqlite_conn, use_sqlite
    try:
        # создаём директорию, если нужно
        Path(os.path.dirname(db_path) or ".").mkdir(parents=True, exist_ok=True)
        sqlite_conn = sqlite3.connect(db_path, check_same_thread=False)
        sqlite_conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_posts (id TEXT PRIMARY KEY, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        sqlite_conn.commit()
        use_sqlite = True
        print(f"Using SQLite DB for persistence at: {db_path}")
    except Exception as e:
        print(f"Failed to init sqlite at {db_path}: {e}")
        sqlite_conn = None
        use_sqlite = False

def load_seen_from_sqlite():
    global SEEN, sqlite_conn
    if not sqlite_conn:
        return
    try:
        cur = sqlite_conn.execute("SELECT id FROM seen_posts")
        rows = cur.fetchall()
        SEEN = set(row[0] for row in rows if row and row[0])
        print(f"Loaded {len(SEEN)} seen ids from SQLite.")
    except Exception as e:
        print("Error loading seen from sqlite:", e)

def add_seen_to_sqlite(sid: str):
    global sqlite_conn
    if not sqlite_conn:
        return
    try:
        sqlite_conn.execute("INSERT OR IGNORE INTO seen_posts (id) VALUES (?)", (sid,))
        sqlite_conn.commit()
    except Exception as e:
        print(f"Error inserting seen id {sid} into sqlite: {e}")

# Fallback file-based storage (for local dev / if no volume)
def load_seen_from_file(path: str):
    global SEEN
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read().strip()
                if data == "":
                    SEEN = set()
                else:
                    parsed = json.loads(data)
                    if isinstance(parsed, list):
                        SEEN = set(str(x) for x in parsed)
                    else:
                        print(f"Warning: {path} содержит не список — игнорирую.")
                        SEEN = set()
        except Exception as e:
            print("Failed to load seen from file:", e)
            SEEN = set()
    else:
        SEEN = set()

def save_seen_to_file(path: str):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(SEEN), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print("Failed to save seen file:", e)

# -------------------------
# Image extraction and utilities (как раньше)
# -------------------------
def title_has_blacklisted_word(title: str, blacklist):
    title_lower = title.lower()
    for word in blacklist:
        if word.lower() in title_lower:
            return True
    return False

def extract_image_url(submission) -> str | None:
    try:
        if getattr(submission, "is_video", False):
            return None
        # gallery
        try:
            if getattr(submission, "is_gallery", False):
                items = getattr(submission, "gallery_data", {}).get("items", [])
                media_meta = getattr(submission, "media_metadata", {}) or {}
                if items and isinstance(items, list) and media_meta:
                    media_id = items[0].get("media_id")
                    if media_id and media_meta.get(media_id):
                        mm = media_meta[media_id]
                        if isinstance(mm, dict) and mm.get("s"):
                            u = mm["s"].get("u")
                            if u:
                                return str(u).replace("&amp;", "&")
        except Exception:
            pass

        # preview
        try:
            preview = getattr(submission, "preview", None)
            if preview and isinstance(preview, dict):
                images = preview.get("images")
                if images and len(images) > 0:
                    src = images[0].get("source", {})
                    url = src.get("url")
                    if url:
                        return str(url).replace("&amp;", "&")
        except Exception:
            pass

        # direct url
        try:
            url = getattr(submission, "url", "") or ""
            if isinstance(url, str) and any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                return url.replace("&amp;", "&")
        except Exception:
            pass

    except Exception:
        traceback.print_exc()
    return None

# -------------------------
# Discord bot init
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# reddit placeholder (инициализируем в on_ready)
reddit = None

# -------------------------
# Startup: choose persistence backend
# -------------------------
def prepare_persistence():
    """
    Если директория PERSIST_DIR доступна и мы можем создать файл внутри — используем sqlite в PERSIST_DIR.
    Иначе — fallback на локальный JSON.
    """
    # Проверим, можно ли писать в PERSIST_DIR
    try:
        Path(PERSIST_DIR).mkdir(parents=True, exist_ok=True)
        test_path = Path(PERSIST_DIR) / ".persist_test"
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        test_path.unlink(missing_ok=True)
        # Инициализируем sqlite
        init_sqlite_db(SEEN_DB_PATH)
        if use_sqlite:
            load_seen_from_sqlite()
            return
    except Exception as e:
        print(f"Persistence dir not usable ({PERSIST_DIR}): {e}")

    # fallback
    print("Using local JSON fallback for seen storage.")
    load_seen_from_file(LOCAL_SEEN_PATH)

# -------------------------
# Bot events and main loop
# -------------------------
@bot.event
async def on_ready():
    global reddit
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Prepare persistence (DB or JSON)
    prepare_persistence()

    # Инициализация asyncpraw внутри event loop
    try:
        reddit = asyncpraw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT
        )
        print("Initialized asyncpraw Reddit client.")
    except Exception as e:
        print("Failed to initialize asyncpraw:", e)
        reddit = None

    check_reddit.start()
    print("Started reddit-check loop.")

@tasks.loop(minutes=CHECK_INTERVAL)
async def check_reddit():
    global reddit
    print("Checking subreddits...")
    blacklist = CONFIG.get("blacklist", [])
    channels_cfg = CONFIG.get("channels", {})

    if reddit is None:
        try:
            reddit = asyncpraw.Reddit(
                client_id=REDDIT_CLIENT_ID,
                client_secret=REDDIT_CLIENT_SECRET,
                user_agent=REDDIT_USER_AGENT
            )
            print("Created reddit client inside check_reddit.")
        except Exception as e:
            print("Cannot create reddit client:", e)
            return

    for channel_id_str, cfg in channels_cfg.items():
        try:
            channel_id = int(channel_id_str)
        except Exception:
            print(f"Invalid channel id in config: {channel_id_str}")
            continue

        channel = bot.get_channel(channel_id)
        if channel is None:
            print(f"Channel {channel_id} not found (bot might not be on that server).")
            continue

        subreddits = cfg.get("subreddits", [])
        upvote_threshold = cfg.get("upvote_threshold", 0)

        for sub in subreddits:
            try:
                subreddit = await reddit.subreddit(sub)

                async for submission in subreddit.new(limit=25):
                    try:
                        sid = submission.id
                        if sid in SEEN:
                            continue

                        title = submission.title or ""
                        score = getattr(submission, "score", 0)
                        if score < upvote_threshold:
                            continue
                        if title_has_blacklisted_word(title, blacklist):
                            continue

                        image_url = extract_image_url(submission)
                        embed = discord.Embed(
                            title=(title if title else "Ссылка на пост"),
                            url=f"https://reddit.com{getattr(submission, 'permalink', '')}",
                            color=0xFF5700
                        )
                        shortlink = f"https://redd.it/{sid}"
                        if getattr(submission, "is_video", False):
                            embed.description = f"Видео пост — [Открыть на Reddit]({shortlink})"
                        else:
                            embed.description = f"[Открыть на Reddit]({shortlink})"

                        if image_url and not getattr(submission, "is_video", False):
                            try:
                                embed.set_image(url=image_url)
                            except Exception as e:
                                print(f"Failed to set embed image for {sid}: {e}")

                        try:
                            sent_msg = await channel.send(embed=embed)
                            # реакции
                            try:
                                await sent_msg.add_reaction("👍")
                            except Exception as e:
                                print(f"Warning: could not add 👍 reaction for {sid}: {e}")
                            try:
                                await sent_msg.add_reaction("👎")
                            except Exception as e:
                                print(f"Warning: could not add 👎 reaction for {sid}: {e}")

                            # отметим как отправленное и запишем в persistence
                            SEEN.add(sid)
                            if use_sqlite:
                                add_seen_to_sqlite(sid)
                            else:
                                save_seen_to_file(LOCAL_SEEN_PATH)
                        except Exception as e:
                            print(f"Failed to send embed to channel {channel_id}: {e}")

                    except Exception as inner_e:
                        print(f"Error processing submission in r/{sub}: {type(inner_e).__name__}: {inner_e}")
                        traceback.print_exc()

            except Exception as e:
                print(f"Error reading r/{sub}: {type(e).__name__}: {e}")
                continue

    print("Check complete.")

# -------------------------
# Graceful shutdown
# -------------------------
async def close_resources():
    global reddit, sqlite_conn
    try:
        if reddit is not None:
            await reddit.close()
            reddit = None
    except Exception:
        pass
    try:
        if sqlite_conn is not None:
            sqlite_conn.close()
    except Exception:
        pass

@bot.command(name="forcecheck")
@commands.is_owner()
async def forcecheck(ctx):
    await ctx.send("Запускаю проверку сейчас...")
    await check_reddit()
    await ctx.send("Проверка завершена.")

# -------------------------
# Run
# -------------------------
try:
    bot.run(DISCORD_TOKEN)
finally:
    try:
        asyncio.run(close_resources())
    except Exception:
        pass
