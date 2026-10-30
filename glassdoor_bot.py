import os
import json
import time
import re
import urllib.parse
import tkinter as tk
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# SeleniumBase UC Driver & Selenium Locators
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

# Google GenAI SDK
from google import genai
from google.genai import types

# -------------------------------------------------------------
# 1. LOAD CREDENTIALS & INITIALIZE GEMINI
# -------------------------------------------------------------
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("Missing GEMINI_API_KEY in .env. Run encrypt_creds.py first.")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_CANDIDATES = ["gemini-3.5-flash-lite", "gemma-4-26b-a4b-it", "gemini-3.1-flash-lite"]

KB_FILE = os.path.abspath("knowledge_base.json")
JOB_CONTEXT_FILE = os.path.abspath("current_job.txt")

KB_CACHE = None
KB_MTIME = 0
AI_RESPONSE_CACHE = {}


def load_kb():
    """Reads the knowledge base and caches it in memory."""
    global KB_CACHE, KB_MTIME
    if os.path.exists(KB_FILE):
        try:
            mtime = os.path.getmtime(KB_FILE)
            if KB_CACHE is None or mtime != KB_MTIME:
                with open(KB_FILE, "r", encoding="utf-8") as f:
                    KB_CACHE = json.load(f)
                    KB_MTIME = mtime
            return KB_CACHE
        except Exception as e:
            print(f"[KB ERROR] Failed to load KB: {e}")
    return KB_CACHE or {}


def clean_question_string(text):
    """Cleans up asterisks, character counters, and extraneous whitespace."""
    if not text:
        return ""
    cleaned = text.replace("*", "").strip()
    cleaned = re.sub(r'\s*\b\d+\s*(?:of|/)\s*\d+\s*(?:characters?|chars?)?\s*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*\b\d+\s*(?:characters?|chars?)\b.*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def is_junk_header(text):
    """Filters out structural headers, character counts, and navigation chrome."""
    if not text or len(text.strip()) < 2:
        return True
    t_clean = text.strip().lower()

    if re.fullmatch(r'[\d\s/]+', t_clean):
        return True
    if re.search(r'^\s*\d+\s*(?:of|/)\s*\d+\s*(?:characters?|chars?)?\s*$', t_clean):
        return True

    # NOTE: "voluntary" and "equal opportunity" removed to prevent skipping demographic forms
    junk_phrases = [
        "contact info", "additional questions", "resume", "privacy notice",
        "character limit", "maximum characters",
        "answer these questions from the employer", "fields marked with", "to save time"
    ]
    if any(phrase in t_clean for phrase in junk_phrases) and len(t_clean) < 60:
        return True
    return False


def save_to_kb(category, question, answer):
    """Saves newly answered questions back to the knowledge base."""
    clean_q = clean_question_string(question)
    clean_a = str(answer).strip()
    if is_junk_header(clean_q) or not clean_q or not clean_a:
        return
    kb = load_kb()
    if category not in kb:
        kb[category] = {}
    kb[category][clean_q] = clean_a
    with open(KB_FILE, "w", encoding="utf-8") as f:
        json.dump(kb, f, indent=2, ensure_ascii=False)
    print(f"\n[KB MEMORY SAVED] '{clean_q}' -> '{clean_a}'")


def match_candidate_option(target_text, options_list):
    """
    Bulletproof option matcher that strictly prevents substring collisions
    (e.g., prevents 'Male' from matching 'Female', or 'Caucasian' from matching 'Asian').
    """
    if not target_text or not options_list:
        return None
    
    tgt = str(target_text).strip()
    tgt_lower = tgt.lower()

    # 1. Exact case-insensitive match
    for opt in options_list:
        if opt.strip().lower() == tgt_lower:
            return opt

    # 2. Strict isolated word-boundary match (whole words only)
    for opt in options_list:
        opt_clean = opt.strip()
        opt_lower = opt_clean.lower()
        
        # Guard against gender substring trap
        if opt_lower == "male" and "female" in tgt_lower:
            continue
        if opt_lower == "female" and tgt_lower == "male":
            continue

        # Guard against ethnicity substring trap
        if opt_lower == "asian" and "caucasian" in tgt_lower:
            continue
        if opt_lower == "caucasian" and tgt_lower == "asian":
            continue

        pattern = r'(?:\b|^)' + re.escape(tgt_lower) + r'(?:\b|$)'
        if re.search(pattern, opt_lower):
            return opt

    # 3. Reverse isolated word-boundary match
    for opt in options_list:
        opt_clean = opt.strip()
        opt_lower = opt_clean.lower()
        
        if opt_lower == "male" and "female" in tgt_lower:
            continue
        if opt_lower == "asian" and "caucasian" in tgt_lower:
            continue

        pattern = r'(?:\b|^)' + re.escape(opt_lower) + r'(?:\b|$)'
        if re.search(pattern, tgt_lower):
            return opt

    return None


# -------------------------------------------------------------
# 2. CLOUDFLARE, RECAPTCHA & ALREADY-APPLIED HANDLERS
# -------------------------------------------------------------
def is_cloudflare_present(driver):
    """Checks if Cloudflare is actively controlling the page."""
    try:
        title = driver.title.lower()
        if "just a moment..." in title or "attention required! | cloudflare" in title or "security check" in title:
            return True

        cf_elements = driver.find_elements(
            By.XPATH,
            '//iframe[contains(@src, "cloudflare") or contains(@src, "turnstile") or contains(@title, "challenge")] | '
            '//*[contains(text(), "Additional Verification Required")] | '
            '//*[contains(text(), "Humans only")] | '
            '//div[@id="challenge-running"] | '
            '//div[contains(@class, "cf-turnstile")]'
        )
        if cf_elements and any(e.is_displayed() for e in cf_elements):
            return True
    except Exception:
        pass
    return False


def wait_for_verification_to_clear(driver, max_wait=180):
    """Pauses bot during Cloudflare verification."""
    if not is_cloudflare_present(driver):
        return True

    print("\n" + "=" * 70)
    print("⚠️  [CLOUDFLARE DETECTED] Resolving verification...")
    print("=" * 70 + "\n")

    try:
        driver.uc_gui_click_captcha()
    except Exception:
        pass

    start_time = time.time()
    while time.time() - start_time < max_wait:
        time.sleep(2.0)
        if not is_cloudflare_present(driver):
            time.sleep(1.5)
            return True

    return False


def is_already_applied(driver):
    """Detects ONLY the genuine 'You have already applied to this job' screen, ignoring friction."""
    try:
        friction_markers = driver.find_elements(
            By.XPATH,
            '//*[contains(translate(text(), "APPLY ANYWAY", "apply anyway"), "apply anyway") or contains(text(), "meet these employer requirements")]'
        )
        for m in friction_markers:
            try:
                if m.is_displayed():
                    return False
            except Exception:
                pass

        applied_headings = driver.find_elements(
            By.XPATH,
            '//*[contains(text(), "You have already applied to this job")] | '
            '//h1[contains(translate(text(), "ALREADY APPLIED", "already applied"), "already applied")]'
        )
        for h in applied_headings:
            try:
                if h.is_displayed():
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def handle_friction_intervention(driver):
    """Bypasses 'It looks like you don’t meet these employer requirements' by clicking 'Apply anyway' safely."""
    try:
        friction_headers = driver.find_elements(
            By.XPATH,
            '//div[contains(@class, "ia-HardFrictionInterventionMessage")] | '
            '//*[contains(text(), "meet these employer requirements") or contains(text(), "employer requirements")]'
        )
        is_friction = False
        for h in friction_headers:
            try:
                if h.is_displayed():
                    is_friction = True
                    break
            except Exception:
                pass

        if not is_friction:
            return False

        print("\n" + "=" * 65)
        print("⚠️  [FRICTION SCREEN DETECTED] Employer requirements notice appeared.")
        print("=" * 65)

        apply_btns = driver.find_elements(
            By.XPATH,
            '//button[@data-testid="continue-button" and (contains(., "Apply anyway") or .//span[contains(text(), "Apply anyway")])]'
        )
        if not apply_btns:
            apply_btns = driver.find_elements(
                By.XPATH,
                '//button[contains(., "Apply anyway") and not(contains(., "Return"))] | '
                '//span[contains(text(), "Apply anyway")]/parent::button'
            )

        valid_btn = None
        for b in apply_btns:
            try:
                if b.is_displayed():
                    txt = b.text.strip().lower()
                    if "return" not in txt and "search" not in txt:
                        valid_btn = b
                        break
            except Exception:
                continue

        if not valid_btn:
            spans = driver.find_elements(By.XPATH, '//span[contains(text(), "Apply anyway")]')
            for s in spans:
                try:
                    if s.is_displayed():
                        valid_btn = s
                        break
                except Exception:
                    continue

        if not valid_btn:
            return False

        print("-> Target confirmed. Clicking 'Apply anyway'...")
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", valid_btn)
        except Exception:
            pass
        time.sleep(0.8)

        try:
            driver.execute_script("""
                const el = arguments[0];
                const span = el.querySelector('span') || el;
                el.focus();
                span.click();
                el.click();
            """, valid_btn)
        except Exception:
            pass

        try:
            ActionChains(driver).move_to_element(valid_btn).click().perform()
        except Exception:
            pass

        print("-> Waiting for friction screen to clear and transition...")
        start_wait = time.time()
        while time.time() - start_wait < 15.0:
            time.sleep(1.0)
            try:
                still_there = driver.find_elements(
                    By.XPATH,
                    '//div[contains(@class, "ia-HardFrictionInterventionMessage")] | //*[contains(text(), "meet these employer requirements")]'
                )
                is_still_visible = False
                for s in still_there:
                    try:
                        if s.is_displayed():
                            is_still_visible = True
                            break
                    except Exception:
                        pass

                if not is_still_visible:
                    print("[FRICTION CLEARED] Successfully bypassed! Proceeding to application...")
                    time.sleep(2.0)
                    return True
            except Exception:
                print("[FRICTION CLEARED] Screen transition confirmed. Proceeding...")
                time.sleep(2.0)
                return True

        return True

    except Exception as e:
        print(f"[FRICTION RECOVERED] {e}. Continuing flow safely...")
        return True


# -------------------------------------------------------------
# 3. CURRENT JOB CONTEXT PERSISTENCE
# -------------------------------------------------------------
def reset_and_save_job_context(title="", company_location="", description=""):
    content = (
        f"Job role: {title.strip()}\n\n"
        f"Company Name/ Location: {company_location.strip()}\n\n"
        f"Job Description:\n{description.strip()}\n"
    )
    with open(JOB_CONTEXT_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def read_job_context():
    if os.path.exists(JOB_CONTEXT_FILE):
        try:
            with open(JOB_CONTEXT_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return ""


# -------------------------------------------------------------
# 4. EMERGENCY RED ALERT & DATE POPUPS (Fallback Only)
# -------------------------------------------------------------
def trigger_red_alert_popup(question_text, options=None, field_type="text", placeholder=None):
    clean_q = clean_question_string(question_text)
    if is_junk_header(clean_q) or not clean_q:
        return ""

    root = tk.Tk()
    root.title("Action Required: Compulsory Question")
    root.geometry("640x400")
    root.configure(bg="#D32F2F")
    root.attributes("-topmost", True)

    msg_var = tk.StringVar()

    title_label = tk.Label(
        root,
        text="⚠️ ACTION REQUIRED - COMPULSORY QUESTION",
        font=("Helvetica", 13, "bold"),
        fg="white",
        bg="#D32F2F",
    )
    title_label.pack(pady=8)

    valid_options = [opt for opt in options if str(opt).lower() != 'on'] if options else None
    info_lines = [f"Question:\n{clean_q}", f"\nFormat: {field_type.upper()}"]
    if placeholder:
        info_lines.append(f"Placeholder: '{placeholder}'")
    if valid_options:
        info_lines.append(f"Choices: {valid_options}")

    desc_label = tk.Label(
        root,
        text="\n".join(info_lines),
        font=("Helvetica", 10),
        fg="white",
        bg="#D32F2F",
        wraplength=600,
        justify="center",
    )
    desc_label.pack(pady=6)

    if valid_options and len(valid_options) <= 6:
        btn_frame = tk.Frame(root, bg="#D32F2F")
        btn_frame.pack(pady=6)
        for opt in valid_options:
            def make_cmd(val=opt):
                msg_var.set(val)
                root.destroy()
            tk.Button(
                btn_frame, text=opt, command=make_cmd, font=("Helvetica", 9, "bold"),
                bg="white", fg="#D32F2F", padx=8, pady=3
            ).pack(side=tk.LEFT, padx=3, pady=2)

    entry = tk.Entry(root, textvariable=msg_var, width=50, font=("Helvetica", 11))
    entry.pack(pady=8)
    entry.focus()

    def on_submit():
        root.destroy()

    ok_btn = tk.Button(
        root,
        text="Save Answer & Continue",
        command=on_submit,
        bg="white",
        fg="#D32F2F",
        font=("Helvetica", 10, "bold"),
        padx=10,
        pady=5,
    )
    ok_btn.pack(pady=6)

    root.mainloop()
    return msg_var.get().strip()


def play_sound_alert():
    """Plays an audible alert chime (Windows beep or terminal bell)."""
    try:
        import winsound
        for freq in (880, 1100, 1320):
            winsound.Beep(freq, 150)
            time.sleep(0.05)
    except Exception:
        try:
            print('\a\a\a', end='', flush=True)
        except Exception:
            pass


def trigger_date_alert_popup(question_text, date_format="DD/MM/YYYY", wait_seconds=20):
    play_sound_alert()

    user_typed_date = [""]
    root = tk.Tk()
    root.title("Action Required: Date Selection")
    
    screen_width = root.winfo_screenwidth()
    root.geometry(f"560x320+{max(0, screen_width - 600)}+60")
    
    MAGENTA_COLOR = "#D81B60"
    root.configure(bg=MAGENTA_COLOR)
    root.attributes("-topmost", True)

    title_label = tk.Label(
        root,
        text="🗓️ ACTION REQUIRED - DATE PICKER",
        font=("Helvetica", 13, "bold"),
        fg="white",
        bg=MAGENTA_COLOR,
    )
    title_label.pack(pady=8)

    q_label = tk.Label(
        root,
        text=f"Question:\n{question_text}",
        font=("Helvetica", 10, "bold"),
        fg="white",
        bg=MAGENTA_COLOR,
        wraplength=520,
        justify="center",
    )
    q_label.pack(pady=4)

    instruction_label = tk.Label(
        root,
        text=f"Expected format: {date_format}\nSelect from the calendar in the browser OR type below:",
        font=("Helvetica", 9),
        fg="white",
        bg=MAGENTA_COLOR,
    )
    instruction_label.pack(pady=4)

    entry_var = tk.StringVar()
    entry = tk.Entry(root, textvariable=entry_var, font=("Helvetica", 11), justify="center", width=22)
    entry.pack(pady=6)
    entry.focus()

    countdown_var = tk.StringVar(value=f"Waiting for selection... ({wait_seconds}s remaining)")
    countdown_label = tk.Label(
        root,
        textvariable=countdown_var,
        font=("Helvetica", 10, "bold"),
        fg="#FFEB3B",
        bg=MAGENTA_COLOR,
    )
    countdown_label.pack(pady=4)

    def on_done():
        user_typed_date[0] = entry_var.get().strip()
        try:
            root.destroy()
        except Exception:
            pass

    btn = tk.Button(
        root,
        text="Done / Resume",
        command=on_done,
        font=("Helvetica", 10, "bold"),
        bg="white",
        fg=MAGENTA_COLOR,
        padx=10,
        pady=3
    )
    btn.pack(pady=6)

    remaining = [wait_seconds]

    def update_timer():
        if remaining[0] > 0:
            remaining[0] -= 1
            countdown_var.set(f"Waiting for selection... ({remaining[0]}s remaining)")
            root.after(1000, update_timer)
        else:
            on_done()

    root.protocol("WM_DELETE_WINDOW", on_done)
    root.after(1000, update_timer)
    root.mainloop()

    return user_typed_date[0]


def clean_and_format_ai_text(text, question_text=""):
    """Normalizes whitespace cleanly."""
    if not text:
        return ""
    t = str(text).strip().strip('"\'`')
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


# -------------------------------------------------------------
# 5. ADVANCED COGNITIVE AGENT (NO HARDCODED RULES)
# -------------------------------------------------------------
def resolve_answer_semantically(question_text, field_type="text", options=None, job_details=""):
    clean_q = clean_question_string(question_text)
    if not clean_q or is_junk_header(clean_q):
        return None

    cache_key = (clean_q.lower(), field_type, tuple(options or []))
    if cache_key in AI_RESPONSE_CACHE:
        return AI_RESPONSE_CACHE[cache_key]

    kb = load_kb()
    valid_options = [opt for opt in options if str(opt).lower() != 'on'] if options else None

    # Fully dynamic human-conscious reasoning prompt:
    # Analyzes citizenship, geography, demographics, and recruiter preferences naturally
    prompt = f"""You are the job applicant completing this employment application on your own behalf.
You possess complete consciousness of your candidate profile autobiography. Think, deduce, and respond with human-level intelligence, common sense, and strategic awareness that maximizes appeal to recruiters.

CONSCIOUS REASONING DIRECTIVES:
1. Complete Autobiography Awareness:
   - Deeply understand your profile: your identity, personal information, legal status, citizenship, education, work experience, tools, and job preferences.
   - For all personal, demographic, legal, and background questions (e.g., gender, race/ethnicity, disability status, veteran/military status, background checks):
     * Faithfully adhere to your documented facts:
       - Gender: Woman / Female (never select Male).
       - Disability: No disability ("disability_status": "No"). Always select the negative option ("No", "I do not have a disability", etc.).
       - Military / Veteran: No military/veteran service. Always select "No".
       - Race / Ethnicity: Asian / Asian (Indian).
       - Criminal / Non-compete: None / No.
2. Geopolitical & Legal Deductive Reasoning:
   - Relate your citizenship, residence, and passport to the context of the job dynamically:
     * Your nationality is Indian, residing in India, holding an Indian passport.
     * For jobs physically located in India: You do NOT require any visa sponsorship or work permit.
     * For jobs located in ANY country outside India (e.g. Germany, USA, UK, Singapore, Canada, EU, UAE, etc.): You DO require visa sponsorship / employment authorization.
     * Apply this deduction logically to any country or location referenced, even if not explicitly written in the profile.
3. Recruiter-Favorable Mindset:
   - When asked about availability, willingness to relocate, commuting, working shifts (night/rotational), working on-site or hybrid:
     * You are flexible, enthusiastic, fully available, and ready to start immediately.
     * Always answer in the manner that keeps your application moving forward positively.
4. Professional & Competency Questions:
   - For open-ended questions, craft articulate, concise, professional first-person ("I...") statements showcasing your relevant skills (Python, SQL, Data Analysis, Power BI, Excel, etc.) naturally and confidently.
5. Strict Verbatim Option Output:
   - When "Available Options" are provided:
     * Carefully evaluate each option and select the SINGLE best option VERBATIM from the list.
     * Do not rephrase, abbreviate, or alter punctuation.
   - If field_type is 'multiselect', return a valid JSON array of choices verbatim from Available Options, e.g. ["Singapore"] or [].
   - If field_type is 'numeric', output digits only.
   - If field_type is 'date':
     * For known historical facts (DOB, graduation date), return the exact date in the requested format.
     * For future scheduling/calendar dates not specified in the profile, return GUESSING_REQUIRED.
   - Output ONLY the exact answer string or JSON array. No explanations, markdown tags, or conversational filler.

--- CANDIDATE PROFILE (AUTOBIOGRAPHY) ---
{json.dumps(kb, indent=2, ensure_ascii=False)}

--- JOB CONTEXT ---
{job_details[:2000] if job_details else "Not provided"}

--- APPLICATION QUESTION ---
Question: "{clean_q}"
Expected Format: {field_type}
Available Options: {valid_options if valid_options else "None (free text / numeric)"}
"""

    for model_name in MODEL_CANDIDATES:
        try:
            config = types.GenerateContentConfig(temperature=0.0)
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )
            ans = response.text.strip() if response.text else ""
            ans = re.sub(r"^```(?:json)?\n|\n```$", "", ans).strip().strip('"\'')

            if ans and "UNKNOWN" not in ans:
                if field_type == "date":
                    if "GUESSING_REQUIRED" in ans:
                        return "GUESSING_REQUIRED"
                    AI_RESPONSE_CACHE[cache_key] = ans
                    return ans

                if field_type == "multiselect":
                    parsed_list = []
                    try:
                        match = re.search(r'\[.*?\]', ans, re.DOTALL)
                        if match:
                            raw_parsed = json.loads(match.group(0))
                            if isinstance(raw_parsed, list):
                                parsed_list = [str(item).strip() for item in raw_parsed]
                    except Exception:
                        pass

                    final_selected = []
                    if valid_options:
                        for item in parsed_list:
                            m = match_candidate_option(item, valid_options)
                            if m and m not in final_selected:
                                final_selected.append(m)
                    else:
                        final_selected = parsed_list

                    AI_RESPONSE_CACHE[cache_key] = final_selected
                    return final_selected

                elif field_type == "numeric":
                    digits = "".join(filter(str.isdigit, ans))
                    if digits:
                        ans = digits
                elif field_type in ["text", "textarea"]:
                    ans = clean_and_format_ai_text(ans, clean_q)

                # Match candidate option accurately using isolated boundaries
                if valid_options:
                    matched = match_candidate_option(ans, valid_options)
                    if matched:
                        ans = matched

                AI_RESPONSE_CACHE[cache_key] = ans
                return ans
            elif "UNKNOWN" in ans:
                return None
        except Exception:
            continue

    return None


def adapt_user_answer_to_format(raw_user_answer, field_type="text", options=None):
    if not raw_user_answer:
        return [] if field_type == "multiselect" else ""

    if field_type == "multiselect":
        if isinstance(raw_user_answer, list):
            return raw_user_answer
        raw_str = str(raw_user_answer).strip()
        matched = []
        if options:
            for opt in options:
                m = match_candidate_option(opt, [raw_str])
                if m:
                    matched.append(opt)
        return matched if matched else [raw_str]

    raw_str = str(raw_user_answer).strip()
    if field_type == "numeric":
        digits = "".join(filter(str.isdigit, raw_str))
        return digits if digits else raw_str

    if options:
        matched = match_candidate_option(raw_str, options)
        if matched:
            return matched

    if field_type == "checkbox":
        return "true" if any(w in raw_str.lower() for w in ["yes", "true", "1", "agree", "check"]) else "false"

    return clean_and_format_ai_text(raw_str)


# -------------------------------------------------------------
# 6. ATOMIC REACT PROTOTYPE INPUT SETTER
# -------------------------------------------------------------
def set_input_value_native(driver, element, value):
    """
    Injects text atomically into React state to prevent dropped keystrokes or typos.
    Handles HTMLInputElement and HTMLTextAreaElement with clipboard paste fallback.
    """
    val_str = str(value).strip()
    if not val_str:
        return

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
        time.sleep(0.2)
        element.click()
        time.sleep(0.1)
    except Exception:
        pass

    success = False
    try:
        driver.execute_script("""
            const el = arguments[0];
            const val = arguments[1];
            
            const isTextArea = (el instanceof HTMLTextAreaElement) || (el.tagName.toLowerCase() === 'textarea');
            const proto = isTextArea ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            
            const nativeSetter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
            if (nativeSetter) {
                nativeSetter.call(el, val);
            } else {
                el.value = val;
            }
            
            if (el._valueTracker) {
                el._valueTracker.setValue('');
            }
            
            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true, composed: true }));
        """, element, val_str)

        time.sleep(0.2)
        current_in_dom = element.get_attribute("value") or ""
        if len(current_in_dom) >= int(len(val_str) * 0.9):
            success = True
    except Exception as e:
        print(f"  [JS INJECT NOTE] {e}. Using clipboard paste...")

    if not success:
        try:
            clipboard_root = tk.Tk()
            clipboard_root.withdraw()
            clipboard_root.clipboard_clear()
            clipboard_root.clipboard_append(val_str)
            clipboard_root.update()
            clipboard_root.destroy()
            time.sleep(0.1)

            element.send_keys(Keys.CONTROL + "a")
            element.send_keys(Keys.BACKSPACE)
            element.send_keys(Keys.CONTROL + "v")
            time.sleep(0.3)
        except Exception:
            element.send_keys(Keys.CONTROL + "a")
            element.send_keys(Keys.BACKSPACE)
            element.send_keys(val_str)


def select_dropdown_native(driver, select_element, target_text):
    val_to_match = str(target_text).strip().lower()
    try:
        sel_obj = Select(select_element)
        for opt in sel_obj.options:
            opt_t = opt.text.strip().lower()
            if opt_t == val_to_match or match_candidate_option(val_to_match, [opt_t]):
                sel_obj.select_by_visible_text(opt.text)
                time.sleep(0.2)
                driver.execute_script("""
                    arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                    arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                """, select_element)
                time.sleep(0.3)
                return True
    except Exception:
        pass
    return False


# -------------------------------------------------------------
# 7. INDEED SMART APPLY COMPLETE ENGINE
# -------------------------------------------------------------
def extract_smart_apply_job_details(driver):
    title, company_location, description = "", "", ""
    try:
        t_elem = driver.find_elements(By.ID, "ia-JobInfoCard-header-title")
        if t_elem:
            title = t_elem[0].text.strip()
        h_info = driver.find_elements(By.XPATH, '//div[contains(@class, "ia-JobHeader-information")]//span')
        if h_info:
            company_location = h_info[0].text.strip()
        d_elem = driver.find_elements(By.XPATH, '//div[contains(@class, "ia-JobDescription")]')
        if d_elem:
            description = d_elem[0].text.strip()
    except Exception:
        pass

    if title or company_location or description:
        reset_and_save_job_context(title, company_location, description)
        print(f"[JOB CONTEXT SAVED] '{title}' at '{company_location}'")


def process_smart_apply_questions(driver, job_details):
    # 1. Native Dropdowns (<select>)
    native_dropdowns = driver.find_elements(By.TAG_NAME, "select")
    for sel in native_dropdowns:
        try:
            sel_obj = Select(sel)
            opts = [o.text.strip() for o in sel_obj.options if o.text.strip()]
            if not opts:
                continue

            curr_sel = sel_obj.first_selected_option.text.strip() if sel_obj.all_selected_options else ""
            q_label = ""
            try:
                parent = sel.find_element(By.XPATH, './ancestor::*[contains(@data-testid, "input-q_") or contains(@id, "q_")]')
                q_label = parent.find_element(By.TAG_NAME, "label").text.strip()
            except Exception:
                q_label = sel.get_attribute("aria-label") or ""

            clean_q = clean_question_string(q_label)
            if not clean_q or is_junk_header(clean_q):
                continue

            is_mandatory = ("*" in q_label) or (sel.get_attribute("required") is not None)
            target_ans = resolve_answer_semantically(clean_q, field_type="dropdown", options=opts, job_details=job_details)
            matched_opt = match_candidate_option(target_ans, opts)

            if not matched_opt and is_mandatory and (not curr_sel or "select" in curr_sel.lower()):
                raw_ans = trigger_red_alert_popup(clean_q, options=opts, field_type="dropdown")
                if raw_ans:
                    save_to_kb("dropdowns", clean_q, raw_ans)
                    matched_opt = match_candidate_option(raw_ans, opts)

            if matched_opt:
                print(f"  [AUDIT DROPDOWN] '{clean_q}' -> Verified: '{matched_opt}'")
                select_dropdown_native(driver, sel, matched_opt)
                time.sleep(0.3)
        except Exception:
            continue

    # 2. Combobox Dropdowns (Dynamic React Lists)
    comboboxes = driver.find_elements(
        By.XPATH,
        '//div[@role="combobox"] | '
        '//*[contains(@id, "single-select-question") and @role="combobox"] | '
        '//div[contains(@data-testid, "select-list-select-list")]'
    )
    for combo in comboboxes:
        try:
            if not combo.is_displayed():
                continue

            q_label = ""
            aria_lbl_id = combo.get_attribute("aria-labelledby")
            if aria_lbl_id:
                for lbl_id in aria_lbl_id.split():
                    lbl_elems = driver.find_elements(By.ID, lbl_id)
                    if lbl_elems and lbl_elems[0].text.strip():
                        q_label = lbl_elems[0].text.strip()
                        break

            if not q_label:
                try:
                    container = combo.find_element(
                        By.XPATH,
                        './ancestor::*[contains(@id, "q_") or contains(@data-testid, "input-q_")][1]'
                    )
                    q_label = container.find_element(By.XPATH, './/label | .//legend | .//h2 | .//h3').text.strip()
                except Exception:
                    pass

            if not q_label:
                q_label = combo.get_attribute("aria-label") or ""

            clean_q = clean_question_string(q_label)
            if not clean_q or is_junk_header(clean_q):
                continue

            combo_text = combo.text.strip()
            is_unanswered = (
                not combo_text or
                "select an option" in combo_text.lower() or
                "choose" in combo_text.lower()
            )

            is_mandatory = (
                ("*" in q_label) or
                (combo.get_attribute("aria-required") == "true") or
                ("error-text" in (combo.get_attribute("aria-describedby") or ""))
            )

            # Expand combobox if collapsed
            is_expanded = combo.get_attribute("aria-expanded") == "true"
            if not is_expanded:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", combo)
                time.sleep(0.3)
                try:
                    ActionChains(driver).move_to_element(combo).click().perform()
                except Exception:
                    driver.execute_script("arguments[0].click();", combo)
                time.sleep(0.8)

            popup_id = combo.get_attribute("aria-controls")
            option_elements = []
            if popup_id:
                option_elements = driver.find_elements(By.XPATH, f'//div[@id="{popup_id}"]//li[@role="option"]')

            if not option_elements:
                option_elements = driver.find_elements(By.XPATH, '//ul[@role="listbox"]//li[@role="option"] | //li[@role="option"]')

            options_map = {}
            for opt_el in option_elements:
                t = opt_el.text.strip()
                if not t:
                    testid = opt_el.get_attribute("data-testid") or ""
                    if "select-list-" in testid:
                        t = testid.split("select-list-")[-1].strip()
                if t:
                    options_map[t] = opt_el

            opts = list(options_map.keys())
            if not opts:
                continue

            target_ans = resolve_answer_semantically(clean_q, field_type="dropdown", options=opts, job_details=job_details)
            matched_opt_text = match_candidate_option(target_ans, opts)

            if not matched_opt_text and is_mandatory and is_unanswered:
                raw_ans = trigger_red_alert_popup(clean_q, options=opts, field_type="dropdown")
                if raw_ans:
                    save_to_kb("dropdowns", clean_q, raw_ans)
                    matched_opt_text = match_candidate_option(raw_ans, opts)

            if matched_opt_text:
                print(f"  [AUDIT COMBOBOX] '{clean_q}' -> Selecting: '{matched_opt_text}'")

                filter_inputs = driver.find_elements(
                    By.XPATH,
                    '//input[contains(@data-testid, "select-list-filter-input") or contains(@placeholder, "Search to select")]'
                )

                selected_successfully = False

                # Fix for Issue 1: Clear React search box cleanly without appending duplicates
                if filter_inputs and filter_inputs[0].is_displayed():
                    f_inp = filter_inputs[0]
                    try:
                        f_inp.click()
                        time.sleep(0.2)
                        
                        # Wipe out existing text cleanly using native event dispatch & keys
                        f_inp.send_keys(Keys.CONTROL + "a")
                        f_inp.send_keys(Keys.BACKSPACE)
                        driver.execute_script("""
                            arguments[0].value = '';
                            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                        """, f_inp)
                        time.sleep(0.2)

                        f_inp.send_keys(matched_opt_text)
                        time.sleep(0.6)

                        # Fix for Issue 1: Re-query options dynamically AFTER typing to avoid stale elements
                        filtered_options = driver.find_elements(
                            By.XPATH,
                            '//ul[@role="listbox"]//li[@role="option"] | //li[@role="option"]'
                        )
                        for opt_el in filtered_options:
                            t = opt_el.text.strip()
                            if match_candidate_option(matched_opt_text, [t]) or matched_opt_text.lower() in t.lower():
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", opt_el)
                                time.sleep(0.2)
                                try:
                                    ActionChains(driver).move_to_element(opt_el).click().perform()
                                except Exception:
                                    driver.execute_script("arguments[0].click();", opt_el)
                                selected_successfully = True
                                time.sleep(0.4)
                                break

                        if not selected_successfully:
                            f_inp.send_keys(Keys.ENTER)
                            selected_successfully = True
                            time.sleep(0.4)

                    except Exception as fe:
                        print(f"  [COMBO FILTER ERROR] {fe}")

                if not selected_successfully and matched_opt_text in options_map:
                    opt_el = options_map[matched_opt_text]
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", opt_el)
                    time.sleep(0.2)
                    try:
                        ActionChains(driver).move_to_element(opt_el).click().perform()
                    except Exception:
                        driver.execute_script("arguments[0].click();", opt_el)
                    time.sleep(0.4)

        except Exception as e:
            print(f"  [COMBOBOX ERROR] {e}")
            continue

    # 3. Date Picker Questions
    date_inputs = driver.find_elements(
        By.XPATH,
        '//input[contains(@id, "date-question") or contains(@placeholder, "YYYY") or contains(@placeholder, "MM/DD") or contains(@placeholder, "DD/MM") or @type="date"]'
    )
    for d_inp in date_inputs:
        try:
            if not d_inp.is_displayed() or not d_inp.is_enabled():
                continue

            curr_val = d_inp.get_attribute("value") or ""
            raw_label = ""
            
            inp_id = d_inp.get_attribute("id")
            if inp_id:
                lbls = driver.find_elements(By.XPATH, f'//label[@for="{inp_id}"]')
                if lbls and lbls[0].text.strip():
                    raw_label = lbls[0].text.strip()

            if not raw_label:
                try:
                    container = d_inp.find_element(By.XPATH, './ancestor::*[contains(@id, "q_") or contains(@data-testid, "input-q_")][1]')
                    raw_label = container.find_element(By.XPATH, './/label').text.strip()
                except Exception:
                    pass

            if not raw_label:
                raw_label = d_inp.get_attribute("aria-label") or ""

            clean_q = clean_question_string(raw_label)
            if not clean_q or is_junk_header(clean_q):
                continue

            date_fmt = d_inp.get_attribute("placeholder") or "DD/MM/YYYY"
            target_date = resolve_answer_semantically(clean_q, field_type="date", job_details=job_details)

            if target_date and target_date != "GUESSING_REQUIRED":
                print(f"  [AUDIT DATE] '{clean_q}' -> Fact Verified: '{target_date}'")
                set_input_value_native(driver, d_inp, target_date)
                time.sleep(0.3)
            elif not curr_val.strip() or target_date == "GUESSING_REQUIRED":
                print(f"\n⚠️  [DATE ALERT] Forward-looking/manual date question detected: '{clean_q}'")
                user_typed = trigger_date_alert_popup(clean_q, date_format=date_fmt, wait_seconds=20)
                if user_typed:
                    set_input_value_native(driver, d_inp, user_typed)
                    save_to_kb("dates", clean_q, user_typed)
                    print(f"  [DATE SAVED] Injected date: '{user_typed}'")
                else:
                    time.sleep(0.5)

        except Exception as e:
            print(f"  [DATE ERROR] {e}")
            continue

    # 4. Text / Textarea Inputs
    text_inputs = driver.find_elements(
        By.XPATH,
        '//div[contains(@id, "q_") or contains(@data-testid, "input-q_") or contains(@class, "contact-info")]//input['
        'not(@type="radio") and not(@type="checkbox") and '
        'not(contains(@id, "date-question")) and not(contains(@placeholder, "YYYY"))'
        '] | '
        '//div[contains(@id, "q_") or contains(@data-testid, "input-q_")]//textarea | '
        '//textarea'
    )

    for inp in text_inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue

            curr_val = inp.get_attribute("value") or ""
            raw_label = ""
            try:
                container = inp.find_element(By.XPATH, './ancestor::div[contains(@id, "q_") or contains(@data-testid, "input-q_")] | ./ancestor::div[contains(@class, "contact-info")][1]')
                raw_label = container.find_element(By.XPATH, './/label').text.strip()
            except Exception:
                raw_label = inp.get_attribute("aria-label") or ""

            clean_q = clean_question_string(raw_label)
            if not clean_q or is_junk_header(clean_q):
                continue

            is_mandatory = ("*" in raw_label) or (inp.get_attribute("required") is not None) or (inp.get_attribute("aria-required") == "true")
            input_mode = (inp.get_attribute("inputmode") or "").lower()
            field_type = "numeric" if (input_mode == "numeric" or "number" in (inp.get_attribute("type") or "")) else "text"

            target_ans = resolve_answer_semantically(clean_q, field_type=field_type, job_details=job_details)

            if not target_ans and is_mandatory and not curr_val.strip():
                raw_ans = trigger_red_alert_popup(clean_q, field_type=field_type, placeholder=inp.get_attribute("placeholder"))
                if raw_ans:
                    save_to_kb("general_questions", clean_q, raw_ans)
                    target_ans = adapt_user_answer_to_format(raw_ans, field_type=field_type)

            if target_ans:
                print(f"  [AUDIT INPUT] '{clean_q}' -> Verified: '{target_ans}'")
                set_input_value_native(driver, inp, target_ans)
                time.sleep(0.3)
        except Exception:
            continue

    # 5. Radio Groups (Expanded Locators & Safe Boundary Matching)
    radio_groups = driver.find_elements(
        By.XPATH,
        '//fieldset[.//input[@type="radio"]] | '
        '//div[@role="radiogroup"] | '
        '//*[(contains(@id, "q_") or contains(@data-testid, "input-q_")) and .//input[@type="radio"]]'
    )
    for rg in radio_groups:
        try:
            if not rg.is_displayed():
                continue

            raw_label = ""
            try:
                raw_label = rg.find_element(By.XPATH, './/legend | .//label[contains(@id, "label")] | .//h2 | .//h3').text.strip()
            except Exception:
                pass

            if not raw_label:
                try:
                    raw_label = rg.find_element(By.XPATH, './ancestor::*[contains(@id, "q_") or contains(@data-testid, "input-q_")]//legend').text.strip()
                except Exception:
                    pass

            if not raw_label:
                raw_label = rg.get_attribute("aria-label") or ""

            clean_q = clean_question_string(raw_label)
            if not clean_q or is_junk_header(clean_q):
                continue

            is_mandatory = ("*" in raw_label) or (rg.get_attribute("aria-required") == "true")
            radios = rg.find_elements(By.XPATH, './/input[@type="radio"]')
            radio_items = []
            selected_choice = ""

            for r in radios:
                choice_text = ""
                r_id = r.get_attribute("id")
                lbl_elem = None
                if r_id:
                    lbl = rg.find_elements(By.XPATH, f'.//label[@for="{r_id}"]')
                    if lbl:
                        lbl_elem = lbl[0]
                        choice_text = lbl_elem.text.strip()
                if not choice_text:
                    try:
                        p = r.find_element(By.XPATH, './ancestor::label')
                        lbl_elem = p
                        choice_text = p.text.strip()
                    except Exception:
                        pass
                if not choice_text:
                    choice_text = r.get_attribute("value") or ""

                choice_text = clean_question_string(choice_text)
                if choice_text and choice_text.lower() != "on":
                    radio_items.append({
                        "input": r,
                        "label_elem": lbl_elem or r,
                        "text": choice_text
                    })
                    if r.is_selected():
                        selected_choice = choice_text

            labels = [item["text"] for item in radio_items]
            if not labels:
                continue

            target_ans = resolve_answer_semantically(clean_q, field_type="radio", options=labels, job_details=job_details)
            matched_label = match_candidate_option(target_ans, labels)

            if not matched_label and is_mandatory and not selected_choice:
                raw_ans = trigger_red_alert_popup(clean_q, options=labels, field_type="radio")
                if raw_ans:
                    save_to_kb("radio_questions", clean_q, raw_ans)
                    matched_label = match_candidate_option(raw_ans, labels)

            # Fix for Issue 2: Select matched radio button with 100% boundary safety
            if matched_label:
                for item in radio_items:
                    if item["text"] == matched_label:
                        print(f"  [AUDIT RADIO] '{clean_q}' -> Selecting: '{matched_label}'")
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", item["label_elem"])
                        time.sleep(0.2)
                        try:
                            ActionChains(driver).move_to_element(item["label_elem"]).click().perform()
                        except Exception:
                            driver.execute_script("arguments[0].click();", item["label_elem"])
                        time.sleep(0.2)
                        if not item["input"].is_selected():
                            driver.execute_script("arguments[0].click();", item["input"])
                        time.sleep(0.3)
                        break
        except Exception:
            continue

    # 6. Multi-Select / Checkbox Groups
    checkbox_containers = driver.find_elements(
        By.XPATH,
        '//fieldset[.//input[@type="checkbox"]] | '
        '//*[(contains(@id, "q_") or contains(@data-testid, "input-q_")) and .//input[@type="checkbox"]]'
    )
    for container in checkbox_containers:
        try:
            if not container.is_displayed():
                continue

            raw_label = ""
            try:
                raw_label = container.find_element(By.XPATH, './/legend | .//label[contains(@id, "label")]').text.strip()
            except Exception:
                pass
            if not raw_label:
                try:
                    raw_label = container.find_element(By.XPATH, './ancestor::*[contains(@id, "q_") or contains(@data-testid, "input-q_")]//legend').text.strip()
                except Exception:
                    pass

            clean_q = clean_question_string(raw_label)
            if not clean_q or is_junk_header(clean_q):
                continue

            checkboxes = container.find_elements(By.XPATH, './/input[@type="checkbox"]')
            if not checkboxes:
                continue

            options_data = []
            labels = []

            for cb in checkboxes:
                cb_id = cb.get_attribute("id")
                lbl_elem = None
                opt_text = ""

                if cb_id:
                    lbls = container.find_elements(By.XPATH, f'.//label[@for="{cb_id}"]')
                    if lbls:
                        lbl_elem = lbls[0]
                        opt_text = lbl_elem.text.strip()

                if not opt_text:
                    try:
                        p = cb.find_element(By.XPATH, './ancestor::label')
                        lbl_elem = p
                        opt_text = p.text.strip()
                    except Exception:
                        pass

                if not opt_text:
                    opt_text = cb.get_attribute("value") or ""

                opt_text = clean_question_string(opt_text)
                if opt_text and opt_text.lower() != "on":
                    options_data.append({
                        "input": cb,
                        "label_element": lbl_elem or cb,
                        "text": opt_text
                    })
                    labels.append(opt_text)

            if not labels:
                continue

            is_mandatory = ("*" in raw_label) or (container.get_attribute("aria-required") == "true") or any(cb.get_attribute("aria-invalid") == "true" for cb in checkboxes)
            target_answers = resolve_answer_semantically(clean_q, field_type="multiselect", options=labels, job_details=job_details)

            has_selection = any(cb.is_selected() for cb in checkboxes)
            if not target_answers and is_mandatory and not has_selection:
                raw_ans = trigger_red_alert_popup(clean_q, options=labels, field_type="multiselect")
                if raw_ans:
                    save_to_kb("multiselect_questions", clean_q, raw_ans)
                    target_answers = adapt_user_answer_to_format(raw_ans, field_type="multiselect", options=labels)

            if target_answers is not None:
                if not isinstance(target_answers, list):
                    target_answers = [target_answers]

                for item in options_data:
                    opt_text = item["text"]
                    should_select = any(
                        match_candidate_option(ans, [opt_text]) is not None
                        for ans in target_answers
                    )
                    is_currently_selected = item["input"].is_selected()

                    if should_select and not is_currently_selected:
                        print(f"  [AUDIT MULTI-SELECT] '{clean_q}' -> Checking: '{opt_text}'")
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", item["label_element"])
                        time.sleep(0.2)
                        
                        try:
                            ActionChains(driver).move_to_element(item["label_element"]).click().perform()
                        except Exception:
                            driver.execute_script("arguments[0].click();", item["label_element"])

                        time.sleep(0.2)
                        if not item["input"].is_selected():
                            driver.execute_script("""
                                arguments[0].checked = true;
                                arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                                arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                            """, item["input"])

                    elif not should_select and is_currently_selected:
                        print(f"  [AUDIT MULTI-SELECT] '{clean_q}' -> Unchecking: '{opt_text}'")
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", item["label_element"])
                        time.sleep(0.2)
                        try:
                            ActionChains(driver).move_to_element(item["label_element"]).click().perform()
                        except Exception:
                            driver.execute_script("arguments[0].click();", item["label_element"])
        except Exception:
            continue


def heal_validation_errors(driver, job_details):
    """Detects active validation errors preventing submission and prompts user."""
    error_items = driver.find_elements(
        By.XPATH,
        '//div[contains(@id, "error-text") and string-length(text()) > 0] | '
        '//div[contains(@class, "error") and string-length(text()) > 0] | '
        '//*[contains(@class, "feedback--error")]'
    )
    visible_errors = [e for e in error_items if e.is_displayed()]
    if not visible_errors:
        return False

    print(f"\n[VALIDATION WARNING] Found {len(visible_errors)} required field error(s). Healing...")
    for err in visible_errors:
        try:
            container = err.find_element(By.XPATH, './ancestor::*[contains(@id, "q_") or contains(@data-testid, "input-q_")][1]')
            raw_q = container.find_element(By.XPATH, './/legend | .//label').text.strip()
            clean_q = clean_question_string(raw_q)
            if not clean_q:
                continue

            inp = container.find_elements(By.XPATH, './/input | .//textarea | .//select')
            if inp:
                raw_ans = trigger_red_alert_popup(clean_q, field_type="text")
                if raw_ans:
                    save_to_kb("general_questions", clean_q, raw_ans)
                    set_input_value_native(driver, inp[0], raw_ans)
                    time.sleep(0.5)
        except Exception:
            continue
    return True


def handle_smart_apply_flow(driver, smart_apply_tab, glassdoor_tab):
    """Executes Indeed Smart Apply, handles multi-audio reCAPTCHA, and reviews/submits."""
    print("--- [SMART APPLY] Switched to application tab ---")
    driver.switch_to.window(smart_apply_tab)
    time.sleep(2.5)

    if is_already_applied(driver):
        print("\n" + "=" * 65)
        print("ℹ️  [ALREADY APPLIED] 'You have already applied to this job' detected!")
        print("-> Closing tab and advancing to next job immediately...")
        print("=" * 65 + "\n")
        try:
            driver.close()
        except Exception:
            pass
        return "ALREADY_APPLIED"

    if not wait_for_verification_to_clear(driver, max_wait=120):
        print("[ABORT] Cloudflare challenge timed out.")
        try:
            driver.close()
        except Exception:
            pass
        return False

    extract_smart_apply_job_details(driver)
    job_details = read_job_context()

    submitted = False
    step = 0
    max_steps = 30

    try:
        while not submitted and step < max_steps:
            step += 1
            time.sleep(2.0)

            if is_already_applied(driver):
                print("\n[ALREADY APPLIED] 'You have already applied to this job' detected! Closing tab...\n")
                return "ALREADY_APPLIED"

            print(f"\n--- [APPLICATION STEP {step}] Inspecting page components ---")

            # reCAPTCHA SOLVER
            recaptcha_frames = driver.find_elements(
                By.XPATH,
                '//iframe[contains(@src, "recaptcha") and contains(@src, "anchor")] | //iframe[@title="reCAPTCHA"]'
            )
            visible_recaptcha = [f for f in recaptcha_frames if f.is_displayed()]

            if visible_recaptcha:
                print("\n🤖 [reCAPTCHA] Checkbox detected. Activating audio solver loop...")

                frame = visible_recaptcha[0]
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", frame)
                time.sleep(1.2)

                try:
                    driver.switch_to.frame(frame)
                    anchor = driver.find_element(By.ID, "recaptcha-anchor")
                    if anchor.get_attribute("aria-checked") != "true":
                        print("-> Clicking reCAPTCHA anchor checkbox...")
                        ActionChains(driver).move_to_element(anchor).pause(0.4).click().perform()
                except Exception as e:
                    print(f"-> Checkbox click error: {e}")
                    try:
                        driver.uc_gui_click_captcha()
                    except Exception:
                        pass
                finally:
                    driver.switch_to.default_content()

                time.sleep(3.0)

                max_audio_passes = 5
                for audio_pass in range(1, max_audio_passes + 1):
                    driver.switch_to.default_content()

                    try:
                        main_frames = driver.find_elements(
                            By.XPATH,
                            '//iframe[contains(@src, "recaptcha") and contains(@src, "anchor")] | //iframe[@title="reCAPTCHA"]'
                        )
                        if main_frames:
                            driver.switch_to.frame(main_frames[0])
                            anchor_el = driver.find_element(By.ID, "recaptcha-anchor")
                            if anchor_el.get_attribute("aria-checked") == "true":
                                driver.switch_to.default_content()
                                print(f"-> [reCAPTCHA SOLVED] Checkbox verified on pass {audio_pass - 1}!")
                                break
                    except Exception:
                        pass
                    finally:
                        driver.switch_to.default_content()

                    challenge_iframes = driver.find_elements(
                        By.CSS_SELECTOR,
                        "iframe[title*='recaptcha challenge'], iframe[src*='bframe'], iframe[name*='c-']"
                    )
                    visible_challenges = [c for c in challenge_iframes if c.is_displayed()]
                    if not visible_challenges:
                        print("-> [reCAPTCHA] Challenge dialogue cleared. Proceeding...")
                        break

                    print(f"-> [reCAPTCHA Pass {audio_pass}] Dialogue active. Inspecting...")
                    try:
                        driver.switch_to.frame(visible_challenges[0])
                    except Exception:
                        time.sleep(1.0)
                        continue

                    audio_btn = driver.find_elements(By.ID, "recaptcha-audio-button")
                    if audio_btn and audio_btn[0].is_displayed():
                        print("-> Clicking audio challenge button (#recaptcha-audio-button)...")
                        try:
                            ActionChains(driver).move_to_element(audio_btn[0]).pause(0.3).click().perform()
                            time.sleep(2.0)
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", audio_btn[0])
                            except Exception:
                                pass

                    multi_solution_notice = driver.find_elements(
                        By.XPATH,
                        '//*[contains(text(), "Multiple correct solutions required") or contains(text(), "solve more")]'
                    )
                    is_multi = len(multi_solution_notice) > 0 and multi_solution_notice[0].is_displayed()
                    if is_multi:
                        print("⚠️  [reCAPTCHA MULTI] 'Multiple correct solutions required' detected!")

                    buster_holders = driver.find_elements(
                        By.CSS_SELECTOR,
                        "div.button-holder.help-button-holder, .help-button-holder, button#solver-button"
                    )

                    if buster_holders and buster_holders[0].is_displayed():
                        b_elem = buster_holders[0]
                        print(f"-> Clicking orange Buster solver button (Round {audio_pass})...")
                        try:
                            ActionChains(driver).move_to_element(b_elem).pause(0.3).click().perform()
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", b_elem)
                            except Exception:
                                b_elem.click()

                        driver.switch_to.default_content()
                        wait_allotted = 15.0 if (audio_pass > 1 or is_multi) else 8.0
                        print(f"-> Allotting {wait_allotted}s for Buster to complete audio round {audio_pass}...")
                        time.sleep(wait_allotted)
                    else:
                        driver.switch_to.default_content()
                        time.sleep(2.0)

                driver.switch_to.default_content()

                print("-> Ensuring CAPTCHA dialogue has completely disappeared before submission...")
                barrier_start = time.time()
                while time.time() - barrier_start < 25.0:
                    ch_frames = driver.find_elements(
                        By.CSS_SELECTOR,
                        "iframe[title*='recaptcha challenge'], iframe[src*='bframe'], iframe[name*='c-']"
                    )
                    if not any(c.is_displayed() for c in ch_frames):
                        print("-> CAPTCHA dialogue is completely gone. Safe to submit!")
                        break
                    time.sleep(1.5)

            # PRIORITY 1: SUBMIT BUTTON
            submit_btns = driver.find_elements(
                By.XPATH,
                '//button[@data-testid="submit-application-button"] | '
                '//button[@name="submit-application"] | '
                '//button[contains(., "Submit your application") or contains(., "Submit application")]'
            )

            if submit_btns:
                print("\n🎯 Clicking Submit button...")
                sub = submit_btns[0]
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", sub)
                time.sleep(1.0)

                try:
                    ActionChains(driver).move_to_element(sub).pause(0.3).click().perform()
                except Exception:
                    driver.execute_script("arguments[0].click();", sub)

                time.sleep(4)
                submitted = True

                try:
                    close_btn = WebDriverWait(driver, 7).until(
                        EC.element_to_be_clickable((
                            By.XPATH,
                            '//button[@data-test="close-modal"] | //button[@aria-label="Close"]'
                        ))
                    )
                    close_btn.click()
                    print("[SUCCESS] Submitted and closed modal!")
                except Exception:
                    print("[SUCCESS] Submitted!")
                break

            # PRIORITY 2: Hard Friction ("Apply anyway")
            if handle_friction_intervention(driver):
                continue

            # PRIORITY 3: Pre-selected Resume Page
            resume_zone = driver.find_elements(By.ID, "mosaic-resumeSelectionModule")
            if resume_zone and resume_zone[0].is_displayed():
                print("[RESUME] Pre-selected resume page detected. Advancing...")
                r_cont = driver.find_elements(By.XPATH, '//button[@data-testid="continue-button"]')
                if r_cont:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", r_cont[0])
                    time.sleep(1.0)
                    driver.execute_script("arguments[0].click();", r_cont[0])
                    time.sleep(2.5)
                    continue

            # PRIORITY 4: Fill & Audit Employer Questions
            has_questions = driver.find_elements(
                By.XPATH,
                '//*[contains(@id, "q_") or contains(@data-testid, "input-q_") or contains(@class, "contact-info")] | '
                '//fieldset | '
                '//div[@role="combobox"]'
            )
            if has_questions:
                process_smart_apply_questions(driver, job_details)
                print("-> Pausing 2.0s so verified answers settle cleanly...")
                time.sleep(2.0)

            # PRIORITY 5: Advance with Continue / Review
            advance_clicked = False
            start_poll = time.time()
            while time.time() - start_poll < 10.0:
                cont_btns = driver.find_elements(
                    By.XPATH,
                    '//button[@data-testid="continue-button"] | '
                    '//button[contains(@class, "ia-continueButton")] | '
                    '//button[contains(@class, "demographic-questions") and (contains(., "Review") or .//span[contains(text(), "Review")])] | '
                    '//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "review your application")] | '
                    '//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "review application")] | '
                    '//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "continue applying")] | '
                    '//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "continue") and not(contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "return"))] | '
                    '//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "next")]'
                )
                if cont_btns:
                    target_btn = cont_btns[0]
                    btn_text = target_btn.text.strip().lower()
                    if "return" in btn_text or "job search" in btn_text:
                        continue

                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", target_btn)
                    time.sleep(0.5)

                    is_disabled = target_btn.get_attribute("disabled") is not None or target_btn.get_attribute("aria-disabled") == "true"
                    if not is_disabled:
                        print(f"[NAV] Clicking navigation button '{target_btn.text.strip()}' to advance...")
                        try:
                            ActionChains(driver).move_to_element(target_btn).click().perform()
                        except Exception:
                            driver.execute_script("arguments[0].click();", target_btn)
                        advance_clicked = True
                        time.sleep(3.0)
                        break
                time.sleep(1.0)

            if not advance_clicked:
                if heal_validation_errors(driver, job_details):
                    continue
                else:
                    time.sleep(2.0)

    finally:
        print("[SMART APPLY] Concluding application tab. Ensuring tab is closed...\n")
        try:
            if smart_apply_tab in driver.window_handles:
                driver.switch_to.window(smart_apply_tab)
                driver.close()
        except Exception:
            pass

    return submitted


# -------------------------------------------------------------
# 8. GLASSDOOR SEARCH & JOB PROCESSING ENGINE
# -------------------------------------------------------------
def run_glassdoor_bot(job_title="AI analyst", job_location="Singapore"):
    profile_dir = os.path.abspath("./chrome_profile_buster")

    print("\n--- INITIALIZING SELENIUMBASE UC DRIVER ---")
    driver = Driver(
        uc=True,
        user_data_dir=profile_dir,
        headless=False,
    )
    driver.set_window_size(1920, 1080)
    driver.maximize_window()

    wait = WebDriverWait(driver, 25)
    visited_job_ids = set()

    try:
        encoded_title = urllib.parse.quote_plus(job_title)
        search_url = f"https://www.glassdoor.co.in/Job/jobs.htm?sc.keyword={encoded_title}&applicationType=1"

        print(f"\n--- Opening Glassdoor ---")
        print(f"Job Title : {job_title}")
        print(f"Location  : {job_location}")
        driver.uc_open_with_reconnect(search_url, reconnect_time=4)
        driver.maximize_window()
        wait_for_verification_to_clear(driver, max_wait=120)

        glassdoor_window = driver.current_window_handle

        try:
            print("-> Typing location into the search box...")
            location_input = wait.until(EC.element_to_be_clickable((
                By.CSS_SELECTOR,
                "input[placeholder*='City'], input[placeholder*='location'], input[data-test='search-bar-location']"
            )))
            
            location_input.click()
            time.sleep(0.5)
            location_input.clear()
            time.sleep(0.3)
            location_input.send_keys(job_location)
            time.sleep(1.0)
            location_input.send_keys(Keys.ENTER)
            time.sleep(3.5)
            print("-> Location typed and search triggered.")
        except Exception as e:
            print(f"-> Could not type location automatically: {e}")

        print("\n--- Verifying 'Easy Apply only' filter pill state ---")
        try:
            easy_filter = wait.until(
                EC.presence_of_element_located((By.XPATH, '//button[@data-test="applicationType"]'))
            )
            time.sleep(1.5)
            if easy_filter.get_attribute("aria-pressed") != "true":
                print("-> Clicking 'Easy Apply only' filter via native ActionChains...")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", easy_filter)
                time.sleep(0.5)
                ActionChains(driver).move_to_element(easy_filter).click().perform()
                time.sleep(3.0)
            print("--- Filter Confirmed: Showing Easy Apply jobs only ---")
        except Exception as e:
            print(f"[FILTER NOTE] {e}")

        processed_count = 0

        while True:
            time.sleep(2.0)

            for handle in list(driver.window_handles):
                if handle != glassdoor_window:
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception:
                        pass

            if glassdoor_window in driver.window_handles:
                driver.switch_to.window(glassdoor_window)
            elif driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
                glassdoor_window = driver.current_window_handle

            job_cards = driver.find_elements(By.XPATH, '//li[@data-test="jobListing"]')
            total_cards = len(job_cards)
            print(f"\n[JOB LIST] Found {total_cards} job listings in the left panel.")

            new_job_found = False

            for idx in range(total_cards):
                if glassdoor_window in driver.window_handles:
                    driver.switch_to.window(glassdoor_window)

                cards = driver.find_elements(By.XPATH, '//li[@data-test="jobListing"]')
                if idx >= len(cards):
                    break

                card = cards[idx]
                job_id = card.get_attribute("data-jobid") or f"job_{idx}"

                if job_id in visited_job_ids:
                    continue

                visited_job_ids.add(job_id)
                new_job_found = True
                processed_count += 1

                try:
                    title_elem = card.find_elements(By.XPATH, './/a | .//span[contains(@class, "title")]')
                    card_title = title_elem[0].text.strip() if title_elem else f"Job ID {job_id}"
                except Exception:
                    card_title = f"Job ID {job_id}"

                print(f"\n{'='*25} JOB {processed_count}: '{card_title}' (ID: {job_id}) {'='*25}")

                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", card)
                    time.sleep(0.8)

                    click_targets = card.find_elements(
                        By.XPATH,
                        './/a[@data-test="job-title"] | .//a[@data-test="job-link"] | .//div[contains(@class, "jobCardContent")] | .//a'
                    )
                    primary_target = click_targets[0] if click_targets else card

                    print("-> Selecting job card with ActionChains pointer...")
                    try:
                        ActionChains(driver).move_to_element(primary_target).click().perform()
                    except Exception:
                        pass

                    driver.execute_script("""
                        const target = arguments[0];
                        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                            target.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window, button: 0, buttons: 1 }));
                        });
                    """, primary_target)

                    print(f"-> Verifying right pane synchronizes to ID {job_id}...")
                    switched_successfully = False
                    for _ in range(8):
                        time.sleep(0.5)
                        headers = driver.find_elements(By.XPATH, '//header[@data-test="job-details-header"]')
                        if headers:
                            h_views = headers[0].get_attribute("data-brandviews") or ""
                            h_html = headers[0].get_attribute("outerHTML") or ""
                            if str(job_id) in h_views or str(job_id) in h_html:
                                switched_successfully = True
                                break

                    if not switched_successfully:
                        try:
                            ActionChains(driver).move_to_element(card).click().perform()
                        except Exception:
                            driver.execute_script("arguments[0].click();", card)
                        time.sleep(1.5)

                    print(f"-> Right pane confirmed on '{card_title}'. Inspecting Easy Apply button...")

                    easy_apply_btn = None
                    for _ in range(8):
                        btns = driver.find_elements(
                            By.XPATH,
                            '//header[@data-test="job-details-header"]//button[@data-test="easyApply"] | '
                            '//div[contains(@class, "JobDetails")]//button[@data-test="easyApply"] | '
                            '//button[@data-test="easyApply"]'
                        )
                        if btns and btns[0].is_displayed():
                            easy_apply_btn = btns[0]
                            break
                        time.sleep(0.5)

                    if not easy_apply_btn:
                        print("-> No Easy Apply button found on right pane. Skipping to next card.")
                        continue

                    print("-> Clicking Easy Apply button...")
                    pre_handles = set(driver.window_handles)
                    try:
                        ActionChains(driver).move_to_element(easy_apply_btn).click().perform()
                    except Exception:
                        driver.execute_script("arguments[0].click();", easy_apply_btn)

                    new_tab_handle = None
                    for _ in range(8):
                        time.sleep(0.5)
                        diff = set(driver.window_handles) - pre_handles
                        if diff:
                            new_tab_handle = list(diff)[0]
                            break

                    if new_tab_handle:
                        status = handle_smart_apply_flow(driver, new_tab_handle, glassdoor_window)
                        if status == "ALREADY_APPLIED":
                            print("-> Already applied detected. Advancing to next listing.")
                    else:
                        print("[NOTE] Application opened in-page or no new tab detected.")

                except Exception as e:
                    print(f"[JOB ERROR] Error on job ID {job_id}: {e}")
                finally:
                    try:
                        time.sleep(1.0)
                        for h in list(driver.window_handles):
                            if h != glassdoor_window:
                                driver.switch_to.window(h)
                                driver.close()
                        driver.switch_to.window(glassdoor_window)
                    except Exception:
                        pass

            if not new_job_found:
                print("\n[PAGINATION] All currently visible jobs processed. Checking for 'Show more jobs'...")
                load_more_btns = driver.find_elements(By.XPATH, '//button[@data-test="load-more"]')
                if load_more_btns and load_more_btns[0].is_displayed():
                    print("-> Clicking 'Show more jobs'...")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", load_more_btns[0])
                    time.sleep(1.0)
                    driver.execute_script("arguments[0].click();", load_more_btns[0])
                    time.sleep(3.5)
                else:
                    print("[COMPLETED] No more jobs available. Job application session finished.")
                    break

    finally:
        print("\nAll tasks completed. Browser closing.")
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        run_glassdoor_bot(
            job_title="",
            job_location="Singapore"
        )
    except KeyboardInterrupt:
        print("\n[STOPPED] Bot stopped by user (Ctrl + C). Exiting cleanly...")
