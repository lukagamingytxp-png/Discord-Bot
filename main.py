import discord
from discord.ext import commands
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import asyncio
import asyncpg
import os
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("AternosBot")

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
DATABASE_URL     = os.getenv("DATABASE_URL")
ATERNOS_USERNAME = os.getenv("ATERNOS_USERNAME")
ATERNOS_PASSWORD = os.getenv("ATERNOS_PASSWORD")
SERVER_IP        = "Tr1alSMP.aternos.me"
DISCORD_INVITE   = "discord.gg/TrialsSMP"
FOOTER_TEXT      = f"🌐 {SERVER_IP}  •  💬 {DISCORD_INVITE}"
COOLDOWN_SECONDS = 300
PORT             = int(os.getenv("PORT", 8080))

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = 0x2ECC71
RED    = 0xE74C3C
YELLOW = 0xF1C40F
BLUE   = 0x3498DB
ORANGE = 0xE67E22
GREY   = 0x95A5A6

# ── Keep-alive HTTP server ────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Tr1alSMP bot is alive!")
    def log_message(self, *_):
        pass

def keep_alive():
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Keep-alive HTTP server started on port {PORT}")

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="+", intents=intents, help_command=None)
bot.db = None

# ── Embed helpers ─────────────────────────────────────────────────────────────
def make_footer(embed):
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def error_embed(title, description):
    return make_footer(discord.Embed(title=f"❌  {title}", description=description, color=RED))

def success_embed(title, description=""):
    return make_footer(discord.Embed(title=f"✅  {title}", description=description, color=GREEN))

def info_embed(title, description="", colour=BLUE):
    return make_footer(discord.Embed(title=title, description=description, color=colour))

# ── Aternos helpers ───────────────────────────────────────────────────────────
def get_aternos_client():
    from python_aternos import Client
    atclient = Client()
    atclient.login(ATERNOS_USERNAME, ATERNOS_PASSWORD)
    return atclient

def get_server(client):
    servers = client.list_servers()
    if not servers:
        raise RuntimeError("No Aternos servers found on this account.")
    return servers[0]

# ── Database helpers ──────────────────────────────────────────────────────────
async def init_db():
    bot.db = await asyncpg.create_pool(DATABASE_URL)
    async with bot.db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id   TEXT      NOT NULL,
                command   TEXT      NOT NULL,
                last_used TIMESTAMP NOT NULL,
                PRIMARY KEY (user_id, command)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS command_logs (
                id        SERIAL    PRIMARY KEY,
                user_id   TEXT      NOT NULL,
                command   TEXT      NOT NULL,
                timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                result    TEXT
            )
        """)
    log.info("Database tables ready.")

async def check_cooldown(user_id, command):
    row = await bot.db.fetchrow(
        "SELECT last_used FROM cooldowns WHERE user_id=$1 AND command=$2",
        str(user_id), command,
    )
    if row is None:
        return None
    remaining = COOLDOWN_SECONDS - (datetime.utcnow() - row["last_used"]).total_seconds()
    return int(remaining) if remaining > 0 else None

async def set_cooldown(user_id, command):
    await bot.db.execute("""
        INSERT INTO cooldowns (user_id, command, last_used)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, command) DO UPDATE SET last_used = EXCLUDED.last_used
    """, str(user_id), command, datetime.utcnow())

async def log_command(user_id, command, result):
    await bot.db.execute(
        "INSERT INTO command_logs (user_id, command, timestamp, result) VALUES ($1,$2,$3,$4)",
        str(user_id), command, datetime.utcnow(), result,
    )

# ── Error handler ─────────────────────────────────────────────────────────────
def _handle_exc(exc, cmd):
    msg = str(exc).lower()
    if "captcha" in msg:
        return error_embed("CAPTCHA Required", "Aternos triggered a CAPTCHA. Try again in a few minutes.")
    if any(x in msg for x in ("credentials", "password", "username", "login", "401", "403", "token")):
        return error_embed("Login Failed", "Could not log into Aternos.\nCheck `ATERNOS_USERNAME` and `ATERNOS_PASSWORD` in your environment variables.")
    if "queue" in msg:
        return error_embed("Queue Error", f"Aternos queue error: `{exc}`\nTry `+status`.")
    if "timeout" in msg or "timed out" in msg:
        return error_embed("Timeout", "Aternos took too long to respond. Try again.")
    log.exception(f"Unclassified error in +{cmd}", exc_info=exc)
    return error_embed("Error", f"```{str(exc)[:1000]}```")

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Tr1alSMP | +help"))

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=error_embed("No Permission", "You need **Administrator** permission to use this command."))
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.exception("Unhandled error", exc_info=error)
        await ctx.send(embed=error_embed("Unexpected Error", f"```{error}```"))

# ── +help ─────────────────────────────────────────────────────────────────────
@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="📋  Tr1alSMP Bot — Command Reference",
                          description="Manage the Aternos Minecraft server straight from Discord.", color=BLUE)
    embed.add_field(name="🟢  `+start`",   value="Start the server.\n👥 **Anyone** — ⏱️ 5-minute cooldown per user.", inline=False)
    embed.add_field(name="🔴  `+stop`",    value="Stop the server.\n🔒 **Administrators only.**", inline=False)
    embed.add_field(name="🔄  `+restart`", value="Restart the server.\n🔒 **Administrators only.**", inline=False)
    embed.add_field(name="📊  `+status`",  value="Show server status, players, IP, MOTD, and version.\n👥 **Anyone.**", inline=False)
    embed.add_field(name="📜  `+logs`",    value="Show the last 10 lines of the server console.\n🔒 **Administrators only.**", inline=False)
    embed.add_field(name="❓  `+help`",    value="This message.\n👥 **Anyone.**", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)
    await log_command(ctx.author.id, "help", "shown")

# ── +start ────────────────────────────────────────────────────────────────────
@bot.command(name="start")
async def start_cmd(ctx):
    remaining = await check_cooldown(ctx.author.id, "start")
    if remaining:
        mins, secs = divmod(remaining, 60)
        await ctx.send(embed=error_embed("Cooldown Active", f"You can use `+start` again in **{mins}m {secs}s**."))
        await log_command(ctx.author.id, "start", f"cooldown:{remaining}s")
        return

    thinking = await ctx.send(embed=info_embed("⏳  Connecting to Aternos…", colour=YELLOW))
    try:
        client = get_aternos_client()
        server = get_server(client)
        status = server.status

        if status == "online":
            players = server.players_list or []
            player_names = "\n".join(f"• {p}" for p in players) if players else "*No players online*"
            embed = info_embed("🟢  Server Already Online", colour=GREEN)
            embed.add_field(name="🌐 Server IP",      value=f"`{SERVER_IP}`", inline=True)
            embed.add_field(name="📦 Version",        value=server.version or "?", inline=True)
            embed.add_field(name="👥 Players Online", value=f"{server.players_on}/{server.players_max}", inline=True)
            embed.add_field(name="📋 Player List",    value=player_names, inline=False)
            embed.set_footer(text=FOOTER_TEXT)
            await thinking.edit(embed=embed)
            await log_command(ctx.author.id, "start", "already_online")
            return

        if status in ("starting", "loading", "preparing"):
            queue = getattr(server, "queue_position", None)
            eta   = getattr(server, "time_until_up", None)
            embed = info_embed("🔄  Server Is Already Starting…", colour=YELLOW)
            embed.add_field(name="🌐 Server IP",      value=f"`{SERVER_IP}`", inline=True)
            embed.add_field(name="📦 Version",        value=server.version or "?", inline=True)
            embed.add_field(name="📋 Queue Position", value=f"#{queue}" if queue else "In queue…", inline=True)
            embed.add_field(name="⏱️ Estimated Wait", value=f"~{eta}s" if eta else "Calculating…", inline=True)
            embed.set_footer(text=FOOTER_TEXT)
            await thinking.edit(embed=embed)
            await log_command(ctx.author.id, "start", "already_starting")
            return

        server.start()
        server = get_server(client)
        queue = getattr(server, "queue_position", None)
        eta   = getattr(server, "time_until_up", None)
        embed = success_embed("Server is Starting! 🚀", "The server is booting up. It may take a few minutes.")
        embed.add_field(name="🌐 Server IP",      value=f"`{SERVER_IP}`", inline=True)
        embed.add_field(name="📦 Version",        value=server.version or "?", inline=True)
        embed.add_field(name="📋 Queue Position", value=f"#{queue}" if queue else "In queue…", inline=True)
        embed.add_field(name="⏱️ Estimated Wait", value=f"~{eta}s" if eta else "Calculating…", inline=True)
        embed.set_footer(text=FOOTER_TEXT)
        await thinking.edit(embed=embed)
        await set_cooldown(ctx.author.id, "start")
        await log_command(ctx.author.id, "start", "started")

    except Exception as exc:
        await thinking.edit(embed=_handle_exc(exc, "start"))
        await log_command(ctx.author.id, "start", f"error:{exc}")

# ── +stop ─────────────────────────────────────────────────────────────────────
@bot.command(name="stop")
@commands.has_permissions(administrator=True)
async def stop_cmd(ctx):
    await ctx.send(embed=info_embed("⚠️  Confirm Server Stop",
        "Are you sure you want to **stop** the server?\nReply `yes` within 15 seconds.", colour=ORANGE))

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ("yes", "no")

    try:
        msg = await bot.wait_for("message", timeout=15.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(embed=info_embed("⏰  Timed Out", "Stop cancelled.", colour=GREY))
        await log_command(ctx.author.id, "stop", "timeout")
        return

    if msg.content.lower() == "no":
        await ctx.send(embed=info_embed("🚫  Cancelled", "Server stop was cancelled.", colour=GREY))
        await log_command(ctx.author.id, "stop", "cancelled")
        return

    thinking = await ctx.send(embed=info_embed("⏳  Stopping server…", colour=YELLOW))
    try:
        client = get_aternos_client()
        server = get_server(client)
        if server.status == "offline":
            await thinking.edit(embed=error_embed("Already Offline", "The server is already offline."))
            await log_command(ctx.author.id, "stop", "already_offline")
            return
        server.stop()
        embed = success_embed("Server Stopped 🔴", "The server has been stopped successfully.")
        embed.add_field(name="🌐 Server IP", value=f"`{SERVER_IP}`", inline=True)
        embed.set_footer(text=FOOTER_TEXT)
        await thinking.edit(embed=embed)
        await log_command(ctx.author.id, "stop", "stopped")
    except Exception as exc:
        await thinking.edit(embed=_handle_exc(exc, "stop"))
        await log_command(ctx.author.id, "stop", f"error:{exc}")

# ── +restart ──────────────────────────────────────────────────────────────────
@bot.command(name="restart")
@commands.has_permissions(administrator=True)
async def restart_cmd(ctx):
    thinking = await ctx.send(embed=info_embed("⏳  Restarting server…", colour=YELLOW))
    try:
        client = get_aternos_client()
        server = get_server(client)
        if server.status == "offline":
            await thinking.edit(embed=error_embed("Server Offline", "Can't restart — server is offline. Use `+start` instead."))
            await log_command(ctx.author.id, "restart", "offline")
            return
        server.restart()
        embed = success_embed("Server Restarting 🔄", "The server is restarting. It will come back online shortly.")
        embed.add_field(name="🌐 Server IP", value=f"`{SERVER_IP}`", inline=True)
        embed.add_field(name="📦 Version",   value=server.version or "?", inline=True)
        embed.set_footer(text=FOOTER_TEXT)
        await thinking.edit(embed=embed)
        await log_command(ctx.author.id, "restart", "restarted")
    except Exception as exc:
        await thinking.edit(embed=_handle_exc(exc, "restart"))
        await log_command(ctx.author.id, "restart", f"error:{exc}")

# ── +status ───────────────────────────────────────────────────────────────────
@bot.command(name="status")
async def status_cmd(ctx):
    thinking = await ctx.send(embed=info_embed("⏳  Fetching status…", colour=YELLOW))
    try:
        client = get_aternos_client()
        server = get_server(client)
        status = server.status
        colour = GREEN if status == "online" else (YELLOW if status in ("starting", "loading") else RED)
        emoji  = {"online": "🟢", "offline": "🔴", "starting": "🟡", "loading": "🟡", "stopping": "🟠"}.get(status, "⚪")
        players = server.players_list or []
        player_names = "\n".join(f"• {p}" for p in players) if players else "*No players online*"
        embed = info_embed(f"{emoji}  Server Status — {status.capitalize()}", colour=colour)
        embed.add_field(name="🌐 Server IP", value=f"`{SERVER_IP}`", inline=True)
        embed.add_field(name="📦 Version",   value=server.version or "?", inline=True)
        embed.add_field(name="👥 Players",   value=f"{server.players_on}/{server.players_max}", inline=True)
        embed.add_field(name="📝 MOTD",      value=server.motd or "*Not set*", inline=False)
        if status == "online":
            embed.add_field(name="📋 Player List", value=player_names, inline=False)
        embed.set_footer(text=FOOTER_TEXT)
        await thinking.edit(embed=embed)
        await log_command(ctx.author.id, "status", status)
    except Exception as exc:
        await thinking.edit(embed=_handle_exc(exc, "status"))
        await log_command(ctx.author.id, "status", f"error:{exc}")

# ── +logs ─────────────────────────────────────────────────────────────────────
@bot.command(name="logs")
@commands.has_permissions(administrator=True)
async def logs_cmd(ctx):
    thinking = await ctx.send(embed=info_embed("⏳  Fetching console logs…", colour=YELLOW))
    try:
        client = get_aternos_client()
        server = get_server(client)
        if server.status == "offline":
            await thinking.edit(embed=error_embed("Server Offline", "Logs unavailable while the server is offline."))
            await log_command(ctx.author.id, "logs", "offline")
            return
        raw_logs = server.get_logs()
        lines = raw_logs.strip().splitlines()[-10:]
        log_text = "\n".join(lines) if lines else "No log output available."
        embed = info_embed("📜  Console — Last 10 Lines", colour=BLUE)
        embed.description = f"```\n{log_text[:1900]}\n```"
        embed.set_footer(text=FOOTER_TEXT)
        await thinking.edit(embed=embed)
        await log_command(ctx.author.id, "logs", "shown")
    except Exception as exc:
        await thinking.edit(embed=_handle_exc(exc, "logs"))
        await log_command(ctx.author.id, "logs", f"error:{exc}")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set in .env")
    if not ATERNOS_USERNAME:
        raise SystemExit("ATERNOS_USERNAME is not set in .env")
    if not ATERNOS_PASSWORD:
        raise SystemExit("ATERNOS_PASSWORD is not set in .env")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set in .env")
    keep_alive()
    bot.run(DISCORD_TOKEN)
