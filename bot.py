# bot.py
import os
import json
import asyncio
import logging
from typing import Dict, List, Any

import discord
from discord.ext import commands

# g4f async client
from g4f.client import AsyncClient
import g4f

# ---------- Config & constants ----------
CONFIG_FILE = "g4f_bot_config.json"
CONTEXT_LIMIT = 12  # number of message pairs to keep (system + recent)
AI_TIMEOUT = 60  # seconds to wait for AI reply before timing out

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("g4f-discord-bot")

# Load env secrets
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")  # must be set in Replit secrets
DEFAULT_MODEL = os.environ.get("G4F_MODEL", "gpt-4o-mini")  # change if you prefer

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN not found in environment/secrets. Add it to Replit secrets.")

if not OWNER_ID:
    raise RuntimeError("OWNER_ID not found in environment/secrets. Add it to Replit secrets.")

try:
    OWNER_ID_INT = int(OWNER_ID)
except Exception:
    raise RuntimeError("OWNER_ID must be an integer (Discord user id) in secrets.")

# ---------- persistence ----------
def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"bound_channel": None, "model": DEFAULT_MODEL, "conversations": {}}

def save_config(cfg: Dict[str, Any]):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

# Ensure conversations key
if "conversations" not in config:
    config["conversations"] = {}

# ---------- bot setup ----------
intents = discord.Intents.default()
intents.message_content = True  # required to read message text
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# g4f client (async)
g4f_client = AsyncClient()

# Per-channel locks to prevent concurrent queries
channel_locks: Dict[int, asyncio.Lock] = {}

# ---------- helpers ----------
def is_owner_check(ctx: commands.Context):
    return ctx.author.id == OWNER_ID_INT

def ensure_channel_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in channel_locks:
        channel_locks[channel_id] = asyncio.Lock()
    return channel_locks[channel_id]

def conv_key(channel_id: int) -> str:
    return str(channel_id)

def trim_history(history: List[Dict[str,str]]) -> List[Dict[str,str]]:
    # keep system + last CONTEXT_LIMIT user/assistant messages
    system = [m for m in history if m.get("role") == "system"]
    non_sys = [m for m in history if m.get("role") != "system"]
    trimmed = non_sys[-(CONTEXT_LIMIT * 2):]  # each exchange has user + assistant approx
    return system + trimmed

async def ai_get_response(channel_id: int, user_text: str, model: str) -> str:
    # conversation stored in config["conversations"][channel_id]
    key = conv_key(channel_id)
    conv = config["conversations"].get(key, [])
    # If empty, start with system prompt
    if not conv:
        conv = [{"role":"system", "content":"You are a helpful, concise assistant in a Discord channel. Answer politely."}]
    conv.append({"role":"user", "content": user_text})
    # Trim to avoid huge payload
    conv = trim_history(conv)
    config["conversations"][key] = conv
    save_config(config)

    try:
        # call g4f async client
        response = await asyncio.wait_for(
            g4f_client.chat.completions.create(
                model=model,
                messages=conv,
                web_search=False
            ),
            timeout=AI_TIMEOUT
        )
        # Extract text
        ai_text = response.choices[0].message.content
        # add assistant message to history
        conv.append({"role":"assistant", "content": ai_text})
        config["conversations"][key] = trim_history(conv)
        save_config(config)
        return ai_text
    except asyncio.TimeoutError:
        return "⚠️ Sorry — the AI took too long to respond. Try again later."
    except Exception as e:
        logger.exception("AI request failed")
        return f"⚠️ Error contacting AI: {e}"

# ---------- Commands (owner only) ----------
@bot.command(name="setupchannel")
async def setupchannel(ctx: commands.Context):
    """Owner only: bind this channel as the bot's listening/responding channel."""
    if ctx.author.id != OWNER_ID_INT:
        await ctx.reply("You are not authorized to use this command.", mention_author=False)
        return
    config["bound_channel"] = str(ctx.channel.id)
    save_config(config)
    await ctx.reply(f"✅ This channel is now bound. I will respond to messages here.", mention_author=False)

@bot.command(name="unsetchannel")
async def unsetchannel(ctx: commands.Context):
    if ctx.author.id != OWNER_ID_INT:
        await ctx.reply("You are not authorized to use this command.", mention_author=False)
        return
    config["bound_channel"] = None
    save_config(config)
    await ctx.reply("✅ Channel unbound. I will no longer listen to channel messages.", mention_author=False)

@bot.command(name="setmodel")
async def setmodel(ctx: commands.Context, model: str):
    if ctx.author.id != OWNER_ID_INT:
        await ctx.reply("You are not authorized to use this command.", mention_author=False)
        return
    config["model"] = model
    save_config(config)
    await ctx.reply(f"✅ Model set to `{model}`.", mention_author=False)

@bot.command(name="clearhistory")
async def clearhistory(ctx: commands.Context):
    if ctx.author.id != OWNER_ID_INT:
        await ctx.reply("You are not authorized to use this command.", mention_author=False)
        return
    key = conv_key(ctx.channel.id)
    config["conversations"].pop(key, None)
    save_config(config)
    await ctx.reply("✅ Conversation history cleared for this channel.", mention_author=False)

@bot.command(name="status")
async def status(ctx: commands.Context):
    bound = config.get("bound_channel")
    model = config.get("model", DEFAULT_MODEL)
    bound_text = f"<#{bound}>" if bound else "Not bound"
    await ctx.reply(f"**Status**\nBound channel: {bound_text}\nModel: `{model}`", mention_author=False)

@bot.command(name="shutdown")
async def shutdown(ctx: commands.Context):
    if ctx.author.id != OWNER_ID_INT:
        await ctx.reply("You are not authorized to use this command.", mention_author=False)
        return
    await ctx.reply("Shutting down... (owner requested)", mention_author=False)
    await bot.close()

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.reply(f"Pong! latency: {round(bot.latency*1000)} ms", mention_author=False)

# ---------- Message handling ----------
@bot.event
async def on_message(message: discord.Message):
    # always process commands first
    await bot.process_commands(message)

    # ignore bots & DMs
    if message.author.bot:
        return
    if message.guild is None:
        # ignore DMs in this implementation
        return

    bound = config.get("bound_channel")
    if not bound:
        return  # not bound yet; do nothing

    try:
        if str(message.channel.id) != str(bound):
            return  # only respond in bound channel
    except Exception:
        return

    # Owner & admin commands are allowed via prefix; non-owners just chat
    # Acquire lock for this channel
    lock = ensure_channel_lock(message.channel.id)
    if lock.locked():
        # Inform user politely that bot is busy with prior request
        try:
            await message.channel.send("⏳ I'm still processing a previous message — please wait a moment.", delete_after=6)
        except Exception:
            pass
        return

    async with lock:
        # show typing indicator
        typing = message.channel.typing()
        await typing.__aenter__()  # start typing
        try:
            model = config.get("model", DEFAULT_MODEL)
            user_text = message.content.strip()
            # safety: short-circuit empty messages/attachments-only
            if not user_text:
                await message.channel.send("I didn't see any text to respond to.", delete_after=6)
                return
            # Call AI
            ai_reply = await ai_get_response(message.channel.id, user_text, model)
            # send reply in thread-safe manner (respect Discord limits)
            # If reply is long, split into chunks
            MAX = 1900
            chunks = [ai_reply[i:i+MAX] for i in range(0, len(ai_reply), MAX)]
            reply_to = message.author
            for chunk in chunks:
                await message.channel.send(chunk)
        finally:
            await typing.__aexit__(None, None, None)

# ---------- startup ----------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    logger.info("------")

def main():
    save_config(config)  # ensure config file exists
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
