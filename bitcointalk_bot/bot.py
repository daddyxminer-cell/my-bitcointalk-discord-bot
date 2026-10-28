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

No-repeat guarantee:
    * Every coin is written to state/seen.json BEFORE it is posted, so a crash,
      timeout or restart can never make it post the same coin again.
    * The state file is written atomically with a backup; a damaged file stops
      the bot instead of silently "starting fresh" and re-posting old coins.
    * Only one copy of the bot can run at a time (state/bot.lock).
    * Besides the topic ID, coins are also matched by name + ticker, so a project
      that re-posts its ANN in a new topic is not announced twice.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
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
BACKUP_FILE = HERE / "state" / "seen.json.bak"
LOCK_FILE = HERE / "state" / "bot.lock"
STATE_VERSION = 2
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
class StateError(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "seen": [],            # topic IDs already looked at
        "max_topic_id": 0,     # highest topic ID handled (older bumped threads are ignored)
        "notified": {},        # topic ID -> {title, at} for every coin ever posted (never trimmed)
        "notified_keys": [],   # name/ticker keys of every coin ever posted (never trimmed)
    }


def _read_state_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "seen" not in data:
        raise ValueError("not a bot state file")
    return data


def load_state() -> dict:
    """Load state, falling back to the backup. Never silently start fresh on damage."""
    damaged = []
    for path in (STATE_FILE, BACKUP_FILE):
        if not path.exists():
            continue
        try:
            data = _read_state_file(path)
        except (ValueError, OSError, UnicodeDecodeError) as e:
            log.warning("Could not read %s: %s", path.name, e)
            damaged.append(path.name)
            continue
        if path == BACKUP_FILE:
            log.warning("Main state file was damaged -- using backup %s", path.name)
        state = _empty_state()
        state.update(data)  # older files simply lack the new keys
        state["notified"] = {str(k): v for k, v in (state.get("notified") or {}).items()}
        return state
    if damaged:
        raise StateError(
            f"State file(s) {', '.join(damaged)} are damaged. Refusing to start fresh because "
            "that would re-post old coins. Delete the files in bitcointalk_bot/state/ to reset."
        )
    return _empty_state()


def save_state(state: dict) -> None:
    """Atomic write (temp file + rename) and keep the previous good version as a backup."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    state["seen"] = sorted({int(i) for i in state["seen"]})[-5000:]
    state["notified_keys"] = sorted(set(state["notified_keys"]))
    data = json.dumps(state, indent=2, ensure_ascii=False)

    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if STATE_FILE.exists():
        try:
            _read_state_file(STATE_FILE)
            shutil.copy2(STATE_FILE, BACKUP_FILE)
        except (ValueError, OSError, UnicodeDecodeError):
            pass  # don't overwrite a good backup with a damaged file
    os.replace(tmp, STATE_FILE)


def merge_state_files(other_path: Path) -> None:
    """Union another copy of the state into ours (used by GitHub Actions to avoid losing memory)."""
    if not other_path.exists():
        return
    try:
        other = _read_state_file(other_path)
    except (ValueError, OSError, UnicodeDecodeError) as e:
        log.warning("Ignoring unreadable state copy %s: %s", other_path, e)
        return
    try:
        state = load_state()
    except StateError:
        state = _empty_state()
    state["seen"] = list(set(state["seen"]) | set(other.get("seen", [])))
    state["max_topic_id"] = max(state["max_topic_id"], int(other.get("max_topic_id", 0)))
    notified = {str(k): v for k, v in (other.get("notified") or {}).items()}
    notified.update(state["notified"])
    state["notified"] = notified
    state["notified_keys"] = list(set(state["notified_keys"]) | set(other.get("notified_keys", [])))
    save_state(state)
    log.info("Merged state from %s", other_path)


class SingleInstance:
    """Prevents two copies of the bot (each posting the same coins) from running at once."""

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOCK_FILE, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise AlreadyRunning(
                "Another copy of the bot is already running (state/bot.lock). "
                "Only run one copy, otherwise every coin gets posted once per copy."
            ) from None
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


# --------------------------------------------------------------------------- #
# Coin identity (to catch the same coin re-posted in a new topic)
# --------------------------------------------------------------------------- #
NOT_TICKERS = {
    "ANN", "PREANN", "PRE-ANN", "TESTNET", "MAINNET", "POW", "POS", "NEW", "LIVE", "BETA",
    "ALPHA", "V2", "V3", "CPU", "GPU", "ASIC", "AUXPOW", "SCRYPT", "SHA256", "RANDOMX",
    "ICO", "IDO", "NFT", "DEFI", "AI", "L1", "L2", "UPDATE", "RELAUNCH", "OFFICIAL",
}


def coin_keys(title: str) -> set[str]:
    """Keys identifying a coin: its normalized title, plus name|ticker when both are found."""
    keys = set()
    norm = re.sub(r"\[(pre-?)?ann\]", " ", title, flags=re.I)
    norm = re.sub(r"[^a-z0-9]+", " ", norm.lower()).strip()
    if norm:
        keys.add("t:" + norm)

    ticker, name = None, None
    for m in re.finditer(r"[\[(]\s*\$?([A-Za-z0-9]{2,8})\s*[\])]", title):
        cand = m.group(1).upper()
        if cand not in NOT_TICKERS and not cand.isdigit():
            ticker = cand
            if m.group(0).startswith("("):  # "CoreByte (CB)" -> name is the word before it
                before = re.findall(r"[A-Za-z][A-Za-z0-9]+", title[:m.start()])
                if before:
                    name = before[-1]
            break
    if ticker and not name:  # "[ANN][SLT] Saltern - ..." -> first word outside brackets
        rest = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title)
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", rest) if w.upper() not in NOT_TICKERS]
        if words:
            name = words[0]
    if ticker and name:
        keys.add(f"c:{name.lower()}|{ticker.lower()}")
    return keys


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
def already_notified(state: dict, topic: Topic) -> bool:
    if str(topic.id) in state["notified"]:
        return True
    return bool(coin_keys(topic.title) & set(state["notified_keys"]))


def mark_notified(state: dict, topic: Topic, now: datetime) -> None:
    state["notified"][str(topic.id)] = {"title": topic.title, "at": now.isoformat(timespec="seconds")}
    state["notified_keys"] = sorted(set(state["notified_keys"]) | coin_keys(topic.title))
    state["seen"] = list(set(state["seen"]) | {topic.id})


def check_once(cfg: dict, dry_run: bool = False) -> int:
    now = datetime.now(timezone.utc)
    state = load_state()
    seen = set(state["seen"])
    fresh_state = state["max_topic_id"] == 0 and not state["notified"]

    topics: list[Topic] = []
    for page in range(cfg["pages"]):
        topics += parse_board(fetch(f"{BASE}?board={BOARD_ID}.{page * 40}"))
    log.info("Scanned %d topics on board %d", len(topics), BOARD_ID)
    if not topics:
        log.warning("Board page had no topics (site down / layout changed?) -- nothing done")
        return 0

    # No memory yet (first start, or the state file was wiped by a host restart).
    # Default: just remember everything currently on the board and post nothing,
    # because we can't know what was already posted to Discord before.
    if fresh_state and not cfg["post_on_first_run"]:
        log.info("First run / no saved state: remembering %d current topics, posting nothing. "
                 "From now on only brand-new coins are posted.", len(topics))
        if not dry_run:
            state["seen"] = [t.id for t in topics]
            state["max_topic_id"] = max(t.id for t in topics)
            save_state(state)
        return 0

    # New topic = never seen before AND newer than anything we've handled
    # (old threads bumped to the top of the board have low IDs, so they're skipped).
    threshold = 0 if fresh_state else state["max_topic_id"]
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
        if already_notified(state, t):
            log.info("Skip (already posted before): %s", t.title)
            continue
        if t.started and t.started < cutoff:
            log.info("Skip (too old, %s): %s", t.started.date(), t.title)
            continue
        if t.kind == "coin" or (t.kind == "token" and cfg["notify_tokens"]):
            to_send.append(t)
            log.info("NEW %s [%s]: %s -> %s", t.kind.upper(), t.algo or "-", t.title, t.url)
        else:
            log.info("Skip (%s): %s", t.kind, t.title)

    # Remember everything we looked at (so it's never re-checked).
    state["seen"] = list(set(state["seen"]) | seen)
    ok_ids = [t.id for t in topics if t.id not in failed]
    if failed:  # don't move the threshold past a topic we still need to read
        ok_ids = [i for i in ok_ids if i < min(failed)]
    state["max_topic_id"] = max([state["max_topic_id"]] + ok_ids)

    if dry_run:
        if to_send:
            print(json.dumps([build_embed(t) for t in to_send], indent=2, ensure_ascii=False))
        else:
            log.info("No new PoW/node coins this time")
        return len(to_send)

    if not to_send:
        save_state(state)
        log.info("No new PoW/node coins this time")
        return 0

    # WRITE-AHEAD: save "posted" BEFORE sending. If the bot is killed or crashes
    # mid-send, the worst case is one missed message -- never a repeated one.
    for t in to_send:
        mark_notified(state, t, now)
    save_state(state)
    send_discord(cfg["webhook"], [build_embed(t) for t in to_send])
    log.info("Sent %d notification(s) to Discord", len(to_send))
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
    parser.add_argument("--merge-state", metavar="FILE", help="merge another seen.json into ours and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.merge_state:
        merge_state_files(Path(args.merge_state))
        return
    load_dotenv()
    cfg = {
        "webhook": os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        "interval": float(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
        "notify_tokens": os.getenv("NOTIFY_TOKENS", "false").lower() in ("1", "true", "yes"),
        "pages": max(1, int(os.getenv("PAGES_TO_SCAN", "1"))),
        "max_age_hours": float(os.getenv("MAX_AGE_HOURS", "24")),
        "post_on_first_run": os.getenv("POST_ON_FIRST_RUN", "false").lower() in ("1", "true", "yes"),
    }
    if not cfg["webhook"] and not args.dry_run:
        sys.exit("ERROR: set DISCORD_WEBHOOK_URL (env var or .env file), or use --dry-run")

    try:
        with SingleInstance():
            if args.once:
                check_once(cfg, args.dry_run)
                return
            log.info("Running every %.0f minutes. Ctrl+C to stop.", cfg["interval"])
            while True:
                try:
                    check_once(cfg, args.dry_run)
                except StateError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.error("Check failed: %s", e)
                time.sleep(cfg["interval"] * 60)
    except (AlreadyRunning, StateError) as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
