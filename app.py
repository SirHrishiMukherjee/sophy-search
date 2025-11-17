import os
import re
import html
from flask import Flask, request, render_template_string, redirect
import collections
import math
import hashlib
import colorsys
from filelock import FileLock

app = Flask(__name__)

# Path to the master log file that Sophy OS writes to
DISK_BASE = "/var/data"
log_folder = os.path.join(DISK_BASE, "Sophy_MasterLog")
os.makedirs(log_folder, exist_ok=True)
MASTER_LOG_PATH = os.path.join(log_folder, "sophy_master_log.txt")

HIST_DIR = os.path.join(DISK_BASE, "Sophy_HistoricalTopics")
os.makedirs(HIST_DIR, exist_ok=True)
HIST_TOPICS_PATH = os.path.join(HIST_DIR, "all_topics.txt")

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

def safe_read_log():
    """
    Safely read the entire master log with a shared lock so that
    writes cannot interleave or corrupt reads.
    """
    lock = FileLock(MASTER_LOG_PATH + ".lock")

    with lock:  # shared read section
        with open(MASTER_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

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
    """
    Search the master log file for the query and return top matches.
    Ranking is based on term frequency in each line.
    Lock-safe version: uses safe_read_log() to avoid partial reads.
    """
    if not query.strip():
        return []

    # Normalize query
    query = query.strip()
    terms = [t.lower() for t in re.split(r"\s+", query) if t.strip()]
    if not terms:
        return []

    results = []

    if not os.path.isfile(MASTER_LOG_PATH):
        return []

    # SAFELY read entire log (shared lock)
    text = safe_read_log()

    # Iterate with stable, correct line numbers
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        line_lower = line.lower()

        # Compute score based on how many times each term appears
        score = 0
        for term in terms:
            score += line_lower.count(term)

        if score > 0:
            results.append({
                "line_no": line_no,
                "text": line,
                "score": score,
            })

    # Sort by score descending, then by line number ascending
    results.sort(key=lambda r: (-r["score"], r["line_no"]))

    # Truncate to limit
    top = results[:limit]

    # Build snippets with highlighted matches
    for r in top:
        r["snippet"] = highlight_terms(r["text"], terms)

    return top

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

def extract_topics(limit=25, tail_bytes=250_000):
    """
    Extract topics only from the tail of the log (fast).
    250k chars ~ last few thousand lines.
    """
    if not os.path.isfile(MASTER_LOG_PATH):
        return []

    lock = FileLock(MASTER_LOG_PATH + ".lock")
    with lock:
        with open(MASTER_LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - tail_bytes)
            f.seek(start)
            raw = f.read().decode(errors="ignore")

    # If we started mid-line, drop the first partial line
    lines = raw.splitlines()[1:]

    topics = []
    for line in lines:
        line = line.strip()
        if not line: 
            continue

        parts = line.split()
        if parts:
            topics.append(parts[0])

        caps = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", line)
        topics.extend(caps)

        prefix = re.split(r"[:\-]", line)[0]
        if len(prefix.split()) == 1:
            topics.append(prefix)

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
    """Append the current extracted topics into historical file."""
    from filelock import FileLock
    
    lock = FileLock(HIST_TOPICS_PATH + ".lock")
    with lock:
        with open(HIST_TOPICS_PATH, "a", encoding="utf-8") as f:
            for t in topics:
                f.write(t + "\n")

def load_historical_topics(limit=25):
    """Return prevalence-weighted historical topics with random jitter."""
    if not os.path.isfile(HIST_TOPICS_PATH):
        return []

    lock = FileLock(HIST_TOPICS_PATH + ".lock")
    
    with lock:
        with open(HIST_TOPICS_PATH, "r", encoding="utf-8") as f:
            lines = [l.strip().lower() for l in f if l.strip()]

    if not lines:
        return []

    freq = collections.Counter(lines)

    import random

    # Weighted randomness
    def score(t):
        return freq[t] * (1 + random.uniform(-0.25, 0.25))

    ranked = sorted(freq.keys(), key=lambda t: score(t), reverse=True)

    return ranked[:limit]

def extract_general_themes(limit=10):
    if not os.path.isfile(MASTER_LOG_PATH):
        return []

    # SAFELY read the entire log (shared lock, no race conditions)
    text = safe_read_log()

    lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
    if not lines:
        return []

    # Remove boilerplate prefixes by skipping first N tokens
    cleaned_lines = []
    for line in lines:
        tokens = re.findall(r"[a-zA-Z0-9]+", line)
        if len(tokens) > 3:
            tokens = tokens[2:]    # Drop first 2 OS meta-tokens
        cleaned_lines.append(tokens)

    # Collect candidate multiword phrases
    candidate_phrases = []

    for tokens in cleaned_lines:
        # Filter out stopwords
        tokens = [t for t in tokens if t not in STOPWORDS]

        # Extract 3–6 word sliding windows
        for n in range(3, 7):
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i:i+n])
                if len(phrase) > 10:    # skip tiny phrases
                    candidate_phrases.append(phrase)

    if not candidate_phrases:
        return []

    counter = collections.Counter(candidate_phrases)

    # Information density score (unchanged)
    def score(phrase):
        length_score = len(phrase.split())
        rarity_score = sum(1 for w in phrase.split() if w not in STOPWORDS)
        freq_score = counter[phrase]
        return freq_score * rarity_score * (0.8 + 0.2 * length_score)

    # Rank by score
    ranked = sorted(counter.keys(), key=lambda p: score(p), reverse=True)

    # Deduplicate by semantic prefix
    themes = []
    seen_starts = set()

    for phrase in ranked:
        start = " ".join(phrase.split()[:2])
        if start not in seen_starts:
            seen_starts.add(start)
            themes.append(phrase)
        if len(themes) >= limit:
            break

    return themes

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

def extract_time_weighted_themes(limit=5):
    if not os.path.isfile(MASTER_LOG_PATH):
        return []

    # SAFELY read full log using shared lock
    text = safe_read_log()
    lines = [l.strip().lower() for l in text.splitlines() if l.strip()]

    if not lines:
        return []

    # Only use recent lines (defines Sophy's "mood")
    SAMPLE_SIZE = 20000
    chunk = lines[-SAMPLE_SIZE:]

    total = len(chunk)
    if total == 0:
        return []

    decay_constant = total / 3.0  # how fast old lines lose influence

    # Preprocess lines
    processed = []
    for idx, line in enumerate(chunk):
        tokens = re.findall(r"[a-zA-Z0-9]+", line)

        # Remove OS-prefix tokens
        if len(tokens) > 3:
            tokens = tokens[2:]

        # Remove your STOPWORDS
        tokens = [t for t in tokens if t not in STOPWORDS]

        if tokens:
            processed.append((idx, tokens))

    # Time-weighted candidate phrase extraction
    candidates = []
    for idx, tokens in processed:
        age = total - idx
        time_weight = math.exp(-age / decay_constant)

        for n in range(3, 7):  # 3–6 word windows
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i:i+n])
                if len(phrase) > 8:
                    candidates.append((phrase, time_weight))

    if not candidates:
        return []

    # Aggregate weighted frequencies + rarity
    freq = collections.defaultdict(float)
    rarity = collections.defaultdict(float)

    for phrase, w in candidates:
        freq[phrase] += w
        rarity[phrase] = max(
            rarity[phrase],
            len([t for t in phrase.split() if t not in STOPWORDS])
        )

    # Composite theme score
    def theme_score(p):
        length_weight = len(p.split())
        return freq[p] * rarity[p] * length_weight

    ranked = sorted(freq.keys(), key=lambda p: theme_score(p), reverse=True)

    # Deduplicate by semantic signature (first 2 words)
    themes = []
    seen = set()

    for p in ranked:
        sig = " ".join(p.split()[:2])
        if sig not in seen:
            seen.add(sig)
            themes.append(p)

        if len(themes) >= limit:
            break

    return themes

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

if __name__ == "__main__":
    if not os.path.isfile(MASTER_LOG_PATH):
        open(MASTER_LOG_PATH, "a", encoding="utf-8").close()

    initialize_sophy_state()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

