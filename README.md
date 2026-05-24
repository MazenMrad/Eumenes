# Eumenes

<div align="center">
  <img src="Untitled design.png" alt="Eumenes Logo" width="120" />
  <h3>Discord Auto-Fulfillment Bot for Digital Product Sales</h3>
  <p>Accept payment receipts via OCR, auto-deliver product codes, and manage your digital storefront — all from Discord.</p>
</div>

<div align="center">
  <video src="eumenes demo.mp4" controls width="640" muted></video>
  <p><em>Demo: Receipt upload → OCR parsing → Merchant confirmation → Code delivery</em></p>
</div>

> **Note:** This is a personal portfolio project. Sensitive data (tokens, keys, credentials) has been removed from the repository. Do not deploy this code as-is without adding your own secrets and reviewing security configurations.

---

## Features

- **Receipt OCR** — Buyers upload payment screenshots; Tesseract OCR (Arabic + French + English) extracts amount, transaction ID, sender, and timestamp
- **Merchant DM Confirmations** — Merchants receive a DM with Confirm/Reject buttons for every order
- **Auto-Delivery** — Set an auto-confirm threshold; buyers with trust ≥ 70 get instant delivery without merchant interaction
- **Product Vault** — Store product codes per product; assigned FIFO on delivery, restockable
- **Buyer Trust System** — Trust starts at 50, +2 per delivery, -5 per rejection (weighted by merchant rejection rate); supports local/global/weighted modes
- **AI Receipt Detection** — Scans raw image bytes for C2PA markers (c2pa, openai, dall-e, stability.ai) to flag suspicious uploads
- **Duplicate Prevention** — Same transaction ref can't be used in two non-rejected orders
- **23 Slash Commands** — Full merchant, admin, and buyer management
- **Hugging Face Deployment** — Runs on HF Spaces with Cloudflare Worker reverse proxy (bypasses HF outbound restrictions) and cron keepalive
- **Auto Backup** — SQLite database backed up to a private Hugging Face dataset on configurable intervals
- **Built-in Dashboard** — Live status page at `/:7860` with bot health, order stats, and keepalive status
- **Zero External AI APIs** — Runs fully offline with local Tesseract OCR

---

## Quick Start

### Local

1. Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) and ensure `tesseract` is in your PATH
2. Copy `.env.example` to `.env` and fill in your `DISCORD_BOT_TOKEN`
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run:
   ```bash
   python bot.py
   ```

### Hugging Face Spaces

1. Create a new Space at [huggingface.co/spaces](https://huggingface.co/spaces) (Docker template)
2. Push this repo to your Space
3. Add these **Space Secrets**:
   - `DISCORD_BOT_TOKEN` (mandatory)
   - `CLOUDFLARE_WORKERS_TOKEN` (mandatory — auto-deploys proxy + keepalive workers)
   - `HF_TOKEN` + `HF_BACKUP_REPO` (optional — auto-backup to HF dataset)
   - `GUILD_ID` (optional — instant slash command sync)
4. The bot auto-deploys Cloudflare Workers on startup:
   - **Proxy Worker** — Reverse proxy for Discord API (HF free tier blocks `discord.com:443`)
   - **Keepalive Worker** — Cron that pings the Space every 5 min to prevent sleep

---

## Discord Setup

1. Create an app at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Under **Bot → Privileged Gateway Intents**, enable:
   - **Message Content Intent**
   - **Server Members Intent**
3. Generate an invite URL with scopes: `bot` + `applications.commands` and permissions: Read Messages, Send Messages, Read Message History, Attach Files
4. Invite the bot to your server
5. Set `GUILD_ID` in your `.env` for instant slash command sync (without it, global sync can take up to 1 hour)

---

## Architecture

| File | Purpose |
|---|---|
| `bot.py` | Discord listener, 23 slash commands, modals, views, health server |
| `db.py` | SQLite layer with thread-safe writes, trust system, analytics, HF backup/restore |
| `ocr.py` | Tesseract wrapper, auto-downloads ara+fra+eng language files, receipt parsing, C2PA scan |
| `config.py` | Environment config, trust constants, HF/Cloudflare settings |
| `cloudflare_proxy.py` | Deploys Cloudflare Worker reverse proxy for Discord outbound API calls |
| `setup_keepalive.py` | Deploys Cloudflare Worker cron to prevent HF Space sleep |

### Database Schema (SQLite)

- `merchants` — Discord merchant accounts, trust mode, auto-confirm settings
- `products` — Product catalog with name, price, merchant ownership
- `codes` — Product codes (FIFO assignment, marked used on delivery, never deleted)
- `buyers` — Buyer profiles with trust score, order history, flag status
- `orders` — Order records with status tracking (pending → confirming → delivered/rejected)
- `trust_events` — Append-only trust change log (with decay support)
- `audit_log` — Full audit trail of all actions
- `buyer_merchant_relations` — Per-shop trust scores and order history
- `server_config` — Guild-level merchant role mapping

---

## Commands

### Merchant Commands

| Command | Description |
|---|---|
| `/setup` | Register as a merchant |
| `/add-product` | Add a product with codes (modal) |
| `/edit-product` | Edit product name or price (dropdown → modal) |
| `/remove-product` | Delete a product and all its codes (dropdown) |
| `/add-codes` | Add more codes to an existing product (dropdown → modal) |
| `/import-codes` | Import codes from a .txt/.csv file via DM (dropdown → DM file) |
| `/export-codes` | Export unused codes as a .txt file (dropdown) |
| `/stock` | View product stock as embed |
| `/codes` | View and manage codes with pagination |
| `/restock` | Return a used code to the available pool |
| `/orders` | View recent orders (paginated) |
| `/export-orders` | Export all orders as CSV |
| `/analytics` | Detailed sales analytics (conversion rate, revenue, avg confirm time) |
| `/stats` | Quick sales stats by status |
| `/buyers` | View all buyers with trust scores |
| `/trust` | Check your buyer trust score |
| `/trust-mode` | Set trust calculation mode (local/global/weighted) |
| `/network` | See which merchants you've traded with |
| `/auto-confirm` | Set auto-deliver threshold (0 to disable) |

### Admin Commands

| Command | Description |
|---|---|
| `/admin-orders` | View all orders in the server |
| `/admin-stats` | View global stats across all merchants |
| `/set-merchant-role` | Set the role that identifies merchants in ticket channels |

### Other

| Command | Description |
|---|---|
| `/flag-buyer` | Flag or unflag a buyer (blocks auto-confirm) |

---

## Trust System

| Event | Trust Change |
|---|---|
| Order delivered | +2 |
| Order rejected | -5 (weighted by merchant rejection rate) |
| Trust decay (90 days inactive) | -10 per 90-day cycle |
| Trust floor | 0 (never goes negative) |

### Trust Modes

- **local** — Trust resets per shop (default)
- **global** — Trust follows the buyer everywhere
- **weighted** — 70% global + 30% local trust

Auto-confirm requires trust ≥ 70 and is blocked for flagged buyers, AI-flagged receipts, or duplicate transaction refs.

---

## OCR Details

- **Languages**: Arabic (`ara`), French (`fra`), English (`eng`) — auto-downloaded on first run
- **Tesseract path**: Auto-detected on Windows (`C:\Program Files\Tesseract-OCR\tesseract.exe`), overridable via `TESSERACT_CMD` env var
- **Tunisian number format**: Comma is decimal separator (`40,000` → `40.0`)
- **Amount extraction**: Returns `0` if no amount found → triggers manual fallback
- **Security scan**: Checks raw bytes for C2PA/AI markers; EXIF not used (Discord strips it)

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token |
| `CLOUDFLARE_WORKERS_TOKEN` | Yes (HF) | Cloudflare API token for proxy + keepalive workers |
| `ADMIN_ROLE_ID` | No | Discord role ID restricting admin commands |
| `GUILD_ID` | No | Server ID for instant slash command sync |
| `HF_TOKEN` | No | Hugging Face write token for auto-backup |
| `HF_BACKUP_REPO` | No | HF dataset repo for DB backup (e.g. `user/eumenes-backup`) |
| `HF_BACKUP_INTERVAL` | No | Backup interval in seconds (default: 300) |
| `HEALTH_PORT` | No | Health server port (default: 7860) |
| `TESSERACT_CMD` | No | Custom Tesseract executable path |

---

## Deployment

### Docker

```bash
docker build -t eumenes .
docker run -d --env-file .env -p 7860:7860 eumenes
```

### Hugging Face Spaces

Add `packages.txt` to your Space for system dependencies:
```
tesseract-ocr
tesseract-ocr-ara
tesseract-ocr-fra
```

The bot auto-downloads Tesseract language files to `tessdata/` on startup. Database persists in `data/eumenes.db` (ephemeral unless persistent storage is attached — use HF backup for durability).

---

## License

© Mazen. All rights reserved.

Source code is provided for portfolio and educational review only.
Commercial use, redistribution, or deployment without written permission is prohibited.
