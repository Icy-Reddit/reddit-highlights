# reddit_highlights_bot.py
import os
import re
import textwrap
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from collections import defaultdict

import praw
import prawcore
from dotenv import load_dotenv

# ===================== Config from .env =====================
load_dotenv()

CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
USERNAME = os.getenv("REDDIT_USERNAME")
PASSWORD = os.getenv("REDDIT_PASSWORD")
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "highlights-bot/1.0 by u/yourname")

SOURCE_SUBREDDIT = os.getenv("SOURCE_SUBREDDIT", os.getenv("SUBREDDIT", "CShortDramas"))
TARGET_SUBREDDIT = os.getenv("TARGET_SUBREDDIT", SOURCE_SUBREDDIT)

SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "1500"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
TZ_NAME = os.getenv("TIMEZONE", "Europe/Warsaw")
DRAMA_REVIEW_LIMIT = int(os.getenv("DRAMA_REVIEW_LIMIT", "5"))
DISCUSSIONS_LIMIT  = int(os.getenv("DISCUSSIONS_LIMIT", "5"))
EVENT_START = (os.getenv("EVENT_START") or "").strip()
EVENT_END = (os.getenv("EVENT_END") or "").strip()
EVENT_NAME = (os.getenv("EVENT_NAME") or "").strip()
EVENT_BODY = (os.getenv("EVENT_BODY") or "").strip()


# presentation options
SHOW_THUMBNAILS = os.getenv("SHOW_THUMBNAILS", "false").lower() == "true"

# optional: flair & sticky on the target subreddit
HIGHLIGHTS_FLAIR = (os.getenv("HIGHLIGHTS_FLAIR") or "").strip()
STICKY = os.getenv("STICKY", "false").lower() == "true"
STICKY_POSITION = os.getenv("STICKY_POSITION", "bottom")
SUGGESTED_SORT: Optional[str] = (os.getenv("SUGGESTED_SORT") or "").strip() or None

# if provided – instead of creating a new post the bot will add a COMMENT under this ID
TARGET_POST_ID = (os.getenv("TARGET_POST_ID") or "").strip()

try:
    from zoneinfo import ZoneInfo
    ZONE = ZoneInfo(TZ_NAME)
except Exception:
    ZONE = timezone.utc

# ===================== Categories, limits and order =====================
# "limit": None -> no limit; number -> TOP N
# ===================== Categories, limits and order =====================
# "limit": None -> no limit; number -> TOP N
CATEGORIES: Dict[str, dict] = {
    "drama review": {
        "icon": "🎭",
        "flairs": ["📝 Drama Review", "Drama Review"],
        "label": "Drama Review",
        "limit": DRAMA_REVIEW_LIMIT,   #  5 (or 7 from .env)
    },
    "vertical vortex": {
        "icon": "🍿",
        "flairs": ["🍿 Vertical Vortex", "Vertical Vortex"],
        "label": "Vertical Vortex",
        "limit": 5,                   
    },
    "discussions": {
        "icon": "💬",
        "flairs": ["🗨️ Discussion", "Discussion", "Discussions"],
        "label": "Discussions",
        "limit": DISCUSSIONS_LIMIT,    # 5 (or 7 from .env)
    },
    "recommendations": {
        "icon": "⭐",
        "flairs": ["⭐ Recommendations", "Recommendation", "Recommendations"],
        "label": "Recommendations",
        "limit": 5
    },
    "actors&couples": {
        "icon": "🌟",
        "flairs": ["🌟Actors/Couples", "Actors/Couples", "Actors & Couples", "Actors&Couples"],
        "label": "Actors & Couples",
        "limit": 5,
    },
    "sneak peek": {
        "icon": "🔮",
        "flairs": ["🔮 Sneak Peek", "Sneak Peek"],
        "label": "Sneak Peek",
        "limit": 3,
    },
    "fun": {
        "icon": "🔥",
        "flairs": ["🔥 Fun 🔥", "Fun"],
        "label": "Fun",
        "limit": 5,
    },
    "found&shared": {
        "icon": "🔗",
        "flairs": ["Found & Shared", "Found&Shared", "Found/Shared"],
        "label": "Found & Shared",
        "limit": 5,
    },
}

CATEGORY_ORDER = [
    "drama review",
    "vertical vortex",
    "discussions",
    "recommendations",
    "sneak peek",
    "actors&couples",
    "fun",
    "found&shared",
]

# ===================== Helpers =====================
def make_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        username=USERNAME,
        password=PASSWORD,
        user_agent=USER_AGENT,
    )

def now_local():
    return datetime.now(ZONE)

def is_event_period() -> bool:
    """
    Returns True if today is between EVENT_START and EVENT_END (inclusive).
    If dates are missing or invalid, returns False.
    """
    if not EVENT_START or not EVENT_END:
        return False

    try:
        start = datetime.fromisoformat(EVENT_START).date()
        end = datetime.fromisoformat(EVENT_END).date()
        today = now_local().date()
        return start <= today <= end
    except Exception:
        # if dates are invalid, just ignore the event
        return False

def to_local(utc_ts: float):
    return datetime.fromtimestamp(utc_ts, tz=timezone.utc).astimezone(ZONE)

def iso_date_local(utc_ts: float) -> str:
    return to_local(utc_ts).date().isoformat()

def clean_one_line(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = re.sub(r"(?s)>.*?$", "", text).strip()  # drop any trailing quote block
    return text

def submission_link(subm) -> str:
    try:
        return f"https://www.reddit.com{subm.permalink}"
    except Exception:
        return f"https://redd.it/{subm.id}"

# ===================== Flair normalization =====================
def norm_flair(s: str) -> str:
    """
    Removes emoji/symbols, lowercases, and replaces non-alphanumerics with spaces.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "S")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

# ===================== Fetch posts (last 7 days) =====================
def fetch_candidates_7days_hybrid(reddit: praw.Reddit, subreddit_name: str, scan_limit: int) -> List:
    sr = reddit.subreddit(subreddit_name)
    since_utc = (datetime.now(timezone.utc) - timedelta(days=7))

    seen = set()
    picked: List = []

    try:
        for s in sr.new(limit=scan_limit):
            # pomijamy NSFW
            if getattr(s, "over_18", False):
                if DEBUG:
                    print(f"[DEBUG] Skipping NSFW post from /new: {s.id} – {clean_one_line(s.title)[:80]!r}")
                continue

            if s.id in seen:
                continue
            if datetime.fromtimestamp(s.created_utc, tz=timezone.utc) >= since_utc:
                picked.append(s); seen.add(s.id)

        for s in sr.top(time_filter="week", limit=max(1, scan_limit // 2)):
            # pomijamy NSFW
            if getattr(s, "over_18", False):
                if DEBUG:
                    print(f"[DEBUG] Skipping NSFW post from /top: {s.id} – {clean_one_line(s.title)[:80]!r}")
                continue

            if s.id in seen:
                continue
            if datetime.fromtimestamp(s.created_utc, tz=timezone.utc) >= since_utc:
                picked.append(s); seen.add(s.id)

    except prawcore.exceptions.Forbidden:
        print(f"[ERROR] 403 Forbidden – no access to r/{subreddit_name}. "
              f"Make sure the sub is public or the bot account has access (approved/mod) and that Adult content is enabled.")
        return []

    if DEBUG:
        print(f"[DEBUG] 7-day candidates: {len(picked)} (new + top(week))")
    return picked

# ===================== Group by categories =====================
def group_by_categories(candidates: List) -> Dict[str, List]:
    sections = defaultdict(list)
    dropped_score = defaultdict(int)
    dropped_nomatch = 0

    for s in candidates:
        ft_raw = (getattr(s, "link_flair_text", "") or "").strip()
        ft_norm = norm_flair(ft_raw)

        matched = False
        for key, cfg in CATEGORIES.items():
            flairs_norm = [norm_flair(f) for f in cfg["flairs"]]
            if ft_norm in flairs_norm:
                matched = True
                if s.score < MIN_SCORE:
                    dropped_score[key] += 1
                    break
                sections[key].append(s)
                break

        if not matched:
            dropped_nomatch += 1

    if DEBUG:
        print("[DEBUG] Matches per section (before limits):")
        for key in CATEGORY_ORDER:
            print(f"  • {key}: {len(sections.get(key, []))} matches; dropped (score)={dropped_score.get(key,0)}")
        print(f"[DEBUG] Unmatched flairs (after normalization): {dropped_nomatch}")

    # sort by score and trim by limits
    for key, posts in list(sections.items()):
        posts.sort(key=lambda x: x.score, reverse=True)
        limit = CATEGORIES[key].get("limit", None)
        if limit is not None:
            sections[key] = posts[:limit]
        else:
            sections[key] = posts

    return sections

# ===================== Build post body (Markdown) =====================
def build_markdown(sections: Dict[str, List]) -> (str, str):
    # --- tytuł posta ---
    base_title = "✨ Our Highlights✨"

    if is_event_period() and EVENT_NAME:
        # np. "✨ Our Highlights✨ · 📰 MOD News"
        title = f"{base_title} · {EVENT_NAME}"
    else:
        title = base_title

    # --- header postu ---
    header_lines = [
        f"Discover the most important highlights on r/{SOURCE_SUBREDDIT} from the last 7 days.",
        "",
    ]

    # blok eventu między Discover a Check...
    if is_event_period() and EVENT_NAME:
        # nazwa eventu (pogrubiona)
        header_lines.append(f"**{EVENT_NAME}**")

        # opcjonalny tekst z linkiem z .env
        if EVENT_BODY:
            header_lines.extend([
                "",
                EVENT_BODY,
            ])

        # pusta linia po bloku eventu
        header_lines.append("")

    # dalej standardowy tekst
    header_lines.extend([
        "Check the [Content Wiki](https://www.reddit.com/r/CShortDramas/wiki/test3/) for even more of our creative work!",
        "",
        "---",
        "",
    ])

    # reszta funkcji bez zmian:
    body_parts = []
    for key in CATEGORY_ORDER:
        cfg = CATEGORIES[key]
        icon = cfg["icon"]
        label = cfg["label"]

        body_parts.append(f"## {icon} {label}")

        posts = sections.get(key, [])
        if not posts:
            body_parts.append("_No items this week._")
            body_parts.append("")
            continue

        lines = []
        for i, s in enumerate(posts, 1):
            link = submission_link(s)
            author = f"u/{s.author.name}" if s.author else "[deleted]"
            title_text = clean_one_line(s.title)

            # (optional) thumbnail
            thumb = getattr(s, "thumbnail", "") or ""
            thumb_md = f"![thumbnail]({thumb}) " if (SHOW_THUMBNAILS and thumb.startswith('http')) else ""

            # only: title (link) + author
            lines.append(f"{i}. {thumb_md}[{title_text}]({link}) — {author}")

        body_parts.append("\n".join(lines))
        body_parts.append("")  # soft spacing between sections

    footer = "*Auto-generated weekly roundup.*"
    content = "\n".join(header_lines) + "\n".join(body_parts) + "\n" + footer + "\n"
    return title, content

# ===================== Flair + Sticky (optional) =====================
def find_flair_template_id(sr, flair_text_target: str):
    if not flair_text_target:
        return None
    try:
        for f in sr.flair.link_templates:
            if (f.get("text") or "").strip() == flair_text_target.strip():
                return f.get("id")
    except Exception:
        pass
    return None

def apply_post_flair(submission, flair_text_target: str) -> bool:
    if not flair_text_target:
        return False
    tid = find_flair_template_id(submission.subreddit, flair_text_target)
    if not tid:
        print(f"[WARN] Flair '{flair_text_target}' not found – skipping.")
        return False
    try:
        submission.flair.select(tid)
        print(f"[OK] Flair set: {flair_text_target}")
        return True
    except Exception as e:
        print(f"[ERR] Failed to set flair: {e}")
        return False

def is_our_highlight(subm) -> bool:
    """Uznajemy post za 'nasz', jeśli autorem jest bot albo tytuł zawiera 'Our Highlights'."""
    try:
        author_ok = (subm.author and subm.author.name.lower() == (USERNAME or "").lower())
    except Exception:
        author_ok = False
    title_ok = "our highlights" in (getattr(subm, "title", "") or "").lower()
    return author_ok or title_ok

def unsticky_previous_in_slot(sr, position: str = "bottom"):
    """
    Zdejmuje poprzedni sticky w danym slocie (top/bottom), jeśli to nasz highlight.
    """
    number = 2 if str(position).lower().strip() == "bottom" else 1
    try:
        prev = sr.sticky(number=number)  # pobierz istniejącego stickiego z tego slotu
        if prev and is_our_highlight(prev):
            prev.mod.sticky(state=False)
            print(f"[OK] Unstickied previous '{'bottom' if number==2 else 'top'}' highlight: https://redd.it/{prev.id}")
        else:
            print(f"[INFO] Slot {'bottom' if number==2 else 'top'} occupied by a different post — leaving as is.")
    except Exception as e:
        # brak stickiego w slocie lub brak uprawnień też trafi tu
        print(f"[INFO] No previous sticky to unstick in this slot or cannot read it: {e}")

def maybe_sticky_submission(submission, position: str = "top", suggested_sort: str | None = None):
    try:
        bottom = (str(position).lower().strip() == "bottom")
        sr = submission.subreddit

        # 1) zdejmij poprzedni sticky w tym samym slocie, jeśli to nasz highlight
        unsticky_previous_in_slot(sr, position="bottom" if bottom else "top")

        # 2) przypnij nowy
        submission.mod.sticky(state=True, bottom=bottom)
        print(f"[OK] Post stickied at '{'bottom' if bottom else 'top'}' slot.")

        # 3) suggested sort (opcjonalnie)
        if suggested_sort:
            submission.mod.suggested_sort(suggested_sort)
            print(f"[OK] Suggested sort set: {suggested_sort}")

    except Exception as e:
        print(f"[ERR] Could not sticky or set sort: {e}")

# ===================== Main =====================
def main():
    reddit = make_reddit()
    
    print(f"[INFO] Sticky={STICKY}  StickyPosition={STICKY_POSITION}  SuggestedSort={SUGGESTED_SORT}")
    
    print(f"[INFO] SOURCE={SOURCE_SUBREDDIT}  TARGET={TARGET_SUBREDDIT}  DRY_RUN={DRY_RUN}")

    # 1) Collect 7-day candidates (from source sub)
    candidates = fetch_candidates_7days_hybrid(reddit, SOURCE_SUBREDDIT, SCAN_LIMIT)
    if not candidates:
        print("[STOP] No posts from the last 7 days or no access.")
        return

    # 2) Group by sections
    sections = group_by_categories(candidates)

    # 3) Build post content
    title, body = build_markdown(sections)

    # 4) Preview or publish (to target sub)
    if DRY_RUN:
        print("=== DRY RUN (not publishing) ===")
        print(title)
        print(body)
        return

    try:
        sr = reddit.subreddit(TARGET_SUBREDDIT)

        if TARGET_POST_ID:
            print(f"[INFO] Adding a comment under an existing post: {TARGET_POST_ID}")
            target = reddit.submission(id=TARGET_POST_ID)
            target.reply(body)
            print(f"[OK] Comment added under: https://redd.it/{TARGET_POST_ID}")
            return

        print(f"[INFO] Submitting a new post to r/{TARGET_SUBREDDIT} …")
        created = sr.submit(title=title, selftext=body)
        print(f"[OK] Posted: https://redd.it/{created.id}")

        if HIGHLIGHTS_FLAIR:
            apply_post_flair(created, HIGHLIGHTS_FLAIR)

        if STICKY:
            maybe_sticky_submission(created, position=STICKY_POSITION, suggested_sort=SUGGESTED_SORT)

    except Exception as e:
        import traceback
        print("[ERROR] Publishing failed:")
        print(" ", e)
        traceback.print_exc()

if __name__ == "__main__":
    print("[BOOT] start reddit_highlights_bot")
    main()
    print("[BOOT] done")
