# Bitcointalk PoW Coin -> Discord Bot

Watches **Announcements (Altcoins)** — https://bitcointalk.org/index.php?board=159.0 —
and sends a Discord message for every **new** project you can **mine (PoW) or run a node for**.
Click the message title to open the topic, for example
`https://bitcointalk.org/index.php?topic=5598068.0`.

```
New coin (2026-10-27): [ANN][SLT] Saltern - RandomX CPU PoW, nothing added to the brine
Algorithm: RandomX | By: fluxy12
```

## How it works
1. Downloads the board page. Bitcointalk's RSS feed is disabled, so the bot reads the page itself.
2. Skips pinned topics and old threads that got bumped. A topic only counts if it is new
   and its first post is from the last 24 hours.
3. Opens each new topic, reads the first post and scores it with keywords:
   - **coin** (blue): PoW, mining, pool, node, masternode, no premine, mainnet, algorithms
     like RandomX, Scrypt, SHA256, KawPow, kHeavyHash…
   - **token** (orange): ERC20/BEP20, Solana, presale, ICO, airdrop, meme… Not sent unless
     `NOTIFY_TOKENS=true`.
4. Posts the matches to your Discord webhook. Every posted coin is remembered in
   `state/seen.json` forever, so it is never posted again.

## Never posts the same coin twice
- A coin is saved as "posted" **before** the message is sent, so a crash or restart can't repeat it.
- Coins are matched by topic ID **and** by name + ticker. If a project opens a new ANN topic
  (for example "CoreByte (CB)" again), it is not announced a second time.
- If the bot starts with no memory (first start, or the memory file was wiped), it only
  remembers what is on the board and posts nothing. After that it posts only brand-new coins.
  Set `POST_ON_FIRST_RUN=true` if you want the last 24 hours posted on the first start.
- The memory file is saved safely with a backup (`seen.json.bak`). If both copies are
  damaged, the bot stops instead of re-posting old coins.
- Only one copy can run at a time (`state/bot.lock`). **Run the bot in one place only.** If it
  runs on your PC *and* on GitHub Actions, each copy has its own memory and you'll get doubles.
- On GitHub Actions the memory is kept on its own branch, `bitcointalk-bot-state`.

## 1. Create a Discord webhook
Discord channel -> **Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL**.

## 2a. Run it free on GitHub Actions (recommended, runs 24/7)
0. Turn on the schedule: on GitHub, create the file `.github/workflows/bitcointalk_bot.yml`
   (**Add file -> Create new file**) and paste in the contents of `bitcointalk_bot/github-workflow.yml`.
   The workflow has to live in `.github/workflows/` before GitHub will run it.
1. In the GitHub repo go to **Settings -> Secrets and variables -> Actions -> New repository secret**.
   Name: `DISCORD_WEBHOOK_URL`, value: your webhook URL.
2. Optional: add a repository **variable** `NOTIFY_TOKENS=true` to also get tokens.
3. Go to **Actions -> "Bitcointalk PoW coin notifier" -> Run workflow** to test it.
   After that it runs by itself every 15 minutes
   (see step 0 below).

> On a private repo the free plan gives 2,000 Action minutes/month. Checking every
> 15 minutes uses about 2,900, so change the cron to `"*/30 * * * *"` there.
> Public repos have no limit.

## 2b. Or run it on your own PC or VPS
```bash
cd bitcointalk_bot
pip install -r requirements.txt
cp .env.example .env        # paste your webhook URL into .env
python bot.py --once --dry-run   # test: prints messages, sends nothing
python bot.py                    # runs forever, checks every 15 minutes
```

## Settings (env vars or `.env`)
| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Your Discord webhook (required) |
| `CHECK_INTERVAL_MINUTES` | 15 | Loop interval when not using `--once` |
| `NOTIFY_TOKENS` | false | Also post tokens/memecoins (orange) |
| `PAGES_TO_SCAN` | 1 | Board pages per check (40 topics each) |
| `MAX_AGE_HOURS` | 24 | Ignore topics whose first post is older |
| `POST_ON_FIRST_RUN` | false | Post the last 24h of coins when there's no memory yet |

To reset the memory, delete `state/seen.json` and `state/seen.json.bak` (or, on GitHub,
the `bitcointalk-bot-state` branch). Don't delete them otherwise: they are what prevents repeats.
To tune what counts as a coin or a token, edit `COIN_PATTERNS` / `TOKEN_PATTERNS` in `bot.py`.
