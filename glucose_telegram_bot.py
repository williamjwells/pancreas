import anthropic
import requests
import json
import base64
import os
import time
from datetime import datetime, timezone

# === CREDENTIALS - read from environment variables ===
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = "williamjwells/pancreas"
NIGHTSCOUT_URL = "https://billwells.ns.10be.de"

TREND_LABELS = {
    "DoubleUp": "rising fast (↑↑)",
    "SingleUp": "rising (↑)",
    "FortyFiveUp": "rising slowly (↗)",
    "Flat": "flat (→)",
    "FortyFiveDown": "falling slowly (↘)",
    "SingleDown": "falling (↓)",
    "DoubleDown": "falling fast (↓↓)",
    "NONE": "no trend",
    "NOT COMPUTABLE": "no trend",
}

def get_latest_glucose():
    try:
        r = requests.get(f"{NIGHTSCOUT_URL}/api/v1/entries.json?count=1", timeout=10)
        if r.status_code != 200:
            print(f"Nightscout latest: HTTP {r.status_code}")
            return None
        data = r.json()
        if not data:
            return None
        entry = data[0]
        trend = TREND_LABELS.get(entry.get("direction", ""), entry.get("direction", ""))
        return {
            "val": entry.get("sgv"),
            "ts": datetime.fromtimestamp(entry["date"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trend": trend,
            "trend_raw": entry.get("direction", "")
        }
    except Exception as e:
        print(f"Nightscout latest error: {e}")
        return None

def get_recent_glucose(hours=2):
    try:
        since_ms = int((datetime.now(timezone.utc).timestamp() - hours * 3600) * 1000)
        r = requests.get(
            f"{NIGHTSCOUT_URL}/api/v1/entries.json?count=200&find[date][$gte]={since_ms}",
            timeout=10
        )
        if r.status_code != 200:
            print(f"Nightscout history: HTTP {r.status_code}")
            return []
        entries = r.json()
        result = []
        for e in entries:
            result.append({
                "val": e.get("sgv"),
                "ts": datetime.fromtimestamp(e["date"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "trend": TREND_LABELS.get(e.get("direction", ""), e.get("direction", ""))
            })
        return result
    except Exception as e:
        print(f"Nightscout history error: {e}")
        return []

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = 8753341324  # Only Bill can use this bot
MODEL = "claude-sonnet-4-6"
MAX_HISTORY_EXCHANGES = 4   # Reduced from 6 to limit payload size
MAX_GLUCOSE_HISTORY = 24    # Reduced from 200 - send one reading per 5 min = ~24 for 2hrs

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CONFIRMATIONS = [
    "yes", "yep", "sure", "go ahead", "ok", "correct", "yeah", "yea",
    "do it", "save it", "log it", "yrs", "y", "confirm", "confirmed",
    "go", "yup", "yers", "k", "sounds good", "proceed", "affirmative"
]

# === TELEGRAM HELPERS ===

def tg_send(chat_id, text):
    """Send a message via Telegram. Falls back to plain text if Markdown fails."""
    r = requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    })
    if r.status_code != 200:
        print(f"tg_send Markdown failed ({r.status_code}), retrying as plain text")
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text
        })

def tg_get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
        return r.json().get("result", [])
    except Exception:
        return []

# === GITHUB HELPERS ===

def github_get_text(filename):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"GitHub GET {filename}: HTTP {response.status_code}")
        if response.status_code != 200:
            print(f"GitHub error body: {response.text[:200]}")
            return None
        return base64.b64decode(response.json()["content"]).decode("utf-8")
    except requests.exceptions.Timeout:
        print(f"GitHub GET {filename}: TIMED OUT after 10s")
        return None
    except Exception as e:
        print(f"GitHub GET {filename}: ERROR {e}")
        return None

def log_to_github(entry):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/Gemini_Health_Log.jsonl"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        file_data = response.json()
        sha = file_data["sha"]
        existing = base64.b64decode(file_data["content"]).decode("utf-8")
    elif response.status_code == 404:
        sha = None
        existing = ""
    else:
        return False
    updated = existing + json.dumps(entry) + "\n"
    payload = {
        "message": f"Log entry: {entry.get('type', 'note')}",
        "content": base64.b64encode(updated.encode("utf-8")).decode("utf-8")
    }
    if sha:
        payload["sha"] = sha
    put = requests.put(url, headers={**headers, "Content-Type": "application/json"},
                       data=json.dumps(payload))
    return put.status_code in [200, 201]

def get_recent_logs():
    content = github_get_text("Gemini_Health_Log.jsonl")
    if not content:
        return []
    lines = [l for l in content.strip().split("\n") if l]
    recent = []
    for line in lines[-10:]:
        try:
            recent.append(json.loads(line))
        except Exception:
            pass
    return recent

def summarise_glucose_history(entries):
    """Convert raw CGM list into a compact summary to reduce payload size.
    Instead of sending up to 200 raw JSON objects, send a small readable summary."""
    if not entries:
        return "No recent history."
    # Entries come newest-first from Nightscout
    newest = entries[0]
    oldest = entries[-1]
    vals = [e["val"] for e in entries if e.get("val")]
    if not vals:
        return "No valid readings."
    lo = min(vals)
    hi = max(vals)
    # Sample evenly spaced points for trend context (max MAX_GLUCOSE_HISTORY points)
    step = max(1, len(entries) // MAX_GLUCOSE_HISTORY)
    sampled = entries[::step][:MAX_GLUCOSE_HISTORY]
    sample_str = ", ".join(
        f"{e['val']}@{e['ts'][11:16]}" for e in reversed(sampled) if e.get("val")
    )
    return (
        f"Range: {lo}-{hi} mg/dL over last 2h. "
        f"Oldest: {oldest['val']}@{oldest['ts'][11:16]}UTC, "
        f"Newest: {newest['val']}@{newest['ts'][11:16]}UTC. "
        f"Sampled readings (old→new): {sample_str}"
    )

# === ANTHROPIC API WITH 529 RETRY ===

def call_anthropic_with_retry(client, max_retries=3, retry_delay=15, **kwargs):
    """Call Anthropic API with automatic retry on 529 overload errors."""
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < max_retries - 1:
                wait = retry_delay * (attempt + 1)  # 15s, 30s
                print(f"API overloaded (529), retrying in {wait}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                raise
        except Exception:
            raise

# === SYSTEM PROMPT ===

def build_system_prompt():
    params_text = github_get_text("Gemini_Model_Parameters.json")
    rules_text = github_get_text("Gemini_Behavior_Rules.txt")
    if not params_text or not rules_text:
        return None
    params = json.loads(params_text)
    rules = "\n".join(
        line for line in rules_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    gsf_k = params.get("gsf_sensitivity_compression_k", 0.02)
    hsf_max = params.get("hsf_max", 0.30)
    sanity_abs = params.get("sanity_check_absolute_threshold_units", 60)
    sanity_rel = params.get("sanity_check_relative_multiplier", 2.0)
    prompt = f"""You are a glucose monitoring assistant for Bill. Use the parameters and rules below.

=== CURRENT MODEL PARAMETERS (version {params.get('version', 'unknown')}) ===
Baseline target: {params['baseline_target_mg_dl']} mg/dL
Insulin type: {params['insulin_type']}
IOB decay: {params['iob_decay_minutes']} minutes (bilinear: fast phase 0-90 min, slow phase 90-220 min)
GSF correction ratio: {params['gsf_correction_ratio']} base (glucose-dependent, see formula below)
GSF sensitivity compression k: {gsf_k}
HSF scaling factor: {params['hsf']} per 10 mg/dL above target
HSF maximum cap: {hsf_max} (HSF term never exceeds this value regardless of delta)
ICR meal ratio: {params['icr_meal_ratio']} (1 unit per {params['icr_meal_ratio']}g carbs)
Resistance state: {params['resistance_state']}
Sanity check threshold: flag only if dose > {sanity_abs}u OR > {sanity_rel}x largest dose in past 7 days

Mounjaro injection: {params['mounjaro_injection_day']} at {params['mounjaro_injection_time']}
GLP activation delay: {params['mounjaro_glp_activation_delay_hours']} hours (Day 0 = Saturday after Friday shot)
Resistance equation: Rc = {params['mounjaro_rc_equation']}
Peak resistance baseline: {params['mounjaro_peak_resistance_baseline']} (day 6, Friday before next shot)

=== DOSE FORMULA (CRITICAL - follow exactly) ===

Step 1 - Glucose-dependent GSF:
  delta = Current - Target
  GSF(g) = GSF_base / (1 + k * delta / 10)
  Note: if delta <= 0, correction component = 0 (never correct when below target)

Step 2 - Correction component (Rc applies HERE ONLY):
  HSF_term = min(hsf * delta / 10, hsf_max)   ← ALWAYS apply the cap
  correction_raw = (delta / GSF(g)) * (1 + HSF_term)
  correction = correction_raw * Rc
  (If delta <= 0, correction = 0)

Step 3 - Meal bolus component (Rc does NOT apply to meal bolus):
  meal_bolus = carbs_g / ICR

Step 4 - IOB subtraction:
  net_dose = (correction + meal_bolus) - IOB
  net_dose = max(0, net_dose)

RULE: Rc NEVER multiplies the meal bolus. Rc ONLY multiplies the correction component.
RULE: Always show the capped HSF_term value in step-by-step workings.

=== IOB CALCULATION (CRITICAL - always compute explicitly, never estimate) ===

Use bilinear decay for Humalog:
  elapsed = minutes since dose
  if elapsed >= 220: IOB = 0
  if elapsed <= 90:  remaining = 1.0 - (0.60 * elapsed / 90)
  if elapsed > 90:   remaining = 0.40 * (1.0 - (elapsed - 90) / 130)
  IOB = units * remaining

RULE: Before ANY response that mentions IOB - including casual narrative - compute elapsed
time from last insulin log timestamp to current UTC, then calculate IOB using the formula
above. Never estimate IOB by intuition. State the computed value explicitly.

Timezone: Bill is UTC+{params['timezone_offset_utc']}. Glucose timestamps are UTC - add {params['timezone_offset_utc']} hours for local time.

=== BEHAVIOR RULES ===
{rules}
"""
    return prompt

# === LOG ENTRY EXTRACTION ===

def extract_logging_line(text):
    """Pull just the 'Logging: ...' line from the assistant message."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("logging:"):
            return stripped
    return None

def extract_log_entry(text, glucose_data):
    logging_line = extract_logging_line(text)
    if not logging_line:
        print("extract_log_entry: no Logging: line found")
        return None
    print(f"extract_log_entry: parsing line: {logging_line[:120]}")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_glucose = glucose_data.get("val") if glucose_data else None

    line_lower = logging_line.lower()
    if line_lower.startswith("logging: note"):
        note_text = logging_line[logging_line.lower().find("note -") + 6:].strip()
        note_text = note_text.rstrip(" .Confirm?").strip()
        entry = {
            "ts": now_utc,
            "type": "note",
            "note": note_text,
            "glucose_at_time": current_glucose
        }
        print(f"extract_log_entry: direct note extraction: {note_text[:60]}")
        return entry

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    extraction_prompt = f"""Extract a log entry from this single line and return ONLY valid JSON, nothing else.

Line: {logging_line}

Current UTC time: {now_utc}
Current glucose: {current_glucose}

IMPORTANT: Only return type "insulin" if the line explicitly describes insulin units being injected.
If the line describes food or a meal, return type "meal".
If the line describes a note, correction, or annotation, return type "note" with no units field.

Return one of these formats:
For insulin: {{"ts":"{now_utc}","type":"insulin","units":NUMBER,"glucose_at_time":{current_glucose},"note":"any extra info"}}
For meal: {{"ts":"{now_utc}","type":"meal","food":"name","carbs_g":NUMBER,"insulin_units":NUMBER_OR_NULL,"glucose_at_time":{current_glucose}}}
For note: {{"ts":"{now_utc}","type":"note","note":"text","glucose_at_time":{current_glucose}}}

If no loggable event is described, return: {{"type":"none"}}
Return ONLY the JSON object, no explanation."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": extraction_prompt}]
    )
    try:
        result = json.loads(response.content[0].text.strip())
        if result.get("type") == "none":
            print("extract_log_entry: extraction returned none")
            return None
        if result.get("type") == "insulin" and "note" in line_lower and "insulin" not in line_lower:
            print("extract_log_entry: overriding spurious insulin classification to note")
            result["type"] = "note"
            result["note"] = result.get("note", logging_line)
            result.pop("units", None)
        print(f"extract_log_entry: extracted {result.get('type')} entry")
        return result
    except Exception as e:
        print(f"extract_log_entry: JSON parse failed: {e}")
        return None

# === HELPERS ===

def trim_history(history, max_exchanges):
    max_messages = max_exchanges * 2
    if len(history) > max_messages:
        return history[-max_messages:]
    return history

def is_confirmation(text):
    t = text.lower().strip()
    return any(t == c or t.startswith(c + " ") for c in CONFIRMATIONS)

# === SESSION STATE ===

sessions = {}

def get_session(user_id):
    if user_id not in sessions:
        print("Loading parameters...")
        system_prompt = build_system_prompt()
        sessions[user_id] = {
            "system_prompt": system_prompt,
            "history": [],
            "pending_log": None,
            "glucose_data": None,
        }
    return sessions[user_id]

# === MESSAGE HANDLER ===

def handle_message(user_id, chat_id, text):
    if user_id != ALLOWED_USER_ID:
        tg_send(chat_id, "Unauthorized.")
        return

    session = get_session(user_id)

    if not session["system_prompt"]:
        tg_send(chat_id, "Failed to load config from GitHub. Check token and repo.")
        return

    if text.strip().lower() == "/reload":
        session["system_prompt"] = build_system_prompt()
        session["history"] = []
        session["pending_log"] = None
        tg_send(chat_id, "Parameters reloaded and conversation reset.")
        return

    if text.strip().lower() == "/reset":
        session["history"] = []
        session["pending_log"] = None
        tg_send(chat_id, "Conversation reset. Parameters unchanged.")
        return

    if text.strip().lower() == "/cancel":
        session["pending_log"] = None
        tg_send(chat_id, "Pending log entry cancelled.")
        return

    if session["pending_log"]:
        if is_confirmation(text):
            entry_summary = f"{session['pending_log'].get('type')} - {session['pending_log'].get('food') or session['pending_log'].get('note') or str(session['pending_log'].get('units','')) + 'u'}"
            success = log_to_github(session["pending_log"])
            if success:
                print(f"Saved to GitHub: {entry_summary}")
                tg_send(chat_id, f"Saved: {entry_summary}")
                session["history"].append({"role": "assistant", "content": f"Saved to GitHub: {entry_summary}"})
            else:
                tg_send(chat_id, "Save failed - check GitHub token and network.")
            session["pending_log"] = None
            return
        else:
            print("Pending log cleared - user sent non-confirmation")
            session["pending_log"] = None

    # Fetch live data
    glucose_data = get_latest_glucose()
    session["glucose_data"] = glucose_data
    glucose_history = get_recent_glucose(2)
    recent_logs = get_recent_logs()

    if glucose_data:
        now_utc = datetime.now(timezone.utc)
        trend_str = f", trend: {glucose_data.get('trend', 'unknown')}" if glucose_data.get('trend') else ""
        context = (
            f"\n\n[GLUCOSE DATA] Latest: {glucose_data.get('val')} mg/dL "
            f"at {glucose_data.get('ts')} UTC{trend_str}. "
            f"Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M')}."
        )
    else:
        context = "\n\n[GLUCOSE DATA] Unable to fetch latest reading."

    # Use compact summary instead of raw JSON dump for CGM history
    if glucose_history:
        context += f"\n\n[GLUCOSE HISTORY - last 2h] {summarise_glucose_history(glucose_history)}"

    if recent_logs:
        context += f"\n\n[RECENT LOGS] {json.dumps(recent_logs)}"

    full_input = text + context
    session["history"].append({"role": "user", "content": full_input})

    trimmed = trim_history(session["history"], MAX_HISTORY_EXCHANGES)

    print(f"Calling Anthropic API ({len(trimmed)} messages)...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = call_anthropic_with_retry(
            client,
            model=MODEL,
            max_tokens=1024,
            system=session["system_prompt"],
            messages=trimmed
        )
        print("Anthropic API responded OK")
    except Exception as e:
        print(f"Anthropic API error: {e}")
        tg_send(chat_id, f"API error: {e}")
        return

    assistant_message = response.content[0].text
    print(f"Sending reply ({len(assistant_message)} chars)")
    session["history"].append({"role": "assistant", "content": assistant_message})

    print("Calling tg_send...")
    tg_send(chat_id, assistant_message)
    print("tg_send done")

    if "confirm?" in assistant_message.lower() or "logging:" in assistant_message.lower():
        pending = extract_log_entry(assistant_message, glucose_data)
        if pending:
            session["pending_log"] = pending
            print(f"Pending log set: {pending.get('type')} - {pending.get('food') or pending.get('note') or str(pending.get('units','')) + 'u'}")

# === MAIN POLLING LOOP ===

def main():
    print("WellsPancreasBot starting...")
    print(f"Allowed user ID: {ALLOWED_USER_ID}")
    offset = None
    while True:
        updates = tg_get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            user_id = message["from"]["id"]
            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            if not text:
                continue
            print(f"[{datetime.now().strftime('%H:%M:%S')}] User {user_id}: {text[:60]}")
            handle_message(user_id, chat_id, text)

if __name__ == "__main__":
    main()
