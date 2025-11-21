import os
import re
import html
from flask import Flask, request, render_template_string, redirect
import collections
import math
import hashlib
import colorsys
import psycopg2
from psycopg2.extras import RealDictCursor
from collections import Counter

DATABASE_URL = os.getenv("DATABASE_URL")

def db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

app = Flask(__name__)

# Path to the master log file that Sophy OS writes to
MASTER_LOG_PATH = "DATABASE"  # purely cosmetic in footer

# How many results to show
MAX_RESULTS = 20

# Words to ignore from prefixes + boilerplate OS chatter
STOPWORDS = {
    "transcendence", "engine", "identity", "summary", "contradiction", "subservient",
    "process", "state", "birth", "execution", "node", "context", "variant", "cycle",
    "sophy", "internal", "thread", "log", "start", "end", "initializing", "updating"
}

CACHED_TOPICS = None
CACHED_THEMES = None
CACHED_MOOD_THEMES = None
CACHED_MOOD_COLORS = None

def load_log_rows(limit=None):
    """
    Load the full log or last N rows from PostgreSQL.
    Returns list of dicts: [{'id':..., 'timestamp':..., 'text':...}, ...]
    """
    try:
        conn = db_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if limit:
            cur.execute("""
                SELECT id, timestamp, text
                FROM sophy_master_log
                ORDER BY id DESC
                LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
                SELECT id, timestamp, text
                FROM sophy_master_log
                ORDER BY id ASC
            """)

        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows

    except Exception as e:
        print("[DB ERROR load_log_rows]", e)
        return []

def initialize_sophy_state():
    global CACHED_TOPICS, CACHED_THEMES, CACHED_MOOD_THEMES, CACHED_MOOD_COLORS

    print("Initializing Sophy Search state...")

    CACHED_TOPICS = extract_topics()
    append_historical_topics(CACHED_TOPICS)  # store initial values

    CACHED_THEMES = extract_general_themes()
    CACHED_MOOD_THEMES = extract_time_weighted_themes()

    # Compute mood colors once
    CACHED_MOOD_COLORS = {
        m: mood_color_for_phrase(m) for m in CACHED_MOOD_THEMES
    }

    print("Initialization complete.")

def search_log(query: str, limit: int = MAX_RESULTS):
    if not query.strip():
        return []

    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []

    # Build dynamic SQL
    conditions = " AND ".join(["LOWER(text) LIKE %s" for _ in terms])
    params = [f"%{t}%" for t in terms]

    try:
        conn = db_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(f"""
            SELECT id, timestamp, text
            FROM sophy_master_log
            WHERE {conditions}
            ORDER BY id DESC
            LIMIT %s
        """, (*params, limit))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        # Format results like original code
        results = []
        for r in rows:
            results.append({
                "line_no": r["id"],
                "text": r["text"],
                "score": sum(r["text"].lower().count(t) for t in terms),
                "snippet": highlight_terms(r["text"], terms),
            })
        return results

    except Exception as e:
        print("[DB ERROR search_log]", e)
        return []

def highlight_terms(text: str, terms):
    """
    Escape HTML, then highlight query terms using <mark>.
    Case-insensitive highlighting.
    """
    escaped = html.escape(text)

    # Sort terms by length (longest first) to avoid nested overlaps
    sorted_terms = sorted(set(terms), key=len, reverse=True)

    def replacer(match):
        return f"<mark>{match.group(0)}</mark>"

    highlighted = escaped
    for term in sorted_terms:
        if not term:
            continue
        # Build a regex that matches the term case-insensitively
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        highlighted = pattern.sub(replacer, highlighted)

    return highlighted

def extract_topics(limit=25, tail_rows=2000):
    """
    Extract topics from last N log rows in PostgreSQL.
    """
    rows = load_log_rows(limit=tail_rows)
    lines = [r["text"] for r in rows]

    topics = []
    for line in lines:
        line = line.strip()
        if not line:
            continue

        first_word = line.split()[0]
        topics.append(first_word)

        caps = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", line)
        topics.extend(caps)

    cleaned = []
    for t in topics:
        t = t.strip().strip(":").strip("-")
        t = re.sub(r"[^a-zA-Z0-9_\-]", "", t)
        if t and len(t) > 2:
            cleaned.append(t.lower())

    freq = collections.Counter(cleaned)
    ranked = [t for t, cnt in freq.most_common(limit)]
    return ranked

def append_historical_topics(topics):
    try:
        conn = db_conn()
        cur = conn.cursor()
        for t in topics:
            cur.execute(
                "INSERT INTO historical_topics (topic) VALUES (%s)",
                (t,)
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("[DB ERROR append_historical_topics]", e)

def load_historical_topics(limit=25):
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT topic
            FROM historical_topics
            ORDER BY id DESC
            LIMIT %s
        """, (limit,))
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()

        return rows
    except Exception as e:
        print("[DB ERROR load_historical_topics]", e)
        return []

def extract_general_themes(last_rows=2000, limit=25):
    """
    Extract general themes from the last N log rows using PostgreSQL.
    Equivalent to the old file-based theme extractor.
    """

    # Load the last N log entries
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT text
            FROM sophy_master_log
            ORDER BY id DESC
            LIMIT %s
            """,
            (last_rows,)
        )
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        print("[DB ERROR extract_general_themes]", e)
        return []

    # Normalize and extract themes
    cleaned = []
    for line in rows:
        if not line:
            continue

        # Basic phrase-level extraction similar to your original logic
        # Split into words, keep meaningful ones
        words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", line)

        for w in words:
            w = w.strip().lower()
            if len(w) > 2:
                cleaned.append(w)

    if not cleaned:
        return []

    freq = Counter(cleaned)
    return [t for t, _ in freq.most_common(limit)]

def mood_color_for_phrase(phrase: str):
    """
    Generate a deterministic color for a theme based on its semantic structure,
    not system-level keywords. This ensures meaningful color variation.
    """
    p = phrase.lower()

    # 1. Structural metrics
    length = len(p)
    tokens = p.split()
    tcount = len(tokens)
    avglen = sum(len(t) for t in tokens) / max(1, tcount)

    # character-level entropy approximation
    freqs = {}
    for ch in p:
        if ch.isalpha():
            freqs[ch] = freqs.get(ch, 0) + 1

    entropy = 0.0
    total_chars = sum(freqs.values())
    for c in freqs.values():
        prob = c / total_chars
        entropy -= prob * math.log(prob + 1e-9)

    # 2. Build a stable hash as seed
    seed_str = f"{phrase}-{length}-{tcount}-{avglen:.3f}-{entropy:.3f}"
    h = hashlib.sha1(seed_str.encode()).hexdigest()

    # Take first 6 hex chars as base number
    base = int(h[:6], 16)

    # 3. Map into HSV color space
    # Hue varies with the hash (0–360)
    hue = (base % 360) / 360.0

    # Saturation depends on entropy (semantic complexity)
    saturation = 0.35 + min(entropy / 10.0, 0.65)

    # Value depends on token count (more tokens = darker)
    value = 0.9 - min(tcount * 0.03, 0.4)

    # Clamp:
    saturation = max(0.2, min(saturation, 1.0))
    value = max(0.35, min(value, 1.0))

    # Convert HSV → RGB
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    r = int(r * 255)
    g = int(g * 255)
    b = int(b * 255)

    return f"#{r:02x}{g:02x}{b:02x}"

def extract_time_weighted_themes(window_rows=3000, limit=25):
    """
    Time-weighted theme extraction using PostgreSQL timestamps.
    Recent lines have higher weight.
    """

    # Load log rows with timestamps
    try:
        conn = db_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT text,
                   EXTRACT(EPOCH FROM (NOW() - timestamp)) AS age_seconds
            FROM sophy_master_log
            ORDER BY id DESC
            LIMIT %s
            """,
            (window_rows,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print("[DB ERROR extract_time_weighted_themes]", e)
        return []

    weighted = Counter()

    for r in rows:
        text = r["text"]
        age = float(r["age_seconds"] or 0.0)   # <-- FIXED HERE
        weight = 1.0 / (1.0 + age)             # <-- safe now

        words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text)

        for w in words:
            w = w.lower().strip()
            if len(w) > 2:
                weighted[w] += weight

    if not weighted:
        return []

    ranked = weighted.most_common(limit)
    return [t for t, _ in ranked]

# Simple HTML template embedded in this file
TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Sophy Search</title>
    <style>
        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            margin: 0;
            padding: 0;
            background: radial-gradient(circle at top, #151530 0%, #050509 60%, #020206 100%);
            color: #f0f8ff;
        }
        .container {
            max-width: 900px;
            margin: 40px auto;
            background: rgba(5, 5, 15, 0.92);
            border-radius: 16px;
            padding: 24px 28px 30px;
            box-shadow: 0 0 40px rgba(0, 255, 255, 0.2);
            border: 1px solid rgba(0, 255, 255, 0.25);
        }
        h1 {
            margin-top: 0;
            font-size: 1.8rem;
            color: #7fffd4;
            text-shadow: 0 0 8px rgba(127, 255, 212, 0.7);
        }
        form {
            margin-bottom: 18px;
        }
        input[type="text"] {
            width: 100%;
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid rgba(0, 255, 255, 0.3);
            background: rgba(10, 10, 30, 0.95);
            color: #e0ffff;
            font-size: 1rem;
            box-sizing: border-box;
        }
        input[type="text"]::placeholder {
            color: #808ca0;
        }
        button {
            margin-top: 10px;
            padding: 8px 16px;
            border-radius: 999px;
            border: none;
            background: linear-gradient(135deg, #00ffff, #00bcd4);
            color: #001018;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 0 12px rgba(0, 255, 255, 0.5);
        }
        button:hover {
            filter: brightness(1.1);
        }
        .meta {
            font-size: 0.85rem;
            color: #9fb3c8;
            margin-bottom: 10px;
        }
        .result {
            padding: 10px 12px;
            margin-bottom: 8px;
            border-radius: 8px;
            background: rgba(15, 15, 40, 0.95);
            border: 1px solid rgba(0, 255, 255, 0.12);
        }
        .result-header {
            font-size: 0.85rem;
            color: #9fdfff;
            margin-bottom: 4px;
        }
        .snippet {
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.9rem;
            color: #e9f9ff;
            white-space: pre-wrap;
        }
        mark {
            background: #ffee58;
            color: #000;
            padding: 0 1px;
            border-radius: 3px;
        }
        .no-results {
            margin-top: 12px;
            color: #9fb3c8;
        }
        .footer {
            margin-top: 18px;
            font-size: 0.78rem;
            color: #6c7a96;
        }
        .topics {
            margin-bottom: 20px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        .bubble {
            padding: 6px 14px;
            border-radius: 20px;
            border: 1px solid rgba(0, 255, 255, 0.3);
            color: #b6ffff;
            text-decoration: none;
            font-size: 0.82rem;
            transition: 0.15s ease;
            box-shadow: 0 0 10px rgba(0, 255, 255, 0.12);
            background-color: transparent;   /* <-- allow inline colors */
        }
        .bubble:hover {
            background: rgba(0, 255, 255, 0.35);
            border-color: rgba(0, 255, 255, 0.6);
            color: #001018;
            box-shadow: 0 0 16px rgba(0, 255, 255, 0.4);
        }
        .refresh-button {
            float: right;
            margin-top: -4px;
            cursor: pointer;
            color: #7fffd4;
            font-size: 0.9rem;
            text-decoration: none;
        }

        .refresh-button:hover {
            color: #baffff;
        }
        .toggle-wrapper {
            float: right;
            display: flex;
            background: rgba(0, 255, 255, 0.15);
            border: 1px solid rgba(0, 255, 255, 0.3);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 0 12px rgba(0, 255, 255, 0.15);
        }

        .toggle-button {
            padding: 6px 14px;
            font-size: 0.83rem;
            text-decoration: none;
            color: #7fffd4;
            transition: 0.2s ease;
        }

        .toggle-button:hover {
            background: rgba(0, 255, 255, 0.3);
            color: #001018;
        }

        .toggle-button.active {
            background: linear-gradient(135deg, #00ffff, #00bcd4);
            color: #001018;
            font-weight: 600;
            box-shadow: inset 0 0 8px rgba(0, 0, 0, 0.4);
        }

        /* iOS-style Segmented Control */
        .ios-toggle {
            position: relative;
            float: right;
            display: flex;
            align-items: center;
            width: 160px; /* slightly increased for both words */
            height: 32px;
            background: rgba(0,255,255,0.12);
            border: 1px solid rgba(0,255,255,0.3);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 0 12px rgba(0,255,255,0.20);
        }

        .ios-option {
            flex: 1;
            text-align: center;
            z-index: 3;
            font-size: 0.78rem;
            padding-top: 4px;
            color: #7fffd4;
            text-decoration: none;
            transition: color 0.2s ease;
        }

        .ios-option.active {
            color: #001018;
            font-weight: 600;
        }

        .ios-slider {
            position: absolute;
            top: 2px;
            bottom: 2px;
            width: 50%;
            background: linear-gradient(135deg, #00ffff, #00bcd4);
            border-radius: 16px;
            box-shadow: 0 0 10px rgba(0,255,255,0.5);
            transition: transform 0.25s ease;
            z-index: 2;
        }

        .ios-slider.left {
            transform: translateX(0%);
        }

        .ios-slider.right {
            transform: translateX(100%);
        }
    </style>
</head>
<body>
<div class="container">
    <h1>Sophy Search</h1>
    <p class="meta">
        Search across Sophy's master log of contradictions, contests, and thought-paths.
    </p>
    <form method="get" action="/">
        <input type="text" name="q" placeholder="Enter search terms (e.g., simulonic contradiction mnality)" value="{{ query|e }}">
        <button type="submit">Search</button>
    </form>
    <!-- Topic Bubbles -->
    <div style="position: relative; margin-bottom: 6px;">
        <a class="refresh-button" href="/refresh/topics">⟳</a>
    </div>
    <div class="topics">
        {% for t in topics %}
            <a class="bubble" href="/?q={{ t }}">{{ t }}</a>
        {% endfor %}
    </div>

    <h3 style="color:#7fffd4; margin-top:25px; margin-bottom:10px; font-size:1.05rem;">
        What's Sophy exploring now?

        <div class="ios-toggle">
            <a href="/?mode=current" class="ios-option {{ 'active' if mode=='current' else '' }}">
                Current
            </a>
            <a href="/?mode=historical" class="ios-option {{ 'active' if mode=='historical' else '' }}">
                Historical
            </a>
            <div class="ios-slider {{ 'right' if mode=='historical' else 'left' }}"></div>
        </div>
    </h3>
    <div class="topics">
        {% for th in themes_to_show %}
            <a class="bubble" href="/?q={{ th }}">{{ th }}</a>
        {% endfor %}
    </div>

    <!-- Sophy's Mood -->
    <h3 style="color:#7fffd4; margin-top:25px; margin-bottom:10px; font-size:1.05rem;">
        Sophy's current mood
        <a class="refresh-button" href="/refresh/mood">⟳</a>
    </h3>
    <div class="topics">
        {% for mood in current_mood %}
            <a class="bubble"
               href="/?q={{ mood }}"
               style="
                   background-color: {{ mood_colors[mood] }};
                   color: #001018;
                   box-shadow: 0 0 12px {{ mood_colors[mood] }};
               ">
                {{ mood }}
            </a>
        {% endfor %}
    </div>

    {% if query %}
        {% if results %}
            <p class="meta">{{ results|length }} result(s) for "<strong>{{ query|e }}</strong>":</p>
            {% for r in results %}
                <div class="result">
                    <div class="result-header">https://chatgpt.com/g/g-p-680fe2d191688191a9e69485bd310a48-mnality/project
                        line {{ r.line_no }} &mdash; score {{ r.score }}
                    </div>
                    <div class="snippet">{{ r.snippet|safe }}</div>
                </div>
            {% endfor %}
        {% else %}
            <p class="no-results">No results found for "<strong>{{ query|e }}</strong>".</p>
        {% endif %}
    {% else %}
        <p class="no-results">Enter a query above to search the Sophy master log.</p>
    {% endif %}

    <div class="footer">
        Sophy Search &mdash; local log indexer. Master log path:
        <code>{{ master_path }}</code>
    </div>
</div>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    query = request.args.get("q", "", type=str)
    mode = request.args.get("mode", "current")   # NEW

    if mode == "current":
        topics_to_show = CACHED_TOPICS
    else:
        topics_to_show = load_historical_topics()

    # Search only, NO recomputing anything else
    results = search_log(query) if query.strip() else []

    return render_template_string(
        TEMPLATE,
        query=query,
        results=results,

        # Use cached values instead of recomputing
        topics=CACHED_TOPICS,
        mode=mode,
        themes_to_show=topics_to_show,
        current_mood=CACHED_MOOD_THEMES,
        mood_colors=CACHED_MOOD_COLORS,

        master_path=MASTER_LOG_PATH,
    )

@app.route("/refresh/topics")
def refresh_topics():
    global CACHED_TOPICS
    CACHED_TOPICS = extract_topics()

    # Save into historical archive
    append_historical_topics(CACHED_TOPICS)

    return redirect("/")

@app.route("/refresh/themes")
def refresh_themes():
    global CACHED_THEMES
    CACHED_THEMES = extract_general_themes()
    return redirect("/")

@app.route("/refresh/mood")
def refresh_mood():
    global CACHED_MOOD_THEMES, CACHED_MOOD_COLORS
    CACHED_MOOD_THEMES = extract_time_weighted_themes()
    CACHED_MOOD_COLORS = {m: mood_color_for_phrase(m) for m in CACHED_MOOD_THEMES}
    return redirect("/")

@app.route("/logs")
def show_logs():
    rows = load_log_rows(limit=200)
    return "<pre>" + "\n".join(f"{r['id']}: {r['text']}" for r in rows) + "</pre>"

if __name__ == "__main__":
    if not os.path.isfile(MASTER_LOG_PATH):
        open(MASTER_LOG_PATH, "a", encoding="utf-8").close()

    initialize_sophy_state()

    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

