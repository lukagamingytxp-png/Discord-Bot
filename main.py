import discord
from discord.ext import commands
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import asyncio
import asyncpg
import os
import logging
import subprocess
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("AternosBot")

# ── Install Chromium at runtime (required on Render free tier) ────────────────
log.info("Installing Chromium...")
subprocess.run(["playwright", "install", "chromium"], check=True)
log.info("Chromium ready.")

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

# ── Aternos Playwright automation ─────────────────────────────────────────────
async def aternos_action(action: str):
    """
    action: "start" | "stop" | "restart" | "status"
    Returns a dict with keys: status, players_on, players_max, players_list, version, motd, queue, eta
    Raises RuntimeError with a descriptive message on failure.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            # ── Login ──────────────────────────────────────────────────────
            log.info("Navigating to Aternos login page...")
            await page.goto("https://aternos.org/go/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fill username
            await page.fill("#user", ATERNOS_USERNAME, timeout=10000)
            await page.wait_for_timeout(500)

            # Fill password
            await page.fill("#password", ATERNOS_PASSWORD, timeout=10000)
            await page.wait_for_timeout(500)

            # Click login
            await page.click("#login-button", timeout=10000)
            await page.wait_for_timeout(3000)

            # Check for CAPTCHA
            if await page.query_selector(".h-captcha") or await page.query_selector("#hcaptcha"):
                raise RuntimeError("CAPTCHA detected — cannot proceed automatically.")

            # Check login succeeded by looking for server list
            await page.wait_for_url("**/servers**", timeout=15000)
            log.info("Logged in successfully.")

            # ── Navigate to server ─────────────────────────────────────────
            await page.goto("https://aternos.org/server/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # ── Scrape current status ──────────────────────────────────────
            async def scrape_status():
                data = {}

                # Status label
                status_el = await page.query_selector(".statuslabel-label")
                data["status"] = (await status_el.inner_text()).strip().lower() if status_el else "unknown"

                # Players
                players_el = await page.query_selector(".server-status-players")
                if players_el:
                    txt = (await players_el.inner_text()).strip()  # e.g. "2/20"
                    parts = txt.split("/")
                    data["players_on"]  = parts[0].strip() if len(parts) > 0 else "0"
                    data["players_max"] = parts[1].strip() if len(parts) > 1 else "20"
                else:
                    data["players_on"]  = "0"
                    data["players_max"] = "20"

                # Player list
                player_els = await page.query_selector_all(".player-list .player")
                data["players_list"] = [await el.inner_text() for el in player_els]

                # Version
                ver_el = await page.query_selector(".server-software-version")
                data["version"] = (await ver_el.inner_text()).strip() if ver_el else "?"

                # MOTD
                motd_el = await page.query_selector(".server-motd")
                data["motd"] = (await motd_el.inner_text()).strip() if motd_el else ""

                # Queue / ETA
                queue_el = await page.query_selector(".queue-position")
                data["queue"] = (await queue_el.inner_text()).strip() if queue_el else None

                eta_el = await page.query_selector(".queue-time")
                data["eta"] = (await eta_el.inner_text()).strip() if eta_el else None

                return data

            if action == "status":
                return await scrape_status()

            # ── Perform action ─────────────────────────────────────────────
            current = await scrape_status()
            current_status = current["status"]

            if action == "start":
                if current_status == "online":
                    return {**current, "already": "online"}
                if current_status in ("starting", "loading", "preparing"):
                    return {**current, "already": "starting"}
                # Click start button
                btn = await page.query_selector("#start")
                if not btn:
                    btn = await page.query_selector(".server-start")
                if not btn:
                    raise RuntimeError("Could not find the Start button on the page.")
                await btn.click()
                await page.wait_for_timeout(4000)
                return {**await scrape_status(), "already": None}

            elif action == "stop":
                if current_status == "offline":
                    return {**current, "already": "offline"}
                btn = await page.query_selector("#stop")
                if not btn:
                    btn = await page.query_selector(".server-stop")
                if not btn:
                    raise RuntimeError("Could not find the Stop button on the page.")
                await btn.click()
                await page.wait_for_timeout(3000)
                return {**await scrape_status(), "already": None}

            elif action == "restart":
                if current_status == "offline":
                    return {**current, "already": "offline"}
                btn = await page.query_selector("#restart")
                if not btn:
                    btn = await page.query_selector(".server-restart")
                if not btn:
                    raise RuntimeError("Could not find the Restart button on the page.")
                await btn.click()
                await page.wait_for_timeout(3000)
                return {**await scrape_status(), "already": None}

        except PlaywrightTimeout as e:
            raise RuntimeError(f"Timed out while talking to Aternos: {e}")
        finally:
            await browser.close()

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
        return error_embed("CAPTCHA Required", "Aternos triggered a CAPTCHA check and the bot can't proceed.\nTry again in a few minutes.")
    if any(x in msg for x in ("login", "credentials", "password", "username")):
        return error_embed("Login Failed", "Could not log into Aternos.\nCheck `ATERNOS_USERNAME` and `ATERNOS_PASSWORD` in your environment variables.")
    if "timed out" in msg or "timeout" in msg:
        return error_embed("Timeout", "Aternos took too long to respond. Try again in a moment.")
    if "start button" in msg or "stop button" in msg or "restart button" in msg:
        return error_embed("Button Not Found", f"Could not find the button on the Aternos page.\nAternos may have updated their site.")
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
        await ctx.send(embed=error_embed("Cooldown Active",
            f"You can use `+start` again in **{mins}m {secs}s**."))
        await log_command(ctx.author.id, "start", f"cooldown:{remaining}s")
        return

    thinking = await ctx.send(embed=info_embed("⏳  Connecting to Aternos…", "This may take up to 30 seconds.", colour=YELLOW))
    try:
        data = await aternos_action("start")

        if data.get("already") == "online":
            players = data.get("players_list", [])
            player_names = "\n".join(f"• {p}" for p in players) if players else "*No players online*"
            embed = info_embed("🟢  Server Already Online", colour=GREEN)
            embed.add_field(name="🌐 Server IP",      value=f"`{SERVER_IP}`", inline=True)
            embed.add_field(name="📦 Version",        value=data.get("version", "?"), inline=True)
            embed.add_field(name="👥 Players Online", value=f"{data.get('players_on','0')}/{data.get('players_max','20')}", inline=True)
            embed.add_field(name="📋 Player List",    value=player_names, inline=False)
            embed.set_footer(text=FOOTER_TEXT)
            await thinking.edit(embed=embed)
            await log_command(ctx.author.id, "start", "already_online")
            return

        if data.get("already") == "starting":
            embed = info_embed("🔄  Server Is Already Starting…", colour=YELLOW)
            embed.add_field(name="🌐 Server IP",      value=f"`{SERVER_IP}`", inline=True)
            embed.add_field(name="📦 Version",        value=data.get("version", "?"), inline=True)
            embed.add_field(name="📋 Queue Position", value=data.get("queue") or "In queue…", inline=True)
            embed.add_field(name="⏱️ Estimated Wait", value=data.get("eta") or "Calculating…", inline=True)
            embed.set_footer(text=FOOTER_TEXT)
            await thinking.edit(embed=embed)
            await log_command(ctx.author.id, "start", "already_starting")
            return

        embed = success_embed("Server is Starting! 🚀", "The server is booting up. It may take a few minutes.")
        embed.add_field(name="🌐 Server IP",      value=f"`{SERVER_IP}`", inline=True)
        embed.add_field(name="📦 Version",        value=data.get("version", "?"), inline=True)
        embed.add_field(name="📋 Queue Position", value=data.get("queue") or "In queue…", inline=True)
        embed.add_field(name="⏱️ Estimated Wait", value=data.get("eta") or "Calculating…", inline=True)
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
    confirm = info_embed("⚠️  Confirm Server Stop",
        "Are you sure you want to **stop** the Minecraft server?\nReply `yes` within 15 seconds to confirm.", colour=ORANGE)
    await ctx.send(embed=confirm)

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ("yes", "no")

    try:
        msg = await bot.wait_for("message", timeout=15.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(embed=info_embed("⏰  Timed Out", "Stop cancelled — no confirmation received.", colour=GREY))
        await log_command(ctx.author.id, "stop", "timeout")
        return

    if msg.content.lower() == "no":
        await ctx.send(embed=info_embed("🚫  Cancelled", "Server stop was cancelled.", colour=GREY))
        await log_command(ctx.author.id, "stop", "cancelled")
        return

    thinking = await ctx.send(embed=info_embed("⏳  Stopping server…", "This may take up to 30 seconds.", colour=YELLOW))
    try:
        data = await aternos_action("stop")
        if data.get("already") == "offline":
            await thinking.edit(embed=error_embed("Already Offline", "The server is already offline."))
            await log_command(ctx.author.id, "stop", "already_offline")
            return
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
    thinking = await ctx.send(embed=info_embed("⏳  Restarting server…", "This may take up to 30 seconds.", colour=YELLOW))
    try:
        data = await aternos_action("restart")
        if data.get("already") == "offline":
            await thinking.edit(embed=error_embed("Server Offline", "Can't restart — server is offline. Use `+start` instead."))
            await log_command(ctx.author.id, "restart", "offline")
            return
        embed = success_embed("Server Restarting 🔄", "The server is restarting. It will come back online shortly.")
        embed.add_field(name="🌐 Server IP", value=f"`{SERVER_IP}`", inline=True)
        embed.add_field(name="📦 Version",   value=data.get("version", "?"), inline=True)
        embed.set_footer(text=FOOTER_TEXT)
        await thinking.edit(embed=embed)
        await log_command(ctx.author.id, "restart", "restarted")
    except Exception as exc:
        await thinking.edit(embed=_handle_exc(exc, "restart"))
        await log_command(ctx.author.id, "restart", f"error:{exc}")

# ── +status ───────────────────────────────────────────────────────────────────
@bot.command(name="status")
async def status_cmd(ctx):
    thinking = await ctx.send(embed=info_embed("⏳  Fetching status…", "This may take up to 30 seconds.", colour=YELLOW))
    try:
        data = await aternos_action("status")
        status = data.get("status", "unknown")
        colour = GREEN if status == "online" else (YELLOW if status in ("starting", "loading") else RED)
        emoji  = {"online": "🟢", "offline": "🔴", "starting": "🟡", "loading": "🟡", "stopping": "🟠"}.get(status, "⚪")
        players = data.get("players_list", [])
        player_names = "\n".join(f"• {p}" for p in players) if players else "*No players online*"
        embed = info_embed(f"{emoji}  Server Status — {status.capitalize()}", colour=colour)
        embed.add_field(name="🌐 Server IP", value=f"`{SERVER_IP}`", inline=True)
        embed.add_field(name="📦 Version",   value=data.get("version", "?"), inline=True)
        embed.add_field(name="👥 Players",   value=f"{data.get('players_on','0')}/{data.get('players_max','20')}", inline=True)
        embed.add_field(name="📝 MOTD",      value=data.get("motd") or "*Not set*", inline=False)
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
    await ctx.send(embed=info_embed("📜  Logs Unavailable",
        "Console logs require the Aternos page to be open.\nUse `+status` to check if the server is online.", colour=GREY))
    await log_command(ctx.author.id, "logs", "unavailable")

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
