#!/usr/bin/env python3
"""
Bitcointalk "Announcements (Altcoins)" -> Discord notifier.

Watches https://bitcointalk.org/index.php?board=159.0 and sends a Discord
message for every NEW topic that looks like a mineable coin (PoW / node /
masternode). Each message is a clickable embed that opens the topic, e.g.
    New coin (2026-10-27): [ANN][SLT] Saltern - RandomX CPU PoW ...

Usage:
    python bot.py --once            # check one time (use with cron / GitHub Actions)
    python bot.py                   # run forever, checking every CHECK_INTERVAL_MINUTES
    python bot.py --once --dry-run  # print what would be sent, send nothing

Settings come from environment variables (or a .env file next to this script):
    DISCORD_WEBHOOK_URL      required (unless --dry-run)
    CHECK_INTERVAL_MINUTES   default 15
    NOTIFY_TOKENS            "true" to also post tokens/memecoins (orange). default false
    PAGES_TO_SCAN            board pages to scan per check (40 topics each). default 1
    MAX_AGE_HOURS            ignore topics whose first post is older than this. default 24
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://bitcointalk.org/index.php"
BOARD_ID = 159
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state" / "seen.json"
USER_AGENT = "Mozilla/5.0 (compatible; altcoin-ann-discord-notifier/1.0)"
REQUEST_DELAY = 1.5  # seconds between requests -- be polite to bitcointalk

COLOR_COIN = 0x1F3FD8   # blue  (like the screenshot)
COLOR_TOKEN = 0xE8590C  # orange

log = logging.getLogger("btt-bot")

# --------------------------------------------------------------------------- #
# Classification keywords
# --------------------------------------------------------------------------- #
# Strong signals that the project is something you can MINE or RUN A NODE for.
COIN_PATTERNS = [
    r"\bpow\b", r"proof[\s-]*of[\s-]*work", r"\bmin(e|er|ers|ing|eable|able)\b",
    r"\bpool(s)?\b", r"\bsolo\b", r"\bpplns\b", r"\bhashrate\b", r"\bgpu\b", r"\bcpu\b",
    r"\basic\b", r"\bfair[\s-]*launch", r"\bno[\s-]*premine\b", r"\bzero[\s-]*premine\b",
    r"\bmainnet\b", r"\btestnet\b", r"\bfull[\s-]*node\b", r"\bnode(s)?\b",
    r"\bmasternode(s)?\b", r"\bblock[\s-]*reward\b", r"\bauxpow\b", r"merge[\s-]*min",
    r"\bpos/pow\b", r"\bpow/pos\b", r"\bhybrid\b",
    # common algorithms
    r"\bsha-?256d?\b", r"\bsha3", r"\bscrypt\b", r"\brandomx\b", r"\bkawpow\b",
    r"\bprogpow\b", r"\bethash\b", r"\betchash\b", r"\bequihash\b", r"\bx11\b",
    r"\bx16r(v2)?\b", r"\bx25x\b", r"\bblake3?\b", r"\bkheavyhash\b", r"\bheavyhash\b",
    r"\bcuckoo(cycle)?\b", r"\bautolykos\b", r"\byespower\b", r"\byescrypt\b",
    r"\bargon2", r"\blyra2", r"\bghostrider\b", r"\bkeccak\b", r"\bverthash\b",
    r"\bneoscrypt\b", r"\bgroestl\b", r"\bqubit\b", r"\bskein\b", r"\bfiropow\b",
    r"\bmeowpow\b", r"\bcryptonight\b", r"\bkarlsenhash\b", r"\bfishhash\b",
    r"\bxelishash\b", r"\bdynex\b", r"\bblockdag\b",
]

# Signals that it is a token / memecoin / presale (not mineable).
TOKEN_PATTERNS = [
    r"\berc-?20\b", r"\bbep-?20\b", r"\btrc-?20\b", r"\bspl\b", r"\bsolana\b",
    r"\bpump\.?fun\b", r"\bpresale\b", r"\bpre-sale\b", r"\bico\b", r"\bido\b",
    r"\bairdrop\b", r"\bmeme(coin)?\b", r"\btoken\b", r"\bdefi\b", r"\bnft\b",
    r"\bgamefi\b", r"\bdex\b", r"\bbnb\b", r"\bbsc\b", r"\bon[\s-]*(base|bsc|eth|ethereum|tron|ton)\b",
    r"\bbase l2\b", r"\bliquidity\b", r"\buniswap\b", r"\bpancakeswap\b",
    r"\braydium\b", r"\bstaking\b", r"\bcontract address\b", r"\b0x[a-f0-9]{40}\b",
]

ALGO_NAMES = [
    "SHA256", "SHA-256", "SHA3", "Scrypt", "RandomX", "KawPow", "ProgPow", "Ethash",
    "Etchash", "Equihash", "X11", "X16R", "X16RV2", "X25X", "Blake3", "Blake2",
    "kHeavyHash", "HeavyHash", "Cuckoo", "Autolykos", "Yespower", "YesCrypt", "Argon2",
    "Lyra2", "GhostRider", "Keccak", "Verthash", "NeoScrypt", "Groestl", "FiroPow",
    "MeowPow", "CryptoNight", "KarlsenHash", "FishHash", "XelisHash", "Etchash",
]

COIN_RE = [re.compile(p, re.I) for p in COIN_PATTERNS]
TOKEN_RE = [re.compile(p, re.I) for p in TOKEN_PATTERNS]


def _score(text: str, patterns: list[re.Pattern]) -> int:
    return sum(1 for p in patterns if p.search(text))


def classify(title: str, body: str) -> tuple[str, str | None]:
    """Return ("coin" | "token" | "other", detected_algorithm)."""
    t_coin = _score(title, COIN_RE)
    t_tok = _score(title, TOKEN_RE)
    b_coin = _score(body, COIN_RE)
    b_tok = _score(body, TOKEN_RE)
    coin = t_coin * 3 + b_coin   # the title counts more than the body
    tok = t_tok * 3 + b_tok

    algo = None
    # Look in the title first (most reliable), then the body.
    for text in (title, body):
        for name in ALGO_NAMES:
            if re.search(r"\b" + re.escape(name) + r"\b", text, re.I):
                algo = name
                break
        if algo:
            break

    if coin == 0 and tok == 0:
        return "other", algo
    if coin >= tok or (algo and t_tok == 0):
        return "coin", algo
    return "token", algo


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
@dataclass
class Topic:
    id: int
    title: str
    url: str
    author: str = ""
    started: datetime | None = None
    kind: str = "other"
    algo: str | None = None
    extra: dict = field(default_factory=dict)


session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def fetch(url: str) -> str:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                time.sleep(REQUEST_DELAY)
                return r.text
            log.warning("HTTP %s for %s", r.status_code, url)
        except requests.RequestException as e:
            log.warning("Request error for %s: %s", url, e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}")


def parse_board(html: str) -> list[Topic]:
    """Return the non-sticky topics listed on a board page."""
    soup = BeautifulSoup(html, "html.parser")
    topics: dict[int, Topic] = {}
    for span in soup.find_all("span", id=re.compile(r"^msg_\d+$")):
        a = span.find("a", href=re.compile(r"topic=\d+\.0$"))
        if not a:
            continue
        row = span.find_parent("tr")
        if row and row.find("img", src=re.compile(r"sticky|quick_sticky", re.I)):
            continue  # pinned rules / warnings
        tid = int(re.search(r"topic=(\d+)", a["href"]).group(1))
        author = ""
        if row:
            prof = row.find("a", title=re.compile(r"^View the profile of"))
            if prof:
                author = prof.get_text(strip=True)
        topics[tid] = Topic(
            id=tid,
            title=a.get_text(" ", strip=True),
            url=f"{BASE}?topic={tid}.0",
            author=author,
        )
    return list(topics.values())


def parse_forum_date(text: str, now: datetime) -> datetime | None:
    """Parse 'Today at 04:52:41 PM' or 'October 27, 2026, 07:44:26 PM' (forum time = UTC)."""
    text = " ".join(text.split())
    m = re.match(r"Today at (\d{1,2}:\d{2}:\d{2} [AP]M)", text, re.I)
    if m:
        t = datetime.strptime(m.group(1), "%I:%M:%S %p").time()
        return datetime.combine(now.date(), t, tzinfo=timezone.utc)
    m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2} [AP]M)", text)
    if m:
        return datetime.strptime(m.group(1), "%B %d, %Y, %I:%M:%S %p").replace(tzinfo=timezone.utc)
    return None


def enrich_topic(topic: Topic, now: datetime) -> None:
    """Open the topic, read the first post date + text, and classify it."""
    soup = BeautifulSoup(fetch(topic.url), "html.parser")
    first = soup.find("td", class_="td_headerandpost")
    body = ""
    if first:
        subj = first.find("div", class_="subject")
        date_div = subj.find_next_sibling("div", class_="smalltext") if subj else None
        if date_div:
            topic.started = parse_forum_date(date_div.get_text(" ", strip=True), now)
        post = first.find("div", class_="post")
        if post:
            body = post.get_text(" ", strip=True)[:20000]
    topic.kind, topic.algo = classify(topic.title, body)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file corrupt, starting fresh")
    return {"seen": [], "max_topic_id": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = sorted(set(state["seen"]))[-2000:]  # keep it small
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def build_embed(topic: Topic) -> dict:
    day = (topic.started or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    label = "New coin" if topic.kind == "coin" else "New token"
    title = f"{label} ({day}): {topic.title}"
    if len(title) > 256:
        title = title[:253] + "..."
    desc = []
    if topic.algo:
        desc.append(f"Algorithm: **{topic.algo}**")
    if topic.author:
        desc.append(f"By: {topic.author}")
    embed = {
        "title": title,
        "url": topic.url,
        "color": COLOR_COIN if topic.kind == "coin" else COLOR_TOKEN,
    }
    if desc:
        embed["description"] = " | ".join(desc)
    return embed


def send_discord(webhook: str, embeds: list[dict]) -> None:
    # Discord allows max 10 embeds per message.
    for i in range(0, len(embeds), 10):
        payload = {"username": "Spidey Bot", "embeds": embeds[i:i + 10]}
        for _ in range(5):
            r = requests.post(webhook, json=payload, timeout=30)
            if r.status_code == 429:  # rate limited
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            r.raise_for_status()
            break
        time.sleep(1)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def check_once(cfg: dict, dry_run: bool = False) -> int:
    now = datetime.now(timezone.utc)
    state = load_state()
    seen = set(state["seen"])
    first_run = state["max_topic_id"] == 0

    topics: list[Topic] = []
    for page in range(cfg["pages"]):
        topics += parse_board(fetch(f"{BASE}?board={BOARD_ID}.{page * 40}"))
    log.info("Scanned %d topics on board %d", len(topics), BOARD_ID)

    # New topic = never seen before AND newer than anything we've seen
    # (old threads bumped to the top of the board have low IDs, so they're skipped).
    # On the very first run we consider everything and filter by first-post date.
    threshold = 0 if first_run else state["max_topic_id"]
    candidates = [t for t in topics if t.id not in seen and t.id > threshold]
    candidates.sort(key=lambda t: t.id)

    to_send: list[Topic] = []
    failed: set[int] = set()
    cutoff = now - timedelta(hours=cfg["max_age_hours"])
    for t in candidates:
        try:
            enrich_topic(t, now)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read topic %s: %s", t.id, e)
            failed.add(t.id)  # retry on the next check
            continue
        seen.add(t.id)
        if t.started and t.started < cutoff:
            log.info("Skip (too old, %s): %s", t.started.date(), t.title)
            continue
        if t.kind == "coin" or (t.kind == "token" and cfg["notify_tokens"]):
            to_send.append(t)
            log.info("NEW %s [%s]: %s -> %s", t.kind.upper(), t.algo or "-", t.title, t.url)
        else:
            log.info("Skip (%s): %s", t.kind, t.title)

    if to_send:
        embeds = [build_embed(t) for t in to_send]
        if dry_run:
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        else:
            send_discord(cfg["webhook"], embeds)
            log.info("Sent %d notification(s) to Discord", len(embeds))
    else:
        log.info("No new PoW/node coins this time")

    if not dry_run:
        state["seen"] = list(seen | {t.id for t in topics if first_run})
        ok_ids = [t.id for t in topics if t.id not in failed]
        if failed:  # don't move the threshold past a topic we still need to read
            ok_ids = [i for i in ok_ids if i < min(failed)]
        state["max_topic_id"] = max([state["max_topic_id"]] + ok_ids)
        save_state(state)
    return len(to_send)


def load_dotenv() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitcointalk altcoin ANN -> Discord")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    cfg = {
        "webhook": os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        "interval": float(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
        "notify_tokens": os.getenv("NOTIFY_TOKENS", "false").lower() in ("1", "true", "yes"),
        "pages": max(1, int(os.getenv("PAGES_TO_SCAN", "1"))),
        "max_age_hours": float(os.getenv("MAX_AGE_HOURS", "24")),
    }
    if not cfg["webhook"] and not args.dry_run:
        sys.exit("ERROR: set DISCORD_WEBHOOK_URL (env var or .env file), or use --dry-run")

    if args.once:
        check_once(cfg, args.dry_run)
        return
    log.info("Running every %.0f minutes. Ctrl+C to stop.", cfg["interval"])
    while True:
        try:
            check_once(cfg, args.dry_run)
        except Exception as e:  # noqa: BLE001
            log.error("Check failed: %s", e)
        time.sleep(cfg["interval"] * 60)


if __name__ == "__main__":
    main()
