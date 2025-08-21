# bot.py
import os
import json
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import tasks, commands
import asyncpraw
from pathlib import Path
import traceback

# Загрузка секретов
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "script:RedditToDiscordBot:1.0 (by u/yourname)")

# Конфиги
CONFIG_PATH = "config.json"
SEEN_PATH = "seen_posts.json"

# Загрузим конфиг
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# Загрузим/инициализируем seen posts (устойчиво к пустым/битым файлам)
SEEN = set()
if os.path.exists(SEEN_PATH):
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            data = f.read().strip()
            if data == "":
                SEEN = set()
            else:
                parsed = json.loads(data)
                if isinstance(parsed, list):
                    SEEN = set(str(x) for x in parsed)
                else:
                    print(f"Warning: {SEEN_PATH} содержит не список, создаю бэкап и использую пустой набор.")
                    os.rename(SEEN_PATH, SEEN_PATH + ".bak")
                    SEEN = set()
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Warning: не смог разобрать {SEEN_PATH}: {e}. Создаю бэкап и использую пустой набор.")
        try:
            os.rename(SEEN_PATH, SEEN_PATH + ".corrupt.bak")
        except Exception:
            pass
        SEEN = set()
else:
    SEEN = set()

def save_seen():
    """
    Атомарно сохраняет список отправленных ID постов.
    """
    tmp = SEEN_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(SEEN), f, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEN_PATH)
    except Exception as e:
        print("Failed to save seen posts:", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

# Помощник: проверка на совпадение с блэклистом
def title_has_blacklisted_word(title: str, blacklist):
    title_lower = title.lower()
    for word in blacklist:
        if word.lower() in title_lower:
            return True
    return False

# Helper: получить наиболее надежный URL картинки из submission (но не для видео)
def extract_image_url(submission) -> str | None:
    """
    Возвращает URL картинки, если удается найти (gallery -> preview -> direct url).
    Если не найдено или это видео — возвращает None.
    """
    try:
        # Если это видео — не возвращаем превью как основное изображение (по требованию)
        if getattr(submission, "is_video", False):
            return None

        # 1) gallery (если это gallery)
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

        # 2) preview
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

        # 3) прямой url, если это изображение по расширению
        try:
            url = getattr(submission, "url", "") or ""
            if isinstance(url, str) and any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                return url.replace("&amp;", "&")
        except Exception:
            pass

    except Exception:
        traceback.print_exc()

    return None

# Инициализируем бота discord
intents = discord.Intents.default()
# Если ты используешь команды с message content — включи Intent в Dev Portal
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# reddit-клиент будет создан в on_ready (чтобы работать в event loop)
reddit = None

CHECK_INTERVAL = CONFIG.get("check_interval_minutes", 60)

@bot.event
async def on_ready():
    global reddit
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Создаём asyncpraw клиент внутри event loop (важно)
    try:
        reddit = asyncpraw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT
        )
    except Exception as e:
        print("Failed to initialize asyncpraw Reddit client in on_ready:", e)
        reddit = None

    # Запускаем цикл
    check_reddit.start()
    print("Started reddit-check loop.")

@tasks.loop(minutes=CHECK_INTERVAL)
async def check_reddit():
    global reddit
    print("Checking subreddits...")
    blacklist = CONFIG.get("blacklist", [])
    channels_cfg = CONFIG.get("channels", {})

    if reddit is None:
        # Попытка создать заново, если по какой-то причине не создался ранее
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
                # Правильно: await для asyncpraw
                subreddit = await reddit.subreddit(sub)

                async for submission in subreddit.new(limit=25):
                    try:
                        sid = submission.id
                        if sid in SEEN:
                            continue

                        title = submission.title or ""
                        score = getattr(submission, "score", 0)
                        # фильтры
                        if score < upvote_threshold:
                            continue
                        if title_has_blacklisted_word(title, blacklist):
                            continue

                        # Получаем изображение (если это НЕ видео)
                        image_url = extract_image_url(submission)

                        # Формируем embed
                        embed = discord.Embed(
                            title=(title if title else "Ссылка на пост"),
                            url=f"https://reddit.com{getattr(submission, 'permalink', '')}",
                            color=0xFF5700
                        )

                        shortlink = f"https://redd.it/{sid}"
                        # Если это видео — даём ссылку и пометим как видео
                        if getattr(submission, "is_video", False):
                            embed.description = f"Видео пост — [Открыть на Reddit]({shortlink})"
                        else:
                            embed.description = f"[Открыть на Reddit]({shortlink})"

                        # Добавляем изображение только если это не видео и вообще есть image_url
                        if image_url and not getattr(submission, "is_video", False):
                            try:
                                embed.set_image(url=image_url)
                            except Exception as e:
                                print(f"Failed to set embed image for {sid}: {e}")

                        # Отправляем embed
                        try:
                            sent_msg = await channel.send(embed=embed)
                            # Добавляем реакции (попытка, с обработкой ошибок прав/лимитов)
                            try:
                                await sent_msg.add_reaction("👍")
                            except Exception as e:
                                print(f"Warning: could not add 👍 reaction to message for {sid}: {e}")
                            try:
                                await sent_msg.add_reaction("👎")
                            except Exception as e:
                                print(f"Warning: could not add 👎 reaction to message for {sid}: {e}")

                            # пометим как отправленное
                            SEEN.add(sid)
                        except Exception as e:
                            print(f"Failed to send embed to channel {channel_id}: {e}")

                    except Exception as inner_e:
                        print(f"Error processing submission in r/{sub}: {type(inner_e).__name__}: {inner_e}")
                        traceback.print_exc()

            except Exception as e:
                print(f"Error reading r/{sub}: {type(e).__name__}: {e}")
                continue

    # Сохраняем состояние после цикла
    save_seen()
    print("Check complete.")

# Graceful shutdown: закрываем reddit сессию
async def close_resources():
    global reddit
    try:
        if reddit is not None:
            await reddit.close()
            reddit = None
    except Exception:
        pass

@bot.command(name="forcecheck")
@commands.is_owner()
async def forcecheck(ctx):
    """Команда: форс-проверка сейчас"""
    await ctx.send("Запускаю проверку сейчас...")
    await check_reddit()
    await ctx.send("Проверка завершена.")

# Запуск
try:
    bot.run(DISCORD_TOKEN)
finally:
    try:
        asyncio.run(close_resources())
    except Exception:
        pass
