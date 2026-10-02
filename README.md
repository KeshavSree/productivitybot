# Charles

A Discord bot that turns whatever you type into sorted tasks on a Notion board.

Type `plan for stack marketing meeting` and it lands in the **Stack** column as
"Plan for stack marketing meeting". Paste a whole brain-dump, one task per line,
and every line gets sorted. No LLM: categories come from rules plus a small
classifier that learns from your corrections.

## How it sorts

1. **Tags win.** `stack: send update`, `#mlp write results`, `[cs 211] lab 4`, or
   `finish homework #stat417` force a category and the tag is removed from the task.
2. **Rules.** Course codes in any form (`cs 373`, `CS373`, `cs-37300`, a bare `373`),
   category names (`stack`, `mlp`), and recruiting words (interview, leetcode,
   resume, internship, apply, coffee chat, referral, OA…).
3. **Classifier.** A TF-IDF + logistic regression model (scikit-learn) trained on
   seed examples and every correction you make. It's only trusted when confident.
4. **Inbox.** Anything still unclear goes to an **Inbox** column, and the bot shows a
   menu asking where it belongs. Your answer moves it in Notion and trains the model.

You can also teach it permanent words with `/keyword` (e.g. `regression` → STAT 417).
Categories, colors and rules live in `charles/categories.py`.

## In Discord

| What | How |
| --- | --- |
| Add tasks | Start a message with `c` or `charles` in any channel (`c do cs 373 hw`), type in your tasks channel, DM Charles, or @mention it. Or `/add` (opens a box for several lines). |
| Add a bunch | `c multi`, then every message you send in that channel is a task until `c end`. |
| Fix a category | `/list` → "Move a task to another category". Inbox tasks get a menu right in the reply. |
| See / finish tasks | `/list` (optionally one category), then "Mark done". |
| Teach a word | `/keyword category word` |
| See the rules | `/categories` |

## Setup

### 1. Discord bot
In the [Developer Portal](https://discord.com/developers/applications) → your app → **Bot**:
- Turn on **Message Content Intent** (needed to read your messages).
- **Reset Token** and keep it for `DISCORD_TOKEN`.

If the bot isn't in your server yet: **OAuth2 → URL Generator**, scopes `bot` and
`applications.commands`, permissions *Send Messages*, *Read Message History*,
*Embed Links*, then open the URL.

Optional, in Discord with Developer Mode on (Settings → Advanced): right-click your
tasks channel → *Copy Channel ID* for `TASK_CHANNEL_ID`, your server icon → *Copy
Server ID* for `GUILD_ID`, and your own name → *Copy User ID* for `ALLOWED_USER_IDS`.

### 2. Notion board
1. At [notion.so/my-integrations](https://www.notion.so/my-integrations), copy your
   integration's secret for `NOTION_TOKEN`.
2. Make an empty Notion page, then **⋯ → Connections → add your integration**.
3. Run, with the page's link:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env        # put NOTION_TOKEN in it
   python -m scripts.setup_notion "https://www.notion.so/Your-Page-abc123..."
   ```
   It prints `NOTION_DATABASE_ID=...`. Put that in `.env`.
4. In Notion, on the new **Tasks** table: **⋯ → Layout → Board**, group by
   **Category**. Each category is now a colored column. To hide finished tasks,
   add a filter **Done is unchecked**. (Notion's API can't set the layout, so this
   one click is manual.)

### 3. Run locally
```bash
python -m charles
```

## Keep it online 24/7 (Railway)

1. Sign in at [railway.com](https://railway.com) with GitHub → **New Project →
   Deploy from GitHub repo** → `productivitybot`.
2. **Variables**: add `DISCORD_TOKEN`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`, and any
   optional ones from `.env.example`. Set `DATA_DIR=/data`.
3. **Add a Volume** to the service, mount path `/data` (so learned keywords and
   corrections survive redeploys).
4. Deploy. `railway.json` starts `python -m charles` and restarts it if it crashes.
   It runs as a worker, so it doesn't need a public domain. Cost is about $5/month on
   the Hobby plan.

Alternatives: a free-tier Oracle Cloud VM or any always-on box with
`python -m charles` under systemd; Fly.io also works with a volume.

## Development
```bash
pip install -r requirements-dev.txt
pytest
```
Tests cover the parser, classifier, a fake Notion API, and the Discord components.

## Limits of this version
- Ticking **Done** in Notion doesn't sync back to Discord's `/list` (Discord → Notion only).
- Only the first 25 tasks show in a menu (Discord's limit); `/list category` narrows it.
