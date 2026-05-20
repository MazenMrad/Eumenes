import os
import discord
import discord.http
from discord import app_commands
from discord.ext import commands, tasks
import config
import db
import ocr
import io
import time
import asyncio
import json
import logging
import html as htmlmod
from pathlib import Path
from aiohttp import web

logger = logging.getLogger("eumenes")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

START_TIME = time.time()

STATE_FILE = Path(config.DB_DIR) / "bot_state.json"


def _load_state():
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"pending_imports": {}, "last_upload": {}}


def _save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(STATE_FILE)
    except Exception:
        pass


_state = _load_state()
pending_imports = _state["pending_imports"]
last_upload = _state["last_upload"]


def _persist_state():
    _state["pending_imports"] = pending_imports
    _state["last_upload"] = last_upload
    _save_state(_state)


def _apply_proxy(proxy_url):
    if hasattr(discord.http.HTTPClient.request, '_eumenes_proxied'):
        return
    discord.http.Route.BASE = proxy_url.rstrip("/") + "/api/v10"

    _orig_request = discord.http.HTTPClient.request

    async def _proxied_request(self, route, *, files=None, form=None, **kwargs):
        session = self._HTTPClient__session
        if not hasattr(session, '_eumenes_proxied'):
            orig = session.request

            def _session_request(method, url, **kw):
                kw.setdefault("headers", {})
                kw["headers"]["x-target-host"] = "discord.com"
                return orig(method, url, **kw)

            session.request = _session_request
            session._eumenes_proxied = True
        return await _orig_request(self, route, files=files, form=form, **kwargs)

    _proxied_request._eumenes_proxied = True
    discord.http.HTTPClient.request = _proxied_request


_proxy_url = os.environ.get("CLOUDFLARE_PROXY_URL", "")
if not _proxy_url and config.CLOUDFLARE_WORKERS_TOKEN:
    try:
        import cloudflare_proxy
        _deployed = cloudflare_proxy.setup(config.CLOUDFLARE_WORKERS_TOKEN)
        if _deployed:
            from urllib.parse import urlparse
            parsed = urlparse(_deployed)
            if parsed.scheme in ("https", "http") and parsed.hostname:
                _proxy_url = _deployed
                os.environ["CLOUDFLARE_PROXY_URL"] = _deployed
                logger.info("Cloudflare Proxy Worker deployed: %s", _deployed)
            else:
                logger.warning("Proxy URL malformed, skipping: %s", _deployed)
    except Exception as exc:
        logger.warning("Proxy deploy skipped: %s", exc)

if _proxy_url:
    _apply_proxy(_proxy_url)
    logger.info("Proxy routing active: %s", _proxy_url)


def fmt(n):
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"


def fmt_uptime():
    total = int(time.time() - START_TIME)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def esc(val):
    return htmlmod.escape(str(val))


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def is_admin():
    async def predicate(interaction: discord.Interaction):
        if not config.ADMIN_ROLE_ID or not interaction.guild:
            return True
        role = interaction.guild.get_role(int(config.ADMIN_ROLE_ID))
        return role in interaction.user.roles if role else True
    return app_commands.check(predicate)


def read_keepalive_status_file():
    path = Path("/tmp/eumenes-keepalive-status.json")
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"configured": False, "status": "unknown", "message": "Keepalive not yet deployed"}


async def status_payload():
    bot_name = str(bot.user) if bot.user else "connecting..."
    uptime_str = fmt_uptime()
    analytics = []
    try:
        analytics = db.get_analytics()
    except Exception:
        pass
    total_orders = sum(r["count"] for r in analytics) if analytics else 0
    total_revenue = sum(r["total"] for r in analytics) if analytics else 0
    merchants = 0
    buyers = 0
    products = 0
    codes_total = 0
    codes_used = 0
    try:
        conn = db.get_conn()
        merchants = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]
        buyers = conn.execute("SELECT COUNT(*) FROM buyers").fetchone()[0]
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        codes_total = conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
        codes_used = conn.execute("SELECT COUNT(*) FROM codes WHERE used = 1").fetchone()[0]
    except Exception:
        pass
    ks = read_keepalive_status_file()
    return {
        "ok": True,
        "bot": bot_name,
        "uptime": uptime_str,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(START_TIME)),
        "stats": {
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "merchants": merchants,
            "buyers": buyers,
            "products": products,
            "codes_total": codes_total,
            "codes_used": codes_used,
            "codes_available": codes_total - codes_used,
        },
        "keepalive": {
            "configured": ks.get("configured", False),
            "target_url": ks.get("targetUrl", ""),
            "worker_url": ks.get("workerUrl", ""),
            "cron": ks.get("cron", ""),
            "status": ks.get("status", "unknown"),
            "message": ks.get("message", ""),
            "error": ks.get("message", "") if ks.get("status") == "error" else "",
        },
        "proxy": {
            "configured": bool(_proxy_url),
            "url": _proxy_url or "",
        },
        "backup": {
            "configured": bool(config.HF_BACKUP_REPO and config.HF_TOKEN),
            "repo": config.HF_BACKUP_REPO or "",
        },
    }


def render_dashboard(data):
    s = data["stats"]
    k = data["keepalive"]
    bot_status = "Online" if data["bot"] != "connecting..." else "Starting"
    bot_tone = "ok" if data["bot"] != "connecting..." else "warn"
    keepalive_tone = "ok" if k["configured"] else ("warn" if k["error"] else "neutral")
    backup_tone = "ok" if data["backup"]["configured"] else "neutral"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Eumenes Dashboard</title>
<style>
  :root {{ color-scheme: dark; --bg:#08080f; --panel:#12111b; --line:#26243a; --text:#f6f4ff; --muted:#7f7a9e; --soft:#b8b3d7; --good:#22c55e; --warn:#f5c542; --bad:#fb7185; --accent:#6557df; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; font-family:Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; background:var(--bg); color:var(--text); font-size:13px; }}
  main {{ width:min(720px, calc(100% - 32px)); margin:0 auto; padding:36px 0 44px; }}
  header {{ text-align:center; margin-bottom:22px; }}
  h1 {{ margin:0; font-size:1.65rem; }}
  .subtitle {{ margin-top:8px; color:var(--muted); font-size:.72rem; text-transform:uppercase; letter-spacing:.14em; font-weight:800; }}
  .overview {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; margin-bottom:10px; }}
  .tile {{ border:1px solid var(--line); background:var(--panel); border-radius:11px; padding:18px; min-height:100px; display:flex; flex-direction:column; gap:8px; }}
  .tile.ok {{ border-color:rgba(34,197,94,.22); }}
  .tile.warn {{ border-color:rgba(245,197,66,.24); }}
  .tile.off {{ border-color:rgba(251,113,133,.28); }}
  .tile-head {{ display:flex; align-items:center; justify-content:space-between; }}
  .tile-title {{ color:var(--muted); font-size:.67rem; letter-spacing:.18em; text-transform:uppercase; font-weight:850; }}
  .tile-dot {{ width:7px; height:7px; border-radius:50%; background:var(--line); }}
  .tile.ok .tile-dot {{ background:var(--good); }}
  .tile.warn .tile-dot {{ background:var(--warn); }}
  .tile.off .tile-dot {{ background:var(--bad); }}
  .tile-value {{ font-size:1.12rem; font-weight:850; }}
  .tile-detail {{ color:var(--soft); line-height:1.45; font-size:.83rem; }}
  .badge {{ display:inline-flex; align-items:center; width:max-content; border-radius:999px; padding:4px 10px; font-size:.72rem; font-weight:850; line-height:1; text-transform:uppercase; border:1px solid var(--line); }}
  .badge.ok {{ color:var(--good); border-color:rgba(34,197,94,.34); background:rgba(34,197,94,.11); }}
  .badge.warn {{ color:var(--warn); border-color:rgba(245,197,66,.34); background:rgba(245,197,66,.11); }}
  .badge.off {{ color:var(--bad); border-color:rgba(251,113,133,.34); background:rgba(251,113,133,.11); }}
  .badge.neutral {{ color:var(--soft); }}
  code {{ background:#232234; border:1px solid #34324c; border-radius:6px; padding:2px 6px; font-size:.9em; }}
  footer {{ color:var(--muted); text-align:center; font-size:.74rem; margin-top:18px; }}
  @media (max-width:700px) {{ .overview {{ grid-template-columns:1fr; }} main {{ width:calc(100% - 22px); padding-top:28px; }} }}
</style>
</head>
<body>
<main>
  <header>
    <h1>Eumenes</h1>
    <div class="subtitle">Discord Auto-Fulfillment Bot</div>
  </header>
  <section class="overview">
    <article class="tile {bot_tone}">
      <div class="tile-head"><span class="tile-title">Bot</span><span class="tile-dot"></span></div>
      <div class="tile-value"><span class="badge {bot_tone}">{bot_status}</span></div>
      <div class="tile-detail">{esc(data["bot"])}</div>
    </article>
    <article class="tile neutral">
      <div class="tile-head"><span class="tile-title">Runtime</span><span class="tile-dot"></span></div>
      <div class="tile-value">{esc(data["uptime"])}</div>
      <div class="tile-detail">Port {config.HEALTH_PORT}</div>
    </article>
    <article class="tile neutral">
      <div class="tile-head"><span class="tile-title">Orders</span><span class="tile-dot"></span></div>
      <div class="tile-value">{s["total_orders"]} ({fmt(s["total_revenue"])} TND)</div>
      <div class="tile-detail">{s["merchants"]} merchants &middot; {s["buyers"]} buyers</div>
    </article>
    <article class="tile neutral">
      <div class="tile-head"><span class="tile-title">Products</span><span class="tile-dot"></span></div>
      <div class="tile-value">{s["products"]} products</div>
      <div class="tile-detail">{s["codes_available"]} codes avail &middot; {s["codes_used"]} used</div>
    </article>
    <article class="tile {keepalive_tone}">
      <div class="tile-head"><span class="tile-title">Keep Awake</span><span class="tile-dot"></span></div>
      <div class="tile-value"><span class="badge {keepalive_tone}">{esc("CF Cron" if k["configured"] else ("Error" if k["error"] else "Not configured"))}</span></div>
      <div class="tile-detail">{esc(k["error"] if k["error"] else (k["target_url"] if k["target_url"] else "CF Worker not deployed"))}</div>
    </article>
    <article class="tile {backup_tone}">
      <div class="tile-head"><span class="tile-title">Backup</span><span class="tile-dot"></span></div>
      <div class="tile-value"><span class="badge {backup_tone}">{esc("Configured" if data["backup"]["configured"] else "Disabled")}</span></div>
      <div class="tile-detail">{esc(data["backup"]["repo"] or "Not set")}</div>
    </article>
  </section>
  <footer>Eumenes Bot &mdash; huggingface.co/spaces/MazenMr/Eumenes</footer>
</main>
</body>
</html>"""


async def health_server():
    app = web.Application()

    async def health(request):
        return web.json_response({"ok": True, "bot": str(bot.user) if bot.user else "connecting...", "uptime": fmt_uptime()})

    async def status_json(request):
        return web.json_response(await status_payload())

    async def dashboard(request):
        data = await status_payload()
        return web.Response(text=render_dashboard(data), content_type="text/html")

    app.router.add_get("/health", health)
    app.router.add_get("/status", status_json)
    app.router.add_get("/", dashboard)
    app.router.add_get("/dashboard", dashboard)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HEALTH_PORT)
    await site.start()
    logger.info("Dashboard & health server on 0.0.0.0:%d", config.HEALTH_PORT)


def deploy_keepalive():
    import setup_keepalive
    result = setup_keepalive.main()
    if result == 0:
        s = setup_keepalive.read_status()
        if s.get("configured"):
            logger.info("Keep-alive Worker deployed: %s (pings %s)", s.get("workerUrl", ""), s.get("targetUrl", ""))
        else:
            logger.info("Keepalive: %s", s.get("message", "skipped"))
    else:
        s = setup_keepalive.read_status()
        logger.error("Keepalive deployment failed: %s", s.get("message", "unknown error"))


@bot.event
async def on_ready():
    if config.HF_BACKUP_REPO and config.HF_TOKEN:
        try:
            restored = db.restore_from_hf()
            if restored:
                logger.info("DB restored from HF backup")
                db.close_conn()
        except Exception as exc:
            logger.warning("HF restore failed: %s", exc)

    db.init_db()
    conn = db.get_conn()
    conn.execute("UPDATE orders SET status = 'pending' WHERE status = 'confirming'")
    conn.commit()
    try:
        db.backup_db()
    except Exception as exc:
        logger.warning("DB backup failed: %s", exc)
    if config.HF_BACKUP_REPO and config.HF_TOKEN:
        logger.info("HF backup loop starting (interval: %ds, repo: %s)", config.HF_BACKUP_INTERVAL, config.HF_BACKUP_REPO)
        hf_backup_loop.start()
    else:
        logger.warning("HF backup NOT started — HF_BACKUP_REPO or HF_TOKEN not set")
    asyncio.create_task(health_server())
    deploy_keepalive()

    existing = await bot.tree.fetch_commands()
    if not existing:
        for guild in bot.guilds:
            await bot.tree.sync(guild=guild)
        await bot.tree.sync()
        logger.info("Commands synced (first run)")
    else:
        logger.info("Commands already synced (%d global, %d guilds)", len(existing), len(bot.guilds))

    logger.info("Bot ready: %s", bot.user)


@tasks.loop(seconds=config.HF_BACKUP_INTERVAL)
async def hf_backup_loop():
    try:
        result = db.backup_to_hf()
        if result and result != "unchanged":
            logger.info("HF backup uploaded: %s", result)
        elif result == "unchanged":
            logger.debug("HF backup skipped — no DB changes")
        else:
            logger.error("HF backup failed")
    except Exception as exc:
        logger.error("HF backup failed: %s", exc)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild and message.attachments:
        await handle_receipt(message)

    if not message.guild and message.attachments:
        uid = str(message.author.id)
        if uid in pending_imports:
            import_started_at = pending_imports[uid].get("started_at", 0) if isinstance(pending_imports[uid], dict) else 0
            if time.time() - import_started_at > 300:
                del pending_imports[uid]
                _persist_state()
                return
            await handle_import_file(message)
            return

    await bot.process_commands(message)


MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB


async def handle_receipt(message):
    if message.guild is None:
        return

    attachment = message.attachments[0]
    if not any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
        return

    if attachment.size > MAX_IMAGE_SIZE:
        await message.channel.send(f"⚠️ Image too large (max {MAX_IMAGE_SIZE // (1024*1024)}MB).", delete_after=10)
        return

    uid = str(message.author.id)
    now = time.time()
    last_upload.update((k, v) for k, v in last_upload.items() if now - v <= 60)
    if uid in last_upload and now - last_upload[uid] < 30:
        await message.channel.send(f"⏳ @{message.author.name} please wait 30 seconds between uploads.", delete_after=5)
        return
    last_upload[uid] = now
    _persist_state()

    channel_name = message.channel.name
    if not channel_name.startswith("order"):
        return

    merchant_id = find_channel_merchant(message.channel, message.author.id)
    if not merchant_id:
        return

    async with message.channel.typing():
        img_bytes = await ocr.download_image(attachment.url)
        if not img_bytes:
            await message.channel.send("Could not download the image.")
            return

        result = await asyncio.to_thread(ocr.parse_receipt, img_bytes)

    if result.get("confidence", 0) < 0.5:
        try:
            merchant = await bot.fetch_user(int(merchant_id))
            await merchant.send(
                f"⚠️ @{message.author.name} sent an image in {message.channel.mention} "
                f"that couldn't be read as a receipt.\n"
                f"Please check manually."
            )
        except discord.HTTPException:
            pass
        return

    tx_id = result.get("tx_id", "")
    sender = result.get("sender", "")
    timestamp = result.get("timestamp", "")

    order_id = db.create_order(
        merchant_id=merchant_id,
        buyer_id=str(message.author.id),
        buyer_name=message.author.name,
        amount=result["amount"],
        tx_id=tx_id or "unknown",
        receipt_url=attachment.url,
    )
    if order_id is None:
        await message.channel.send("⚠️ This receipt was already submitted.", delete_after=10)
        return

    commission = result.get("commission", 0)
    auth_code = result.get("auth_code", "")
    suspicious = result.get("suspicious", False)

    duplicate = False
    if tx_id and tx_id != "unknown" and len(tx_id) > 4:
        existing = db.get_order_by_tx_id(tx_id)
        duplicate = existing is not None and existing["id"] != order_id

    details = f"**New order #{order_id}**\n"
    details += f"👤 Buyer: @{message.author.name}\n"
    details += f"💰 Amount: **{fmt(result['amount'])} TND**\n"
    if commission:
        details += f"💸 Commission: {fmt(commission)} TND\n"
    if tx_id:
        details += f"📋 Ref: `{tx_id}`\n"
    if auth_code:
        details += f"🔑 Auth: `{auth_code}`\n"
    if sender:
        details += f"👤 From: {sender}\n"
    if timestamp:
        details += f"🕐 Date: {timestamp}\n"

    if duplicate:
        details += "⚠️ **Warning: This ref was already used in another order!**\n"

    if suspicious:
        details += "⚠️ No camera data — could be AI-generated. Verify carefully.\n"

    db.upsert_buyer(str(message.author.id), message.author.name)
    db.upsert_buyer_merchant(str(message.author.id), merchant_id)
    db.log_audit(str(message.author.id), "order_created", "order", str(order_id), f"merchant={merchant_id} amount={result['amount']}")
    buyer = db.get_buyer(str(message.author.id))
    if buyer:
        details += f"\n📊 **Buyer:** {buyer['total_orders']} orders | {fmt(buyer['total_spent'])} TND spent | Trust: {buyer['trust_score']}"
        if buyer["flagged"]:
            details += " ⚠️ FLAGGED"
    details += "\nCheck your bank app then confirm."

    try:
        merchant_user = await bot.fetch_user(int(merchant_id))
    except discord.HTTPException:
        await message.channel.send("Could not notify merchant.")
        return

    merchant_data = db.get_merchant(merchant_id)
    auto_max = merchant_data.get("auto_confirm_max", 0) if merchant_data else 0
    trust_mode = merchant_data.get("trust_mode", "local") if merchant_data else "local"

    buyer_trust = 0
    if trust_mode == "global":
        buyer_trust = db.get_trust_score_with_decay(str(message.author.id))
    elif trust_mode == "weighted":
        global_t = db.get_trust_score_with_decay(str(message.author.id))
        local_r = db.get_buyer_merchant_trust(str(message.author.id), merchant_id)
        local_t = local_r["trust_score"] if local_r else 50
        buyer_trust = int(0.7 * global_t + 0.3 * local_t)
    else:
        local_r = db.get_buyer_merchant_trust(str(message.author.id), merchant_id)
        buyer_trust = local_r["trust_score"] if local_r else 50

    if auto_max > 0 and result["amount"] <= auto_max + 0.005 and buyer_trust >= config.AUTO_CONFIRM_THRESHOLD and not suspicious and not duplicate:
        await auto_fulfill(order_id, merchant_id, result["amount"], str(message.author.id), message.author.name, message.channel.id)
        await message.channel.send(f"✅ **{fmt(result['amount'])} TND** — auto-confirmed.")
        return

    view = ConfirmView(order_id, result["amount"], merchant_id, str(message.author.id), message.channel.id)
    try:
        await merchant_user.send(details, view=view)
    except discord.HTTPException:
        await message.channel.send("⚠️ Merchant has DMs disabled.")
        return
    await message.channel.send(f"✅ Receipt read — **{result['amount']} TND**. Merchant notified.")


async def auto_fulfill(order_id, merchant_id, amount, buyer_id, buyer_name, channel_id):
    products = db.get_products(merchant_id)
    matching = [p for p in products if abs(p["price"] - amount) < 1.0 and p["stock"] > 0]
    if not matching:
        if channel_id:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(f"⚠️ Auto-confirm failed: no matching product for **{amount} TND**.")
        return

    product = matching[0]
    code = db.assign_code(product["id"], order_id)
    if not code:
        if channel_id:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(f"⚠️ Auto-confirm failed: out of stock for **{product['name']}**.")
        return

    db.update_order_status(order_id, "delivered")
    db.log_audit(merchant_id, "order_auto_confirmed", "order", str(order_id), f"amount={amount} buyer={buyer_id} product={product['id']}")
    db.log_audit(merchant_id, "code_assigned", "code", code, f"order_id={order_id}")
    db.update_buyer_merchant_on_delivery(buyer_id, merchant_id, amount)

    try:
        buyer = await bot.fetch_user(int(buyer_id))
        await buyer.send(f"**Your order is here!**\nProduct: {product['name']}\nCode: `{code}`")
    except discord.HTTPException:
        pass

    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"✅ **Auto-delivered**\n"
                f"Product: {product['name']} ({product['price']} TND)\n"
                f"Code: `{code}`\n"
                f"Buyer: {buyer_name}"
            )

    db.add_trust_event(buyer_id, "purchase_completed", config.TRUST_CONFIRM_BONUS, order_id)
    db.upsert_buyer(buyer_id, buyer_name)
    db.update_buyer_stats(buyer_id)


def find_channel_merchant(channel, buyer_id=None):
    role_id = db.get_merchant_role(str(channel.guild.id))
    if role_id:
        for member in channel.members:
            if member.bot or str(member.id) == buyer_id:
                continue
            if any(r.id == int(role_id) for r in member.roles):
                return str(member.id)
    for member in channel.members:
        if member.bot or str(member.id) == buyer_id:
            continue
        if member.guild_permissions.administrator:
            return str(member.id)
    for member in channel.members:
        if member.bot or str(member.id) == buyer_id:
            continue
        if member.guild_permissions.manage_messages or member.guild_permissions.manage_channels:
            return str(member.id)
    return None


class PaginatedOrdersView(discord.ui.View):
    def __init__(self, rows, title, fmt_order, per_page=10):
        super().__init__(timeout=120)
        self.rows = rows
        self.title = title
        self.fmt_order = fmt_order
        self.per_page = per_page
        self.page = 0
        self.max_page = max(0, (len(rows) - 1) // per_page)
        self.update_buttons()

    def update_buttons(self):
        self.max_page = max(0, (len(self.rows) - 1) // self.per_page) if self.rows else 0
        if self.page > self.max_page:
            self.page = self.max_page
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.max_page

    def build_embed(self):
        embed = discord.Embed(title=self.title, color=0x2b2d31)
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.rows[start:end]
        if not chunk:
            embed.description = "No orders yet."
        else:
            for o in chunk:
                name, val = self.fmt_order(o)
                embed.add_field(name=name, value=val, inline=False)
        embed.set_footer(text=f"Page {self.page + 1} / {self.max_page + 1}  •  {len(self.rows)} orders")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class DeleteCodeModal(discord.ui.Modal, title="Delete a Code"):
    code_value = discord.ui.TextInput(label="Code value to delete", placeholder="e.g. ABC-123", max_length=100)

    def __init__(self, product_id):
        super().__init__()
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        all_codes = db.get_all_codes(self.product_id)
        match = [c for c in all_codes if c["code_value"] == self.code_value.strip()]
        if not match:
            await interaction.response.send_message("Code not found in this product.", ephemeral=True)
            return
        ok = db.delete_code(match[0]["id"], str(interaction.user.id))
        if ok:
            db.log_audit(str(interaction.user.id), "code_deleted", "code", self.code_value.strip(), f"product_id={self.product_id}")
            await interaction.response.send_message(f"🗑️ Deleted `{self.code_value.strip()}`.", ephemeral=True)
        else:
            await interaction.response.send_message("Could not delete code.", ephemeral=True)


class CodesPaginatedView(discord.ui.View):
    def __init__(self, rows, title, fmt_code, product_id, per_page=15):
        super().__init__(timeout=120)
        self.rows = rows
        self.title = title
        self.fmt_code = fmt_code
        self.per_page = per_page
        self.product_id = product_id
        self.page = 0
        self.max_page = max(0, (len(rows) - 1) // per_page)
        self.update_buttons()

    def update_buttons(self):
        self.max_page = max(0, (len(self.rows) - 1) // self.per_page) if self.rows else 0
        if self.page > self.max_page:
            self.page = self.max_page
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.max_page

    def build_embed(self):
        embed = discord.Embed(title=self.title, color=0x2b2d31)
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.rows[start:end]
        for c in chunk:
            name, val = self.fmt_code(c)
            embed.add_field(name=name, value=val, inline=False)
        embed.set_footer(text=f"Page {self.page + 1} / {self.max_page + 1}  •  {len(self.rows)} codes")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="🗑 Delete a code", style=discord.ButtonStyle.red, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DeleteCodeModal(self.product_id))


class ConfirmView(discord.ui.View):
    def __init__(self, order_id, amount, merchant_id, buyer_id, channel_id=None):
        super().__init__(timeout=3600)
        self.order_id = order_id
        self.amount = amount
        self.merchant_id = merchant_id
        self.buyer_id = buyer_id
        self.channel_id = channel_id

    @discord.ui.button(label="✅ Confirm & Deliver", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = db.get_order(self.order_id)
        if not order or order["status"] != "pending":
            await interaction.response.send_message("Order already processed.", ephemeral=True)
            return

        db.update_order_status(self.order_id, "confirming")

        products = db.get_products(self.merchant_id)
        matching = [p for p in products if abs(p["price"] - self.amount) < 1.0 and p["stock"] > 0]

        if not matching:
            closest = sorted(products, key=lambda p: abs(p["price"] - self.amount))
            msg = f"No codes for **{self.amount} TND**. Your products:\n"
            for p in closest[:3]:
                msg += f"- {p['name']} ({p['price']} TND) — stock: {p['stock']}\n"
            msg += "Use `/add-product` to add codes for this amount."
            db.update_order_status(self.order_id, "pending")
            await interaction.response.edit_message(content=interaction.message.content, view=None)
            await interaction.followup.send(msg, ephemeral=True)
            return

        product = matching[0]
        code = db.assign_code(product["id"], self.order_id)
        if not code:
            db.update_order_status(self.order_id, "pending")
            await interaction.response.edit_message(content=interaction.message.content, view=None)
            await interaction.followup.send("Out of stock!", ephemeral=True)
            return

        db.update_order_status(self.order_id, "delivered")
        db.log_audit(self.merchant_id, "order_confirmed", "order", str(self.order_id), f"amount={self.amount} buyer={self.buyer_id}")
        db.log_audit(self.merchant_id, "code_assigned", "code", code, f"order_id={self.order_id}")
        db.update_buyer_merchant_on_delivery(self.buyer_id, self.merchant_id, self.amount)

        await interaction.response.edit_message(
            content=f"✅ Delivered {product['name']} code — {self.amount} TND",
            view=None,
        )

        try:
            buyer = await bot.fetch_user(int(self.buyer_id))
            await buyer.send(f"**Your order is here!**\nProduct: {product['name']}\nCode: `{code}`")
        except discord.HTTPException:
            pass

        if self.channel_id:
            channel = bot.get_channel(self.channel_id)
            if channel:
                await channel.send(
                    f"✅ **Order delivered**\n"
                    f"Product: {product['name']} ({product['price']} TND)\n"
                    f"Code: `{code}`\n"
                    f"Buyer: {order['buyer_name']}"
                )

        db.add_trust_event(self.buyer_id, "purchase_completed", config.TRUST_CONFIRM_BONUS, self.order_id)
        db.update_buyer_stats(self.buyer_id)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.update_order_status(self.order_id, "rejected")

        rejection_rate = db.get_merchant_rejection_rate(self.merchant_id)
        if rejection_rate > config.REJECTION_RATE_HIGH:
            weight = config.REJECTION_WEIGHT_HIGH
        elif rejection_rate < config.REJECTION_RATE_LOW:
            weight = config.REJECTION_WEIGHT_LOW
        else:
            weight = 1.0
        penalty = int(config.TRUST_REJECT_PENALTY * weight)

        db.add_trust_event(self.buyer_id, "order_rejected", -penalty, self.order_id)
        db.update_buyer_merchant_on_rejection(self.buyer_id, self.merchant_id, penalty)
        db.update_buyer_stats(self.buyer_id)
        db.log_audit(self.merchant_id, "order_rejected", "order", str(self.order_id), f"amount={self.amount} buyer={self.buyer_id} weight={weight}")

        await interaction.response.edit_message(
            content=f"❌ Order #{self.order_id} rejected (penalty: {penalty})",
            view=None,
        )

        try:
            buyer = await bot.fetch_user(int(self.buyer_id))
            await buyer.send(f"❌ Your order #{self.order_id} was rejected by the merchant.")
        except discord.HTTPException:
            pass

        if self.channel_id:
            channel = bot.get_channel(self.channel_id)
            if channel:
                await channel.send(f"❌ Order #{self.order_id} was rejected by the merchant.")


@bot.tree.command(name="setup", description="Register as a merchant")
async def setup(interaction: discord.Interaction):
    db.create_merchant(str(interaction.user.id), interaction.user.name)
    await interaction.response.send_message(
        "✅ Registered! Set up auto-confirm with `/auto-confirm <amount>`",
        ephemeral=True,
    )


@bot.tree.command(name="auto-confirm", description="Auto-deliver orders below this amount (no merchant tap)")
@app_commands.describe(amount="Max TND to auto-confirm (0 to disable)")
async def auto_confirm(interaction: discord.Interaction, amount: float):
    if amount < 0:
        await interaction.response.send_message("Amount cannot be negative.", ephemeral=True)
        return
    if amount > 10000:
        await interaction.response.send_message("Max auto-confirm amount is 10,000 TND.", ephemeral=True)
        return
    db.create_merchant(str(interaction.user.id), interaction.user.name)
    db.update_merchant(str(interaction.user.id), auto_confirm_max=amount)
    db.log_audit(str(interaction.user.id), "auto_confirm_changed", "merchant", str(interaction.user.id), f"amount={amount}")
    if amount > 0:
        await interaction.response.send_message(
            f"✅ Orders ≤ **{amount} TND** will auto-deliver for buyers with trust ≥ {config.AUTO_CONFIRM_THRESHOLD}.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message("Auto-confirm disabled.", ephemeral=True)


class AddProductModal(discord.ui.Modal, title="Add Product"):
    name = discord.ui.TextInput(label="Product Name", placeholder="e.g. Netflix 1 Month", max_length=50)
    price = discord.ui.TextInput(label="Price (TND)", placeholder="e.g. 25", max_length=10)
    codes = discord.ui.TextInput(label="Product Codes (one per line)", placeholder="ABC-123-XYZ\nDEF-456-UVW\nGHI-789-RST", style=discord.TextStyle.paragraph, max_length=2000)

    async def on_submit(self, interaction: discord.Interaction):
        db.create_merchant(str(interaction.user.id), interaction.user.name)
        try:
            price = float(self.price.value)
        except ValueError:
            await interaction.response.send_message("Invalid price.", ephemeral=True)
            return
        code_list = self.codes.value.strip().splitlines()
        code_list = [c.strip() for c in code_list if c.strip()]
        if not code_list:
            await interaction.response.send_message("No codes provided.", ephemeral=True)
            return
        product_id = db.add_product(str(interaction.user.id), self.name.value, price, code_list)
        db.log_audit(str(interaction.user.id), "product_added", "product", str(product_id), f"name={self.name.value} price={price} codes={len(code_list)}")
        await interaction.response.send_message(
            f"✅ Added **{self.name.value}** ({price} TND) with **{len(code_list)}** codes.",
            ephemeral=True,
        )


class EditProductModal(discord.ui.Modal, title="Edit Product"):
    name = discord.ui.TextInput(label="Product Name", required=False, max_length=50)
    price = discord.ui.TextInput(label="Price (TND)", required=False, max_length=10)

    def __init__(self, product_id, current_name, current_price):
        super().__init__()
        self.product_id = product_id
        self.name.default = current_name
        self.price.default = str(fmt(current_price))

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name.value.strip() or None
        price = None
        if self.price.value.strip():
            try:
                price = float(self.price.value.strip())
            except ValueError:
                await interaction.response.send_message("Invalid price.", ephemeral=True)
                return
        db.update_product(self.product_id, str(interaction.user.id), name=name, price=price)
        db.log_audit(str(interaction.user.id), "product_edited", "product", str(self.product_id), f"name={name} price={price}")
        await interaction.response.send_message("✅ Product updated.", ephemeral=True)


class AddCodesModal(discord.ui.Modal, title="Add Codes"):
    codes = discord.ui.TextInput(label="Codes (one per line)", placeholder="ABC-123-XYZ\nDEF-456-UVW", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, product_id, product_name):
        super().__init__()
        self.product_id = product_id
        self.product_name = product_name

    async def on_submit(self, interaction: discord.Interaction):
        code_list = self.codes.value.strip().splitlines()
        code_list = [c.strip() for c in code_list if c.strip()]
        if not code_list:
            await interaction.response.send_message("No codes provided.", ephemeral=True)
            return
        count = db.add_codes_to_product(self.product_id, code_list)
        await interaction.response.send_message(
            f"✅ Added **{count}** codes to **{self.product_name}**.",
            ephemeral=True,
        )


async def product_autocomplete(interaction: discord.Interaction, current: str):
    products = db.get_products(str(interaction.user.id))
    return [
        app_commands.Choice(name=f"{p['name']} ({p['price']} TND) — stock: {p['stock']}", value=str(p['id']))
        for p in products if current.lower() in p['name'].lower()
    ][:25]


@bot.tree.command(name="add-product", description="Add a product with codes to your vault")
async def add_product(interaction: discord.Interaction):
    await interaction.response.send_modal(AddProductModal())


@bot.tree.command(name="stock", description="Check your product stock")
async def stock(interaction: discord.Interaction):
    products = db.get_products(str(interaction.user.id))
    embed = discord.Embed(title="Your Vault", color=0x2b2d31)
    if not products:
        embed.description = "No products yet. Use `/add-product` to add one."
    else:
        for p in products:
            name = f"`[{p['id']}]` {p['name']}"
            val = f"Price: **{fmt(p['price'])} TND** — Stock: **{p['stock']}**"
            embed.add_field(name=name, value=val, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="remove-product", description="Delete a product and its unused codes")
@app_commands.describe(product="Select a product to delete")
@app_commands.autocomplete(product=product_autocomplete)
async def remove_product(interaction: discord.Interaction, product: str):
    pid = int(product)
    p = db.get_product_by_id(pid, str(interaction.user.id))
    if not p:
        await interaction.response.send_message("Product not found.", ephemeral=True)
        return
    db.delete_product(pid, str(interaction.user.id))
    db.log_audit(str(interaction.user.id), "product_removed", "product", str(pid), f"name={p['name']}")
    await interaction.response.send_message(
        f"🗑️ Removed **{p['name']}** ({p['price']} TND) and all its codes.",
        ephemeral=True,
    )


@bot.tree.command(name="add-codes", description="Add more codes to an existing product")
@app_commands.describe(product="Select a product")
@app_commands.autocomplete(product=product_autocomplete)
async def add_codes(interaction: discord.Interaction, product: str):
    pid = int(product)
    p = db.get_product_by_id(pid, str(interaction.user.id))
    if not p:
        await interaction.response.send_message("Product not found.", ephemeral=True)
        return
    await interaction.response.send_modal(AddCodesModal(pid, p["name"]))


@bot.tree.command(name="edit-product", description="Edit a product name or price")
@app_commands.describe(product="Select a product to edit")
@app_commands.autocomplete(product=product_autocomplete)
async def edit_product(interaction: discord.Interaction, product: str):
    pid = int(product)
    p = db.get_product_by_id(pid, str(interaction.user.id))
    if not p:
        await interaction.response.send_message("Product not found.", ephemeral=True)
        return
    await interaction.response.send_modal(EditProductModal(pid, p["name"], p["price"]))


@bot.tree.command(name="codes", description="View and manage codes for a product")
@app_commands.describe(product="Select a product")
@app_commands.autocomplete(product=product_autocomplete)
async def codes(interaction: discord.Interaction, product: str):
    pid = int(product)
    p = db.get_product_by_id(pid, str(interaction.user.id))
    if not p:
        await interaction.response.send_message("Product not found.", ephemeral=True)
        return
    all_codes = db.get_all_codes(pid)
    if not all_codes:
        await interaction.response.send_message("No codes for this product.", ephemeral=True)
        return
    def fmt_code(c):
        status = "✅" if not c["used"] else f"❌ used on order #{c['order_id']}"
        return f"`{c['code_value']}`", status
    view = CodesPaginatedView(all_codes, f"Codes — {p['name']}", fmt_code, pid)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="restock", description="Return a code back to usable pool")
@app_commands.describe(code="The product code to restock")
async def restock(interaction: discord.Interaction, code: str):
    ok = db.restock_code(code.strip(), str(interaction.user.id))
    if ok:
        db.log_audit(str(interaction.user.id), "code_restocked", "code", code.strip(), "")
        await interaction.response.send_message(f"✅ Code `{code}` is now available again.", ephemeral=True)
    else:
        await interaction.response.send_message("Code not found or not yours.", ephemeral=True)


@bot.tree.command(name="orders", description="View your recent orders")
async def orders(interaction: discord.Interaction):
    rows = db.get_all_merchant_orders(str(interaction.user.id))
    if not rows:
        await interaction.response.send_message("No orders yet.", ephemeral=True)
        return
    def fmt_order(o):
        return f"#{o['id']} — {o['status'].capitalize()}", f"Buyer: {o['buyer_name']} — **{o['amount']} TND**"
    view = PaginatedOrdersView(rows, "Recent Orders", fmt_order)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="buyers", description="View all buyers and their trust scores")
async def buyers(interaction: discord.Interaction):
    merchant_id = str(interaction.user.id)
    local_rows = db.get_merchant_buyers(merchant_id)
    global_rows = db.get_all_buyers(merchant_id)
    embed = discord.Embed(title="Buyers", color=0x2b2d31)
    if not local_rows and not global_rows:
        embed.description = "No buyers yet."
    else:
        seen = set()
        for b in local_rows:
            seen.add(b["buyer_id"])
            flag = " ⚠️" if b["flagged"] else ""
            embed.add_field(
                name=f"{b['name']}{flag} (this shop)",
                value=f"Local trust: **{b['trust_score']}** | Global: **{b['global_trust']}** | Orders: {b['total_orders']} | Spent: {fmt(b['total_spent'])} TND",
                inline=False,
            )
        for b in global_rows:
            if b["discord_id"] not in seen:
                flag = " ⚠️" if b["flagged"] else ""
                embed.add_field(
                    name=f"{b['name']}{flag} (global)",
                    value=f"Trust: **{b['trust_score']}** | Orders: {b['total_orders']} | Spent: {fmt(b['total_spent'])} TND",
                    inline=False,
                )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="trust", description="Check your buyer trust score")
async def trust(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    global_score = db.get_trust_score_with_decay(uid)
    raw_score = db.get_trust_score(uid)
    network = db.get_buyer_network(uid)
    msg = f"🌐 **Global trust:** {global_score} (raw: {raw_score})\n"
    msg += f"Starts at 50. Earn +2 per completed purchase. "
    msg += f"Trust ≥ {config.AUTO_CONFIRM_THRESHOLD} enables auto-confirm.\n"
    if network:
        msg += f"\n**Shops you've traded with:** {len(network)}\n"
        for rel in network[:3]:
            msg += f"• merchant `{rel['merchant_id'][:8]}` — trust: {rel['trust_score']} | orders: {rel['total_orders']}\n"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="trust-mode", description="Set how buyer trust is calculated (local/global/weighted)")
@app_commands.describe(mode="local = per-shop reset, global = trust follows buyer, weighted = 70% global + 30% local")
@app_commands.choices(mode=[
    app_commands.Choice(name="local — trust resets per shop", value="local"),
    app_commands.Choice(name="global — trust follows buyer everywhere", value="global"),
    app_commands.Choice(name="weighted — 70% global + 30% local", value="weighted"),
])
async def trust_mode(interaction: discord.Interaction, mode: str):
    db.create_merchant(str(interaction.user.id), interaction.user.name)
    db.update_merchant(str(interaction.user.id), trust_mode=mode)
    db.log_audit(str(interaction.user.id), "trust_mode_changed", "merchant", str(interaction.user.id), f"mode={mode}")
    await interaction.response.send_message(f"✅ Trust mode set to **{mode}**.", ephemeral=True)


@bot.tree.command(name="network", description="See which merchants you've traded with")
async def network(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    network = db.get_buyer_network(uid)
    embed = discord.Embed(title="Your Merchant Network", color=0x2b2d31)
    if not network:
        embed.description = "No merchant relationships yet. Place an order to build trust."
    else:
        for rel in network:
            embed.add_field(
                name=f"Merchant `{rel['merchant_id'][:8]}`",
                value=f"Trust: **{rel['trust_score']}** | Orders: {rel['total_orders']} | Spent: {rel['total_spent']} TND",
                inline=False,
            )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="View your sales stats")
async def stats(interaction: discord.Interaction):
    rows = db.get_analytics(str(interaction.user.id))
    embed = discord.Embed(title="Your Stats", color=0x2b2d31)
    total_orders = sum(r["count"] for r in rows)
    total_revenue = sum(r["total"] for r in rows)
    embed.add_field(name="Total Orders", value=str(total_orders), inline=True)
    embed.add_field(name="Total Revenue", value=f"{fmt(total_revenue)} TND", inline=True)
    for r in rows:
        embed.add_field(name=r["status"].capitalize(), value=f"{r['count']} orders ({fmt(r['total'])} TND)", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="admin-orders", description="[Admin] View all orders in the server")
@is_admin()
async def admin_orders(interaction: discord.Interaction):
    rows = db.get_all_orders()
    if not rows:
        await interaction.response.send_message("No orders.", ephemeral=True)
        return
    def fmt_admin_order(o):
        return f"#{o['id']} — {o['status'].capitalize()}", f"Merchant: {o['merchant_id'][:8]} — **{o['amount']} TND**"
    view = PaginatedOrdersView(rows, "All Orders", fmt_admin_order)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="admin-stats", description="[Admin] View global stats")
@is_admin()
async def admin_stats(interaction: discord.Interaction):
    rows = db.get_analytics()
    embed = discord.Embed(title="Global Stats", color=0x2b2d31)
    total_orders = sum(r["count"] for r in rows)
    total_revenue = sum(r["total"] for r in rows)
    embed.add_field(name="Total Orders", value=str(total_orders), inline=True)
    embed.add_field(name="Total Revenue", value=f"{fmt(total_revenue)} TND", inline=True)
    for r in rows:
        embed.add_field(name=r["status"].capitalize(), value=f"{r['count']} orders ({fmt(r['total'])} TND)", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def handle_import_file(message):
    uid = str(message.author.id)
    entry = pending_imports.get(uid)
    if not entry:
        return
    product_id = entry.get("product_id")
    product_name = entry.get("product_name", "unknown")
    attachment = message.attachments[0]
    filename = attachment.filename.lower()

    if not any(filename.endswith(ext) for ext in [".txt", ".csv"]):
        await message.channel.send("Please upload a .txt or .csv file with one code per line.")
        return

    try:
        content = (await attachment.read()).decode("utf-8-sig")
    except Exception:
        await message.channel.send("Could not read file. Make sure it's UTF-8 encoded.")
        return

    codes = [line.strip() for line in content.splitlines() if line.strip()]
    if not codes:
        await message.channel.send("No codes found in file.")
        return

    count = db.add_codes_to_product(product_id, codes)
    db.log_audit(uid, "codes_imported", "product", str(product_id), f"codes={count} name={product_name}")
    del pending_imports[uid]
    _persist_state()
    await message.channel.send(f"✅ Imported **{count}** codes into **{product_name}**.")


@bot.tree.command(name="export-codes", description="Export unused codes as a .txt file")
@app_commands.describe(product="Select a product")
@app_commands.autocomplete(product=product_autocomplete)
async def export_codes(interaction: discord.Interaction, product: str):
    pid = int(product)
    p = db.get_product_by_id(pid, str(interaction.user.id))
    if not p:
        await interaction.response.send_message("Product not found.", ephemeral=True)
        return
    codes = db.get_unused_codes(pid)
    if not codes:
        await interaction.response.send_message("No unused codes.", ephemeral=True)
        return
    content = "\n".join(codes)
    file = discord.File(io.BytesIO(content.encode()), filename=f"{p['name']}-codes.txt")
    await interaction.response.send_message(
        f"**{len(codes)}** unused codes for **{p['name']}**:",
        file=file,
        ephemeral=True,
    )


@bot.tree.command(name="import-codes", description="Import codes from a .txt or .csv file")
@app_commands.describe(product="Select a product")
@app_commands.autocomplete(product=product_autocomplete)
async def import_codes(interaction: discord.Interaction, product: str):
    pid = int(product)
    p = db.get_product_by_id(pid, str(interaction.user.id))
    if not p:
        await interaction.response.send_message("Product not found.", ephemeral=True)
        return
    pending_imports[str(interaction.user.id)] = {"product_id": pid, "product_name": p["name"], "started_at": time.time()}
    await interaction.response.send_message(
        f"Send me a .txt or .csv file with one code per line in a DM.",
        ephemeral=True,
    )
    try:
        await interaction.user.send(f"Upload a .txt or .csv file for **{p['name']}**. One code per line.")
    except discord.HTTPException:
        await interaction.followup.send("I can't DM you. Enable DMs and try again.", ephemeral=True)


@bot.tree.command(name="export-orders", description="Export all orders as CSV")
async def export_orders(interaction: discord.Interaction):
    orders = db.get_all_merchant_orders(str(interaction.user.id))
    if not orders:
        await interaction.response.send_message("No orders to export.", ephemeral=True)
        return
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "buyer_id", "buyer_name", "product_name", "amount", "tx_id", "status", "created_at", "confirmed_at", "delivered_at"])
    for o in orders:
        w.writerow([o["id"], o["buyer_id"], o["buyer_name"], o["product_name"], o["amount"], o["tx_id"], o["status"], o["created_at"], o.get("confirmed_at", ""), o.get("delivered_at", "")])
    buf.seek(0)
    file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="orders.csv")
    await interaction.response.send_message(f"**{len(orders)}** orders exported.", file=file, ephemeral=True)


@bot.tree.command(name="analytics", description="View your detailed sales analytics")
async def analytics(interaction: discord.Interaction):
    a = db.get_merchant_analytics_extended(str(interaction.user.id))
    embed = discord.Embed(title="Analytics", color=0x2b2d31)
    embed.add_field(name="Total Orders", value=str(a["total_orders"]), inline=True)
    embed.add_field(name="Delivered", value=str(a["delivered"]), inline=True)
    embed.add_field(name="Rejected", value=str(a["rejected"]), inline=True)
    embed.add_field(name="Conversion Rate", value=f"{a['conversion_rate']}%", inline=True)
    embed.add_field(name="Revenue", value=f"{fmt(a['revenue'])} TND", inline=True)
    embed.add_field(name="Avg Confirm Time", value=f"{a['avg_confirm_hours']}h", inline=True)
    embed.add_field(name="Unique Buyers", value=str(a["unique_buyers"]), inline=True)
    embed.add_field(name="Rejection Rate", value=f"{a['rejection_rate']}%", inline=True)
    if a["top_buyers"]:
        top = "\n".join(f"{b['buyer_name']}: {b['cnt']} orders ({fmt(b['spent'])} TND)" for b in a["top_buyers"])
        embed.add_field(name="Top Buyers", value=top, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set-merchant-role", description="[Admin] Set the role that identifies merchants in ticket channels")
@app_commands.describe(role="The role to use for merchant identification")
@is_admin()
async def set_merchant_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    db.set_merchant_role(str(interaction.guild.id), str(role.id))
    db.log_audit(str(interaction.user.id), "merchant_role_set", "server", str(interaction.guild.id), f"role={role.name} id={role.id}")
    await interaction.response.send_message(
        f"✅ Merchants will now be identified by the **@{role.name}** role.\n"
        f"Anyone with this role in a ticket channel will receive order confirmations.",
        ephemeral=True,
    )


@bot.tree.command(name="flag-buyer", description="Flag or unflag a buyer")
@app_commands.describe(user="The buyer to flag/unflag")
async def flag_buyer(interaction: discord.Interaction, user: discord.User):
    uid = str(user.id)
    buyer = db.get_buyer(uid)
    if not buyer:
        await interaction.response.send_message("No buyer found with that ID.", ephemeral=True)
        return
    new_flag = db.toggle_buyer_flag(uid)
    status = "Flagged ⚠️" if new_flag else "Unflagged ✅"
    db.log_audit(str(interaction.user.id), "buyer_flagged" if new_flag else "buyer_unflagged", "buyer", uid, f"name={user.name}")
    await interaction.response.send_message(
        f"{status} **{user.name}** (trust: {buyer['trust_score']}, orders: {buyer['total_orders']})",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(config.DISCORD_BOT_TOKEN)
