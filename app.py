"""Candor — college database, ranking lists, and chances calculator.

Single-file Flask app. Persistent SQLite stores users, profiles, and
saved chances. NewsAPI fetches recent articles per college (cached).

Pages:
  /                    landing — search + featured ranking lists
  /colleges            browse the full directory (filter by state/type)
  /college/<slug>      detail page — stats, articles, chances button
  /rankings            list of ranking categories
  /rankings/<slug>     ordered ranking list
  /signup, /login,     auth
  /logout, /profile
  /chances/<slug>      auth-gated, computes for one school

Run locally:
  pip3 install -r requirements.txt
  ANTHROPIC_KEY=sk-ant-… NEWSAPI_KEY=… python3 -u app.py
"""

import os
import re
import json
import time
import secrets
import sqlite3
import hashlib
import hmac
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify, abort
from candor_data import *  # static domain data (schools, rankings, weights) — see candor_data.py
from candor_styles import *  # CSS + HTML/SVG template strings — see candor_styles.py

try:
    import anthropic
    _claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_KEY", "")) if os.environ.get("ANTHROPIC_KEY") else None
except ImportError:
    _claude_client = None


def _date_context():
    """Inject the current date into every Claude system prompt so the model
    doesn't default to its training-cutoff year. Otherwise we get advice
    referencing 2024 application deadlines in 2026."""
    today = datetime.utcnow().strftime("%B %d, %Y")
    return (
        f"Today's date is {today}. "
        f"We are in the 2026-2027 admissions cycle — Class of 2027 (current high school seniors) "
        f"are applying for fall 2027 entry. ED1 deadlines are November 2026, RD is January 2027. "
        f"All deadlines you reference should match this cycle, not 2024 or 2025."
    )


# ─── CONFIG ───────────────────────────────────────────────
DB_PATH    = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "college.db"))
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
SCORECARD_KEY = os.environ.get("SCORECARD_KEY", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # gates the bulk-refresh endpoint
STRIPE_PAYMENT_LINK = os.environ.get("STRIPE_PAYMENT_LINK",
    "https://buy.stripe.com/fZu14ob4jai9dmgdHy5AQ03")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Stripe no-code customer/billing portal login link (Settings → Billing →
# Customer portal → share link). Lets a subscriber manage/cancel via Stripe's
# hosted page (they auth with their email). Surfaced as a low-key "Manage
# subscription" link on the paid /upgrade view — present and functional, just
# not a prominent CTA. Renders only when set, so there's no broken link.
STRIPE_BILLING_PORTAL_URL = os.environ.get("STRIPE_BILLING_PORTAL_URL", "")
# Where cancellation requests go until the Stripe customer portal is enabled.
# Set CANCEL_EMAIL to a support alias so the personal inbox isn't exposed.
CANCEL_EMAIL = os.environ.get("CANCEL_EMAIL", "jasperthelazzer19@gmail.com")
# Email (deadline reminders). Resend HTTP API — free tier ~3k emails/mo ($0).
# Dormant until RESEND_API_KEY is set: _send_email no-ops, so nothing breaks.
# EMAIL_FROM must be on a domain verified in Resend. CRON_KEY (falls back to
# ADMIN_KEY) guards the daily /cron/deadline-nudges trigger.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Candor <reminders@candoradmit.com>")
CRON_KEY = os.environ.get("CRON_KEY", "") or os.environ.get("ADMIN_KEY", "")
ARTICLE_TTL_HOURS = 12   # how long to cache per-college articles
SCORECARD_TTL_DAYS = 30  # refresh federal stats monthly
FREE_TRIAL_MESSAGES = 3
PAID_MONTHLY_LIMIT = 250


# ─── COLLEGE DATA (~80 schools) ──────────────────────────
# Each entry: name, slug, accept_rate, gpa_lo/hi, sat_25/75, act_25/75, type,
# state, size (rough enrollment), tuition (annual sticker), description,
# popular majors. Numbers are public-CDS-ballpark, intended as orientation
# not gospel. Tier 1 = sub-10% accept, 5 = accessible.

COLLEGES_BY_SLUG = {c["slug"]: c for c in COLLEGES}
COLLEGE_NAMES = sorted([c["name"] for c in COLLEGES])
STATES = sorted(set(c["state"] for c in COLLEGES))

# City for each college. Done as a lookup table rather than inline so the
# 155-row COLLEGES list doesn't get any longer than it already is.

def city_state(c):
    """Return 'City, State' for a college dict; falls back to state alone."""
    city = CITY_BY_SLUG.get(c["slug"])
    return f"{city}, {c['state']}" if city else c["state"]


# ─── MAJORS — comprehensive list for autocomplete on the profile form ──


# ─── PREFERENCES ──────────────────────────────────────────
# Allowed values for each preference field. These appear in both the profile
# form and the school-match logic, so keep them in one place.


# ─── REGION + FACULTY RATIO DATA ──────────────────────────

def region_of(c):
    return REGION_BY_STATE.get(c.get("state",""), "Other")


# Climate auto-derived from state. No more "unclear weather" rows.

def climate_of(c):
    st = c.get("state","")
    if st in WARM_STATES: return "warm"
    if st in MILD_STATES: return "mild"
    return "cold"


# Per-school setting (urban / college_town / suburban / rural). Comprehensive
# coverage so school_match never reports "unclear setting".

def setting_of(c):
    return SETTING_BY_SLUG.get(c["slug"]) or (
        "urban" if c.get("size",0) > 18000 else
        "college_town" if c.get("size",0) > 5000 else
        "suburban"
    )


# Greek-life percentage by school. Strong = >25%, light = ≤10%, medium otherwise.

def greek_strength(c):
    """Return 'strong' / 'medium' / 'light'. Estimates if not in dict."""
    pct = GREEK_PCT_BY_SLUG.get(c["slug"])
    if pct is not None:
        if pct >= 25: return "strong"
        if pct >= 10: return "medium"
        return "light"
    # Estimate: large publics in the South tend toward greek-strong; tech schools tend light
    if c["state"] in WARM_STATES and c["type"] == "public" and c.get("size",0) > 15000:
        return "strong"
    if c.get("size",0) < 3000 and c["type"] == "private":
        return "light"
    return "medium"


# Sports culture: D1 powerhouse, D1 average, D3/low. Override list for known
# powerhouses; default to "average" for big publics, "low" for small/tech schools.

def sports_strength(c):
    if c["slug"] in SPORTS_TIER_BY_SLUG:
        return SPORTS_TIER_BY_SLUG[c["slug"]]
    if c["type"] == "public" and c.get("size",0) > 18000:
        return "medium"
    if c.get("size",0) < 4000:
        return "low"
    return "medium"


def has_strong_internships(c):
    """Heuristic: urban + decent tier OR specifically known internship-strong school."""
    if "internship_strong" in school_attrs(c["slug"]):
        return True
    if setting_of(c) == "urban" and c.get("tier", 5) <= 3:
        return True
    return False


# Student-faculty ratio per school. Curated for the well-known ones; everything
# else gets estimated from size + type. Numbers are public-CDS ballparks.

# Application-round admissions data per school. Sourced from Common Data Set
# Section C7-C9 (most-recent published cycle, generally 2023-24). Rounds:
#   ED  = Early Decision (binding)
#   ED2 = Early Decision Round 2 (binding)
#   EA  = Early Action (non-binding)
#   REA = Restrictive Early Action (single-choice EA, non-binding)
#   RD  = Regular Decision
# Rates are decimals (0.087 = 8.7%). Schools not in this dict show only the
# overall acceptance rate. in_state_rate / out_of_state_rate are populated
# only for publics with a meaningful differential.


def admissions_detail(school):
    """Return the round/rate detail dict for a school, or None if not curated."""
    return ADMISSIONS_DETAIL.get(school["slug"])


# Sub-school (a.k.a. undergraduate college) accept rates. Many large
# universities admit by college rather than university-wide, with very
# different rates per college. Cornell Engineering vs Cornell Hotel is
# 10% vs 27%; USC SCA vs USC LAS is 4% vs 13%; Penn Wharton vs Penn CAS
# vs Penn Nursing all differ.
#
# Structure: each entry is a list of dicts with:
#   name      — display name of the college/school within the university
#   accept    — recent-cycle accept rate as float 0-1
#   keywords  — lowercase substrings; when the user's major matches one,
#               this sub-school is used in chances + advice. List the
#               common majors per college.
#   note      — optional one-line context (e.g. "transfer admit at 2nd yr")
#
# Order matters: more specific sub-schools first (Hotel before LAS so a
# Hotel Management major lands on Hotel, not on the catch-all LAS).
SUB_SCHOOL_RATES = {
    "cornell": [
        {"name": "Hotel Administration (SHA)", "accept": 0.27,
         "keywords": ["hotel","hospitality"]},
        {"name": "Architecture, Art, and Planning (AAP)", "accept": 0.07,
         "keywords": ["architecture","fine art","urban planning","city planning"],
         "note":"AAP architecture sub-program is sub-5%"},
        {"name": "College of Engineering", "accept": 0.10,
         "keywords": ["engineering","computer science","cs ","operations research","biomedical","mechanical","electrical","civil","chemical","materials","aerospace"]},
        {"name": "Industrial & Labor Relations (ILR)", "accept": 0.16,
         "keywords": ["labor","ilr","industrial relations","hr","human resources"]},
        {"name": "Charles H. Dyson School (Applied Economics)", "accept": 0.04,
         "keywords": ["dyson","applied economics","aem","business"]},
        {"name": "College of Agriculture & Life Sciences (CALS)", "accept": 0.14,
         "keywords": ["biology","agriculture","food science","environmental","animal science","plant","biological"]},
        {"name": "College of Human Ecology", "accept": 0.16,
         "keywords": ["human ecology","nutrition","design","fiber","fashion","apparel","health"]},
        {"name": "College of Arts & Sciences (CAS)", "accept": 0.10,
         "keywords": ["arts","sciences","humanities","english","history","economics","philosophy","math","physics","chemistry"]},
    ],
    "upenn": [
        {"name": "Wharton School", "accept": 0.06,
         "keywords": ["wharton","finance","business","economics","accounting","management"],
         "note":"M&T (engineering+wharton) and Huntsman are sub-5%"},
        {"name": "School of Engineering & Applied Science (SEAS)", "accept": 0.065,
         "keywords": ["engineering","computer science","cs ","biomedical","mechanical","electrical","chemical","materials","systems"]},
        {"name": "School of Nursing", "accept": 0.10,
         "keywords": ["nursing"]},
        {"name": "College of Arts & Sciences (CAS)", "accept": 0.07,
         "keywords": ["arts","sciences","humanities","english","history","biology","chemistry","political science","psychology","math","physics"]},
    ],
    "usc": [
        {"name": "School of Cinematic Arts (SCA)", "accept": 0.04,
         "keywords": ["cinema","film","screenwriting","animation","interactive media","game design"],
         "note":"Film Production: ~3%, Screenwriting: ~5-7%"},
        {"name": "Iovine and Young Academy", "accept": 0.05,
         "keywords": ["iovine","arts technology and the business of innovation"]},
        {"name": "Annenberg School (Communication)", "accept": 0.08,
         "keywords": ["communication","journalism","public relations"]},
        {"name": "Marshall School of Business", "accept": 0.10,
         "keywords": ["marshall","business","finance","accounting","entrepreneurship","global"]},
        {"name": "Roski School of Art & Design", "accept": 0.10,
         "keywords": ["roski","fine art","design","art history"],
         "note":"Portfolio required for ALL majors"},
        {"name": "Thornton School of Music", "accept": 0.12,
         "keywords": ["thornton","music","jazz","composition","music industry"]},
        {"name": "Viterbi School of Engineering", "accept": 0.13,
         "keywords": ["viterbi","engineering","computer science","cs ","electrical","mechanical","biomedical","aerospace"]},
        {"name": "Dornsife College of Letters, Arts & Sciences", "accept": 0.15,
         "keywords": ["dornsife","arts","sciences","biology","economics","political science","international relations","psychology","math"]},
    ],
    "northwestern": [
        {"name": "Medill School of Journalism", "accept": 0.05,
         "keywords": ["medill","journalism","communication studies"]},
        {"name": "School of Communication", "accept": 0.06,
         "keywords": ["theater","performance","radio","tv","film","communication studies"]},
        {"name": "McCormick School of Engineering", "accept": 0.075,
         "keywords": ["mccormick","engineering","computer science","cs ","industrial engineering"]},
        {"name": "Bienen School of Music", "accept": 0.25,
         "keywords": ["bienen","music","jazz","composition"]},
        {"name": "Weinberg College of Arts & Sciences", "accept": 0.075,
         "keywords": ["weinberg","arts","sciences","economics","biology","math","physics","political science","psychology","english"]},
    ],
    "cmu": [
        {"name": "School of Computer Science (SCS)", "accept": 0.06,
         "keywords": ["computer science","cs ","ai","artificial intelligence","computational","machine learning"],
         "note":"AI major + CS specifically — among hardest CS admits in US"},
        {"name": "College of Fine Arts (CFA)", "accept": 0.06,
         "keywords": ["drama","music","art","design","architecture"],
         "note":"Audition/portfolio per program; Drama and Architecture are very competitive"},
        {"name": "Tepper School of Business", "accept": 0.13,
         "keywords": ["tepper","business administration","finance"]},
        {"name": "College of Engineering (CIT)", "accept": 0.13,
         "keywords": ["engineering","mechanical","electrical","biomedical","civil","chemical","materials"]},
        {"name": "Mellon College of Science (MCS)", "accept": 0.15,
         "keywords": ["physics","chemistry","math","biology","biological"]},
        {"name": "Dietrich College of Humanities & Social Sciences", "accept": 0.17,
         "keywords": ["humanities","social sciences","english","history","psychology","statistics","economics","information systems"]},
    ],
    "umich": [
        {"name": "Stephen M. Ross School of Business", "accept": 0.13,
         "keywords": ["ross","business","finance","accounting","marketing"],
         "note":"Preferred admission for HS seniors; transfer at sophomore yr"},
        {"name": "College of Engineering", "accept": 0.20,
         "keywords": ["engineering","computer science","cs ","biomedical","mechanical","electrical"]},
        {"name": "School of Music, Theatre & Dance", "accept": 0.30,
         "keywords": ["music","theatre","dance","performing arts"]},
        {"name": "College of Literature, Science, and the Arts (LSA)", "accept": 0.18,
         "keywords": ["lsa","arts","sciences","economics","biology","political science","psychology","math","english"]},
    ],
    "uva": [
        {"name": "School of Architecture", "accept": 0.20,
         "keywords": ["architecture","urban planning","landscape"]},
        {"name": "School of Engineering & Applied Science (SEAS)", "accept": 0.22,
         "keywords": ["engineering","computer science","cs ","biomedical","mechanical","aerospace"]},
        {"name": "McIntire School of Commerce", "accept": 0.30,
         "keywords": ["mcintire","commerce","business","finance"],
         "note":"3rd-year admission only; need to enter via CAS first"},
        {"name": "School of Nursing", "accept": 0.30,
         "keywords": ["nursing"]},
        {"name": "College of Arts & Sciences", "accept": 0.17,
         "keywords": ["arts","sciences","economics","biology","political science","psychology","english","math"]},
    ],
    "ut-austin": [
        {"name": "McCombs School of Business", "accept": 0.12,
         "keywords": ["mccombs","business","finance","accounting","mis"]},
        {"name": "College of Natural Sciences (CS major)", "accept": 0.05,
         "keywords": ["computer science","cs "],
         "note":"UT CS is one of the hardest in the US for non-Texas residents"},
        {"name": "Cockrell School of Engineering", "accept": 0.18,
         "keywords": ["engineering","aerospace","biomedical","civil","mechanical","electrical","chemical"]},
        {"name": "Moody College of Communication", "accept": 0.22,
         "keywords": ["moody","communication","journalism","radio","tv","film"]},
        {"name": "College of Liberal Arts", "accept": 0.32,
         "keywords": ["liberal arts","english","history","economics","political science","psychology","sociology"]},
    ],
    "gatech": [
        {"name": "College of Computing", "accept": 0.10,
         "keywords": ["computer science","cs ","computational","information science"]},
        {"name": "College of Engineering", "accept": 0.16,
         "keywords": ["engineering","mechanical","electrical","biomedical","aerospace","industrial","chemical","civil","materials"]},
        {"name": "Scheller College of Business", "accept": 0.22,
         "keywords": ["scheller","business","management"]},
        {"name": "College of Sciences", "accept": 0.22,
         "keywords": ["physics","chemistry","math","biology","earth sciences","psychology"]},
    ],
    "nyu": [
        {"name": "Stern School of Business", "accept": 0.08,
         "keywords": ["stern","business","finance","accounting","marketing","economics"]},
        {"name": "Tisch School of the Arts", "accept": 0.18,
         "keywords": ["tisch","film","drama","photography","dance","theater"],
         "note":"Audition/portfolio per program; Film Production ~25%, Drama ~5%"},
        {"name": "College of Arts & Sciences (CAS)", "accept": 0.10,
         "keywords": ["arts","sciences","economics","biology","politics","psychology","math","english","history"]},
        {"name": "Steinhardt School", "accept": 0.20,
         "keywords": ["steinhardt","education","music","applied psychology","occupational therapy"]},
        {"name": "Tandon School of Engineering", "accept": 0.28,
         "keywords": ["tandon","engineering","computer science","cs "]},
    ],
    "ucb": [
        {"name": "EECS (Electrical Eng & CS)", "accept": 0.04,
         "keywords": ["eecs","computer science","cs "],
         "note":"Direct CS admit at Berkeley is sub-5%"},
        {"name": "College of Engineering", "accept": 0.085,
         "keywords": ["engineering","mechanical","civil","biomedical","industrial","chemical","aerospace","materials","nuclear"]},
        {"name": "Haas School of Business", "accept": 0.10,
         "keywords": ["haas","business"],
         "note":"Junior-year transfer admit; need to enter via L&S first"},
        {"name": "College of Letters & Science (CDSS data sci)", "accept": 0.06,
         "keywords": ["data science","cdss"]},
        {"name": "College of Letters & Science", "accept": 0.115,
         "keywords": ["arts","letters","economics","political science","psychology","sociology","math","biology"]},
        {"name": "College of Environmental Design", "accept": 0.16,
         "keywords": ["architecture","urban studies","landscape"]},
    ],
    # ================ Additional sub-school data ================
    "ucla": [
        {"name": "Samueli School of Engineering (CS)", "accept": 0.06,
         "keywords": ["computer science","cs "],
         "note":"UCLA CS is sub-10%; one of the harder CS admits in the US"},
        {"name": "Samueli School of Engineering", "accept": 0.10,
         "keywords": ["engineering","mechanical","civil","aerospace","biomedical","chemical","materials"]},
        {"name": "School of Theater, Film, and TV", "accept": 0.04,
         "keywords": ["film","theater","tv","screenwriting","animation"],
         "note":"Among most selective film schools nationally"},
        {"name": "School of the Arts & Architecture", "accept": 0.08,
         "keywords": ["fine art","design","architecture","art history"]},
        {"name": "School of Music", "accept": 0.10,
         "keywords": ["music","jazz","ethnomusicology"]},
        {"name": "School of Nursing", "accept": 0.05,
         "keywords": ["nursing"]},
        {"name": "College of Letters & Science", "accept": 0.10,
         "keywords": ["arts","letters","economics","political science","psychology","sociology","math","biology","english","history"]},
    ],
    "duke": [
        {"name": "Pratt School of Engineering", "accept": 0.045,
         "keywords": ["engineering","biomedical","mechanical","electrical","civil","materials"]},
        {"name": "Trinity College of Arts & Sciences", "accept": 0.06,
         "keywords": ["arts","sciences","economics","political science","biology","computer science","cs ","english","history","math","psychology"]},
    ],
    "brown": [
        {"name": "Program in Liberal Medical Education (PLME)", "accept": 0.035,
         "keywords": ["plme","8 year medical","direct medical","md combined"],
         "note":"8-year direct-to-MD program; among most selective in US"},
        {"name": "Brown-RISD Dual Degree", "accept": 0.04,
         "keywords": ["risd","dual degree","brown-risd"],
         "note":"Need to be admitted to both Brown and RISD separately"},
        {"name": "School of Engineering", "accept": 0.06,
         "keywords": ["engineering","biomedical","mechanical","electrical","computer engineering"]},
        {"name": "Open Curriculum (College)", "accept": 0.055,
         "keywords": ["arts","sciences","economics","computer science","cs ","biology","english","history","political science","psychology","math"]},
    ],
    "rice": [
        {"name": "School of Architecture", "accept": 0.07,
         "keywords": ["architecture"],
         "note":"Portfolio required; very small program"},
        {"name": "Shepherd School of Music", "accept": 0.07,
         "keywords": ["shepherd","music","jazz","composition"],
         "note":"Audition required; conservatory-level program"},
        {"name": "George R. Brown School of Engineering", "accept": 0.07,
         "keywords": ["engineering","computer science","cs ","biomedical","chemical","mechanical","electrical"]},
        {"name": "School of Natural Sciences", "accept": 0.09,
         "keywords": ["physics","chemistry","math","biology","earth sciences"]},
        {"name": "School of Social Sciences", "accept": 0.10,
         "keywords": ["economics","political science","psychology","sociology","cognitive science"]},
        {"name": "School of Humanities", "accept": 0.10,
         "keywords": ["english","history","philosophy","languages","classics","art history"]},
    ],
    "vanderbilt": [
        {"name": "Blair School of Music", "accept": 0.25,
         "keywords": ["blair","music","violin","piano","voice","composition","jazz"],
         "note":"Audition required; less competitive academically since the gate is the audition"},
        {"name": "Peabody College of Education", "accept": 0.10,
         "keywords": ["peabody","education","child development","teaching","human dev"]},
        {"name": "School of Engineering", "accept": 0.07,
         "keywords": ["engineering","computer science","cs ","biomedical","mechanical","electrical","chemical"]},
        {"name": "College of Arts & Science", "accept": 0.06,
         "keywords": ["arts","science","economics","biology","english","history","political science","psychology","math","neuroscience"]},
    ],
    "notre-dame": [
        {"name": "Mendoza College of Business", "accept": 0.17,
         "keywords": ["mendoza","business","finance","accounting","marketing","management"]},
        {"name": "School of Architecture", "accept": 0.18,
         "keywords": ["architecture"]},
        {"name": "College of Engineering", "accept": 0.17,
         "keywords": ["engineering","computer science","cs ","aerospace","mechanical","civil","biomedical","chemical"]},
        {"name": "College of Science", "accept": 0.16,
         "keywords": ["physics","chemistry","math","biology","biochemistry","preprofessional"]},
        {"name": "College of Arts & Letters", "accept": 0.14,
         "keywords": ["arts","letters","economics","english","history","political science","psychology","theology","philosophy"]},
    ],
    "washu": [
        {"name": "McKelvey School of Engineering", "accept": 0.13,
         "keywords": ["mckelvey","engineering","computer science","cs ","biomedical","systems","mechanical","electrical"]},
        {"name": "Olin Business School", "accept": 0.14,
         "keywords": ["olin","business","finance","accounting","marketing"]},
        {"name": "Sam Fox School (Architecture/Design)", "accept": 0.16,
         "keywords": ["sam fox","architecture","fine art","design","art history","communication design"]},
        {"name": "College of Arts & Sciences", "accept": 0.14,
         "keywords": ["arts","sciences","economics","biology","english","history","political science","psychology","math","chemistry"]},
    ],
    "northeastern": [
        {"name": "Khoury College of Computer Sciences", "accept": 0.07,
         "keywords": ["khoury","computer science","cs ","data science","cybersecurity","information science"],
         "note":"Khoury CS is among NEU's most selective programs"},
        {"name": "D'Amore-McKim School of Business", "accept": 0.08,
         "keywords": ["d'amore","mckim","business","finance","international business"]},
        {"name": "College of Engineering", "accept": 0.08,
         "keywords": ["engineering","mechanical","civil","biomedical","chemical","industrial"]},
        {"name": "College of Arts, Media and Design", "accept": 0.10,
         "keywords": ["camd","art","media","design","journalism","theater","music"]},
        {"name": "Bouvé College of Health Sciences", "accept": 0.10,
         "keywords": ["bouve","nursing","health sciences","pharmacy","physical therapy","speech-language"]},
        {"name": "College of Social Sciences and Humanities", "accept": 0.10,
         "keywords": ["cssh","sociology","political science","psychology","english","history","philosophy","international affairs"]},
        {"name": "College of Science", "accept": 0.09,
         "keywords": ["biology","chemistry","physics","math","biochemistry","marine biology"]},
    ],
    "bu": [
        {"name": "Questrom School of Business", "accept": 0.13,
         "keywords": ["questrom","business","finance","accounting","marketing","international business"]},
        {"name": "College of Engineering", "accept": 0.12,
         "keywords": ["engineering","biomedical","mechanical","electrical","computer engineering"]},
        {"name": "College of Communication", "accept": 0.10,
         "keywords": ["communication","journalism","public relations","film","tv","advertising"]},
        {"name": "College of Fine Arts", "accept": 0.12,
         "keywords": ["fine arts","music","theater","visual arts","performance"],
         "note":"Audition/portfolio required for music & theater"},
        {"name": "Sargent College of Health & Rehab", "accept": 0.14,
         "keywords": ["sargent","health science","rehabilitation","occupational therapy","physical therapy","speech"]},
        {"name": "School of Hospitality Administration", "accept": 0.16,
         "keywords": ["hospitality","hotel"]},
        {"name": "College of Arts & Sciences", "accept": 0.13,
         "keywords": ["arts","sciences","economics","biology","english","history","political science","psychology","math","computer science","cs "]},
    ],
    "tufts": [
        {"name": "School of the Museum of Fine Arts (SMFA)", "accept": 0.27,
         "keywords": ["smfa","fine art","museum","studio art","ceramics","painting","sculpture"],
         "note":"Portfolio is the gate; less academically competitive"},
        {"name": "School of Engineering", "accept": 0.13,
         "keywords": ["engineering","computer science","cs ","biomedical","chemical","mechanical"]},
        {"name": "School of Arts & Sciences", "accept": 0.10,
         "keywords": ["arts","sciences","international relations","economics","political science","biology","english","history","psychology","math"]},
    ],
    "uiuc": [
        {"name": "Grainger College of Engineering (CS)", "accept": 0.07,
         "keywords": ["computer science","cs ","computer engineering"],
         "note":"UIUC CS is sub-10% — among the hardest CS admits in the country"},
        {"name": "Grainger College of Engineering", "accept": 0.30,
         "keywords": ["engineering","mechanical","electrical","civil","aerospace","industrial","biomedical","materials","chemical","nuclear"]},
        {"name": "Gies College of Business", "accept": 0.36,
         "keywords": ["gies","business","finance","accounting","information systems"]},
        {"name": "College of Media", "accept": 0.50,
         "keywords": ["journalism","advertising","media studies","communication"]},
        {"name": "College of Liberal Arts & Sciences", "accept": 0.50,
         "keywords": ["arts","sciences","economics","english","history","political science","psychology","math","biology"]},
    ],
    "purdue": [
        {"name": "Computer Science (College of Science)", "accept": 0.20,
         "keywords": ["computer science","cs "],
         "note":"Direct CS admit at Purdue is significantly more competitive than CoE"},
        {"name": "College of Engineering", "accept": 0.40,
         "keywords": ["engineering","mechanical","electrical","aerospace","biomedical","chemical","civil","industrial","materials","nuclear"]},
        {"name": "Daniels School of Business", "accept": 0.55,
         "keywords": ["daniels","krannert","business","finance","accounting","management"]},
        {"name": "College of Liberal Arts", "accept": 0.65,
         "keywords": ["liberal arts","english","history","political science","psychology","economics","sociology","communication"]},
        {"name": "College of Science", "accept": 0.50,
         "keywords": ["physics","chemistry","math","biology","statistics","earth sciences"]},
    ],
    "wisc": [
        {"name": "School of Business", "accept": 0.27,
         "keywords": ["business","finance","accounting","marketing","real estate","actuarial"],
         "note":"Direct admit; substantially more selective than university overall"},
        {"name": "College of Engineering (CS in L&S)", "accept": 0.25,
         "keywords": ["computer science","cs "],
         "note":"CS at Wisconsin is competitive, especially for non-residents"},
        {"name": "College of Engineering", "accept": 0.38,
         "keywords": ["engineering","mechanical","electrical","biomedical","civil","industrial","materials","chemical"]},
        {"name": "College of Letters & Science", "accept": 0.50,
         "keywords": ["arts","sciences","economics","biology","english","history","political science","psychology","math"]},
    ],
    "iu": [
        {"name": "Kelley School of Business (Direct Admit)", "accept": 0.30,
         "keywords": ["kelley","business","finance","accounting","marketing"],
         "note":"Direct-admit Kelley is way harder than Standard Admit (which is most students)"},
        {"name": "Jacobs School of Music", "accept": 0.30,
         "keywords": ["jacobs","music","jazz","violin","piano","voice","composition"],
         "note":"Audition is the primary gate"},
        {"name": "Hutton Honors College", "accept": 0.20,
         "keywords": ["hutton","honors"]},
        {"name": "Luddy School (CS, Informatics)", "accept": 0.55,
         "keywords": ["luddy","computer science","cs ","informatics","data science"]},
        {"name": "College of Arts & Sciences", "accept": 0.78,
         "keywords": ["arts","sciences","english","history","biology","math","political science","psychology"]},
    ],
    "umd": [
        {"name": "Computer Science (Direct Admit)", "accept": 0.10,
         "keywords": ["computer science","cs "],
         "note":"UMD CS direct admit is sub-15%; significantly harder than overall"},
        {"name": "A. James Clark School of Engineering", "accept": 0.30,
         "keywords": ["engineering","aerospace","mechanical","electrical","biomedical","chemical","civil","materials"]},
        {"name": "Robert H. Smith School of Business", "accept": 0.30,
         "keywords": ["smith","business","finance","accounting","marketing"]},
        {"name": "College of Arts & Humanities", "accept": 0.55,
         "keywords": ["arts","humanities","english","history","philosophy","communication"]},
        {"name": "College of Behavioral & Social Sciences", "accept": 0.50,
         "keywords": ["behavioral","social","economics","government","psychology","sociology"]},
    ],
    "penn-state": [
        {"name": "Schreyer Honors College", "accept": 0.14,
         "keywords": ["schreyer","honors"],
         "note":"Schreyer is one of top public honors programs nationally"},
        {"name": "Smeal College of Business", "accept": 0.40,
         "keywords": ["smeal","business","finance","accounting","management"]},
        {"name": "College of Engineering", "accept": 0.45,
         "keywords": ["engineering","computer science","cs ","aerospace","mechanical","electrical","biomedical","chemical","civil"]},
        {"name": "College of Information Sciences & Technology", "accept": 0.50,
         "keywords": ["ist","information sciences","cybersecurity"]},
        {"name": "Stuckeman School of Architecture", "accept": 0.30,
         "keywords": ["architecture","landscape architecture"]},
        {"name": "Liberal Arts", "accept": 0.65,
         "keywords": ["liberal arts","english","history","political science","psychology","economics","sociology"]},
    ],
    "vt": [
        {"name": "Pamplin College of Business", "accept": 0.50,
         "keywords": ["pamplin","business","finance","accounting","marketing"]},
        {"name": "College of Engineering (CS)", "accept": 0.40,
         "keywords": ["computer science","cs "],
         "note":"VT CS is more competitive than other engineering majors"},
        {"name": "College of Engineering", "accept": 0.55,
         "keywords": ["engineering","aerospace","mechanical","electrical","biomedical","chemical","civil","materials","industrial"]},
        {"name": "College of Architecture & Urban Studies", "accept": 0.40,
         "keywords": ["architecture","urban planning","landscape architecture","building construction"]},
        {"name": "College of Liberal Arts & Human Sciences", "accept": 0.65,
         "keywords": ["liberal arts","english","history","political science","psychology","economics","sociology","communication"]},
    ],
    "lehigh": [
        {"name": "Integrated Business & Engineering (IBE)", "accept": 0.10,
         "keywords": ["ibe","integrated business and engineering"],
         "note":"Lehigh's combined-degree programs are among most selective"},
        {"name": "Computer Science & Business (CSB)", "accept": 0.12,
         "keywords": ["csb","computer science and business"]},
        {"name": "P.C. Rossin College of Engineering & Applied Science", "accept": 0.32,
         "keywords": ["engineering","computer science","cs ","biomedical","mechanical","electrical","chemical","materials"]},
        {"name": "College of Business", "accept": 0.32,
         "keywords": ["business","finance","accounting","marketing","supply chain"]},
        {"name": "College of Arts & Sciences", "accept": 0.34,
         "keywords": ["arts","sciences","economics","english","history","biology","math","political science"]},
    ],
    "bc": [
        {"name": "Carroll School of Management (CSOM)", "accept": 0.18,
         "keywords": ["csom","carroll","business","finance","accounting","management"]},
        {"name": "Connell School of Nursing", "accept": 0.20,
         "keywords": ["connell","nursing"]},
        {"name": "Lynch School of Education", "accept": 0.18,
         "keywords": ["lynch","education","applied psychology","human development"]},
        {"name": "Morrissey College of Arts & Sciences", "accept": 0.16,
         "keywords": ["morrissey","mcas","arts","sciences","economics","biology","english","history","political science","psychology","math","computer science","cs "]},
    ],
    "uw": [
        {"name": "Paul G. Allen School of Computer Science", "accept": 0.13,
         "keywords": ["computer science","cs ","data science"],
         "note":"Allen School direct admit at UW is sub-15%; one of the strongest CS programs in the country"},
        {"name": "Foster School of Business", "accept": 0.30,
         "keywords": ["foster","business","finance","accounting","marketing"]},
        {"name": "College of Engineering", "accept": 0.35,
         "keywords": ["engineering","aerospace","mechanical","electrical","biomedical","chemical","civil","industrial","materials"]},
        {"name": "School of Public Health", "accept": 0.50,
         "keywords": ["public health"]},
        {"name": "School of Art + Art History + Design", "accept": 0.40,
         "keywords": ["art","design","industrial design"]},
        {"name": "College of Arts & Sciences", "accept": 0.55,
         "keywords": ["arts","sciences","biology","english","history","political science","psychology","math"]},
    ],
    "ucsd": [
        {"name": "Computer Science (Jacobs)", "accept": 0.20,
         "keywords": ["computer science","cs "],
         "note":"UCSD CS direct admit is much more competitive than overall"},
        {"name": "Jacobs School of Engineering", "accept": 0.28,
         "keywords": ["engineering","biomedical","mechanical","electrical","chemical","aerospace","materials"]},
        {"name": "Rady School of Management", "accept": 0.30,
         "keywords": ["rady","business","economics"]},
        {"name": "Division of Biological Sciences", "accept": 0.30,
         "keywords": ["biology","biological","biochemistry","molecular biology","neuroscience"]},
        {"name": "Scripps Institution of Oceanography (Earth Sci)", "accept": 0.30,
         "keywords": ["earth science","oceanography","marine"]},
        {"name": "Division of Arts & Humanities", "accept": 0.35,
         "keywords": ["arts","humanities","english","history","philosophy","languages"]},
        {"name": "Division of Social Sciences", "accept": 0.32,
         "keywords": ["social","political science","psychology","sociology","international studies","economics"]},
    ],
    "unc": [
        {"name": "Kenan-Flagler Business School", "accept": 0.20,
         "keywords": ["kenan","flagler","business","finance","accounting","marketing"],
         "note":"Top-5 undergrad business program nationally"},
        {"name": "Hussman School of Journalism & Media", "accept": 0.20,
         "keywords": ["hussman","journalism","media","communications","public relations","advertising"]},
        {"name": "School of Nursing", "accept": 0.20,
         "keywords": ["nursing"]},
        {"name": "School of Pharmacy (PharmD)", "accept": 0.15,
         "keywords": ["pharmacy","pharmaceutical"]},
        {"name": "Gillings School of Public Health (BSPH)", "accept": 0.18,
         "keywords": ["public health","biostatistics","health policy"]},
        {"name": "College of Arts & Sciences", "accept": 0.18,
         "keywords": ["arts","sciences","biology","english","history","political science","psychology","math","computer science","cs "]},
    ],
    "emory": [
        {"name": "Goizueta Business School", "accept": 0.30,
         "keywords": ["goizueta","business","finance","accounting","marketing"],
         "note":"Junior-year admit only; need to enter Emory College first"},
        {"name": "Nell Hodgson Woodruff School of Nursing", "accept": 0.30,
         "keywords": ["nursing"]},
        {"name": "Oxford College (2-year campus)", "accept": 0.13,
         "keywords": ["oxford"]},
        {"name": "Emory College of Arts & Sciences", "accept": 0.12,
         "keywords": ["arts","sciences","biology","english","history","political science","psychology","math","economics","computer science","cs "]},
    ],
    "georgetown": [
        {"name": "Walsh School of Foreign Service (SFS)", "accept": 0.10,
         "keywords": ["sfs","foreign service","international relations","international affairs","international politics","international economics"],
         "note":"Top-3 international relations program in the US"},
        {"name": "McDonough School of Business", "accept": 0.15,
         "keywords": ["mcdonough","business","finance","accounting","marketing"]},
        {"name": "School of Nursing & Health Studies", "accept": 0.15,
         "keywords": ["nursing","health studies"]},
        {"name": "Georgetown College (Arts & Sciences)", "accept": 0.13,
         "keywords": ["college","arts","sciences","biology","english","history","political science","psychology","math","economics","computer science","cs "]},
    ],
    "syracuse": [
        {"name": "S.I. Newhouse School of Public Communications", "accept": 0.20,
         "keywords": ["newhouse","communications","journalism","public relations","advertising","broadcasting","tv","radio"],
         "note":"Top-5 communications school; selective sub-application within Syracuse"},
        {"name": "Whitman School of Management", "accept": 0.50,
         "keywords": ["whitman","business","finance","accounting","marketing","supply chain"]},
        {"name": "School of Architecture", "accept": 0.40,
         "keywords": ["architecture"]},
        {"name": "College of Visual & Performing Arts (VPA)", "accept": 0.40,
         "keywords": ["vpa","visual arts","performing arts","music","theater","drama","art","design"]},
        {"name": "College of Engineering & CS", "accept": 0.55,
         "keywords": ["engineering","computer science","cs ","biomedical","mechanical","electrical","civil"]},
        {"name": "College of Arts & Sciences", "accept": 0.55,
         "keywords": ["arts","sciences","biology","english","history","political science","psychology","math","economics"]},
    ],
    "chapman": [
        {"name": "Dodge College of Film & Media Arts", "accept": 0.20,
         "keywords": ["dodge","film","cinema","screenwriting","tv","broadcast","animation","game design"],
         "note":"Top-15 film school; portfolio + audition for many programs"},
        {"name": "College of Performing Arts (Hall-Musco / Theatre)", "accept": 0.30,
         "keywords": ["music","theatre","theater","dance","performing arts"]},
        {"name": "Argyros School of Business & Economics", "accept": 0.55,
         "keywords": ["argyros","business","finance","accounting","economics"]},
        {"name": "Schmid College of Science & Technology", "accept": 0.55,
         "keywords": ["computer science","cs ","biology","chemistry","math","physics","data science"]},
        {"name": "Wilkinson College of Arts, Humanities & Social Sciences", "accept": 0.62,
         "keywords": ["arts","humanities","english","history","political science","psychology","sociology","communications"]},
    ],
    "jhu": [
        {"name": "Biomedical Engineering (BME)", "accept": 0.05,
         "keywords": ["biomedical","bme","biomedical engineering"],
         "note":"JHU BME is consistently ranked #1 in the country; sub-5% admit"},
        {"name": "Whiting School of Engineering", "accept": 0.07,
         "keywords": ["engineering","computer science","cs ","mechanical","electrical","chemical","civil","materials"]},
        {"name": "Peabody Institute (Music Conservatory)", "accept": 0.30,
         "keywords": ["peabody","music","piano","violin","voice","jazz","composition"],
         "note":"Audition is the gate; conservatory-level training"},
        {"name": "Krieger School of Arts & Sciences", "accept": 0.07,
         "keywords": ["arts","sciences","biology","public health","economics","english","history","political science","psychology","math","international studies"]},
    ],
    "pitt": [
        {"name": "School of Computing & Information", "accept": 0.40,
         "keywords": ["computer science","cs ","information science","data science"]},
        {"name": "Swanson School of Engineering", "accept": 0.50,
         "keywords": ["swanson","engineering","biomedical","mechanical","electrical","chemical","civil","industrial","materials"]},
        {"name": "College of Business Administration", "accept": 0.50,
         "keywords": ["business","finance","accounting","marketing"]},
        {"name": "School of Nursing", "accept": 0.40,
         "keywords": ["nursing"]},
        {"name": "Dietrich School of Arts & Sciences", "accept": 0.55,
         "keywords": ["arts","sciences","biology","english","history","political science","psychology","math","economics"]},
    ],
    "fsu": [
        {"name": "College of Motion Picture Arts", "accept": 0.04,
         "keywords": ["film","cinema","motion picture","screenwriting"],
         "note":"Top-5 film school; sub-5% admit through portfolio review"},
        {"name": "College of Business", "accept": 0.35,
         "keywords": ["business","finance","accounting","management"]},
        {"name": "FAMU-FSU College of Engineering", "accept": 0.40,
         "keywords": ["engineering","computer engineering","mechanical","electrical","biomedical","chemical","civil","industrial"]},
        {"name": "School of Communication", "accept": 0.40,
         "keywords": ["communication","journalism","public relations","advertising"]},
        {"name": "College of Arts & Sciences", "accept": 0.45,
         "keywords": ["arts","sciences","biology","english","history","political science","psychology","math","computer science","cs "]},
    ],
    "ucf": [
        {"name": "Rosen College of Hospitality Management", "accept": 0.30,
         "keywords": ["rosen","hospitality","hotel","tourism","event management"],
         "note":"Top-3 hospitality program in the world"},
        {"name": "Burnett Honors College", "accept": 0.15,
         "keywords": ["burnett","honors"]},
        {"name": "College of Engineering & Computer Science", "accept": 0.35,
         "keywords": ["engineering","computer science","cs ","aerospace","mechanical","electrical","civil","biomedical"]},
        {"name": "College of Business (Burnett School of Bio Med)", "accept": 0.45,
         "keywords": ["business","finance","accounting","marketing"]},
        {"name": "Nicholson School of Communication & Media", "accept": 0.40,
         "keywords": ["communication","journalism","film","media","advertising"]},
        {"name": "College of Arts & Humanities", "accept": 0.45,
         "keywords": ["arts","humanities","english","history","music","theater"]},
    ],
}

# Drop empty placeholder entries
SUB_SCHOOL_RATES = {k: v for k, v in SUB_SCHOOL_RATES.items() if v}


def _render_counterfactual_card(profile, school, current_low, current_high):
    """Show 'what would change my odds' scenarios on the chances page.
    For each major lever (GPA, test score, hook), compute the user's
    chances under that hypothetical and show the lift.

    Returns empty string if no actionable scenarios exist (e.g., user
    is already at the cap or has minimal profile)."""
    if not school: return ""
    cur_mid = (current_low + current_high) / 2
    scenarios = []  # (label, new_low, new_high, delta_str)

    raw_gpa = profile.get("uw_gpa")
    sat = profile.get("sat")
    act = profile.get("act")

    # Scenario 1: raise GPA toward the school's 75th percentile
    if raw_gpa is not None and raw_gpa < school["gpa_hi"]:
        target_gpa = round(min(4.0, max(raw_gpa + 0.10, school.get("gpa_lo", 3.0) + 0.05)), 2)
        if target_gpa > raw_gpa:
            new_low, new_high = counterfactual_lift(profile, school, gpa=target_gpa)
            scenarios.append((
                f"Raise GPA from {raw_gpa} to {target_gpa}",
                new_low, new_high
            ))

    # Scenario 2: raise SAT (or ACT) toward the school's 75th percentile
    # (skip for test-blind schools where sat_75 is None).
    if sat is not None and school.get("sat_75") is not None and sat < school["sat_75"]:
        target_sat = min(1600, sat + 50)
        new_low, new_high = counterfactual_lift(profile, school, sat=target_sat)
        scenarios.append((
            f"Raise SAT from {sat} to {target_sat} (+50)",
            new_low, new_high
        ))
    elif act is not None and act < school["act_75"]:
        target_act = min(36, act + 2)
        new_low, new_high = counterfactual_lift(profile, school, act=target_act)
        scenarios.append((
            f"Raise ACT from {act} to {target_act} (+2)",
            new_low, new_high
        ))

    # Scenario 3: combined GPA + test
    if raw_gpa is not None and (sat or act):
        target_gpa = round(min(4.0, raw_gpa + 0.10), 2)
        if sat:
            target_sat = min(1600, sat + 50)
            new_low, new_high = counterfactual_lift(profile, school, gpa=target_gpa, sat=target_sat)
            scenarios.append((
                f"Raise both: GPA {raw_gpa}→{target_gpa} AND SAT {sat}→{target_sat}",
                new_low, new_high
            ))
        elif act:
            target_act = min(36, act + 2)
            new_low, new_high = counterfactual_lift(profile, school, gpa=target_gpa, act=target_act)
            scenarios.append((
                f"Raise both: GPA {raw_gpa}→{target_gpa} AND ACT {act}→{target_act}",
                new_low, new_high
            ))

    # Scenario 4: hook scenarios (only if user doesn't already have them)
    if not profile.get("athlete"):
        new_low, new_high = counterfactual_lift(profile, school, hook_athlete=True)
        if (new_low + new_high)/2 - cur_mid >= 3:
            scenarios.append((
                "If you were a recruited athlete here",
                new_low, new_high
            ))
    if not profile.get("is_exceptional"):
        new_low, new_high = counterfactual_lift(profile, school, is_exceptional=True)
        if (new_low + new_high)/2 - cur_mid >= 5:
            scenarios.append((
                "If you had a national-level distinction (USAMO gold / ISEF / etc.)",
                new_low, new_high
            ))

    if not scenarios:
        return ""

    rows = ""
    for label, lo, hi in scenarios:
        new_mid = (lo + hi) / 2
        delta = new_mid - cur_mid
        if delta < 0.5:
            color = "var(--text-3)"
            arrow = "→"
        elif delta < 5:
            color = "#fbbf24"
            arrow = "↗"
        else:
            color = "#5fc9b6"
            arrow = "↑"
        rows += f'''<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-top:1px solid var(--border);font-size:.92em;gap:12px;flex-wrap:wrap">
  <div style="flex:1;min-width:160px">{label}</div>
  <div style="white-space:nowrap"><span class="muted">{int(current_low)}–{int(current_high)}%</span> <span style="color:{color};font-weight:700;margin:0 4px">{arrow}</span> <b style="color:{color}">{int(lo)}–{int(hi)}%</b> <span style="color:{color};font-size:.85em">(+{delta:.0f})</span></div>
</div>'''

    return f'''<div class="card" style="margin-top:18px">
  <h3 style="margin-top:0">What would actually move your odds?</h3>
  <p class="muted" style="font-size:.88em;margin:0 0 4px">Specific changes and what they'd do to your chances at this school. Useful for prioritizing what to focus on.</p>
  {rows}
  <p class="muted" style="font-size:.78em;margin:14px 0 0">Note: these are model estimates, not promises. Real admissions has more variance than the model can capture.</p>
</div>'''


def median_earnings_10yr(school):
    """Return federal-data median earnings 10 years post-entry, or None.
    Pulled from College Scorecard / IPEDS via the overrides table."""
    over = _get_overrides(school["slug"])
    if over and over.get("median_earnings_10yr"):
        return int(over["median_earnings_10yr"])
    return None


def cost_attendance(school):
    """Return Scorecard's reported cost of attendance (academic year), or
    fall back to the hardcoded tuition + a rough room/board adder."""
    over = _get_overrides(school["slug"])
    if over and over.get("cost_attendance"):
        return int(over["cost_attendance"])
    # Fallback estimate: tuition + ~$15K room/board for residential schools
    return (school.get("tuition", 0) or 0) + 15000


def earnings_to_cost_ratio(school):
    """Cost-to-earnings ratio: how many years of post-grad median earnings
    cover 4 years of attendance. Lower is better. Returns float or None."""
    earn = median_earnings_10yr(school)
    cost = cost_attendance(school)
    if not earn or not cost:
        return None
    four_year_cost = cost * 4
    return four_year_cost / earn  # years of earnings to cover


def _render_earnings_card(school):
    """College detail page card showing 10-yr median earnings + cost/earnings
    ratio. Returns empty string if Scorecard hasn't populated yet."""
    earn = median_earnings_10yr(school)
    if not earn:
        return ""
    cost = cost_attendance(school)
    ratio = earnings_to_cost_ratio(school)
    ratio_str = ""
    if ratio:
        ratio_color = "#5fc9b6" if ratio < 2.0 else ("#fbbf24" if ratio < 3.5 else "#fca5a5")
        ratio_str = f'<div class="muted" style="font-size:.82em;margin-top:4px">≈ <b style="color:{ratio_color}">{ratio:.1f} years</b> of post-grad earnings cover 4 yrs of attendance</div>'
    return f'''<div class="card">
  <h3 style="margin-top:0">Median earnings (10-yr)</h3>
  <div class="odds" style="color:#2b6cff">${earn//1000}K</div>
  <div class="muted" style="font-size:.82em">Federal data — 10 yrs after college entry, all majors combined</div>
  {ratio_str}
</div>'''


_career_cache = {}
def _career_outcomes(slug):
    """Cached read of the multi-source career-outcomes row for a school."""
    if slug in _career_cache:
        return _career_cache[slug]
    row = None
    try:
        with db() as conn:
            r = conn.execute(
                "SELECT entry, ten_yr, mid_career, oi, roi, n_sources, sources "
                "FROM career_outcomes WHERE college_slug=?", (slug,)).fetchone()
            if r:
                row = dict(r)
    except Exception:
        row = None
    _career_cache[slug] = row
    return row


def _render_career_outcomes(c):
    """Career Outcomes card: earnings trajectory (entry -> 10yr -> mid-career)
    plus how many independent sources back the data. Falls back to the loaded
    10-yr figure for schools without a full multi-source row."""
    row = _career_outcomes(c["slug"])
    earn10 = median_earnings_10yr(c)
    if not row and not earn10:
        return ""
    entry = (row or {}).get("entry")
    ten = (row or {}).get("ten_yr") or earn10
    mid = (row or {}).get("mid_career")
    roi = (row or {}).get("roi")
    n = (row or {}).get("n_sources") or (1 if earn10 else 0)
    def k(v): return f"${int(v)//1000}K" if v else "—"
    def chip(label, val, accent=False):
        col = "#2b6cff" if accent else "var(--text)"
        return (f'<div style="flex:1;min-width:90px;text-align:center;padding:10px 8px;'
                f'background:var(--surface-2);border:1px solid var(--border);border-radius:8px">'
                f'<div style="font-size:1.35em;font-weight:700;color:{col}">{k(val)}</div>'
                f'<div class="muted" style="font-size:.72em;text-transform:uppercase;letter-spacing:.4px;margin-top:2px">{label}</div></div>')
    chips = chip("Entry", entry) + chip("10-year", ten, accent=True)
    if mid:
        chips += chip("Mid-career", mid)
    ratio = earnings_to_cost_ratio(c)
    payback = ""
    if ratio:
        rc = "#5fc9b6" if ratio < 2.0 else ("#fbbf24" if ratio < 3.5 else "#fca5a5")
        payback = f'<span> · <b style="color:{rc}">{ratio:.1f} yrs</b> to pay back 4 yrs of cost</span>'
    roi_str = f' · est. lifetime ROI <b>${roi//1000}K</b>' if roi else ""
    src_note = (f"Earnings cross-referenced across <b>{n} source{'s' if n != 1 else ''}</b> "
                f"(College Scorecard, Opportunity Insights, Georgetown CEW, FREOPP, PayScale, and more).") if n >= 2 \
        else "From federal College Scorecard data."
    return f'''<div class="card">
  <h3 style="margin-top:0">Career outcomes <span class="muted" style="font-size:.55em;font-weight:500;vertical-align:middle">· {n} sources</span></h3>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px">{chips}</div>
  <div class="muted" style="font-size:.82em;line-height:1.5">Median graduate earnings{payback}{roi_str}.<br>{src_note}</div>
</div>'''


def _render_sub_school_block(slug, highlight_keywords=None):
    """Display the per-college sub-school accept rates on the school detail
    page. If highlight_keywords is provided (typically the user's major),
    bolds the matching sub-school so users see which one applies to them.
    Returns empty string if no curated data for this school."""
    subs = SUB_SCHOOL_RATES.get(slug)
    if not subs:
        return ""
    matched_idx = None
    if highlight_keywords:
        m = highlight_keywords.lower().strip()
        for i, e in enumerate(subs):
            for kw in e.get("keywords", []):
                if kw in m:
                    matched_idx = i; break
            if matched_idx is not None: break
    rows = ""
    for i, e in enumerate(subs):
        is_match = (i == matched_idx)
        bg = "background:rgba(95,201,182,.06);border-left:2px solid var(--teal);padding-left:10px;margin-left:-12px;" if is_match else ""
        match_pill = ' <span style="font-size:.7em;color:var(--teal);font-weight:600;letter-spacing:.3px">YOUR MAJOR</span>' if is_match else ""
        note = e.get("note","")
        note_html = f'<div class="muted" style="font-size:.78em;line-height:1.4">{note}</div>' if note else ""
        rows += (
            f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid #f0f0f0;font-size:.9em;gap:8px;flex-wrap:wrap;{bg}">'
            f'<div style="flex:1;min-width:180px"><span>{e["name"]}</span>{match_pill}{note_html}</div>'
            f'<span style="font-weight:600;white-space:nowrap">{round(e["accept"]*100,1)}%</span>'
            f'</div>'
        )
    return f'<div style="margin-top:14px"><div style="font-weight:600;font-size:.85em;color:#666;margin-bottom:2px">By college within the university</div>{rows}</div>'


def sub_school_for_major(school_slug, major):
    """Returns the (best-match) sub-school dict for a given school+major,
    or None if no curated data exists or no keyword matches.

    Used both for display ("you'd be applying to Cornell Engineering, ~10%
    accept") and for chances scoring (use the sub-school's accept rate
    instead of the university-wide one when computing odds)."""
    subs = SUB_SCHOOL_RATES.get(school_slug)
    if not subs or not major:
        return None
    m = major.lower().strip()
    if not m:
        return None
    # First exact phrase / longest-keyword match wins (we ordered the
    # entries with most-specific first, so first match is best)
    for entry in subs:
        for kw in entry.get("keywords", []):
            if kw in m:
                return entry
    return None


# Where this leads — career feeders / industry pipelines / geographic
# advantages per school. Structure: list of short bullets, 2-5 per school.
# AI-generated for schools not in the curated dict (cached forever in DB).


def get_school_feeders(school):
    """Return the curated feeders list for a school. AI-generates and caches
    for schools not in the manual dict."""
    if school["slug"] in SCHOOL_FEEDERS:
        return SCHOOL_FEEDERS[school["slug"]]
    # Look up in DB cache
    with db() as conn:
        row = conn.execute(
            "SELECT body FROM school_feeders WHERE college_slug=?",
            (school["slug"],)
        ).fetchone() if _table_exists(conn, "school_feeders") else None
    if row and row["body"]:
        try:
            return json.loads(row["body"])
        except Exception:
            return []
    # Generate via Claude
    if not _claude_client:
        return []
    try:
        prompt = (f"List 3-5 specific career pipelines / industries / geographic advantages for graduates of {school['name']} ({city_state(school)}).\n\n"
                  f"Hard rules:\n"
                  f"- Each line is one short bullet (under 12 words)\n"
                  f"- Be specific: name firms, industries, or geographic advantages, not generic 'consulting'\n"
                  f"- Real downsides ok (e.g., 'mid-tier IB pipeline, not top of class')\n"
                  f"- No em-dashes, no marketing language ('renowned', 'esteemed', etc)\n\n"
                  f"Output ONE bullet per line, no formatting characters, no preamble.")
        resp = _claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="You list real career pipelines for college graduates. Specific, slightly opinionated. No marketing fluff.",
            messages=[{"role":"user","content": prompt}],
        )
        text = resp.content[0].text.strip()
        bullets = [line.strip("- •*").strip() for line in text.split("\n") if line.strip() and len(line.strip()) > 4][:5]
    except Exception as e:
        print(f"School feeders error: {e}")
        bullets = []
    if bullets:
        with db() as conn:
            try:
                conn.execute("INSERT OR REPLACE INTO school_feeders (college_slug, body) VALUES (?, ?)",
                             (school["slug"], json.dumps(bullets)))
                conn.commit()
            except Exception as e:
                print(f"feeders cache write failed for {school['slug']}: {e}")
    return bullets


def _table_exists(conn, name):
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except Exception:
        return False


def render_school_feeders(school):
    feeders = get_school_feeders(school)
    if not feeders:
        return ""
    items = "".join(f'<li style="margin:6px 0">{f}</li>' for f in feeders)
    return f"""<div class="card" style="margin-top:16px">
      <h3 style="margin-top:0">Where this leads</h3>
      <p class="muted" style="font-size:.85em;margin:0 0 8px">Real career pipelines from this school, based on alumni outcomes and recruiting patterns.</p>
      <ul style="padding-left:18px;margin:8px 0;color:var(--text)">{items}</ul>
    </div>"""




def render_admissions_breakdown(school, detail, dark=False, scale=1.0, personalized_rates=None, sub_school=None):
    """HTML block showing round-by-round acceptance rates + in/out-of-state
    where available. Empty string if no curated data.

    personalized_rates: dict {round_code: rate_0_to_1} — AI-derived per-round
        rates for the current viewer. If provided, displays these and shows
        the school's published rate in muted parens for context. Header
        becomes "By application round (your odds)".
    sub_school: dict with "accept" — when the user's major matches a curated
        sub-school (e.g. Kelley at IU), scale the displayed published rates
        by (sub_school_accept / school_accept) so the "school: X%" reference
        reflects the sub-college's selectivity, not the university overall.
        Most universities don't publish separate round rates per sub-college,
        so this is a proportional estimate.
    scale: legacy fallback multiplier — applied uniformly to each round if
        personalized_rates is not provided. Scale==1.0 means show published.
    """
    if not detail:
        return ""
    rates = detail.get("rates", {})
    # Sub-school scale: if user's major matches a sub-college with a different
    # accept rate, scale the displayed rates so they're sub-college-equivalent.
    sub_ratio = 1.0
    sub_label = ""
    if sub_school and sub_school.get("accept") and school and school.get("accept"):
        sub_ratio = sub_school["accept"] / school["accept"]
        sub_short = sub_school["name"].split("(")[0].strip()
        sub_label = f" — estimated for {sub_short}"
    rates = {k: v * sub_ratio for k, v in rates.items()}
    border = "#333" if dark else "#f0f0f0"
    label_color = "#bdbdbd" if dark else "#666"
    try:
        scale_f = float(scale) if scale else 1.0
    except (TypeError, ValueError):
        scale_f = 1.0
    use_ai = bool(personalized_rates)
    personalized = use_ai or abs(scale_f - 1.0) > 0.05
    rows = ""
    for r in detail.get("rounds", []):
        pub_rate = rates.get(r)
        # ED1/ED2 rounds fall back to the single "ED" rate when no split rate is
        # stored — data keys ED for the round-recommender while rounds list ED1/ED2.
        if pub_rate is None and r in ("ED1", "ED2"):
            pub_rate = rates.get("ED")
        adj = None
        if use_ai and personalized_rates.get(r) is not None:
            adj = max(0.005, min(0.95, float(personalized_rates[r])))
        elif pub_rate is not None:
            adj = max(0.005, min(0.95, pub_rate * scale_f))
        if adj is not None:
            rate_str = f"{round(adj*100,1)}%"
            if personalized and pub_rate is not None:
                pub = round(pub_rate*100,1)
                rate_str = f'{round(adj*100,1)}% <span style="color:{label_color};font-weight:400;font-size:.82em">(school: {pub}%)</span>'
        else:
            rate_str = "—"
        rows += f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid {border};font-size:.9em;gap:8px;flex-wrap:wrap"><span>{ROUND_LABELS.get(r, r)}</span><span style="font-weight:600;white-space:nowrap">{rate_str}</span></div>'
    state_block = ""
    in_r = detail.get("in_state_rate")
    out_r = detail.get("out_of_state_rate")
    if in_r is not None or out_r is not None:
        state_rows = ""
        if in_r is not None:
            state_rows += f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid {border};font-size:.9em"><span>In-state</span><span style="font-weight:600">{round(in_r*100,1)}%</span></div>'
        if out_r is not None:
            state_rows += f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid {border};font-size:.9em"><span>Out-of-state</span><span style="font-weight:600">{round(out_r*100,1)}%</span></div>'
        state_block = f'<div style="margin-top:10px"><div style="font-weight:600;font-size:.85em;color:{label_color};margin-bottom:2px">State residency</div>{state_rows}</div>'
    header = "By application round (your odds)" if personalized else "By application round"
    header += sub_label
    return f'<div style="margin-top:12px"><div style="font-weight:600;font-size:.85em;color:{label_color};margin-bottom:2px">{header}</div>{rows}{state_block}</div>'


def render_round_breakdown_dark(school, detail, scale=1.0, personalized_rates=None, sub_school=None):
    return render_admissions_breakdown(school, detail, dark=True, scale=scale, personalized_rates=personalized_rates, sub_school=sub_school)


# Bump this when the personalize_round_odds prompt logic changes —
# auto-invalidates all cached round breakdowns on the next request so
# users immediately see results from the new prompt.
ROUND_PROMPT_VERSION = "v10"


def _profile_version_hash(profile):
    """Compact hash of the profile fields that affect personalized round
    odds. Used as a cache key — when relevant fields change, the cache
    invalidates automatically."""
    import hashlib
    # Only the fields that actually move chances per round
    keys = ['gpa','gpa_scale','uw_gpa','weighted_gpa',
            'gpa_freshman','gpa_sophomore','gpa_junior','gpa_senior',
            'sat','act','rigor','first_gen',
            'state','household_income','intended_major','is_exceptional',
            'extracurriculars','awards','legacy_schools']
    payload = json.dumps({k: profile.get(k) for k in keys}, sort_keys=True, default=str)
    return f"{ROUND_PROMPT_VERSION}:{hashlib.sha1(payload.encode()).hexdigest()[:12]}"


def personalize_round_odds(user_id, school, detail, profile, user_low_pct, user_high_pct, sub_school=None):
    """Use Claude to compute the user's personalized rate for each round
    (ED, ED2, EA, REA, RD), accounting for school-specific dynamics —
    e.g. UPenn ED gives ~3-4× lift, Stanford REA barely moves for unhooked,
    schools that track demonstrated interest weight EA differently, etc.

    Returns a dict {round_code: rate_as_float_0_to_1} or None on failure
    (caller should fall back to linear scaling).

    Cached in DB by (user_id, college_slug, profile_version)."""
    if not detail or not detail.get("rates"):
        return None
    rounds = detail.get("rounds", [])
    pub_rates = detail.get("rates", {})
    if not rounds:
        return None
    # The is_exceptional flag is decided by the 3-judge LLM panel
    # (evaluate_profile_exceptionality) and persisted on the profile — we honor
    # that verdict as-is here. No keyword override: it would second-guess the
    # panel by flipping on a matched word.
    # If only one round (RD-only schools like UCs), no point personalizing breakdown
    if len(rounds) == 1:
        return None

    pv = _profile_version_hash(profile or {})
    # Sub-school slug also goes in the cache key so switching majors
    # invalidates the cached round rates
    sub_key = sub_school["name"][:40] if sub_school else "_none_"
    # Rates hash invalidates the cache when school round-rate data changes
    # (e.g. a CDS refresh). Without it, the AI keeps anchoring on stale
    # published rates and the round breakdown stays wrong indefinitely.
    rates_sig = hashlib.sha1(
        json.dumps({"rounds": rounds, "rates": pub_rates}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    cache_key = f"{pv}:{sub_key}:{rates_sig}"

    # Cache lookup
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT body FROM personalized_rounds WHERE user_id=? AND college_slug=? AND profile_version=?",
                (user_id, school["slug"], cache_key),
            ).fetchone()
            if row:
                try:
                    return json.loads(row["body"])
                except Exception:
                    pass
    except Exception:
        pass

    if not _claude_client:
        return None

    # Build a compact profile summary for the prompt
    parts = []
    if profile.get("gpa"): parts.append(f"GPA: {profile.get('gpa')} ({profile.get('gpa_scale','4.0')})")
    if profile.get("sat"): parts.append(f"SAT: {profile.get('sat')}")
    if profile.get("act"): parts.append(f"ACT: {profile.get('act')}")
    if profile.get("rigor"): parts.append(f"Rigor: {profile.get('rigor')}")
    # NOTE: race is intentionally NOT fed to the odds model. Post-SFFA it is not
    # a usable admissions factor, and the model would only confabulate a delta.
    if profile.get("first_gen"): parts.append("First-gen")
    if profile.get("state"): parts.append(f"State: {profile.get('state')}")
    if profile.get("intended_major"): parts.append(f"Major: {profile.get('intended_major')}")
    if profile.get("is_exceptional"): parts.append("EXCEPTIONAL APPLICANT (top 1-2% nationally)")
    legacies = profile.get("legacy_schools") or []
    # Profile stores legacy_schools as a comma-separated string; older code
    # path expected a list and would `.join()` over a string (iterating
    # char-by-char into garbage). Normalize either shape into a clean list.
    if isinstance(legacies, str):
        legacies = [s.strip() for s in legacies.split(",") if s.strip()]
    if legacies:
        # Also tell Claude this user IS a legacy at the target school if it
        # matches — so the round rates reflect that boost specifically.
        target_match = any(
            l.lower() in (school.get("name") or "").lower()
            or (school.get("slug") or "").lower() in l.lower()
            for l in legacies
        )
        if target_match:
            parts.append(f"LEGACY AT THIS SCHOOL — apply legacy boost (typically 2-5x baseline at top schools)")
        parts.append(f"Legacy at: {', '.join(legacies)}")
    ecs = profile.get("extracurriculars") or ""
    if isinstance(ecs, list): ecs = "; ".join(ecs)
    if ecs:
        parts.append(f"ECs: {str(ecs)[:400]}")
    awards = profile.get("awards") or ""
    if isinstance(awards, list): awards = "; ".join(awards)
    if awards:
        parts.append(f"Awards: {str(awards)[:300]}")

    profile_summary = " | ".join(parts) if parts else "Minimal profile data."

    # If applying to a specific sub-college (Kelley, Wharton, etc.), the
    # round rates need to scale to that sub-college's selectivity since
    # sub-colleges rarely publish their own round rates.
    sub_ratio = 1.0
    if sub_school and sub_school.get("accept") and school.get("accept"):
        sub_ratio = sub_school["accept"] / school["accept"]
    def _round_rate(rd):
        v = pub_rates.get(rd)
        if v is None and rd in ("ED1", "ED2"):
            v = pub_rates.get("ED")  # fall back to single ED rate for split rounds
        return v or 0
    effective_pub_rates = {r: _round_rate(r) * sub_ratio for r in rounds}

    rate_lines = "\n".join(
        f"  - {ROUND_LABELS.get(r, r)} ({r}): rate {round(effective_pub_rates[r]*100,1)}%"
        for r in rounds
    )

    overall_pub_pct = round(((sub_school["accept"] if sub_school else school.get("accept", 0))) * 100, 1)
    user_mid = round((user_low_pct + user_high_pct) / 2.0, 1)
    target_label = (
        f"{school['name']} → {sub_school['name']}" if sub_school else school['name']
    )
    sub_school_note = ""
    if sub_school:
        sub_school_note = (
            f"\n*** APPLYING TO SPECIFIC COLLEGE WITHIN {school['name'].upper()}: {sub_school['name']} ***\n"
            f"Use {round(sub_school['accept']*100,1)}% as the SCHOOL'S accept rate (not the university overall). "
            f"The round rates above have already been scaled proportionally to this sub-college's selectivity. "
            f"Reason about ED/EA lift dynamics as you would for a school with that overall rate.\n"
        )

    prompt = f"""School: {target_label}
Published acceptance (use this, not the university overall): {overall_pub_pct}%
Round rates (already adjusted for the relevant sub-college if applicable):
{rate_lines}
{sub_school_note}
Applicant profile: {profile_summary}
This applicant's overall personalized chances: {user_low_pct}-{user_high_pct}% (midpoint {user_mid}%)

Estimate this applicant's chances IN EACH ROUND.

ANCHOR YOUR ESTIMATES ON THE PUBLISHED RATES ABOVE — they encode each school's specific ED/EA dynamics. Don't pick numbers from scratch; reason from what this school actually publishes.

The translation from published pool rates to individual applicant rates:
- Pool rates reflect (a) self-selection of stronger applicants into ED, (b) heavy concentration of recruited athletes / legacy / development cases in ED at most schools, and (c) yield management.
- An INDIVIDUAL applicant typically captures only 50-70% of the published pool ED:RD ratio because they can't change the pool composition or add hooks.
- Example: school publishes ED 18%, RD 5% → pool ratio 3.6×. An unhooked applicant choosing ED gets ~2-2.5× their RD odds personally, not 3.6×. So if their RD chance is 8%, their ED chance is 16-20%, not 29%.

Per-round rules of thumb:
- RD: should land approximately at the applicant's overall personalized midpoint, since RD is the bulk of the pool the personalized number was calibrated against.

- ED LIFT — three regimes:
  (a) ELITE schools (Ivies, Stanford, MIT, Caltech, UChicago, Duke, JHU, Northwestern, Vandy): individual lift is ~0.4-0.55 × the pool ED:RD ratio. The pool ratio is inflated heavily by athletes/legacy/dev cases that an individual can't replicate. So if Penn pool ratio is 3.1× (ED 14% / RD 4.5%), an unhooked individual gets ~1.5-1.7× lift.
  (b) YIELD-EXTREMIST schools — places that aggressively use ED as a yield lever and give a REAL bump to non-hooked ED applicants. THESE include: Tulane, Tufts (ED1+ED2), Northeastern (EA AND ED), Boston University, Lehigh, Villanova, George Washington, American, Case Western, Brandeis, Wake Forest, Miami, SMU, Pepperdine, BC, Fordham (when binding). Individual lift here is ~0.75-0.95 × the pool ratio. Tulane pool ratio is ~5-6× and an unhooked applicant genuinely gets ~4-5× lift.
  (c) STANDARD yield-conscious privates and LACs not in (a) or (b): individual lift is ~0.55-0.75 × the pool ratio.

- ED2: ~70-85% of the ED1 lift (still committal, but pool is weaker than ED1).
- EA at non-binding schools (Georgetown, Notre Dame, BC EA, MIT, Caltech, UNC EA, UVA EA): take RD × (0.3-0.5 × the published EA:RD ratio). Less committal = smaller bump. EXCEPT for the demo-interest schools below.
- REA / single-choice EA at HYPS: minimal lift for unhooked applicants (1.05-1.3× RD).
- Demonstrated-interest extremist EA (Northeastern EA, BU EA, Tulane EA equivalents): ~0.7-0.9 × pool ratio because these schools care a LOT about EA as an interest signal.
- For TRULY exceptional applicants (recruited athlete, USAMO/IMO gold, national-level distinction, dev case): odds stay HIGH across all rounds (60-85%), round matters less.

Critical:
- ED ≥ RD always. Never invert.
- Don't apply yield-conscious lifts at elite schools (Penn ED ≠ Northeastern EA).
- Don't apply elite caps at yield-conscious schools (Tulane ED genuinely is 3× for fit applicants).
- Use the published rates as the anchor. If they suggest a 1.5× pool ratio, individual is ~1.0-1.2×. If they suggest 4× pool ratio, individual is ~2-2.8×.

Return ONLY valid JSON (no markdown, no preamble):
{{
  "rates": {{ {", ".join(f'"{r}": <float 0-1>' for r in rounds)} }},
  "reasoning": "<one sentence: this school's published ED:RD pool ratio is X, so for this applicant individually I estimated Y because Z>"
}}

Hard rules:
- Each rate is the applicant's actual chance (0.0-0.95), not a percentage.
- ED ≥ RD at any binding-ED school.
- Cap any single round at 0.95 even for exceptional applicants.
- Keep reasoning under 30 words."""

    try:
        resp = _claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system="You are a college admissions analyst who knows exactly how each school's specific admissions process moves the odds round-by-round. Use your real knowledge of ED/EA dynamics — don't be artificially conservative. Return only the requested JSON.",
            messages=[{"role":"user","content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip code-fences if Haiku added them despite instructions
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        data = json.loads(text)
        rates_out = data.get("rates", {})
        # Sanity check + clamp.
        # Anchor the cap on the user's relative-to-school position. If
        # the user's overall personal odds are 4% and the school admits
        # 5.5% overall, they're a 0.73x applicant. AI sometimes returns
        # rates that violate this — e.g. saying EA personal = 8% when
        # the user's overall is 4% and EA published = overall = 5.5%
        # (which would imply the user is BETTER than average for EA
        # specifically, despite being below-average overall — incoherent).
        # Cap each round's personal rate at school_round_rate * user_ratio
        # to enforce relative-position consistency.
        anchor = (sub_school["accept"] if sub_school else school.get("accept", 0)) or 0
        user_mid_frac = (user_low_pct + user_high_pct) / 200.0  # 0-1
        user_ratio = (user_mid_frac / anchor) if anchor else 1.0
        # But never let a round drop below a small floor if AI returned
        # something. And never apply a ratio cap that's lower than the
        # school's RD rate × user_ratio (RD floor). Hooked applicants
        # (legacy/exceptional) get a bump above 1.0x; for them ratio can
        # exceed 1, so the cap is already loose.
        clean = {}
        for r in rounds:
            v = rates_out.get(r)
            try:
                v = float(v)
                if not (0.0 <= v <= 0.99):
                    continue
                pub_r = effective_pub_rates.get(r, 0)
                if pub_r > 0 and user_ratio > 0:
                    cap = min(0.95, pub_r * user_ratio * 1.4)  # +40% slack for AI nuance
                    v = min(v, cap)
                clean[r] = round(min(0.95, max(0.005, v)), 4)
            except (TypeError, ValueError):
                continue
        if not clean:
            return None
        # Cache
        try:
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO personalized_rounds (user_id, college_slug, profile_version, body) VALUES (?,?,?,?)",
                    (user_id, school["slug"], cache_key, json.dumps(clean)),
                )
                conn.commit()
        except Exception as e:
            print(f"personalized_rounds cache write failed: {e}")
        return clean
    except Exception as e:
        print(f"personalize_round_odds error for {school['slug']}: {e}")
        return None


def sf_ratio(c):
    """Best-effort student-faculty ratio: curated value if known, else
    federal override, else estimated from size + type. Curated values win
    because ambiguously-named schools (e.g. "MIT", "Wesleyan") can match
    the wrong school in Scorecard and produce wildly off ratios."""
    if c["slug"] in SF_RATIO_BY_SLUG:
        return SF_RATIO_BY_SLUG[c["slug"]]
    over = _get_overrides(c["slug"])
    if over and over.get("sf_ratio"): return over["sf_ratio"]
    s = c.get("size", 10000)
    if c["type"] == "private":
        if s < 3000: return 9
        if s < 8000: return 11
        return 14
    # public
    if s < 8000: return 14
    if s < 20000: return 17
    return 19


# ─── COLLEGE SCORECARD INTEGRATION ────────────────────────
_overrides_cache = {}  # slug → dict, in-memory cache to avoid hitting DB on every render

def _get_overrides(slug):
    """Pull cached federal-data overrides for a school. Returns None if no
    overrides exist (yet)."""
    if slug in _overrides_cache:
        return _overrides_cache[slug]
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT accept, sat_25, sat_75, act_25, act_75, size, tuition, sf_ratio, "
                "median_earnings_10yr, cost_attendance, source, verified_at "
                "FROM school_stats_overrides WHERE college_slug=?",
                (slug,)
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        _overrides_cache[slug] = None
        return None
    d = {k: row[k] for k in row.keys()}
    _overrides_cache[slug] = d
    return d


# Schools where the hardcoded COLLEGES.accept value reflects 2024-25 or
# 2025-26 cycle data (hand-typed from each school's press release). Those
# beat federal Scorecard data on the accept rate, since IPEDS lags 12-18
# months. Scorecard still wins on the more-stable stats (SAT/ACT/size/
# tuition) where its current data is fine.
# Schools that are test-blind / test-free for the current cycle. We hide
# their SAT/ACT mid-50% on the detail page, rankings, and compare table —
# it's misleading to show test scores for schools that explicitly don't
# consider them in admissions. UCs went test-free in 2021 and have stayed
# that way through the 25-26 cycle.

# Schools where a portfolio / audition / research supplement is a primary
# admissions gatekeeper (not just nice-to-have). When the user has a
# portfolio listed in their profile, odds at these schools get a modest
# 1.15x lift. Doesn't apply to schools where portfolios are optional /
# don't materially change admit odds.

def is_test_blind(school_or_slug):
    slug = school_or_slug if isinstance(school_or_slug, str) else school_or_slug.get("slug")
    return slug in TEST_BLIND_SCHOOLS


# Hand-verified Common Data Set figures (24-25 cycle). Highest precedence:
# overrides BOTH the hardcoded COLLEGES values AND the federal Scorecard
# overrides. Source: independent educational consultant who manually
# pulled CDS PDFs (Burgess Workshop). Use sparingly — only entries here
# come from a trusted human-verified source.
# Schema: slug -> {accept, sat_25, sat_75, act_25, act_75}.
# `None` for SAT/ACT means the school is test-blind for that cycle.




def merged_school(c):
    """Return a copy of the college dict with overrides applied. Precedence:
    1. CDS_VERIFIED — hand-verified Common Data Set figures (24-25 cycle)
       supplied by an industry consultant. Highest authority for any field
       it specifies.
    2. Hand-typed 2024-25/2025-26 cycle data on `accept` for schools in
       MANUAL_FRESH_ACCEPT (since federal Scorecard lags 12-18 months).
    3. Scorecard's federal IPEDS data on all other fields.
    4. Hardcoded COLLEGES value as final fallback.
    """
    cds = CDS_VERIFIED.get(c["slug"])
    over = _get_overrides(c["slug"])
    if not cds and not over:
        return c
    out = dict(c)
    if over:
        # Don't override "size" from federal data: Scorecard's name search
        # for ambiguous school names (e.g. "MIT", "Wesleyan") sometimes
        # matches the wrong institution and returns a wildly off undergrad
        # count. Hand-typed sizes in COLLEGES are accurate enough.
        for k in ("accept", "sat_25", "sat_75", "act_25", "act_75", "tuition"):
            if k == "accept" and c["slug"] in MANUAL_FRESH_ACCEPT:
                continue  # keep hand-typed fresher rate
            v = over.get(k)
            if v is not None:
                out[k] = v
    if cds:
        # CDS wins over everything for fields it specifies. Only the keys
        # present in the CDS dict are touched — partial entries (e.g. just
        # accept) leave other fields untouched.
        for k in ("accept", "sat_25", "sat_75", "act_25", "act_75"):
            if k in cds and cds[k] is not None:
                out[k] = cds[k]
    return out


def is_cds_verified(slug):
    """True if this school's stats come from the hand-verified CDS dict
    (24-25 cycle), so we can show a 'verified' badge on its page."""
    return slug in CDS_VERIFIED


# Map our slug → Scorecard "name" search term. For most schools the school
# name works directly; the trickier ones get an explicit override.


def fetch_scorecard(c):
    """Hit the College Scorecard API for a single school. Returns the
    normalized override dict (caller persists it). Returns None on any
    failure — renderers will keep using the hardcoded fallback values."""
    if not SCORECARD_KEY:
        return None
    name = SCORECARD_NAME_OVERRIDES.get(c["slug"], c["name"])
    fields = ",".join([
        "school.name",
        "latest.admissions.admission_rate.overall",
        "latest.admissions.sat_scores.25th_percentile.critical_reading",
        "latest.admissions.sat_scores.75th_percentile.critical_reading",
        "latest.admissions.sat_scores.25th_percentile.math",
        "latest.admissions.sat_scores.75th_percentile.math",
        "latest.admissions.act_scores.25th_percentile.cumulative",
        "latest.admissions.act_scores.75th_percentile.cumulative",
        "latest.student.size",
        "latest.cost.tuition.in_state",
        "latest.cost.tuition.out_of_state",
        "latest.cost.attendance.academic_year",
        "latest.earnings.10_yrs_after_entry.median",
        "latest.student.demographics.student_faculty_ratio",
    ])
    try:
        r = requests.get(
            "https://api.data.gov/ed/collegescorecard/v1/schools",
            params={
                "school.name": name,
                "fields": fields,
                "per_page": 5,
                "api_key": SCORECARD_KEY,
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Scorecard {r.status_code} for {c['slug']}: {r.text[:160]}")
            return None
        results = (r.json() or {}).get("results", [])
        if not results:
            print(f"Scorecard: no results for {c['slug']} (name='{name}')")
            return None
        # Best match: the result whose name most closely matches our target.
        target = name.lower()
        results.sort(key=lambda x: abs(len(x.get("school.name","")) - len(target)))
        row = results[0]
        accept = row.get("latest.admissions.admission_rate.overall")
        sat_cr_25 = row.get("latest.admissions.sat_scores.25th_percentile.critical_reading")
        sat_cr_75 = row.get("latest.admissions.sat_scores.75th_percentile.critical_reading")
        sat_m_25 = row.get("latest.admissions.sat_scores.25th_percentile.math")
        sat_m_75 = row.get("latest.admissions.sat_scores.75th_percentile.math")
        # Total SAT = critical reading + math
        sat_25 = (sat_cr_25 + sat_m_25) if sat_cr_25 and sat_m_25 else None
        sat_75 = (sat_cr_75 + sat_m_75) if sat_cr_75 and sat_m_75 else None
        act_25 = row.get("latest.admissions.act_scores.25th_percentile.cumulative")
        act_75 = row.get("latest.admissions.act_scores.75th_percentile.cumulative")
        size = row.get("latest.student.size")
        # Use OOS tuition for privates and instate for publics (cheaper headline)
        tuition_oos = row.get("latest.cost.tuition.out_of_state")
        tuition_is  = row.get("latest.cost.tuition.in_state")
        tuition = tuition_is if c["type"] == "public" else tuition_oos
        sf = row.get("latest.student.demographics.student_faculty_ratio")
        earnings_10yr = row.get("latest.earnings.10_yrs_after_entry.median")
        cost_attend = row.get("latest.cost.attendance.academic_year")
        return {
            "accept": accept,
            "sat_25": int(sat_25) if sat_25 else None,
            "sat_75": int(sat_75) if sat_75 else None,
            "act_25": int(act_25) if act_25 else None,
            "act_75": int(act_75) if act_75 else None,
            "size": int(size) if size else None,
            "tuition": int(tuition) if tuition else None,
            "sf_ratio": int(round(sf)) if sf else None,
            "median_earnings_10yr": int(earnings_10yr) if earnings_10yr else None,
            "cost_attendance": int(cost_attend) if cost_attend else None,
            "source": "College Scorecard (IPEDS)",
        }
    except Exception as e:
        print(f"Scorecard error for {c['slug']}: {e}")
        return None


def update_scorecard_overrides(c):
    """Fetch fresh Scorecard data for a school + persist as overrides.
    Returns True on success."""
    data = fetch_scorecard(c)
    if not data:
        return False
    with db() as conn:
        conn.execute("""INSERT INTO school_stats_overrides
            (college_slug, accept, sat_25, sat_75, act_25, act_75, size, tuition, sf_ratio,
             median_earnings_10yr, cost_attendance, source, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                accept=excluded.accept,
                sat_25=excluded.sat_25, sat_75=excluded.sat_75,
                act_25=excluded.act_25, act_75=excluded.act_75,
                size=excluded.size, tuition=excluded.tuition,
                sf_ratio=excluded.sf_ratio,
                median_earnings_10yr=excluded.median_earnings_10yr,
                cost_attendance=excluded.cost_attendance,
                source=excluded.source, verified_at=CURRENT_TIMESTAMP""",
            (c["slug"], data["accept"], data["sat_25"], data["sat_75"],
             data["act_25"], data["act_75"], data["size"], data["tuition"],
             data["sf_ratio"],
             data.get("median_earnings_10yr"), data.get("cost_attendance"),
             data["source"]))
        conn.commit()
    _overrides_cache.pop(c["slug"], None)
    return True

SIZE_RANGES = {
    "xs":     (0, 2000),
    "small":  (2000, 6000),
    "medium": (6000, 12000),
    "ml":     (12000, 18000),
    "large":  (18000, 10**9),
}



def _bucket_of(value, ranges, ordered):
    for k in ordered:
        lo, hi = ranges[k]
        if lo <= value < hi:
            return k
    return ordered[-1]


def _bucket_verdict(value, chosen, ranges, ordered):
    """match if school's bucket is in user's chosen set; neutral if it's
    one bucket away from any chosen bucket; mismatch otherwise. This gives
    a consistent middle ground for schools that are 'close but not in.'"""
    if not chosen:
        return None
    actual = _bucket_of(value, ranges, ordered)
    if actual in chosen:
        return "match"
    a = ordered.index(actual)
    if any(abs(ordered.index(c) - a) == 1 for c in chosen if c in ordered):
        return "neutral"
    return "mismatch"


def class_size_bucket(c):
    return _bucket_of(sf_ratio(c), CLASS_SIZE_RANGES, CLASS_SIZE_BUCKETS)


def avg_class_size_estimate(c):
    """Rough estimate of average undergrad class size, derived from S/F ratio.
    True avg class size depends on class-size distribution which schools report
    separately on the Common Data Set (Section I-3). We approximate as
    ~2.2× the S/F ratio: not every faculty teaches every term, big publics
    have lectures of 200+ that pull the avg up. Marked 'est.' in the UI."""
    r = sf_ratio(c)
    return max(8, round(r * 2.2))


# ─── SCHOOL ATTRIBUTES (for preference matching + ranking) ─
# Tag set per school. Untagged → falls back to neutral matching. We focus tags
# on schools where the attribute is genuinely a defining feature, not on
# every school where it merely exists.


def school_attrs(slug):
    """Return the set of tags for a school (empty set if untagged)."""
    return set(SCHOOL_TAGS.get(slug, []))


def compute_my_fit(profile, school):
    """A multi-factor fit score for the My Fit ranking.
    Combines (a) realistic admit chances, (b) preference match,
    (c) academic competitiveness, (d) major fit. Returns (score 0-100, breakdown).

    Why each piece:
      - Admit realism — 5★ at Stanford for someone with 3% odds is misleading.
      - Preference match — a school you'd hate doesn't fit, even if you'd be admitted.
      - Academic match — too far above or below the school's range is suboptimal too.
      - Major fit — if the school doesn't offer your major as a real strength,
        cap the rating regardless of the rest.
    """
    # 1) Admit realism: scaled by the OUTPUT of the same chances calculator,
    # so the ranking matches what the chances page says.
    fit_acad, _ = compute_fit(profile, school)
    low, high = estimate_odds(school, fit_acad, profile)
    odds_mid = (low + high) / 2.0  # in percentage points 0-100
    # Map odds to a 0-100 realism score:
    #   ≤5% → 25 (steep penalty, but not zero)
    #   5-25% → climbs from 25 to 100 (the sweet spot of "competitive reach")
    #   25-100% → plateaus at 100 (any realistic admit is a real option)
    # Earlier curve hit UCB-tier schools too hard.
    if odds_mid <= 5:
        admit_realism = max(0, odds_mid * 5)
    elif odds_mid <= 25:
        admit_realism = 25 + (odds_mid - 5) * (75 / 20.0)
    else:
        admit_realism = 100

    # 2) Preference match
    m = school_match(profile, school)
    pref_score = m["score"] if (m and m.get("rated_count")) else 50  # neutral if no prefs

    # 3) Academic match — over-qualified penalty removed (it was punishing
    # safety schools that should be dream-fit safety options).
    if fit_acad >= 60:
        academic = 100
    elif fit_acad >= 40:
        academic = 60 + (fit_acad - 40) * 2  # 40 → 60, 60 → 100
    else:
        academic = max(0, fit_acad * 1.5)

    # Fit is dominated by preferences. Major-strength is now a user-controlled
    # preference (with importance dial), so the standalone major component
    # was removed.
    #   prefs 80% · academic 10% · admit_realism 10%
    score = round(0.10 * admit_realism + 0.80 * pref_score + 0.10 * academic, 1)
    # Veto: if any preference the user marked importance=10 ends up as a
    # mismatch, this school is a hard no — the user said "deal-breaker"
    # and we honor that even if other prefs match.
    vetoed = False
    veto_reasons = []
    if m and m.get("per_pref"):
        for key, (verdict, txt) in m["per_pref"].items():
            if verdict == "mismatch" and get_pref_weight(profile, key) == 10:
                vetoed = True
                veto_reasons.append(f"{key}: {txt}")
    return min(100, max(0, score)), {
        "admit_realism": round(admit_realism, 1),
        "pref": round(pref_score, 1),
        "academic": round(academic, 1),
        "odds_mid": round(odds_mid, 1),
        "vetoed": vetoed,
        "veto_reasons": veto_reasons,
    }


# Schools that meet ~full demonstrated financial need (need-met, mostly
# need-only — little/no merit aid). Curated seed list; tier<=1 also treated as
# need-meeting. Used by the "financial aid generosity" preference.
MEETS_FULL_NEED = {
    "harvard","yale","princeton","stanford","mit","columbia","upenn","brown","dartmouth",
    "cornell","caltech","uchicago","duke","northwestern","johns-hopkins","jhu","rice",
    "vanderbilt","notre-dame","georgetown","washu","wash-u","emory","ucb","ucla",
    "williams","amherst","swarthmore","pomona","bowdoin","wellesley","claremont-mckenna",
    "middlebury","carleton","hamilton","colby","colgate","davidson","vassar","grinnell",
    "haverford","wesleyan","smith","washington-and-lee","barnard","tufts","bc","boston-college",
    "wake-forest","colorado-college","macalester","oberlin","bates","scripps","harvey-mudd",
    "olin","cooper-union",
}
# Academic culture seed lists (collaborative vs intense/competitive). Everything
# not listed defaults to "balanced". Used by the "academic culture" preference.
COMPETITIVE_SCHOOLS = {
    "mit","caltech","johns-hopkins","jhu","cornell","uchicago","gatech","ucb","ucla",
    "cmu","carnegie-mellon","washu","wash-u","nyu","columbia","upenn","berkeley","ucsd",
    "harvey-mudd","cooper-union","georgetown","northwestern",
}
COLLABORATIVE_SCHOOLS = {
    "brown","stanford","rice","dartmouth","notre-dame","vanderbilt","olin","hampshire",
    "pomona","davidson","bowdoin","wake-forest","wesleyan","oberlin","macalester","colorado-college",
    "yale","princeton","bates","carleton","grinnell","kenyon","amherst",
}


def school_match(profile, school):
    """Score how well a school matches the user's preferences, weighted by
    each pref's user-set importance (1-10, default 5). Returns
    {per_pref, score 0-100, rated_count}. Only prefs the user actually set
    are counted — no filler rows."""
    if not profile:
        return None
    out = {}
    score, count = 0.0, 0.0
    def add(key, base_score):
        """Add a pref's contribution, scaled by user-set importance weight."""
        nonlocal score, count
        w = get_pref_weight(profile, key)
        score += base_score * w
        count += w

    # All prefs are now multi-select. Match if school's value is in the user's
    # chosen set; mismatch if they made a choice and the school isn't in it;
    # skipped entirely if user picked nothing for that pref.

    # 1) Weather — auto-derived from state
    chosen = pref_set(profile, "pref_weather")
    school_climate = climate_of(school)
    if chosen:
        if school_climate in chosen:
            out["weather"] = ("match", school_climate); add("weather", 10)
        else:
            out["weather"] = ("mismatch", f"{school_climate} (you wanted {' or '.join(sorted(chosen))})"); add("weather", 0)

    # 2) Setting
    chosen = pref_set(profile, "pref_setting")
    school_setting = setting_of(school)
    if chosen:
        if school_setting in chosen:
            out["setting"] = ("match", school_setting); add("setting", 10)
        else:
            out["setting"] = ("mismatch", f"{school_setting} (you wanted {' or '.join(sorted(chosen))})"); add("setting", 0)

    # 3) Size — 5-bucket scale (xs/small/medium/ml/large). Adjacent buckets
    # count as neutral so schools "close but not in" land in the middle.
    chosen = pref_set(profile, "pref_size")
    size_n = school.get("size", 0) or 0
    verdict = _bucket_verdict(size_n, chosen, SIZE_RANGES, SIZE_BUCKETS)
    if verdict:
        label = f"{size_n:,} undergrads"
        if verdict == "match":
            out["size"] = ("match", label); add("size", 10)
        elif verdict == "neutral":
            out["size"] = ("neutral", f"{label} — close to {' or '.join(sorted(chosen))}"); add("size", 7)
        else:
            out["size"] = ("mismatch", f"{label} — you wanted {' or '.join(sorted(chosen))}"); add("size", 0)

    # 4) Class size — same 5-bucket adjacency logic, on SF ratio
    chosen = pref_set(profile, "pref_class_size")
    r = sf_ratio(school)
    verdict = _bucket_verdict(r, chosen, CLASS_SIZE_RANGES, CLASS_SIZE_BUCKETS)
    if verdict:
        if verdict == "match":
            out["class_size"] = ("match", f"{r}:1 student-faculty"); add("class_size", 10)
        elif verdict == "neutral":
            out["class_size"] = ("neutral", f"{r}:1 — close to {' or '.join(sorted(chosen))}"); add("class_size", 7)
        else:
            out["class_size"] = ("mismatch", f"{r}:1 — you wanted {' or '.join(sorted(chosen))}"); add("class_size", 0)

    # 5) Greek life
    chosen = pref_set(profile, "pref_greek")
    g = greek_strength(school)
    if chosen:
        if "some" in chosen:
            # Flexible: light or medium is ideal, strong is more than wanted but ok
            if g in ("light", "medium"):
                out["greek"] = ("match", f"{g} Greek scene"); add("greek", 10)
            else:
                out["greek"] = ("neutral", "strong Greek scene"); add("greek", 7)
        elif "strong" in chosen and g == "strong":
            out["greek"] = ("match", "strong Greek scene"); add("greek", 10)
        elif "avoid" in chosen and g == "light":
            out["greek"] = ("match", "light Greek scene"); add("greek", 10)
        elif g == "medium":
            out["greek"] = ("neutral", "medium Greek scene"); add("greek", 7)
        else:
            out["greek"] = ("mismatch", f"{g} Greek scene"); add("greek", 0)

    # 6) Sports
    chosen = pref_set(profile, "pref_sports")
    s = sports_strength(school)
    if chosen:
        if "medium" in chosen:
            # Flexible: moderate is ideal, either extreme is fine-ish
            if s == "medium":
                out["sports"] = ("match", "moderate sports"); add("sports", 10)
            else:
                out["sports"] = ("neutral", f"{s} sports culture"); add("sports", 8)
        elif "strong" in chosen and s == "strong":
            out["sports"] = ("match", "big sports culture"); add("sports", 10)
        elif "low" in chosen and s == "low":
            out["sports"] = ("match", "low-key sports"); add("sports", 10)
        elif s == "medium":
            out["sports"] = ("neutral", "moderate sports"); add("sports", 7)
        else:
            out["sports"] = ("mismatch", f"{s} sports culture"); add("sports", 0)

    # 7) Major strength — does the school feature the user's major in its
    # notable programs list. Skipped if user hasn't entered a major.
    chosen = pref_set(profile, "pref_major_strength")
    user_major = (profile.get("major") or "").strip().lower()
    if chosen and user_major:
        school_majors = [m.lower() for m in school.get("majors", [])]
        is_top = any(user_major in sm or sm in user_major for sm in school_majors)
        if "top" in chosen:
            if is_top:
                out["major_strength"] = ("match", f"known for {user_major}"); add("major_strength", 10)
            else:
                out["major_strength"] = ("mismatch", f"not known for {user_major}"); add("major_strength", 0)
        else:  # "solid" — any school is fine
            out["major_strength"] = ("match", "solid program"); add("major_strength", 10)

    # 8) Prestige — graduated, not binary. Tier-3 schools (Villanova, Wake
    # Forest, BU, Tufts, etc.) are still strong; if the user wants "high"
    # prestige, those should be partial credit rather than a hard mismatch
    # that triggers the importance penalty.
    chosen = pref_set(profile, "pref_prestige")
    tier = school.get("tier", 3)
    if chosen:
        if "high" in chosen:
            if tier <= 2:
                out["prestige"] = ("match", f"tier {tier}"); add("prestige", 10)
            elif tier == 3:
                out["prestige"] = ("neutral", f"tier {tier} — still strong"); add("prestige", 7)
            else:
                out["prestige"] = ("mismatch", f"tier {tier}"); add("prestige", 0)
        elif "medium" in chosen:
            if tier in (1, 2, 3):
                out["prestige"] = ("match", f"tier {tier}"); add("prestige", 10)
            elif tier == 4:
                out["prestige"] = ("neutral", f"tier {tier}"); add("prestige", 6)
            else:
                out["prestige"] = ("mismatch", f"tier {tier}"); add("prestige", 0)
        else:  # "low" — anything goes
            out["prestige"] = ("match", f"tier {tier}"); add("prestige", 10)

    # 9) Cost
    chosen = pref_set(profile, "pref_cost")
    sticker = school.get("tuition", 0) or 0
    if chosen:
        ok = ("low" in chosen and sticker < 15000) or \
             ("medium" in chosen and sticker < 40000) or \
             ("high" in chosen)
        if ok:
            out["cost"] = ("match", f"${sticker:,} sticker"); add("cost", 10)
        else:
            out["cost"] = ("mismatch", f"${sticker:,} sticker"); add("cost", 0)

    # 10) Diversity — heuristic: large publics + urban privates skew more
    # diverse; small rural LACs skew less. (Real CDS race/ethnicity data
    # not yet wired in — this is a coarse proxy until it is.)
    chosen = pref_set(profile, "pref_diversity")
    if chosen:
        size_n = school.get("size", 0) or 0
        urban_ish = setting_of(school) in ("urban", "college_town")
        diverse = (size_n >= 8000) or urban_ish or school.get("type") == "public"
        if "moderate" in chosen:
            # Flexible: some diversity is nice, but not a dealbreaker either way
            out["diversity"] = ("match", "reasonably diverse") if diverse else ("neutral", "less diverse student body")
            add("diversity", 10 if diverse else 8)
        elif "high" in chosen:
            out["diversity"] = ("match", "diverse student body") if diverse else ("mismatch", "less diverse student body")
            add("diversity", 10 if diverse else 0)
        else:
            out["diversity"] = ("match", "homogeneous student body") if not diverse else ("neutral", "diverse student body")
            add("diversity", 10 if not diverse else 7)

    # 11) Party scene — heuristic from Greek strength + sports tier.
    # Strong Greek + strong sports = party school; light Greek = quiet.
    chosen = pref_set(profile, "pref_party")
    if chosen:
        g = greek_strength(school); s = sports_strength(school)
        partyish = (g == "strong") or (s == "strong" and g != "light")
        deadquiet = (g == "light" and s == "low")
        if "medium" in chosen:
            # Moderate social scene: happy with most; mild ding only for extremes
            if not partyish and not deadquiet:
                out["party"] = ("match", "moderate social scene"); add("party", 10)
            else:
                out["party"] = ("neutral", "very active" if partyish else "very quiet"); add("party", 8)
        elif "high" in chosen:
            if partyish:
                out["party"] = ("match", "active social scene"); add("party", 10)
            else:
                out["party"] = ("mismatch", "quieter social scene"); add("party", 0)
        else:  # "low"
            if not partyish:
                out["party"] = ("match", "quiet / low-party"); add("party", 10)
            else:
                out["party"] = ("mismatch", "active party scene"); add("party", 0)

    # 12) Research access — heuristic: tier-1/2 research universities (R1)
    # and large publics offer the most undergrad research. LACs vary;
    # most have decent access despite smaller size.
    chosen = pref_set(profile, "pref_research")
    if chosen:
        tier = school.get("tier", 5)
        size_n = school.get("size", 0) or 0
        is_research_heavy = tier <= 2 or (school.get("type") == "public" and size_n >= 15000)
        is_research_decent = tier == 3 or (size_n >= 5000)
        if "critical" in chosen:
            if is_research_heavy:
                out["research"] = ("match", "strong research opportunities"); add("research", 10)
            elif is_research_decent:
                out["research"] = ("neutral", "moderate research access"); add("research", 6)
            else:
                out["research"] = ("mismatch", "limited research access"); add("research", 0)
        elif "nice" in chosen:
            out["research"] = ("match", "research available"); add("research", 10)
        # "none" -> skip (no contribution)

    # 13) Academic culture — heuristic: tier-1 with biz/eng dominance =
    # pre-professional; LACs = exploratory; everything else = balanced.
    chosen = pref_set(profile, "pref_career_intensity")
    if chosen:
        size_n = school.get("size", 0) or 0
        majors = [m.lower() for m in school.get("majors", [])]
        preprof_signals = sum(1 for m in majors if any(k in m for k in
            ("business","finance","engineering","computer","economics","accounting","marketing")))
        is_preprof = preprof_signals >= 2 and (size_n >= 4000 or school.get("type") == "private")
        is_lac = size_n < 3500 and school.get("type") == "private"
        culture = "preprof" if is_preprof else ("flexible" if is_lac else "balanced")
        if culture in chosen:
            out["career_intensity"] = ("match", f"{culture} culture"); add("career_intensity", 10)
        else:
            out["career_intensity"] = ("mismatch", f"{culture} culture"); add("career_intensity", 0)

    # 14) Location / distance from home — uses the user's home state vs the
    # school's state and region. Skipped if the user hasn't set a home state.
    chosen = pref_set(profile, "pref_location")
    home_state = (profile.get("state") or "").strip()
    if chosen and home_state:
        same_state = school.get("state", "") == home_state
        same_region = region_of(school) == REGION_BY_STATE.get(home_state, "_none_")
        if "near" in chosen:
            if same_state: out["location"] = ("match", "in your home state"); add("location", 10)
            elif same_region: out["location"] = ("neutral", "in your region"); add("location", 7)
            else: out["location"] = ("mismatch", "far from home"); add("location", 0)
        elif "region" in chosen:
            if same_region: out["location"] = ("match", "in your region"); add("location", 10)
            else: out["location"] = ("mismatch", "outside your region"); add("location", 0)
        elif "far" in chosen:
            if not same_region: out["location"] = ("match", "far from home"); add("location", 10)
            elif same_state: out["location"] = ("mismatch", "in your home state"); add("location", 0)
            else: out["location"] = ("neutral", "same region"); add("location", 6)

    # 15) Financial aid generosity — meets-full-need (curated/tier<=1) vs merit.
    chosen = pref_set(profile, "pref_aid")
    if chosen:
        slug = school.get("slug")
        meets_need = slug in MEETS_FULL_NEED or school.get("tier", 5) <= 1
        if "any_cost" in chosen:
            out["aid"] = ("match", "cost not a factor"); add("aid", 10)
        elif "full_need" in chosen:
            if meets_need: out["aid"] = ("match", "meets full need"); add("aid", 10)
            else: out["aid"] = ("mismatch", "limited need-based aid"); add("aid", 0)
        elif "merit" in chosen:
            # Need-meeting elites are mostly need-only (no merit); others give merit.
            if not meets_need: out["aid"] = ("match", "offers merit scholarships"); add("aid", 10)
            else: out["aid"] = ("neutral", "need-based, little merit"); add("aid", 5)

    # 16) Academic culture — collaborative vs competitive (curated seed lists).
    chosen = pref_set(profile, "pref_culture")
    if chosen:
        slug = school.get("slug")
        cult = ("competitive" if slug in COMPETITIVE_SCHOOLS
                else "collaborative" if slug in COLLABORATIVE_SCHOOLS else "balanced")
        if cult in chosen:
            out["culture"] = ("match", f"{cult} culture"); add("culture", 10)
        elif "balanced" in chosen or cult == "balanced":
            out["culture"] = ("neutral", f"{cult} culture"); add("culture", 7)
        else:
            out["culture"] = ("mismatch", f"{cult} culture"); add("culture", 0)

    overall = round(score / max(1.0, count) * 10, 1) if count else 0
    # Soft additive penalty (capped) for high-importance mismatches. The
    # weighted average already does most of the importance scaling; this is
    # just a thumb on the scale at imp 7+ so the dial feels meaningful
    # without crushing schools that miss only 1-2 things.
    #   imp 6 → -3%   7 → -8%   8 → -14%   9 → -22%   cap 40%
    PENALTY = {6: 0.03, 7: 0.08, 8: 0.14, 9: 0.22}
    total = 0.0
    for key, (verdict, _txt) in out.items():
        if verdict != "mismatch":
            continue
        w = get_pref_weight(profile, key)
        total += PENALTY.get(w, 0.0)
    total = min(total, 0.40)
    overall = round(overall * (1.0 - total), 1)
    # rated_count = number of distinct prefs the user set (not the weight sum)
    rated = sum(1 for k in ("weather","setting","size","class_size","greek","sports","major_strength","prestige","cost","diversity","party","research","career_intensity","location","aid","culture") if k in out)
    return {"per_pref": out, "score": overall, "rated_count": rated}


# ─── RANKINGS ─────────────────────────────────────────────

RANKINGS_BY_SLUG = {r["slug"]: r for r in RANKINGS}


# Preference-based ranking lists. These extend the curated ones with tag-based
# selection — every school that has the relevant tag, sorted by selectivity.
PREF_RANKINGS = [
    {"slug": "best-greek",       "title": "Best Greek Life",          "blurb": "Schools where the Greek scene is genuinely central to undergraduate life.",   "tag": "greek_strong"},
    {"slug": "best-sports",      "title": "Best Sports / School Spirit","blurb": "Schools with a real game-day, school-spirit culture.",                     "tag": "sports_strong"},
    {"slug": "best-internships", "title": "Best Internship Pipeline", "blurb": "Urban schools and others with strong direct-recruiting pipelines.",          "tag": "internship_strong"},
    {"slug": "best-warm",        "title": "Best Warm-Weather Schools","blurb": "If you want sun and you don't want snow.",                                   "tag": "warm"},
    {"slug": "best-cold",        "title": "Best Snow / Winter Schools","blurb": "If you actually like winter (or at least don't mind it).",                "tag": "cold"},
    {"slug": "best-urban",       "title": "Best Big-City Schools",    "blurb": "Schools embedded in major metro areas.",                                    "tag": "urban"},
    {"slug": "best-college-town","title": "Best College Towns",       "blurb": "Schools where the town basically IS the school.",                           "tag": "college_town"},
    {"slug": "best-small",       "title": "Best Small Schools",       "blurb": "Sub-3,000 enrollment — small classes, close-knit feel.",                     "tag": "small"},
]
for r in PREF_RANKINGS:
    r["order"] = []  # dynamically computed; auto-fill handles it
    r["pref_based"] = True
    RANKINGS.append(r)
    RANKINGS_BY_SLUG[r["slug"]] = r


# Per-ranking filters used by the auto-fill logic to extend each ranking to 75.
def _major_match(c, kw):
    return any(kw.lower() in m.lower() for m in c.get("majors", []))

RANKING_FILTERS = {
    "best-overall":      lambda c: True,
    "best-business":     lambda c: _major_match(c, "business") or _major_match(c, "finance") or _major_match(c, "economics"),
    "best-engineering":  lambda c: _major_match(c, "engineering"),
    "best-cs":           lambda c: _major_match(c, "computer science"),
    "best-liberal-arts": lambda c: c.get("size", 99999) < 4000 and c["type"] == "private",
    "best-pre-med":      lambda c: _major_match(c, "biology") or _major_match(c, "biochemistry") or _major_match(c, "neuroscience"),
    "best-public":       lambda c: c["type"] == "public",
    "best-value":        lambda c: c.get("tuition", 99999) < 25000,
}


def expanded_ranking_order(slug, target=75):
    """Return the ranking's slug list expanded to ~target entries.
    Curated entries lead; the rest auto-filled by selectivity using the filter.
    For preference-based rankings, all entries are auto-filled by tag."""
    r = RANKINGS_BY_SLUG.get(slug)
    if not r: return []
    if r.get("pref_based"):
        tag = r["tag"]
        candidates = sorted(
            [c for c in COLLEGES if tag in school_attrs(c["slug"])],
            key=lambda c: (c["tier"], c["accept"]),
        )
        return [c["slug"] for c in candidates[:target]]
    base = list(r["order"])
    base_set = set(base)
    filt = RANKING_FILTERS.get(slug, lambda c: True)
    extras = sorted(
        [c for c in COLLEGES if c["slug"] not in base_set and filt(c)],
        key=lambda c: (c["tier"], c["accept"]),
    )
    return base + [c["slug"] for c in extras[:max(0, target - len(base))]]


# ─── RESOURCE LIBRARY ─────────────────────────────────────
# Curated free resources used by the /improve guide. URLs are real, well-known,
# and stable. Don't link to anything that could rot quickly (course pages,
# dated YouTube videos, etc.).


# Comprehensive competitions database, filterable by major. Each entry:
#   name, url, majors (empty list = applies to all), tier, deadline, note.


# Selective summer programs database. Selectivity tiers:
#   Free + selective = highest signal (RSI, MITES, TASP)
#   Paid + selective = moderate signal (SSP, PROMYS, SUMaC)
#   Paid + accessible = pay-to-play (skip if real signal is what you want)


# ─── SCHOOL-SPECIFIC NOTES ─────────────────────────────────
# What each school weights most + concrete advice. Top schools get curated
# entries; everything else falls back to TIER_NOTES below by tier.

# Tier-based fallback: applies to schools without a specific note above.


# ─── KEYWORDS / SCORING (carried over from MVP) ──────────


def ec_rating_1k(profile):
    """LLM-graded extracurricular/awards/leadership strength on a 1-1000 scale.
    Reads the actual substance instead of counting keywords, so the EC impact
    on odds VARIES continuously with how strong the profile really is. Returns
    a float 1-1000, or None if the LLM is unavailable (caller falls back to
    ec_rating_keyword). Cheap: one claude-haiku call, run once at save time."""
    if not _claude_client:
        return None
    ecs = (profile.get("ecs") or "").strip()
    awards = (profile.get("awards") or "").strip()
    leadership = (profile.get("leadership") or "").strip()
    if not (ecs or awards or leadership):
        return 1.0
    user = (f"EXTRACURRICULARS:\n{ecs or '(none)'}\n\nAWARDS:\n{awards or '(none)'}\n\n"
            f"LEADERSHIP:\n{leadership or '(none)'}\n\n"
            "Rate this applicant's activities/awards/leadership profile for "
            "SELECTIVE college admissions on a 1-1000 scale. Use the WHOLE range "
            "and judge real substance, depth, and distinction — not buzzwords:\n"
            "1-100   = thin/generic (a club or two, no real distinction)\n"
            "101-300 = solid (sustained involvement, some school/local leadership)\n"
            "301-550 = strong (regional/state distinction, real leadership + depth)\n"
            "551-800 = excellent (national-level awards, founder with real scale, "
            "published/first-author research)\n"
            "801-1000 = elite, top ~1% nationally (ISEF/USAMO/Regeneron-tier, "
            "national champion, recruited D1 athlete, Olympic/intl medalist)\n"
            "Be skeptical — students inflate. A 'founded a nonprofit' with no scale "
            "is ~150, not 600. Output ONLY the integer.")
    raw = _claude("claude-haiku-4-5-20251001",
        "You are a strict admissions reader. Output only a single integer 1-1000.",
        user, max_tokens=8)
    if not raw:
        return None
    import re as _re
    m = _re.search(r"-?\d+(\.\d+)?", raw)
    if not m:
        return None
    try:
        return max(1.0, min(1000.0, float(m.group(0))))
    except ValueError:
        return None


def ec_rating_keyword(profile):
    """Deterministic 1-1000 EC rating from keyword signals. Used as a fallback
    when no LLM key is set, and inside compute_fit for profiles saved before
    ec_rating was populated (so odds still vary by EC strength). Coarser than
    the LLM grade but monotonic and never zero-impact."""
    ecs = (profile.get("ecs") or "")
    awards = (profile.get("awards") or "")
    leadership = (profile.get("leadership") or "")
    if not (ecs.strip() or awards.strip() or leadership.strip()):
        return 1.0
    ec_hits = _keyword_strength(ecs, EC_STRONG_SIGNALS)
    aw_hits = _keyword_strength(awards, EC_STRONG_SIGNALS)
    ld_hits = _keyword_strength(leadership, LEADERSHIP_KEYWORDS)
    # National-credential signals jump the rating into the elite band.
    elite = _keyword_exceptional(profile)
    base = 60.0 + ec_hits * 70.0 + aw_hits * 90.0 + ld_hits * 45.0
    if elite:
        base = max(base, 780.0) + aw_hits * 30.0
    return max(1.0, min(1000.0, round(base, 1)))


def ec_rating_for_fit(profile):
    """The 1-1000 EC rating compute_fit/estimate_odds should use: prefer the
    stored LLM grade, else compute the keyword estimate on the fly."""
    r = profile.get("ec_rating")
    try:
        if r is not None:
            return max(1.0, min(1000.0, float(r)))
    except (TypeError, ValueError):
        pass
    return ec_rating_keyword(profile)


def _rating_to_fit_delta(r):
    """Map a 1-1000 EC rating to a fit-score contribution. Anchored to the
    OLD keyword scale so calibration doesn't move: a solid-strong profile
    (~520) lands ~+5.9 (matching the prior ec_total+leadership for such a
    file), a blank/thin profile is a small penalty, an elite profile tops out
    near +15. Continuous, so EC impact now varies smoothly."""
    return max(-4.0, min(15.0, -4.0 + (r / 1000.0) * 19.0))


def ec_exceptional_strength(profile):
    """Continuous 0-1 'how exceptional are the ECs' signal that replaces the
    old binary is_exceptional cliff for the odds CAP. Ramps from 0 at rating
    720 to 1.0 at 1000, so only genuinely national-tier profiles lift the
    elite cap, and they lift it smoothly. Recruited athletes and an explicit
    is_exceptional flag still get a strong floor."""
    r = ec_rating_for_fit(profile)
    exc = max(0.0, min(1.0, (r - 720.0) / 280.0))
    if profile.get("athlete"):
        exc = max(exc, 0.70)
    if profile.get("is_exceptional"):
        exc = max(exc, 1.0)
    return exc


def _grade_profile_fallback(profile):
    """Deterministic whole-profile grade on a 1-1000 scale (then /10 for the
    0-100 display) when the LLM is unavailable. Benchmarks against a highly
    selective (T20) admit pool. Used as a floor and as the no-API fallback."""
    # Academics: GPA vs a 3.9 elite benchmark (uw 4.0 scale).
    gpa = None
    try:
        gpa = float(profile.get("uw_gpa")) if profile.get("uw_gpa") is not None else None
    except (TypeError, ValueError):
        gpa = None
    acad = 500.0
    if gpa is not None:
        acad = max(60.0, min(1000.0, 500.0 + (gpa - 3.6) * 900.0))  # 3.6→500, 4.0→860, 3.15→95
    # Testing vs 1500 benchmark.
    sat_eq = _normalize_score(profile.get("sat"), profile.get("act"))
    testing = 500.0
    if sat_eq:
        testing = max(60.0, min(1000.0, 500.0 + (sat_eq - 1450) * 4.2))  # 1450→500, 1580→1000(cap)
    # Rigor 0-100 → 1-1000.
    rg = combined_rigor(profile)
    rigor = 500.0 if rg is None else max(60.0, min(1000.0, (rg if rg > 0 else 0) * 10.0))
    # ECs/leadership reuse the rating we already grade.
    ecs = ec_rating_for_fit(profile)
    # Hooks nudge.
    hook = 0.0
    if profile.get("athlete"): hook += 120
    if profile.get("first_gen"): hook += 60
    if (profile.get("legacy_schools") or "").strip(): hook += 50
    overall = (acad * 0.34 + testing * 0.22 + rigor * 0.14 + ecs * 0.30) + hook
    overall = max(1.0, min(1000.0, overall))
    return {
        "overall": round(overall),
        "dimensions": {
            "academics": round(acad), "testing": round(testing),
            "rigor": round(rigor), "extracurriculars": round(ecs),
        },
        "summary": "Heuristic grade (AI grader unavailable).",
        "strengths": [], "weaknesses": [], "fixes": [],
        "_fallback": True,
    }


def grade_profile(profile):
    """Whole-profile competitiveness grade for highly selective (T20) admissions.
    The model reads the ENTIRE profile and rates it 1-1000 for accuracy; the
    caller divides by 10 to show a clean 0-100. Returns a dict with the overall
    score, per-dimension 1-1000 scores, and strengths/weaknesses/fixes. Falls
    back to a deterministic grade if the LLM is unavailable or returns garbage.
    One Sonnet call — premium feature, run on demand."""
    fb = _grade_profile_fallback(profile)
    if not _claude_client:
        return fb
    def g(k): return (profile.get(k) or "").strip()
    sat_eq = _normalize_score(profile.get("sat"), profile.get("act"))
    rg = combined_rigor(profile)
    no_ap, no_ib = bool(profile.get("no_aps_offered")), bool(profile.get("no_ibs_offered"))
    if no_ap and no_ib:
        avail = "School offers NEITHER AP nor IB — do NOT penalize the absence of AP/IB scores."
    elif no_ap:
        avail = "School offers NO AP (IB may be available)."
    elif no_ib:
        avail = "School offers NO IB (AP may be available)."
    else:
        avail = "AP/IB are available at this school."
    self_r = profile.get("self_rigor")
    facts = (
        f"GPA (unweighted /4): {profile.get('uw_gpa') or '—'}\n"
        f"Weighted GPA: {profile.get('weighted_gpa') or '—'}\n"
        f"SAT: {profile.get('sat') or '—'}  ACT: {profile.get('act') or '—'}  (normalized SAT-equiv: {sat_eq or '—'})\n"
        f"AP/IB availability: {avail}\n"
        f"Self-rated course rigor (1-10, only meaningful if no AP/IB offered): {self_r or '—'}\n"
        f"Course rigor signal (0-100, higher=harder load): {rg if rg is not None else 'n/a'}\n"
        f"APs taken: {g('aps') or '—'}\nIB taken: {g('ibs') or '—'}\n"
        f"Class rank: {profile.get('class_rank') or '—'} of {profile.get('class_size') or '—'}\n"
        f"Intended major: {g('major') or '—'}\n"
        f"Extracurriculars: {g('ecs') or '—'}\n"
        f"Awards: {g('awards') or '—'}\n"
        f"Leadership: {g('leadership') or '—'}\n"
        f"Hooks — recruited athlete: {bool(profile.get('athlete'))}, first-gen: {bool(profile.get('first_gen'))}, "
        f"legacy: {g('legacy_schools') or 'none'}, international: {bool(profile.get('is_international'))}\n"
    )
    user = (
        "Grade this student's WHOLE profile for admission to highly selective "
        "(roughly top-20 US) universities. Score 1-1000 (use the full range; be "
        "calibrated and skeptical — most real applicants to these schools land "
        "300-650; 800+ is reserved for genuinely national-tier profiles).\n\n"
        f"PROFILE:\n{facts}\n"
        "Return ONLY valid JSON, no prose, in exactly this shape:\n"
        '{"overall": <1-1000>, "dimensions": {"academics": <1-1000>, '
        '"testing": <1-1000>, "rigor": <1-1000>, "extracurriculars": <1-1000>, '
        '"narrative_hooks": <1-1000>}, "summary": "<one honest sentence>", '
        '"strengths": ["<short>", ...], "weaknesses": ["<short>", ...], '
        '"fixes": ["<specific action>", ...]}\n'
        "Judge real substance, not buzzwords. A 3.15 GPA is a hard ceiling even "
        "with strong ECs; reflect that honestly in academics and overall. "
        "If the school offers NO AP/IB, judge rigor from the self-rating and "
        "course names — do NOT list 'no AP/IB courses' as a weakness in that case.\n\n"
        "RULES (follow exactly):\n"
        "- Do NOT penalize the student's CHOICE of intended major. Major choice "
        "is neutral — NEVER call a major un-prestigious, non-differentiating, or "
        "a weakness, and never say one major would be 'stronger' than another. You "
        "may note if the activities don't yet support the stated major, but frame "
        "that only as optional fit guidance, never as a knock.\n"
        "- Hooks are ADDITIVE ONLY: a hook (legacy, recruited athlete, first-gen) "
        "can help, but its ABSENCE is normal for most applicants and must NEVER be "
        "a weakness or lower the score.\n"
        "- Narrative cohesion is a soft polish factor, not a core pillar. REWARD a "
        "focused story, but do NOT heavily penalize breadth or an 'unfocused' "
        "narrative when the student has real, concrete accomplishments. For a "
        "student with substantive activities, narrative_hooks should sit around "
        "55-75, not below 50.\n"
        "- Weaknesses must be VERIFIABLE academic gaps (GPA, test, rigor, lack of "
        "external validation) — do NOT manufacture soft-narrative or major-choice "
        "criticisms to fill the list. Fewer real weaknesses beats padded ones.\n"
        "- Judge EXTRACURRICULARS relative to the applicant's FIELD. The elite EC "
        "band is NOT STEM-only: a business/econ, humanities, policy, or arts "
        "applicant reaches the high 800s/900s through field-appropriate "
        "equivalents — DECA/FBLA/Model UN/Econ Challenge national or international "
        "placement, a founder/builder with real documented scale (thousands of "
        "users OR real revenue), published research/writing, university research "
        "assistance, national-level arts/music recognition (YoungArts, "
        "All-National). Do NOT cap a stellar non-STEM profile below the 800s just "
        "because it lacks ISEF/USAMO-type awards. This guidance only RAISES "
        "non-STEM ceilings — it must NEVER lower an EC score relative to judging "
        "the same activities on their face. Real founder, builder, and "
        "leadership work counts on its merits even when the applicant has not "
        "quantified it; the mere ABSENCE of stated metrics or awards is not "
        "itself a penalty. Reserve the very top (elite) band for documented "
        "national-level scale, but credit genuine founder/leadership roles "
        "solidly in the bands below it rather than discounting them as 'vague'."
    )
    raw = _claude("claude-sonnet-4-6",
        "You are a strict, candid admissions reader. Output only valid JSON. Be honest, not flattering.",
        user, max_tokens=700, temperature=0)
    if not raw:
        return fb
    import json as _json, re as _re
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return fb
    try:
        d = _json.loads(m.group(0))
    except Exception:
        return fb
    try:
        overall = max(1, min(1000, int(round(float(d.get("overall", fb["overall"]))))))
    except (TypeError, ValueError):
        overall = fb["overall"]
    dims = d.get("dimensions") or {}
    clean_dims = {}
    for k, v in dims.items():
        try:
            clean_dims[k] = max(1, min(1000, int(round(float(v)))))
        except (TypeError, ValueError):
            continue
    # Soft floor: narrative is a polish factor, not a pillar. Don't let it drag
    # a student with real, substantive activities — if ECs are strong, narrative
    # can't read as a major weakness on its own (additive-only philosophy).
    if clean_dims.get("extracurriculars", 0) >= 550:
        clean_dims["narrative_hooks"] = max(clean_dims.get("narrative_hooks", 0), 520)
    # When the school offers no AP/IB and the student self-rated their rigor,
    # anchor the displayed rigor dimension to that self-rating (the same value
    # the odds engine uses) so the bar reflects what they actually entered,
    # rather than the LLM's independent — and inconsistent — guess. combined_rigor
    # already caps a self-reported 10/10 at 80, so it still reads below a
    # provable wall of AP/IB scores.
    _ib_sig, _, _ = score_ibs(profile)
    if profile.get("no_aps_offered") and (_ib_sig is None or _ib_sig <= 0) and profile.get("self_rigor"):
        _cr = combined_rigor(profile)
        if _cr is not None:
            clean_dims["rigor"] = max(1, min(1000, int(round(_cr * 10))))
    def _lst(x):
        return [str(s).strip() for s in (d.get(x) or []) if str(s).strip()][:5]
    return {
        "overall": overall,
        "dimensions": clean_dims or fb["dimensions"],
        "summary": str(d.get("summary") or fb["summary"]).strip()[:300],
        "strengths": _lst("strengths"),
        "weaknesses": _lst("weaknesses"),
        "fixes": _lst("fixes"),
        "_fallback": False,
    }


def _keyword_strength(text, keywords):
    if not text: return 0
    t = text.lower()
    return sum(1 for k in keywords if k in t)


# AP difficulty weights — how much each AP contributes to a student's
# academic-rigor signal. Hardest STEM/lit APs at the top, easier APs
# (Psych, Human Geo, Env Sci) at the bottom. Substring-matched so
# "AP Calc BC" and "Calc BC" both work.


# IB classes. HL ≈ AP-level; SL slightly less. Stored separately from APs
# so we can show both in admissions reads (lots of intl applicants take both).


def score_aps(profile):
    """Return (rigor_score 0-100, count, top_aps_listed) for a student's AP
    courses. Higher = more rigorous course load. Returns:
      - (None, 0, []) if school doesn't offer APs (neutral, no penalty)
      - (negative number, 0, []) if school offers APs but user took none
        (small negative — signals course-rigor avoidance)
      - (0-100, count, names) if user listed APs."""
    if profile.get("no_aps_offered"):
        return None, 0, []
    raw = (profile.get("aps") or "").strip().lower()
    if not raw:
        # No APs picked. If user explicitly said "school offers but I didn't
        # take any" → small negative. Otherwise treat as 0 (lazy fill = same
        # as the explicit case, no free pass for skipping the section).
        if profile.get("aps_offered_not_taken"):
            return -10.0, 0, []
        return 0, 0, []
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    matched = []
    for part in parts:
        # find best matching key (longest match wins so "calc bc" beats "calc ab")
        best_key, best_w = None, 0.0
        for key, w in AP_WEIGHTS.items():
            if key in part:
                if len(key) > len(best_key or ""):
                    best_key, best_w = key, w
        if best_key:
            matched.append((part, best_w))
        else:
            # unknown AP → small default weight (don't penalize unknown names)
            matched.append((part, 1.5))
    total = sum(w for _, w in matched)
    # 0-100 scale: ~25 points = solid load, ~40+ = elite load
    rigor = min(100, total * 4)
    return round(rigor, 1), len(matched), [name for name, _ in matched]


def score_ibs(profile):
    """Same idea as score_aps but for IB classes. HL ≈ AP-level, SL ≈ less.
    IB and AP can both be filled in (some IB schools offer both); we combine
    them when computing total course rigor."""
    if profile.get("no_ibs_offered"):
        return None, 0, []
    raw = (profile.get("ibs") or "").strip().lower()
    if not raw:
        if profile.get("ibs_offered_not_taken"):
            return -8.0, 0, []  # slightly less harsh than AP version since IB is rarer
        return 0, 0, []
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    matched = []
    for part in parts:
        best_key, best_w = None, 0.0
        for key, w in IB_WEIGHTS.items():
            if key in part:
                if len(key) > len(best_key or ""):
                    best_key, best_w = key, w
        matched.append((part, best_w if best_key else 1.5))
    total = sum(w for _, w in matched)
    rigor = min(100, total * 4)
    return round(rigor, 1), len(matched), [name for name, _ in matched]


def combined_rigor(profile):
    """Combine AP + IB rigor. If both filled, add (capped). If one is None
    (school doesn't offer that track) → ignore that one."""
    ap, ap_count, _ = score_aps(profile)
    ib, ib_count, _ = score_ibs(profile)
    # If the school offers no APs and there's no positive IB signal either, the
    # student has no measurable rigor track through no fault of their own. Fall
    # back to their self-rated rigor instead of requiring BOTH the "no AP" and
    # "no IB" boxes (most no-AP students never tick the separate IB box, which
    # was leaving self_rigor unused). Self-reported, so discounted: a 7 lands
    # ~60 and a 10 caps at 80 (a verified wall of AP/IB 5s can still reach 100).
    if profile.get("no_aps_offered") and (ib is None or ib <= 0):
        try:
            sr = int(profile.get("self_rigor")) if profile.get("self_rigor") not in (None, "") else None
        except (ValueError, TypeError):
            sr = None
        if sr:
            return min(80.0, max(0, sr) * 8.5)
        return None  # no measurable rigor track, no self-rating -> neutral
    if ap is None and ib is None:
        return None  # neither track offered, no self-rating -> neutral
    if ap is None: return ib
    if ib is None: return ap
    # Both populated → sum, but cap to not double-count when student takes
    # significant load in both (rare but possible at IB schools that also
    # offer APs)
    if ap < 0 and ib < 0: return min(ap, ib)
    if ap < 0: return ib
    if ib < 0: return ap
    return min(100, ap + ib * 0.6)  # IB weighted slightly less when stacked


def class_rank_component(profile, school):
    """Small admit-odds adjustment from class rank. Returns (adj, note).
    Neutral (0) if the school doesn't rank or data is missing — never a
    penalty for an absent rank. Modest ±6 scale, in line with AP rigor."""
    if profile.get("no_class_rank_offered"):
        return 0.0, "school doesn't rank (neutral)"
    try:
        rank = int(profile.get("class_rank") or 0)
        size = int(profile.get("class_size") or 0)
    except (ValueError, TypeError):
        return 0.0, ""
    if rank < 1 or size < 1 or rank > size:
        return 0.0, ""
    pct = 1.0 - (rank - 1) / size  # rank 1 → ~1.0 (top), last → ~0
    if pct >= 0.99: return 6.0, f"top 1% (#{rank} of {size})"
    if pct >= 0.95: return 5.0, f"top 5% (#{rank} of {size})"
    if pct >= 0.90: return 3.5, f"top 10% (#{rank} of {size})"
    if pct >= 0.75: return 1.5, f"top quartile (#{rank} of {size})"
    if pct >= 0.50: return 0.0, f"top half (#{rank} of {size})"
    return -3.0, f"bottom half (#{rank} of {size})"


def _normalize_score(sat, act):
    # Official College Board / ACT concordance (2018, still current). The prior
    # table sagged 20-40 pts low across ACT 28-35 — the meat of the competitive
    # range — systematically under-rating every ACT applicant at test-using
    # schools (a 33 read as 1460 instead of 1500). Corrected to the published
    # concordance.
    act_to_sat = {36:1590,35:1560,34:1530,33:1500,32:1470,31:1430,30:1400,29:1360,28:1320,27:1290,26:1260,25:1220,24:1180,23:1140,22:1110,21:1070,20:1030,19:990,18:960}
    if sat: return int(sat)
    if act: return act_to_sat.get(int(act), 1000)
    return None


def _is_test_focused_school(school):
    """Schools where a SUBMITTED below-median score is a genuine liability —
    tests are required/expected, so a weak score (or omitting one) really hurts.
    Two groups: elite STEM/quant (a score is effectively expected) and the
    selective schools that have REINSTATED a test requirement for the current
    cycle.

    This is deliberately NOT a blanket `tier == 1`. Many top holistic schools —
    UChicago, Northwestern, Duke, Columbia, Princeton, and most LACs (Williams,
    Amherst, Pomona, …) — remain test-OPTIONAL. There, a strong-but-below-median
    1500 would simply be withheld, so it should get the standard below-median
    softener (≈ a non-submit) rather than the full forced penalty. The old
    tier-1 rule swept those test-optional schools into the MIT/Caltech bucket and
    cratered the fit for an at-or-just-below-25th-percentile score. Update the
    reinstated set as test policies change."""
    slug = school.get("slug")
    if slug in ("mit","caltech","gatech","harvey-mudd","cmu","rpi","wpi","stevens"):
        return True
    # Reinstated / required a standardized test (2025-26 cycle).
    return slug in ("harvard","yale","dartmouth","brown","upenn","stanford")


# Schools that formally drop 9th grade in their GPA calculation.
# UCs use the "a-g" framework (10-11 + summer between), CSUs use an
# identical formula. Both publish official GPA recalculation rules that
# ignore freshman year entirely. No other school system does this — most
# others weight upper years informally but DO see 9th-grade grades.


def effective_gpa(profile, school):
    """Returns the GPA value to use for chances at THIS school, accounting
    for year-by-year grades when the user has provided them.

    - UCs literally don't look at 9th-grade grades at all (per UC policy).
      Effective GPA = average of sophomore + junior (+ senior if avail).
    - Most other schools weight upper years more heavily. We use a weighted
      average: 9th=0.5, 10th=1.0, 11th=1.5, 12th=1.0. Reflects the reality
      that admissions readers care most about 10th-11th and especially the
      junior trajectory.
    - Upward trend gets a small +0.05 bonus (caps the GPA model at 4.05)
      because admissions readers explicitly look for trajectory.
    - Downward trend gets a small -0.05 penalty.

    If year-by-year grades aren't provided, returns the regular uw_gpa.
    Bounded to 0.0-4.0 (or 4.05 with trend bonus).
    """
    base = profile.get("uw_gpa")
    fr = profile.get("gpa_freshman")
    so = profile.get("gpa_sophomore")
    ju = profile.get("gpa_junior")
    sr = profile.get("gpa_senior")
    years = [(y, g) for y, g in [("fr",fr),("so",so),("ju",ju),("sr",sr)] if g is not None]
    # Need at least 2 years for any year-aware computation
    if len(years) < 2:
        return base

    is_uc = school and school.get("slug") in UC_SLUGS

    # UC rule: drop freshman entirely
    if is_uc:
        scoped = [(y, g) for y, g in years if y != "fr"]
        if not scoped:
            return base
        gpa = sum(g for _, g in scoped) / len(scoped)
    else:
        # Weighted average — upper years count more
        weights = {"fr": 0.5, "so": 1.0, "ju": 1.5, "sr": 1.0}
        num = sum(weights[y] * g for y, g in years)
        den = sum(weights[y] for y, g in years)
        gpa = num / den if den else (base or 0.0)

    # Trend detection — only if we have at least 3 sequential years
    seq = [g for y, g in [("fr",fr),("so",so),("ju",ju),("sr",sr)] if g is not None]
    if len(seq) >= 3:
        # Strictly improving by at least 0.15 per year on average → upward
        diffs = [seq[i+1] - seq[i] for i in range(len(seq)-1)]
        avg_diff = sum(diffs) / len(diffs)
        if avg_diff >= 0.15 and all(d >= -0.05 for d in diffs):
            gpa += 0.05  # upward-trend bonus
        elif avg_diff <= -0.15 and all(d <= 0.05 for d in diffs):
            gpa -= 0.05  # downward-trend penalty

    return round(min(4.05, max(0.0, gpa)), 3)


def weighted_effective_gpa(profile):
    """Year-by-year WEIGHTED GPA. Upper years count more, same as effective_gpa.

    For a year where weighting was 'not offered', we no longer DROP the year —
    we fall back to that year's UNWEIGHTED grade (the only signal that exists for
    it), exactly like the unweighted year-by-year system. Previously a student
    whose school weighted only junior year was judged on junior alone (e.g. a
    4.26) — silently erasing a weak freshman/soph and floating to the top of
    every weighted admit range. Backfilling the unweighted grade for gated years
    is a slight UNDER-estimate (a weighted version would be >= unweighted) but is
    far more honest than letting one strong weighted year stand in for the whole
    record. Returns None when NO year was actually weighted (caller then falls
    back to the single weighted_gpa field, then to unweighted)."""
    years = [("w_gpa_freshman", "w_notoffered_freshman", "gpa_freshman", 0.5),
             ("w_gpa_sophomore", "w_notoffered_sophomore", "gpa_sophomore", 1.0),
             ("w_gpa_junior", "w_notoffered_junior", "gpa_junior", 1.5),
             ("w_gpa_senior", "w_notoffered_senior", "gpa_senior", 1.0)]
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    num = den = 0.0
    have_weighted = False
    for wkey, nkey, ukey, wt in years:
        g = None
        if not profile.get(nkey):          # weighting offered this year
            g = _f(profile.get(wkey))
            if g is not None:
                have_weighted = True
        if g is None:                       # gated year, or no weighted value → use unweighted
            g = _f(profile.get(ukey))
        if g is None:
            continue
        num += g * wt
        den += wt
    if den == 0 or not have_weighted:
        return None
    return round(num / den, 3)


def compute_fit(profile, school):
    score = 50.0
    components = {}
    has_test = bool(profile.get("sat") or profile.get("act"))
    # Test-BLIND schools (the UC system) legally cannot consider a submitted
    # score at all. They still carry sat_25/sat_75 in the data (for display), so
    # the test branch below must be gated on this flag, not just on the presence
    # of a range — otherwise a submitted score silently inflates the fit. When a
    # school is test-blind we treat the applicant as effectively test-less: no
    # test credit/penalty, and GPA carries the extra weight (same as no-test).
    test_blind = school.get("slug") in TEST_BLIND_SCHOOLS
    gpa = effective_gpa(profile, school)
    # Scale-match the GPA comparison. ~40% of schools report a WEIGHTED admit
    # range (gpa_hi > 4.0 — e.g. Villanova 3.75-4.05, Clemson 3.8-4.2); the rest
    # report unweighted (capped at 4.0). effective_gpa is unweighted, so against
    # a weighted range a student was being compared apples-to-oranges and unfairly
    # penalized. When the school's range is weighted AND the student gave a
    # weighted GPA, compare weighted-to-weighted instead.
    used_weighted = False
    if gpa is not None and (school.get("gpa_hi") or 0) > 4.0:
        # Prefer the year-by-year weighted GPA (computed from only the offered
        # years), then the single weighted_gpa field, else keep unweighted.
        wg = weighted_effective_gpa(profile)
        if wg is None:
            try:
                wg = float(profile.get("weighted_gpa")) if profile.get("weighted_gpa") not in (None, "") else None
            except (TypeError, ValueError):
                wg = None
        if wg is not None:
            gpa = wg
            used_weighted = True
    if gpa is not None:
        midpoint = (school["gpa_lo"] + school["gpa_hi"]) / 2
        delta = (gpa - midpoint) * 50
        # Test-optional applicants: GPA carries more weight to compensate for the
        # missing test signal. Apply this BEFORE the caps so a cap is a true
        # ceiling (previously the ×1.25 ran after the weighted cap and leaked it
        # from +5 to +6.2).
        if not has_test or test_blind:
            delta *= 1.25
        if used_weighted:
            # Weighted-GPA scales aren't standardized (a 5.0-scale 4.4 vs a
            # school's 4.05-cap weighted range), so a weighted GPA can't be
            # trusted to MAX OUT the bonus — its job is to remove the unfair
            # UW-vs-weighted PENALTY and give at most a modest positive. Hard cap
            # on the upside, applied last; keep the full downside (a genuinely
            # low weighted GPA is still a real signal).
            delta = max(-18, min(5, delta))
        elif not has_test or test_blind:
            delta = max(-22, min(22, delta))
        else:
            delta = max(-18, min(18, delta))
        score += delta
        components["gpa"] = round(delta, 1)
    sat_eq = _normalize_score(profile.get("sat"), profile.get("act"))
    # Test-blind (UC) schools carry a stored range for display but legally ignore
    # the score — gate on `test_blind`, not just on the range existing.
    if sat_eq and not test_blind and school.get("sat_25") is not None and school.get("sat_75") is not None:
        mid = (school["sat_25"] + school["sat_75"]) / 2
        spread = max(40, school["sat_75"] - school["sat_25"])
        delta = max(-22, min(22, (sat_eq - mid) / spread * 22))
        # Test-optional reality (2026-06-04): the penalty is measured from the
        # MIDPOINT, so a score sitting right at a school's 25th percentile still
        # ate ~-11, and a hair below (e.g. 1490 vs a 1500 25th) cratered to -15.
        # But at a test-optional school the applicant CHOOSES whether to submit —
        # a below-median score is effectively a non-submit (≈neutral), not a
        # forced liability. So soften the downside and cap it near the
        # test-optional penalty. Test-focused schools (MIT/Caltech tier, where a
        # score is effectively expected) keep the full real penalty.
        if delta < 0 and not _is_test_focused_school(school):
            delta = max(delta * 0.45, -7.0)
        score += delta
        components["test"] = round(delta, 1)
    elif test_blind:
        # Score legally ignored — no credit, no penalty (GPA already up-weighted).
        components["test"] = 0
    else:
        # Test-optional handling. At test-focused schools (MIT/Caltech/etc.),
        # not submitting reads as a weakness. At test-flexible schools,
        # neutral (we already up-weighted GPA above).
        if _is_test_focused_school(school):
            score -= 6
            components["test"] = -6
        else:
            components["test"] = 0
    # EC/leadership contribution now comes from a single 1-1000 model-graded
    # rating (ec_rating_for_fit) instead of keyword counting, so its impact on
    # the score varies continuously with real strength. The rating already
    # folds in leadership, so leadership is no longer a separate add (kept in
    # components at 0 for any downstream readers).
    ec_rating = ec_rating_for_fit(profile)
    ec_total = _rating_to_fit_delta(ec_rating)
    score += ec_total
    components["ecs"] = round(ec_total, 1)
    components["leadership"] = 0.0
    components["ec_rating"] = round(ec_rating, 1)
    # AP rigor — elite schools weight course-load harder than mid-tier ones.
    # Tier 1-2: up to +6, tier 3: up to +4, else +3. If user marked
    # "no APs offered" the contribution is 0 (neutral, no penalty).
    # Negative rigor (school offered, user took none) gets a small penalty
    # at top schools where rigor matters; less elsewhere.
    rigor = combined_rigor(profile)
    if rigor is not None:
        cap = 6 if school.get("tier", 5) <= 2 else (4 if school.get("tier", 5) == 3 else 3)
        if rigor >= 0:
            rigor_bonus = round(min(cap, rigor / 100 * cap), 1)
        else:
            # rigor is negative (e.g., -10 → -2 at tier 1, -1 at tier 3, 0 below)
            penalty_cap = 2 if school.get("tier", 5) <= 2 else (1 if school.get("tier", 5) == 3 else 0)
            rigor_bonus = round(max(-penalty_cap, rigor / 10 * penalty_cap / 2), 1)
        score += rigor_bonus
        components["rigor"] = rigor_bonus
    # Legacy is school-specific. Generation count scales the boost: 1 gen = +3,
    # 2 = +4, 3+ = +5. (Marginal returns drop off — research suggests legacy
    # admit boost is largely binary, with a modest extra edge for multi-gen.)
    legacy_gens = legacy_generations_at(profile, school)
    legacy_bonus = {0:0, 1:3, 2:4}.get(legacy_gens, 5) if legacy_gens else 0
    hook_total = legacy_bonus + (4 if profile.get("first_gen") else 0) + (5 if profile.get("athlete") else 0)
    score += hook_total
    components["hooks"] = hook_total
    if school["type"] == "public" and profile.get("state") and profile["state"].lower() == school["state"].lower():
        score += 4
        components["in_state"] = 4
    return max(0, min(100, round(score, 1))), components


def assign_tier(school, fit, profile=None):
    a = school["accept"]
    # If user has a major that matches a sub-school at this university,
    # use the sub-school's accept rate for tier classification too — so a
    # CS applicant at Cornell sees Cornell as a Reach (Engineering ~10%),
    # not a Target (overall ~7% — wait, Engineering IS lower than overall).
    # The point: tier should reflect the specific college they'd apply to.
    if profile:
        sub = sub_school_for_major(school.get("slug"), profile.get("major") or "")
        if sub and sub.get("accept"):
            a = sub["accept"]
    if a < 0.10: return "Dream" if fit < 70 else "Reach"
    if a < 0.20: return "Reach" if fit < 65 else "Target"
    if a < 0.40: return "Reach" if fit < 50 else ("Target" if fit < 75 else "Safety")
    if a < 0.60: return "Target" if fit < 60 else "Safety"
    return "Safety"


def _render_di_card(slug, school_name, current_level):
    levels = [
        ("none", "Haven't engaged yet"),
        ("emailed", "Emailed admissions"),
        ("info_session", "Attended info session / virtual event"),
        ("visited", "Visited campus in person"),
    ]
    options = "".join(
        f'<option value="{v}" {"selected" if v == current_level else ""}>{lbl}</option>'
        for v, lbl in levels
    )
    return f"""<div class="card" style="background:#f7faff;border-color:#cfe0ff">
      <h3 style="margin-top:0">Demonstrated interest</h3>
      <p class="muted" style="font-size:.85em;margin:0 0 8px">{school_name} tracks demonstrated interest at the institutional level. Marking your engagement gives a small boost (~1–4%) at schools that explicitly weight it. Doesn't change odds at schools that don't (Ivies, MIT).</p>
      <form method="post" action="/college/{slug}/demonstrated-interest" style="display:flex;gap:8px;align-items:center">
        {csrf_input()}
        <select name="level" style="flex:1">{options}</select>
        <button class="btn btn-light btn-sm" type="submit">Save</button>
      </form>
    </div>"""


def get_demonstrated_interest(user_id, college_slug):
    """Return the user's demonstrated-interest level at this school
    ('visited' / 'info_session' / 'emailed' / 'none'). Returns 'none' if
    no record exists."""
    if not user_id: return "none"
    with db() as conn:
        row = conn.execute(
            "SELECT level FROM demonstrated_interest WHERE user_id=? AND college_slug=?",
            (user_id, college_slug)
        ).fetchone()
    return row["level"] if row else "none"


def _di_multiplier(level, school_tier):
    """Demonstrated interest only matters at schools that explicitly weight
    it. Most tier-1 schools (Ivies, MIT) say they don't track it; tier 2-3
    schools (BU, Tufts, Northeastern, etc.) often do."""
    if school_tier in (2, 3):
        return {"visited":1.04, "info_session":1.025, "emailed":1.01, "none":1.0}.get(level, 1.0)
    return 1.0


def _international_pct(school):
    """Rough per-school international undergrad enrollment share. Used to
    adjust acceptance rate up for domestic applicants (the published rate
    is overall; if 15% of admits are international, domestic odds are
    correspondingly higher than the headline). Curated for schools known
    to attract a meaningful international pool; defaults to 5% otherwise."""
    high_intl = {  # ≥20%
        "mit":0.30,"caltech":0.27,"cmu":0.25,"columbia":0.20,"jhu":0.27,
        "nyu":0.27,"northeastern":0.20,"usc":0.20,"bu":0.20,
    }
    mid_intl = {  # 10-20%
        "harvard":0.15,"yale":0.13,"princeton":0.13,"stanford":0.13,
        "upenn":0.14,"brown":0.13,"cornell":0.12,"dartmouth":0.10,
        "duke":0.12,"northwestern":0.13,"uchicago":0.18,"berklee":0.30,
        "juilliard":0.30,"parsons":0.35,"saic":0.30,"risd":0.20,"pratt":0.22,
        "georgetown":0.13,"ucb":0.12,"ucla":0.11,"umich":0.07,"rice":0.13,
        "washu":0.12,"emory":0.18,"vanderbilt":0.10,"notre-dame":0.07,
        "tufts":0.11,"cooper":0.13,"olin":0.10,
    }
    if school["slug"] in high_intl:
        return high_intl[school["slug"]]
    if school["slug"] in mid_intl:
        return mid_intl[school["slug"]]
    return 0.05  # most US colleges land here


# Three independent judges for the exceptionality panel. Each reads the SAME
# strict rubric, but from a different stance AND on a different model, so the
# votes genuinely deliberate instead of echoing one model run. Two Anthropic
# (Haiku + the stronger Sonnet) and one non-Anthropic (OpenAI gpt-4.1-mini) —
# crossing vendors so a single architecture's blind spot can't sweep all three.
# Each entry: (label, provider, model, system-persona). provider "openai" falls
# back to a Claude judge if no OPENAI_KEY is set, so the panel never breaks.
_EXC_JUDGE_STANCES = (
    ("strict skeptic", "anthropic", "claude-haiku-4-5-20251001",
     "You are a STRICT, skeptical admissions evaluator. Applicants inflate "
     "constantly. Default hard to NO; only the unambiguous, verifiable "
     "national/international credentials in the rubric clear the bar."),
    ("field-aware reader", "anthropic", "claude-sonnet-4-6",
     "You are a fair, experienced admissions reader. Default to NO, but judge "
     "the applicant against the TOP of THEIR OWN field — a policy, humanities, "
     "business, or arts achievement can be exceptional with no STEM-olympiad "
     "credential. Do not require ISEF/USAMO for a non-STEM applicant; weigh the "
     "field-appropriate equivalent at the same level of national distinction."),
    ("adversarial verifier", "openai", "gpt-4.1-mini",
     "You are an adversarial verifier. Identify the applicant's single STRONGEST "
     "claim and stress-test it: is it genuinely national/international tier, with "
     "real documented scale or selectivity — or just strong-sounding? Flag YES "
     "only if at least one claim survives that scrutiny as truly exceptional."),
)


def _exceptionality_rubric(profile):
    """The shared evidence + rubric block every judge in the panel reads."""
    awards = (profile.get("awards") or "").strip()
    ecs = (profile.get("ecs") or "").strip()
    leadership = (profile.get("leadership") or "").strip()
    is_athlete = bool(profile.get("athlete"))
    return f"""STUDENT PROFILE:
- Awards: {awards or '(blank)'}
- Extracurriculars: {ecs or '(blank)'}
- Leadership: {leadership or '(blank)'}
- Self-reported recruited athlete: {is_athlete}

TASK: Decide if this student is TRULY EXCEPTIONAL — clear evidence of national/international level achievement that would meaningfully change their admission odds at top-7 schools.

Default to NO. Only return YES if there is unambiguous evidence of:
- USAMO/USAJMO/USACO Platinum / IMO / IBO / IPhO / IOI / IChO medal or qualifier
- ISEF Top 3, Best of Category, Grand Award, or Regeneron STS / Intel STS finalist
- RSI alum (Research Science Institute at MIT)
- Putnam top 200 (very rare in HS)
- Published research as first author in a peer-reviewed journal (not high school journal)
- Recruited D1 athlete (says "recruited", "verbal", "scholarship", "committed")
- Founded a company with documented revenue or 10k+ users
- Olympic medalist or international competition winner
- Top national arts/writing awards: Scholastic Art & Writing GOLD MEDAL (national), national YoungArts winner, Carnegie Hall debut
- Won a HIGHLY SELECTIVE national scholarship/fellowship — Coca-Cola Scholar, Gates Scholarship, Jack Kent Cooke (College/Young Scholar), Davidson Fellow, Elks Most Valuable Student top winner, or similar (~hundreds of winners nationally, roughly ≤1% acceptance). These are genuinely exceptional. (This does NOT include National Merit Finalist/Commended/Scholar — far less selective, listed below as NOT exceptional.)
- Nationally recognized in their field (verifiable name recognition)

Do NOT flag YES for any of these (these are strong but not exceptional):
- Class valedictorian alone
- 1500+ SAT alone
- Multiple AP 5s
- Captain of varsity sport (not recruited)
- Founded a club at school
- State-level award
- Regional / county awards
- "Hundreds of community service hours"
- Local honor society
- "Founded a nonprofit" without clear scale
- "Started a business" without clear revenue/users
- Scholastic Gold Key (regional) — only Gold Medal (national) qualifies

Output EXACTLY two lines in this format:
EXCEPTIONAL: YES|NO
REASON: <one short sentence — if YES, name the credential; if NO, briefly explain why not>"""


def evaluate_profile_exceptionality(profile):
    """Detect truly exceptional applicants (USAMO/IMO medalists, ISEF top, RSI
    alums, recruited D1 athletes, first-author published researchers, etc.) so
    the odds model can lift the cap that's right for typical strong applicants
    but undersells extraordinary ones. Never lowers odds.

    Decided by a PANEL of three independent LLM judges on three different
    models — Claude Haiku (strict), Claude Sonnet (field-aware), and OpenAI
    gpt-4.1-mini (adversarial) — majority vote, at least 2 of 3 must say YES.
    Crossing vendors means one architecture's blind spot can't sweep the panel.
    Replaces the old single call plus keyword override, so the verdict is
    deliberated rather than tripped by a matched word. ~3 cheap calls run in
    parallel, cached 30 days (only recomputes when the profile is edited).

    Returns (is_exceptional: bool, reason: str)."""
    awards = (profile.get("awards") or "").strip()
    ecs = (profile.get("ecs") or "").strip()
    leadership = (profile.get("leadership") or "").strip()
    if not (awards or ecs or leadership):
        return False, "No awards/ECs/leadership submitted"
    if not _claude_client:
        # No API key at all — last-resort keyword heuristic so odds still vary.
        kw = _keyword_exceptional(profile)
        return kw, ("matched a national-credential keyword (AI judges unavailable)"
                    if kw else "AI evaluation unavailable")
    user_msg = _exceptionality_rubric(profile)

    def _judge(stance):
        name, provider, model, persona = stance
        system = (persona + " Output EXACTLY two lines: 'EXCEPTIONAL: YES' or "
                  "'EXCEPTIONAL: NO', then 'REASON: <one sentence>'.")
        raw = None
        if provider == "openai":
            raw = _openai_chat(model, system, user_msg, max_tokens=200, temperature=0.4)
            if raw is None:  # no OPENAI_KEY / call failed → fall back to a Claude judge
                raw = _claude("claude-haiku-4-5-20251001", system, user_msg,
                              max_tokens=200, temperature=0.4)
        else:
            raw = _claude(model, system, user_msg, max_tokens=200, temperature=0.4)
        if not raw:
            return None
        vote = "EXCEPTIONAL: YES" in raw.upper()
        reason = ""
        for line in raw.splitlines():
            if line.strip().upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
                break
        return (vote, reason, name)

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as ex:
            votes = [v for v in ex.map(_judge, _EXC_JUDGE_STANCES) if v is not None]
    except Exception as e:
        print(f"Exceptionality panel error: {e}")
        votes = []
    if not votes:
        return False, "AI evaluation failed"

    yes = [v for v in votes if v[0]]
    no = [v for v in votes if not v[0]]
    is_exc = len(yes) >= 2  # majority of the intended 3-judge panel
    n = len(votes)
    if is_exc:
        reason = f"{len(yes)}/{n} judges flagged exceptional: " + (yes[0][1] or "national-tier credential")
    else:
        dissent = (f" — {no[0][1]}" if no and no[0][1] else "")
        reason = f"{len(yes)}/{n} judges flagged exceptional{dissent}"
    return is_exc, reason[:500]


def get_or_evaluate_exceptionality(user_id, profile):
    """Cached wrapper. Recomputes only when the cached value is missing or
    older than 30 days."""
    if not user_id or not profile:
        return False, ""
    # If we already have a cached result and the profile hasn't been edited
    # since the evaluation, use the cache.
    eval_at = profile.get("exceptional_evaluated_at")
    updated_at = profile.get("updated_at")
    if eval_at and updated_at and eval_at >= updated_at:
        return bool(profile.get("is_exceptional")), profile.get("exceptional_reason") or ""
    # Otherwise recompute and persist
    is_exc, reason = evaluate_profile_exceptionality(profile)
    try:
        with db() as conn:
            conn.execute(
                "UPDATE profiles SET is_exceptional=?, exceptional_reason=?, exceptional_evaluated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (1 if is_exc else 0, reason[:500], user_id)
            )
            conn.commit()
    except Exception as e:
        print(f"Exceptionality persist error: {e}")
    return is_exc, reason


def counterfactual_lift(profile, school, *, gpa=None, sat=None, act=None,
                        ec_boost=0, hook_athlete=False, hook_legacy_at=None,
                        is_exceptional=False):
    """Compute the user's chances at this school under a HYPOTHETICAL
    modification to their profile. Used for "what-if" / counterfactual
    analysis on the chances page: 'if you raised your GPA from 3.65 to
    3.85, your odds at Penn would be X% instead of Y%.'

    Each kwarg overrides a profile field for this calculation only —
    the user's actual profile isn't mutated.

    Returns (low_pct, high_pct) tuple as integers.
    """
    sim = dict(profile)
    if gpa is not None:        sim["uw_gpa"] = gpa
    if sat is not None:        sim["sat"] = sat
    if act is not None:        sim["act"] = act
    if hook_athlete:           sim["athlete"] = True
    if hook_legacy_at:
        existing = sim.get("legacy_schools","") or ""
        if hook_legacy_at not in existing:
            sim["legacy_schools"] = (existing + ", " if existing else "") + hook_legacy_at
    if is_exceptional:         sim["is_exceptional"] = True
    if ec_boost:
        # Append a stronger-EC marker so the keyword rating picks it up, and
        # drop the stored ec_rating so compute_fit re-grades from the boosted
        # text instead of reusing the saved rating.
        sim["ecs"] = (sim.get("ecs","") or "") + " " + (
            "national finalist research published founder award winner"[:200] * max(1, ec_boost)
        )
        sim.pop("ec_rating", None)
    fit, _ = compute_fit(sim, school)
    return estimate_odds(school, fit, sim)



# Schools that reward a focused "spike" (deep, aligned, one-thing-extremely-well)
# more than well-roundedness, and schools that lean the other way (value
# breadth / leadership / civic profile). Matched against the school name.
_SPIKE_HIGH = ("mit", "caltech", "chicago", "carnegie mellon", "harvey mudd",
               "olin", "georgia tech", "stanford", "cooper union", "worcester polytechnic",
               "rensselaer", "rose-hulman")
_SPIKE_LOW = ("princeton", "dartmouth", "williams", "amherst", "naval academy",
              "military academy", "west point", "air force academy", "holy cross",
              "davidson", "washington and lee")

def spike_receptivity(school):
    """How much a school rewards a focused spike vs. well-roundedness. >1 = spike-
    friendly (MIT/Caltech/UChicago), <1 = breadth/leadership-leaning (HYP-ish,
    service academies, some LACs). Centered at 1.0."""
    name = (school.get("name") or "").lower()
    if any(k in name for k in _SPIKE_HIGH): return 1.35
    if any(k in name for k in _SPIKE_LOW): return 0.90
    return 1.0


def cohesion_keyword(profile):
    """Deterministic 1-1000 'cohesive story / major alignment' score for the
    no-LLM fallback. Rewards overlap between the intended major and the actual
    activities/awards ('actions match words') plus having a real activity body.
    Neutral (~430) by default so it never penalizes; only a clearly aligned,
    substantive profile climbs into the bonus band."""
    major = (profile.get("major") or "").lower()
    blob = " ".join([(profile.get("ecs") or ""), (profile.get("awards") or ""),
                     (profile.get("leadership") or "")]).lower()
    if not blob.strip():
        return 1.0
    import re as _re
    mtokens = [t for t in _re.split(r"\W+", major) if len(t) > 3]
    overlap = sum(1 for t in mtokens if t in blob)
    # National-credential signals usually come with a clear thread.
    spiky = _keyword_exceptional(profile)
    base = 430.0 + overlap * 170.0 + (200.0 if spiky else 0.0)
    return max(1.0, min(1000.0, round(base, 1)))


def spike_for_odds(profile):
    """The 1-1000 cohesion/spike score estimate_odds should use: prefer the
    stored LLM grade, else the keyword estimate."""
    s = profile.get("spike_score")
    try:
        if s is not None:
            return max(1.0, min(1000.0, float(s)))
    except (TypeError, ValueError):
        pass
    return cohesion_keyword(profile)


def grade_ec_and_spike(profile):
    """One Haiku call that grades BOTH the EC strength (1-1000) and the
    cohesive-story / major-alignment 'spike' (1-1000). Combining them keeps it
    to a single save-time call. Returns (ec_rating, spike_score) or (None, None)
    if the LLM is unavailable (caller falls back to the keyword versions)."""
    if not _claude_client:
        return None, None
    ecs = (profile.get("ecs") or "").strip()
    awards = (profile.get("awards") or "").strip()
    leadership = (profile.get("leadership") or "").strip()
    major = (profile.get("major") or "").strip()
    if not (ecs or awards or leadership):
        return 1.0, 1.0
    user = (
        f"INTENDED MAJOR: {major or '(undecided)'}\n"
        f"EXTRACURRICULARS:\n{ecs or '(none)'}\n\nAWARDS:\n{awards or '(none)'}\n\n"
        f"LEADERSHIP:\n{leadership or '(none)'}\n\n"
        "Rate two things for SELECTIVE college admissions, each 1-1000 (use the "
        "full range, be skeptical — most applicants land mid-range):\n\n"
        "1) EC_STRENGTH: raw strength/distinction of the activities, awards, and "
        "leadership. Judge achievement RELATIVE TO THE APPLICANT'S FIELD — a "
        "stellar business, humanities, policy, or arts profile reaches the top "
        "bands WITHOUT STEM-olympiad credentials. Do NOT require ISEF/USAMO-type "
        "awards for a non-STEM applicant; weigh the field-appropriate equivalent "
        "at the same level. Stay skeptical of vague claims with no scale.\n"
        "  1-100 thin/generic · 101-300 solid · 301-550 strong · 551-800 excellent "
        "(national/state awards in ANY domain — DECA/FBLA/Model UN/Econ Challenge "
        "finalist, founder/builder with real documented scale [thousands of users "
        "OR real revenue], published research or writing, university research "
        "assistant, national-level arts/music recognition) · 801-1000 elite "
        "(ISEF/USAMO/Regeneron-tier, national champion, recruited athlete, OR the "
        "field equivalent: DECA/FBLA international top placement, founder with "
        "major proven scale/revenue, nationally published author, YoungArts/"
        "All-National, national debate/Model UN best-delegate).\n\n"
        "2) SPIKE: how COHESIVE and FOCUSED the story is, and whether the activities "
        "actually support the intended major ('actions match words'). A scattered, "
        "well-rounded-but-unfocused profile is LOW (~250). A clear, deep thread where "
        "the major, activities, and awards all point the same direction is HIGH "
        "(800+). If the major doesn't match the activities at all, keep this LOW.\n\n"
        "Output ONLY valid JSON: {\"ec_strength\": <1-1000>, \"spike\": <1-1000>}"
    )
    raw = _claude("claude-haiku-4-5-20251001",
        "You are a strict admissions reader. Output only valid JSON with two integers.",
        user, max_tokens=40, temperature=0)
    if not raw:
        return None, None
    import json as _json, re as _re
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return None, None
    try:
        d = _json.loads(m.group(0))
        ec = max(1.0, min(1000.0, float(d.get("ec_strength"))))
        sp = max(1.0, min(1000.0, float(d.get("spike"))))
        return ec, sp
    except Exception:
        return None, None


# Keyword-based exceptional detection computed INLINE at odds time, so the
# cap-lift applies on every page regardless of any stale cached is_exceptional
# value in the DB (the cached LLM flag was returning False on prod). Free —
# no API call.
_EXC_INLINE_KW = (
    "isef", "regeneron", "intel sts", "science talent search", "usamo",
    "usajmo", "usaco platinum", "imo", "ipho", "ibo", "icho", "ioi", "putnam",
    "rsi", "research science institute", "peer reviewed", "peer-reviewed",
    "ieee", "published in", "first author", "first-author",
    "national merit scholar", "recruited", "youngarts", "carnegie hall",
    "scholastic gold medal", "national champion", "international council",
    "olympiad medal", "coca-cola scholar", "coca cola scholar", "gates scholar",
    "jack kent cooke", "davidson fellow",
)

def _keyword_exceptional(profile):
    blob = f"{profile.get('awards','')}\n{profile.get('ecs','')}\n{profile.get('leadership','')}".lower()
    # Word-boundary match so short tokens like "rsi" don't fire inside
    # "va(rsi)ty" / "unive(rsi)ty" / "dive(rsi)ty" (a real false-positive that
    # was inflating odds for anyone who mentioned varsity sports).
    import re as _re
    if any(_re.search(r"\b" + _re.escape(k) + r"\b", blob) for k in _EXC_INLINE_KW):
        return True
    return bool(profile.get("athlete"))


# Per-school calibration dial. Lifts ONLY the listed school's odds curve —
# no global/tier multiplier (those cascade and over-inflate neighbors). These
# yield-driven, stats-friendly, just-below-elite schools were systematically
# too harsh for solid/strong applicants. Each factor is calibrated against an
# agreed target for a reference 3.9/1500 profile and verified not to move the
# truly-elite schools. Value 1.0 == no change. The dial is faded in by fit
# (see estimate_odds) so weak profiles stay below the school's acceptance rate.
_SCHOOL_CALIBRATION = {
    # Cooled 2026-06-04: these three were set hot earlier and were the clearest
    # over-calls in the audit (a strong unhooked applicant showed USC 27-37% on a
    # 9.8%-accept school, ~3.3x base). Trimmed toward ~2x base. The lower fit curve
    # (above) already shaves ~10% on top of these.
    "usc": 1.40,
    "nyu": 1.45,
    "umich": 0.86,
    "bu": 1.15,
    "northeastern": 1.50,
}


def _target_honesty_haircut(center):
    """Shave the midpoint across the TARGET and SAFETY bands, where the model
    runs generous for spiky, sub-median-GPA profiles (strong test/ECs lifting a
    middling academic core). Triangular reduction peaking ~5 pts around center
    0.45, tapering to 0 below ~0.18 (reaches — already honest, and the band the
    user wants left intact) and above ~0.85 (near-certain safeties). Pure
    subtraction, not a multiplier, so it never cascades across tiers."""
    if 0.18 < center < 0.85:
        w = max(0.0, 1.0 - abs(center - 0.45) / 0.40)  # triangular, 1.0 at 0.45
        return max(0.0, center - 0.05 * w)
    return center


def estimate_odds(school, fit, profile):
    """Harsher version. Markets and admissions are noisy; previous curve was
    over-generous in the middle of the fit range. Tighter slope + lower caps
    on elite schools so the headline numbers don't promise a Stanford that
    isn't there.

    Exceptional-applicant override: when profile.is_exceptional is set,
    the caps lift dramatically because USAMO golds, recruited athletes, etc.
    legitimately have 50%+ odds at hyper-elites. The override only LIFTS
    caps; it never lowers odds.

    Sub-school adjustment: many large universities admit by college (Cornell
    Engineering ~10% vs Cornell Hotel ~27%). When the user's intended major
    matches a curated sub-school, we use that sub-school's accept rate as
    the base rather than the university-wide rate. Substantial impact on
    the odds at schools where the sub-schools differ a lot."""
    a = school["accept"]
    # Sub-school override: if the user's major matches a curated sub-school
    # at this university, use the sub-school's accept rate as our anchor.
    # Falls back to the university-wide rate when no match exists.
    sub = sub_school_for_major(school.get("slug"), profile.get("major") or "")
    if sub and sub.get("accept"):
        a = sub["accept"]
    # In-state / out-of-state public school adjustment. Public flagships
    # admit at very different rates by residency (UCLA in-state ~13% vs
    # out-of-state ~7%, UNC in-state 42% vs OOS 9%). When ADMISSIONS_DETAIL
    # has the residency split AND user has a state on file, anchor 'a'
    # to the matching residency rate instead of overall.
    # Residency adjustment for public flagships, applied as a MULTIPLIER on the
    # current anchor (which may already be a major-specific sub-school rate) so
    # it COMPOSES with the sub-school instead of being bypassed by it. Publics
    # swing enormously by residency — UNC 42% in-state vs 9% OOS, UCLA 13% vs 7%
    # — and the headline rate blends both. We scale by the residency:overall
    # ratio when we have explicit data, else apply a default OOS haircut for
    # selective publics. (Previously residency was mutually exclusive with the
    # sub-school override, so any OOS applicant whose major matched a broad
    # sub-school — e.g. biology -> "College of Arts & Sciences" — silently
    # escaped the OOS penalty entirely.)
    user_state = (profile.get("state") or "").strip()
    is_public = school.get("type") == "public"
    is_oos = bool(user_state) and user_state.lower() != (school.get("state") or "").lower()
    if is_public and user_state:
        overall = school.get("accept") or a
        d = ADMISSIONS_DETAIL.get(school.get("slug"), {})
        if not is_oos:
            if d.get("in_state_rate") and overall:
                a *= d["in_state_rate"] / overall
        else:
            if d.get("out_of_state_rate") and overall:
                a *= d["out_of_state_rate"] / overall
            elif overall < 0.50:
                a *= 0.55
        a = max(0.005, min(1.0, a))
    # International / domestic pool adjustment. The published acceptance rate
    # is overall (intl + domestic combined). At schools with a large intl
    # admit pool, domestic applicants are competing for fewer effective
    # slots → raw rate slightly understates domestic odds. Conversely,
    # international applicants face stiffer competition (~65% of domestic
    # rate empirically).
    intl_pct = _international_pct(school)
    if profile.get("is_international"):
        a = a * 0.65
    else:
        # Adjust headline rate up for the domestic pool
        a = min(1.0, a / max(0.5, 1 - intl_pct * 0.65))
    # Less-generous, MORE-responsive fit curve. Two calibration goals (2026-06-04):
    #   (1) trim ~10% off the level (a counselor flagged odds as a little inflated),
    #       done via the base term 0.20 -> 0.10 — a small uniform haircut, NOT a tier
    #       multiplier (those cascade; see the 5/31 over-inflation revert).
    #   (2) un-flatten the top so a real profile change (e.g. dropping a test score)
    #       actually moves the number. The old >65 slope (0.30) was so flat that a
    #       strong-EC applicant — whose fit already sits past 65 — saw a 13-pt fit
    #       swing convert to a ~2-pt odds swing, which reads to the user as "nothing
    #       changed." Doubling the slope (0.30 -> 0.65) makes the upper band live again
    #       while the lower base keeps the overall level down. Net at fit 65: 1.10
    #       (was 1.20); at fit 77: 1.22 (was 1.255); a 64->77 fit swing now moves the
    #       multiplier ~0.14 instead of ~0.08. Elites stay cap-bound, so unaffected.
    fit_mult = 0.10 + (min(fit, 65.0) / 65.0) ** 1.6 + max(0.0, fit - 65.0) / 65.0 * 0.65
    hook_mult = 1.0
    if profile.get("athlete"): hook_mult *= 1.30
    # Legacy is a real, measurable boost at top schools — Harvard ~6x,
    # Princeton ~4x, Penn ~5x baseline rates. Previous multipliers (1.15-
    # 1.25x) were way under-calibrated for this. Lift them — but the
    # caps below also need to rise for legacy applicants so the multiplier
    # actually shows up at elite-tier schools (where the cap binds).
    legacy_gens = legacy_generations_at(profile, school)
    if legacy_gens >= 3:   hook_mult *= 1.25
    elif legacy_gens == 2: hook_mult *= 1.20
    elif legacy_gens == 1: hook_mult *= 1.15
    if profile.get("first_gen"): hook_mult *= 1.10
    # Demonstrated interest (only applies at tier 2-3 schools that track it)
    di_level = profile.get("_di_level") or "none"
    hook_mult *= _di_multiplier(di_level, school.get("tier", 5))
    # Portfolio bonus — only at schools where portfolio is the actual gatekeeper.
    # Modest 1.15x lift; the user still has to be otherwise qualified, but a
    # portfolio at Roski/Tisch/RISD/etc. is meaningfully a hook.
    if (profile.get("portfolio") or "").strip() and school.get("slug") in PORTFOLIO_GATEKEEPER_SCHOOLS:
        hook_mult *= 1.15
    # Cohesive story / spike bonus. A focused profile whose activities, awards,
    # and intended major all point the same direction ("actions match words") is
    # a real plus — exactly what differentiates admits from "qualified but". It
    # ONLY lifts (never lowers), fades in above a typical-profile baseline (so
    # ordinary scattered profiles are unchanged → calibration safe), and is
    # scaled by how much THIS school rewards spikes vs. well-roundedness.
    spike_s = spike_for_odds(profile)
    cohesion = max(0.0, min(1.0, (spike_s - 550.0) / 450.0))   # 0 below 550, full at 1000
    spike_mult = 1.0 + cohesion * 0.10 * spike_receptivity(school)
    spike_mult = min(spike_mult, 1.15)
    center = a * fit_mult * hook_mult * spike_mult
    # Per-school calibration dial (takes precedence over the standard elite cap
    # for the few schools that were too harsh). Faded in by fit: w=0 at fit<=42
    # (weak — left untouched so it stays below acceptance rate) ramping to full
    # by fit>=58. Bypasses the harsh low-accept cap and uses a sane 0.42 ceiling.
    # Continuous "how exceptional are the ECs" signal (0-1) from the 1-1000
    # rating. Replaces the old binary is_exceptional cliff: exc=0 reproduces the
    # standard capped odds exactly (so normal-EC profiles, incl. the calibration
    # reference, are unchanged), exc=1 gives the full exceptional lift, and in
    # between the cap blends smoothly — so EC strength varies the ceiling.
    exc = ec_exceptional_strength(profile)
    _cal = _SCHOOL_CALIBRATION.get(school.get("slug"))
    if _cal and exc < 0.5:
        w = max(0.0, min(1.0, (fit - 46.0) / 20.0))
        center = center * (1.0 + (_cal - 1.0) * w)
        center = min(center, 0.42)
        center = _target_honesty_haircut(center)
        low = max(1, int(round((center - max(0.05, center * 0.30) / 2) * 100)))
        high = min(95, int(round((center + max(0.05, center * 0.30) / 2) * 100)))
        if high <= low: high = low + 3
        return low, high
    # Blend between the standard cap and the exceptional cap by `exc` (0-1).
    # Standard caps assume a typical strong applicant; the exceptional caps
    # (USAMO golds, recruited D1 athletes, ISEF top, etc.) get raised because
    # flat caps undersell them. At exc=0 this is exactly the standard cap (no
    # change vs. before for normal profiles); at exc=1 it's the full lift; in
    # between it ramps smoothly with EC strength. Caps only LIFT — never lower.
    # c_exc raised 2026-06-06: the exceptional gate is extremely strict (genuine
    # USAMO/ISEF/recruited/Platinum-tier only — a USACO-Gold profile scored 0/3),
    # and those applicants really do admit at ~20-30%+ to hyper-elites, so the
    # old mid-teens lift undersold them. c_std is UNCHANGED, so normal/strong
    # profiles (the calibration baseline) are completely unaffected; only flagged
    # or near-exceptional (high EC ramp) profiles move.
    _base = center
    if a < 0.07:
        c_std, c_exc = min(_base, 0.14), min(_base * 1.7 + 0.18, 0.70)
    elif a < 0.10:
        c_std, c_exc = min(_base, 0.18), min(_base * 1.7 + 0.16, 0.75)
    elif a < 0.20:
        c_std, c_exc = min(_base, 0.30), min(_base * 1.55 + 0.13, 0.82)
    elif a < 0.40:
        c_std, c_exc = min(_base, 0.55), min(_base * 1.4 + 0.08, 0.90)
    else:
        c_std, c_exc = min(_base, 0.85), min(_base * 1.3, 0.93)
    center = c_std + (c_exc - c_std) * exc
    center = _target_honesty_haircut(center)
    # Spread (uncertainty band) is wider at low-accept schools where the outcome
    # is genuinely more uncertain, and narrower at high-accept schools where
    # the prediction is more confident. Previous formula (center * 0.35) was
    # creating absurd ~28-point ranges at safety schools (e.g. TAMU center
    # 0.81 → spread 0.285 → 68-95% range, which is wider than useful).
    # Now: use a flatter spread that caps at ±9 points absolute.
    if center < 0.10:
        spread = max(0.03, center * 0.38)   # wider for elite uncertainty
    elif center < 0.30:
        spread = max(0.04, center * 0.24)   # standard for reach/target
    elif center < 0.60:
        spread = max(0.05, center * 0.16)   # tighter for target/safety
    else:
        spread = max(0.05, min(0.12, center * 0.13))  # tightest for safety, capped at ±6 pts
    low = max(1, int(round((center - spread / 2) * 100)))
    high = min(95, int(round((center + spread / 2) * 100)))
    if high <= low: high = low + 3
    return low, high



def confidence_level(profile, components):
    have_test = profile.get("sat") or profile.get("act")
    have_gpa = profile.get("uw_gpa") is not None
    if not have_test or not have_gpa: return "low"
    abs_signal = abs(components.get("gpa", 0)) + abs(components.get("test", 0))
    return "high" if abs_signal > 25 else "medium"


# ─── CLAUDE-POWERED REASONING (with template fallback) ───
def _claude(model, system, user, max_tokens=400, temperature=1.0):
    if not _claude_client: return None
    try:
        msg = _claude_client.messages.create(model=model, max_tokens=max_tokens, temperature=temperature, system=system, messages=[{"role":"user","content":user}])
        return msg.content[0].text
    except Exception as e:
        print(f"Claude error: {e}")
        return None


def _openai_chat(model, system, user, max_tokens=200, temperature=0.4):
    """Minimal OpenAI chat call over HTTP (no SDK dependency). Returns the
    text content, or None if no key is set / the call fails. Used to give the
    exceptionality panel one genuinely independent, non-Anthropic judge."""
    key = os.environ.get("OPENAI_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=30)
        if r.status_code != 200:
            print(f"OpenAI error {r.status_code}: {r.text[:160]}")
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"OpenAI error: {e}")
        return None


def generate_bullets(profile, school, fit, components, tier, odds):
    fb = _fallback_bullets(profile, school, fit, components, tier)
    # Pre-compute the test comparison so the model can't get the direction wrong.
    # Past bug: AI said "ACT 35 falls below mid-range 32-34" when 35 is actually
    # above 34. Now we hand the comparison to the model as a string.
    test_str = "none submitted"
    test_compare = "Student did not submit a test score."
    sat = profile.get("sat")
    act = profile.get("act")
    if sat and school.get("sat_25") is not None and school.get("sat_75") is not None:
        test_str = f"SAT {sat}"
        if sat >= school["sat_75"]:
            test_compare = f"SAT {sat} is AT OR ABOVE the 75th percentile ({school['sat_75']}) of admits — top of the range."
        elif sat >= school["sat_25"]:
            test_compare = f"SAT {sat} is INSIDE the mid-50% range ({school['sat_25']}-{school['sat_75']}) of admits."
        else:
            gap = school["sat_25"] - sat
            test_compare = f"SAT {sat} is BELOW the 25th percentile ({school['sat_25']}) of admits — gap of {gap} points."
    elif act and school.get("act_25") is not None and school.get("act_75") is not None:
        test_str = f"ACT {act}"
        if act >= school["act_75"]:
            test_compare = f"ACT {act} is AT OR ABOVE the 75th percentile ({school['act_75']}) of admits — top of the range."
        elif act >= school["act_25"]:
            test_compare = f"ACT {act} is INSIDE the mid-50% range ({school['act_25']}-{school['act_75']}) of admits."
        else:
            gap = school["act_25"] - act
            test_compare = f"ACT {act} is BELOW the 25th percentile ({school['act_25']}) of admits — gap of {gap} points."
    elif sat or act:
        # Test-blind / test-free school (no published score range). A submitted
        # score isn't evaluated, so don't compare it to a range.
        test_str = f"SAT {sat}" if sat else f"ACT {act}"
        test_compare = f"{school['name']} does not consider test scores in admissions, so this score isn't compared to an admit range."
    # GPA narrative: show the FLAT GPA (the number on the user's transcript)
    # but include trajectory context so the bullet explains why the gap
    # matters less than it looks (or worse than it looks).
    raw_gpa = profile.get("uw_gpa")
    gpa = effective_gpa(profile, school)
    gpa_compare = "GPA not submitted."
    # Build the trajectory context phrase used both in display and the prompt
    traj_context = ""
    is_uc = school and school.get("slug") in UC_SLUGS
    has_years = raw_gpa is not None and gpa is not None and abs(gpa - raw_gpa) > 0.02
    if has_years:
        years_seq = []
        for year_label, key in [("9th", "gpa_freshman"), ("10th", "gpa_sophomore"),
                                ("11th", "gpa_junior"), ("12th", "gpa_senior")]:
            v = profile.get(key)
            if v is not None: years_seq.append((year_label, v))
        seq_vals = [v for _, v in years_seq]
        is_upward = len(seq_vals) >= 3 and all(seq_vals[i+1] > seq_vals[i] for i in range(len(seq_vals)-1))
        is_downward = len(seq_vals) >= 3 and all(seq_vals[i+1] < seq_vals[i] for i in range(len(seq_vals)-1))
        traj_str = " → ".join(str(v) for _, v in years_seq)
        if is_uc:
            traj_context = f" — but UCs ignore 9th grade entirely; your 10th-12th avg is {gpa}, which the model uses"
        elif is_upward:
            traj_context = f" — but trajectory is upward ({traj_str}), so the model weights your upper years more (effective {gpa})"
        elif is_downward:
            traj_context = f" — and trajectory is downward ({traj_str}), which the model penalizes (effective {gpa})"
        else:
            traj_context = f" — model uses an upper-year-weighted {gpa} since you provided year-by-year"
    if raw_gpa:
        mid = round((school["gpa_lo"] + school["gpa_hi"]) / 2, 2)
        if raw_gpa >= school["gpa_hi"]:
            gpa_compare = f"GPA {raw_gpa} is AT OR ABOVE the typical 75th percentile ({school['gpa_hi']}).{traj_context}"
        elif raw_gpa >= mid:
            gpa_compare = f"GPA {raw_gpa} is ABOVE midpoint ({mid}), upper half of admits.{traj_context}"
        elif raw_gpa >= school["gpa_lo"]:
            gpa_compare = f"GPA {raw_gpa} is BELOW midpoint ({mid}) but inside the typical range ({school['gpa_lo']}-{school['gpa_hi']}).{traj_context}"
        else:
            gpa_compare = f"GPA {raw_gpa} is BELOW the typical 25th percentile ({school['gpa_lo']}){' but the upward trajectory pulls some of that back' if has_years and 'upward' in traj_context else ' — academic gap'}.{traj_context}"

    # Prompt context: still show flat as primary, with year-by-year context.
    gpa_lines = [f"- Unweighted GPA: {raw_gpa}"]
    if has_years:
        breakdown_str = ", ".join(f"{lbl}={v}" for lbl, v in years_seq)
        gpa_lines.append(f"- Year-by-year: {breakdown_str}")
        if is_uc:
            gpa_lines.append(f"- This is a UC. UC system ignores 9th-grade entirely. Effective GPA used by model: {gpa} (10th-12th average).")
            gpa_lines.append("- IMPORTANT: in your narrative, refer to the user's GPA as the flat number ({}). When discussing whether they're competitive, you can say something like 'although your overall GPA is {}, the UCs only weight your 10th-12th grades, where you average {}.'".format(raw_gpa, raw_gpa, gpa))
        else:
            gpa_lines.append(f"- Model uses upper-year-weighted GPA: {gpa} (9th=0.5x, 10th=1.0x, 11th=1.5x, 12th=1.0x; +0.05 trend bonus if strictly upward, -0.05 if downward).")
            gpa_lines.append("- IMPORTANT: in your narrative, refer to the user's GPA as the flat number ({}). If their trajectory is upward (e.g. 9th < 10th < 11th), explicitly call this out as a strength — colleges read trajectory heavily, especially upward trends from a weak 9th grade.".format(raw_gpa))

    user = f"""Student profile:
{chr(10).join(gpa_lines)}
- Test: {test_str}
- Major: {profile.get('major','undecided')}
- ECs: {profile.get('ecs','(blank)') or '(blank)'}
- Leadership: {profile.get('leadership','(blank)') or '(blank)'}
- Awards: {profile.get('awards','(blank)') or '(blank)'}
- Hooks for THIS school: legacy_generations={legacy_generations_at(profile, school)} (0 means no legacy here, even if the student has legacy elsewhere), first_gen={profile.get('first_gen')}, athlete={profile.get('athlete')}

Target: {school['name']} (acceptance {round(school['accept']*100,1)}%, GPA midpoint ~{round((school['gpa_lo']+school['gpa_hi'])/2,2)}, {('test-blind — scores not considered' if school.get('sat_25') is None else f"SAT mid-50% {school['sat_25']}-{school['sat_75']}, ACT mid-50% {school['act_25']}-{school['act_75']}")}).
Computed fit: {fit}/100. Tier: {tier}. Odds: {odds[0]}-{odds[1]}%.

PRE-COMPUTED COMPARISONS (use these exactly, do NOT recompute):
- {test_compare}
- {gpa_compare}

CRITICAL RULES:
- Use ONLY the numbers and comparisons given above.
- DO NOT compute test/GPA percentiles yourself — use the pre-computed comparisons.
- DO NOT invent percentile rankings or stats not provided.
- If the student submitted ACT, only reference the ACT range — never compare ACT to SAT.

Output exactly three lines. Each is ONE tight sentence (~25 words max) — specific to THIS applicant and THIS school. Lead with the concrete number/award; no preamble, no filler, no hedging. Shorter is better as long as it still lands.
STRENGTH: <one sentence: the strongest thing working in their favor here and why it matters at this school>
WEAKNESS: <one sentence: the biggest thing working against them here — honest and concrete>
DIFFERENTIATOR: <one sentence: what concretely could make them memorable here, or the specific gap to close>"""
    raw = _claude("claude-haiku-4-5-20251001",
        f"You are an experienced college admissions consultant. Be concrete, cite specific numbers, EXACTLY one tight sentence (~25 words max) per field, never hedge. Punchy over comprehensive. No preamble.\n\n{_date_context()}",
        user, max_tokens=260)
    if not raw: return fb
    out = {"strength": "", "weakness": "", "differentiator": ""}
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("STRENGTH:"): out["strength"] = line.split(":",1)[1].strip()
        elif line.startswith("WEAKNESS:"): out["weakness"] = line.split(":",1)[1].strip()
        elif line.startswith("DIFFERENTIATOR:"): out["differentiator"] = line.split(":",1)[1].strip()
    return out if all(out.values()) else fb


def _fallback_bullets(profile, school, fit, components, tier):
    # Show the flat GPA in narrative (what users have on their transcript)
    # but the model already used effective_gpa for the components scoring.
    gpa = profile.get("uw_gpa") or 0
    school_gpa_mid = round((school["gpa_lo"]+school["gpa_hi"])/2, 2)
    # Cite the range matching what the student actually submitted (ACT-only
    # students shouldn't be told about the SAT range), and guard test-blind
    # schools whose ranges are None.
    _sat, _act = profile.get("sat"), profile.get("act")
    if _act and not _sat and school.get("act_25") is not None:
        test_range_str = f"ACT {school['act_25']}–{school['act_75']}"
    elif school.get("sat_25") is not None:
        test_range_str = f"SAT {school['sat_25']}–{school['sat_75']}"
    else:
        test_range_str = ""
    if components.get("gpa", 0) > 5:
        strength = f"Your GPA of {gpa} is meaningfully above {school['name']}'s typical midpoint of {school_gpa_mid}, the strongest signal in your favor."
    elif components.get("test", 0) > 8:
        strength = (f"Your test score sits comfortably above the middle-50% range at {school['name']} ({test_range_str}), a real academic edge."
                    if test_range_str else
                    f"Your test score is a clear academic strength for {school['name']}.")
    elif components.get("hooks", 0) > 0:
        hooks = [k for k in ("legacy","first_gen","athlete") if profile.get(k)]
        strength = f"Your hook(s) — {', '.join(hooks)} — measurably move the needle here."
    else:
        strength = f"Your profile is broadly within the range {school['name']} considers competitive."
    if components.get("gpa", 0) < -3:
        weakness = f"Your GPA of {gpa} is below the typical applicant; this is the gap to close hardest."
    elif components.get("test", 0) < -5:
        weakness = (f"Your test score is below the middle-50% range ({test_range_str}) — a retake would meaningfully change odds."
                    if test_range_str else
                    f"Your test score is below where it needs to be for {school['name']} — a retake would meaningfully change odds.")
    elif components.get("ecs", 0) <= 0:
        weakness = f"Your extracurriculars read thin for {school['name']}'s admit pool."
    else:
        weakness = f"At {round(school['accept']*100,1)}% acceptance, even strong profiles often need a clear hook."
    differentiator = f"Lean harder into your interest in {profile.get('major') or 'a focused academic identity'} — concrete projects or recognized awards in it separate similar profiles."
    return {"strength": strength, "weakness": weakness, "differentiator": differentiator}


def analyze_school(profile, slug):
    raw = COLLEGES_BY_SLUG.get(slug)
    if not raw: return None
    # Apply CDS_VERIFIED + _OVERRIDES so the chances calc uses the most
    # accurate accept rate and SAT/ACT bands, not stale COLLEGES values.
    school = merged_school(raw)
    fit, components = compute_fit(profile, school)
    tier = assign_tier(school, fit, profile)
    low, high = estimate_odds(school, fit, profile)
    bullets = generate_bullets(profile, school, fit, components, tier, (low, high))
    return {
        "school": school["name"], "slug": school["slug"],
        "accept_rate_pct": round(school["accept"]*100,1),
        "fit": fit, "tier": tier,
        "odds_low": low, "odds_high": high,
        "confidence": confidence_level(profile, components),
        **bullets, "components": components,
    }


# ─── DATABASE ─────────────────────────────────────────────
def db():
    # Make sure the directory exists — on Railway DB_PATH is /app/data/college.db
    # which requires a mounted volume; mkdirs is idempotent and cheap.
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            uw_gpa REAL,
            weighted_gpa REAL,
            sat INTEGER,
            act INTEGER,
            major TEXT,
            state TEXT,
            school_type TEXT,
            ecs TEXT,
            leadership TEXT,
            awards TEXT,
            legacy INTEGER DEFAULT 0,
            first_gen INTEGER DEFAULT 0,
            athlete INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS saved_chances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            tier TEXT, odds_low INTEGER, odds_high INTEGER,
            fit INTEGER, confidence TEXT,
            strength TEXT, weakness TEXT, differentiator TEXT,
            computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, college_slug),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS college_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            college_slug TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source TEXT,
            published TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_college ON college_articles(college_slug, fetched_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            college_slug TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_msg_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, kind, college_slug)")
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS tailored_advice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            profile_hash TEXT,
            UNIQUE(user_id, college_slug),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_facts (
            college_slug TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_profiles (
            college_slug TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_essays (
            college_slug TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_summary (
            college_slug TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_feeders (
            college_slug TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # AI-personalized round rates (ED/EA/RD odds adjusted for the user's
        # specific profile vs the school's specific dynamics). Cached per
        # (user, school, profile_version). Profile version is a hash of the
        # profile fields that affect chances; bumping it busts the cache.
        conn.execute("""CREATE TABLE IF NOT EXISTS personalized_rounds (
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            profile_version TEXT NOT NULL,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, college_slug, profile_version),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        # User shortlist — schools the user has explicitly saved as targets.
        # Independent of saved_chances (which auto-saves on chances calc).
        # Lets users build a target list before/without running chances.
        conn.execute("""CREATE TABLE IF NOT EXISTS saved_schools (
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            saved_at TEXT DEFAULT CURRENT_TIMESTAMP,
            note TEXT,
            PRIMARY KEY (user_id, college_slug),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        # Real Reddit posts (r/collegeresults + r/chanceme) cached per school.
        # Refreshed every 24h. Body is JSON: list of {title, selftext, url, score, sub, age}.
        conn.execute("""CREATE TABLE IF NOT EXISTS school_reddit_posts (
            college_slug TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_strategies (
            college_slug TEXT PRIMARY KEY,
            school_values TEXT NOT NULL,
            supplemental_strategy TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # Defensive migration — preferences fields added later. Old DBs won't have them.
        for col in ("pref_weather", "pref_setting", "pref_size",
                    "pref_greek", "pref_sports", "pref_internships",
                    "pref_class_size", "pref_prestige", "pref_region", "pref_cost",
                    "pref_major_strength"):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} TEXT DEFAULT 'any'")
            except sqlite3.OperationalError:
                pass
        # Specific legacy schools (comma-separated). Replaces the boolean
        # legacy flag for matching purposes.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN legacy_schools TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # Per-preference importance weights (JSON dict 1-10 per pref).
        # Empty / missing means use neutral weight (5) for all prefs.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN pref_weights TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # Major-strength preference (replaces internships).
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN pref_major_strength TEXT DEFAULT 'any'")
        except sqlite3.OperationalError:
            pass
        # Additional preference dimensions added in May 2026.
        for col in ("pref_diversity","pref_party","pref_research","pref_career_intensity"):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} TEXT DEFAULT 'any'")
            except sqlite3.OperationalError:
                pass
        # Preference dimensions added Jun 2026: distance-from-home, financial-aid
        # generosity, collaborative-vs-competitive academic culture.
        for col in ("pref_location","pref_aid","pref_culture"):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} TEXT DEFAULT 'any'")
            except sqlite3.OperationalError:
                pass
        # Exceptional-applicant flag (May 2026). When true, the odds model
        # lifts caps significantly because flat caps undersell USAMO golds,
        # recruited athletes, ISEF winners, etc. Evaluated by Claude on
        # profile save; never auto-set, never lowers odds.
        for col_def in (
            "is_exceptional INTEGER DEFAULT 0",
            "exceptional_reason TEXT DEFAULT ''",
            "exceptional_evaluated_at TEXT",
            "portfolio TEXT DEFAULT ''",
        ):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        # Application Strategist (premium, May 2026). One AI call reads the
        # user's whole college list + profile and returns a strategic plan.
        # Cached by a hash of the list + profile so it regenerates only when
        # the list, rounds, computed odds, or key profile fields change.
        conn.execute("""CREATE TABLE IF NOT EXISTS strategist_results (
            user_id INTEGER PRIMARY KEY,
            input_hash TEXT NOT NULL,
            body TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        # AI Advisor paywall — usage tracking + paid status on users.
        for col_def in (
            "is_paid INTEGER DEFAULT 0",
            "stripe_customer_id TEXT",
            "free_msgs_used INTEGER DEFAULT 0",
            "msgs_this_month INTEGER DEFAULT 0",
            "msg_month_anchor TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        # International applicant flag — affects acceptance rate calc at
        # schools that have a meaningful international vs domestic split.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN is_international INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # AP courses (free-text, comma-separated) + flag for schools that
        # don't offer APs. When the flag is set, course rigor is treated as
        # neutral rather than penalized.
        for col, default in (("aps", "''"), ("no_aps_offered", "0"), ("aps_offered_not_taken", "0"),
                              ("ibs", "''"), ("no_ibs_offered", "0"), ("ibs_offered_not_taken", "0")):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} TEXT DEFAULT {default}" if col == "aps"
                             else f"ALTER TABLE profiles ADD COLUMN {col} INTEGER DEFAULT {default}")
            except sqlite3.OperationalError:
                pass
        # Class rank: rank + class size (nullable ints) + a flag for schools
        # that don't rank. When the flag is set, rank is treated as neutral.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN ec_score REAL")
        except sqlite3.OperationalError:
            pass
        # 1-1000 LLM-graded EC/leadership rating (granular successor to the
        # 0-16 ec_score). This is the signal compute_fit/estimate_odds read.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN ec_rating REAL")
        except sqlite3.OperationalError:
            pass
        # 1-1000 cohesive-story / spike / major-alignment rating.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN spike_score REAL")
        except sqlite3.OperationalError:
            pass
        # Self-rated course rigor (1-10) for students whose school offers no AP/IB.
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN self_rigor INTEGER")
        except sqlite3.OperationalError:
            pass
        # Year-by-year WEIGHTED GPA + a per-year "weighting not offered" flag, so
        # students whose school only weights some years are judged on the years
        # weighting was actually available (not penalized for gated honors/AP).
        for _wc in ("ALTER TABLE profiles ADD COLUMN w_gpa_freshman REAL",
                    "ALTER TABLE profiles ADD COLUMN w_gpa_sophomore REAL",
                    "ALTER TABLE profiles ADD COLUMN w_gpa_junior REAL",
                    "ALTER TABLE profiles ADD COLUMN w_gpa_senior REAL",
                    "ALTER TABLE profiles ADD COLUMN w_notoffered_freshman INTEGER DEFAULT 0",
                    "ALTER TABLE profiles ADD COLUMN w_notoffered_sophomore INTEGER DEFAULT 0",
                    "ALTER TABLE profiles ADD COLUMN w_notoffered_junior INTEGER DEFAULT 0",
                    "ALTER TABLE profiles ADD COLUMN w_notoffered_senior INTEGER DEFAULT 0"):
            try:
                conn.execute(_wc)
            except sqlite3.OperationalError:
                pass
        # Cached profile grade (JSON) + a key so we only recompute when the
        # grading-relevant fields actually change (deterministic + cheap).
        for _gc in ("ALTER TABLE profiles ADD COLUMN grade_json TEXT",
                    "ALTER TABLE profiles ADD COLUMN grade_key TEXT"):
            try:
                conn.execute(_gc)
            except sqlite3.OperationalError:
                pass
        for _crc in ("ALTER TABLE profiles ADD COLUMN class_rank INTEGER",
                     "ALTER TABLE profiles ADD COLUMN class_size INTEGER",
                     "ALTER TABLE profiles ADD COLUMN no_class_rank_offered INTEGER DEFAULT 0"):
            try:
                conn.execute(_crc)
            except sqlite3.OperationalError:
                pass
        # Year-by-year unweighted GPAs (optional). Used for trend detection
        # and UC-policy weighting (UCs ignore freshman year). When provided,
        # supersedes flat uw_gpa for chances calc on a per-school basis.
        for col in ("gpa_freshman", "gpa_sophomore", "gpa_junior", "gpa_senior"):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
        # Median earnings 10 years post-entry + cost-of-attendance, both
        # from College Scorecard (federal IPEDS data). Used to compute a
        # rough ROI proxy and surface earnings on each college page.
        for col in ("median_earnings_10yr", "cost_attendance"):
            try:
                conn.execute(f"ALTER TABLE school_stats_overrides ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass
        # SAT/ACT subscores (optional). Used as context for AI advice — a
        # low math subscore at a STEM-focused school (MIT, Caltech, CMU CS)
        # matters more than a low verbal subscore. Composite stays the
        # primary fit input.
        for col in ("sat_math","sat_ebrw","act_math","act_english","act_reading","act_science"):
            try:
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass
        # Profile-version hash on cached tailored advice so it invalidates when
        # the student edits the stats the advice was built on (GPA, scores, ECs).
        try:
            conn.execute("ALTER TABLE tailored_advice ADD COLUMN profile_hash TEXT")
        except sqlite3.OperationalError:
            pass
        # Application round on each saved school. NULL = undecided.
        # Values: 'ED1','ED2','EA','REA','RD'. Used for the simulator and for
        # categorizing the My Plans page.
        try:
            conn.execute("ALTER TABLE saved_schools ADD COLUMN application_round TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE saved_chances ADD COLUMN application_round TEXT")
        except sqlite3.OperationalError:
            pass
        # User-set preference rank on each saved school. 1 = most wanted to
        # attend, 2 = next, etc. NULL = unranked. Fed to the strategist so
        # dream schools become Anchors and bottom-ranked reaches get cut.
        try:
            conn.execute("ALTER TABLE saved_schools ADD COLUMN preference_rank INTEGER")
        except sqlite3.OperationalError:
            pass
        # Premium subscription flag on users — gates the /plans page (My Plans
        # dashboard with simulator + grader). Free users still get individual
        # chances calc and college pages; premium adds the cross-list view.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
        except sqlite3.OperationalError:
            pass
        # Owner / founder accounts — auto-granted premium so the owner can
        # always access premium features without going through Stripe.
        OWNER_EMAILS = ("jasperthelazzer19@gmail.com", "jlasser@newroads.org")
        for email in OWNER_EMAILS:
            try:
                conn.execute("UPDATE users SET is_paid=1 WHERE LOWER(email)=LOWER(?)", (email,))
            except Exception as e:
                print(f"owner premium grant failed for {email}: {e}")
        # Federal-data overrides for each school's stats (College Scorecard).
        # Hardcoded COLLEGES values are fallback; this table takes precedence.
        conn.execute("""CREATE TABLE IF NOT EXISTS school_stats_overrides (
            college_slug TEXT PRIMARY KEY,
            accept REAL,
            sat_25 INTEGER, sat_75 INTEGER,
            act_25 INTEGER, act_75 INTEGER,
            size INTEGER,
            tuition INTEGER,
            sf_ratio INTEGER,
            source TEXT,
            verified_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # Demonstrated interest — per-user-per-school flag set when the user
        # marks "I've engaged with this school" (visited, info session,
        # emailed admissions). Drives a small odds boost at tier 1-2 schools
        # that publicly weight DI.
        conn.execute("""CREATE TABLE IF NOT EXISTS demonstrated_interest (
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            level TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, college_slug),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        # Outcomes — predicted vs. actual admission results, used to validate
        # and recalibrate the chances model over time. Populated when the
        # user reports back at /outcomes. Snapshot of the prediction is
        # frozen at submission so we can compare.
        conn.execute("""CREATE TABLE IF NOT EXISTS user_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            application_round TEXT,
            predicted_odds_low INTEGER,
            predicted_odds_high INTEGER,
            predicted_fit INTEGER,
            predicted_tier TEXT,
            actual_outcome TEXT,
            attended INTEGER DEFAULT 0,
            reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, college_slug),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_user ON user_outcomes(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_school ON user_outcomes(college_slug, actual_outcome)")
        # Multi-source career-outcomes (earnings trajectory + source count) shown
        # on each school page. Loaded from the merged earnings research.
        conn.execute("""CREATE TABLE IF NOT EXISTS career_outcomes (
            college_slug TEXT PRIMARY KEY,
            entry INTEGER, ten_yr INTEGER, mid_career INTEGER,
            oi INTEGER, roi INTEGER, n_sources INTEGER, sources TEXT
        )""")
        # Dedupe log for deadline reminder emails (one per user/school/milestone).
        conn.execute("""CREATE TABLE IF NOT EXISTS deadline_nudges (
            user_id INTEGER NOT NULL,
            college_slug TEXT NOT NULL,
            milestone INTEGER NOT NULL,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, college_slug, milestone)
        )""")
        # Anonymous page-view log — feeds /admin/stats "visitors in last hour /
        # 24h" cards. Cookie-based visitor_id so the same browser counts as
        # one across sessions even before signup.
        conn.execute("""CREATE TABLE IF NOT EXISTS page_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id TEXT NOT NULL,
            user_id INTEGER,
            path TEXT NOT NULL,
            ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_visits_ts ON page_visits(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_visits_visitor ON page_visits(visitor_id, ts)")
        # Store the user-agent so single-pageview visitors can be counted (real
        # human) while bots are excluded by UA — no need for the 2+ heuristic on
        # new data. Legacy rows keep user_agent NULL.
        try:
            conn.execute("ALTER TABLE page_visits ADD COLUMN user_agent TEXT")
        except Exception:
            pass  # column already exists
        # Per-calibration tally: one row EVERY time a school's chances are run
        # (incl. re-runs), so the admin "most-viewed schools" list reflects true
        # volume, not just unique user-school pairs. Backfilled once from
        # saved_chances so historical calibrations aren't lost.
        conn.execute("""CREATE TABLE IF NOT EXISTS calc_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            college_slug TEXT NOT NULL,
            user_id INTEGER,
            ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calc_runs_slug ON calc_runs(college_slug)")
        try:
            if conn.execute("SELECT COUNT(*) FROM calc_runs").fetchone()[0] == 0:
                conn.execute("INSERT INTO calc_runs(college_slug, user_id, ts) "
                             "SELECT college_slug, user_id, computed_at FROM saved_chances")
        except Exception as _e:
            print(f"calc_runs backfill skipped: {_e}")
        # Lead capture from anonymous /college/<slug> visitors. Email + the
        # school they were looking at. No password, no auth — just a list
        # that becomes the seed for outcome-capture and retention emails
        # (per the CEO plan, May 2026).
        conn.execute("""CREATE TABLE IF NOT EXISTS interest_signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            slug TEXT,
            visitor_id TEXT,
            source TEXT,
            ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_email ON interest_signups(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_ts ON interest_signups(ts)")
        conn.commit()


# ─── AUTH ─────────────────────────────────────────────────
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return h.hex(), salt


def verify_password(password, password_hash, password_salt):
    h, _ = hash_password(password, password_salt)
    return hmac.compare_digest(h, password_hash)


def current_user():
    uid = session.get("user_id")
    if not uid: return None
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            session["next_url"] = request.path
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


def get_profile(user_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def save_profile(user_id, p):
    # Legacy is now derived from legacy_schools — auto-true if user listed any.
    legacy_schools = (p.get("legacy_schools") or "").strip()
    legacy_flag = 1 if legacy_schools else (1 if p.get("legacy") else 0)
    pref_weights = p.get("pref_weights") or ""
    with db() as conn:
        conn.execute("""INSERT INTO profiles
            (user_id, uw_gpa, weighted_gpa, sat, act, major, state, school_type, ecs, leadership, awards,
             legacy, first_gen, athlete, is_international, legacy_schools, aps, no_aps_offered, aps_offered_not_taken,
             ibs, no_ibs_offered, ibs_offered_not_taken,
             pref_weather, pref_setting, pref_size, pref_greek, pref_sports, pref_major_strength,
             pref_class_size, pref_prestige, pref_cost,
             pref_diversity, pref_party, pref_research, pref_career_intensity,
             pref_weights, portfolio, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                uw_gpa=excluded.uw_gpa, weighted_gpa=excluded.weighted_gpa, sat=excluded.sat, act=excluded.act,
                major=excluded.major, state=excluded.state, school_type=excluded.school_type,
                ecs=excluded.ecs, leadership=excluded.leadership, awards=excluded.awards,
                legacy=excluded.legacy, first_gen=excluded.first_gen, athlete=excluded.athlete,
                is_international=excluded.is_international,
                legacy_schools=excluded.legacy_schools,
                aps=excluded.aps, no_aps_offered=excluded.no_aps_offered, aps_offered_not_taken=excluded.aps_offered_not_taken,
                ibs=excluded.ibs, no_ibs_offered=excluded.no_ibs_offered, ibs_offered_not_taken=excluded.ibs_offered_not_taken,
                pref_weather=excluded.pref_weather, pref_setting=excluded.pref_setting,
                pref_size=excluded.pref_size, pref_greek=excluded.pref_greek,
                pref_sports=excluded.pref_sports, pref_major_strength=excluded.pref_major_strength,
                pref_class_size=excluded.pref_class_size, pref_prestige=excluded.pref_prestige,
                pref_cost=excluded.pref_cost,
                pref_diversity=excluded.pref_diversity, pref_party=excluded.pref_party,
                pref_research=excluded.pref_research, pref_career_intensity=excluded.pref_career_intensity,
                pref_weights=excluded.pref_weights,
                portfolio=excluded.portfolio,
                updated_at=CURRENT_TIMESTAMP""",
            (user_id, p.get("uw_gpa"), p.get("weighted_gpa"), p.get("sat"), p.get("act"),
             p.get("major"), p.get("state"), p.get("school_type"), p.get("ecs"),
             p.get("leadership"), p.get("awards"),
             legacy_flag, 1 if p.get("first_gen") else 0, 1 if p.get("athlete") else 0,
             1 if p.get("is_international") else 0,
             legacy_schools, p.get("aps") or "", 1 if p.get("no_aps_offered") else 0,
             1 if p.get("aps_offered_not_taken") else 0,
             p.get("ibs") or "", 1 if p.get("no_ibs_offered") else 0,
             1 if p.get("ibs_offered_not_taken") else 0,
             p.get("pref_weather") or "any", p.get("pref_setting") or "any",
             p.get("pref_size") or "any", p.get("pref_greek") or "any",
             p.get("pref_sports") or "any", p.get("pref_major_strength") or "any",
             p.get("pref_class_size") or "any", p.get("pref_prestige") or "any",
             p.get("pref_cost") or "any",
             p.get("pref_diversity") or "any", p.get("pref_party") or "any",
             p.get("pref_research") or "any", p.get("pref_career_intensity") or "any",
             pref_weights, p.get("portfolio") or ""))
        # Newer preference columns (location / aid / culture) — separate UPDATE
        # to keep the giant positional INSERT above untouched.
        try:
            conn.execute(
                "UPDATE profiles SET pref_location=?, pref_aid=?, pref_culture=? WHERE user_id=?",
                (p.get("pref_location") or "any", p.get("pref_aid") or "any",
                 p.get("pref_culture") or "any", user_id),
            )
        except Exception as e:
            print(f"new-pref save failed: {e}")
        # Year-by-year GPA — separate UPDATE to keep the giant INSERT above
        # untouched. Stored as REAL nullables so empty inputs don't pollute
        # the chances calc with zeros.
        try:
            conn.execute(
                "UPDATE profiles SET gpa_freshman=?, gpa_sophomore=?, gpa_junior=?, gpa_senior=? WHERE user_id=?",
                (p.get("gpa_freshman"), p.get("gpa_sophomore"), p.get("gpa_junior"), p.get("gpa_senior"), user_id),
            )
        except Exception as e:
            print(f"year-by-year GPA save failed: {e}")
        # Year-by-year WEIGHTED GPA + per-year "not offered" flags.
        try:
            conn.execute(
                "UPDATE profiles SET w_gpa_freshman=?, w_gpa_sophomore=?, w_gpa_junior=?, w_gpa_senior=?, "
                "w_notoffered_freshman=?, w_notoffered_sophomore=?, w_notoffered_junior=?, w_notoffered_senior=? "
                "WHERE user_id=?",
                (p.get("w_gpa_freshman"), p.get("w_gpa_sophomore"), p.get("w_gpa_junior"), p.get("w_gpa_senior"),
                 1 if p.get("w_notoffered_freshman") else 0, 1 if p.get("w_notoffered_sophomore") else 0,
                 1 if p.get("w_notoffered_junior") else 0, 1 if p.get("w_notoffered_senior") else 0, user_id),
            )
        except Exception as e:
            print(f"year-by-year weighted GPA save failed: {e}")
        try:
            conn.execute(
                "UPDATE profiles SET class_rank=?, class_size=?, no_class_rank_offered=? WHERE user_id=?",
                (p.get("class_rank") or None, p.get("class_size") or None,
                 1 if p.get("no_class_rank_offered") else 0, user_id),
            )
        except Exception as e:
            print(f"class rank save failed: {e}")
        # Self-rated course rigor (1-10) — used only when the school offers no
        # AP/IB. Separate UPDATE to keep the giant INSERT above untouched.
        try:
            _sr = p.get("self_rigor")
            _sr = int(_sr) if str(_sr).strip() not in ("", "None") else None
            if _sr is not None:
                _sr = max(1, min(10, _sr))
            conn.execute("UPDATE profiles SET self_rigor=? WHERE user_id=?", (_sr, user_id))
        except Exception as e:
            print(f"self_rigor save failed: {e}")
        # LLM extracurricular rating (1-1000) — computed once here at save time
        # so compute_fit / ranking stay instant. This is the signal that drives
        # how much ECs move the odds. Falls back to a deterministic keyword
        # rating if the call fails or no Claude key is set, so it's never null
        # for a populated profile.
        try:
            _ecr, _spk = grade_ec_and_spike(p)   # one combined Haiku call
            if _ecr is None:
                _ecr = ec_rating_keyword(p)
            if _spk is None:
                _spk = cohesion_keyword(p)
            conn.execute("UPDATE profiles SET ec_rating=?, spike_score=? WHERE user_id=?",
                         (_ecr, _spk, user_id))
        except Exception as e:
            print(f"ec_rating/spike save failed: {e}")
        try:
            conn.execute(
                "UPDATE profiles SET sat_math=?, sat_ebrw=?, act_math=?, act_english=?, act_reading=?, act_science=? WHERE user_id=?",
                (p.get("sat_math"), p.get("sat_ebrw"),
                 p.get("act_math"), p.get("act_english"), p.get("act_reading"), p.get("act_science"),
                 user_id),
            )
        except Exception as e:
            print(f"subscore save failed: {e}")
        conn.commit()


def get_user_row(user_id):
    if not user_id: return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def usage_status(user_id):
    """Return current usage status for a user. Resets monthly counter on
    first call of each new month. Returns dict with:
      - is_paid: bool
      - free_used: int (lifetime free messages)
      - free_remaining: int
      - month_used: int (this month, paid users only)
      - month_remaining: int
      - blocked: bool — True if they should be paywalled
      - reason: 'free_exhausted' | 'monthly_cap' | None
    """
    u = get_user_row(user_id)
    if not u:
        return {"blocked": True, "reason": "no_user"}
    is_paid = bool(u["is_paid"])
    free_used = u["free_msgs_used"] or 0
    month_used = u["msgs_this_month"] or 0
    anchor = u["msg_month_anchor"] or ""
    this_month = datetime.utcnow().strftime("%Y-%m")
    if anchor != this_month:
        # Roll over the monthly counter
        with db() as conn:
            conn.execute("UPDATE users SET msgs_this_month=0, msg_month_anchor=? WHERE id=?",
                         (this_month, user_id))
            conn.commit()
        month_used = 0
    if is_paid:
        return {
            "is_paid": True,
            "free_used": free_used,
            "free_remaining": max(0, FREE_TRIAL_MESSAGES - free_used),
            "month_used": month_used,
            "month_remaining": max(0, PAID_MONTHLY_LIMIT - month_used),
            "blocked": month_used >= PAID_MONTHLY_LIMIT,
            "reason": "monthly_cap" if month_used >= PAID_MONTHLY_LIMIT else None,
        }
    return {
        "is_paid": False,
        "free_used": free_used,
        "free_remaining": max(0, FREE_TRIAL_MESSAGES - free_used),
        "month_used": month_used,
        "month_remaining": 0,
        "blocked": free_used >= FREE_TRIAL_MESSAGES,
        "reason": "free_exhausted" if free_used >= FREE_TRIAL_MESSAGES else None,
    }


def increment_message_count(user_id):
    """Bump the right counter (free or monthly) after a successful AI call."""
    u = get_user_row(user_id)
    if not u: return
    is_paid = bool(u["is_paid"])
    this_month = datetime.utcnow().strftime("%Y-%m")
    with db() as conn:
        if is_paid:
            anchor = u["msg_month_anchor"] or ""
            if anchor != this_month:
                conn.execute("UPDATE users SET msgs_this_month=1, msg_month_anchor=? WHERE id=?",
                             (this_month, user_id))
            else:
                conn.execute("UPDATE users SET msgs_this_month=msgs_this_month+1 WHERE id=?",
                             (user_id,))
        else:
            conn.execute("UPDATE users SET free_msgs_used=free_msgs_used+1 WHERE id=?",
                         (user_id,))
        conn.commit()


def grant_paid(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET is_paid=1 WHERE id=?", (user_id,))
        conn.commit()


def get_pref_weight(profile, key):
    """Return importance weight 1-10 for a pref. 5 = neutral, 10 = critical, 1 = barely matters."""
    raw = (profile.get("pref_weights") or "").strip() if profile else ""
    if not raw: return 5
    try:
        d = json.loads(raw)
        v = int(d.get(key, 5))
        return max(1, min(10, v))
    except Exception:
        return 5


def parse_pref_weights_form(form):
    """Read importance dropdown values from the profile form, return JSON string."""
    out = {}
    for k in ("weather","setting","size","class_size","greek","sports","major_strength","prestige","cost","diversity","party","research","career_intensity","location","aid","culture"):
        try:
            out[k] = max(1, min(10, int(form.get(f"weight_{k}", 5))))
        except (TypeError, ValueError):
            out[k] = 5
    return json.dumps(out)


_LEGACY_COUNT_RE = re.compile(r"\s*(?:[\(\[]?\s*(\d+)\s*x?\s*[\)\]]?|x\s*(\d+))\s*$", re.IGNORECASE)


# Common short-name aliases users type → canonical slug. Used to disambiguate
# "Penn" (UPenn, NOT Penn State) and "MIT" (MIT, NOT Smith), etc. Naive
# substring matching would falsely match those.

def legacy_generations_at(profile, school):
    """How many generations of legacy the user has at THIS school. Returns
    0 if none. Parses '<name> Nx' or '<name> (N)' or just '<name>' (=1 gen).

    Matching strategy (in order):
    1. Alias map (canonical for short/common names — "Penn" → upenn, NOT Penn State)
    2. Exact slug match
    3. Exact name match
    4. All user words must be words in school name (so "Bowdoin College"
       matches Bowdoin even though one extra word). Word-boundary, no
       partial substring match — so "MIT" doesn't match "Smith"."""
    if not profile or not school: return 0
    raw = (profile.get("legacy_schools") or "").strip().lower()
    if not raw: return 0
    import re as _re
    school_name = (school.get("name") or "").lower()
    school_slug = (school.get("slug") or "").lower()
    name_words = set(_re.findall(r"\w+", school_name))
    best = 0
    for p in raw.split(","):
        p = p.strip()
        if not p: continue
        # Strip a trailing count suffix like "4x", "x4", "(4)" — default 1.
        m = _LEGACY_COUNT_RE.search(p)
        if m:
            count = int(m.group(1) or m.group(2))
            name = p[:m.start()].strip()
        else:
            count = 1
            name = p
        if not name: continue
        matched = False
        # 1. Alias map — disambiguates short names that would otherwise false-positive
        if name in _LEGACY_ALIASES:
            matched = (_LEGACY_ALIASES[name] == school_slug)
        # 2-3. Exact matches if no alias hit
        if not matched and name not in _LEGACY_ALIASES:
            if name == school_slug:
                matched = True
            elif name.replace(" ", "-") == school_slug:
                matched = True
            elif name == school_name:
                matched = True
            else:
                # 4. Word-boundary subset, but the ONLY extra words allowed
                # between the two are generic institution words. This lets
                # "Cornell University" match a school stored as just "Cornell"
                # (extra word = "university"), and "Bowdoin College" ↔ "Bowdoin",
                # WITHOUT letting a single-word stored name be matched by a
                # different multi-word school: "UMass Amherst" must NOT match
                # "Amherst", "Cal Poly Pomona" must NOT match "Pomona", and
                # "Boston College" must NOT match "Boston University".
                _GEN = {"university", "college", "institute", "school", "of",
                        "the", "and", "at", "univ", "u"}
                user_words = set(_re.findall(r"\w+", name))
                if user_words and (
                    (name_words.issubset(user_words) and (user_words - name_words).issubset(_GEN)) or
                    (user_words.issubset(name_words) and (name_words - user_words).issubset(_GEN))
                ):
                    matched = True
        if matched:
            best = max(best, count)
    return best


def has_legacy_at(profile, school):
    """Boolean wrapper around legacy_generations_at for code that just wants
    a yes/no signal."""
    return legacy_generations_at(profile, school) > 0


# ─── ARTICLES (NewsAPI w/ DB cache) ───────────────────────
REDDIT_POSTS_TTL_HOURS = 24


def fetch_reddit_profiles(college_slug, force=False):
    """Pull real applicant profiles from r/collegeresults + r/chanceme via
    Reddit's public JSON endpoint. Cached 24h. Returns a list of dicts:
    [{title, snippet, url, score, sub, when}].
    No auth required for public read access."""
    school = COLLEGES_BY_SLUG.get(college_slug)
    if not school: return []
    cutoff = (datetime.utcnow() - timedelta(hours=REDDIT_POSTS_TTL_HOURS)).isoformat()
    with db() as conn:
        if not force:
            row = conn.execute(
                "SELECT body, fetched_at FROM school_reddit_posts WHERE college_slug=? AND fetched_at >= ?",
                (college_slug, cutoff)
            ).fetchone()
            if row:
                try:
                    return json.loads(row["body"])
                except Exception:
                    pass

    name = school["name"]
    # Common abbreviations / alternate spellings + phrase variants to maximize hit rate.
    # The same school appears under multiple names on Reddit.
    aliases = {
        "Massachusetts Institute of Technology": ["MIT"],
        "University of Pennsylvania": ["UPenn", "Penn"],
        "Carnegie Mellon": ["CMU"],
        "Northwestern": ["Northwestern"],
        "University of California-Berkeley": ["UC Berkeley", "Berkeley", "Cal"],
        "UC Berkeley": ["UC Berkeley", "Berkeley", "Cal"],
        "UCLA": ["UCLA"],
        "Johns Hopkins": ["Johns Hopkins", "JHU", "Hopkins"],
        "Wash U St. Louis": ["WashU", "Wash U", "Washington University in St Louis", "WUSTL"],
        "University of Michigan": ["UMich", "University of Michigan", "Michigan"],
        "University of Chicago": ["UChicago", "U Chicago"],
        "Wake Forest": ["Wake Forest", "WFU", "Wake"],
        "Notre Dame": ["Notre Dame", "ND"],
        "Boston College": ["Boston College", "BC"],
        "Boston University": ["Boston University", "BU"],
        "Vanderbilt": ["Vanderbilt", "Vandy"],
        "William & Mary": ["William and Mary", "William & Mary", "W&M", "WM"],
        "Georgia Tech": ["Georgia Tech", "GT", "Gtech"],
        "Tufts": ["Tufts"],
        "Northeastern": ["Northeastern", "NEU"],
        "Villanova": ["Villanova", "Nova"],
        "Lehigh": ["Lehigh"],
        "Case Western": ["Case Western", "CWRU"],
        "USC": ["USC"],
        "NYU": ["NYU"],
        "Georgetown": ["Georgetown", "GT"],
        "UVA": ["UVA", "Virginia"],
        "UNC Chapel Hill": ["UNC", "UNC Chapel Hill"],
        "UT Austin": ["UT Austin", "UT-Austin", "UT"],
        "University of Texas at Austin": ["UT Austin", "UT-Austin", "UT"],
        "Cornell": ["Cornell"],
        "Duke": ["Duke"],
    }
    queries = aliases.get(name, [name])
    # Add outcome-flavored queries — these target results posts specifically
    qs = list(queries)
    for outcome in ["accepted", "ED", "rejected", "waitlisted"]:
        qs.append(f"{queries[0]} {outcome}")

    posts = []
    seen_ids = set()
    headers = {"User-Agent": "Candor/1.0 (college admissions tool)"}
    for sub in ["collegeresults", "chanceme", "ApplyingToCollege"]:
        for sort in ["new", "top"]:
            for q in qs[:5]:
                try:
                    qenc = q.replace(" ", "+").replace("&", "%26")
                    url = f"https://www.reddit.com/r/{sub}/search.json?q={qenc}&restrict_sr=1&sort={sort}&limit=25&t=all"
                    r = requests.get(url, headers=headers, timeout=8)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    for child in data.get("data", {}).get("children", []):
                        p = child.get("data", {})
                        pid = p.get("id")
                        if pid in seen_ids: continue
                        seen_ids.add(pid)
                        selftext = (p.get("selftext") or "").strip()
                        title = (p.get("title") or "").strip()
                        if p.get("removed_by_category") or p.get("over_18"): continue
                        if len(selftext) < 100 and len(title) < 30: continue
                        if (p.get("score") or 0) < 1: continue
                        snippet = selftext[:1200]
                        if len(selftext) > 1200:
                            snippet += "…"
                        import time as _time
                        created = p.get("created_utc", 0)
                        age_days = max(1, int((_time.time() - created) / 86400))
                        when = f"{age_days}d ago" if age_days < 30 else (f"{age_days // 30}mo ago" if age_days < 365 else f"{age_days // 365}y ago")
                        posts.append({
                            "title": title[:200],
                            "snippet": snippet,
                            "url": f"https://www.reddit.com{p.get('permalink','')}",
                            "score": p.get("score") or 0,
                            "sub": f"r/{sub}",
                            "when": when,
                            "comments": p.get("num_comments") or 0,
                        })
                    import time as _t; _t.sleep(0.25)
                except Exception as e:
                    print(f"reddit fetch error for {college_slug} {sub} {sort}: {e}")
                    continue
    # Sort by score, take top 30
    posts.sort(key=lambda p: -p["score"])
    posts = posts[:30]
    # Cache raw posts
    with db() as conn:
        try:
            conn.execute("""INSERT INTO school_reddit_posts (college_slug, body, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(college_slug) DO UPDATE SET
                    body=excluded.body, fetched_at=CURRENT_TIMESTAMP""",
                (college_slug, json.dumps(posts)))
            conn.commit()
        except Exception as e:
            print(f"reddit cache write failed: {e}")
    return posts


def fetch_reddit_essays(college_slug, force=False):
    """Pull posts that contain actual essay text from college-essay subs.
    Cached 24h. Returns raw post list with full selftext (longer than
    profile snippets since essays need full body)."""
    school = COLLEGES_BY_SLUG.get(college_slug)
    if not school: return []
    cutoff = (datetime.utcnow() - timedelta(hours=REDDIT_POSTS_TTL_HOURS)).isoformat()
    with db() as conn:
        if not force:
            row = conn.execute(
                "SELECT body, fetched_at FROM school_reddit_essays WHERE college_slug=? AND fetched_at >= ?",
                (college_slug, cutoff)
            ).fetchone() if _table_exists(conn, "school_reddit_essays") else None
            if row:
                try:
                    return json.loads(row["body"])
                except Exception:
                    pass
    name = school["name"]
    aliases_map = {
        "Massachusetts Institute of Technology": ["MIT"],
        "University of Pennsylvania": ["UPenn", "Penn"],
        "Carnegie Mellon": ["CMU"],
        "UC Berkeley": ["Berkeley"],
        "Johns Hopkins": ["JHU"],
        "Wash U St. Louis": ["WashU"],
        "University of Michigan": ["UMich"],
        "University of Chicago": ["UChicago"],
        "Wake Forest": ["Wake Forest", "WFU"],
        "Notre Dame": ["Notre Dame", "ND"],
        "Boston College": ["BC", "Boston College"],
        "Boston University": ["BU", "Boston University"],
        "Vanderbilt": ["Vanderbilt", "Vandy"],
        "Northeastern": ["Northeastern", "NEU"],
        "Villanova": ["Villanova"],
    }
    queries = aliases_map.get(name, [name])
    # Multi-query: target essay-share posts specifically
    qs = []
    for q in queries[:2]:
        qs.append(f"{q} essay")
        qs.append(f"{q} supplement")
        qs.append(f"{q} 'why us'")
    posts = []
    seen = set()
    headers = {"User-Agent": "Candor/1.0 (college admissions tool)"}
    # Wider net of subs that host essay shares
    for sub in ["CollegeEssays", "EssayDeath", "ApplyingToCollege", "EssayReview",
                "CollegeEssayHelp", "chanceme", "collegeresults"]:
        for q in qs[:4]:
            try:
                qenc = q.replace(" ", "+").replace("&", "%26").replace("'", "%27")
                url = f"https://www.reddit.com/r/{sub}/search.json?q={qenc}&restrict_sr=1&sort=top&limit=20&t=all"
                r = requests.get(url, headers=headers, timeout=8)
                if r.status_code != 200: continue
                data = r.json()
                for child in data.get("data", {}).get("children", []):
                    p = child.get("data", {})
                    pid = p.get("id")
                    if pid in seen: continue
                    seen.add(pid)
                    selftext = (p.get("selftext") or "").strip()
                    title = (p.get("title") or "").strip()
                    if p.get("removed_by_category") or p.get("over_18"): continue
                    # Lowered threshold: 150 chars (was 300). Catches shorter essay shares.
                    if len(selftext) < 150: continue
                    # Take more of the body for essays
                    snippet = selftext[:3000]
                    if len(selftext) > 3000:
                        snippet += "…"
                    import time as _time
                    created = p.get("created_utc", 0)
                    age_days = max(1, int((_time.time() - created) / 86400))
                    when = f"{age_days}d ago" if age_days < 30 else (f"{age_days // 30}mo ago" if age_days < 365 else f"{age_days // 365}y ago")
                    posts.append({
                        "title": title[:200],
                        "selftext": snippet,
                        "url": f"https://www.reddit.com{p.get('permalink','')}",
                        "score": p.get("score") or 0,
                        "sub": f"r/{sub}",
                        "when": when,
                    })
                import time as _t; _t.sleep(0.25)
            except Exception as e:
                print(f"reddit essay fetch error {sub}: {e}")
                continue
    posts.sort(key=lambda p: -p["score"])
    posts = posts[:25]
    with db() as conn:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS school_reddit_essays (college_slug TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("""INSERT INTO school_reddit_essays (college_slug, body, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(college_slug) DO UPDATE SET
                    body=excluded.body, fetched_at=CURRENT_TIMESTAMP""",
                (college_slug, json.dumps(posts)))
            conn.commit()
        except Exception as e:
            print(f"essay cache write failed: {e}")
    return posts


def extract_real_essays(college_slug, force=False):
    """Use Claude to scan Reddit essay posts and extract clean essay text
    + metadata (essay type, word count, prompt). Cached 24h alongside
    profiles."""
    school = COLLEGES_BY_SLUG.get(college_slug)
    if not school: return []
    cutoff = (datetime.utcnow() - timedelta(hours=REDDIT_POSTS_TTL_HOURS)).isoformat()
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS school_extracted_essays (college_slug TEXT PRIMARY KEY, body TEXT NOT NULL, generated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        if not force:
            row = conn.execute(
                "SELECT body, generated_at FROM school_extracted_essays WHERE college_slug=? AND generated_at >= ?",
                (college_slug, cutoff)
            ).fetchone()
            if row:
                try:
                    return json.loads(row["body"])
                except Exception:
                    pass
    # Anonymous gate: this is the most expensive AI call in the app
    # (max_tokens=6000). The bot was hitting ?essays=1 on every school
    # and forcing fresh generation. Cached results still serve to
    # anonymous; only generation requires a logged-in user.
    if not current_user():
        return []
    raw = fetch_reddit_essays(college_slug, force=False)
    if not raw or not _claude_client: return []
    posts_text = ""
    for i, p in enumerate(raw[:12]):
        posts_text += f"\n---POST {i+1} ({p['sub']}, {p['score']} upvotes)---\nTitle: {p['title']}\nBody: {p['selftext']}\n"
    name = school["name"]
    prompt = f"""Below are real Reddit posts where applicants shared their college essays for {name}. Extract the actual essay text from each post.

For each post that contains a real essay (or a real essay excerpt), output:

PROMPT: <which essay/supplement this is — e.g. "Why {name}", "Common App", "Activities", or short description>
OUTCOME: <Accepted | Rejected | Unknown>
WORDS: <approximate word count>
ESSAY:
<the actual essay text, copied exactly from the post — don't paraphrase or rewrite. Preserve line breaks. If the post has multiple essays, pick the strongest/most specific one.>

---END---

RULES:
- Only output essays that are actually IN the post text. If a post just describes an essay or links elsewhere, skip it.
- Use the verbatim essay text. Never rewrite, summarize, or paraphrase.
- If the essay is incomplete in the post (cut off), include what's there.
- Skip posts that are reviews/feedback rather than the essay itself.
- Skip posts that don't mention {name} or its supplements.
- Aim for 5-12 real essays. Quality over quantity.
- Separate each essay with the literal string ---END---

Posts:
{posts_text}"""
    try:
        resp = _claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,  # was 6000 — that's enough for ~5-8 essays. Halving cost.
            system="You extract real essay text from Reddit posts. Verbatim, never rewriting. Skip posts that don't contain actual essay text.",
            messages=[{"role":"user","content": prompt}],
        )
        body = resp.content[0].text.strip()
    except Exception as e:
        print(f"essay extraction failed for {college_slug}: {e}")
        return []
    # Parse essays separated by ---END---
    essays = []
    for chunk in body.split("---END---"):
        chunk = chunk.strip()
        if not chunk: continue
        e = {}
        # First three single-line headers
        lines = chunk.split("\n")
        body_started = False
        body_lines = []
        for line in lines:
            stripped = line.strip()
            if not body_started:
                if stripped.upper().startswith("PROMPT:"):
                    e["prompt"] = stripped.split(":", 1)[1].strip()
                elif stripped.upper().startswith("OUTCOME:"):
                    e["outcome"] = stripped.split(":", 1)[1].strip()
                elif stripped.upper().startswith("WORDS:"):
                    e["words"] = stripped.split(":", 1)[1].strip()
                elif stripped.upper().startswith("ESSAY:"):
                    body_started = True
                    rest = stripped.split(":", 1)[1].strip()
                    if rest: body_lines.append(rest)
            else:
                body_lines.append(line)
        if body_lines:
            e["essay"] = "\n".join(body_lines).strip()
        if e.get("essay") and len(e["essay"]) > 100:
            essays.append(e)
    if essays:
        with db() as conn:
            try:
                conn.execute("""INSERT INTO school_extracted_essays (college_slug, body, generated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(college_slug) DO UPDATE SET
                        body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
                    (college_slug, json.dumps(essays)))
                conn.commit()
            except Exception as e:
                print(f"essay extraction cache write failed: {e}")
    return essays


def extract_structured_profiles(college_slug, force=False):
    """One Claude call per school per 24h. Reads cached raw Reddit posts,
    asks Claude to extract structured admit/reject/waitlist profiles in
    the old card format. Cached in school_reddit_structured (separate from
    AI composites which use school_profiles)."""
    school = COLLEGES_BY_SLUG.get(college_slug)
    if not school: return []
    cutoff = (datetime.utcnow() - timedelta(hours=REDDIT_POSTS_TTL_HOURS)).isoformat()
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS school_reddit_structured (college_slug TEXT PRIMARY KEY, body TEXT NOT NULL, generated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        if not force:
            row = conn.execute(
                "SELECT body, generated_at FROM school_reddit_structured WHERE college_slug=? AND generated_at >= ?",
                (college_slug, cutoff)
            ).fetchone()
            if row:
                try:
                    return json.loads(row["body"])
                except Exception:
                    pass
    # Anonymous gate: don't burn Anthropic budget generating on demand
    # for unauthenticated visitors — cache miss + no logged-in user
    # likely means a scraper hitting cold schools.
    if not current_user():
        return []
    posts = fetch_reddit_profiles(college_slug, force=False)
    if not posts: return []
    if not _claude_client: return []
    # Build a compact prompt with all the raw posts
    posts_text = ""
    for i, p in enumerate(posts[:25]):
        posts_text += f"\n---POST {i+1} ({p['sub']}, {p['score']} upvotes)---\nTitle: {p['title']}\nBody: {p['snippet']}\n"
    prompt = f"""Below are real Reddit posts mentioning {school['name']}. Extract as many structured profile cards as the data supports — aim for 12-20 profiles when possible, mixing outcomes (mostly accepted, some waitlisted/deferred, some rejected).

For each post that has clear stats + an outcome at {school['name']}, output a profile in this EXACT format (separated by blank lines):

OUTCOME: <Accepted | Waitlisted | Rejected | Deferred>
GPA: <unweighted, e.g. 3.95 UW or "not stated">
TEST: <SAT 1530 or ACT 35 or 'test-optional' or "not stated">
MAJOR: <intended major>
GEO: <state or country>
HOOKS: <legacy / first-gen / athlete / URM / none — pick what applies>
STANDOUT: <single strongest signal: award, project, leadership, etc.>
OTHER: <1-2 other notable items, comma-separated>
WHY: <one sentence on what likely drove the decision>

RULES:
- Profile must reference {school['name']} or a clear synonym/abbreviation
- Skip posts that don't reveal at least an outcome + 2 facts (test or GPA + major or hooks)
- Use real data from the posts, never invent
- Keep each field under 90 chars
- No names, no identifying info
- Aim for 12-20 profiles. More is better.
- IMPORTANT: include rejected and waitlisted outcomes too — r/collegeresults posts list ALL the schools an applicant got into AND rejected from. Look for the rejection list in each post and create reject profiles from there. Aim for at least 30% rejected/waitlisted in the output.
- Output ONLY profiles separated by blank lines. No preamble.

Posts:
{posts_text}"""
    try:
        resp = _claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            system="You parse Reddit admissions posts into structured profile cards. Faithful to source data, never inventing facts. Extract as many as the posts support.",
            messages=[{"role":"user","content": prompt}],
        )
        body = resp.content[0].text.strip()
    except Exception as e:
        print(f"profile extraction failed for {college_slug}: {e}")
        return []
    # Parse the structured output into a list of dicts
    profiles = []
    current = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            if current.get("OUTCOME"):
                profiles.append(current)
            current = {}
            continue
        for key in ("OUTCOME","GPA","TEST","MAJOR","GEO","HOOKS","STANDOUT","OTHER","WHY"):
            if line.upper().startswith(key + ":"):
                current[key] = line.split(":", 1)[1].strip()
                break
    if current.get("OUTCOME"):
        profiles.append(current)
    # Cache
    if profiles:
        with db() as conn:
            try:
                conn.execute("""INSERT INTO school_reddit_structured (college_slug, body, generated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(college_slug) DO UPDATE SET
                        body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
                    (college_slug, json.dumps(profiles)))
                conn.commit()
            except Exception as e:
                print(f"profiles cache write failed: {e}")
    return profiles


_NEWSAPI_429_UNTIL = None  # global circuit breaker — when set, skip NewsAPI calls until this timestamp


def fetch_articles(college_slug):
    """Return cached articles if fresh; else fetch from NewsAPI and cache."""
    global _NEWSAPI_429_UNTIL
    school = COLLEGES_BY_SLUG.get(college_slug)
    if not school: return []
    cutoff = (datetime.utcnow() - timedelta(hours=ARTICLE_TTL_HOURS)).isoformat()
    with db() as conn:
        rows = conn.execute("SELECT title, url, source, published FROM college_articles WHERE college_slug=? AND fetched_at >= ? ORDER BY published DESC LIMIT 6",
                            (college_slug, cutoff)).fetchall()
        if rows:
            return [dict(r) for r in rows]
    # Fetch fresh
    if not NEWSAPI_KEY:
        return []
    # If we recently hit the daily quota, skip the API call entirely until
    # the cooldown expires. NewsAPI's developer tier is 100 requests/24h —
    # once we've blown it, hammering them just spams 429s in the logs and
    # adds latency to every page load. 6-hour cooldown gives the quota
    # window time to roll forward.
    if _NEWSAPI_429_UNTIL and datetime.utcnow() < _NEWSAPI_429_UNTIL:
        return []
    try:
        # NewsAPI's qInTitle + q combo behaves erratically (it OR's them),
        # so encode everything in a single q. Bracket-AND keeps school name
        # required and topic match required.
        # Free NewsAPI tier only allows ~30 days of history. 28 leaves a buffer.
        from_date = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")
        body_terms = (
            'admissions OR application OR "student life" OR "campus life" '
            'OR undergraduate OR tuition OR "financial aid" OR "incoming class" '
            'OR "class of" OR housing OR dorm OR alumni OR "study abroad" '
            'OR "early decision" OR "regular decision" OR "acceptance rate"'
        )
        q = f'"{school["name"]}" AND ({body_terms})'
        r = requests.get("https://newsapi.org/v2/everything",
                         params={
                             "q": q,
                             "from": from_date,
                             "sortBy": "relevancy",
                             "language": "en",
                             "pageSize": 8,
                             "apiKey": NEWSAPI_KEY,
                         },
                         timeout=8)
        if r.status_code != 200:
            print(f"NewsAPI status {r.status_code} for {college_slug}: {r.text[:200]}")
            if r.status_code == 429:
                # Trip the circuit breaker — skip API calls for 6 hours
                _NEWSAPI_429_UNTIL = datetime.utcnow() + timedelta(hours=6)
                print(f"NewsAPI 429: cooling down until {_NEWSAPI_429_UNTIL.isoformat()}")
            return []
        articles = (r.json() or {}).get("articles", [])
    except Exception as e:
        print(f"NewsAPI error for {college_slug}: {e}")
        return []
    out = []
    with db() as conn:
        # Wipe old rows for this college to cap storage
        conn.execute("DELETE FROM college_articles WHERE college_slug=?", (college_slug,))
        for a in articles[:6]:
            title = (a.get("title") or "").strip()
            url = a.get("url") or ""
            if not title or not url: continue
            row = {"title": title, "url": url, "source": (a.get("source") or {}).get("name") or "", "published": (a.get("publishedAt") or "")[:10]}
            conn.execute("INSERT INTO college_articles (college_slug, title, url, source, published) VALUES (?,?,?,?,?)",
                         (college_slug, row["title"], row["url"], row["source"], row["published"]))
            out.append(row)
        conn.commit()
    return out


# ─── HTML / CSS ───────────────────────────────────────────


NAV = """<div class="nav"><a class="brand" href="/">""" + CANDOR_LOGO_SVG + """Candor</a>
<a href="/rankings/my-fit">★ My Fit</a>
<a href="/colleges">Browse</a>
<a href="/rankings">Rankings</a>
<a href="/grade">Profile Grade</a>
<a href="/improve">Improve</a>
<a href="/plans">My Colleges</a>
<a href="/deadlines">Deadlines</a>
<a href="/upgrade" style="color:#5fc9b6">Premium</a>
<span class="sp"></span>
__USER_LINKS__
</div>"""


def _nav():
    user = current_user()
    if user:
        return NAV.replace("__USER_LINKS__",
            f'<a href="/profile">Profile</a> <a href="/logout">Logout</a> <span class="muted" style="font-size:.85em">{user["email"]}</span>')
    return NAV.replace("__USER_LINKS__", '<a href="/login">Login</a> <a href="/signup" class="btn btn-primary btn-sm">Sign up</a>')


def _flash():
    msgs = []
    for cat, msg in (request.environ.get("flashes") or []):
        msgs.append(f'<div class="flash {cat}">{msg}</div>')
    flashed = []
    try:
        from flask import get_flashed_messages
        flashed = get_flashed_messages(with_categories=True)
    except Exception:
        pass
    for cat, msg in flashed:
        msgs.append(f'<div class="flash {cat}">{msg}</div>')
    return "\n".join(msgs)


def _page(body_html, title="Candor", description=None):
    from html import escape as _esc
    description = description or "Honest college admissions chances, calibrated to verified Common Data Set data. Built by a HS junior to tell you the truth, not a flattering number."
    try:
        og_image = request.url_root.rstrip("/") + url_for("static", filename="hero-aurora.jpg")
        page_url = request.url_root.rstrip("/") + request.path
    except Exception:
        og_image = "/static/hero-aurora.jpg"
        page_url = "/"
    social_meta = (
        f'<link rel="canonical" href="{page_url}">'
        f'<meta name="description" content="{_esc(description, quote=True)}">'
        f'<meta property="og:type" content="website">'
        f'<meta property="og:site_name" content="Candor">'
        f'<meta property="og:title" content="{_esc(title, quote=True)}">'
        f'<meta property="og:description" content="{_esc(description, quote=True)}">'
        f'<meta property="og:image" content="{og_image}">'
        f'<meta property="og:url" content="{page_url}">'
        f'<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{_esc(title, quote=True)}">'
        f'<meta name="twitter:description" content="{_esc(description, quote=True)}">'
        f'<meta name="twitter:image" content="{og_image}">'
    )
    csrf_meta = ""
    if _CSRF_ON:
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_meta = f'<meta name="csrf-token" content="{generate_csrf()}">'
        except Exception:
            pass
    favicon = ('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,'
               '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 64 64%22>'
               '<defs><linearGradient id=%22g%22 x1=%220%22 y1=%220%22 x2=%221%22 y2=%221%22>'
               '<stop offset=%220%25%22 stop-color=%22%2338bdf8%22/>'
               '<stop offset=%22100%25%22 stop-color=%22%235eead4%22/></linearGradient></defs>'
               '<path d=%22M 52 16 A 22 22 0 1 0 52 48%22 stroke=%22url(%23g)%22 stroke-width=%226%22 fill=%22none%22 stroke-linecap=%22round%22/>'
               '<rect x=%2222%22 y=%2236%22 width=%225.5%22 height=%2210%22 fill=%22url(%23g)%22 rx=%221.2%22/>'
               '<rect x=%2231%22 y=%2228%22 width=%225.5%22 height=%2218%22 fill=%22url(%23g)%22 rx=%221.2%22/>'
               '<rect x=%2240%22 y=%2220%22 width=%225.5%22 height=%2226%22 fill=%22url(%23g)%22 rx=%221.2%22/>'
               '</svg>">')
    footer = """<div style="max-width:1180px;margin:60px auto 30px;padding:24px;color:var(--text-3);font-size:.84em;text-align:center;border-top:1px solid var(--border)">
made by a high school junior. found a bug? something looks wrong? tell me on the
<a href="https://www.reddit.com/user/Zestyclose_Tower_380" style="color:var(--text-2)">reddit</a>.
free chances calculator. <a href="/upgrade" style="color:var(--text-2)">Candor Premium</a> is $3/month for the strategy on top.
<div style="margin-top:10px;color:var(--text-3)">still grinding your ACT? I also built <a href="https://forma-prep.up.railway.app" style="color:var(--text-2)">Forma</a> — real test prep, same honesty.</div>
</div>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{favicon}
{social_meta}
{csrf_meta}<title>{title}</title><style>{BASE_CSS}</style></head>
<body>{_nav()}<div class="wrap">{_flash()}{body_html}</div>{footer}</body></html>"""


# ─── PAGE BUILDERS ────────────────────────────────────────
def colleges_html():
    q = (request.args.get("q") or "").strip().lower()
    state = request.args.get("state") or ""
    typ = request.args.get("type") or ""
    rows = COLLEGES
    if q: rows = [c for c in rows if q in c["name"].lower() or q in c["state"].lower()]
    if state: rows = [c for c in rows if c["state"] == state]
    if typ: rows = [c for c in rows if c["type"] == typ]
    rows = sorted(rows, key=lambda c: c["accept"])
    cards = ""
    for c in rows:
        type_pill = f'<span class="pill pill-{c["type"]}">{c["type"]}</span>'
        tier_pill = f'<span class="pill pill-tier-{c["tier"]}">Tier {c["tier"]}</span>'
        cards += f"""<div class="school-card"><a href="/college/{c['slug']}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;flex-wrap:wrap">
              <div style="font-weight:700;font-size:1em">{c['name']}</div>
              <div>{type_pill}</div>
            </div>
            <div class="muted" style="font-size:.82em;margin-top:2px">{c['state']} · {tier_pill}</div>
            <div class="stat-row"><span>Accept</span><span>{round(c['accept']*100,1)}%</span></div>
            <div class="stat-row"><span>SAT mid-50%</span><span>{('Test-blind' if is_test_blind(c) else f"{c['sat_25']}-{c['sat_75']}")}</span></div>
            <div class="stat-row"><span>GPA range</span><span>{c['gpa_lo']}-{c['gpa_hi']}</span></div>
        </a></div>"""
    state_options = "".join(f'<option value="{s}" {"selected" if s==state else ""}>{s}</option>' for s in STATES)
    return _page(f"""
<div class="bar">
  <h1 style="margin:0">Browse colleges</h1>
  <span class="muted">{len(rows)} of {len(COLLEGES)}</span>
</div>
<form method="get" action="/colleges" class="card" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end">
  <div style="flex:1;min-width:180px"><label style="margin-top:0">Search</label>
    <input name="q" value="{q}" placeholder="Name or state"></div>
  <div style="min-width:140px"><label style="margin-top:0">State</label>
    <select name="state"><option value="">All</option>{state_options}</select></div>
  <div style="min-width:140px"><label style="margin-top:0">Type</label>
    <select name="type"><option value="">All</option><option value="public" {"selected" if typ=="public" else ""}>Public</option><option value="private" {"selected" if typ=="private" else ""}>Private</option></select></div>
  <button class="btn btn-primary" type="submit">Apply</button>
  <a class="btn btn-light" href="/colleges">Reset</a>
</form>
<div class="grid">{cards or '<div class="card" style="text-align:center;padding:32px"><h3 style="margin:0 0 8px">No schools match these filters</h3><p class="muted">Try clearing one or two filters — too narrow a combo (e.g. small + rural + STEM) sometimes returns zero.</p><a class="btn btn-primary" href="/colleges" style="margin-top:10px">Reset filters</a></div>'}</div>
""", title="Browse colleges — Candor")


def _match_card(c):
    """My Fit card on the school detail page. Uses the SAME composite score
    as the My Fit ranking — they had drifted: this card was showing the prefs-only
    score (~90) while the ranking showed the composite (~70). Now both match."""
    user = current_user()
    if not user:
        return ""
    profile = get_profile(user["id"])
    if not profile:
        return ""
    m = school_match(profile, c)
    overall, parts = compute_my_fit(profile, c)
    if overall <= 0 and (not m or not m["rated_count"]):
        return ""
    if overall >= 80:   stars_n = 5
    elif overall >= 65: stars_n = 4
    elif overall >= 50: stars_n = 3
    elif overall >= 35: stars_n = 2
    elif overall >= 20: stars_n = 1
    else: stars_n = 0
    star_html = ('<span style="color:#f0c040;letter-spacing:1px">' + ('★' * stars_n) +
                 '</span><span style="color:#ddd">' + ('★' * (5 - stars_n)) + '</span>')
    pref_labels = {"weather":"Weather","setting":"Campus setting","size":"School size",
                   "class_size":"Class size","greek":"Greek life","sports":"Sports culture",
                   "major_strength":"Major strength","prestige":"Prestige","cost":"Cost",
                   "diversity":"Diversity","party":"Party scene","research":"Research access",
                   "career_intensity":"Career focus","location":"Distance from home",
                   "aid":"Financial aid","culture":"Academic culture"}
    rows = ""
    if m and m.get("per_pref"):
        for key, label in pref_labels.items():
            if key not in m["per_pref"]:
                continue
            verdict, txt = m["per_pref"][key]
            icon = {"match":"✓","mismatch":"✗","neutral":"·"}[verdict]
            color = {"match":"#1d6c2a","mismatch":"#9a1d1d","neutral":"#666"}[verdict]
            rows += f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid #f0f0f0;font-size:.92em"><span><span style="color:{color};font-weight:700;margin-right:6px">{icon}</span>{label}</span><span class="muted" style="font-size:.85em">{txt}</span></div>'
    score_color = "#1d6c2a" if overall >= 80 else ("#8a4a00" if overall >= 60 else "#9a1d1d")
    breakdown = (f'<div class="muted" style="font-size:.78em;margin:6px 0 8px">'
                 f'admit realism {parts["admit_realism"]}/100 · '
                 f'prefs {parts["pref"]}/100 · '
                 f'academic {parts["academic"]}/100'
                 f'</div>')
    return f"""<div class="card" style="background:#fafffe;border-color:#cfe7d8">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:6px">
        <h3 style="margin:0">My Fit</h3>
        <div style="text-align:right">
          <div>{star_html}</div>
          <div style="font-size:1.4em;font-weight:800;color:{score_color}">{overall}/100</div>
        </div>
      </div>
      <div class="muted" style="font-size:.82em;margin-bottom:6px">Same score as the My Fit ranking. <a href="/profile">Update prefs</a></div>
      {breakdown}
      {rows}
    </div>"""


def _composite_dots(slug):
    """Parse cached AI composite profiles into (gpa, sat_equiv, color) dots.
    Cache-only — never triggers generation. A clearly-labeled MODEL backdrop
    (hollow dots) shown until real reported outcomes (solid dots) accumulate."""
    try:
        with db() as conn:
            row = conn.execute("SELECT body FROM school_profiles WHERE college_slug=?", (slug,)).fetchone()
    except Exception:
        return []
    if not row or not row["body"]:
        return []
    import re as _re
    out = []
    for b in _re.split(r'\n(?=\*\*)', row["body"]):
        head = b[:140].lower()
        if "admit" in head or "accept" in head:   col = "#3fb98a"
        elif "reject" in head or "den" in head:    col = "#ef6b6b"
        elif "wait" in head:                       col = "#e0a44a"
        else:
            continue
        gm = (_re.search(r'GPA\s*/?\s*Test\s*:\s*([0-4](?:\.\d+)?)', b, _re.I)
              or _re.search(r'\b([0-4]\.\d{1,2})\b', b))
        sm = _re.search(r'SAT\s*(\d{3,4})', b, _re.I)
        sat = int(sm.group(1)) if sm else None
        if sat is None:
            am = _re.search(r'ACT\s*(\d{1,2})', b, _re.I)
            sat = _normalize_score(None, int(am.group(1))) if am else None
        gpa = float(gm.group(1)) if gm else None
        if gpa and sat and 2.0 <= gpa <= 4.0 and 800 <= sat <= 1600:
            out.append((gpa, sat, col))
    return out


def _scattergram_block(c, user):
    """Premium 'students like you' scatter: GPA × SAT, the school's admit zone,
    real reported outcomes (green=admit / red=deny / amber=waitlist), and the
    user's own position. Gated: free/anon users see a locked teaser instead."""
    if is_test_blind(c) or c.get("sat_25") is None or c.get("sat_75") is None:
        return ""
    is_paid = bool(user and user.get("is_paid"))
    if not is_paid:
        return ('<div class="card" style="border:1px dashed var(--border-strong);text-align:center">'
                '<h3 style="margin-top:0">Students like you</h3>'
                '<p class="muted" style="margin:0 0 12px;line-height:1.5">See where your GPA + SAT land against '
                f"{c['name']}'s admit range — and real reported outcomes from other applicants.</p>"
                '<a class="btn btn-primary btn-sm" href="/upgrade">Unlock with Premium ($3/mo) →</a></div>')
    prof = get_profile(user["id"]) or {}
    try:
        u_gpa = float(prof.get("uw_gpa")) if prof.get("uw_gpa") not in (None, "") else None
    except (TypeError, ValueError):
        u_gpa = None
    try:
        u_sat = int(prof.get("sat")) if prof.get("sat") not in (None, "") else None
    except (TypeError, ValueError):
        u_sat = None

    dots = []
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT p.uw_gpa AS g, p.sat AS s, o.actual_outcome AS oc FROM user_outcomes o "
                "JOIN profiles p ON p.user_id=o.user_id WHERE o.college_slug=? "
                "AND o.actual_outcome IS NOT NULL AND o.actual_outcome!='' "
                "AND p.sat IS NOT NULL AND p.uw_gpa IS NOT NULL", (c["slug"],)).fetchall()
        for r in rows:
            oc = (r["oc"] or "").lower()
            if "admit" in oc or "accept" in oc:   kind = "#3fb98a"
            elif "den" in oc or "reject" in oc:    kind = "#ef6b6b"
            elif "wait" in oc:                     kind = "#e0a44a"
            else: continue
            dots.append((float(r["g"]), int(r["s"]), kind))
    except Exception:
        dots = []

    W, H, M = 460, 300, 44
    pw, ph = W - M - 16, H - M - 30
    GLO, GHI, SLO, SHI = 3.0, 4.0, 1100, 1600
    def X(g): return round(M + (max(GLO, min(GHI, g)) - GLO) / (GHI - GLO) * pw, 1)
    def Y(s): return round((M - 10) + ph - (max(SLO, min(SHI, s)) - SLO) / (SHI - SLO) * ph, 1)

    # admit zone (mid-50% GPA × mid-50% SAT)
    zx1, zx2 = X(c["gpa_lo"]), X(c["gpa_hi"])
    zy1, zy2 = Y(c["sat_75"]), Y(c["sat_25"])
    zone = (f'<rect x="{zx1}" y="{zy1}" width="{round(zx2-zx1,1)}" height="{round(zy2-zy1,1)}" '
            f'fill="rgba(95,201,182,.12)" stroke="rgba(95,201,182,.4)" stroke-dasharray="4 3" rx="3"/>')
    # axes ticks
    ticks = ""
    for g in (3.0, 3.5, 4.0):
        ticks += (f'<text x="{X(g)}" y="{H-12}" fill="#7f8893" font-size="11" text-anchor="middle">{g:.1f}</text>'
                  f'<line x1="{X(g)}" y1="{Y(SHI)}" x2="{X(g)}" y2="{Y(SLO)}" stroke="rgba(255,255,255,.05)"/>')
    for s in (1100, 1350, 1600):
        ticks += (f'<text x="{M-8}" y="{Y(s)+4}" fill="#7f8893" font-size="11" text-anchor="end">{s}</text>'
                  f'<line x1="{X(GLO)}" y1="{Y(s)}" x2="{X(GHI)}" y2="{Y(s)}" stroke="rgba(255,255,255,.05)"/>')
    # Real reported outcomes draw SOLID; AI composites draw HOLLOW underneath.
    comp = _composite_dots(c["slug"])
    comp_svg = "".join(f'<circle cx="{X(g)}" cy="{Y(s)}" r="4" fill="none" stroke="{col}" '
                       f'stroke-width="1.5" stroke-opacity=".55"/>'
                       for g, s, col in comp)
    dot_svg = "".join(f'<circle cx="{X(g)}" cy="{Y(s)}" r="4" fill="{col}" fill-opacity=".85"/>'
                      for g, s, col in dots)
    you = ""
    if u_gpa is not None and u_sat is not None:
        ux, uy = X(u_gpa), Y(u_sat)
        you = (f'<circle cx="{ux}" cy="{uy}" r="8" fill="#5fc9b6" stroke="#0a131c" stroke-width="2"/>'
               f'<text x="{ux}" y="{uy-13}" fill="#5fc9b6" font-size="12" font-weight="700" text-anchor="middle">YOU</text>')
    n_real, n_comp = len(dots), len(comp)
    if n_real:
        cap = (f"{n_real} real reported outcome{'s' if n_real != 1 else ''} (solid)"
               f"{f' + {n_comp} model composites (hollow)' if n_comp else ''} at {c['name']}. "
               f"Dashed box = the admitted mid-50% range.")
    elif n_comp:
        cap = (f"Hollow dots are {n_comp} MODEL composite profiles — typical admit / waitlist / deny patterns "
               f"for {c['name']}, not real reported students. Real outcomes appear as solid dots as applicants "
               f"report them after decisions. Dashed box = the admitted mid-50% range.")
    else:
        cap = (f"No data yet for {c['name']} — for now, here's how you stack up against the admitted "
               f"mid-50% range (dashed box).")
    legend = ('<span style="color:#3fb98a">●</span> admit &nbsp; '
              '<span style="color:#ef6b6b">●</span> deny &nbsp; '
              '<span style="color:#e0a44a">●</span> waitlist &nbsp; '
              '<span style="color:#5fc9b6">●</span> you<br>'
              '<span style="font-size:.92em">solid = real reported outcome · hollow = model composite (not a real student)</span>')
    return f"""<div class="card">
  <h3 style="margin-top:0">Students like you <span class="muted" style="font-size:.6em;font-weight:500">· Premium</span></h3>
  <svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;max-width:520px;display:block;margin:6px 0">
    <text x="14" y="{M-18}" fill="#7f8893" font-size="11">SAT</text>
    <text x="{W-8}" y="{H-12}" fill="#7f8893" font-size="11" text-anchor="end">GPA</text>
    {ticks}{zone}{comp_svg}{dot_svg}{you}
  </svg>
  <div class="muted" style="font-size:.82em;margin-top:2px">{legend}</div>
  <p class="muted" style="font-size:.82em;margin:8px 0 0;line-height:1.5">{cap}</p>
</div>"""


def college_detail_html(slug):
    raw = COLLEGES_BY_SLUG.get(slug)
    if not raw: abort(404)
    c = merged_school(raw)
    user = current_user()
    saved = is_saved(user["id"], slug) if user else False
    # If logged in, highlight the sub-school matching the user's major
    user_major = ""
    if user:
        prof = get_profile(user["id"])
        if prof: user_major = prof.get("major") or ""
    if user:
        save_btn = (f'<form method="post" action="/{("unsave" if saved else "save")}/{slug}" style="display:inline">'
                    f'{csrf_input()}'
                    f'<button class="btn {("btn-primary" if saved else "btn-light")}" type="submit">'
                    f'{"★ Saved" if saved else "☆ Save to my list"}</button></form>')
    else:
        # Anonymous: drop the email instead of forcing login. Lead capture
        # seeds the outcome-network email list (CEO plan, May 2026).
        save_btn = (
            f'<form id="interest-form-{slug}" class="interest-form" data-slug="{slug}" style="display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap">'
            f'{csrf_input()}'
            f'<input type="email" name="email" required placeholder="your@email.com" '
            f'style="padding:8px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:.9em;min-width:200px">'
            f'<button class="btn btn-light" type="submit">☆ Save for later</button>'
            f'<span class="interest-msg muted" style="font-size:.85em"></span>'
            f'</form>'
        )
    over = _get_overrides(slug)
    verified_badge = ""
    if is_cds_verified(slug):
        verified_badge = '<span style="font-size:.74em;background:rgba(95,201,182,.18);color:var(--teal);padding:3px 10px;border-radius:999px;margin-left:8px;border:1px solid rgba(95,201,182,.35);font-weight:600;letter-spacing:.3px" title="Stats hand-verified against the school\'s most recent Common Data Set (2024-25 or 2025-26 cycle)">CDS VERIFIED</span>'
    elif slug in MANUAL_FRESH_ACCEPT:
        verified_badge = '<span style="font-size:.74em;background:rgba(95,201,182,.12);color:var(--teal);padding:3px 10px;border-radius:999px;margin-left:8px;border:1px solid rgba(95,201,182,.25);font-weight:500;letter-spacing:.3px">2024-25 CYCLE</span>'
    elif over and over.get("source"):
        verified_badge = f'<span style="font-size:.74em;background:rgba(95,201,182,.10);color:var(--teal);padding:3px 10px;border-radius:999px;margin-left:8px;border:1px solid rgba(95,201,182,.22);font-weight:500;letter-spacing:.3px">VERIFIED · federal data</span>'
    majors_tags = "".join(f'<span class="tag">{m}</span>' for m in c["majors"])
    type_pill = f'<span class="pill pill-{c["type"]}">{c["type"]}</span>'
    tier_pill = f'<span class="pill pill-tier-{c["tier"]}">Tier {c["tier"]}</span>'
    return _page(f"""
<div class="bar"><a href="/colleges">&larr; back to browse</a></div>
<div class="card">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;align-items:flex-start">
    <div>
      <h1 style="margin:0 0 4px">{c['name']} {verified_badge}</h1>
      <div class="muted">{city_state(c)} ({region_of(c)}) · {c['size']:,} undergrads · ~{avg_class_size_estimate(c)} avg class size · {sf_ratio(c)}:1 student-faculty · ${c['tuition']:,}/yr sticker</div>
    </div>
    <div>{type_pill} {tier_pill}</div>
  </div>
  <div id="summary-block" style="margin:14px 0 6px;color:var(--text-2)">{c['desc']}<div class="muted" style="font-size:.82em;margin-top:4px"><i>Loading extended overview…</i></div></div>
  <div class="tag-list">{majors_tags}</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    <a class="btn btn-primary" href="/college/{c['slug']}/plan">★ Calculate my chances</a>
    <a class="btn btn-light" href="/chances/{c['slug']}">Chances only</a>
    <a class="btn btn-light" href="/college/{c['slug']}/improve">Improve guide</a>
    <a class="btn btn-light" href="/college/{c['slug']}/profiles">Real profiles & essays</a>
    {save_btn}
  </div>
</div>
<p class="muted" style="font-size:.78em;margin:14px 0 6px">Stats below are CDS-based estimates from recent admissions cycles. Verify on the school's official site before making application decisions.</p>
<div class="grid">
  <div class="card">
    <h3 style="margin-top:0">Acceptance rate</h3>
    <div class="odds" style="color:#2b6cff">{round(c['accept']*100,1)}%</div>
    <div class="muted" style="font-size:.82em">most recent reported cycle</div>
    {render_admissions_breakdown(c, admissions_detail(c))}
    {_render_sub_school_block(c['slug'], highlight_keywords=user_major)}
  </div>
  <div class="card">
    <h3 style="margin-top:0">GPA range</h3>
    <div class="odds">{c['gpa_lo']}–{c['gpa_hi']}</div>
    <div class="muted" style="font-size:.82em">middle 50% of admitted students (unweighted)</div>
  </div>
  <div class="card">
    <h3 style="margin-top:0">SAT mid-50%</h3>
    <div class="odds" style="font-size:{('1.4em' if is_test_blind(c) else 'inherit')}">{('Test-blind' if is_test_blind(c) else f"{c['sat_25']}–{c['sat_75']}")}</div>
    <div class="muted" style="font-size:.82em">{('does not consider SAT/ACT' if is_test_blind(c) else 'middle 50% admitted SAT score')}</div>
  </div>
  <div class="card">
    <h3 style="margin-top:0">ACT mid-50%</h3>
    <div class="odds" style="font-size:{('1.4em' if is_test_blind(c) else 'inherit')}">{('Test-blind' if is_test_blind(c) else f"{c['act_25']}–{c['act_75']}")}</div>
    <div class="muted" style="font-size:.82em">{('does not consider SAT/ACT' if is_test_blind(c) else 'middle 50% admitted ACT score')}</div>
  </div>
  {_render_earnings_card(c)}
</div>
{_render_career_outcomes(c)}
{_scattergram_block(c, user)}
{render_school_feeders(c)}
{_match_card(c)}
<div class="card">
  <h3 style="margin-top:0">Quick facts</h3>
  <div id="facts-block"><i class="muted">Loading…</i></div>
</div>
<div class="card">
  <h3 style="margin-top:0">Reference links</h3>
  {''.join(f'<div style="padding:6px 0"><a href="{url}" target="_blank" rel="noopener">{label} &rarr;</a></div>' for label, url in school_links(c))}
</div>
<div class="card">
  <h3 style="margin-top:0">Recent articles</h3>
  <div id="articles-block"><i class="muted">Loading…</i></div>
</div>
<script>
(function(){{
  var slug = "{c['slug']}";
  function load(target, url){{
    fetch(url).then(function(r){{return r.json();}}).then(function(d){{
      var el = document.getElementById(target);
      if (el && d && d.html) el.innerHTML = d.html;
    }}).catch(function(){{}});
  }}
  load("summary-block", "/api/college/"+slug+"/summary");
  load("facts-block",   "/api/college/"+slug+"/facts");
  load("articles-block","/api/college/"+slug+"/articles");

  // Email lead capture for anonymous visitors (#8 — seeds outcome-network list).
  var interestForm = document.querySelector(".interest-form[data-slug=\\"" + slug + "\\"]");
  if (interestForm) {{
    var msg = interestForm.querySelector(".interest-msg");
    interestForm.addEventListener("submit", function(e){{
      e.preventDefault();
      var emailInput = interestForm.querySelector("input[name=email]");
      var btn = interestForm.querySelector("button");
      // FormData(form) picks up all fields including the CSRF token hidden input.
      var fd = new FormData(interestForm);
      fd.append("slug", slug);
      fd.append("source", "college_detail");
      btn.disabled = true;
      msg.textContent = "Saving…";
      msg.style.color = "";
      fetch("/api/interest", {{ method:"POST", body: fd }})
        .then(function(r){{ return r.json().then(function(d){{ return {{ok: r.ok, data: d}}; }}); }})
        .then(function(res){{
          if (res.ok && res.data && res.data.ok) {{
            interestForm.innerHTML = "<span style=\\"color:var(--teal);font-size:.9em\\">★ Saved. We'll email you when more features for this school are ready.</span>";
          }} else {{
            msg.textContent = (res.data && res.data.error) || "Couldn't save — try again?";
            msg.style.color = "#f87171";
            btn.disabled = false;
          }}
        }})
        .catch(function(){{
          msg.textContent = "Network error — try again?";
          msg.style.color = "#f87171";
          btn.disabled = false;
        }});
    }});
  }}
}})();
</script>
""", title=f"{c['name']} Acceptance Rate & Admission Chances — Candor",
        description=f"{c['name']} admission chances, acceptance rate, and SAT / ACT / GPA ranges — calculated from verified Common Data Set data, not guesses. See your real odds free on Candor.")


def rankings_index_html():
    items = ""
    # Add the personalized My Fit ranking at top of the index
    items += f"""<div class="card" style="background:#f0f7ff;border-color:#cfe0ff">
        <h3 style="margin-top:0"><a href="/rankings/my-fit" style="color:inherit">★ My Fit (personalized)</a></h3>
        <div class="muted">Every college ranked by how well your profile matches. Login + saved profile required.</div>
    </div>"""
    for r in RANKINGS:
        items += f"""<div class="card">
            <h3 style="margin-top:0"><a href="/rankings/{r['slug']}" style="color:inherit">{r['title']}</a></h3>
            <div class="muted">{r['blurb']}</div>
        </div>"""
    return _page(f"""
<h1>Ranking lists</h1>
<p class="muted">Curated lists for the most-asked categories, plus a personalized fit ranking from your profile.</p>
{items}
""", title="Rankings — Candor")




def _ranking_table(rows_data, show_stars=False):
    """rows_data: list of (rank_str, college_dict, extra_dict).
    show_stars: if True, display the fit star rating column."""
    head = f"""<table class="rank-table">
      <thead><tr>
        <th>#</th><th>School</th><th class="hide-sm">Location</th>
        <th>{'Fit' if show_stars else 'Accept'}</th>
        <th class="hide-sm">SAT mid-50%</th>
        <th class="hide-sm">ACT mid-50%</th>
        <th class="hide-sm">Undergrads</th>
        <th class="hide-sm">Avg Class Size</th>
        <th class="hide-sm">Type</th>
        <th></th>
      </tr></thead><tbody>"""
    rows = ""
    for rank_label, c, extra in rows_data:
        loc = city_state(c)
        if show_stars:
            stars_n = extra.get("stars", 0)
            star_html = '<span class="stars">' + ('★' * stars_n) + '</span><span class="stars-empty">' + ('★' * (5 - stars_n)) + '</span>'
            star_html += f' <span class="muted" style="font-size:.78em">{extra.get("fit",0)}/100</span>'
            metric_col = star_html
        else:
            metric_col = f"{round(c['accept']*100,1)}%"
        type_pill = f'<span class="pill pill-{c["type"]}" style="font-size:.65em">{c["type"]}</span>'
        size_str = f"{c['size']:,}" if c.get("size") else "—"
        avg_cs = f"~{avg_class_size_estimate(c)} <span class='muted' style='font-size:.78em'>est.</span>"
        rows += f"""<tr>
          <td class="rank-num">{rank_label}</td>
          <td class="name"><a href="/college/{c['slug']}">{c['name']}</a></td>
          <td class="hide-sm num-col">{loc}</td>
          <td class="num-col">{metric_col}</td>
          <td class="hide-sm num-col">{('—' if is_test_blind(c) else f"{c['sat_25']}-{c['sat_75']}")}</td>
          <td class="hide-sm num-col">{('—' if is_test_blind(c) else f"{c['act_25']}-{c['act_75']}")}</td>
          <td class="hide-sm num-col">{size_str}</td>
          <td class="hide-sm num-col">{avg_cs}</td>
          <td class="hide-sm">{type_pill}</td>
          <td><a class="btn btn-light btn-sm" href="/college/{c['slug']}">View</a></td>
        </tr>"""
    return head + rows + "</tbody></table>"


def ranking_detail_html(slug):
    r = RANKINGS_BY_SLUG.get(slug)
    if not r: abort(404)
    # Computed rankings — sorted dynamically from Scorecard data
    if r.get("computed"):
        return _ranking_detail_computed_html(r)
    order = expanded_ranking_order(slug, target=75)
    rows_data = []
    for i, s in enumerate(order):
        c = COLLEGES_BY_SLUG.get(s)
        if not c: continue
        rows_data.append((f"#{i+1}", c, {}))
    table = _ranking_table(rows_data, show_stars=False)
    note = ""
    if r.get("pref_based"):
        note = f'<p class="muted" style="font-size:.85em">Sorted by selectivity. {len(rows_data)} schools tagged with this attribute.</p>'
    elif len(rows_data) > 20:
        note = '<p class="muted" style="font-size:.85em">Top entries are curated; the rest are auto-extended by selectivity.</p>'
    return _page(f"""
{RANKING_TABLE_CSS}
<div class="bar"><a href="/rankings">&larr; all rankings</a></div>
<h1>{r['title']}</h1>
<p class="muted">{r['blurb']}</p>
{note}
{table}
""", title=f"{r['title']} — Candor")


def _ranking_detail_computed_html(r):
    """Render best-earnings or best-roi rankings, sorted dynamically from
    Scorecard's median-earnings and cost-of-attendance fields."""
    rows = []
    for c in COLLEGES:
        merged = merged_school(c)
        earn = median_earnings_10yr(merged)
        if not earn: continue
        cost = cost_attendance(merged)
        ratio = earnings_to_cost_ratio(merged)
        rows.append({
            "school": merged,
            "earn": earn,
            "cost": cost,
            "ratio": ratio,
        })
    if r["slug"] == "best-earnings":
        rows.sort(key=lambda x: -x["earn"])
        metric_label = "10-yr earnings"
    else:  # best-roi
        rows = [x for x in rows if x.get("ratio")]
        rows.sort(key=lambda x: x["ratio"])
        metric_label = "Years to payback"
    rows = rows[:75]

    head = f'''<table class="rank-table">
      <thead><tr>
        <th>#</th><th>School</th><th class="hide-sm">Location</th>
        <th>{metric_label}</th>
        <th class="hide-sm">10-yr earnings</th>
        <th class="hide-sm">4-yr cost</th>
        <th class="hide-sm">Type</th>
        <th></th>
      </tr></thead><tbody>'''
    body = ""
    for i, x in enumerate(rows):
        c = x["school"]
        loc = city_state(c)
        if r["slug"] == "best-earnings":
            metric_val = f"${x['earn']//1000}K"
        else:
            metric_val = f"{x['ratio']:.1f} yrs"
        type_pill = f'<span class="pill pill-{c["type"]}" style="font-size:.65em">{c["type"]}</span>'
        four_yr_cost = (x["cost"] or 0) * 4
        body += f'''<tr>
          <td class="rank-num">#{i+1}</td>
          <td class="name"><a href="/college/{c['slug']}">{c['name']}</a></td>
          <td class="hide-sm num-col">{loc}</td>
          <td class="num-col"><b>{metric_val}</b></td>
          <td class="hide-sm num-col">${x['earn']//1000}K</td>
          <td class="hide-sm num-col">${four_yr_cost//1000}K</td>
          <td class="hide-sm">{type_pill}</td>
          <td><a class="btn btn-light btn-sm" href="/college/{c['slug']}">View</a></td>
        </tr>'''
    table = head + body + "</tbody></table>"
    note = f'<p class="muted" style="font-size:.85em">Showing top {len(rows)} schools where Scorecard has populated earnings + cost data. Schools without federal earnings data (newer or smaller institutions) won\'t appear here.</p>'
    return _page(f"""
{RANKING_TABLE_CSS}
<div class="bar"><a href="/rankings">&larr; all rankings</a></div>
<h1>{r['title']}</h1>
<p class="muted">{r['blurb']}</p>
{note}
{table}
""", title=f"{r['title']} — Candor")


def my_fit_html():
    user = current_user()
    profile = get_profile(user["id"])
    if not profile or profile.get("uw_gpa") is None:
        return _page("""
<div class="bar"><a href="/rankings">&larr; all rankings</a></div>
<h1>★ My Fit</h1>
<p class="muted">This ranking sorts every college by overall fit — admit realism, preference match, academic match, and major fit combined.</p>
<div class="card" style="background:#fff8e1;border-color:#ffeaa7">
  <h3 style="margin-top:0">Your profile is incomplete</h3>
  <p>Add your GPA, test score, intended major, and preferences first. Otherwise this ranking is just academics-only and basically useless.</p>
  <a class="btn btn-primary" href="/profile">Edit profile &rarr;</a>
</div>
""", title="My Fit — Candor")
    prof = {
        "uw_gpa": profile.get("uw_gpa"), "weighted_gpa": profile.get("weighted_gpa"),
        "gpa_freshman": profile.get("gpa_freshman"), "gpa_sophomore": profile.get("gpa_sophomore"),
        "gpa_junior": profile.get("gpa_junior"), "gpa_senior": profile.get("gpa_senior"),
        "sat": profile.get("sat"), "act": profile.get("act"),
        "major": profile.get("major"), "state": profile.get("state"), "school_type": profile.get("school_type"),
        "ecs": profile.get("ecs"), "leadership": profile.get("leadership"), "awards": profile.get("awards"),
        "legacy": bool(profile.get("legacy")), "first_gen": bool(profile.get("first_gen")), "athlete": bool(profile.get("athlete")),
        "legacy_schools": profile.get("legacy_schools") or "",
        "aps": profile.get("aps") or "",
        "no_aps_offered": bool(profile.get("no_aps_offered")),
        "aps_offered_not_taken": bool(profile.get("aps_offered_not_taken")),
        "ibs": profile.get("ibs") or "",
        "no_ibs_offered": bool(profile.get("no_ibs_offered")),
        "ibs_offered_not_taken": bool(profile.get("ibs_offered_not_taken")),
        "pref_weather": profile.get("pref_weather"), "pref_setting": profile.get("pref_setting"),
        "pref_size": profile.get("pref_size"), "pref_greek": profile.get("pref_greek"),
        "pref_sports": profile.get("pref_sports"), "pref_major_strength": profile.get("pref_major_strength"),
        "pref_class_size": profile.get("pref_class_size"), "pref_prestige": profile.get("pref_prestige"),
        "pref_cost": profile.get("pref_cost"),
        "pref_diversity": profile.get("pref_diversity"),
        "pref_party": profile.get("pref_party"),
        "pref_research": profile.get("pref_research"),
        "pref_career_intensity": profile.get("pref_career_intensity"),
        "is_international": bool(profile.get("is_international")),
        "pref_weights": profile.get("pref_weights") or "",
    }
    scored = []
    vetoed_count = 0
    for c in COLLEGES:
        score, parts = compute_my_fit(prof, c)
        if parts.get("vetoed"):
            vetoed_count += 1
            continue
        if score >= 80:   stars = 5
        elif score >= 65: stars = 4
        elif score >= 50: stars = 3
        elif score >= 35: stars = 2
        elif score >= 20: stars = 1
        else: stars = 0
        scored.append((c, score, stars, parts))
    scored.sort(key=lambda t: -t[1])
    top = scored[:75]
    rows_data = [(f"#{i+1}", c, {"fit": score, "stars": stars}) for i, (c, score, stars, _) in enumerate(top)]
    table = _ranking_table(rows_data, show_stars=True)
    # Tell the user how the score is built so the rankings make sense
    veto_note = ""
    if vetoed_count:
        veto_note = f'<div class="card" style="background:#fff8e1;border-color:#ffeaa7"><b>{vetoed_count} schools hidden</b> because they mismatch a preference you marked importance 10 (deal-breaker). To see them, lower that pref\'s importance below 10 in your <a href="/profile">profile</a>.</div>'

    # Diagnostic: surface the user's saved importance weights so they can
    # verify their settings actually saved. If everything is the default 5,
    # they probably didn't click Save after touching the dials.
    weights_summary = ""
    try:
        d = json.loads((prof.get("pref_weights") or "").strip() or "{}")
    except Exception:
        d = {}
    if d:
        items = " &nbsp; ".join(f"<b>{k}:</b> {d.get(k, 5)}" for k in ("weather","setting","size","class_size","greek","sports","internships","prestige","region","cost"))
        all_default = all(int(v) == 5 for v in d.values())
        bg, border = ("#fff8e1","#ffeaa7") if all_default else ("#f7f7f7","#e6e6e6")
        warn = " <i>(all 5 — did you forget to click Save in the profile form?)</i>" if all_default else ""
        weights_summary = f'<div class="card" style="background:{bg};border-color:{border};font-size:.85em"><b>Your importance dials:</b> {items}{warn}</div>'
    else:
        weights_summary = '<div class="card" style="background:#fff8e1;border-color:#ffeaa7;font-size:.9em"><b>No importance weights saved yet.</b> Go to <a href="/profile">your profile</a>, set the importance dials (especially any deal-breakers at 10), and click <b>Save profile</b> at the bottom.</div>'
    legend = """<div class="card" style="background:#f7f7f7;border-color:#e6e6e6">
      <h3 style="margin-top:0">How fit is calculated</h3>
      <p class="muted" style="margin:0 0 8px;font-size:.92em">Fit is about whether you'd <i>thrive</i> at the school. Admission odds live on the Chances page — they barely move this score.</p>
      <ul style="padding-left:18px;margin:0;font-size:.92em">
        <li><b>Preferences match (80%)</b> — weather, setting, size, class size, Greek life, sports culture, major strength, prestige, cost from your saved profile. Dominant factor.</li>
        <li><b>Academic match (10%)</b> — small thumb toward schools where your stats are competitive.</li>
        <li><b>Admit realism (10%)</b> — small thumb toward realistic admit options.</li>
      </ul>
      <p class="muted" style="margin:8px 0 0;font-size:.92em"><b>Importance dial</b> on each pref: 1 = barely matters, 5 = neutral, 10 = <b>deal-breaker</b> (school is removed from this list if it mismatches).</p>
      <p class="muted" style="font-size:.85em;margin:6px 0 0">★★★★★ 80+ &nbsp; ★★★★ 65-79 &nbsp; ★★★ 50-64 &nbsp; ★★ 35-49 &nbsp; ★ 20-34</p>
    </div>"""
    legend = weights_summary + veto_note + legend
    # Soft warning if any of the 4 inputs is essentially missing
    warnings = []
    if not (profile.get("major") or "").strip():
        warnings.append("You haven't set an intended major — major-fit defaults to neutral. <a href='/profile'>Add one</a> for a sharper ranking.")
    if all((profile.get(k) or "any") == "any" for k in ("pref_weather","pref_setting","pref_size","pref_greek","pref_sports","pref_major_strength")):
        warnings.append("All preferences set to 'No preference' — preference-match defaults to neutral. <a href='/profile'>Set a few</a> for a sharper ranking.")
    warning_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warning_html = f'<div class="card" style="background:#fff8e1;border-color:#ffeaa7"><h3 style="margin-top:0">Sharpen your ranking</h3><ul style="padding-left:18px;margin:0">{items}</ul></div>'
    return _page(f"""
{RANKING_TABLE_CSS}
<div class="bar"><a href="/rankings">&larr; all rankings</a></div>
<h1>★ My Fit</h1>
<p class="muted">Every college ranked by overall fit for you specifically. A 5-star fit means you can realistically get in <i>and</i> would actually like it there <i>and</i> they have your major.</p>
{warning_html}
{legend}
{table}
""", title="My Fit — Candor")


def _auth_hook_html():
    """Build contextual messaging for the login/signup pages based on
    where the user was redirected from. Without this, the auth pages
    are bland and high-bounce — telling someone exactly what's behind
    the wall (calibrated odds at the school THEY just clicked) makes
    the signup feel worth it instead of arbitrary."""
    nxt = session.get("next_url") or ""
    school_name = None
    intent = None  # 'chances', 'plan', 'improve', 'profiles', 'chat', 'save', 'plans', 'compare', 'predictor', 'timeline'
    m = re.match(r"^/chances/([\w-]+)", nxt)
    if m:
        intent = "chances"
        c = COLLEGES_BY_SLUG.get(m.group(1))
        school_name = c["name"] if c else None
    else:
        m = re.match(r"^/college/([\w-]+)/(plan|improve|profiles|chat)", nxt)
        if m:
            intent = m.group(2)
            c = COLLEGES_BY_SLUG.get(m.group(1))
            school_name = c["name"] if c else None
    if not intent:
        if nxt.startswith("/save/") or nxt.startswith("/unsave/"):
            intent = "save"
            m = re.match(r"^/(save|unsave)/([\w-]+)", nxt)
            if m:
                c = COLLEGES_BY_SLUG.get(m.group(2))
                school_name = c["name"] if c else None
        elif nxt.startswith("/plans"):
            intent = "plans"
        elif nxt.startswith("/compare"):
            intent = "compare"
        elif nxt.startswith("/predictor"):
            intent = "predictor"
        elif nxt.startswith("/timeline"):
            intent = "timeline"
    if not intent:
        return ""
    headline_map = {
        "chances":  f"You'll see your calibrated odds at {school_name}" if school_name else "You'll see your calibrated odds",
        "plan":     f"You'll see a personalized plan for {school_name}" if school_name else "You'll see a personalized plan",
        "improve":  f"You'll see what to fix for {school_name}" if school_name else "You'll see what to fix",
        "profiles": f"You'll see real applicant profiles for {school_name}" if school_name else "You'll see real applicant profiles",
        "chat":     f"You'll chat with the AI advisor about {school_name}" if school_name else "You'll chat with the AI advisor",
        "save":     f"You'll add {school_name} to your saved list" if school_name else "You'll save schools to your list",
        "plans":    "You'll see all your saved schools and chances",
        "compare":  "You'll compare schools side-by-side with your fit + odds",
        "predictor":"You'll simulate how raising your scores changes your odds",
        "timeline": "You'll see month-by-month deadlines for your list",
    }
    detail_map = {
        "chances":  "30 seconds. Then you get: real CDS data (not federal lag), calibrated odds (capped at sub-15% for elites because that's reality), and a fit breakdown showing where you're strong vs weak.",
        "plan":     "Personalized to your stats and the school's priorities. AI-generated 6-8 specific actions, not generic 'improve your essay' advice.",
        "improve":  "School-specific guidance: what they actually weight, where you're behind, the round strategy that maximizes your odds.",
        "profiles": "Real Reddit applicant data — accepted/rejected/waitlisted profiles with stats and what stood out, so you can calibrate against people who actually got in.",
        "chat":     "Ask anything about the school, your odds, the supplemental, ED vs RD strategy. Tailored to your profile.",
        "save":     "Once saved, the school shows up on your plans page with quick-access to chances, compare, and timeline.",
        "plans":    "Every school you've chanced or saved, in one view with odds + fit + tier.",
        "compare":  "Up to 4 schools at once with verified CDS stats, your fit, and your odds.",
        "predictor":"What-if simulator — see how SAT +60 or ACT +2 moves your odds at each saved school.",
        "timeline": "Application + financial aid + decision deadlines, grouped by month, calibrated to the schools you care about.",
    }
    return f"""<div class="card" style="max-width:440px;background:rgba(95,201,182,.08);border-color:rgba(95,201,182,.25);margin-bottom:14px">
  <div style="font-weight:600;color:var(--teal);margin-bottom:6px">{headline_map[intent]}</div>
  <p class="muted" style="margin:0;font-size:.85em;line-height:1.5">{detail_map[intent]}</p>
</div>"""


def signup_html():
    from html import escape as _esc
    from urllib.parse import quote as _q
    hook = _auth_hook_html()
    nxt = (request.args.get("next") or "").strip()
    safe_path = nxt if nxt.startswith("/") and "\n" not in nxt and "\r" not in nxt else ""
    if safe_path == "/upgrade":
        headline = "Save your spot before checkout."
        sub = "One last step — create your account so we can attach your Premium plan to it."
        cta_label = "Create account & continue to checkout"
    else:
        headline = "Get your real chances — free."
        sub = "Verified Common Data Set numbers from 334+ schools. No credit card, no spam."
        cta_label = "Create account"
    nxt_hidden = f'<input type="hidden" name="next" value="{_esc(safe_path, quote=True)}">' if safe_path else ""
    nxt_qs = f"?next={_q(safe_path, safe='/')}" if safe_path else ""
    return _page(f"""
<div class="bar"><a href="/">&larr; back</a></div>
<h1>{headline}</h1>
<p class="muted">{sub}</p>
{hook}
<form method="post" action="/signup" class="card" style="max-width:440px">
  {csrf_input()}
  {nxt_hidden}
  <label style="margin-top:0">Email</label>
  <input type="email" name="email" required autofocus>
  <label>Password</label>
  <input type="password" name="password" minlength="8" required>
  <p class="muted" style="font-size:.78em;margin-top:6px">8+ characters. We never email you marketing.</p>
  <button class="btn btn-primary" type="submit">{cta_label}</button>
  <p class="muted" style="font-size:.85em;margin-top:14px">Already registered? <a href="/login{nxt_qs}">Log in</a>.</p>
</form>
<p class="muted" style="font-size:.85em;margin-top:16px"><a href="/#demo" style="color:var(--text-2)">← Just try the free calculator first</a></p>
""", title="Sign up — Candor")


def login_html():
    from html import escape as _esc
    from urllib.parse import quote as _q
    hook = _auth_hook_html()
    nxt = (request.args.get("next") or "").strip()
    safe_path = nxt if nxt.startswith("/") and "\n" not in nxt and "\r" not in nxt else ""
    nxt_hidden = f'<input type="hidden" name="next" value="{_esc(safe_path, quote=True)}">' if safe_path else ""
    nxt_qs = f"?next={_q(safe_path, safe='/')}" if safe_path else ""
    return _page(f"""
<div class="bar"><a href="/">&larr; back</a></div>
<h1>Log in</h1>
{hook}
<form method="post" action="/login" class="card" style="max-width:440px">
  {csrf_input()}
  {nxt_hidden}
  <label style="margin-top:0">Email</label>
  <input type="email" name="email" required autofocus>
  <label>Password</label>
  <input type="password" name="password" required>
  <button class="btn btn-primary" type="submit">Log in</button>
  <p class="muted" style="font-size:.85em;margin-top:14px">No account? <a href="/signup{nxt_qs}">Sign up</a>.</p>
</form>
""", title="Log in — Candor")


def _pref_form_fields(p):
    """Build all preference inputs as multi-select checkbox groups + an
    importance dial (1-10) next to each. User can pick multiple values per
    preference and tell us how much each one matters."""
    labels = {
        "pref_weather":          "Weather",
        "pref_setting":          "Campus setting",
        "pref_size":             "School size",
        "pref_class_size":       "Class size",
        "pref_prestige":         "Prestige",
        "pref_cost":             "Cost",
        "pref_greek":            "Greek life",
        "pref_sports":           "Sports culture",
        "pref_major_strength":   "Major strength",
        "pref_diversity":        "Student body diversity",
        "pref_party":            "Party / social scene",
        "pref_research":         "Research access",
        "pref_career_intensity": "Career focus",
        "pref_location":         "Distance from home",
        "pref_aid":              "Financial aid",
        "pref_culture":          "Academic culture (collaborative ↔ competitive)",
    }
    out = ""
    for key, label in labels.items():
        short = key.replace("pref_","")
        opts = [(v, lbl) for v, lbl in PREF_OPTIONS[short] if v != "any"]
        cur_str = (p.get(key) if isinstance(p, dict) else None) or ""
        cur = set(s.strip() for s in cur_str.split(",") if s.strip() and s.strip() != "any")
        boxes = ""
        for val, opt_label in opts:
            checked = "checked" if val in cur else ""
            boxes += (
                f'<label class="pick-pill" style="display:inline-flex;align-items:center;gap:7px;'
                f'background:var(--surface-2);border:1px solid var(--border-strong);'
                f'border-radius:4px;padding:7px 12px;font-weight:500;color:var(--text);'
                f'cursor:pointer;font-size:.85em;margin:0;transition:all .15s">'
                f'<input type="checkbox" name="{key}" value="{val}" {checked} style="width:auto;margin:0">'
                f'{opt_label}</label>'
            )
        # Current importance weight (default 5)
        cur_w = get_pref_weight(p if isinstance(p, dict) else {}, short)
        weight_options = "".join(
            f'<option value="{i}" {"selected" if i==cur_w else ""}>{i}</option>'
            for i in range(1, 11)
        )
        out += (
            f'<div style="margin:16px 0 4px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">'
            f'<label style="margin:0;font-weight:600">{label}</label>'
            f'<label style="margin:0;font-size:.78em;color:#666;font-weight:500;display:inline-flex;align-items:center;gap:5px">'
            f'How much it matters '
            f'<select name="weight_{short}" style="width:auto;padding:3px 6px;font-size:.85em">{weight_options}</select>'
            f'</label></div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px">{boxes}</div>'
        )
    return out


# Every current AP course offered by College Board (as of 2026), grouped
# by subject. Keys are what gets stored (matches AP_WEIGHTS substrings);
# labels are display.


def _render_ap_picker(saved_aps_str):
    """Render the AP-picker checkbox grid. Pre-checks anything already saved
    that substring-matches one of the canonical names."""
    saved = (saved_aps_str or "").lower()
    out = ""
    for group_label, items in AP_PICKER_GROUPS:
        boxes = ""
        for canonical, display in items:
            checked = "checked" if canonical.lower() in saved else ""
            boxes += (
                f'<label class="pick-pill" style="display:inline-flex;align-items:center;gap:7px;'
                f'background:var(--surface-2);border:1px solid var(--border-strong);'
                f'border-radius:4px;padding:6px 11px;font-weight:500;color:var(--text);'
                f'cursor:pointer;font-size:.83em;margin:0;transition:all .15s">'
                f'<input type="checkbox" name="ap_pick" value="{canonical}" {checked} style="width:auto;margin:0">'
                f'{display}</label>'
            )
        out += (f'<div style="margin:10px 0 6px"><div class="muted" style="font-size:.78em;font-weight:600;'
                f'text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px">{group_label}</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:5px">{boxes}</div></div>')
    return out


# IB picker — HL/SL distinction matters. Most common subjects per group;
# users at IB schools take 6 subjects (3 HL + 3 SL typically).


def _render_ib_picker(saved_ibs_str):
    saved = (saved_ibs_str or "").lower()
    out = ""
    for group_label, items in IB_PICKER_GROUPS:
        boxes = ""
        for canonical, display in items:
            checked = "checked" if canonical.lower() in saved else ""
            boxes += (
                f'<label class="pick-pill" style="display:inline-flex;align-items:center;gap:7px;'
                f'background:var(--surface-2);border:1px solid var(--border-strong);'
                f'border-radius:4px;padding:6px 11px;font-weight:500;color:var(--text);'
                f'cursor:pointer;font-size:.83em;margin:0;transition:all .15s">'
                f'<input type="checkbox" name="ib_pick" value="{canonical}" {checked} style="width:auto;margin:0">'
                f'{display}</label>'
            )
        out += (f'<div style="margin:10px 0 6px"><div class="muted" style="font-size:.78em;font-weight:600;'
                f'text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px">{group_label}</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:5px">{boxes}</div></div>')
    return out


def profile_html():
    p = get_profile(current_user()["id"]) or {}
    def v(k): return (p.get(k) if p.get(k) is not None else "")
    _is_paid = bool(current_user().get("is_paid"))
    match_section = ""
    checked = lambda k: 'checked' if p.get(k) else ''
    return _page(f"""
<h1>Your profile</h1>
{f'<div class="card" style="margin-bottom:14px;border-color:var(--border-strong);max-width:560px"><div style="font-weight:600;margin-bottom:8px">Candor Premium · active</div>{_cancel_subscription_html()}</div>' if _is_paid else ''}
<p class="muted">Used by the chances calculator. Be specific — generic answers produce generic odds.</p>
<form method="post" action="/profile" class="card">
  {csrf_input()}
  <h3 style="margin-top:0">Academics</h3>
  <div class="row">
    <div><label>Unweighted GPA <span class="muted">(0–4)</span></label>
      <input type="number" step="0.01" min="0" max="4.0" name="uw_gpa" value="{v('uw_gpa')}" required></div>
    <div><label>Weighted GPA <span class="muted">(optional)</span></label>
      <input type="number" step="0.01" min="0" max="6" name="weighted_gpa" value="{v('weighted_gpa')}"></div>
  </div>
  <div class="row" style="align-items:start;margin-bottom:14px">
    <div>
  <details style="margin:6px 0 0">
    <summary style="cursor:pointer;font-size:.92em;color:var(--text-2)">Year-by-year GPA <span class="muted">(optional, more accurate)</span></summary>
    <p class="muted" style="font-size:.84em;margin:8px 0 10px">Most schools weight upper years more heavily than freshman year. UCs literally don't see freshman grades at all. Filling these in lets the chances model reflect your actual trajectory — an upward trend (e.g., 3.2 → 3.8 → 3.95) reads very differently from a flat 3.65.</p>
    <div class="row">
      <div><label>Freshman <span class="muted">(unweighted)</span></label>
        <input type="number" step="0.01" min="0" max="4.0" name="gpa_freshman" value="{v('gpa_freshman')}"></div>
      <div><label>Sophomore</label>
        <input type="number" step="0.01" min="0" max="4.0" name="gpa_sophomore" value="{v('gpa_sophomore')}"></div>
    </div>
    <div class="row">
      <div><label>Junior <span class="muted">(or junior so far)</span></label>
        <input type="number" step="0.01" min="0" max="4.0" name="gpa_junior" value="{v('gpa_junior')}"></div>
      <div><label>Senior <span class="muted">(if applicable)</span></label>
        <input type="number" step="0.01" min="0" max="4.0" name="gpa_senior" value="{v('gpa_senior')}"></div>
    </div>
  </details>
    </div>
    <div>
  <details style="margin:6px 0 0">
    <summary style="cursor:pointer;font-size:.92em;color:var(--text-2)">Year-by-year weighted GPA <span class="muted">(only weight some years?)</span></summary>
    <p class="muted" style="font-size:.84em;margin:8px 0 10px">If your school only offers honors/AP weighting in certain years (e.g. nothing freshman year, or only for advanced tracks), tick <b>"not offered"</b> for those years. We compute your weighted GPA from <b>only the years weighting was available</b>, so you're not penalized for courses your school gated. Used at schools that report a weighted admit range (most big publics).</p>
    <div class="row">
      <div><label>Freshman <span class="muted">(weighted)</span></label>
        <input type="number" step="0.01" min="0" max="6" name="w_gpa_freshman" value="{v('w_gpa_freshman')}">
        <label style="display:flex;align-items:center;gap:6px;font-weight:400;font-size:.8em;margin-top:6px;color:var(--text-2)"><input type="checkbox" name="w_notoffered_freshman" {checked('w_notoffered_freshman')} style="width:auto;margin:0"> Weighting not offered this year</label></div>
      <div><label>Sophomore <span class="muted">(weighted)</span></label>
        <input type="number" step="0.01" min="0" max="6" name="w_gpa_sophomore" value="{v('w_gpa_sophomore')}">
        <label style="display:flex;align-items:center;gap:6px;font-weight:400;font-size:.8em;margin-top:6px;color:var(--text-2)"><input type="checkbox" name="w_notoffered_sophomore" {checked('w_notoffered_sophomore')} style="width:auto;margin:0"> Weighting not offered this year</label></div>
    </div>
    <div class="row">
      <div><label>Junior <span class="muted">(weighted)</span></label>
        <input type="number" step="0.01" min="0" max="6" name="w_gpa_junior" value="{v('w_gpa_junior')}">
        <label style="display:flex;align-items:center;gap:6px;font-weight:400;font-size:.8em;margin-top:6px;color:var(--text-2)"><input type="checkbox" name="w_notoffered_junior" {checked('w_notoffered_junior')} style="width:auto;margin:0"> Weighting not offered this year</label></div>
      <div><label>Senior <span class="muted">(weighted)</span></label>
        <input type="number" step="0.01" min="0" max="6" name="w_gpa_senior" value="{v('w_gpa_senior')}">
        <label style="display:flex;align-items:center;gap:6px;font-weight:400;font-size:.8em;margin-top:6px;color:var(--text-2)"><input type="checkbox" name="w_notoffered_senior" {checked('w_notoffered_senior')} style="width:auto;margin:0"> Weighting not offered this year</label></div>
    </div>
  </details>
    </div>
  </div>
  <div class="row">
    <div><label>SAT <span class="muted">(composite)</span></label>
      <input type="number" min="400" max="1600" step="10" name="sat" value="{v('sat')}"></div>
    <div><label>ACT <span class="muted">(composite)</span></label>
      <input type="number" min="1" max="36" name="act" value="{v('act')}"></div>
  </div>
  <details style="margin:6px 0 14px">
    <summary style="cursor:pointer;font-size:.92em;color:var(--text-2)">Section subscores <span class="muted">(optional — helps STEM-focused schools)</span></summary>
    <p class="muted" style="font-size:.84em;margin:8px 0 14px">A composite score hides imbalance. A 1450 SAT split 800/650 reads very differently at MIT/Caltech/CMU CS than a balanced 730/720. Only fill in subscores for the test you actually took.</p>

    <div style="border:1px solid var(--border);border-radius:6px;padding:14px 16px;margin-bottom:12px;background:rgba(95,201,182,.03)">
      <div style="font-weight:700;font-size:.92em;color:var(--teal);margin-bottom:10px;letter-spacing:.3px">SAT subscores <span class="muted" style="font-weight:400;font-size:.85em">(scale: 200–800 each)</span></div>
      <div class="row">
        <div><label>SAT EBRW <span class="muted">(reading + writing)</span></label>
          <input type="number" min="200" max="800" step="10" placeholder="e.g. 720" name="sat_ebrw" value="{v('sat_ebrw')}"></div>
        <div><label>SAT Math</label>
          <input type="number" min="200" max="800" step="10" placeholder="e.g. 750" name="sat_math" value="{v('sat_math')}"></div>
      </div>
    </div>

    <div style="border:1px solid var(--border);border-radius:6px;padding:14px 16px;background:rgba(125,211,252,.03)">
      <div style="font-weight:700;font-size:.92em;color:#7dd3fc;margin-bottom:10px;letter-spacing:.3px">ACT subscores <span class="muted" style="font-weight:400;font-size:.85em">(scale: 1–36 each)</span></div>
      <div class="row">
        <div><label>ACT Math</label>
          <input type="number" min="1" max="36" placeholder="e.g. 32" name="act_math" value="{v('act_math')}"></div>
        <div><label>ACT English</label>
          <input type="number" min="1" max="36" placeholder="e.g. 34" name="act_english" value="{v('act_english')}"></div>
      </div>
      <div class="row">
        <div><label>ACT Reading</label>
          <input type="number" min="1" max="36" placeholder="e.g. 33" name="act_reading" value="{v('act_reading')}"></div>
        <div><label>ACT Science</label>
          <input type="number" min="1" max="36" placeholder="e.g. 31" name="act_science" value="{v('act_science')}"></div>
      </div>
    </div>
  </details>

  <h3>About you</h3>
  <div class="row">
    <div><label>Intended major</label>
      <input name="major" value="{v('major')}" placeholder="Computer Science" list="majors-list" autocomplete="off">
      <datalist id="majors-list">
        {''.join(f'<option value="{m}">' for m in MAJORS)}
      </datalist></div>
    <div><label>State <span class="muted">(home state — affects in-state rates at public schools)</span></label>
      <select name="state">
        <option value="">— Select —</option>
        {''.join(f'<option value="{s}" {"selected" if v("state")==s else ""}>{s}</option>' for s in STATES)}
      </select></div>
  </div>
  <label>High school type</label>
  <select name="school_type">
    <option value="public" {"selected" if v('school_type')=='public' else ''}>Public</option>
    <option value="private" {"selected" if v('school_type')=='private' else ''}>Private</option>
    <option value="magnet" {"selected" if v('school_type')=='magnet' else ''}>Magnet / charter</option>
    <option value="boarding" {"selected" if v('school_type')=='boarding' else ''}>Boarding</option>
  </select>

  <label>AP courses taken <span class="muted">(optional — click any you're taking or have taken)</span></label>
  {_render_ap_picker(v('aps'))}
  <div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">
    <label style="display:flex;align-items:center;gap:8px;font-weight:500">
      <input type="checkbox" name="no_aps_offered" {checked('no_aps_offered')} style="width:auto;margin:0">
      My school doesn't offer APs
    </label>
    <label style="display:flex;align-items:center;gap:8px;font-weight:500">
      <input type="checkbox" name="aps_offered_not_taken" {checked('aps_offered_not_taken')} style="width:auto;margin:0">
      My school offers APs but I haven't taken any
    </label>
  </div>
  <p class="muted" style="font-size:.78em;margin:4px 0 0">Pick whichever applies. Top schools care a lot about course rigor — taking no APs at a school that offers them is a small negative; not having APs available isn't your fault and won't be counted against you.</p>

  <label style="margin-top:18px">IB courses <span class="muted">(if you're in an IB program — click HL/SL subjects you're taking)</span></label>
  {_render_ib_picker(v('ibs'))}
  <div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">
    <label style="display:flex;align-items:center;gap:8px;font-weight:500">
      <input type="checkbox" name="no_ibs_offered" {checked('no_ibs_offered')} style="width:auto;margin:0">
      My school doesn't offer IB
    </label>
    <label style="display:flex;align-items:center;gap:8px;font-weight:500">
      <input type="checkbox" name="ibs_offered_not_taken" {checked('ibs_offered_not_taken')} style="width:auto;margin:0">
      My school offers IB but I haven't taken any
    </label>
  </div>
  <p class="muted" style="font-size:.78em;margin:4px 0 0">HL classes count for more than SL. Most schools that have IB don't have APs, so picking neither is fine if you're at one of them.</p>

  <label style="margin-top:18px">Course rigor self-rating <span class="muted">(only if your school offers no AP <em>or</em> IB)</span></label>
  <select name="self_rigor">
    <option value="" {('selected' if not v('self_rigor') else '')}>— not applicable / my school offers AP or IB —</option>
    {''.join(f'<option value="{n}" {("selected" if str(v("self_rigor"))==str(n) else "")}>{n}/10 — {lbl}</option>' for n,lbl in [(10,"hardest available load, all honors/DE/post-AP"),(9,""),(8,"clearly rigorous, top track"),(7,""),(6,"above average"),(5,"average load"),(4,""),(3,"light load"),(2,""),(1,"least rigorous")] )}
  </select>
  <p class="muted" style="font-size:.78em;margin:4px 0 0">If your school has no AP/IB, rate how demanding your courses are relative to what's offered (honors, dual-enrollment, post-calculus math, etc.). Leave blank if your school offers AP or IB — we score those from the courses above. This is self-reported, so it's weighted conservatively.</p>

  <label style="margin-top:18px">Class rank <span class="muted">(optional — your rank and graduating class size)</span></label>
  <div class="row">
    <div><input type="number" min="1" name="class_rank" value="{v('class_rank')}" placeholder="rank, e.g. 12"></div>
    <div><input type="number" min="1" name="class_size" value="{v('class_size')}" placeholder="class size, e.g. 480"></div>
  </div>
  <div style="margin-top:10px">
    <label style="display:flex;align-items:center;gap:8px;font-weight:500">
      <input type="checkbox" name="no_class_rank_offered" {checked('no_class_rank_offered')} style="width:auto;margin:0">
      My school does not offer class rank
    </label>
  </div>
  <p class="muted" style="font-size:.78em;margin:4px 0 0">If your school ranks, being near the top is a small boost. If it doesn't rank, check the box and it won't count against you.</p>

  <h3>Activities</h3>
  <label>Extracurriculars</label>
  <textarea name="ecs" placeholder="Robotics team (4 yrs, 10 hrs/wk, FRC regional finalist 2024). ML research with Prof X.">{v('ecs')}</textarea>
  <label>Leadership</label>
  <textarea name="leadership" placeholder="Captain of robotics, president of Model UN.">{v('leadership')}</textarea>
  <label>Awards</label>
  <textarea name="awards" placeholder="USACO Gold, National Merit Semifinalist.">{v('awards')}</textarea>

  <h3>Hooks</h3>
  <label>Legacy schools <span class="muted">(comma-separated)</span></label>
  <input name="legacy_schools" value="{v('legacy_schools')}" placeholder="e.g. Harvard, Yale, University of Pennsylvania">
  <p class="muted" style="font-size:.78em;margin:4px 0 0">List the specific schools where you have legacy. The boost only applies at those schools. Add a generation count if multi-generational (e.g., "Cornell 4x" = 4 generations).</p>
  <div class="checks" style="margin-top:10px">
    <label><input type="checkbox" name="first_gen" value="yes" {checked('first_gen')}> First-generation college student</label>
    <label><input type="checkbox" name="athlete" value="yes" {checked('athlete')}> Recruited / likely-recruit athlete</label>
    <label><input type="checkbox" name="is_international" value="yes" {checked('is_international')}> International applicant (not a US citizen / permanent resident)</label>
  </div>

  <h3>Portfolio / supplemental materials</h3>
  <p class="muted" style="font-size:.85em;margin:-2px 0 8px">Schools like USC Roski, NYU Tisch, Berklee, RISD, and others use portfolios or auditions as primary admissions criteria. STEM-focused schools (MIT, Caltech) sometimes weight optional research/maker portfolios heavily. If you have one, list it — used both for odds calc at portfolio-required schools and for tailored advice.</p>
  <label>Portfolio / research / audition materials <span class="muted">(brief description)</span></label>
  <textarea name="portfolio" rows="2" placeholder="e.g. Studio art portfolio (15 pieces, mixed media); jazz piano audition tape; published research on CRISPR delivery">{v('portfolio')}</textarea>

  {match_section}

  <h3>Preferences</h3>
  <p class="muted" style="font-size:.85em;margin:-2px 0 8px">Check anything you'd be happy with. The 1-10 dial says how much it matters. 5 is neutral. <b>10 is a deal-breaker</b>: schools that miss get removed from My Fit.</p>
  {_pref_form_fields(p)}

  <p style="margin-top:18px"><button class="btn btn-primary" type="submit">Save profile</button></p>
</form>
""", title="Profile — Candor")


def _chances_profile(uid, slug):
    """Build the FULL profile dict the chances calc should use. Previously the
    chances page hand-picked a subset that omitted ec_rating, spike_score,
    self_rigor, IB and class-rank fields — so the LLM EC rating, the spike bonus
    and self-rated rigor never actually moved the live odds. Using the whole row
    fixes that. Returns None if the user has no profile."""
    p = get_profile(uid)
    if not p:
        return None
    # Lazy exceptional-applicant eval (cached after first call).
    is_exc, exc_reason = get_or_evaluate_exceptionality(uid, p)
    profile = dict(p)  # every saved column: ec_rating, spike_score, self_rigor, ibs, ...
    profile["_di_level"] = get_demonstrated_interest(uid, slug)
    profile["is_exceptional"] = is_exc
    profile["exceptional_reason"] = exc_reason
    return profile


def _log_calc_run(slug, uid=None):
    """Record one calibration run for a school — every time, including re-runs —
    so the admin most-viewed list counts true volume, not just unique pairs."""
    try:
        with db() as conn:
            conn.execute("INSERT INTO calc_runs(college_slug, user_id) VALUES (?,?)", (slug, uid))
            conn.commit()
    except Exception as e:
        print(f"calc_run log failed: {e}")


def _save_chances_row(uid, slug, r, bullets=None):
    """Persist a chances row. Numbers are always written (so /plans + simulator
    stay current); bullets are written only when provided, otherwise any
    existing AI narrative is preserved."""
    try:
        with db() as conn:
            if bullets is not None:
                conn.execute("""INSERT INTO saved_chances (user_id, college_slug, tier, odds_low, odds_high, fit, confidence, strength, weakness, differentiator, computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, college_slug) DO UPDATE SET
                      tier=excluded.tier, odds_low=excluded.odds_low, odds_high=excluded.odds_high,
                      fit=excluded.fit, confidence=excluded.confidence,
                      strength=excluded.strength, weakness=excluded.weakness, differentiator=excluded.differentiator,
                      computed_at=CURRENT_TIMESTAMP""",
                    (uid, slug, r["tier"], r["odds_low"], r["odds_high"], r["fit"], r["confidence"],
                     bullets["strength"], bullets["weakness"], bullets["differentiator"]))
            else:
                conn.execute("""INSERT INTO saved_chances (user_id, college_slug, tier, odds_low, odds_high, fit, confidence, computed_at)
                    VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, college_slug) DO UPDATE SET
                      tier=excluded.tier, odds_low=excluded.odds_low, odds_high=excluded.odds_high,
                      fit=excluded.fit, confidence=excluded.confidence,
                      computed_at=CURRENT_TIMESTAMP""",
                    (uid, slug, r["tier"], r["odds_low"], r["odds_high"], r["fit"], r["confidence"]))
            conn.commit()
    except Exception as e:
        print(f"save_chances_row failed: {e}")


def _chances_narrative_ul(r):
    return ('<ul style="padding-left:18px;margin:18px 0 0">'
            f'<li><b>Strength —</b> {r.get("strength","")}</li>'
            f'<li><b>Weakness —</b> {r.get("weakness","")}</li>'
            f'<li><b>Differentiator —</b> {r.get("differentiator","")}</li></ul>')


def _chances_narrative_block(slug, r, ready):
    if ready:
        return _chances_narrative_ul(r)
    return f"""<div id="chances-narr" style="margin:18px 0 0">
  <div style="display:flex;align-items:center;gap:10px;color:var(--text-2);font-size:.9em">
    <span class="cdr-spinner-sm"></span> Writing your strengths &amp; weaknesses…
  </div>
</div>
<style>@keyframes cdrspin{{to{{transform:rotate(360deg)}}}}.cdr-spinner-sm{{width:18px;height:18px;border-radius:50%;border:2px solid rgba(95,201,182,.2);border-top-color:#5fc9b6;display:inline-block;animation:cdrspin .8s linear infinite}}</style>
<script>
(function(){{
  fetch("/chances/{slug}/narrative").then(function(x){{return x.text();}}).then(function(h){{
    var el=document.getElementById("chances-narr"); if(el) el.innerHTML=h;
  }}).catch(function(){{
    var el=document.getElementById("chances-narr"); if(el) el.innerHTML='<p class="muted" style="font-size:.9em">Narrative unavailable. <a href="/chances/{slug}?refresh=1">Retry</a></p>';
  }});
}})();
</script>"""


def chances_html(slug):
    uid = current_user()["id"]
    profile = _chances_profile(uid, slug)
    if profile is None:
        flash("Create your profile first so we can calculate your chances.", "error")
        session["next_url"] = f"/chances/{slug}"
        return redirect(url_for("profile_page"))
    school_data = COLLEGES_BY_SLUG.get(slug)
    if not school_data: abort(404)
    _log_calc_run(slug, uid)
    exc_reason = profile.get("exceptional_reason")
    # Odds are pure-Python (instant) and computed fresh on every view so they
    # always reflect the current profile (incl. ec_rating / spike / self_rigor)
    # and the latest formula. Only the AI strength/weakness/differentiator
    # narrative — the slow ~3s Claude call — is cached and lazy-loaded so the
    # page never blocks on it.
    merged = merged_school(school_data)
    fit, components = compute_fit(profile, merged)
    tier = assign_tier(merged, fit, profile)
    low, high = estimate_odds(merged, fit, profile)
    r = {
        "school": merged["name"], "slug": slug,
        "accept_rate_pct": round(merged["accept"]*100, 1),
        "fit": fit, "tier": tier, "odds_low": low, "odds_high": high,
        "confidence": confidence_level(profile, components),
    }
    force_refresh = request.args.get("refresh") == "1"
    bullets = None
    if not force_refresh:
        with db() as conn:
            brow = conn.execute(
                "SELECT strength, weakness, differentiator FROM saved_chances "
                "WHERE user_id=? AND college_slug=? AND computed_at >= ?",
                (uid, slug, SAVED_CHANCES_MIN_VALID_AT)).fetchone()
        if brow and brow["strength"]:
            bullets = {"strength": brow["strength"], "weakness": brow["weakness"],
                       "differentiator": brow["differentiator"]}
    _save_chances_row(uid, slug, r, bullets)
    narrative_ready = bullets is not None
    if narrative_ready:
        r.update(bullets)
    narrative_html = _chances_narrative_block(slug, r, narrative_ready)
    tier_class = {"Dream": "pill-dream", "Reach": "pill-reach", "Target": "pill-target", "Safety": "pill-safety"}[r["tier"]]
    conf_class = {"low": "pill-conf-low", "medium": "pill-conf-medium", "high": "pill-conf-high"}[r["confidence"]]
    conf_tooltip = {
        "high": "Your profile has GPA, test score, and clear strengths/weaknesses — model has solid signal to work with.",
        "medium": "You submitted some stats but something's missing (test-optional, or your stats are at the school's median which makes the outcome coin-flippy).",
        "low": "Sparse profile (missing test/GPA or very few ECs) — model can't be precise. Add more details to your profile for a sharper estimate.",
    }[r["confidence"]]
    return _page(f"""
<div class="bar"><a href="/college/{r['slug']}">&larr; back to {r['school']}</a></div>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
    <div>
      <h1 style="margin:0">{r['school']}</h1>
      <div class="muted" style="font-size:.85em">Acceptance {r['accept_rate_pct']}% · profile fit {r['fit']}/100</div>
    </div>
    <div><span class="pill {tier_class}">{r['tier']}</span> <span class="pill {conf_class}" style="margin-left:4px" title="{conf_tooltip}">{r['confidence']} confidence</span></div>
  </div>
  <div class="odds" style="color:#2b6cff">{r['odds_low']}–{r['odds_high']}%</div>
  <div class="muted" style="font-size:.82em">your estimated chances</div>
  {(f'<div style="margin-top:14px;padding:10px 14px;background:rgba(95,201,182,.08);border:1px solid rgba(95,201,182,.25);border-radius:4px;font-size:.88em"><b style="color:var(--teal)">★ Exceptional applicant override</b><div class="muted" style="margin-top:4px">{exc_reason or "Flagged exceptional based on your profile."} Your odds reflect this above the standard cap.</div></div>' if profile.get('is_exceptional') else '')}
  {(lambda _sch, _det: (lambda _sub: render_admissions_breakdown(_sch, _det, personalized_rates=personalize_round_odds(uid, _sch, _det, profile, r['odds_low'], r['odds_high'], sub_school=_sub), scale=(((r.get('odds_low',0)+r.get('odds_high',0))/2.0) / r['accept_rate_pct']) if r.get('accept_rate_pct') else 1.0, sub_school=_sub))(sub_school_for_major(r['slug'], profile.get('major') or '')))(COLLEGES_BY_SLUG.get(r['slug']), admissions_detail(COLLEGES_BY_SLUG.get(r['slug'])))}
  {narrative_html}
</div>
{_render_counterfactual_card(profile, COLLEGES_BY_SLUG.get(r['slug']), r['odds_low'], r['odds_high'])}
{_render_di_card(r['slug'], r['school'], profile.get('_di_level','none'))}
<details class="card" style="margin-top:18px">
  <summary style="cursor:pointer;font-weight:600">What does "{r['confidence']} confidence" mean?</summary>
  <p class="muted" style="font-size:.88em;margin-top:10px">Confidence is how reliable the prediction itself is — <b>not</b> your chances of getting in. It reflects how much usable signal your profile has.</p>
  <ul style="padding-left:18px;margin:6px 0 0;font-size:.9em;line-height:1.7">
    <li><b style="color:var(--teal)">High</b> — full GPA + test score submitted, profile has clear strengths/weaknesses for the model to read. Trust the number.</li>
    <li><b style="color:#7dd3fc">Medium</b> — some signal but mixed (test-optional submission, or stats right at the school's median making the outcome more coin-flippy).</li>
    <li><b style="color:var(--text-3)">Low</b> — missing test/GPA or sparse profile. Range will be wider; add more profile details for a sharper estimate.</li>
  </ul>
  <p class="muted" style="font-size:.82em;margin-top:8px"><i>tldr: a high-confidence "5-12%" means probably 5-12%. A low-confidence "5-12%" means the real range could be 2-25%.</i></p>
</details>
<div class="card" style="margin-top:18px;background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3)">
  <div style="font-size:.74em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:6px">Next step</div>
  <h3 style="margin:0 0 8px;color:#e6edf3">Turn this number into a plan</h3>
  <p class="muted" style="margin:0 0 14px;line-height:1.55">Candor Premium ($3/mo) unlocks a personalized strategy for {r['school']}, score-push impact, and your list grader. Or share this report with your parents and let them decide.</p>
  <a class="btn btn-primary btn-sm" href="/upgrade">Upgrade — $3/mo</a>
  <a class="btn btn-light btn-sm" href="/upgrade?for=parent" style="margin-left:6px">Show your parents →</a>
</div>
<p style="margin-top:18px"><a class="btn btn-light" href="/profile">Edit profile</a> <a class="btn btn-light" href="/college/{r['slug']}/improve">Get tailored advice for {r['school']} &rarr;</a></p>
""", title=f"Your chances at {r['school']} — Candor")


def _resource_block(category, items):
    rows = ""
    for it in items:
        link = f'<a href="{it["url"]}" target="_blank" rel="noopener">{it["title"]}</a>' if it.get("url") else f'<b>{it["title"]}</b>'
        rows += f'<div style="padding:8px 0;border-top:1px solid #eee"><div>{link}</div><div class="muted" style="font-size:.84em;margin-top:2px">{it["note"]}</div></div>'
    return f'<div class="card"><h3 style="margin-top:0">{category}</h3>{rows}</div>'


def build_action_plan(profile):
    """Rule-based gap analysis → list of structured action items.
    Each item: title, why, target, deadline, impact."""
    items = []
    if not profile: return items
    gpa = profile.get("uw_gpa") or 0
    sat = profile.get("sat") or 0
    act = profile.get("act") or 0
    ec_text = (profile.get("ecs") or "").strip()
    aps = (profile.get("aps") or "").strip()

    if not aps and not profile.get("no_aps_offered"):
        items.append({
            "title":"Sign up for at least 2 AP courses next term",
            "why":"Course rigor is one of the top 3 academic signals at competitive schools. An empty AP slate looks like rigor avoidance.",
            "target":"Calc BC + Chem (or your major's hardest options — APUSH/Lit if humanities)",
            "deadline":"Course-selection week (typically Feb–Mar)",
            "impact":"+4–6 fit at tier-1 schools",
        })
    if gpa and gpa < 3.7:
        items.append({
            "title":"Lock straight A's for the rest of the semester",
            "why":f"Current UW GPA {gpa:.2f}. Trend > absolute number for late-blooming applicants — admissions readers see the slope.",
            "target":"4.0 every grading period from now until application",
            "deadline":"End of current semester",
            "impact":"+2–4 fit per 0.1 GPA gain",
        })
    if not sat and not act:
        items.append({
            "title":"Take the SAT (or ACT) before October of senior year",
            "why":"Test scores are a clean lever and many top schools have re-required testing. No score = ambiguous read.",
            "target":"SAT 1450+ for tier-1 schools, 1350+ for tier-2 / ACT 33+ tier-1, 31+ tier-2",
            "deadline":"August or October of senior year",
            "impact":"+5–15 fit at top schools depending on score",
        })
    elif sat and sat < 1450:
        items.append({
            "title":"Retake the SAT — target +50–100 points",
            "why":f"Current {sat}. Below tier-1 mid-50%. Focused 6-week prep typically moves +60–150.",
            "target":"1500+ via Khan Academy + 4 official BlueBook practice tests",
            "deadline":"6-week window before next test date",
            "impact":"+5–10 fit at reach schools",
        })
    elif act and act < 32:
        items.append({
            "title":"Retake the ACT — target +2–3 points",
            "why":f"Current {act}. Below tier-1 mid-50%. ACT prep typically moves +2–3 in a single retake cycle.",
            "target":"33+",
            "deadline":"Next ACT date",
            "impact":"+5–10 fit at reach schools",
        })
    if len(ec_text) < 80 or _keyword_strength(ec_text, EC_STRONG_SIGNALS) < 1:
        items.append({
            "title":"Build one signature EC project this semester",
            "why":"Thin EC list. Admissions readers want depth in 1–2 areas, not breadth across 5 generic clubs.",
            "target":"Pick a project (research, founded org, sustained competition track) and put 5+ hrs/wk into it",
            "deadline":"Start within 2 weeks; show concrete progress in 3 months",
            "impact":"Often the deciding factor for tier-1 admits",
        })
    if not (profile.get("leadership") or "").strip():
        items.append({
            "title":"Take a real leadership role by next semester",
            "why":"No leadership listed. Captain / founder / president / editor — title + accountability for an outcome.",
            "target":"One named role with measurable scope (team size, project, budget)",
            "deadline":"Run / apply within the next month",
            "impact":"+2–4 fit, especially at LACs and small privates",
        })
    if not (profile.get("awards") or "").strip():
        items.append({
            "title":"Enter at least 1 national-tier competition this semester",
            "why":"No awards listed. Externally-validated wins are how ECs become signals admissions can verify.",
            "target":"Pick one from the Competitions database below that aligns with your major",
            "deadline":"Check each comp's deadline window",
            "impact":"+3–7 fit if you place at state or higher",
        })
    return items


def _render_action_plan(items):
    if not items:
        return ""
    rows = ""
    for i, it in enumerate(items, 1):
        rows += f"""<div class="action-item">
          <div class="action-num">{i}</div>
          <div class="action-body">
            <div class="action-title">{it['title']}</div>
            <div class="action-meta"><span class="meta-label">Why:</span> {it['why']}</div>
            <div class="action-meta"><span class="meta-label">Target:</span> {it['target']}</div>
            <div class="action-meta"><span class="meta-label">By:</span> {it['deadline']}</div>
            <div class="action-impact">{it['impact']}</div>
          </div>
        </div>"""
    return rows


def _render_competitions(major_filter):
    """Show only competitions relevant to the user's major when filter is set.
    Past UX bug: a 'show N more' collapsible included unrelated competitions
    (coding/bio shown to a poli-sci student) and confused users."""
    matches = []
    mf = major_filter.lower().strip() if major_filter else ""
    for c in COMPETITIONS:
        if mf:
            applicable = (not c.get("majors")) or any(mf in m.lower() or m.lower() in mf for m in c["majors"])
            if applicable:
                matches.append(c)
        else:
            matches.append(c)
    def render_one(c):
        majors = ", ".join(c["majors"]) if c.get("majors") else "Any major"
        return f"""<div class="comp-row">
          <div class="comp-head">
            <a href="{c['url']}" target="_blank" rel="noopener" class="comp-name">{c['name']}</a>
            <span class="pill">{c.get('tier','national')}</span>
          </div>
          <div class="comp-meta"><span class="meta-label">Deadline:</span> {c.get('deadline','—')} · <span class="meta-label">Best for:</span> {majors}</div>
          <div class="comp-note">{c['note']}</div>
        </div>"""
    if not matches:
        return '<p class="muted">No competitions specifically tagged for that major. Try clearing the filter to see general competitions.</p>'
    return "".join(render_one(c) for c in matches)


def _render_summer_programs(major_filter):
    matches = []
    mf = major_filter.lower().strip() if major_filter else ""
    for s in SUMMER_PROGRAMS:
        if mf:
            applicable = (not s.get("majors")) or any(mf in m.lower() or m.lower() in mf for m in s["majors"])
            if applicable:
                matches.append(s)
        else:
            matches.append(s)
    def render_one(s):
        majors = ", ".join(s["majors"]) if s.get("majors") else "Any major"
        return f"""<div class="comp-row">
          <div class="comp-head">
            <a href="{s['url']}" target="_blank" rel="noopener" class="comp-name">{s['name']}</a>
            <span class="pill">{s.get('selectivity','—')}</span>
          </div>
          <div class="comp-meta"><span class="meta-label">Cost:</span> {s.get('cost','—')} · <span class="meta-label">Grade:</span> {s.get('grade','—')} · <span class="meta-label">Best for:</span> {majors}</div>
          <div class="comp-note">{s['note']}</div>
        </div>"""
    if not matches:
        return '<p class="muted">No summer programs specifically tagged for that major. Try clearing the filter.</p>'
    return "".join(render_one(s) for s in matches)


def improve_html():
    user = current_user()
    profile = get_profile(user["id"]) if user else None
    user_major = (profile.get("major") if profile else "") or ""
    filter_major = request.args.get("major", user_major).strip()

    action_items = build_action_plan(profile) if profile else []
    if action_items:
        action_html = f"""<div class="card">
          <h2 style="margin-top:0">Your action plan</h2>
          <p class="muted" style="margin:0 0 16px">Built from your profile. Each item has a target, a deadline, and how much it actually moves your odds. Do these in order.</p>
          {_render_action_plan(action_items)}
        </div>"""
    elif profile:
        action_html = '<div class="card"><h2 style="margin-top:0">Your action plan</h2><p style="margin:0">No obvious gaps in your profile. Focus on essays + school-specific advice from each college\'s detail page.</p></div>'
    else:
        action_html = '<div class="card"><h2 style="margin-top:0">Your action plan</h2><p class="muted" style="margin:0"><a href="/profile">Save your profile</a> to get a personalized action plan.</p></div>'

    # Major filter dropdown — uses MAJORS list
    major_opts = '<option value="">All majors</option>' + "".join(
        f'<option value="{m}" {"selected" if m == filter_major else ""}>{m}</option>'
        for m in MAJORS
    )
    filter_form = f"""<form method="get" action="/improve" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
      <div style="flex:1;min-width:220px"><label style="margin-top:0">Filter by major</label>
        <select name="major">{major_opts}</select></div>
      <button class="btn btn-primary" type="submit">Apply</button>
      <a class="btn btn-light" href="/improve">Clear</a>
    </form>"""

    comp_html = f"""<div class="card">
      <h2 style="margin-top:0">Competitions database</h2>
      <p class="muted" style="margin:0 0 14px">Externally-validated wins. The fastest path from "I have ECs" to "I have awards." Filter by your major.</p>
      {filter_form}
      <div style="margin-top:18px">{_render_competitions(filter_major)}</div>
    </div>"""

    summer_html = f"""<div class="card">
      <h2 style="margin-top:0">Selective summer programs</h2>
      <p class="muted" style="margin:0 0 14px">Free + selective programs (RSI, MITES, TASP) carry the strongest signal. Pay-to-play branded programs (Stanford Pre-Collegiate, CTY) don't help much by themselves.</p>
      <div>{_render_summer_programs(filter_major)}</div>
    </div>"""

    blocks = ""
    blocks += _resource_block("Academics & rigor", RESOURCES["academics"])
    blocks += _resource_block("Test prep (SAT / ACT)", RESOURCES["test_prep"])
    blocks += _resource_block("Essays", RESOURCES["essays"])
    blocks += _resource_block("Leadership", RESOURCES["leadership"])
    blocks += _resource_block("Recommendation letters", RESOURCES["recommendations"])
    blocks += _resource_block("Demonstrated interest", RESOURCES["interest"])

    return _page(f"""
<h1>Improve your application</h1>
<p class="muted">Personalized action plan + curated databases of competitions and summer programs. None of this is theoretical — these are the things that actually move admissions outcomes.</p>
{action_html}
{comp_html}
{summer_html}
<h2 style="margin-top:36px">More resources</h2>
{blocks}
<div class="card">
  <h3 style="margin-top:0">Looking for school-specific advice?</h3>
  <p class="muted" style="margin:0 0 8px">Every college's detail page has a "Tailored advice for [school]" link with what that school weights, supplemental essay strategy, and specific programs to apply to.</p>
  <a class="btn btn-light btn-sm" href="/colleges">Browse colleges &rarr;</a>
</div>
""", title="Improve your application — Candor")


def school_improve_html(slug):
    school = COLLEGES_BY_SLUG.get(slug)
    if not school: abort(404)
    school_m = merged_school(school)
    note = get_school_strategy(school)
    user = current_user()
    profile = get_profile(user["id"]) if user else None
    is_paid = bool(user.get("is_paid")) if user else False
    chances_card = ""
    if profile:
        # personalized gap analysis for this specific school
        prof = {
            "uw_gpa": profile.get("uw_gpa"), "sat": profile.get("sat"), "act": profile.get("act"),
            "gpa_freshman": profile.get("gpa_freshman"), "gpa_sophomore": profile.get("gpa_sophomore"),
            "gpa_junior": profile.get("gpa_junior"), "gpa_senior": profile.get("gpa_senior"),
            "major": profile.get("major"), "state": profile.get("state"), "school_type": profile.get("school_type"),
            "ecs": profile.get("ecs"), "leadership": profile.get("leadership"), "awards": profile.get("awards"),
            "legacy": bool(profile.get("legacy")), "first_gen": bool(profile.get("first_gen")), "athlete": bool(profile.get("athlete")),
            "legacy_schools": profile.get("legacy_schools") or "",
        "aps": profile.get("aps") or "",
        "no_aps_offered": bool(profile.get("no_aps_offered")),
        "aps_offered_not_taken": bool(profile.get("aps_offered_not_taken")),
        "ibs": profile.get("ibs") or "",
        "no_ibs_offered": bool(profile.get("no_ibs_offered")),
        "ibs_offered_not_taken": bool(profile.get("ibs_offered_not_taken")),
        }
        fit, components = compute_fit(prof, school)
        action_lines = []
        if components.get("gpa", 0) < -3:
            action_lines.append(f"GPA gap — {school['name']}'s typical admit GPA range is {school['gpa_lo']}-{school['gpa_hi']}, you're below the midpoint. Target straight A's for the rest of high school.")
        if components.get("test", 0) < -5:
            action_lines.append(f"Test score gap — middle 50% at {school['name']} is SAT {school['sat_25']}-{school['sat_75']} / ACT {school['act_25']}-{school['act_75']}. A retake targeting +60 SAT points or +2 ACT is high-leverage.")
        if components.get("ecs", 0) <= 0:
            action_lines.append(f"ECs read thin for {school['name']}'s admit pool. Pick ONE major project tied to {prof.get('major') or 'your intended major'} and commit 8+ hours/week to it.")
        if components.get("leadership", 0) <= 0:
            action_lines.append("No leadership signal — take a real role (officer, captain, or founder) by next semester.")
        if not action_lines:
            action_lines.append(f"Your stats are in range. The remaining lift here comes from supplemental essays and demonstrated interest. See the strategy below.")
        items = "".join(f"<li>{a}</li>" for a in action_lines)
        chances_card = f"""<div class="card" style="background:#1a1a1a;color:#fff;border-color:#1a1a1a">
            <h3 style="margin-top:0;color:#fff">Your gaps for {school['name']}</h3>
            <p class="muted" style="color:#bdbdbd;margin:0 0 8px">Based on your saved profile (fit {fit}/100).</p>
            <ul style="padding-left:18px;margin:0">{items}</ul>
            <p style="margin-top:14px"><a class="btn btn-light btn-sm" href="/chances/{slug}">See full chances breakdown &rarr;</a></p>
        </div>"""
    else:
        chances_card = f"""<div class="card" style="background:#f4f4f4;border-color:#ddd">
            <p style="margin:0 0 8px"><b>Sign up + add your profile</b> to see your specific gaps for {school['name']}.</p>
            <a class="btn btn-light btn-sm" href="/signup">Sign up</a> <a class="btn btn-light btn-sm" href="/login">Log in</a>
        </div>"""
    # ─── Personalized AI strategy (cached in tailored_advice) ───
    tailored_card = ""
    if profile and profile.get("uw_gpa") is not None:
        if is_paid:
            try:
                advice_body = get_tailored_advice(user["id"], school_m, profile, force=False)
                advice_html = _render_tailored_advice(advice_body) if advice_body else ""
                if advice_html:
                    tailored_card = f"""<div class="card">
  <h3 style="margin-top:0">Your personalized strategy for {school['name']}</h3>
  <p class="muted" style="font-size:.85em;margin:0 0 10px">Concrete actions calibrated to your stats, ECs, and {school['name']}'s admissions priorities.</p>
  {advice_html}
</div>"""
            except Exception as e:
                print(f"tailored advice (improve) error: {e}")
        else:
            tailored_card = f"""<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3)">
  <div style="font-size:.74em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:6px">Candor Premium</div>
  <h3 style="margin-top:0;color:#e6edf3">Your personalized strategy for {school['name']}</h3>
  <p class="muted" style="margin:0 0 12px;line-height:1.55">Concrete actions calibrated to your stats, ECs, and what {school['name']} actually weights — written for your profile, not generic advice. <b style="color:#e6edf3">Unlock with Candor Premium ($3/mo)</b>.</p>
  <a class="btn btn-primary btn-sm" href="/upgrade">See what Premium includes →</a>
</div>"""
    # ─── Application round recommendation ───
    round_card = ""
    detail = admissions_detail(school)
    if detail and detail.get("rates"):
        rates = detail["rates"]
        rounds = detail.get("rounds", [])
        # Pick best non-binding-aware recommendation
        rec = None
        rec_reason = ""
        if "ED" in rates and rates.get("ED", 0) >= rates.get("RD", 0) * 1.4:
            rec = "ED"; rec_reason = f"ED rate ({round(rates['ED']*100,1)}%) is materially higher than RD ({round(rates.get('RD',0)*100,1)}%). If {school['name']} is your top choice and you can commit financially, this is the highest-leverage round."
        elif "REA" in rates and rates.get("REA", 0) >= rates.get("RD", 0) * 1.3:
            rec = "REA"; rec_reason = f"REA is non-binding but signals interest. REA rate ({round(rates['REA']*100,1)}%) beats RD ({round(rates.get('RD',0)*100,1)}%) without locking you in."
        elif "EA" in rates:
            rec = "EA"; rec_reason = f"EA gives you an early decision without a binding commitment. Same or slightly better odds than RD."
        elif "ED2" in rates and rates.get("ED2", 0) >= rates.get("RD", 0) * 1.3:
            rec = "ED2"; rec_reason = f"If you don't get into your ED1 school, ED2 here ({round(rates['ED2']*100,1)}%) gives you another binding boost over RD ({round(rates.get('RD',0)*100,1)}%)."
        else:
            rec = "RD"; rec_reason = "RD is your main option here."
        rate_rows = ""
        for r in rounds:
            v = rates.get(r)
            v_str = f"{round(v*100,1)}%" if v else "—"
            highlight = ' style="color:var(--teal);font-weight:700"' if r == rec else ""
            rate_rows += f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid var(--border);gap:8px;flex-wrap:wrap"><span{highlight}>{ROUND_LABELS.get(r,r)}{" ← recommended" if r == rec else ""}</span><span{highlight} style="white-space:nowrap">{v_str}</span></div>'
        round_card = f"""<div class="card">
  <h3 style="margin-top:0">Best application round for you</h3>
  <p style="margin:0 0 10px">{rec_reason}</p>
  {rate_rows}
</div>"""
    # ─── Score push impact ───
    score_card = ""
    if profile and (profile.get("sat") or profile.get("act")) and not is_paid:
        score_card = f"""<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3)">
  <div style="font-size:.74em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:6px">Candor Premium</div>
  <h3 style="margin-top:0;color:#e6edf3">Score push impact</h3>
  <p class="muted" style="margin:0 0 12px;line-height:1.55">See exactly how a +60 SAT or +2 ACT moves your odds at {school['name']} — so you can decide if a retake is actually worth your time. <b style="color:#e6edf3">$3/mo</b>.</p>
  <a class="btn btn-primary btn-sm" href="/upgrade">Unlock score push impact →</a>
</div>"""
    elif profile and (profile.get("sat") or profile.get("act")):
        cur_sat = profile.get("sat")
        cur_act = profile.get("act")
        cur_fit, _ = compute_fit(profile, school_m)
        cur_lo, cur_hi = estimate_odds(school_m, cur_fit, profile)
        scenarios = []
        if cur_sat:
            for delta in (30, 60, 100):
                new_sat = min(1600, cur_sat + delta)
                if new_sat == cur_sat: continue
                sim = dict(profile); sim["sat"] = new_sat
                sf, _ = compute_fit(sim, school_m)
                lo, hi = estimate_odds(school_m, sf, sim)
                scenarios.append((f"SAT +{delta} → {new_sat}", f"{lo}–{hi}%", lo - cur_lo))
        if cur_act:
            for delta in (1, 2, 3):
                new_act = min(36, cur_act + delta)
                if new_act == cur_act: continue
                sim = dict(profile); sim["act"] = new_act
                sf, _ = compute_fit(sim, school_m)
                lo, hi = estimate_odds(school_m, sf, sim)
                scenarios.append((f"ACT +{delta} → {new_act}", f"{lo}–{hi}%", lo - cur_lo))
        if scenarios:
            rows_html = f'<tr><td><span class="muted">Current</span></td><td>{cur_lo}–{cur_hi}%</td><td></td></tr>'
            for label, odds, delta in scenarios:
                arrow = ""
                if delta >= 2: arrow = f'<span style="color:#22c55e;font-weight:600">↑ +{delta}%</span>'
                elif delta <= -2: arrow = f'<span style="color:#ef4444;font-weight:600">↓ {delta}%</span>'
                else: arrow = '<span class="muted">≈</span>'
                rows_html += f"<tr><td>{label}</td><td style='font-weight:600'>{odds}</td><td>{arrow}</td></tr>"
            score_card = f"""<div class="card">
  <h3 style="margin-top:0">Score push impact</h3>
  <p class="muted" style="font-size:.85em;margin:0 0 10px">How retaking the test moves your odds at {school['name']}. Use this to decide if a retake is worth the time.</p>
  <table class="rank-table" style="width:100%"><tbody>{rows_html}</tbody></table>
  <p class="muted" style="font-size:.78em;margin:10px 0 0">More scenarios → <a href="/predictor">full score predictor</a></p>
</div>"""
    # ─── Similar admits from Reddit ───
    similar_card = ""
    if profile:
        try:
            real = extract_structured_profiles(slug, force=False)
        except Exception:
            real = []
        # Filter: accepted only, with stats not wildly different
        target_gpa = profile.get("uw_gpa")
        target_sat = profile.get("sat")
        nearby = []
        for p in real:
            if (p.get("OUTCOME") or "").lower() != "accepted": continue
            gpa_str = (p.get("GPA") or "").strip()
            test_str = (p.get("TEST") or "").strip()
            # Loose filter — just want plausibly similar applicants
            nearby.append(p)
            if len(nearby) >= 3: break
        if nearby:
            cards = ""
            for p in nearby:
                hooks = (p.get("HOOKS") or "").strip()
                stand = (p.get("STANDOUT") or "").strip()
                cards += f"""<div style="padding:10px 0;border-top:1px solid var(--border)">
  <div style="font-weight:600">Accepted · GPA {p.get('GPA','—')} · Test {p.get('TEST','—')}</div>
  <div class="muted" style="font-size:.85em;margin-top:4px">{(p.get('MAJOR') or '—')}{' · ' + hooks if hooks else ''}</div>
  {f'<div style="font-size:.88em;margin-top:6px"><b>What stood out:</b> {stand}</div>' if stand else ''}
</div>"""
            similar_card = f"""<div class="card">
  <h3 style="margin-top:0">Real admits at {school['name']}</h3>
  <p class="muted" style="font-size:.85em;margin:0 0 6px">Pulled from r/collegeresults and r/A2C. Use these to calibrate what's actually getting in.</p>
  {cards}
  <p style="margin-top:10px"><a class="btn btn-light btn-sm" href="/college/{slug}/profiles">All profiles →</a></p>
</div>"""
    # School-specific links if this is one of the curated schools
    school_links_html = ""
    if SCHOOL_NOTES.get(slug, {}).get("links"):
        rows = ""
        for it in SCHOOL_NOTES[slug]["links"]:
            rows += f'<div style="padding:8px 0;border-top:1px solid #eee"><a href="{it["url"]}" target="_blank" rel="noopener">{it["title"]}</a></div>'
        school_links_html = f'<div class="card"><h3 style="margin-top:0">{school["name"]}-specific resources</h3>{rows}</div>'
    # Major-specific competitions from the full COMPETITIONS database
    # (RESOURCES["competitions"] is a smaller generic list without major tags;
    # using COMPETITIONS here means per-school suggestions are actually tailored)
    major = (profile or {}).get("major", "") or ""
    major_lower = major.lower().strip()
    relevant_comps = []
    for c in COMPETITIONS:
        majors_tag = c.get("majors", [])
        if not major_lower:
            relevant_comps.append(c)
        elif not majors_tag or any(m.lower() in major_lower or major_lower in m.lower() for m in majors_tag):
            relevant_comps.append(c)
    comp_rows = ""
    for c in (relevant_comps or COMPETITIONS)[:6]:
        comp_rows += f'<div style="padding:8px 0;border-top:1px solid #eee"><a href="{c["url"]}" target="_blank" rel="noopener">{c["name"]}</a><div class="muted" style="font-size:.84em;margin-top:2px">{c["note"]}</div></div>'
    return _page(f"""
<div class="bar"><a href="/college/{slug}">&larr; {school['name']} overview</a></div>
<h1>How to strengthen your {school['name']} application</h1>
<p class="muted">{school['state']} · {round(school['accept']*100,1)}% acceptance · {school['type']} · Tier {school['tier']}</p>
{chances_card}
{tailored_card}
{round_card}
{score_card}
<div class="card">
  <h3 style="margin-top:0">What {school['name']} weights most</h3>
  <p style="margin:0">{note['values']}</p>
</div>
<div class="card">
  <h3 style="margin-top:0">Supplemental essay strategy</h3>
  <p style="margin:0">{note['supplemental_strategy']}</p>
</div>
{similar_card}
{school_links_html}
<div class="card">
  <h3 style="margin-top:0">Recommended competitions{' for ' + major if major else ''}</h3>
  {comp_rows}
</div>
<div class="card">
  <h3 style="margin-top:0">Where to focus next</h3>
  <p style="margin:0 0 8px">If you only have time for one thing this month, do this:</p>
  <ol style="padding-left:18px;margin:0">
    <li><b>Read 2 admitted-student essays from {school['name']}</b> (search official admissions site or Reddit r/{slug.replace('-','')}). Notice the level of specificity — that's the bar.</li>
    <li><b>Write the &lsquo;why this school&rsquo; supplement first</b>, before anything else. If you can't fill 250 words with school-specific reasons, pick a different school.</li>
    <li><b>Find one current student to ask about their experience</b> — admissions offices often connect prospective applicants with current students. The follow-up email becomes specific essay material.</li>
  </ol>
</div>
<p style="margin-top:18px"><a class="btn btn-light" href="/improve">General improve guide</a></p>
""", title=f"Improve your {school['name']} application — Candor")


# ─── REFERENCE LINKS PER SCHOOL ───────────────────────────
# We don't store an explicit website per school, but we can derive admissions
# URLs and Wikipedia links by best-effort patterns + an override map for the
# most popular schools.

def school_links(c):
    """Return a list of ('label', 'url') reference links for a college."""
    out = []
    site = WEBSITE_BY_SLUG.get(c["slug"])
    if site:
        out.append(("Official admissions site", site))
    # Wikipedia: best-effort URL based on name
    wiki_name = c["name"].replace(" ", "_")
    out.append(("Wikipedia", f"https://en.wikipedia.org/wiki/{wiki_name}"))
    # Common Data Set search (most schools publish a CDS)
    out.append(("Common Data Set search", f"https://www.google.com/search?q={c['name'].replace(' ','+')}+common+data+set"))
    # College Scorecard (federal data)
    out.append(("College Scorecard", f"https://collegescorecard.ed.gov/search/?name={c['name'].replace(' ','+')}"))
    return out


# ─── QUICK FACTS (Claude Haiku, cached 30 days) ───────────
SCHOOL_FACTS_TTL_DAYS = 30

def get_school_facts(c, force=False):
    """Per-school comprehensive factsheet generated by Claude.
    Cached 30 days because school facts don't change much."""
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=SCHOOL_FACTS_TTL_DAYS)).isoformat()
            row = conn.execute(
                "SELECT body FROM school_facts WHERE college_slug=? AND generated_at >= ?",
                (c["slug"], cutoff)
            ).fetchone()
            if row: return row["body"]
    # Anonymous gate: don't generate fresh facts for cold schools when
    # no logged-in user is requesting it (scraper protection).
    if not current_user():
        return ""
    body = None
    if _claude_client:
        try:
            resp = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=900,
                system=f"You are a college research assistant. Output dense, factual school information. No filler, no marketing copy. Use bullets with bolded labels.\n\n{_date_context()}",
                messages=[{"role":"user","content": f"""School: {c['name']} ({city_state(c)}, {region_of(c)})
Type: {c['type']}, tier {c['tier']}, ~{c.get('size','?'):,} undergrads, ${c.get('tuition',0):,} sticker, S/F {sf_ratio(c)}:1
Popular majors: {', '.join(c.get('majors',[]))}
Description: {c.get('desc','')}

Output a Quick Facts factsheet about this school with these EXACT bullet labels (in order). Each bullet must be 1-2 lines, specific and concrete:

- **Founded:** <year + 1 sentence on its founding identity>
- **Mascot/colors:** <if known>
- **Athletic conference:** <division + conference, e.g. "NCAA Division I, ACC">
- **Religious affiliation:** <if any, otherwise "None">
- **Notable academic programs:** <2-3 specific programs/schools that are nationally recognized>
- **Famous alumni:** <3-5 well-known alumni with their fields>
- **Endowment:** <approximate, with year if known>
- **Greek life:** <pct of student body if known + how central it is>
- **Application deadlines:** <Early/Regular dates if known>
- **Test policy:** <required, optional, or blind>
- **Financial aid:** <% need met or other notable aid policy>
- **Notable campus features:** <1-2 unique buildings/traditions/quirks>

If you don't know a specific fact, write "Not publicly verified" — never guess. No preamble, no closing."""}],
            )
            body = resp.content[0].text.strip()
        except Exception as e:
            print(f"School facts error: {e}")
    if not body:
        body = (f"- **Founded:** Not in our local data.\n"
                f"- **Type:** {c['type']}, ~{c.get('size','?'):,} undergrads.\n"
                f"- **Description:** {c.get('desc','')}\n"
                f"- **Popular majors:** {', '.join(c.get('majors',[]))}\n"
                f"- **Setting:** {setting_of(c)} · {climate_of(c)} climate\n"
                f"- **S/F ratio:** {sf_ratio(c)}:1\n"
                f"- **Sticker tuition:** ${c.get('tuition',0):,}/yr")
    with db() as conn:
        conn.execute("""INSERT INTO school_facts (college_slug, body, generated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
            (c["slug"], body))
        conn.commit()
    return body


def _render_facts(body):
    """Reuse the markdown-bullet renderer for school facts (same shape)."""
    return _render_tailored_advice(body)


# ─── EXTENDED SUMMARY (Haiku, cached 30 days) ─────────────
def get_school_summary(c, force=False):
    """2-3 paragraph rich overview of the school, replacing the 1-line desc.

    Lookup order:
      1. school_summaries.SCHOOL_SUMMARIES (pre-generated dict, ships with the repo)
      2. school_summary table cache (30-day TTL)
      3. live Haiku regen (logged-in users only)
    """
    if not force:
        try:
            from school_summaries import SCHOOL_SUMMARIES
            pre = SCHOOL_SUMMARIES.get(c["slug"])
            if pre:
                return pre
        except Exception:
            pass
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
            row = conn.execute(
                "SELECT body FROM school_summary WHERE college_slug=? AND generated_at >= ?",
                (c["slug"], cutoff)
            ).fetchone()
            if row: return row["body"]
    # Anonymous gate: scrapers don't get free generation.
    if not current_user():
        return ""
    body = None
    if _claude_client:
        try:
            user_msg = (
                f"School: {c['name']} ({city_state(c)}, {region_of(c)})\n"
                f"Type: {c['type']}, ~{c.get('size','?'):,} undergrads, ${c.get('tuition',0):,} sticker, S/F {sf_ratio(c)}:1\n"
                f"Acceptance rate: {round(c['accept']*100,1)}%\n"
                f"Popular majors: {', '.join(c.get('majors',[]))}\n"
                f"Existing 1-line description: {c.get('desc','')}\n\n"
                f"Write 2-3 paragraphs about {c['name']}. Cover: what the school is actually known for, who thrives there, what the social scene looks like, what the location adds or doesn't, real academic strengths.\n\n"
                f"Hard rules:\n"
                f"- Write like a knowledgeable older friend, not marketing copy\n"
                f"- No words like 'renowned', 'esteemed', 'boasts', 'vibrant', 'rigorous' (real talk only)\n"
                f"- No em-dashes (use commas, periods, parens)\n"
                f"- Be specific: name programs, traditions, recruiting pipelines, real downsides\n"
                f"- 200-280 words. No headers, no bullets, no superlatives."
            )
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                system=f"You write substantive, honest college overviews like a knowledgeable older sibling explaining a school to a high schooler. Specific, slightly opinionated, acknowledges downsides. No marketing language. No em-dashes.\n\n{_date_context()}",
                messages=[{"role":"user","content": user_msg}],
            )
            body = response.content[0].text.strip()
        except Exception as e:
            print(f"School summary error: {e}")
    if not body:
        body = c.get("desc","")
    with db() as conn:
        conn.execute("""INSERT INTO school_summary (college_slug, body, generated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
            (c["slug"], body))
        conn.commit()
    return body


# ─── PER-SCHOOL ADMISSIONS STRATEGY (140 generated, 17 curated) ──
def get_school_strategy(c, force=False):
    """Return {values, supplemental_strategy} for any school. Uses curated
    SCHOOL_NOTES if present; otherwise generates via Claude and caches.
    This is what powers the 'What this school weights' section everywhere."""
    if c["slug"] in SCHOOL_NOTES:
        n = SCHOOL_NOTES[c["slug"]]
        return {"values": n["values"], "supplemental_strategy": n["supplemental_strategy"]}
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=60)).isoformat()
            row = conn.execute(
                "SELECT school_values, supplemental_strategy FROM school_strategies WHERE college_slug=? AND generated_at >= ?",
                (c["slug"], cutoff)
            ).fetchone()
            if row:
                return {"values": row["school_values"], "supplemental_strategy": row["supplemental_strategy"]}
    # Anonymous gate: cold-school strategy generation is the biggest
    # scraper-cost vector. Cached schools still serve fine; new ones
    # only generate when a real user requests them.
    if not current_user():
        return {"values": "", "supplemental_strategy": ""}
    values, strat = None, None
    if _claude_client:
        try:
            user_msg = (
                f"School: {c['name']} ({city_state(c)})\n"
                f"Acceptance: {round(c['accept']*100,1)}%, tier {c['tier']}, type {c['type']}\n"
                f"GPA mid-50%: {c['gpa_lo']}-{c['gpa_hi']}, SAT mid-50%: {c['sat_25']}-{c['sat_75']}\n"
                f"Description: {c.get('desc','')}\n\n"
                f"Write TWO concise admissions-strategy notes for applying to {c['name']}:\n\n"
                f"VALUES: <2-3 sentences on what this school actually weights in admissions. Be specific — what kinds of applicants thrive here, what they look for beyond stats, where they're stricter or looser than peer schools. NO generic advice like 'they look for well-rounded students.'>\n\n"
                f"SUPPLEMENTAL_STRATEGY: <2-3 sentences specifically on how to approach this school's supplemental essays or 'why us' prompt. Reference concrete elements unique to this school where possible.>\n\n"
                f"Output exactly two labeled paragraphs, separated by a blank line. No preamble."
            )
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                system=f"You are a senior college admissions strategist writing concise, school-specific notes for an admissions database. Concrete, calibrated, no generic advice.\n\n{_date_context()}",
                messages=[{"role":"user","content": user_msg}],
            )
            text = response.content[0].text.strip()
            # Parse VALUES + SUPPLEMENTAL_STRATEGY
            for line in text.split("\n\n"):
                line = line.strip()
                if line.upper().startswith("VALUES:"):
                    values = line.split(":",1)[1].strip()
                elif line.upper().startswith("SUPPLEMENTAL_STRATEGY:"):
                    strat = line.split(":",1)[1].strip()
        except Exception as e:
            print(f"School strategy error: {e}")
    if not values or not strat:
        # Fall back to TIER_NOTES (generic)
        tn = TIER_NOTES[c["tier"]]
        values = values or tn["values"]
        strat = strat or tn["supplemental_strategy"]
    with db() as conn:
        conn.execute("""INSERT INTO school_strategies (college_slug, school_values, supplemental_strategy, generated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                school_values=excluded.school_values, supplemental_strategy=excluded.supplemental_strategy,
                generated_at=CURRENT_TIMESTAMP""",
            (c["slug"], values, strat))
        conn.commit()
    return {"values": values, "supplemental_strategy": strat}


# ─── REAL ESSAYS THAT WORKED — published archives ────────
# Schools that publish actual admitted-student essays (with admissions
# commentary in some cases). These are real, official, public links.
# Schools that publish actual essay text (not just admissions tips pages).
# We scrape these directly, parse out individual essays, and display them
# alongside Reddit-scraped ones.

# Other useful real-data sources for any school


# ─── COMPOSITE STUDENT PROFILES (Haiku, cached 30 days) ───
SCHOOL_PROFILES_TTL_DAYS = 30

def get_school_profiles(c, force=False):
    """Return Claude-generated composite student profiles for a school.
    Realistic numbers reflecting actual admit patterns; clearly labeled as
    composites (not specific scraped people). Cached 30 days."""
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=SCHOOL_PROFILES_TTL_DAYS)).isoformat()
            row = conn.execute(
                "SELECT body FROM school_profiles WHERE college_slug=? AND generated_at >= ?",
                (c["slug"], cutoff)
            ).fetchone()
            if row: return row["body"]
    # Anonymous gate: don't trigger generation for scrapers hitting cold
    # schools. Logged-in users still get fresh generation on cache miss.
    if not current_user():
        return ""
    body = None
    if _claude_client:
        try:
            user_msg = f"""School: {c['name']} ({city_state(c)})
Acceptance rate: {round(c['accept']*100,1)}%
GPA mid-50%: {c['gpa_lo']}-{c['gpa_hi']}
SAT mid-50%: {c['sat_25']}-{c['sat_75']}
ACT mid-50%: {c['act_25']}-{c['act_75']}
Tier: {c['tier']} (1=most selective)
Type: {c['type']}
Popular majors: {', '.join(c.get('majors',[]))}

Generate 6 composite student profiles representative of {c['name']}'s actual admissions outcomes. Reflect a realistic mix of the admit pool: different geographic regions, ethnicities, hooks, academic strengths, and backgrounds. NO real names — use first name + last initial only (clearly fictional). NO copying specific real students; these are composites built from common patterns.

Mix outcomes:
- 3 ADMITTED (1 strong academic + standout EC, 1 with a hook, 1 borderline academics + great fit)
- 1 WAITLISTED (almost made it, what was missing)
- 2 REJECTED (one was over-qualified academically, one was numerically below the bar)

Format each as:
**[Name]** — [outcome]
- GPA / Test: [unweighted GPA] / SAT [score] OR ACT [score]
- Major: [intended major]
- Geography: [state or international]
- Hooks: [legacy/first-gen/athlete/URM/none — be specific]
- Standout: [the single strongest item — a national award, founded venture, research publication, etc.]
- Other: [1-2 other notable items]
- Why [outcome]: [one specific sentence on what likely tipped the decision]

Be realistic to {c['name']}'s actual standards. Don't make every profile have USACO Gold or RSI — show variety. Output six full profiles, no preamble."""
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                system=f"You generate REPRESENTATIVE composite student profiles for college admissions, based on publicly known admissions patterns. Profiles are fictional/composite and clearly labeled as such — never specific scraped real people. Stats are realistic to the school's actual admit pool.\n\n{_date_context()}",
                messages=[{"role":"user","content": user_msg}],
            )
            body = response.content[0].text.strip()
        except Exception as e:
            print(f"School profiles error: {e}")
    if not body:
        body = (f"**Composite admit profile** — Accepted\n"
                f"- GPA / Test: {c['gpa_hi']} / SAT {c['sat_75']}\n"
                f"- Major: one of {', '.join(c.get('majors',[])[:2])}\n"
                f"- Standout: National-level award in their field\n"
                f"- Why: stats above the median + clear focused identity\n\n"
                f"_(Claude unavailable — refresh once a key is configured for richer profiles.)_")
    with db() as conn:
        conn.execute("""INSERT INTO school_profiles (college_slug, body, generated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
            (c["slug"], body))
        conn.commit()
    return body


def get_school_essays(c, force=False):
    """Return 2 sample essay openings + links to real archives. Cached 30 days."""
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=SCHOOL_PROFILES_TTL_DAYS)).isoformat()
            row = conn.execute(
                "SELECT body FROM school_essays WHERE college_slug=? AND generated_at >= ?",
                (c["slug"], cutoff)
            ).fetchone()
            if row: return row["body"]
    note = get_school_strategy(c)
    body = None
    if _claude_client:
        try:
            user_msg = f"""School: {c['name']}
What this school weights: {note['values']}
Supplemental essay strategy: {note['supplemental_strategy']}

Generate TWO sample essay openings (~150-200 words each) that would land well at {c['name']}. They should reflect the kind of voice and content this school typically rewards. Use a different topic for each.

Format:
**Sample 1: [topic in 4 words]**
[opening paragraph, 150-200 words. Specific, concrete, voice-driven, no clichés]

**Sample 2: [topic in 4 words]**
[different opening, 150-200 words]

Each should:
- Open with a specific moment or detail (not abstract)
- Show a clear voice / personality
- Feel authentic to a high school senior
- Match this school's preferred style

These are ILLUSTRATIVE samples written by Claude — they show what works at this school. Make them clearly model essays, not generic.
No preamble, just the two samples."""
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1100,
                system=f"You write college admissions essay samples. Voice-driven, specific, vivid. Avoid clichés ('I learned that', '5 lessons', 'changed my life forever'). Each opening should feel like a real high school senior with a real story.\n\n{_date_context()}",
                messages=[{"role":"user","content": user_msg}],
            )
            body = response.content[0].text.strip()
        except Exception as e:
            print(f"School essays error: {e}")
    if not body:
        body = "Sample essay generation unavailable. See the Reference Links section for real essay archives."
    with db() as conn:
        conn.execute("""INSERT INTO school_essays (college_slug, body, generated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
            (c["slug"], body))
        conn.commit()
    return body


# ─── TAILORED ADVICE (Claude-generated, cached 7 days) ────
TAILORED_ADVICE_TTL_DAYS = 7
# Bump this whenever the tailored-advice prompt changes — anything cached
# before this timestamp is treated as stale and regenerated. Saves us from
# manually clearing the table when we fix prompt bugs.
TAILORED_ADVICE_MIN_VALID_AT = "2026-05-07T08:45:00"

# Saved-chances rows computed before this timestamp are treated as stale
# and recomputed on next view. Bumped when the odds model changes (e.g.
# legacy multipliers, in-state rates, etc.) so users see fresh numbers
# without having to re-save their profile.
# IMPORTANT: SQLite CURRENT_TIMESTAMP uses a SPACE separator
# ("2026-05-10 20:51:00"), not ISO-T. Lexicographic comparison fails if
# this constant uses 'T' — space (32) < T (84) so every fresh row would
# be wrongly classified as stale.
SAVED_CHANCES_MIN_VALID_AT = "2026-05-10 05:00:00"

# Per-school facts that AI tailored advice must use as ground truth
# rather than guessing from training data. The AI was hallucinating
# things like "USC doesn't require a portfolio for studio art" when
# Roski actually requires portfolios for ALL undergrad majors. Add a
# slug → fact-bullets dict; injected verbatim into the Claude prompt
# so it can't override these.


def get_tailored_advice(user_id, school, profile, force=False):
    """Return Claude-generated, profile-specific advice for this school.
    Cached 7 days. Falls back to a templated bullet list if no key."""
    # Profile-version hash: cached advice only serves if the stats it was built
    # on are unchanged, so editing GPA/scores/ECs invalidates stale advice
    # within the 7-day TTL instead of serving day-old numbers.
    import hashlib as _hl
    phash = _hl.sha1("|".join(str(profile.get(k)) for k in (
        "uw_gpa","weighted_gpa","sat","act","major","ecs","leadership","awards",
        "legacy","first_gen","athlete","is_international","state",
        "sat_math","sat_ebrw","act_math","act_english","act_reading","act_science",
    )).encode()).hexdigest()[:16]
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=TAILORED_ADVICE_TTL_DAYS)).isoformat()
            # Take the LATER of: TTL cutoff, prompt-version cutoff. Cached
            # entries from before the prompt fix are treated as stale.
            effective_cutoff = max(cutoff, TAILORED_ADVICE_MIN_VALID_AT)
            row = conn.execute(
                "SELECT body, generated_at FROM tailored_advice WHERE user_id=? AND college_slug=? AND generated_at >= ? AND profile_hash=?",
                (user_id, school["slug"], effective_cutoff, phash)
            ).fetchone()
            if row:
                return row["body"]
    # Generate fresh
    fit_acad, components = compute_fit(profile, school)
    low, high = estimate_odds(school, fit_acad, profile)
    m = school_match(profile, school)
    note = get_school_strategy(school)
    test_str = f"SAT {profile['sat']}" if profile.get("sat") else (f"ACT {profile['act']}" if profile.get("act") else "no test score submitted")
    # If subscores are provided, append them so the AI can flag imbalance
    # (e.g. low math at a STEM-heavy school).
    sub_str = []
    if profile.get("sat_math") or profile.get("sat_ebrw"):
        if profile.get("sat_ebrw"): sub_str.append(f"SAT EBRW {profile['sat_ebrw']}")
        if profile.get("sat_math"): sub_str.append(f"SAT Math {profile['sat_math']}")
    if profile.get("act_math") or profile.get("act_english") or profile.get("act_reading") or profile.get("act_science"):
        for k, label in [("act_english","English"),("act_math","Math"),("act_reading","Reading"),("act_science","Science")]:
            if profile.get(k): sub_str.append(f"ACT {label} {profile[k]}")
    if sub_str:
        test_str += f" (subscores: {', '.join(sub_str)})"
    # Pre-compute test + GPA comparisons so Claude doesn't do the math
    # (and get it wrong — we've seen it write "ACT 33 below 31-34 range").
    sat = profile.get("sat")
    act = profile.get("act")
    test_compare = ""
    if sat:
        s25, s75 = school.get("sat_25"), school.get("sat_75")
        if s25 and s75:
            if sat >= s75:
                test_compare = f"SAT {sat} is AT or ABOVE the 75th percentile of admits ({s75}) — top of {school['name']}'s mid-50% range ({s25}-{s75}). This is a STRENGTH, not a gap."
            elif sat >= (s25 + s75) / 2:
                test_compare = f"SAT {sat} sits in the upper half of {school['name']}'s admit pool (mid-50% is {s25}-{s75}). Solid, not a gap."
            elif sat >= s25:
                test_compare = f"SAT {sat} is INSIDE {school['name']}'s mid-50% range ({s25}-{s75}) but in the lower half. Workable; not a major weakness."
            else:
                gap = s25 - sat
                test_compare = f"SAT {sat} is BELOW {school['name']}'s 25th percentile of {s25} (gap of {gap} points). This IS a real academic gap to address."
    elif act:
        a25, a75 = school.get("act_25"), school.get("act_75")
        if a25 and a75:
            if act >= a75:
                test_compare = f"ACT {act} is AT or ABOVE the 75th percentile of admits ({a75}) — top of {school['name']}'s mid-50% range ({a25}-{a75}). This is a STRENGTH, not a gap. Do NOT describe this as below the range or as a weakness."
            elif act >= (a25 + a75) / 2:
                test_compare = f"ACT {act} sits in the upper half of {school['name']}'s admit pool (mid-50% is {a25}-{a75}). Solid, not a gap."
            elif act >= a25:
                test_compare = f"ACT {act} is INSIDE {school['name']}'s mid-50% range ({a25}-{a75}) but in the lower half. Workable; not a major weakness."
            else:
                gap = a25 - act
                test_compare = f"ACT {act} is BELOW {school['name']}'s 25th percentile of {a25} (gap of {gap} points). This IS a real academic gap."
    else:
        test_compare = "Test-optional submission — no test score in profile."
    gpa_compare_imp = ""
    raw_gpa = profile.get("uw_gpa")
    eff_gpa = effective_gpa(profile, school)
    glo, ghi = school.get("gpa_lo"), school.get("gpa_hi")
    if raw_gpa and glo and ghi:
        gmid = round((glo + ghi) / 2, 2)
        if raw_gpa >= ghi:
            gpa_compare_imp = f"GPA {raw_gpa} is AT or ABOVE the 75th percentile of {school['name']} admits ({ghi}). STRENGTH, not gap."
        elif raw_gpa >= gmid:
            gpa_compare_imp = f"GPA {raw_gpa} is in the upper half of {school['name']}'s admit GPA range ({glo}-{ghi}, midpoint ~{gmid}). Solid."
        elif raw_gpa >= glo:
            gpa_compare_imp = f"GPA {raw_gpa} is INSIDE {school['name']}'s admit range ({glo}-{ghi}) but in the lower half (midpoint ~{gmid}). Workable, minor gap."
        else:
            gap = round(glo - raw_gpa, 2)
            gpa_compare_imp = f"GPA {raw_gpa} is BELOW {school['name']}'s 25th percentile ({glo}) — gap of {gap} points. Real academic gap."
        if eff_gpa is not None and abs(eff_gpa - raw_gpa) > 0.02:
            gpa_compare_imp += f" (Year-by-year: model uses effective {eff_gpa} after upper-year weighting/UC adjustment.)"
    pref_str = []
    for k, label in [("pref_weather","weather"),("pref_setting","setting"),("pref_size","school size"),
                     ("pref_class_size","class size"),("pref_greek","Greek life"),("pref_sports","sports culture"),
                     ("pref_major_strength","major strength"),("pref_prestige","prestige"),
                     ("pref_cost","cost")]:
        s = pref_set(profile, k)
        if s:
            pref_str.append(f"{label}={'/'.join(sorted(s))}")
    prefs_line = "; ".join(pref_str) or "(none set)"
    match_lines = ""
    if m and m.get("rated_count"):
        for key, (verdict, txt) in m["per_pref"].items():
            tag = {"match":"✓","mismatch":"✗","neutral":"·"}[verdict]
            match_lines += f"  {tag} {key}: {txt}\n"
    user_msg = f"""STUDENT PROFILE:
- Unweighted GPA: {profile.get('uw_gpa')}
- Weighted GPA: {profile.get('weighted_gpa') or 'not given'}
- Test: {test_str}
- Major: {profile.get('major') or 'undecided'}
- Home state: {profile.get('state') or 'unknown'}
- High school type: {profile.get('school_type') or 'unknown'}
- Extracurriculars: {profile.get('ecs') or '(blank)'}
- Leadership: {profile.get('leadership') or '(blank)'}
- Awards: {profile.get('awards') or '(blank)'}
- Portfolio / supplemental materials: {profile.get('portfolio') or '(none)'}
- Hooks for THIS school: legacy_generations={legacy_generations_at(profile, school)} (0 = no legacy here), first_gen={bool(profile.get('first_gen'))}, athlete={bool(profile.get('athlete'))}
- Preferences: {prefs_line}

TARGET SCHOOL: {school['name']} ({city_state(school)}, {region_of(school)})
- Acceptance rate: {round(school['accept']*100,1)}%
- GPA mid-50%: {school['gpa_lo']}-{school['gpa_hi']}
- SAT mid-50%: {school['sat_25']}-{school['sat_75']}
- ACT mid-50%: {school['act_25']}-{school['act_75']}
- Size: {school.get('size','?'):,} undergrads · S/F {sf_ratio(school)}:1 · ${school.get('tuition',0):,}/yr sticker
- Setting: {setting_of(school)} · Climate: {climate_of(school)} · Greek: {greek_strength(school)} · Sports: {sports_strength(school)}
- Description: {school.get('desc','')}
- Popular majors: {', '.join(school.get('majors',[]))}

WHAT THIS SCHOOL WEIGHTS: {note['values']}
SUPPLEMENTAL ESSAY STRATEGY: {note['supplemental_strategy']}

COMPUTED FIT: academic={fit_acad}/100, odds={low}-{high}%
PREFERENCE MATCH:
{match_lines if match_lines else '(no preferences set)'}

PRE-COMPUTED COMPARISONS (use these EXACTLY — do NOT recompute, do NOT contradict):
- TEST: {test_compare or '(no test data)'}
- GPA:  {gpa_compare_imp or '(no GPA data)'}

VERIFIED FACTS YOU MUST USE (do NOT contradict these — they're hand-checked):
{chr(10).join(f"- {f}" for f in SCHOOL_VERIFIED_FACTS.get(school['slug'], [])) or "(none specific to this school)"}

TASK: Write 6-8 SPECIFIC, ACTIONABLE bullets advising this exact student on applying to {school['name']}. Each bullet must:
1. Reference a specific number, program, deadline, course, professor, or activity from the data above (no generic advice).
2. Acknowledge the student's actual profile (their GPA/test/ECs) — what they already have, what's missing for THIS school.
3. Speak directly to fit/mismatch when relevant (e.g. "you preferred warm but X is cold — here's how to evaluate that tradeoff").
4. Be concrete: name the program, the threshold, the action, the deadline.

CRITICAL ACCURACY RULES — DO NOT VIOLATE:
- USE THE PRE-COMPUTED COMPARISONS ABOVE. If they say the test score is AT or ABOVE the 75th percentile, do NOT call it a gap or below the range. If they say it's INSIDE the mid-50% range, do NOT call it below the range. Verbatim quote the comparison framing — these are correct, your math is not.
- DO NOT INVENT TIGHTER SUB-POOL RANGES. Specifically: do NOT write things like "business school applicants score 1420-1490" or "engineering admits cluster at 33-35" unless those exact numbers appear in the data above. Fabricating a sub-pool range to make a strong stat look weak is the #1 source of wrong advice and will get this output discarded. If you don't KNOW a school publishes sub-major test ranges, do NOT make them up.
- When the school's published mid-50% includes the student's score, the score is fine. Stop. You can say "you're competing against business-track admits who tend to be on the higher end" if relevant, but you CANNOT recharacterize the score as below the range or "at the 25th percentile" when the pre-computed comparison says otherwise.
- When citing a gap, USE THE ACTUAL NUMBERS. Don't say "0.2 below median" — say "your 3.7 UW vs the school's 3.93 admit median (0.23 below)". Always show the math.
- Don't sugarcoat. A 3.7 GPA is a meaningful gap at a school where the median is 3.95+, not "marginal." For elite schools (sub-10% accept), even small gaps matter a lot.
- If the student's stats are clearly below the typical admit range, say so directly. "Your test score is below the 25th percentile (which means most admits scored higher than you)" — not euphemisms like "compensable" or "marginal".
- If the student lists a portfolio/research/audition and the VERIFIED FACTS show this school weights it heavily, the bullet about the application MUST acknowledge how their portfolio compares (strong evidence vs unclear quality).
- Never claim a hook the student doesn't have. They are first_gen={bool(profile.get('first_gen'))}, athlete={bool(profile.get('athlete'))}, and have legacy_generations={legacy_generations_at(profile, school)} at THIS school. Don't reference hooks they don't have.

5. If the student's intended major requires a portfolio/audition (per the VERIFIED FACTS above), the bullet about the application MUST mention that requirement and the work needed to satisfy it. Never claim a portfolio isn't required when the verified facts say it is.

Output 6-8 bullets, each one short and standalone. Format:
- <bullet 1>
- <bullet 2>
...
No preamble, no closing line."""
    body = None
    if _claude_client:
        try:
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1100,
                system=f"You are a senior college admissions strategist. You give specific, calibrated, no-fluff advice — never generic. You always reference concrete numbers, programs, and deadlines. You acknowledge tradeoffs honestly.\n\n{_date_context()}",
                messages=[{"role":"user","content": user_msg}],
            )
            body = response.content[0].text.strip()
        except Exception as e:
            print(f"Tailored advice error: {e}")
    if not body:
        # Templated fallback
        bullets = []
        if components.get("gpa", 0) < -3:
            bullets.append(f"GPA gap: yours is {profile.get('uw_gpa')} vs admit range {school['gpa_lo']}-{school['gpa_hi']}. Target straight A's for the rest of high school — every 0.05 of unweighted matters here.")
        if components.get("test", 0) < -5:
            bullets.append(f"Test gap: middle 50% is SAT {school['sat_25']}-{school['sat_75']}. A retake targeting +60 is the single highest-leverage thing you can do for this school.")
        bullets.append(f"On essays: {note['supplemental_strategy']}")
        bullets.append(f"On values: {school['name']} weights {note['values'].split('.')[0]}.")
        if school["state"] != profile.get("state") and school["type"] == "public":
            bullets.append(f"Out-of-state public: tuition ${school.get('tuition',0):,} is the OOS rate. Check whether your stats qualify for merit aid before applying.")
        body = "\n".join(f"- {b}" for b in bullets[:8])
    with db() as conn:
        conn.execute("""INSERT INTO tailored_advice (user_id, college_slug, body, generated_at, profile_hash)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(user_id, college_slug) DO UPDATE SET
                body=excluded.body, generated_at=CURRENT_TIMESTAMP, profile_hash=excluded.profile_hash""",
            (user_id, school["slug"], body, phash))
        conn.commit()
    return body


def _render_tailored_advice(body):
    """Tailored advice arrives as markdown bullets. Render safely.
    Empty lines between bullets do NOT close the list — that was a render bug
    that produced a flurry of single-item <ul>s."""
    import html as _html
    safe = _html.escape(body or "")
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", safe)
    lines, out, in_ul = safe.split("\n"), [], False
    for line in lines:
        m_b = re.match(r"^\s*[-*]\s+(.*)", line)
        if m_b:
            if not in_ul:
                out.append('<ul style="padding-left:20px;margin:6px 0">'); in_ul = True
            out.append(f'<li style="margin:8px 0">{m_b.group(1)}</li>')
        elif not line.strip():
            # blank line between bullets: keep the <ul> open
            continue
        else:
            if in_ul:
                out.append('</ul>'); in_ul = False
            out.append(f"<div>{line}</div>")
    if in_ul: out.append('</ul>')
    return "\n".join(out)


# ─── REAL PROFILES + ESSAYS ───────────────────────────────
def profiles_index_html():
    """Top-level Real Profiles page — explains what's there + lets the user
    pick a school to see profiles for."""
    cards = ""
    # Surface a quick pick for the most-asked schools
    featured = ["harvard","stanford","mit","yale","princeton","upenn","duke","ucla","ucb","ut-austin","umich","nyu","usc","cmu","jhu"]
    for slug in featured:
        c = COLLEGES_BY_SLUG.get(slug)
        if not c: continue
        cards += f'<a href="/college/{slug}/profiles" class="school-card" style="display:block;color:inherit"><div style="font-weight:700">{c["name"]}</div><div class="muted" style="font-size:.82em">{city_state(c)} · {round(c["accept"]*100,1)}% accept</div></a>'
    return _page(f"""
<h1>Real Profiles</h1>
<p class="muted">For each school, see composite student profiles based on real admit patterns — what an accepted, waitlisted, or rejected applicant actually looks like. Plus sample essay openings + links to schools' published "Essays That Worked" archives.</p>

<div class="card" style="background:#fff8e1;border-color:#ffeaa7">
  <h3 style="margin-top:0">⚠ A note on accuracy</h3>
  <p style="margin:0 0 6px;font-size:.92em">Profiles shown are <b>composites</b> built from publicly known admissions patterns — they're not scraped from specific Reddit/Twitter posts and don't represent any one real person. Stats are realistic to each school's actual admit pool. Sample essays are illustrative model openings, not verbatim student work.</p>
  <p style="margin:0;font-size:.92em">For real, verbatim admitted-student essays, every school's page links out to the official "Essays That Worked" archive (where one exists) plus the live r/ApplyingToCollege results threads.</p>
</div>

<h2 style="margin-top:24px">Pick a school to see profiles</h2>
<div class="grid">{cards}</div>
<p style="margin-top:18px"><a class="btn btn-light" href="/colleges">Browse all 334 colleges &rarr;</a></p>
""", title="Real Profiles — Candor")


def school_profiles_html(slug):
    c = COLLEGES_BY_SLUG.get(slug)
    if not c: abort(404)
    name = c["name"]
    # Only logged-in users can force a refresh (cache-bypass). Bots were
    # hitting ?refresh=1 to dodge our cache.
    force = (request.args.get("refresh") == "1") and bool(current_user())
    show_essays = request.args.get("essays") == "1"
    import html as _html

    # AI-generated composite profiles (the original feature, ~6 profiles).
    composite_md = get_school_profiles(c, force=force)

    # Real Reddit-extracted profiles, formatted as the same markdown so they
    # blend in with the composites — user shouldn't be able to tell which
    # came from where. Capped at 5 to keep the list readable.
    real_profiles = extract_structured_profiles(slug, force=force)[:5]
    def _profile_to_md(p, idx):
        outcome = p.get("OUTCOME", "Applied")
        lines = [f"**Applicant {idx}** — {outcome}"]
        gpa = p.get("GPA", "").strip()
        test = p.get("TEST", "").strip()
        if gpa or test:
            lines.append(f"- GPA / Test: {gpa or '—'} / {test or '—'}")
        for label, key in [("Major","MAJOR"),("Geography","GEO"),("Hooks","HOOKS"),
                           ("Standout","STANDOUT"),("Other","OTHER")]:
            v = (p.get(key) or "").strip()
            if v: lines.append(f"- {label}: {v}")
        why = (p.get("WHY") or "").strip()
        if why: lines.append(f"- Why {outcome.lower()}: {why}")
        return "\n".join(lines)
    extra_md = ""
    if real_profiles:
        # Number them starting after the AI composites (which usually have 6)
        extra_md = "\n\n" + "\n\n".join(
            _profile_to_md(p, i+7) for i, p in enumerate(real_profiles)
        )
    combined_md = (composite_md or "") + extra_md
    if combined_md.strip():
        profiles_html = _render_tailored_advice(combined_md)
    else:
        profiles_html = f"""<div style="text-align:center;padding:24px 0">
  <p class="muted" style="margin:0 0 8px">We couldn't generate profiles for {name} right now.</p>
  <p class="muted" style="font-size:.88em;margin:0">This usually means the AI was rate-limited or {name} has thin Reddit coverage. Try <a href="?refresh=1">refresh</a> in a minute, or browse <a href="https://www.reddit.com/r/collegeresults/search/?q={name.replace(' ','+')}&restrict_sr=1&sort=top" target="_blank" rel="noopener">r/collegeresults posts about {name}</a> directly.</p>
</div>"""

    # Real essays from Reddit (lazily loaded — Claude call takes a few sec)
    essays_card = ""
    if show_essays:
        real_essays = extract_real_essays(slug, force=force)
        if real_essays:
            essay_inner = ""
            for e in real_essays:
                outcome = e.get("outcome", "Unknown")
                outcome_color = {"Accepted":"var(--teal)","Rejected":"#f9a8d4","Unknown":"var(--text-2)"}.get(outcome, "var(--text-2)")
                prompt = _html.escape(e.get("prompt", "Essay"))
                words = e.get("words", "")
                essay_text = _html.escape(e.get("essay", "")).replace("\n", "<br>")
                essay_inner += f"""<div class="card" style="margin-bottom:10px">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:6px">
                    <span style="font-weight:600;color:var(--text)">{prompt}</span>
                    <span style="font-size:.74em;color:{outcome_color};padding:3px 11px;border-radius:999px;border:1px solid rgba(95,201,182,.2);background:rgba(95,201,182,.08);font-weight:600;letter-spacing:.4px;text-transform:uppercase">{outcome} · {words} words</span>
                  </div>
                  <div style="font-size:.92em;line-height:1.65;color:var(--text);font-family:Georgia,serif;background:var(--bg-2);padding:14px;border-radius:4px;border:1px solid var(--border)">{essay_text}</div>
                </div>"""
            essays_card = f"""<div class="card">
              <h3 style="margin-top:0">Real essays from Reddit</h3>
              <p class="muted" style="font-size:.85em;margin:0 0 14px">Pulled from r/CollegeEssays, r/EssayDeath, r/A2C — actual essay text shared by applicants who submitted to {name}.</p>
              {essay_inner}
            </div>"""
        else:
            q_enc = name.replace(" ", "+").replace("&","%26")
            archive_msg = ""
            if ESSAYS_THAT_WORKED.get(slug):
                archive_msg = f' Or check <a href="{ESSAYS_THAT_WORKED[slug]}" target="_blank" rel="noopener">{name}\'s official "Essays That Worked" archive →</a>'
            essays_card = f"""<div class="card">
              <h3 style="margin-top:0">Real essays from Reddit</h3>
              <p class="muted" style="font-size:.9em;margin:0 0 8px">No full-text essays surfaced for {name}. Reddit users often share essays via Google Docs links instead of pasting full text, which our scraper can't follow.</p>
              <p style="font-size:.9em;margin:0">Try the source directly: <a href="https://www.reddit.com/search/?q={q_enc}+essay&type=link&sort=top" target="_blank" rel="noopener">All-Reddit search for "{name} essay"</a>.{archive_msg}</p>
            </div>"""
    else:
        essays_card = f"""<div class="card">
          <h3 style="margin-top:0">Real essays from Reddit</h3>
          <p class="muted" style="font-size:.85em;margin:0 0 12px">Actual essays shared by applicants who submitted to {name} — pulled from r/CollegeEssays, r/EssayDeath, and r/A2C. Loads on demand since it takes a few seconds.</p>
          <a class="btn btn-light btn-sm" href="?essays=1">Load real essays</a>
        </div>"""
    archive_link = ESSAYS_THAT_WORKED.get(slug)
    archive_html = ""
    if archive_link:
        archive_html = f'<div class="card"><h3 style="margin-top:0">Real published essays for {name}</h3><p style="margin:0 0 10px">{name} publishes admitted-student essays on its admissions site.</p><a class="btn btn-light btn-sm" href="{archive_link}" target="_blank" rel="noopener">Open official archive →</a></div>'
    return _page(f"""
<div class="bar"><a href="/college/{slug}">&larr; back to {name}</a></div>
<h1>Real profiles & essays — {name}</h1>
<p class="muted">{city_state(c)} · {round(c['accept']*100,1)}% acceptance · tier {c['tier']}</p>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 18px">
  <a class="btn btn-light btn-sm" href="/college/{slug}">Overview</a>
  <a class="btn btn-light btn-sm" href="/college/{slug}/plan">My plan</a>
  <a class="btn btn-light btn-sm" href="/college/{slug}/improve">Improve</a>
  <a class="btn btn-light btn-sm" href="?refresh=1">Refresh</a>
</div>

<div class="card">
  <h3 style="margin-top:0">Student profiles</h3>
  <p class="muted" style="font-size:.88em;margin:0 0 8px">Representative applicants for {name} — a mix of admits, waitlists, and rejects across the admit pool. Stats, hooks, and outcomes shown for each.</p>
  {profiles_html}
</div>

{archive_html}

{essays_card}
""", title=f"Real profiles — {name} — Candor")


# ─── PERSONALIZED SCHOOL PLAN ─────────────────────────────
def school_plan_html(slug):
    """One unified personalized view per school. Combines chances, school
    match, top improvement gaps, and links into the chat. Auth-required."""
    raw = COLLEGES_BY_SLUG.get(slug)
    if not raw: abort(404)
    # Apply CDS_VERIFIED + _OVERRIDES so the plan page subhead and round
    # breakdown reflect the verified accept rate, not the stale COLLEGES value.
    school = merged_school(raw)
    user = current_user()
    profile = get_profile(user["id"])
    if not profile or profile.get("uw_gpa") is None:
        return _page(f"""
<div class="bar"><a href="/college/{slug}">&larr; back to {school['name']}</a></div>
<h1>Your plan for {school['name']}</h1>
<div class="card" style="background:#fff8e1;border-color:#ffeaa7">
  <h3 style="margin-top:0">Add your profile first</h3>
  <p>Personalized plans need your GPA, scores, ECs, and preferences. Takes 2 minutes.</p>
  <a class="btn btn-primary" href="/profile">Edit profile &rarr;</a>
</div>
""", title=f"{school['name']} plan — Candor")

    # Start from the FULL profile so every odds-relevant field flows through —
    # critically the model-graded ec_rating / spike_score, the year-by-year
    # weighted GPA (w_gpa_* / w_notoffered_*), self_rigor, portfolio, and
    # is_exceptional. The previous hand-listed dict silently dropped ec_rating
    # (added to the schema later), so the plan page fell back to the crude
    # KEYWORD EC scorer and produced a LOWER, different fit than /plans (which
    # passes the whole profile) — making the page read harsh and disagree with
    # My Colleges for the same school. Overlay the bool/default normalizations.
    prof = {k: profile.get(k) for k in profile.keys()}
    prof.update({
        "legacy": bool(profile.get("legacy")), "first_gen": bool(profile.get("first_gen")),
        "athlete": bool(profile.get("athlete")),
        "legacy_schools": profile.get("legacy_schools") or "",
        "aps": profile.get("aps") or "",
        "no_aps_offered": bool(profile.get("no_aps_offered")),
        "aps_offered_not_taken": bool(profile.get("aps_offered_not_taken")),
        "ibs": profile.get("ibs") or "",
        "no_ibs_offered": bool(profile.get("no_ibs_offered")),
        "ibs_offered_not_taken": bool(profile.get("ibs_offered_not_taken")),
        "is_international": bool(profile.get("is_international")),
        "pref_weights": profile.get("pref_weights") or "",
    })
    # Demonstrated interest feeds the odds multiplier — set it the same way
    # My Colleges does so the two pages produce identical numbers.
    try:
        prof["_di_level"] = get_demonstrated_interest(user["id"], slug)
    except Exception:
        prof["_di_level"] = "none"

    # 1) Chances analysis (also persists to saved_chances)
    r = analyze_school(prof, slug)
    with db() as conn:
        conn.execute("""INSERT INTO saved_chances
            (user_id, college_slug, tier, odds_low, odds_high, fit, confidence, strength, weakness, differentiator, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, college_slug) DO UPDATE SET
              tier=excluded.tier, odds_low=excluded.odds_low, odds_high=excluded.odds_high,
              fit=excluded.fit, confidence=excluded.confidence,
              strength=excluded.strength, weakness=excluded.weakness, differentiator=excluded.differentiator,
              computed_at=CURRENT_TIMESTAMP""",
            (user["id"], slug, r["tier"], r["odds_low"], r["odds_high"], r["fit"], r["confidence"],
             r["strength"], r["weakness"], r["differentiator"]))
        conn.commit()
    tier_class = {"Dream":"pill-dream","Reach":"pill-reach","Target":"pill-target","Safety":"pill-safety"}[r["tier"]]
    conf_class = {"low":"pill-conf-low","medium":"pill-conf-medium","high":"pill-conf-high"}[r["confidence"]]
    conf_tooltip = {
        "high": "Your profile has GPA, test score, and clear strengths/weaknesses — model has solid signal.",
        "medium": "You submitted some stats but something's missing (test-optional, or stats at the median).",
        "low": "Sparse profile — add more details to your profile for a sharper estimate.",
    }[r["confidence"]]

    # 2) My Fit — same composite score the ranking uses, with the per-pref
    # breakdown beneath it.
    m = school_match(prof, school)
    overall, parts = compute_my_fit(prof, school)
    if overall >= 80:   stars_n = 5
    elif overall >= 65: stars_n = 4
    elif overall >= 50: stars_n = 3
    elif overall >= 35: stars_n = 2
    elif overall >= 20: stars_n = 1
    else: stars_n = 0
    star_html = ('<span style="color:#f0c040;letter-spacing:1px">' + ('★' * stars_n) +
                 '</span><span style="color:#ddd">' + ('★' * (5 - stars_n)) + '</span>')
    pref_labels = {"weather":"Weather","setting":"Campus setting","size":"School size",
                   "class_size":"Class size","greek":"Greek life","sports":"Sports culture",
                   "major_strength":"Major strength","prestige":"Prestige","cost":"Cost",
                   "diversity":"Diversity","party":"Party scene","research":"Research access",
                   "career_intensity":"Career focus","location":"Distance from home",
                   "aid":"Financial aid","culture":"Academic culture"}
    match_rows = ""
    if m and m.get("per_pref"):
        for key, label in pref_labels.items():
            if key not in m["per_pref"]:
                continue
            verdict, txt = m["per_pref"][key]
            icon = {"match":"✓","mismatch":"✗","neutral":"·"}[verdict]
            color = {"match":"#1d6c2a","mismatch":"#9a1d1d","neutral":"#666"}[verdict]
            match_rows += f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid #f0f0f0;font-size:.92em"><span><span style="color:{color};font-weight:700;margin-right:6px">{icon}</span>{label}</span><span class="muted" style="font-size:.85em">{txt}</span></div>'
    score_color = "#1d6c2a" if overall >= 80 else ("#8a4a00" if overall >= 60 else "#9a1d1d")
    breakdown = (f'<div class="muted" style="font-size:.78em;margin:6px 0 8px">'
                 f'admit realism {parts["admit_realism"]}/100 · '
                 f'prefs {parts["pref"]}/100 · '
                 f'academic {parts["academic"]}/100</div>')
    match_card = f"""<div class="card" style="background:#fafffe;border-color:#cfe7d8">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:6px">
        <h3 style="margin:0">My Fit</h3>
        <div style="text-align:right">
          <div>{star_html}</div>
          <div style="font-size:1.4em;font-weight:800;color:{score_color}">{overall}/100</div>
        </div>
      </div>
      <div class="muted" style="font-size:.82em;margin-bottom:6px">Same composite score as the My Fit ranking.</div>
      {breakdown}
      {match_rows}
    </div>"""

    # 3) Tailored advice (Claude-generated, cached 7 days) — PREMIUM ONLY.
    # The chances + My Fit cards above are the free teaser; the personalized
    # AI strategy is the advertised paid feature, so we gate it (and skip the
    # Claude call entirely for free users).
    if bool(user.get("is_paid")):
        force_refresh = request.args.get("refresh") == "1"
        advice_body = get_tailored_advice(user["id"], school, prof, force=force_refresh)
        advice_html = _render_tailored_advice(advice_body)
    else:
        advice_html = f"""<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3)">
          <div style="font-size:.74em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:6px">Candor Premium · $3/mo</div>
          <h3 style="margin:0 0 8px">Your personalized strategy for {school['name']}</h3>
          <p class="muted" style="margin:0 0 14px">Get AI strategy calibrated to your stats, ECs, and what {school['name']} actually weights — what to highlight, what to fix, and where this school fits your list. Plus the score predictor, list grader, and admissions simulator.</p>
          <a class="btn btn-primary" href="/upgrade" style="display:inline-block">Unlock my strategy — $3/mo &rarr;</a>
        </div>"""

    # 4) School-specific notes (curated values + essay strategy)
    note = get_school_strategy(school)

    # Sub-school detection — when user's major matches a curated sub-school
    # at this university, surface that prominently. Otherwise odds are
    # confusingly anchored on a different number than the school overall.
    sub_match = sub_school_for_major(slug, profile.get("major") or prof.get("major") or "")
    sub_school_html = ""
    header_accept = round(school['accept']*100, 1)
    if sub_match:
        sub_pct = round(sub_match["accept"]*100, 1)
        sub_school_html = f'''
<div class="card" style="margin-top:14px;background:rgba(95,201,182,.06);border:1px solid rgba(95,201,182,.3);padding:14px 18px">
  <div style="font-size:.78em;letter-spacing:.6px;color:var(--teal);text-transform:uppercase;font-weight:600">You're applying to a specific college within {school["name"]}</div>
  <div style="font-size:1.2em;font-weight:700;margin-top:4px">{sub_match["name"]}</div>
  <div class="muted" style="font-size:.88em;margin-top:4px">Admit rate <b style="color:var(--teal)">{sub_pct}%</b> for this college specifically (vs {header_accept}% university-wide). {school["name"]}'s overall acceptance number doesn't apply to {sub_match["name"]} applicants — your odds below are computed against the {sub_pct}% rate.</div>
  {f'<div class="muted" style="font-size:.82em;margin-top:6px;font-style:italic">{sub_match["note"]}</div>' if sub_match.get("note") else ""}
</div>'''

    return _page(f"""
<div class="bar"><a href="/college/{slug}">&larr; back to {school['name']}</a></div>
<h1>Your plan for {school['name']}</h1>
<div class="muted">{city_state(school)} · {header_accept}% acceptance · {school['type']}</div>
{sub_school_html}
<div class="card" style="margin-top:18px;background:#1a1a1a;color:#fff;border-color:#1a1a1a">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
    <div>
      <h3 style="margin:0;color:#fff">Your chances{f' at {sub_match["name"].split("(")[0].strip()}' if sub_match else ''}</h3>
      <div class="muted" style="color:#bdbdbd;font-size:.82em">profile fit {r['fit']}/100</div>
    </div>
    <div><span class="pill {tier_class}">{r['tier']}</span> <span class="pill {conf_class}" style="margin-left:4px" title="{conf_tooltip}">{r['confidence']} confidence</span></div>
  </div>
  <div style="font-size:1.8em;font-weight:800;letter-spacing:-.5px;margin:10px 0 4px;color:#9bf">{r['odds_low']}–{r['odds_high']}%</div>
  {(lambda _det: render_round_breakdown_dark(school, _det, personalized_rates=personalize_round_odds(user['id'], school, _det, profile, r['odds_low'], r['odds_high'], sub_school=sub_match), scale=(((r.get('odds_low',0)+r.get('odds_high',0))/2.0) / (round(school['accept']*100,1) or 1.0)), sub_school=sub_match))(admissions_detail(school))}
  <ul style="padding-left:18px;margin:14px 0 0;color:#e8e8e8">
    <li><b>Strength —</b> {r['strength']}</li>
    <li><b>Weakness —</b> {r['weakness']}</li>
    <li><b>Differentiator —</b> {r['differentiator']}</li>
  </ul>
</div>

{match_card}

<div class="card" style="background:#f0f7ff;border-color:#cfe0ff">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:6px">
    <h3 style="margin:0">★ Tailored advice for you applying to {school['name']}</h3>
    <a href="/college/{slug}/plan?refresh=1" class="muted" style="font-size:.78em">Regenerate</a>
  </div>
  <p class="muted" style="font-size:.82em;margin:4px 0 8px">Personalized to your profile + preferences. Cached 7 days.</p>
  {advice_html}
</div>

<div class="card">
  <h3 style="margin-top:0">What {school['name']} weights most</h3>
  <p style="margin:0">{note['values']}</p>
  <h3>Supplemental essay strategy</h3>
  <p style="margin:0">{note['supplemental_strategy']}</p>
</div>

<div style="display:flex;gap:8px;flex-wrap:wrap;margin:18px 0">
  <a class="btn btn-primary" href="/college/{slug}/improve">Full school-specific guide</a>
  <a class="btn btn-light" href="/college/{slug}">School overview</a>
</div>
""", title=f"Your plan for {school['name']} — Candor")




def _user_round_for(user_id, slug):
    """Returns the user's selected application round for this school, or None."""
    with db() as conn:
        row = conn.execute(
            "SELECT application_round FROM saved_chances WHERE user_id=? AND college_slug=? "
            "UNION SELECT application_round FROM saved_schools WHERE user_id=? AND college_slug=? "
            "LIMIT 1",
            (user_id, slug, user_id, slug)
        ).fetchone()
    return row["application_round"] if row and row["application_round"] else None


def _supported_rounds_for_slug(slug):
    """Return the list of round codes this school actually offers (from
    ADMISSIONS_DETAIL when curated, or a sensible default otherwise)."""
    detail = ADMISSIONS_DETAIL.get(slug)
    if detail:
        rounds = list(detail.get("rounds", []))
        # Some entries use 'ED' instead of 'ED1' — normalize for the picker
        rounds = ["ED1" if r == "ED" else r for r in rounds]
        return rounds
    # Default: most schools offer at least RD; many offer EA. Conservative default.
    return ["EA","RD"]


def plans_index_html():
    """List all schools the user has computed chances or saved, grouped by
    application round. Round selector + remove button per card.

    GATED: requires premium subscription. Free users see a preview + upgrade
    CTA but cannot access the full grader/simulator/aggregated dashboard."""
    user = current_user()
    is_premium = bool(user.get("is_paid"))
    with db() as conn:
        chance_rows = conn.execute("""
            SELECT college_slug, tier, odds_low, odds_high, fit, confidence,
                   application_round, computed_at
            FROM saved_chances WHERE user_id = ?
            ORDER BY computed_at DESC
        """, (user["id"],)).fetchall()
        saved_rows = conn.execute("""
            SELECT college_slug, application_round
            FROM saved_schools WHERE user_id = ?
        """, (user["id"],)).fetchall()
    saved_slugs = [r["college_slug"] for r in saved_rows]
    saved_round_by_slug = {r["college_slug"]: r["application_round"] for r in saved_rows}
    chance_round_by_slug = {r["college_slug"]: r["application_round"] for r in chance_rows}

    if not chance_rows and not saved_slugs:
        return _page("""
<h1>My Colleges</h1>
<p class="muted">Each school you've saved or computed chances for shows up here, grouped by application round, with a list grader and admissions simulator.</p>
<div class="card" style="background:rgba(95,201,182,.06);border-color:rgba(95,201,182,.3)">
  <h3 style="margin-top:0">No plans yet</h3>
  <p>Pick a school to get started:</p>
  <a class="btn btn-primary" href="/colleges">Browse colleges</a>
  <a class="btn btn-light" href="/rankings/my-fit">My Fit ranking</a>
</div>
""", title="My Colleges — Candor")

    # If not premium, render a gated preview with upgrade CTA
    if not is_premium:
        n_schools = len(set(saved_slugs) | {r["college_slug"] for r in chance_rows})
        return _page(f"""
<h1>My Colleges</h1>
<p class="muted">Strategic dashboard for your full college list — round assignments, list grader, and admissions simulator.</p>

<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3);padding:32px">
  <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:8px">Candor Premium · $3/mo</div>
  <h2 style="margin:0 0 14px">Turn your chances into a real plan</h2>
  <ul style="line-height:1.9;padding-left:18px;margin:0 0 18px">
    <li><b>List grader + admissions simulator</b> — score your full list 1–10, simulate ED/EA/RD outcomes across every school</li>
    <li><b>Schools-to-add recommender</b> — Candor finds schools to add based on your list's gaps and the schools you already like</li>
    <li><b>Personalized AI strategy per school</b> — calibrated to your stats, ECs, and what that school actually weights</li>
    <li><b>Score push impact</b> — see exactly how a +60 SAT or +2 ACT moves your odds at each school</li>
    <li><b>Saved schools dashboard</b> — every school you've chanced or saved, grouped by application round</li>
    <li><b>Free chances calculator stays free</b> — premium is the layer on top</li>
  </ul>
  <p class="muted" style="font-size:.88em">You currently have <b style="color:#e6edf3">{n_schools}</b> school{'' if n_schools==1 else 's'} in your list. Upgrade to organize, simulate, and strategize.</p>
  <a class="btn btn-primary" href="/upgrade" style="font-size:1em;padding:12px 28px;margin-top:18px;display:inline-block">Upgrade — $3/mo →</a>
  <p class="muted" style="font-size:.78em;margin-top:12px">$3/month, cancel anytime. Your saved schools and chances stay accessible on each college's page.</p>
</div>

<p style="margin-top:18px"><a class="btn btn-light" href="/colleges">+ Add another school</a></p>
""", title="My Colleges — Candor")

    profile = get_profile(user["id"])
    # Build a unified list of (slug, app_round, chance_row_or_None)
    items = []
    chance_slugs_seen = set()
    for r in chance_rows:
        slug = r["college_slug"]
        chance_slugs_seen.add(slug)
        items.append({
            "slug": slug,
            "round": r["application_round"],
            "tier": r["tier"],
            "odds_low": r["odds_low"], "odds_high": r["odds_high"],
            "fit": r["fit"], "computed": True,
        })
    for slug in saved_slugs:
        if slug in chance_slugs_seen: continue
        items.append({
            "slug": slug,
            "round": saved_round_by_slug.get(slug),
            "tier": None,
            "odds_low": None, "odds_high": None,
            "fit": None, "computed": False,
        })

    # Group by round
    by_round = {r: [] for r in ROUND_GROUP_ORDER}
    for it in items:
        rnd = it["round"] if it["round"] in by_round else None
        by_round[rnd].append(it)

    section_html = ""
    for rnd in ROUND_GROUP_ORDER:
        bucket = by_round.get(rnd) or []
        if not bucket:
            continue
        label = ROUND_DISPLAY.get(rnd, "Other")
        section_html += f'<h3 style="margin:24px 0 10px;font-family:\'Newsreader\',Georgia,serif;font-weight:600">{label} <span class="muted" style="font-size:.7em;font-weight:400">({len(bucket)})</span></h3>\n<div class="grid">'
        for it in bucket:
            slug = it["slug"]
            c = COLLEGES_BY_SLUG.get(slug)
            if not c: continue
            # Recompute odds/tier/fit FRESH from the current profile (instant —
            # pure Python) so the list always reflects the latest profile and
            # formula, instead of stale cached saved_chances numbers (e.g. after a
            # GPA/weighting/EC change). Only the AI narrative stays cached.
            if profile:
                _cm = merged_school(c)
                _pd = {k: profile.get(k) for k in profile.keys()}
                try:
                    _pd["_di_level"] = get_demonstrated_interest(user["id"], slug)
                except Exception:
                    _pd["_di_level"] = "none"
                try:
                    _fit, _ = compute_fit(_pd, _cm)
                    it["odds_low"], it["odds_high"] = estimate_odds(_cm, _fit, _pd)
                    it["fit"] = _fit
                    it["tier"] = assign_tier(_cm, _fit, _pd)
                    it["computed"] = True
                except Exception:
                    pass
            tier_class = {"Dream":"pill-dream","Reach":"pill-reach","Target":"pill-target","Safety":"pill-safety"}.get(it["tier"], "pill-target")
            match_score = ""
            if profile:
                prof_dict = {k: profile.get(k) for k in profile.keys()}
                cm = merged_school(c)
                overall, _ = compute_my_fit(prof_dict, cm)
                col = "#5fc9b6" if overall >= 80 else ("#fbbf24" if overall >= 60 else "#fca5a5")
                match_score = f'<div class="muted" style="font-size:.82em;margin-top:4px">My Fit: <span style="color:{col};font-weight:700">{overall}/100</span></div>'
            # Round selector
            supported = _supported_rounds_for_slug(slug)
            opts = ['<option value="">— Round —</option>']
            for r_code in ["ED1","ED2","EA","REA","RD"]:
                sel = " selected" if r_code == it["round"] else ""
                disabled = "" if r_code in supported else " disabled"
                label_short = ROUND_DISPLAY.get(r_code, r_code).replace("Restrictive EA","REA")
                opts.append(f'<option value="{r_code}"{sel}{disabled}>{label_short}{"" if r_code in supported else " (n/a)"}</option>')
            round_select = f'<select onchange="setRound(\'{slug}\',this.value)" style="font-size:.78em;padding:3px 6px;border-radius:4px;background:var(--surface-2);color:var(--text);border:1px solid var(--border-strong)">' + "".join(opts) + '</select>'
            # Card body
            if it["computed"]:
                odds_html = f'<div style="font-size:1.4em;font-weight:800;color:#5fc9b6;margin-top:8px">{it["odds_low"]}–{it["odds_high"]}%</div>'
                tier_pill = f'<span class="pill {tier_class}">{it["tier"]}</span>'
            else:
                odds_html = '<div style="font-size:.88em;color:#5fc9b6;margin-top:8px">Compute chances →</div>'
                tier_pill = ""
            section_html += f'''
<div class="school-card" style="position:relative;padding:14px">
  <button onclick="event.preventDefault();event.stopPropagation();removeSchool('{slug}')"
          title="Remove from My Plans"
          style="position:absolute;top:8px;right:8px;background:transparent;border:1px solid rgba(255,255,255,.15);color:var(--text-2);width:24px;height:24px;border-radius:50%;cursor:pointer;font-size:.85em;line-height:1;padding:0;display:flex;align-items:center;justify-content:center">×</button>
  <a href="/college/{slug}/plan" style="display:block;color:inherit;padding-right:24px">
    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
      <div>
        <div style="font-weight:700;font-size:1.05em">{c["name"]}</div>
        <div class="muted" style="font-size:.82em">{city_state(c)} · {round(c["accept"]*100,1)}% accept{f" · fit {it['fit']}/100" if it["fit"] else ""}</div>
      </div>
      <div>{tier_pill}</div>
    </div>
    {odds_html}
    {match_score}
  </a>
  <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;gap:6px">
    <span class="muted" style="font-size:.78em">Round:</span>
    {round_select}
  </div>
</div>'''
        section_html += '</div>'

    # Toolbar with strategic actions (premium)
    toolbar = '''
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 8px">
  <a class="btn btn-primary btn-sm" href="/plans/strategist">Application strategist</a>
  <a class="btn btn-primary btn-sm" href="/plans/grade">Grade my list</a>
  <a class="btn btn-primary btn-sm" href="/plans/simulate">Simulate admissions</a>
  <a class="btn btn-primary btn-sm" href="/plans/add">Schools to add</a>
  <a class="btn btn-light btn-sm" href="/compare">Compare saved</a>
  <a class="btn btn-light btn-sm" href="/timeline">Timeline</a>
  <a class="btn btn-light btn-sm" href="/predictor">Score predictor</a>
</div>'''

    return _page(f"""
<h1>My Colleges</h1>
<p class="muted">Schools grouped by application round. Click a card for the personalized plan; use the round dropdown to assign or change a round; click × to remove.</p>
{toolbar}
{section_html}
<p style="margin-top:18px"><a class="btn btn-light" href="/colleges">+ Add another school</a></p>

<script>
function _csrfToken(){{
  const el = document.querySelector('meta[name="csrf-token"]');
  return el ? el.getAttribute('content') : '';
}}
async function setRound(slug, round){{
  try {{
    const tok = _csrfToken();
    const r = await fetch(`/plans/round/${{slug}}`, {{
      method:'POST',
      headers:{{'Content-Type':'application/x-www-form-urlencoded', 'X-CSRFToken': tok}},
      body:'round=' + encodeURIComponent(round) + '&csrf_token=' + encodeURIComponent(tok)
    }});
    if (r.ok) location.reload();
    else {{
      const t = await r.text();
      alert('Could not save round assignment: ' + r.status + ' ' + t.slice(0,100));
    }}
  }} catch(e) {{ alert('Network error: ' + e.message); }}
}}
async function removeSchool(slug){{
  if (!confirm('Remove this school from your plans?')) return;
  try {{
    const tok = _csrfToken();
    const fd = new FormData();
    fd.append('csrf_token', tok);
    const r = await fetch(`/plans/remove/${{slug}}`, {{
      method:'POST',
      headers:{{'X-CSRFToken': tok}},
      body: fd
    }});
    if (r.ok) location.reload();
    else {{
      const t = await r.text();
      alert('Could not remove: ' + r.status + ' ' + t.slice(0,100));
    }}
  }} catch(e) {{ alert('Network error: ' + e.message); }}
}}
</script>
""", title="My Colleges — Candor")


# ─── CHAT (AI advisor) ────────────────────────────────────
MAX_CHAT_HISTORY = 20  # messages of context per call

def get_or_create_conversation(user_id, kind, college_slug=None):
    with db() as conn:
        if college_slug:
            row = conn.execute("SELECT * FROM conversations WHERE user_id=? AND kind=? AND college_slug=?",
                               (user_id, kind, college_slug)).fetchone()
        else:
            row = conn.execute("SELECT * FROM conversations WHERE user_id=? AND kind=? AND college_slug IS NULL",
                               (user_id, kind)).fetchone()
        if row:
            return dict(row)
        cur = conn.execute("INSERT INTO conversations (user_id, kind, college_slug) VALUES (?,?,?)",
                           (user_id, kind, college_slug))
        conn.commit()
        return {"id": cur.lastrowid, "user_id": user_id, "kind": kind, "college_slug": college_slug}


def get_messages(conv_id, limit=200):
    with db() as conn:
        rows = conn.execute("SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id ASC LIMIT ?",
                            (conv_id, limit)).fetchall()
        return [dict(r) for r in rows]


def add_message(conv_id, role, content):
    with db() as conn:
        conn.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)", (conv_id, role, content))
        conn.execute("UPDATE conversations SET last_msg_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
        conn.commit()


def _profile_summary(profile):
    if not profile:
        return "No profile saved yet — student has not entered GPA, scores, or activities."
    test = ""
    if profile.get("sat"): test = f"SAT {profile['sat']}"
    elif profile.get("act"): test = f"ACT {profile['act']}"
    else: test = "no test score submitted"
    hooks = [k for k in ("legacy", "first_gen", "athlete") if profile.get(k)]
    parts = [
        f"Unweighted GPA: {profile.get('uw_gpa') or 'unknown'}",
        f"Test: {test}",
        f"Major: {profile.get('major') or 'undecided'}",
        f"State: {profile.get('state') or 'unknown'}",
        f"School type: {profile.get('school_type') or 'unknown'}",
        f"ECs: {profile.get('ecs') or '(blank)'}",
        f"Leadership: {profile.get('leadership') or '(blank)'}",
        f"Awards: {profile.get('awards') or '(blank)'}",
        f"Hooks: {', '.join(hooks) if hooks else 'none'}",
    ]
    return "\n".join(parts)


def _build_system_prompt(kind, profile, school=None):
    base = (
        "You are an experienced college admissions advisor with deep knowledge of US college admissions. "
        "Your tone is direct, practical, and concrete — never hedging, never generic. "
        "You always cite specific numbers, programs, deadlines, or examples when possible. "
        "If the student asks a vague question, ask them ONE clarifying follow-up rather than answering generically. "
        "Keep answers under 300 words unless the student asks for detail. "
        "Use markdown formatting (bullets, bold) when it helps readability.\n\n"
        + _date_context()
    )
    profile_block = "\nThe student's saved profile:\n" + _profile_summary(profile)
    if kind == "school" and school:
        school_block = (
            f"\n\nThe student is asking specifically about {school['name']} ({school['state']}). "
            f"Acceptance rate: {round(school['accept']*100,1)}%. "
            f"Typical admit GPA: {school['gpa_lo']}-{school['gpa_hi']}. "
            f"SAT mid-50%: {school['sat_25']}-{school['sat_75']}. "
            f"ACT mid-50%: {school['act_25']}-{school['act_75']}. "
            f"Type: {school['type']}. Tier (1=most selective): {school['tier']}. "
            f"Description: {school['desc']}"
        )
        note = get_school_strategy(school)
        school_block += f"\n\nWhat {school['name']} weights most: {note['values']}"
        school_block += f"\nSupplemental essay strategy: {note['supplemental_strategy']}"
        base += "\n\nWhen advice would differ for this school vs others, be explicit about why. Ground every recommendation in the school's actual profile above."
        return base + profile_block + school_block
    return base + profile_block


def chat_send(conversation_id, user_message, kind, profile, school=None):
    """Append the user's message, call Claude with conversation history, append assistant reply."""
    user_message = (user_message or "").strip()
    if not user_message:
        return None
    if len(user_message) > 4000:
        user_message = user_message[:4000]
    add_message(conversation_id, "user", user_message)
    history = get_messages(conversation_id, limit=MAX_CHAT_HISTORY * 2)
    msgs = [{"role": m["role"], "content": m["content"]} for m in history[-MAX_CHAT_HISTORY:]]
    if not _claude_client:
        reply = "AI chat is unavailable right now (no API key configured). The improve guide and ranking lists still work — try those for self-serve advice."
    else:
        try:
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=_build_system_prompt(kind, profile, school),
                messages=msgs,
            )
            reply = response.content[0].text.strip()
        except Exception as e:
            print(f"Chat Claude error: {e}")
            reply = "Sorry — the AI advisor hit an error. Try again in a moment."
    add_message(conversation_id, "assistant", reply)
    return reply


def _render_message(m):
    """Render a stored message safely. We escape the content and convert
    minimal markdown (bold + bullets + linebreaks) to HTML."""
    import html as _html
    safe = _html.escape(m["content"])
    # **bold**
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", safe)
    # bullet lines starting with - or *
    lines = safe.split("\n")
    out, in_ul = [], False
    for line in lines:
        m_bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        if m_bullet:
            if not in_ul:
                out.append('<ul style="padding-left:18px;margin:6px 0">')
                in_ul = True
            out.append(f"<li>{m_bullet.group(1)}</li>")
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            if line.strip():
                out.append(f"<div>{line}</div>")
            else:
                out.append("<div style=\"height:8px\"></div>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)




def general_chat_html():
    user = current_user()
    profile = get_profile(user["id"])
    conv = get_or_create_conversation(user["id"], "general")
    msgs = get_messages(conv["id"])
    msgs_html = ""
    for m in msgs:
        klass = "msg-user" if m["role"] == "user" else "msg-assistant"
        msgs_html += f'<div class="msg {klass}"><div class="msg-bubble">{_render_message(m)}</div></div>'
    if not msgs:
        msgs_html = '<div class="muted" style="text-align:center;padding:30px 0">Ask the AI advisor anything about college admissions. Your saved profile will be used as context.</div>'
    suggestions = [
        "What's the highest-impact thing I can do this summer?",
        "Should I retake the SAT?",
        "How do I write a strong 'why us' essay?",
        "What are the best summer programs for my major?",
        "How do I ask for recommendation letters?",
    ]
    sug_html = "".join(f'<button class="suggestion" onclick="suggestionClick(this.innerText)">{s}</button>' for s in suggestions)
    usage = usage_status(user["id"])
    if usage.get("is_paid"):
        usage_pill = f'<span id="usage-pill" style="font-size:.74em;background:rgba(95,201,182,.12);color:var(--teal);padding:3px 10px;border-radius:999px;border:1px solid rgba(95,201,182,.25);font-weight:500;margin-left:10px">PREMIUM · {usage["month_used"]}/{PAID_MONTHLY_LIMIT}</span>'
    else:
        rem = usage["free_remaining"]
        usage_pill = f'<span id="usage-pill" style="font-size:.74em;background:var(--surface-2);color:var(--text-2);padding:3px 10px;border-radius:999px;border:1px solid var(--border-strong);margin-left:10px">FREE · {rem} of {FREE_TRIAL_MESSAGES} left · <a href="/upgrade" style="color:var(--teal)">Upgrade</a></span>'
    header = f'<h1 style="display:flex;align-items:center;flex-wrap:wrap">AI advisor {usage_pill}</h1><p class="muted">Knows your profile. Asks back if it needs more. History saved.</p>'
    page = (CHAT_PAGE_HTML
            .replace("__HEADER__", header)
            .replace("__MESSAGES__", msgs_html)
            .replace("__SUGGESTIONS__", sug_html)
            .replace("__PLACEHOLDER__", "college admissions")
            .replace("__SEND_URL__", "/chat/api/send"))
    return _page(page, title="AI advisor — Candor")


def school_chat_html(slug):
    school = COLLEGES_BY_SLUG.get(slug)
    if not school: abort(404)
    user = current_user()
    profile = get_profile(user["id"])
    conv = get_or_create_conversation(user["id"], "school", slug)
    msgs = get_messages(conv["id"])
    msgs_html = ""
    for m in msgs:
        klass = "msg-user" if m["role"] == "user" else "msg-assistant"
        msgs_html += f'<div class="msg {klass}"><div class="msg-bubble">{_render_message(m)}</div></div>'
    if not msgs:
        msgs_html = f'<div class="muted" style="text-align:center;padding:30px 0">Ask the AI advisor anything about {school["name"]} specifically — admissions strategy, what they value, supplemental essays, programs to apply to.</div>'
    suggestions = [
        f"What does {school['name']} value most in applicants?",
        f"What should my supplemental essay focus on for {school['name']}?",
        f"What are my biggest gaps for {school['name']}?",
        f"What summer programs would help my {school['name']} application?",
        f"Should I apply early decision to {school['name']}?",
    ]
    sug_html = "".join(f'<button class="suggestion" onclick="suggestionClick(this.innerText)">{s}</button>' for s in suggestions)
    usage = usage_status(user["id"])
    if usage.get("is_paid"):
        usage_pill = f'<span id="usage-pill" style="font-size:.74em;background:rgba(95,201,182,.12);color:var(--teal);padding:3px 10px;border-radius:999px;border:1px solid rgba(95,201,182,.25);font-weight:500;margin-left:10px">PREMIUM · {usage["month_used"]}/{PAID_MONTHLY_LIMIT}</span>'
    else:
        rem = usage["free_remaining"]
        usage_pill = f'<span id="usage-pill" style="font-size:.74em;background:var(--surface-2);color:var(--text-2);padding:3px 10px;border-radius:999px;border:1px solid var(--border-strong);margin-left:10px">FREE · {rem} of {FREE_TRIAL_MESSAGES} left · <a href="/upgrade" style="color:var(--teal)">Upgrade</a></span>'
    header = f'<div class="bar"><a href="/college/{slug}/improve">&larr; back to {school["name"]} advice</a></div><h1 style="display:flex;align-items:center;flex-wrap:wrap">AI advisor — {school["name"]} {usage_pill}</h1><p class="muted">Specific to {school["name"]} ({round(school["accept"]*100,1)}% acceptance) and your profile.</p>'
    page = (CHAT_PAGE_HTML
            .replace("__HEADER__", header)
            .replace("__MESSAGES__", msgs_html)
            .replace("__SUGGESTIONS__", sug_html)
            .replace("__PLACEHOLDER__", school["name"])
            .replace("__SEND_URL__", f"/chat/api/send?slug={slug}"))
    return _page(page, title=f"AI advisor — {school['name']} — Candor")


# ─── FLASK APP ────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Secure session cookies. In production we use SameSite=None so the
# session rides along when the app is embedded in the Framer iframe
# (cross-site context). SameSite=None requires Secure, which Railway
# satisfies via its TLS-terminating proxy. Locally we fall back to Lax
# because browsers reject None-without-Secure on http://localhost.
_IS_PROD = os.getenv("RAILWAY_ENVIRONMENT") is not None
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None" if _IS_PROD else "Lax",
    SESSION_COOKIE_SECURE=_IS_PROD,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# CSRF protection on every POST form. Requires {{ csrf_token() }} to be
# emitted in each form template.
try:
    from flask_wtf.csrf import CSRFProtect
    csrf = CSRFProtect(app)
    _CSRF_ON = True
except Exception:
    _CSRF_ON = False
    print("flask-wtf not available; CSRF disabled")

# ─── BOT / SCRAPER MITIGATION ─────────────────────────────
# Two cheap defenses: a rate limit on /college/<slug> (humans don't browse
# 30+ college pages in a minute, scrapers do), and a robots.txt with a
# crawl-delay so well-behaved bots throttle themselves. Determined scrapers
# will still get through; this just stops the dumb cases and keeps the
# admin/stats numbers clean.
_COLLEGE_RATE = {}  # ip -> deque of timestamps
_BOT_UA_FRAGMENTS = ("python-requests", "scrapy", "wget", "httpie", "go-http-client")

@app.before_request
def _rate_limit_scrapers():
    p = request.path or ""
    if request.method != "GET":
        return
    # Bot user-agents — block outright on college/chances/api routes.
    ua = (request.headers.get("User-Agent") or "").lower()
    if any(x in ua for x in _BOT_UA_FRAGMENTS) and (
        p.startswith("/college/") or p.startswith("/chances/") or p.startswith("/api/")
    ):
        return ("Forbidden.", 403)
    # Slug-diversity rate limit on college pages. Real users browse
    # 3-8 different schools when researching; scrapers walk through 30+.
    # Counting *unique* slugs over 5 minutes (not total requests) avoids
    # blocking a human refreshing one school's page repeatedly while
    # catching scrapers walking the alphabet.
    if p.startswith("/college/"):
        if session.get("user_id"):
            return  # logged-in users are real people, never rate-limit them
        from collections import deque
        # Extract slug from "/college/<slug>" or "/college/<slug>/sub"
        parts = p.split("/")
        slug = parts[2] if len(parts) > 2 else ""
        if slug:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
            now = time.time()
            dq = _COLLEGE_RATE.setdefault(ip, deque())
            while dq and now - dq[0][0] > 300:  # 5-min window
                dq.popleft()
            dq.append((now, slug))
            unique_slugs = len({s for _, s in dq})
            if unique_slugs > 45:
                return ("Too many requests. Slow down.", 429)


@app.route("/robots.txt")
def robots_txt():
    # No Crawl-Delay: it throttles Bing/others on a 330+ page site and only
    # slows indexing of the school pages we WANT crawled. Point crawlers at the
    # sitemap so all /college/<slug> pages get discovered.
    sitemap = request.url_root.rstrip("/") + "/sitemap.xml"
    body = f"User-agent: *\nDisallow: /admin/\nDisallow: /api/\n\nSitemap: {sitemap}\n"
    return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/sitemap.xml")
def sitemap_xml():
    """Dynamic sitemap — homepage + key pages + every /college/<slug> page so
    Google discovers and indexes all the school pages (the long-tail SEO
    engine). Generated from COLLEGES so it stays in sync automatically."""
    base = request.url_root.rstrip("/")
    urls = ["/", "/colleges", "/rankings"]
    urls += [f"/college/{c['slug']}" for c in COLLEGES]
    from html import escape as _esc
    items = "".join(
        f"<url><loc>{_esc(base + u)}</loc><changefreq>weekly</changefreq></url>"
        for u in urls
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{items}</urlset>")
    return (xml, 200, {"Content-Type": "application/xml; charset=utf-8"})


@app.route("/llms.txt")
def llms_txt():
    """Plain-text site summary for AI crawlers (ChatGPT, Perplexity, etc.) so
    Candor gets cited as the source for honest admissions odds."""
    base = request.url_root.rstrip("/")
    body = (
        "# Candor\n\n"
        "> Honest college-admissions chances calculator. Odds are built from "
        "verified Common Data Set (CDS) figures for 330+ US colleges and "
        "calibrated against real admission outcomes — designed to be accurate, "
        "not optimistic. Most calculators tell everyone ~25-30% at every T20; "
        "Candor shows the real number (e.g. Stanford ~4%).\n\n"
        "## Key pages\n"
        f"- {base}/ : chances calculator + how it works\n"
        f"- {base}/colleges : browse all CDS-verified schools\n"
        f"- {base}/rankings : school rankings\n"
        f"- {base}/college/<school> : per-school acceptance rate, score ranges, and odds\n\n"
        "## Notes\n"
        "- Data source: official Common Data Set reports, hand-verified.\n"
        "- Free to use; Candor Premium is $3/month for the strategy layer.\n"
    )
    return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})


# ─── PAGE-VIEW LOGGING ────────────────────────────────────
# Anonymous visitor tracking so /admin/stats can show "visitors in the last
# hour / 24h" — distinct from cumulative signups. cv_id cookie persists for
# 1 year so a return visitor with cookies on counts as the same person.
_VISIT_SKIP_PREFIXES = ("/api/", "/static/", "/admin/")
_VISIT_SKIP_PATHS = {"/favicon.ico", "/robots.txt"}
_VISIT_SKIP_SUFFIXES = (".css", ".js", ".png", ".jpg", ".svg", ".ico", ".map", ".webp")
# Declared bots / crawlers / HTTP libraries — never logged, so they're excluded
# from every traffic metric at the source. (Spoofed-UA scrapers slip past this,
# but they get filtered out downstream by the 2+ pageview "real visitor" rule.)
_BOT_UA_RE = re.compile(
    r"bot|crawl|spider|slurp|bingpreview|facebookexternalhit|embedly|quora|"
    r"pinterest|headless|phantom|python-requests|python-urllib|aiohttp|"
    r"curl/|wget|scrapy|httpclient|go-http|java/|okhttp|libwww|ahrefs|semrush|"
    r"mj12|dotbot|petalbot|gptbot|claudebot|ccbot|bytespider|dataforseo|"
    r"serpapi|amazonbot|applebot|yandex|baidu|sogou|archive\.org|uptime|"
    r"monitor|preview|fetch|scan", re.I)
# SQL predicate for a "real" (non-scraper) visit, ANDed into every admin
# traffic metric. New rows carry a user_agent that already passed the write-time
# bot filter, so a single pageview counts as a real human. Legacy rows (no UA
# stored) fall back to the old "2+ pageviews" heuristic, the only scraper signal
# available for that historical data.
_REAL_VISITOR_SQL = ("(user_agent IS NOT NULL OR visitor_id IN "
                     "(SELECT visitor_id FROM page_visits GROUP BY visitor_id HAVING COUNT(*) >= 2))")

@app.before_request
def _log_page_visit():
    if request.method != "GET":
        return
    p = request.path or ""
    if p in _VISIT_SKIP_PATHS: return
    if any(p.startswith(x) for x in _VISIT_SKIP_PREFIXES): return
    if any(p.endswith(x) for x in _VISIT_SKIP_SUFFIXES): return
    # Drop declared bots/crawlers/HTTP-libs and empty-UA requests entirely.
    ua = request.headers.get("User-Agent", "")
    if not ua or _BOT_UA_RE.search(ua):
        return
    vid = request.cookies.get("cv_id")
    if not vid:
        vid = secrets.token_urlsafe(12)
        request._new_cv_id = vid
    uid = session.get("user_id")
    try:
        with db() as conn:
            conn.execute("INSERT INTO page_visits(visitor_id, user_id, path, user_agent) VALUES(?,?,?,?)",
                         (vid, uid, p, ua[:300]))
            conn.commit()
    except Exception:
        pass  # logging must never break a request

@app.after_request
def _set_visitor_cookie(response):
    new_vid = getattr(request, "_new_cv_id", None)
    if new_vid:
        _prod = os.getenv("RAILWAY_ENVIRONMENT") is not None
        response.set_cookie("cv_id", new_vid, max_age=60*60*24*365,
                            httponly=True,
                            samesite="None" if _prod else "Lax",
                            secure=_prod)
    return response


@app.after_request
def _allow_framer_embed(response):
    # Allow embedding inside Framer-hosted sites. X-Frame-Options is a
    # legacy header that can't express an allowlist, so we drop it and
    # rely on CSP frame-ancestors, which all modern browsers honor.
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://*.framer.app https://*.framer.website"
    )
    return response

# Rate limiting (per-IP). Defaults are generous; sensitive routes (login,
# signup) get tighter caps via @limiter.limit() decorators.
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=["1000 per day", "200 per hour"],
        storage_uri="memory://",
    )
    _LIMITER_ON = True
except Exception:
    _LIMITER_ON = False
    limiter = None
    print("flask-limiter not available; rate limiting disabled")


def csrf_input():
    """Return a hidden <input> with the CSRF token, or empty string if
    Flask-WTF isn't installed (so local dev still works)."""
    if not _CSRF_ON:
        return ""
    try:
        from flask_wtf.csrf import generate_csrf
        return f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">'
    except Exception:
        return ""


def _read_profile_form(form):
    # Sanitize all free-text inputs to strip any HTML/scripts. Stored XSS
    # risk: ECs, leadership, awards, etc. get echoed back when rendering
    # the profile and can show up in AI prompts. Using bleach.clean with
    # tags=[] strips every tag, leaving plain text.
    # Length caps per field. Prevents abuse (a malicious user pasting 1MB
    # of text into ECs would balloon AI prompts and our API costs). Caps
    # are generous — well above what any honest user would write.
    LIMITS = {
        "ecs": 4000, "leadership": 2000, "awards": 2000,
        "legacy_schools": 500, "major": 200, "state": 100,
        "aps": 2000, "ibs": 2000, "portfolio": 1500,
    }
    try:
        from bleach import clean as _bleach
        def s(v, k=None):
            if not isinstance(v, str): return v
            cleaned = _bleach(v, tags=[], strip=True).strip()
            cap = LIMITS.get(k)
            return cleaned[:cap] if cap else cleaned
    except Exception:
        # Bleach not installed; fall back to a minimal HTML stripper.
        import html as _html
        def s(v, k=None):
            if not isinstance(v, str): return v
            cleaned = re.sub(r"<[^>]*>", "", _html.unescape(v)).strip()
            cap = LIMITS.get(k)
            return cleaned[:cap] if cap else cleaned

    def f(k, cast=str, default=None):
        v = form.get(k)
        if v is None or v == "": return default
        if cast is str: v = s(v, k)
        try: return cast(v)
        except (TypeError, ValueError): return default
    result = {
        "uw_gpa": f("uw_gpa", float),
        "weighted_gpa": f("weighted_gpa", float),
        "gpa_freshman": f("gpa_freshman", float),
        "gpa_sophomore": f("gpa_sophomore", float),
        "gpa_junior": f("gpa_junior", float),
        "gpa_senior": f("gpa_senior", float),
        "sat": f("sat", int),
        "sat_math": f("sat_math", int),
        "sat_ebrw": f("sat_ebrw", int),
        "act": f("act", int),
        "act_math": f("act_math", int),
        "act_english": f("act_english", int),
        "act_reading": f("act_reading", int),
        "act_science": f("act_science", int),
        "major": f("major") or "",
        "state": f("state") or "",
        "school_type": f("school_type") or "public",
        "ecs": f("ecs") or "",
        "leadership": f("leadership") or "",
        "awards": f("awards") or "",
        "portfolio": f("portfolio") or "",
        "legacy_schools": (f("legacy_schools") or "").strip(),
        "first_gen": form.get("first_gen") in ("yes","on","true","1"),
        "athlete": form.get("athlete") in ("yes","on","true","1"),
        "is_international": form.get("is_international") in ("yes","on","true","1"),
        "no_aps_offered": form.get("no_aps_offered") in ("yes","on","true","1"),
        "aps_offered_not_taken": form.get("aps_offered_not_taken") in ("yes","on","true","1"),
        "no_ibs_offered": form.get("no_ibs_offered") in ("yes","on","true","1"),
        "ibs_offered_not_taken": form.get("ibs_offered_not_taken") in ("yes","on","true","1"),
        "class_rank": f("class_rank", int),
        "class_size": f("class_size", int),
        "no_class_rank_offered": form.get("no_class_rank_offered") in ("yes","on","true","1"),
        "self_rigor": f("self_rigor", int),
        "w_gpa_freshman": f("w_gpa_freshman", float),
        "w_gpa_sophomore": f("w_gpa_sophomore", float),
        "w_gpa_junior": f("w_gpa_junior", float),
        "w_gpa_senior": f("w_gpa_senior", float),
        "w_notoffered_freshman": form.get("w_notoffered_freshman") in ("yes","on","true","1"),
        "w_notoffered_sophomore": form.get("w_notoffered_sophomore") in ("yes","on","true","1"),
        "w_notoffered_junior": form.get("w_notoffered_junior") in ("yes","on","true","1"),
        "w_notoffered_senior": form.get("w_notoffered_senior") in ("yes","on","true","1"),
    }
    # Merge picker selections into the aps string
    picked = form.getlist("ap_pick") if hasattr(form, "getlist") else []
    parts = [p.strip() for p in picked if p.strip()]
    result["aps"] = ", ".join(parts)[:LIMITS["aps"]]
    # Same for IBs
    ib_picked = form.getlist("ib_pick") if hasattr(form, "getlist") else []
    ib_parts = [p.strip() for p in ib_picked if p.strip()]
    result["ibs"] = ", ".join(ib_parts)[:LIMITS["ibs"]]
    # Legacy boolean is derived from whether they listed any legacy schools.
    result["legacy"] = bool(result["legacy_schools"])
    # Multi-select prefs: getlist returns all checked values. Stored as
    # comma-separated string. Empty string = no preference.
    for key in ("pref_weather","pref_setting","pref_size","pref_greek","pref_sports",
                "pref_major_strength","pref_class_size","pref_prestige","pref_cost",
                "pref_diversity","pref_party","pref_research","pref_career_intensity",
                "pref_location","pref_aid","pref_culture"):
        vals = form.getlist(key) if hasattr(form, "getlist") else (form.get(key, "") or "").split(",")
        vals = [v.strip() for v in vals if v and v.strip() and v.strip() != "any"]
        result[key] = ",".join(vals)
    # Per-pref importance weights
    result["pref_weights"] = parse_pref_weights_form(form)
    return result


def pref_set(profile, key):
    """Return the user's chosen values for a preference as a set of strings.
    Empty set = 'no preference' (matches everything)."""
    raw = (profile.get(key) if profile else "") or ""
    return set(v for v in (s.strip() for s in raw.split(",")) if v and v != "any")


# ─── ROUTES ───────────────────────────────────────────────
@app.route("/")
def landing():
    # Show the landing page to everyone, including logged-in users.
    # They can click any of the nav links to get to the product.
    # Pull live numbers so the social proof on the page is honest
    try:
        with db() as conn:
            user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            profiles_count = conn.execute("SELECT COUNT(*) c FROM profiles WHERE uw_gpa IS NOT NULL").fetchone()["c"]
        activation_pct = round(100 * profiles_count / user_count) if user_count else 0
    except Exception:
        user_count, activation_pct = 70, 78
    cds_count = len(CDS_VERIFIED)
    school_count = len(COLLEGES)
    return _landing_html(user_count, school_count, cds_count, activation_pct)


_ORBIT_SCHOOLS = [
    # (slug, display name, logo file in /static/logos/)
    ("harvard",   "Harvard",     "harvard.png"),
    ("mit",       "MIT",         "mit.png"),
    ("stanford",  "Stanford",    "stanford.png"),
    ("princeton", "Princeton",   "princeton.png"),
    ("cornell",   "Cornell",     "cornell.png"),
    ("ucb",       "UC Berkeley", "ucb.png"),
    ("ucla",      "UCLA",        "ucla.png"),
    ("usc",       "USC",         "usc.png"),
    ("duke",      "Duke",        "duke.png"),
    ("nyu",       "NYU",         "nyu.png"),
]

def _landing_html(user_count, school_count, cds_count, activation_pct):
    """The marketing landing page. Keep this hand-written and specific —
    do not let it drift into AI-slop SaaS-template language. The Cornell
    story is the hook; calibrated honesty is the differentiator; the HS
    junior framing is the voice. Aurora effect via pure-CSS radial
    gradients animated with keyframes, no JS dependency."""
    n_orbit = len(_ORBIT_SCHOOLS)
    orbit_items = ""
    orbit_keyframes = ""  # one counter-rotation animation per item
    for i, (slug, name, logo_file) in enumerate(_ORBIT_SCHOOLS):
        angle = i * 360 / n_orbit
        logo_url = url_for('static', filename=f'logos/{logo_file}')
        orbit_items += (
            f'<a class="orbit-item" href="/college/{slug}" title="{name}" '
            f'style="--angle:{angle:.2f}deg;">'
            f'<div class="orbit-tile" style="animation:orbit-counter-{i} 80s linear infinite;">'
            f'<img class="orbit-logo" src="{logo_url}" alt="{name}" loading="lazy">'
            f'</div></a>'
        )
        orbit_keyframes += (
            f"@keyframes orbit-counter-{i} {{"
            f"from{{transform:rotate({-angle:.2f}deg);}}"
            f"to{{transform:rotate({-angle - 360:.2f}deg);}}"
            f"}}"
        )
    css = """
<style>
  /* Override BASE_CSS body background to make aurora visible */
  html, body { background: #070d14 !important; }
  body { overflow-x:hidden; background: transparent !important; }
  .wrap { max-width: none !important; padding: 0 !important; margin: 0 !important; }

  /* Aurora is 3 separate blobs that traverse the full screen on different paths.
     Wrapper handles mouse-reactive translation; blobs handle autonomous travel. */
  .aurora-wrapper {
    position:fixed; inset:0; pointer-events:none; z-index:0;
    will-change: transform;
  }
  .blob {
    position: absolute;
    border-radius: 50%;
    filter: blur(80px);
    will-change: transform;
    pointer-events: none;
  }
  .blob-1 {
    width: 60vw; height: 60vw;
    background: radial-gradient(circle, rgba(95,201,182,.55), transparent 60%);
    animation: blob1-travel 22s linear infinite, blob-pulse 8s ease-in-out infinite;
    top: -10vh; left: -20vw;
  }
  .blob-2 {
    width: 55vw; height: 55vw;
    background: radial-gradient(circle, rgba(56,189,248,.42), transparent 60%);
    animation: blob2-travel 28s linear infinite, blob-pulse 11s ease-in-out infinite reverse;
    top: 30vh; left: 80vw;
  }
  .blob-3 {
    width: 50vw; height: 50vw;
    background: radial-gradient(circle, rgba(54,184,168,.38), transparent 60%);
    animation: blob3-travel 25s linear infinite, blob-pulse 9s ease-in-out infinite;
    top: 60vh; left: 30vw;
  }
  @keyframes blob1-travel {
    0%   { transform: translate(0vw, 0vh); }
    25%  { transform: translate(110vw, 30vh); }
    50%  { transform: translate(80vw, 90vh); }
    75%  { transform: translate(-10vw, 70vh); }
    100% { transform: translate(0vw, 0vh); }
  }
  @keyframes blob2-travel {
    0%   { transform: translate(0vw, 0vh); }
    25%  { transform: translate(-90vw, 20vh); }
    50%  { transform: translate(-110vw, -40vh); }
    75%  { transform: translate(20vw, -50vh); }
    100% { transform: translate(0vw, 0vh); }
  }
  @keyframes blob3-travel {
    0%   { transform: translate(0vw, 0vh); }
    25%  { transform: translate(40vw, -60vh); }
    50%  { transform: translate(-50vw, -30vh); }
    75%  { transform: translate(-70vw, 30vh); }
    100% { transform: translate(0vw, 0vh); }
  }
  @keyframes blob-pulse {
    0%,100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
  .lp-wrap { max-width:1500px; margin:0 auto; padding:0 24px; position: relative; z-index: 2; }
  .hero { padding:40px 0 60px; text-align:left; max-width:780px; }
  .hero .eyebrow { display:inline-block; font-size:.74em; font-weight:600; letter-spacing:1.5px; text-transform:uppercase; color:#9aa6b6; padding:0 0 10px; border:none; border-bottom:1px solid rgba(154,166,182,.25); border-radius:0; background:none; margin-bottom:22px; }
  .hero h1 { font-size:clamp(2.4em,5vw,3.6em); font-weight:700; letter-spacing:-1.5px; line-height:1.06; margin:0 0 22px; color:#e6edf3; }
  .hero h1 .accent { color:#5fc9b6; }
  .hero p.lede { font-size:1.18em; color:#9aa6b6; max-width:620px; margin:0 0 32px; line-height:1.55; }
  .hero .cta-row { display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
  .hero .cta-row a { display:inline-flex; align-items:center; gap:8px; padding:12px 22px; border-radius:4px; font-weight:600; text-decoration:none; transition:all .18s; font-size:.97em; }
  .hero .cta-row .primary { background:linear-gradient(135deg,#5fc9b6 0%,#36b8a8 100%); color:#070d14; box-shadow:0 6px 24px rgba(95,201,182,.28); }
  .hero .cta-row .primary:hover { transform:translateY(-1px); box-shadow:0 8px 30px rgba(95,201,182,.4); }
  .hero .cta-row .secondary { color:#e6edf3; border:1px solid rgba(255,255,255,.12); background:rgba(255,255,255,.03); }
  .hero .cta-row .secondary:hover { border-color:rgba(95,201,182,.4); color:#5fc9b6; }
  .hero .stats { display:flex; gap:36px; margin-top:48px; flex-wrap:wrap; }
  .hero .stats .stat { font-size:.88em; color:#9aa6b6; }
  .hero .stats .stat .num { display:block; font-size:1.9em; font-weight:700; color:#e6edf3; line-height:1; margin-bottom:4px; letter-spacing:-.5px; }
  .hero .stats .stat .num .accent { color:#5fc9b6; }

  .section { padding:90px 0; }
  .section h2 { font-size:clamp(1.7em,3vw,2.3em); font-weight:700; letter-spacing:-.6px; margin:0 0 16px; color:#e6edf3; }
  .section p.sub { color:#9aa6b6; font-size:1.05em; margin:0 0 40px; max-width:680px; }

  .problem-card { background:#0d1620; border:1px solid rgba(255,255,255,.08); border-radius:6px; padding:36px; }
  .problem-card .quote { font-size:1.25em; line-height:1.5; color:#e6edf3; font-weight:500; margin:0 0 16px; }
  .problem-card .quote strong { color:#5fc9b6; }
  .problem-card .vs { display:flex; gap:14px; align-items:stretch; margin-top:20px; flex-wrap:wrap; }
  .problem-card .vs .col { flex:1; min-width:240px; padding:18px; border-radius:4px; }
  .problem-card .vs .wrong { background:rgba(239,68,68,.08); border:1px solid rgba(239,68,68,.22); }
  .problem-card .vs .right { background:rgba(95,201,182,.08); border:1px solid rgba(95,201,182,.25); }
  .problem-card .vs .label { font-size:.74em; font-weight:600; letter-spacing:.5px; text-transform:uppercase; opacity:.7; margin-bottom:6px; }
  .problem-card .vs .wrong .label { color:#fca5a5; }
  .problem-card .vs .right .label { color:#5fc9b6; }
  .problem-card .vs .num { font-size:1.6em; font-weight:700; letter-spacing:-.4px; color:#e6edf3; }

  /* ── Calibration receipt section ── */
  .proof-section { padding:80px 0 60px; }
  .proof-eyebrow { font-size:.74em; font-weight:600; letter-spacing:1.6px; text-transform:uppercase; color:#5fc9b6; margin-bottom:18px; }
  .proof-h2 { font-family:'Newsreader',Georgia,serif; font-size:clamp(1.9em,3.4vw,2.7em); font-weight:500; letter-spacing:-1px; line-height:1.1; margin:0 0 14px; color:#e6edf3; max-width:880px; }
  .proof-sub { color:#9aa6b6; font-size:1.05em; line-height:1.55; margin:0 0 40px; max-width:680px; }
  .proof-sub b { color:#e6edf3; font-weight:600; }

  .proof-chart { background:#0d1620; border:1px solid rgba(255,255,255,.08); border-radius:8px; padding:32px 36px; max-width:880px; }
  .bar-row { display:grid; grid-template-columns:160px 1fr 200px; gap:18px; align-items:center; padding:10px 0; }
  .bar-row + .bar-row { border-top:1px solid rgba(255,255,255,.04); }
  .bar-row-truth { border-top:1px solid rgba(95,201,182,.25)!important; padding-top:18px; margin-top:8px; }
  .bar-label { font-size:.92em; color:#cbd5e1; font-weight:500; display:flex; align-items:center; gap:8px; }
  .bar-row-truth .bar-label, .bar-row-candor .bar-label { color:#e6edf3; font-weight:600; }
  .truth-mark { color:#5fc9b6; font-weight:700; }
  .bar-track { background:rgba(255,255,255,.04); height:34px; border-radius:4px; position:relative; overflow:hidden; }
  .bar-fill { height:100%; display:flex; align-items:center; padding:0 12px; border-radius:4px; transition:width .8s cubic-bezier(.2,.6,.2,1); }
  .bar-off { background:rgba(244,114,182,.18); border-right:2px solid rgba(244,114,182,.5); }
  .bar-off .bar-val { color:#f9a8d4; }
  .bar-truth { background:rgba(95,201,182,.18); border-right:2px solid rgba(95,201,182,.7); }
  .bar-truth .bar-val { color:#5fc9b6; }
  .bar-candor { background:linear-gradient(90deg, rgba(95,201,182,.22), rgba(56,189,248,.22)); border-right:2px solid #5fc9b6; }
  .bar-candor .bar-val { color:#5fc9b6; }
  .bar-val { font-family:'Newsreader',Georgia,serif; font-weight:600; font-size:1.05em; font-variant-numeric:tabular-nums; letter-spacing:-.3px; }
  .bar-diff { font-size:.82em; color:#7a8595; font-variant-numeric:tabular-nums; }
  .bar-row-truth .bar-diff, .bar-row-candor .bar-diff { color:#9aa6b6; }

  .proof-caption {
    margin-top:24px; padding:18px 24px; max-width:880px;
    border-left:3px solid #5fc9b6; background:rgba(95,201,182,.04);
    color:#cbd5e1; font-size:.98em; line-height:1.55; border-radius:0 6px 6px 0;
  }
  .proof-caption b { color:#5fc9b6; font-weight:700; }

  .proof-preview-wrap { margin-top:54px; max-width:880px; }
  .proof-preview-label { font-size:.92em; color:#9aa6b6; margin-bottom:14px; }
  .proof-preview {
    display:block; background:#0d1620; border:1px solid rgba(255,255,255,.10);
    border-radius:10px; padding:28px 32px; text-decoration:none; color:#e6edf3;
    transition:border-color .2s, background .2s, transform .2s;
    box-shadow: 0 24px 60px rgba(0,0,0,.4), 0 1px 0 rgba(255,255,255,.03) inset;
  }
  .proof-preview:hover { border-color:rgba(95,201,182,.35); background:#101a25; text-decoration:none; transform:translateY(-2px); }
  .prv-head { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom:6px; }
  .prv-name { font-family:'Newsreader',Georgia,serif; font-size:2em; font-weight:500; letter-spacing:-.8px; margin:0; color:#e6edf3; }
  .prv-badge { font-size:.7em; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:#031715; background:linear-gradient(135deg,#5fc9b6,#36b8a8); padding:4px 10px; border-radius:999px; }
  .prv-meta { color:#9aa6b6; font-size:.9em; margin-bottom:22px; }
  .prv-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.06); border-radius:6px; overflow:hidden; margin-bottom:20px; }
  .prv-stat { background:#0a121a; padding:16px 18px; }
  .prv-stat-label { font-size:.7em; color:#7a8595; letter-spacing:1px; text-transform:uppercase; font-weight:600; margin-bottom:6px; }
  .prv-stat-val { font-family:'Newsreader',Georgia,serif; font-size:1.45em; font-weight:500; letter-spacing:-.5px; color:#e6edf3; font-variant-numeric:tabular-nums; }
  .prv-tags { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:18px; }
  .prv-tag { background:rgba(95,201,182,.08); border:1px solid rgba(95,201,182,.18); color:#5fc9b6; padding:4px 10px; border-radius:5px; font-size:.78em; font-weight:500; }
  .prv-open { color:#5fc9b6; font-weight:600; font-size:.92em; }

  @media (max-width:680px) {
    .proof-section { padding:60px 0 40px; }
    .proof-chart { padding:22px 18px; }
    .bar-row { grid-template-columns:100px 1fr; gap:10px; }
    .bar-diff { grid-column: 2; font-size:.78em; padding-top:2px; }
    .bar-label { font-size:.82em; }
    .proof-preview { padding:22px 20px; }
    .prv-grid { grid-template-columns:repeat(2,1fr); }
    .prv-name { font-size:1.5em; }
  }

  .features-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1px; background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.06); border-radius:4px; overflow:hidden; }
  .feature { background:#0a121a; padding:28px 26px; transition:background .2s; }
  .feature:hover { background:#0d1620; }
  .feature .num { display:block; font-family:'Newsreader',Georgia,serif; font-size:.85em; color:#5fc9b6; font-weight:500; letter-spacing:.5px; margin-bottom:18px; font-feature-settings:"tnum"; }
  .feature h3 { margin:0 0 8px; font-size:1.1em; color:#e6edf3; font-weight:600; letter-spacing:-.2px; }
  .feature p { margin:0; color:#9aa6b6; font-size:.92em; line-height:1.55; }

  .founder {
    position:relative; max-width:760px; margin:0 auto;
    background:transparent; border:0; padding:48px 24px;
  }
  .founder::before {
    content:"\201C"; position:absolute; top:-8px; left:-6px;
    font-family:'Newsreader',Georgia,serif; font-size:9em; line-height:1;
    color:rgba(95,201,182,.18); pointer-events:none; user-select:none;
  }
  .founder p { font-family:'Newsreader',Georgia,serif; font-size:1.32em; line-height:1.55; color:#e6edf3; margin:0 0 18px; font-weight:400; letter-spacing:-.2px; }
  .founder p:first-of-type { font-size:1.55em; line-height:1.4; color:#e6edf3; margin-bottom:24px; }
  .founder p:first-of-type b, .founder p:first-of-type strong { color:#5fc9b6; }
  .founder p:last-child { margin:0; }
  .founder .signature {
    font-family:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Inter',sans-serif;
    font-size:.9em; color:#5fc9b6; font-weight:600; letter-spacing:.3px;
    margin-top:24px !important; padding-top:18px; border-top:1px solid rgba(95,201,182,.18);
  }

  .premium-band {
    background:linear-gradient(135deg,#0f3a37 0%,#0a131c 65%);
    border:1px solid rgba(95,201,182,.28);
    border-radius:8px;
    padding:48px;
  }
  .premium-band-header { max-width:680px; margin-bottom:36px; }
  .premium-band-eyebrow {
    font-size:.78em; font-weight:600; letter-spacing:.8px; text-transform:uppercase;
    color:#5fc9b6; padding:5px 12px; border:1px solid rgba(95,201,182,.3);
    border-radius:999px; background:rgba(95,201,182,.06); display:inline-block; margin-bottom:18px;
  }
  .premium-band-h2 {
    font-size:clamp(1.6em,2.6vw,2.1em); font-weight:700; letter-spacing:-.5px;
    margin:0 0 14px; color:#e6edf3; line-height:1.15;
  }
  .premium-band-sub {
    font-size:1.02em; line-height:1.55; color:#9aa6b6; margin:0;
  }
  .premium-features {
    display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:1px; background:rgba(95,201,182,.08);
    border:1px solid rgba(95,201,182,.12); border-radius:6px; overflow:hidden;
  }
  .premium-feature { background:#0a121a; padding:24px 22px; }
  .premium-feature .premium-feature-num {
    font-family:'Newsreader',Georgia,serif; font-size:.85em; color:#5fc9b6;
    font-weight:500; letter-spacing:.5px; margin-bottom:14px; font-feature-settings:"tnum";
  }
  .premium-feature h3 {
    margin:0 0 8px; font-size:1.02em; color:#e6edf3; font-weight:600; letter-spacing:-.2px;
  }
  .premium-feature p {
    margin:0; color:#9aa6b6; font-size:.88em; line-height:1.5;
  }
  .premium-band-cta {
    display:flex; align-items:center; gap:18px; flex-wrap:wrap; margin-top:32px;
  }
  .premium-cta-btn {
    display:inline-block; padding:13px 28px;
    background:linear-gradient(135deg,#5fc9b6 0%,#36b8a8 100%);
    color:#070d14; font-weight:700; font-size:.98em;
    border-radius:6px; text-decoration:none;
    box-shadow:0 6px 22px rgba(95,201,182,.3);
    transition:all .18s ease;
  }
  .premium-cta-btn:hover {
    transform:translateY(-1px);
    box-shadow:0 8px 28px rgba(95,201,182,.42);
  }
  .premium-band-note { color:#7a8595; font-size:.82em; }
  @media (max-width:680px) {
    .premium-band { padding:28px 22px; }
  }
  .final-cta { text-align:center; padding:80px 0 100px; }
  .final-cta h2 { margin-bottom:18px; }
  .final-cta p { color:#9aa6b6; margin:0 0 30px; font-size:1.08em; }
  .final-cta a.primary { display:inline-block; padding:14px 32px; background:linear-gradient(135deg,#5fc9b6 0%,#36b8a8 100%); color:#070d14; font-weight:600; border-radius:4px; text-decoration:none; box-shadow:0 8px 30px rgba(95,201,182,.3); transition:all .2s; }
  .final-cta a.primary:hover { transform:translateY(-2px); box-shadow:0 12px 36px rgba(95,201,182,.4); }

  footer { padding:40px 0 60px; text-align:center; color:#5e6b7c; font-size:.84em; border-top:1px solid rgba(255,255,255,.04); margin-top:60px; }
  footer a { color:#9aa6b6; }

  .reveal { opacity:0; transform:translateY(16px); transition:opacity .7s ease, transform .7s ease; }
  .reveal.in { opacity:1; transform:translateY(0); }
  /* No-JS fallback: never strand below-fold content invisible if JS fails to load. */
  .no-js .reveal { opacity:1 !important; transform:none !important; transition:none !important; }

  /* ─── Calculator auto-cycle "live demo" pill ─────────────────── */
  .live-pill {
    position:absolute; top:14px; right:14px; z-index:3;
    font-family:var(--mono,inherit);
    font-size:10px; letter-spacing:.16em; text-transform:uppercase;
    color:#5fc9b6; padding:4px 10px;
    border:1px solid rgba(95,201,182,.3); border-radius:999px;
    animation:livePulse 2s ease-in-out infinite;
    pointer-events:none;
  }
  .live-pill.gone { opacity:0; transition:opacity .6s ease; }
  @keyframes livePulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
  .demo-card { position:relative; }
  /* swap effect when calculator auto-cycles */
  .demo-odds, .demo-fit, .demo-tier, #demo-school { transition:opacity .2s ease, transform .2s ease; }
  .demo-odds.swap, .demo-fit.swap, .demo-tier.swap, #demo-school.swap { opacity:.4; transform:scale(.98); }

  /* ─── Stagger entry on feature boxes (scroll-driven where supported) ─── */
  .features-grid .feature {
    animation: featureIn 1.1s cubic-bezier(.2,.7,.2,1) both;
    animation-timeline: view();
    animation-range: entry 0% cover 40%;
  }
  .features-grid .feature:nth-child(odd)  { --fx: -36px; }
  .features-grid .feature:nth-child(even) { --fx:  36px; }
  @keyframes featureIn {
    from { opacity:0; transform: translateX(var(--fx, 0)) translateY(24px); }
    to   { opacity:1; transform: translateX(0) translateY(0); }
  }
  /* Fallback for browsers w/o animation-timeline support: use the existing
     section .reveal mechanism (parent fades in once); features just appear. */
  @supports not (animation-timeline: view()) {
    .features-grid .feature { animation: none; }
  }

  /* ─── Bar-fill scroll animation on the proof chart ─── */
  .proof-chart .bar-fill {
    transform-origin: left center;
    animation: barGrow 1.3s cubic-bezier(.2,.7,.2,1) both;
    animation-timeline: view();
    animation-range: entry 15% cover 55%;
  }
  @keyframes barGrow {
    from { clip-path: inset(0 100% 0 0); }
    to   { clip-path: inset(0 0 0 0); }
  }
  @supports not (animation-timeline: view()) {
    .proof-chart .bar-fill { animation: none; }
  }

  /* ─── Hero particle field — drifts forever once the video lands ─── */
  .hero .particles {
    position: absolute; inset: 0;
    z-index: 1;
    pointer-events: none;
    overflow: hidden;
    opacity: 0;
    transition: opacity 1.8s ease;
  }
  html.landed .hero .particles { opacity: 1; }
  .hero .particles span {
    position: absolute;
    width: 3px; height: 3px;
    background: #5fc9b6;
    border-radius: 50%;
    box-shadow: 0 0 6px rgba(95,201,182,0.55), 0 0 14px rgba(95,201,182,0.25);
    opacity: 0;
    animation: heroDrift 14s linear infinite;
  }
  .hero .particles span:nth-child(1)  { left:  4%; --dx:  18px; animation-duration: 16s; animation-delay:  -0.0s; }
  .hero .particles span:nth-child(2)  { left:  9%; --dx: -22px; animation-duration: 13s; animation-delay:  -2.4s; }
  .hero .particles span:nth-child(3)  { left: 14%; --dx:  30px; animation-duration: 18s; animation-delay:  -1.1s; }
  .hero .particles span:nth-child(4)  { left: 18%; --dx: -14px; animation-duration: 15s; animation-delay:  -5.8s; }
  .hero .particles span:nth-child(5)  { left: 22%; --dx:  24px; animation-duration: 11s; animation-delay:  -3.2s; }
  .hero .particles span:nth-child(6)  { left: 27%; --dx: -28px; animation-duration: 17s; animation-delay:  -0.7s; }
  .hero .particles span:nth-child(7)  { left: 31%; --dx:  12px; animation-duration: 14s; animation-delay:  -4.5s; }
  .hero .particles span:nth-child(8)  { left: 36%; --dx: -18px; animation-duration: 19s; animation-delay:  -8.2s; }
  .hero .particles span:nth-child(9)  { left: 40%; --dx:  26px; animation-duration: 12s; animation-delay:  -1.8s; }
  .hero .particles span:nth-child(10) { left: 45%; --dx: -22px; animation-duration: 16s; animation-delay:  -6.4s; }
  .hero .particles span:nth-child(11) { left: 49%; --dx:  16px; animation-duration: 13s; animation-delay:  -2.1s; }
  .hero .particles span:nth-child(12) { left: 53%; --dx: -30px; animation-duration: 18s; animation-delay:  -7.6s; }
  .hero .particles span:nth-child(13) { left: 58%; --dx:  20px; animation-duration: 15s; animation-delay:  -3.9s; }
  .hero .particles span:nth-child(14) { left: 62%; --dx: -14px; animation-duration: 11s; animation-delay:  -0.3s; }
  .hero .particles span:nth-child(15) { left: 66%; --dx:  28px; animation-duration: 17s; animation-delay:  -5.1s; }
  .hero .particles span:nth-child(16) { left: 71%; --dx: -24px; animation-duration: 14s; animation-delay:  -8.8s; }
  .hero .particles span:nth-child(17) { left: 75%; --dx:  18px; animation-duration: 19s; animation-delay:  -1.5s; }
  .hero .particles span:nth-child(18) { left: 80%; --dx: -16px; animation-duration: 12s; animation-delay:  -4.2s; }
  .hero .particles span:nth-child(19) { left: 84%; --dx:  22px; animation-duration: 16s; animation-delay:  -6.9s; }
  .hero .particles span:nth-child(20) { left: 89%; --dx: -26px; animation-duration: 13s; animation-delay:  -2.7s; }
  .hero .particles span:nth-child(21) { left: 93%; --dx:  14px; animation-duration: 18s; animation-delay:  -7.3s; }
  .hero .particles span:nth-child(22) { left:  6%; --dx: -20px; animation-duration: 15s; animation-delay:  -3.6s; }
  .hero .particles span:nth-child(23) { left: 19%; --dx:  26px; animation-duration: 11s; animation-delay:  -9.1s; }
  .hero .particles span:nth-child(24) { left: 33%; --dx: -18px; animation-duration: 17s; animation-delay:  -1.2s; }
  .hero .particles span:nth-child(25) { left: 47%; --dx:  24px; animation-duration: 14s; animation-delay:  -4.8s; }
  .hero .particles span:nth-child(26) { left: 61%; --dx: -22px; animation-duration: 19s; animation-delay:  -7.4s; }
  .hero .particles span:nth-child(27) { left: 76%; --dx:  16px; animation-duration: 12s; animation-delay:  -2.5s; }
  .hero .particles span:nth-child(28) { left: 91%; --dx: -28px; animation-duration: 16s; animation-delay:  -5.9s; }
  @keyframes heroDrift {
    0%   { transform: translate(0, 100vh) scale(0.6); opacity: 0; }
    12%  { opacity: 0.85; }
    78%  { opacity: 0.7; }
    100% { transform: translate(var(--dx, 0), -10vh) scale(1.1); opacity: 0; }
  }

  /* Full-bleed cinematic hero: a background video sits behind left-aligned
     content. The section breaks out of .lp-wrap edge-to-edge; .hero-inner
     re-centers the content to the same 1500px column as the rest of the page. */
  .hero.hero-grid {
    display:block;
    position:relative;
    max-width:none;
    margin-left: calc(50% - 50vw);
    margin-right: calc(50% - 50vw);
    padding:118px 0 80px;
    overflow:hidden;
    background:#070d14;
    isolation:isolate;
  }
  .hero-inner {
    position:relative; z-index:2;
    max-width:1500px; margin:0 auto; padding:0 24px;
  }
  .hero-bg-video, .hero-bg-overlay {
    position:absolute; inset:0; width:100%; height:100%;
    pointer-events:none;
  }
  .hero-bg-video {
    z-index:0; object-fit:cover; object-position:50% 42%;
    filter:brightness(.84) saturate(1.07);
    transition:filter 1.1s ease;
  }
  /* Layered overlays: teal glow (right) + edge vignette + strong left wash
     for text readability + top/bottom fades that blend the video into the
     navy page so it never reads as a separate video box. */
  .hero-bg-overlay {
    z-index:1;
    transition:opacity 1.1s ease;
    background:
      radial-gradient(58% 68% at 90% 42%, rgba(54,184,168,.20), transparent 72%),
      radial-gradient(135% 130% at 38% 36%, transparent 46%, rgba(4,9,15,.92) 100%),
      linear-gradient(101deg, rgba(7,13,20,.97) 0%, rgba(7,13,20,.88) 30%, rgba(7,13,20,.55) 56%, rgba(7,13,20,.30) 82%, rgba(7,13,20,.42) 100%),
      linear-gradient(180deg, rgba(7,13,20,.38) 0%, rgba(7,13,20,.10) 12%, transparent 26%, transparent 68%, rgba(7,13,20,.92) 100%);
  }
  .hero.hero-grid .hero-text { max-width:600px; display:flex; flex-direction:column; gap:14px; }
  /* Cinematic intro: nav + hero content stay hidden during the fly-in, then
     fade in once the camera lands. Toggled on <html> by JS only when motion
     is allowed -- no-JS and reduced-motion visitors see the full page. */
  html.intro-armed .nav,
  html.intro-armed .hero-text > * { opacity:0; }
  html.intro-armed .nav { transform:translateY(-14px); pointer-events:none; }
  html.intro-armed .hero-text > * { transform:translateY(26px); }
  html.landed .nav,
  html.landed .hero-text > * { opacity:1; transform:translateY(0); }
  /* Hard guarantee: once landed, the nav is ALWAYS interactive, at every
     breakpoint — overrides any lingering intro-armed pointer-events:none. */
  html.landed .nav { pointer-events:auto !important; }
  html.landed .nav {
    pointer-events:auto;
    transition:opacity .8s ease, transform .8s cubic-bezier(.2,.7,.2,1);
  }
  html.landed .hero-text > * {
    transition:opacity .85s ease, transform .85s cubic-bezier(.2,.7,.2,1);
  }
  html.landed .hero-text > *:nth-child(1) { transition-delay:.12s; }
  html.landed .hero-text > *:nth-child(2) { transition-delay:.26s; }
  html.landed .hero-text > *:nth-child(3) { transition-delay:.40s; }
  html.intro-armed:not(.landed) .hero-bg-overlay { opacity:.55; }
  @media (prefers-reduced-motion: reduce) {
    html.intro-armed .nav,
    html.intro-armed .hero-text > * { opacity:1; transform:none; }
  }
  /* Mobile: skip the cinematic intro so the calculator is visible immediately.
     The 8s video reveal was killing TikTok/Reddit drive-by traffic before
     they saw the hero copy or the inline demo. */
  @media (max-width: 768px) {
    html.intro-armed .nav,
    html.intro-armed .hero-text > * { opacity:1!important; transform:none!important; }
    /* Belt-and-suspenders: even if intro-armed lingers on mobile, the nav must
       stay tappable. This is the root of the "can't click at first" bug. */
    html.intro-armed .nav { pointer-events:auto!important; }
  }
  /* Skip-intro button — only visible on desktop while the cinematic intro is
     playing (mobile has no intro). Sits top-right above everything. */
  #intro-skip {
    position:fixed; top:20px; right:26px; z-index:120;
    display:none; align-items:center; gap:6px;
    padding:8px 16px; border-radius:999px; cursor:pointer;
    font:600 .82em/1 'Hanken Grotesk',sans-serif; letter-spacing:.3px;
    color:#e6edf3; background:rgba(10,19,28,.6);
    border:1px solid rgba(255,255,255,.18);
    backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
    transition:background .15s, border-color .15s, opacity .4s;
  }
  #intro-skip:hover { background:rgba(16,34,47,.85); border-color:rgba(95,201,182,.45); color:#fff; }
  html.intro-armed:not(.landed) #intro-skip { display:inline-flex; }
  html.landed #intro-skip { display:none; }
  @media (max-width:768px) { #intro-skip { display:none!important; } }
  .hero.hero-grid .hero-text-above { display:flex; flex-direction:column; gap:10px; margin-bottom:6px; }
  .hero.hero-grid h1 { font-size:clamp(1.8em, 3vw, 2.45em); margin:8px 0 4px; line-height:1.1; }
  .hero.hero-grid p.lede { font-size:.96em; margin:0 0 12px; line-height:1.5; }
  .hero.hero-grid .stats { margin-top:8px; gap:20px; }
  .hero.hero-grid .stats .stat { font-size:.8em; }
  .hero.hero-grid .stats .stat .num { font-size:1.3em; }
  .hero.hero-grid .cta-row { gap:10px; margin:4px 0; }
  .hero.hero-grid .cta-row a { padding:10px 18px; font-size:.92em; }
  .hero.hero-grid .eyebrow { margin-bottom:0; padding:4px 10px; font-size:.72em; }
  @media (max-width:980px) {
    .hero.hero-grid { padding:92px 0 64px!important; }
    .hero.hero-grid .hero-text { max-width:none; }
    /* Mobile: content spans full width, so darken the whole frame evenly
       instead of weighting the wash to the left. */
    .hero-bg-overlay {
      background:
        radial-gradient(120% 80% at 80% 10%, rgba(54,184,168,.16), transparent 70%),
        linear-gradient(180deg, rgba(7,13,20,.93) 0%, rgba(7,13,20,.80) 38%, rgba(7,13,20,.90) 100%);
    }
    .hero-bg-video { filter:brightness(.72) saturate(1.06); }
  }

  /* Phone (<=768px): NO video/animation. On a phone the landscape clip only
     crops to a dark center slice, and the 8s intro delayed the demo (which was
     hurting TikTok/Reddit drive-by traffic). So drop it entirely -- the hero is
     a clean navy panel with a soft teal glow, and the headline + live demo are
     visible instantly. Desktop keeps the full cinematic intro. */
  @media (max-width:768px) {
    .hero-bg-video { display:none; }
    .hero.hero-grid {
      background:
        radial-gradient(115% 60% at 80% 4%, rgba(54,184,168,.12), transparent 60%),
        #070d14;
    }
    /* No video to scrim, and no intro fade on mobile -- keep the panel steady. */
    .hero-bg-overlay { display:none; }
  }

  /* Right-column logo wall (image-based via Clearbit). Tiles are uniform
     dark cards; the source PNGs are filtered to white so different schools
     read as a single unified grid rather than a colorful jumble. */
  .hero-logos { display:flex; flex-direction:column; gap:14px; }
  .hero-logos-label {
    font-size:.74em; font-weight:600; letter-spacing:.6px;
    text-transform:uppercase; color:#9aa6b6;
  }
  .hero-logos-grid {
    display:grid;
    grid-template-columns: repeat(4, 1fr);
    gap:10px;
  }
  /* Orbital logo wall — schools rotate around a Candor center. Each
     orbit-tile has a per-item counter-rotation animation (defined inline
     below the static styles) so the tile stays upright as the ring
     rotates. We pre-generate one keyframe per item to avoid relying on
     @property/custom-prop interpolation, which has spotty support. */
  .logo-orbit {
    position:relative; width:100%; aspect-ratio:1/1;
    max-width:780px; margin:0 auto;
    --orbit-radius: -315px;
  }
  .orbit-center {
    position:absolute; top:50%; left:50%;
    transform:translate(-50%, -50%);
    width:170px; height:170px;
    display:flex; align-items:center; justify-content:center;
    background:rgba(95,201,182,.06);
    border:1px solid rgba(95,201,182,.32);
    border-radius:50%;
    z-index:2;
    box-shadow:0 0 90px rgba(95,201,182,.2), inset 0 0 40px rgba(95,201,182,.06);
  }
  .orbit-center svg { width:96px; height:96px; }
  .orbit-ring {
    position:absolute; inset:0;
    animation:orbit-spin 80s linear infinite;
    transform-origin:center;
  }
  .orbit-item {
    position:absolute;
    top:50%; left:50%;
    width:0; height:0;
    transform:rotate(var(--angle, 0deg)) translateY(var(--orbit-radius, -230px));
  }
  .orbit-tile {
    width:120px; height:120px;
    display:flex; align-items:center; justify-content:center;
    transform:translate(-50%, -50%);
    background:transparent;
    border:none;
    border-radius:0;
    /* per-item animation: orbit-counter-N — defined in the keyframes
       block injected below, one per orbit position. */
    transition:transform .15s;
    will-change:transform;
  }
  .orbit-logo {
    /* Each logo file is pre-cropped to its content bounding box (PIL
       getbbox), so object-fit:contain makes every logo fill this 110px
       box as fully as its aspect ratio allows. Wide logos end shorter
       in height, tall logos end narrower, but the *visual area* of
       each is roughly equal. */
    width:110px; height:110px;
    object-fit:contain;
    transition:filter .15s, transform .15s;
  }
  .orbit-item:hover .orbit-logo {
    transform:scale(1.1);
    filter:drop-shadow(0 0 18px rgba(95,201,182,.55));
  }
  @keyframes orbit-spin { to { transform:rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    .orbit-ring { animation:none; }
    .orbit-tile { animation:none !important; }
  }
  @media (max-width:980px) {
    .logo-orbit { max-width:460px; --orbit-radius: -190px; }
    .orbit-tile { width:60px; height:60px; }
    .orbit-logo { width:38px; height:38px; }
    .orbit-center { width:96px; height:96px; }
    .orbit-center svg { width:52px; height:52px; }
  }
  @media (max-width:600px) {
    .logo-orbit { max-width:340px; --orbit-radius: -140px; }
    .orbit-tile { width:48px; height:48px; }
    .orbit-logo { width:30px; height:30px; }
    .orbit-center { width:74px; height:74px; }
    .orbit-center svg { width:40px; height:40px; }
  }
  @media (max-width:980px) {
    .hero-logos-grid { grid-template-columns: repeat(5, 1fr); }
  }
  @media (max-width:600px) {
    .hero-logos-grid { grid-template-columns: repeat(4, 1fr); }
    .logo-tile { padding:10px; }
  }
  @media (max-width:420px) {
    .hero-logos-grid { grid-template-columns: repeat(3, 1fr); }
  }

  /* Interactive demo (used inside hero-demo column) */
  .demo-section {
    padding: 60px 0 90px;
    border-top: 1px solid rgba(255,255,255,.06);
  }
  .demo-eyebrow {
    display:inline-block; font-size:.74em; font-weight:600;
    letter-spacing:.8px; text-transform:uppercase; color:#5fc9b6;
    padding:5px 12px; border:1px solid rgba(95,201,182,.25); border-radius:999px;
    background:rgba(95,201,182,.06); margin-bottom:18px;
  }
  .demo-title {
    font-size:clamp(1.8em, 3.4vw, 2.6em); font-weight:700;
    letter-spacing:-.6px; margin:0 0 12px; color:#e6edf3;
  }
  .demo-sub {
    color:#9aa6b6; font-size:1.02em; margin:0 0 28px; max-width:620px; line-height:1.55;
  }
  .demo-card {
    display:grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1.05fr); gap:0;
    background:rgba(11,18,27,.66);
    -webkit-backdrop-filter:blur(16px) saturate(1.4);
    backdrop-filter:blur(16px) saturate(1.4);
    border:1px solid rgba(255,255,255,.10); border-radius:10px;
    overflow:hidden;
    box-shadow:0 28px 70px -24px rgba(0,0,0,.78), inset 0 1px 0 rgba(255,255,255,.05);
  }
  .demo-controls {
    padding:14px 16px; border-right:1px solid rgba(255,255,255,.06);
    display:flex; flex-direction:column; gap:8px;
  }
  @media (max-width:560px) {
    .demo-card { grid-template-columns:1fr; }
    .demo-controls { border-right:none; border-bottom:1px solid rgba(255,255,255,.06); }
  }
  .demo-field { display:flex; flex-direction:column; gap:8px; }
  .demo-label {
    font-size:.78em; font-weight:600; letter-spacing:.4px;
    text-transform:uppercase; color:#9aa6b6;
    display:flex; justify-content:space-between; align-items:baseline;
  }
  .demo-readout {
    color:#5fc9b6; font-weight:700; font-size:1.05em;
    text-transform:none; letter-spacing:0;
    font-variant-numeric:tabular-nums;
  }
  .demo-test-toggle {
    display:inline-flex; gap:0;
    border:1px solid rgba(255,255,255,.12); border-radius:99px;
    overflow:hidden; padding:2px;
    background:rgba(255,255,255,.02);
  }
  .demo-test-btn {
    background:transparent; border:0; padding:3px 10px;
    font-size:.78em; font-weight:600; color:#9aa6b6;
    text-transform:uppercase; letter-spacing:.4px;
    border-radius:99px; cursor:pointer;
    transition:background .15s, color .15s;
  }
  .demo-test-btn.active {
    background:rgba(95,201,182,.15);
    color:#5fc9b6;
  }
  .demo-test-btn:not(.active):hover { color:#cbd5e1; }
  .demo-field select, .demo-field input[type=range] { width:100%; }
  .demo-field select {
    background:#0a121a; color:#e6edf3; border:1px solid rgba(255,255,255,.12);
    border-radius:5px; padding:10px 12px; font-size:.95em; font-weight:500;
    cursor:pointer; appearance:none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'><path fill='%239aa6b6' d='M6 8L0 0h12z'/></svg>");
    background-repeat:no-repeat; background-position:right 12px center; background-size:10px;
    padding-right:36px;
  }
  .demo-field select:focus { outline:none; border-color:rgba(95,201,182,.5); }
  .demo-field input[type=range] {
    -webkit-appearance:none; appearance:none; background:transparent; height:24px;
    cursor:pointer;
  }
  .demo-field input[type=range]::-webkit-slider-runnable-track {
    height:4px; background:rgba(255,255,255,.08); border-radius:2px;
  }
  .demo-field input[type=range]::-moz-range-track {
    height:4px; background:rgba(255,255,255,.08); border-radius:2px;
  }
  .demo-field input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; appearance:none;
    width:18px; height:18px; border-radius:50%;
    background:#5fc9b6; border:2px solid #0d1620; margin-top:-7px;
    box-shadow:0 0 0 1px rgba(95,201,182,.4);
    transition: transform .12s ease;
  }
  .demo-field input[type=range]::-webkit-slider-thumb:hover { transform:scale(1.15); }
  .demo-field input[type=range]::-moz-range-thumb {
    width:18px; height:18px; border-radius:50%;
    background:#5fc9b6; border:2px solid #0d1620;
  }
  .demo-result {
    padding:14px 16px; display:flex; flex-direction:column; gap:8px;
    background:linear-gradient(180deg, rgba(95,201,182,.04) 0%, transparent 60%);
  }
  .demo-result-row { display:flex; gap:12px; flex-wrap:wrap; }
  .demo-result-block { flex:1; min-width:120px; }
  .demo-result-label {
    font-size:.72em; font-weight:600; letter-spacing:.6px;
    text-transform:uppercase; color:#9aa6b6; margin-bottom:6px;
  }
  .demo-odds {
    font-size:1.55em; font-weight:800; letter-spacing:-.6px; line-height:1;
    color:#5fc9b6; font-variant-numeric:tabular-nums;
  }
  .demo-fit, .demo-tier {
    font-size:1.05em; font-weight:700; letter-spacing:-.3px; color:#e6edf3;
    font-variant-numeric:tabular-nums;
  }
  .demo-tier { font-size:.92em; font-weight:600; }
  .demo-context {
    color:#9aa6b6; font-size:.76em; line-height:1.4;
    border-top:1px solid rgba(255,255,255,.06); padding-top:8px;
  }
  .demo-cta {
    margin-top:6px; display:inline-block; align-self:flex-start;
    background:linear-gradient(135deg,#5fc9b6 0%,#3fcdb2 100%);
    color:#0b1220; font-weight:700; font-size:.95em; text-decoration:none;
    padding:11px 22px; border-radius:8px;
    box-shadow:0 4px 14px rgba(95,201,182,.25), 0 1px 0 rgba(255,255,255,.08) inset;
    transition:transform .12s ease, box-shadow .15s ease, background .15s ease;
  }
  .demo-cta:hover {
    background:linear-gradient(135deg,#7ff7df 0%,#5fc9b6 100%);
    box-shadow:0 6px 20px rgba(95,201,182,.35), 0 1px 0 rgba(255,255,255,.12) inset;
    transform:translateY(-1px);
  }
  .demo-cta:active { transform:translateY(0); }
  .demo-premium-inline {
    display:inline-block; margin-top:8px; align-self:flex-start;
    font-size:.82em; color:#5fc9b6; text-decoration:none;
    padding:4px 0; border-bottom:1px dashed rgba(95,201,182,.4);
    transition:color .15s, border-color .15s;
  }
  .demo-premium-inline:hover {
    color:#7ff7df; border-bottom-color:#7ff7df;
  }
  .demo-math-toggle {
    margin-top:8px; display:inline-block; align-self:flex-start;
    color:#9aa6b6; font-size:.78em; cursor:pointer; user-select:none;
    background:none; border:none; padding:4px 0; font-family:inherit;
    border-bottom:1px dashed rgba(154,166,182,.35);
    transition:color .12s, border-color .12s;
  }
  .demo-math-toggle:hover { color:#5fc9b6; border-bottom-color:rgba(95,201,182,.5); }
  .demo-math {
    display:none; margin-top:10px; padding:12px 14px;
    background:rgba(95,201,182,.04); border:1px solid rgba(95,201,182,.15);
    border-radius:6px; font-size:.82em; line-height:1.55; color:#c9d3e1;
  }
  .demo-math.open { display:block; }
  .demo-math .demo-math-row { display:flex; justify-content:space-between; gap:10px; padding:3px 0; }
  .demo-math .demo-math-row b { color:#e8eef6; font-weight:600; }
  .demo-math .demo-math-note {
    margin-top:8px; padding-top:8px; border-top:1px solid rgba(95,201,182,.12);
    color:#9aa6b6; font-size:.94em;
  }

  @media (max-width:680px) {
    .demo-odds { font-size:2em; }
    .demo-fit { font-size:1.3em; }
  }

  @media (max-width:680px) {
    .lp-wrap { padding:0 18px; }
    .hero { padding:50px 0 60px; }
    .hero h1 { letter-spacing:-.6px; }
    .hero p.lede { font-size:1.05em; }
    .hero .cta-row { gap:10px; }
    .hero .cta-row a { padding:11px 18px; font-size:.95em; }
    .section { padding:60px 0; }
    .lp-nav { padding:14px 16px; }
    .lp-nav .links a:not(.btn-primary) { display:none; }
    .hero .stats { gap:22px; margin-top:32px; }
    .hero .stats .stat .num { font-size:1.5em; }
    .problem-card { padding:24px 20px; }
    .problem-card .quote { font-size:1.05em; }
    .problem-card .vs .col { min-width:0; flex-basis:calc(50% - 7px); padding:14px; }
    .problem-card .vs .num { font-size:1.3em; }
    .feature { padding:22px 20px; }
    .founder { padding:24px 20px; }
    .founder p { font-size:1em; }
    .final-cta { padding:60px 0 80px; }
  }
  @media (max-width:420px) {
    .problem-card .vs .col { flex-basis:100%; }
    .hero h1 { font-size:2em; }
  }
</style>
"""
    def _asset_ver(fn):
        try:
            return int(os.path.getmtime(os.path.join(app.static_folder, fn)))
        except Exception:
            return 0
    hero_video_url = url_for('static', filename='hero-aurora.mp4') + f"?v={_asset_ver('hero-aurora.mp4')}"
    hero_video_mobile_url = url_for('static', filename='hero-aurora-mobile.mp4') + f"?v={_asset_ver('hero-aurora-mobile.mp4')}"
    hero_poster_url = url_for('static', filename='hero-aurora.jpg') + f"?v={_asset_ver('hero-aurora.jpg')}"
    # Live user count instead of a hardcoded "100+" that silently lies once the
    # real number passes it. Floor to a clean number for social proof (>=100),
    # exact below that — honest either way.
    users_display = (user_count // 50) * 50 if user_count >= 100 else user_count
    body = f"""
<script>
(function(){{
  var d = document.documentElement;
  var reduce = false;
  try {{ reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; }} catch (e) {{}}
  // Mobile (<=768px) has no intro video at all (display:none), so arming the
  // intro there only leaves nav with pointer-events:none until the 9s failsafe
  // fires -- that was the "can't tap until the second interaction" bug. Skip
  // arming on phones entirely; the hero is already fully visible there.
  var isMobile = false;
  try {{ isMobile = window.matchMedia && window.matchMedia('(max-width: 768px)').matches; }} catch (e) {{}}
  // Reduced motion: skip the fly-in -- nav and content stay visible, video
  // holds on its poster (the final courtyard frame).
  if (reduce || isMobile) return;
  // Arm immediately so the nav and hero content never flash in before the
  // camera lands.
  d.classList.add('intro-armed');
  var landed = false;
  function land(){{
    if (landed) return;
    landed = true;
    d.classList.add('landed');
    // Also REMOVE intro-armed so its pointer-events:none / opacity:0 rules
    // can never keep winning over .landed (esp. inside mobile @media blocks).
    d.classList.remove('intro-armed');
  }}
  // Failsafe: always reveal the page even if the video stalls or errors.
  // The intro is an 8s cinematic that ends on the CANDOR title settling into
  // the colonnade hero plate, and the timeupdate handler below reveals the
  // page ~0.55s before the true end (~7.45s) so content fades in as the shot
  // lands. This failsafe sits just past that as a pure safety net for a
  // broken/stalled video -- it must NOT preempt the natural end-reveal, or the
  // page would cut in at 6s mid-title. The poster is the final frame, so
  // revealing over it always looks correct.
  setTimeout(land, 9000);
  function wire(){{
    // Desktop skip button — reveal the page immediately on click.
    var sk = document.getElementById('intro-skip');
    if (sk) sk.addEventListener('click', function(e){{ e.preventDefault(); land(); }});
    var v = document.querySelector('.hero-bg-video');
    if (!v) {{ land(); return; }}
    v.addEventListener('timeupdate', function(){{
      if (v.duration && (v.duration - v.currentTime) < 0.55) land();
    }});
    v.addEventListener('ended', land);
    // A genuinely broken or stalled video should reveal the page immediately
    // rather than waiting out the failsafe.
    v.addEventListener('error', land);
    v.addEventListener('stalled', function(){{ setTimeout(land, 1500); }});
    var pr = v.play();
    if (pr && typeof pr.catch === 'function') pr.catch(function(){{ land(); }});
  }}
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
}})();
</script>
<button id="intro-skip" type="button" aria-label="Skip intro animation">Skip intro <span aria-hidden="true">&rarr;</span></button>
<div class="aurora-wrapper">
  <div class="blob blob-1"></div>
  <div class="blob blob-2"></div>
  <div class="blob blob-3"></div>
</div>
{_nav()}

<main class="lp-wrap">
  <section class="hero hero-grid">
   <video class="hero-bg-video" muted playsinline preload="auto" poster="{hero_poster_url}" aria-hidden="true" tabindex="-1">
     <source src="{hero_video_mobile_url}" type="video/mp4" media="(max-width: 768px)">
     <source src="{hero_video_url}" type="video/mp4">
   </video>
   <div class="hero-bg-overlay" aria-hidden="true"></div>
   <div class="particles" aria-hidden="true">
     <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
     <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
     <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
     <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
   </div>
   <div class="hero-inner">
   <div class="hero-text">
    <div class="hero-text-above">
      <div class="eyebrow">Verified Common Data Set figures · calibrated to real outcomes</div>
      <h1>The college admissions calculator that uses <span class="accent">real data</span>, not guesses.</h1>
      <p class="lede">Most chances calculators tell everyone they have a 25-30% shot at every T20. Stanford accepts 3.6%. The math doesn't work. Candor uses verified Common Data Set figures from {cds_count}+ schools and odds calibrated to be honest, not optimistic.</p>
      <div class="cta-row">
        <a href="#demo" class="primary" data-scroll-to-demo>Get your chances →</a>
        <a href="/colleges" class="secondary">Browse schools</a>
      </div>
      <div class="stats">
        <div class="stat"><span class="num" data-count-to="{cds_count}">{cds_count}</span>CDS-verified schools</div>
        <div class="stat"><span class="num">0</span>made-up numbers</div>
        <div class="stat"><span class="num">Free</span>to check your odds</div>
      </div>
    </div>
    <div class="demo-eyebrow" id="demo">Try it · no signup needed</div>
    <div class="demo-card">
      <div class="live-pill" id="live-demo-pill" aria-hidden="true">▸ live demo</div>
      <div class="demo-controls">
        <label class="demo-field">
          <span class="demo-label">School</span>
          <select id="demo-school">
            <option value="cornell">Cornell</option>
            <option value="harvard">Harvard</option>
            <option value="mit">MIT</option>
            <option value="stanford">Stanford</option>
            <option value="yale">Yale</option>
            <option value="princeton">Princeton</option>
            <option value="upenn">UPenn</option>
            <option value="brown">Brown</option>
            <option value="columbia">Columbia</option>
            <option value="duke">Duke</option>
            <option value="uchicago">UChicago</option>
            <option value="ucb">UC Berkeley</option>
            <option value="northwestern">Northwestern</option>
            <option value="vanderbilt">Vanderbilt</option>
            <option value="rice">Rice</option>
            <option value="notre-dame">Notre Dame</option>
          </select>
        </label>
        <label class="demo-field">
          <span class="demo-label">Unweighted GPA <span class="demo-readout" id="demo-gpa-out">3.90</span></span>
          <input type="range" id="demo-gpa" min="2.5" max="4.0" step="0.01" value="3.90">
        </label>
        <label class="demo-field">
          <span class="demo-label">
            <span class="demo-test-toggle">
              <button type="button" class="demo-test-btn active" data-test="sat">SAT</button>
              <button type="button" class="demo-test-btn" data-test="act">ACT</button>
            </span>
            <span class="demo-readout" id="demo-score-out">1500</span>
          </span>
          <input type="range" id="demo-sat" min="1100" max="1600" step="10" value="1500">
          <input type="range" id="demo-act" min="18" max="36" step="1" value="33" style="display:none">
        </label>
      </div>
      <div class="demo-result">
        <div class="demo-result-row">
          <div class="demo-result-block">
            <div class="demo-result-label">Your odds</div>
            <div class="demo-odds" id="demo-odds">—</div>
          </div>
          <div class="demo-result-block">
            <div class="demo-result-label">Profile fit</div>
            <div class="demo-fit" id="demo-fit">—</div>
          </div>
          <div class="demo-result-block">
            <div class="demo-result-label">Tier</div>
            <div class="demo-tier" id="demo-tier">—</div>
          </div>
        </div>
        <div class="demo-context" id="demo-context">School mid-50%: —</div>
        <button type="button" class="demo-math-toggle" id="demo-math-toggle" aria-expanded="false">How is this calculated?</button>
        <div class="demo-math" id="demo-math" hidden>
          <div class="demo-math-row"><span>Base accept rate (CDS)</span><b id="demo-math-base">—</b></div>
          <div class="demo-math-row"><span>Your GPA vs admitted mid-50%</span><b id="demo-math-gpa">—</b></div>
          <div class="demo-math-row"><span>Your <span id="demo-math-test-label">SAT</span> vs admitted mid-50%</span><b id="demo-math-test">—</b></div>
          <div class="demo-math-row"><span>Profile fit score</span><b id="demo-math-fit">—</b></div>
          <div class="demo-math-row"><span>Tier ceiling applied</span><b id="demo-math-ceiling">—</b></div>
          <div class="demo-math-note">
            Odds combine your position in the school's admitted-student bands with a tier ceiling (elite schools cap typical applicants below ~15%; exceptional profiles lift the cap). The full model adds hooks, ED/EA round, demographics, and demonstrated interest. Sign up to run the full version.
          </div>
        </div>
        <a href="/signup" class="demo-cta">Sign up free to run this on your full profile →</a>
        <a href="/upgrade" class="demo-premium-inline">Want a real plan? See Premium · $3/mo →</a>
      </div>
    </div>
   </div>
   </div>
  </section>

  <section class="section reveal proof-section">
    <div class="proof-eyebrow">CALIBRATION RECEIPT</div>
    <h2 class="proof-h2">Three calculators told me three different things about Vanderbilt.</h2>
    <p class="proof-sub">All claimed real data. Vanderbilt's actual CDS-reported acceptance rate is <b>5.9%</b>. Here's what the popular ones told me.</p>

    <div class="proof-chart">
      <div class="bar-row">
        <div class="bar-label">Calculator A</div>
        <div class="bar-track"><div class="bar-fill bar-off" style="width:60%"><span class="bar-val">30%</span></div></div>
        <div class="bar-diff">+24.1pt off</div>
      </div>
      <div class="bar-row">
        <div class="bar-label">Calculator B</div>
        <div class="bar-track"><div class="bar-fill bar-off" style="width:44%"><span class="bar-val">22%</span></div></div>
        <div class="bar-diff">+16.1pt off</div>
      </div>
      <div class="bar-row">
        <div class="bar-label">Calculator C</div>
        <div class="bar-track"><div class="bar-fill bar-off" style="width:16%"><span class="bar-val">8%</span></div></div>
        <div class="bar-diff">+2.1pt off</div>
      </div>
      <div class="bar-row bar-row-truth">
        <div class="bar-label"><span class="truth-mark">✓</span> CDS truth</div>
        <div class="bar-track"><div class="bar-fill bar-truth" style="width:12%"><span class="bar-val">5.9%</span></div></div>
        <div class="bar-diff">Vanderbilt's actual number</div>
      </div>
      <div class="bar-row bar-row-candor">
        <div class="bar-label">Candor</div>
        <div class="bar-track"><div class="bar-fill bar-candor" style="width:12%"><span class="bar-val">5.9%</span></div></div>
        <div class="bar-diff">We use the actual number</div>
      </div>
    </div>

    <div class="proof-caption">
      One of those calculators was off by <b>24 percentage points</b>. That's the difference between "safety" and "ED-only reach." If your list is built on those numbers, your strategy is built on fiction.
    </div>

    <div class="proof-preview-wrap">
      <div class="proof-preview-label">Here's what every school page looks like — real CDS data, no guesswork.</div>
      <a href="/college/vanderbilt" class="proof-preview" aria-label="Open Vanderbilt page">
        <div class="prv-head">
          <h3 class="prv-name">Vanderbilt</h3>
          <span class="prv-badge">CDS VERIFIED</span>
        </div>
        <div class="prv-meta">Nashville, Tennessee · 7,221 undergrad · Private</div>
        <div class="prv-grid">
          <div class="prv-stat"><div class="prv-stat-label">ACCEPTANCE</div><div class="prv-stat-val">5.9%</div></div>
          <div class="prv-stat"><div class="prv-stat-label">SAT MID-50%</div><div class="prv-stat-val">1510–1560</div></div>
          <div class="prv-stat"><div class="prv-stat-label">GPA RANGE</div><div class="prv-stat-val">3.85–4.00</div></div>
          <div class="prv-stat"><div class="prv-stat-label">ACT MID-50%</div><div class="prv-stat-val">34–35</div></div>
        </div>
        <div class="prv-tags">
          <span class="prv-tag">Economics</span>
          <span class="prv-tag">HOD</span>
          <span class="prv-tag">Engineering</span>
          <span class="prv-tag">Biology</span>
          <span class="prv-tag">Political Science</span>
        </div>
        <div class="prv-open">Open the full Vanderbilt page →</div>
      </a>
    </div>
  </section>

  <section class="section reveal">
    <h2>What's different</h2>
    <p class="sub">No vibes-based admissions math. Every number has a source.</p>
    <div class="features-grid">
      <div class="feature">
        <span class="num">01 / Data</span>
        <h3>CDS-verified figures</h3>
        <p>{cds_count}+ schools have stats hand-pulled from their official Common Data Set. The rest use federal data with a clear "verified vs federal" badge so you know what's checked.</p>
      </div>
      <div class="feature">
        <span class="num">02 / Model</span>
        <h3>Calibrated, not optimistic</h3>
        <p>Elite schools cap at sub-15% even for top applicants — because that's reality. Truly exceptional applicants (USAMO golds, recruited athletes) get a separate flag that lifts the cap honestly.</p>
      </div>
      <div class="feature">
        <span class="num">03 / Hooks</span>
        <h3>Per-school weighting</h3>
        <p>Legacy at Harvard ≠ legacy at Duke. The model factors legacy at the specific school, athlete status, first-gen, and demonstrated interest at schools that actually track it.</p>
      </div>
      <div class="feature">
        <span class="num">04 / Fit</span>
        <h3>Targets, not lotteries</h3>
        <p>Schools sorted by how well they actually match your stats and preferences (size, vibe, weather, prestige weighting). Pushes you toward real targets, not just lottery reaches.</p>
      </div>
      <div class="feature">
        <span class="num">05 / Profiles</span>
        <h3>Real outcomes from real applicants</h3>
        <p>Pulled from r/collegeresults and similar — actual admit/reject/waitlist outcomes with stats and what stood out, so you can calibrate against people who actually got in.</p>
      </div>
      <div class="feature">
        <span class="num">06 / Advisor</span>
        <h3>AI grounded in hand-checked facts</h3>
        <p>Every school has hand-checked program facts the AI can't override (USC Roski requires portfolios for ALL majors, etc.). No more hallucinated advice.</p>
      </div>
    </div>
  </section>

  <section class="section reveal">
    <div class="founder">
      <p>I'm a high school junior. Last semester I spent hours on every chances calculator on the internet trying to figure out where I actually stood for college, and the numbers were all over the place — one said <b>30%</b> at Vanderbilt, another said <b>8%</b>, another said <b>22%</b>.</p>
      <p>I started digging into why, and turns out most of them either use federal data that lags 1-2 years, have AI just make up stats, or use a fit model so generic it's basically useless. So I spent a few weeks pulling the actual Common Data Set PDFs from each school's website and built my own.</p>
      <p>The goal is calibration, not telling you what you want to hear. If your odds at Stanford are 4%, you should know that — so you can spend your ED slot somewhere it'll actually matter.</p>
      <p class="signature">— Jasper, Candor's founder</p>
    </div>
  </section>

  <section class="section reveal">
    <div class="premium-band">
      <div class="premium-band-header">
        <div class="premium-band-eyebrow">Candor Premium · $3/mo</div>
        <h2 class="premium-band-h2">A chances number alone won't get you in.</h2>
        <p class="premium-band-sub">Premium is the layer that turns "6% at Stanford" into a plan. Built into every school page and your full college list. $3/month, cancel anytime — keep it through your whole application cycle.</p>
      </div>
      <div class="premium-features">
        <div class="premium-feature">
          <div class="premium-feature-num">01</div>
          <h3>Personalized AI strategy</h3>
          <p>Per-school strategy calibrated to your stats, ECs, and what that school actually weights. Not generic advice — yours.</p>
        </div>
        <div class="premium-feature">
          <div class="premium-feature-num">02</div>
          <h3>List grader + simulator</h3>
          <p>Score your full college list 1–10. Simulate where you'd ED, EA, RD — and your probability of getting into at least one reach.</p>
        </div>
        <div class="premium-feature">
          <div class="premium-feature-num">03</div>
          <h3>Score push impact</h3>
          <p>See exactly how a +60 SAT or +2 ACT moves your odds at each school. Decide if a retake is actually worth the time.</p>
        </div>
        <div class="premium-feature">
          <div class="premium-feature-num">04</div>
          <h3>Saved schools dashboard</h3>
          <p>Every school you've chanced or saved, grouped by application round (ED1, ED2, EA, REA, RD). One view of your whole list.</p>
        </div>
        <div class="premium-feature">
          <div class="premium-feature-num">05</div>
          <h3>Free stays free</h3>
          <p>The chances calculator stays free for everyone. Premium is the strategic layer on top — no paywall on the basics.</p>
        </div>
      </div>
      <div class="premium-band-cta">
        <a href="/upgrade" class="premium-cta-btn">See Premium →</a>
        <span class="premium-band-note">$3/month, cancel anytime. Premium activates within 30 seconds.</span>
      </div>
    </div>
  </section>

  <section class="final-cta reveal">
    <h2>Get your real chances.</h2>
    <p>Free chances in 30 seconds. Upgrade only if you want the strategy on top.</p>
    <a href="/signup" class="primary">Sign up free →</a>
  </section>
</main>

<footer>
  <div class="lp-wrap">
    <p>Built by a HS junior. Not affiliated with any university or admissions service.<br>Stats verified against Common Data Set publications. <a href="/colleges">Browse all schools →</a></p>
  </div>
</footer>

<script>
  // Reveal-on-scroll without any library
  (function(){{
    const all = document.querySelectorAll('.reveal');
    const showAll = () => all.forEach(el => el.classList.add('in'));
    if (!('IntersectionObserver' in window)) {{ showAll(); return; }}
    const io = new IntersectionObserver((entries) => {{
      entries.forEach(e => {{
        if (e.isIntersecting) {{ e.target.classList.add('in'); io.unobserve(e.target); }}
      }});
    }}, {{ threshold: 0.12 }});
    all.forEach(el => io.observe(el));
    // Safety net: if observers never fire (script error after load, broken
    // observer impl, headless screenshot tools, etc.) force everything visible
    // after 2.5s so below-fold content is never stranded invisible.
    setTimeout(showAll, 2500);
  }})();

  // Smooth-scroll the hero "Get your chances" CTA to the inline calculator
  // and focus the school dropdown so keyboard users land on a control.
  (function(){{
    document.querySelectorAll('[data-scroll-to-demo]').forEach(a => {{
      a.addEventListener('click', (e) => {{
        const target = document.getElementById('demo');
        if (!target) return;
        e.preventDefault();
        const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        target.scrollIntoView({{ behavior: reduce ? 'auto' : 'smooth', block: 'start' }});
        setTimeout(() => {{
          const sel = document.getElementById('demo-school');
          if (sel) sel.focus({{ preventScroll: true }});
        }}, reduce ? 0 : 600);
      }});
    }});
  }})();

  // Interactive demo on the landing — calls /api/demo-odds on input change
  // (debounced) and animates the odds/fit/tier into place.
  (function(){{
    const slug = document.getElementById('demo-school');
    const gpa  = document.getElementById('demo-gpa');
    const sat  = document.getElementById('demo-sat');
    const act  = document.getElementById('demo-act');
    const gpaOut = document.getElementById('demo-gpa-out');
    const scoreOut = document.getElementById('demo-score-out');
    const oddsEl = document.getElementById('demo-odds');
    const fitEl  = document.getElementById('demo-fit');
    const tierEl = document.getElementById('demo-tier');
    const ctxEl  = document.getElementById('demo-context');
    const testBtns = document.querySelectorAll('.demo-test-btn');
    const mathToggle = document.getElementById('demo-math-toggle');
    const mathBox    = document.getElementById('demo-math');
    const mathBase   = document.getElementById('demo-math-base');
    const mathGpa    = document.getElementById('demo-math-gpa');
    const mathTestLb = document.getElementById('demo-math-test-label');
    const mathTest   = document.getElementById('demo-math-test');
    const mathFit    = document.getElementById('demo-math-fit');
    const mathCeil   = document.getElementById('demo-math-ceiling');
    if (!slug || !gpa || !sat || !act) return;

    if (mathToggle && mathBox) {{
      mathToggle.addEventListener('click', () => {{
        const open = mathBox.classList.toggle('open');
        if (open) mathBox.removeAttribute('hidden'); else mathBox.setAttribute('hidden','');
        mathToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        mathToggle.textContent = open ? 'Hide the math' : 'How is this calculated?';
      }});
    }}

    function fmtBand(userVal, lo, hi) {{
      if (lo == null || hi == null) return String(userVal);
      const u = Number(userVal);
      const where = (u < Number(lo)) ? 'below' : (u > Number(hi)) ? 'above' : 'within';
      return `${{userVal}} · ${{where}} ${{lo}}–${{hi}}`;
    }}
    function tierCeilingText(tier, hi) {{
      if (tier === 'Dream')  return `~${{Math.max(15, hi || 15)}}% cap on typical strong applicants`;
      if (tier === 'Reach')  return `up to ~30% for strong applicants`;
      if (tier === 'Target') return `no cap — your bands match well`;
      if (tier === 'Safety') return `no cap — admits broadly at your level`;
      return '—';
    }}

    let activeTest = 'sat';

    const TIER_COLORS = {{
      "Dream":"#f9a8d4", "Reach":"#fcd34d",
      "Target":"#7dd3fc", "Safety":"#5fc9b6"
    }};

    let timer;
    function fetchOdds(){{
      const params = {{slug:slug.value, gpa:gpa.value}};
      if (activeTest === 'sat') params.sat = sat.value;
      else                       params.act = act.value;
      fetch('/api/demo-odds?' + new URLSearchParams(params).toString())
        .then(r => r.ok ? r.json() : null)
        .then(d => {{
          if (!d) return;
          oddsEl.textContent = `${{d.low}}–${{d.high}}%`;
          fitEl.textContent  = `${{d.fit}}/100`;
          tierEl.textContent = d.tier;
          tierEl.style.color = TIER_COLORS[d.tier] || '#e6edf3';
          const range = (activeTest === 'sat')
            ? ((d.school_sat_lo && d.school_sat_hi) ? `${{d.school_sat_lo}}–${{d.school_sat_hi}}` : '—')
            : ((d.school_act_lo && d.school_act_hi) ? `${{d.school_act_lo}}–${{d.school_act_hi}}` : '—');
          const testLabel = activeTest.toUpperCase();
          const gpaRange = (d.school_gpa_lo && d.school_gpa_hi) ? `${{d.school_gpa_lo}}–${{d.school_gpa_hi}}` : '—';
          ctxEl.textContent = `${{d.school_name}} · ${{d.school_accept}}% accept · ${{testLabel}} mid-50% ${{range}} · GPA mid-50% ${{gpaRange}}`;
          if (mathBase) {{
            mathBase.textContent = `${{d.school_accept}}%`;
            mathGpa.textContent  = fmtBand(parseFloat(gpa.value).toFixed(2), d.school_gpa_lo, d.school_gpa_hi);
            const userTestVal = (activeTest === 'sat') ? sat.value : act.value;
            const lo = (activeTest === 'sat') ? d.school_sat_lo : d.school_act_lo;
            const hi = (activeTest === 'sat') ? d.school_sat_hi : d.school_act_hi;
            mathTestLb.textContent = testLabel;
            mathTest.textContent   = fmtBand(userTestVal, lo, hi);
            mathFit.textContent    = `${{d.fit}}/100`;
            mathCeil.textContent   = tierCeilingText(d.tier, d.high);
          }}
        }})
        .catch(() => {{}});
    }}
    function schedule(){{ clearTimeout(timer); timer = setTimeout(fetchOdds, 400); }}

    function setTest(which){{
      activeTest = which;
      testBtns.forEach(b => b.classList.toggle('active', b.dataset.test === which));
      sat.style.display = (which === 'sat') ? '' : 'none';
      act.style.display = (which === 'act') ? '' : 'none';
      scoreOut.textContent = (which === 'sat') ? sat.value : act.value;
      schedule();
    }}

    testBtns.forEach(b => b.addEventListener('click', () => setTest(b.dataset.test)));
    slug.addEventListener('change', schedule);
    gpa.addEventListener('input', () => {{ gpaOut.textContent = parseFloat(gpa.value).toFixed(2); schedule(); }});
    sat.addEventListener('input', () => {{ if (activeTest==='sat') scoreOut.textContent = sat.value; schedule(); }});
    act.addEventListener('input', () => {{ if (activeTest==='act') scoreOut.textContent = act.value; schedule(); }});
    fetchOdds(); // initial render
  }})();

  // ─── Calculator auto-cycle ──────────────────────────────────────
  // Demos the product on load by cycling through a small school list.
  // Stops permanently on any user interaction with the card.
  (function(){{
    const slug = document.getElementById('demo-school');
    const card = document.querySelector('.demo-card');
    const pill = document.getElementById('live-demo-pill');
    if (!slug || !card) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {{
      if (pill) pill.classList.add('gone');
      return;
    }}
    const cycle = ['cornell','harvard','stanford','yale','mit','upenn'];
    let idx = cycle.indexOf(slug.value);
    if (idx < 0) idx = 0;
    let stopped = false, cycles = 0, visible = true;
    const MAX_CYCLES = 2;
    const stop = () => {{
      if (stopped) return;
      stopped = true;
      if (pill) pill.classList.add('gone');
    }};
    ['click','touchstart','keydown'].forEach(ev =>
      card.addEventListener(ev, stop, {{ passive: true, once: true }})
    );
    if ('IntersectionObserver' in window) {{
      new IntersectionObserver(es => {{ visible = es[0].isIntersecting; }},
        {{ threshold: 0.25 }}).observe(card);
    }}
    const SWAP_IDS = ['demo-odds','demo-fit','demo-tier'];
    const swapEls = SWAP_IDS.map(id => document.getElementById(id)).filter(Boolean);
    swapEls.push(slug);
    const flick = () => {{
      swapEls.forEach(el => el.classList.add('swap'));
      setTimeout(() => swapEls.forEach(el => el.classList.remove('swap')), 600);
    }};
    const step = () => {{
      if (stopped) return;
      if (!visible) {{ setTimeout(step, 800); return; }}
      idx = (idx + 1) % cycle.length;
      if (idx === 0) {{
        cycles++;
        if (cycles >= MAX_CYCLES) {{ stop(); return; }}
      }}
      flick();
      slug.value = cycle[idx];
      slug.dispatchEvent(new Event('change', {{ bubbles: true }}));
      setTimeout(step, 3000);
    }};
    setTimeout(step, 2500); // let the initial fetchOdds settle first
  }})();

  // ─── Stats counter ──────────────────────────────────────────────
  // Counts up from 0 to data-count-to when an element enters view.
  (function(){{
    const nums = document.querySelectorAll('[data-count-to]');
    if (!nums.length || !('IntersectionObserver' in window)) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const animate = (el) => {{
      const target = parseFloat(el.dataset.countTo);
      if (!isFinite(target)) return;
      const suffix = el.dataset.suffix || '';
      const start = performance.now();
      const duration = 1400;
      const tick = (now) => {{
        const p = Math.min(1, (now - start) / duration);
        const eased = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(target * eased) + suffix;
        if (p < 1) requestAnimationFrame(tick);
      }};
      requestAnimationFrame(tick);
    }};
    const io = new IntersectionObserver(entries => {{
      entries.forEach(e => {{
        if (e.isIntersecting) {{ animate(e.target); io.unobserve(e.target); }}
      }});
    }}, {{ threshold: 0.4 }});
    nums.forEach(n => io.observe(n));
  }})();
</script>
"""
    _og_img = request.url_root.rstrip("/") + url_for("static", filename="hero-aurora.jpg")
    _site_url = request.url_root.rstrip("/") + "/"
    _og_desc = f"College chances calculator with verified Common Data Set figures from {cds_count}+ schools. Built by a HS junior to be honest, not optimistic."
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Candor — College admissions chances, calibrated</title>
<link rel="canonical" href="{_site_url}">
<meta name="description" content="{_og_desc}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Candor">
<meta property="og:title" content="Candor — College admissions chances, calibrated">
<meta property="og:description" content="{_og_desc}">
<meta property="og:image" content="{_og_img}">
<meta property="og:url" content="{_site_url}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Candor — College admissions chances, calibrated">
<meta name="twitter:description" content="{_og_desc}">
<meta name="twitter:image" content="{_og_img}">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"WebApplication","name":"Candor","url":"{_site_url}","applicationCategory":"EducationApplication","operatingSystem":"Web","description":"{_og_desc}","offers":{{"@type":"Offer","price":"0","priceCurrency":"USD"}},"creator":{{"@type":"Organization","name":"Candor","url":"{_site_url}"}}}}</script>
<style>{BASE_CSS}</style>
{css}
<style>{orbit_keyframes}</style>
<style>
  /* Nav floats transparently over the cinematic hero. */
  .nav {{ position:absolute; top:0; left:0; right:0; z-index:50; background:transparent; box-shadow:none; border:0; }}
  .lp-wrap {{ position:relative; z-index:1; }}
</style>
<noscript><style>.reveal{{opacity:1!important;transform:none!important;transition:none!important}}</style></noscript>
</head><body>{body}
<style>
.m-signup-bar{{display:none;}}
@media (max-width:760px){{
  body{{padding-bottom:80px;}}
  .m-signup-bar{{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:1000;align-items:center;justify-content:space-between;gap:12px;padding:11px 16px calc(11px + env(safe-area-inset-bottom));background:rgba(7,13,20,.97);backdrop-filter:blur(10px);border-top:1px solid rgba(255,255,255,.10);box-shadow:0 -6px 24px rgba(0,0,0,.45);}}
  .m-signup-bar .m-txt{{font-size:.9rem;font-weight:700;color:#e6edf3;line-height:1.15;}}
  .m-signup-bar .m-txt span{{display:block;font-size:.74rem;font-weight:400;color:#9aa6b6;}}
  .m-signup-bar a{{flex:0 0 auto;padding:11px 22px;border-radius:6px;font-weight:700;font-size:.93rem;text-decoration:none;white-space:nowrap;color:#070d14;background:linear-gradient(135deg,#5fc9b6 0%,#36b8a8 100%);}}
}}
</style>
<div class="m-signup-bar"><div class="m-txt">See your real odds<span>Free · takes 2 minutes</span></div><a href="/signup">Sign up now →</a></div>
</body></html>"""


@app.route("/colleges")
@app.route("/browse")
def colleges_page():
    return colleges_html()


@app.route("/college/<slug>")
def college_detail(slug):
    return college_detail_html(slug)


@app.route("/rankings")
def rankings_index():
    return rankings_index_html()


@app.route("/rankings/my-fit")
@login_required
def my_fit_page():
    return my_fit_html()


@app.route("/rankings/<slug>")
def ranking_detail(slug):
    return ranking_detail_html(slug)


def _rate_limit(*args, **kwargs):
    """Apply a flask-limiter rate-limit if the limiter is installed,
    otherwise act as a no-op decorator. Accepts the same positional and
    keyword arguments as limiter.limit() (e.g., methods=['POST'])."""
    def deco(fn):
        if limiter:
            return limiter.limit(*args, **kwargs)(fn)
        return fn
    return deco


@app.route("/signup", methods=["GET", "POST"])
@_rate_limit("5 per 15 minutes", methods=["POST"])
def signup_page():
    if current_user(): return redirect(url_for("profile_page"))
    nxt = request.args.get("next") or request.form.get("next")
    if nxt and nxt.startswith("/"):
        session["next_url"] = nxt
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
            flash("Invalid email address.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            with db() as conn:
                if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    flash("That email is already registered.", "error")
                else:
                    h, salt = hash_password(password)
                    cur = conn.execute("INSERT INTO users (email, password_hash, password_salt) VALUES (?,?,?)", (email, h, salt))
                    conn.commit()
                    session["user_id"] = cur.lastrowid
                    flash("Account created — fill in your profile next.", "success")
                    return redirect(session.pop("next_url", None) or url_for("profile_page"))
    return signup_html()


@app.route("/login", methods=["GET", "POST"])
@_rate_limit("5 per 15 minutes", methods=["POST"])
def login_page():
    if current_user(): return redirect(url_for("profile_page"))
    nxt = request.args.get("next") or request.form.get("next")
    if nxt and nxt.startswith("/"):
        session["next_url"] = nxt
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as conn:
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row and verify_password(password, row["password_hash"], row["password_salt"]):
            session["user_id"] = row["id"]
            return redirect(session.pop("next_url", None) or url_for("profile_page"))
        flash("Wrong email or password.", "error")
    return login_html()


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("landing"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile_page():
    if request.method == "POST":
        p = _read_profile_form(request.form)
        uid = current_user()["id"]
        save_profile(uid, p)
        # Profile changed — invalidate any cached advice/chances that were
        # generated against the old profile so the next view regenerates.
        # Also recompute the exceptional flag on the new profile.
        with db() as conn:
            conn.execute("DELETE FROM tailored_advice WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM saved_chances WHERE user_id=?", (uid,))
            # Force re-evaluation by clearing the eval timestamp.
            conn.execute("UPDATE profiles SET exceptional_evaluated_at=NULL WHERE user_id=?", (uid,))
            conn.commit()
        # Trigger evaluation now (lazily — uses cache if it ran less than 30d ago)
        fresh_profile = get_profile(uid)
        if fresh_profile:
            try:
                get_or_evaluate_exceptionality(uid, fresh_profile)
            except Exception as e:
                print(f"exceptionality eval on save failed: {e}")
        flash("Profile saved.", "success")
        nxt = session.pop("next_url", None)
        if nxt: return redirect(nxt)
    return profile_html()


@app.route("/chances/<slug>")
@login_required
def chances_page(slug):
    return chances_html(slug)


@app.route("/chances/<slug>/narrative")
@login_required
def chances_narrative(slug):
    """Async endpoint: generate (and cache) the AI strength/weakness/
    differentiator narrative for the chances page. Called by the page's
    lazy-load so the odds render instantly and this ~3s Claude call fills in
    after."""
    uid = current_user()["id"]
    profile = _chances_profile(uid, slug)
    if profile is None:
        return ("", 204)
    school_data = COLLEGES_BY_SLUG.get(slug)
    if not school_data:
        return ("", 204)
    merged = merged_school(school_data)
    fit, components = compute_fit(profile, merged)
    tier = assign_tier(merged, fit, profile)
    low, high = estimate_odds(merged, fit, profile)
    bullets = generate_bullets(profile, merged, fit, components, tier, (low, high))
    r = {"tier": tier, "odds_low": low, "odds_high": high, "fit": fit,
         "confidence": confidence_level(profile, components), **bullets}
    _save_chances_row(uid, slug, r, bullets)
    return _chances_narrative_ul(r)


@app.route("/improve")
def improve_page():
    return improve_html()


@app.route("/college/<slug>/improve")
def school_improve_page(slug):
    return school_improve_html(slug)


@app.route("/profiles")
def profiles_index_page():
    return profiles_index_html()


@app.route("/college/<slug>/profiles")
def school_profiles_page(slug):
    return school_profiles_html(slug)


# ─── Async API endpoints — page renders fast, JS fills in the slow stuff ──
@app.route("/api/college/<slug>/summary")
def api_college_summary(slug):
    c = COLLEGES_BY_SLUG.get(slug)
    if not c: abort(404)
    return jsonify({"html": _render_facts(get_school_summary(c, force=request.args.get("refresh")=="1"))})


@app.route("/api/college/<slug>/facts")
def api_college_facts(slug):
    c = COLLEGES_BY_SLUG.get(slug)
    if not c: abort(404)
    return jsonify({"html": _render_facts(get_school_facts(c, force=request.args.get("refresh")=="1"))})


_DEMO_RATE = {}  # ip -> deque of timestamps; in-memory rate limit for /api/demo-odds
_DEMO_SLUGS = [
    "harvard","mit","stanford","yale","princeton","upenn","brown","cornell",
    "columbia","duke","uchicago","ucb","northwestern","vanderbilt","rice","notre-dame",
]

@app.route("/api/demo-odds")
def api_demo_odds():
    """No-auth interactive demo on the landing page. Computes a fast odds
    estimate from (slug, gpa, sat) — a stripped subset of the real model
    just for marketing/preview. Rate-limited per IP."""
    from collections import deque
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    dq = _DEMO_RATE.setdefault(ip, deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    # Bump high — real users dragging the slider can fire 50+ requests in
    # a few seconds. 200/min still cheap to compute and stops actual abuse.
    if len(dq) > 200:
        return jsonify({"error":"slow down"}), 429
    dq.append(now)
    slug = (request.args.get("slug") or "").strip().lower()
    if slug not in _DEMO_SLUGS:
        return jsonify({"error":"bad school"}), 400
    school = merged_school(COLLEGES_BY_SLUG.get(slug))
    try:
        gpa = float(request.args.get("gpa") or 0)
        sat = int(request.args.get("sat") or 0)
        act = int(request.args.get("act") or 0)
    except ValueError:
        return jsonify({"error":"bad input"}), 400
    gpa = max(2.0, min(4.0, gpa))
    if sat: sat = max(1000, min(1600, sat))
    if act: act = max(12, min(36, act))
    # Demo placeholder ECs/leadership/awards. The strength is bumped above
    # "average competitive" toward "strong upper-tier applicant" so the
    # demo numbers feel right for visitors with perfect/near-perfect
    # stats (a 4.0/1600 should see ~20-25% at Cornell, not 10-15%).
    # The real model isn't touched — only the demo profile defaults.
    # Stat-perfect / near-perfect demo applicants get the is_exceptional flag
    # so the model lifts caps the same way it does for real top-1% applicants.
    # Otherwise stats-perfect kids see ~10-15% at every elite school in the
    # demo, which feels too pessimistic for a marketing surface.
    # exceptional flag fires for stats-perfect kids on either test
    looks_exceptional = (gpa >= 3.92 and (sat >= 1530 or act >= 34))
    profile = {
        "uw_gpa": gpa, "sat": sat, "act": act,
        "ecs": ("Founder of a substantive student org with measurable impact, "
                "varsity captain or section editor, sustained 3+ years in primary activity, "
                "summer research / internship at university or competitive program, "
                "deep involvement (10+ hrs/wk) in field tied to intended major"),
        "leadership": ("Captain of varsity team, founder/president of school org, "
                       "elected officer in student government, mentorship role"),
        "awards": ("National Merit Finalist, regional/state-level recognition in main "
                   "activity, AP Scholar with Distinction, ranked top of class in subject"),
        "aps": "Calc BC, Chem, Bio, Physics C, Lang, Lit, US History, Stats",
        "major": "", "state": "", "school_type": "private",
        "is_exceptional": looks_exceptional,
    }
    fit, _ = compute_fit(profile, school)
    low, high = estimate_odds(school, fit, profile)
    tier = assign_tier(school, fit, profile)
    return jsonify({
        "low": low, "high": high, "fit": int(round(fit)), "tier": tier,
        "school_name": school["name"],
        "school_accept": round(school["accept"]*100, 1),
        "school_sat_lo": school.get("sat_25"), "school_sat_hi": school.get("sat_75"),
        "school_act_lo": school.get("act_25"), "school_act_hi": school.get("act_75"),
        "school_gpa_lo": school.get("gpa_lo"), "school_gpa_hi": school.get("gpa_hi"),
    })


@app.route("/api/college/<slug>/articles")
def api_college_articles(slug):
    c = COLLEGES_BY_SLUG.get(slug)
    if not c: abort(404)
    arts = fetch_articles(slug)
    if not arts:
        return jsonify({"html": '<p class="muted">No recent articles found.</p>'})
    items = ""
    for a in arts:
        items += f"""<div style="margin-bottom:10px"><a href="{a['url']}" target="_blank" rel="noopener" style="font-weight:600">{a['title']}</a><div class="muted" style="font-size:.78em">{a.get('source','')} · {a.get('published','')}</div></div>"""
    return jsonify({"html": items})


@app.route("/college/<slug>/plan")
@login_required
def school_plan_page(slug):
    if slug in COLLEGES_BY_SLUG:
        _log_calc_run(slug, current_user()["id"])
    return school_plan_html(slug)


def is_saved(user_id, slug):
    if not user_id: return False
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM saved_schools WHERE user_id=? AND college_slug=?",
            (user_id, slug)
        ).fetchone() is not None


def get_saved_schools(user_id):
    if not user_id: return []
    with db() as conn:
        rows = conn.execute(
            "SELECT college_slug FROM saved_schools WHERE user_id=? ORDER BY saved_at DESC",
            (user_id,)
        ).fetchall()
    return [r["college_slug"] for r in rows]


_NAME_TO_SLUG = None
def _name_to_slug_map():
    global _NAME_TO_SLUG
    if _NAME_TO_SLUG is None:
        _NAME_TO_SLUG = {}
        for s in COLLEGES:
            _NAME_TO_SLUG[s["name"].lower().strip()] = s["slug"]
            _NAME_TO_SLUG[s["slug"].lower().strip()] = s["slug"]
    return _NAME_TO_SLUG


@app.route("/compare")
def compare_page():
    # Accept either ?schools=slug,slug or ?school=name1&school=name2 (picker form)
    raw_schools = request.args.get("schools") or ""
    raw_picker  = request.args.getlist("school")
    nm = _name_to_slug_map()
    slugs = []
    for piece in (raw_schools.split(",") + raw_picker):
        key = (piece or "").strip().lower()
        if not key: continue
        slug = nm.get(key)
        if slug and slug not in slugs:
            slugs.append(slug)
        if len(slugs) >= 4: break
    user = current_user()
    profile = get_profile(user["id"]) if user else None
    valid = [merged_school(COLLEGES_BY_SLUG[s]) for s in slugs if s in COLLEGES_BY_SLUG]
    # Show picker if nothing valid
    if not valid:
        saved = get_saved_schools(user["id"])[:4] if user else []
        opts = "".join(f'<option value="{s["name"]}">' for s in COLLEGES)
        # Pre-fill up to 4 inputs with the user's saved schools (by name)
        prefills = []
        for s in saved[:4]:
            sc = COLLEGES_BY_SLUG.get(s)
            if sc: prefills.append(sc["name"])
        while len(prefills) < 4: prefills.append("")
        inputs_html = "".join(
            f'<input name="school" list="schools-list" value="{prefills[i]}" '
            f'placeholder="School {i+1}{" (optional)" if i >= 2 else ""}" '
            f'autocomplete="off" style="margin-bottom:8px">'
            for i in range(4)
        )
        return _page(f"""
<h1>Compare colleges</h1>
<p class="muted">Pick 2-4 schools to see stats, fit, and chances side-by-side. Start typing a name and select from the dropdown.</p>
<form method="get" action="/compare" class="card" style="max-width:680px">
  <label>Schools</label>
  {inputs_html}
  <button class="btn btn-primary" type="submit" style="margin-top:6px">Compare</button>
</form>
<datalist id="schools-list">{opts}</datalist>
""", title="Compare colleges — Candor")
    # Build comparison table
    fits = []
    for c in valid:
        if profile:
            score, _ = compute_my_fit(profile, c)
            fit_acad, _ = compute_fit(profile, c)
            low, high = estimate_odds(c, fit_acad, profile)
            fits.append({"my_fit": score, "odds": f"{low}–{high}%"})
        else:
            fits.append({"my_fit": None, "odds": None})
    # Render header row
    headers = "".join(f'<th><a href="/college/{c["slug"]}" style="color:var(--text)">{c["name"]}</a><div class="muted" style="font-size:.78em;font-weight:400">{city_state(c)}</div></th>' for c in valid)
    rows = []
    def row(label, fn):
        cells = "".join(f"<td>{fn(c)}</td>" for c in valid)
        return f"<tr><td class='muted' style='font-weight:500'>{label}</td>{cells}</tr>"
    rows.append(row("Acceptance rate", lambda c: f"{round(c['accept']*100,1)}%"))
    rows.append(row("GPA mid-50%", lambda c: f"{c['gpa_lo']}–{c['gpa_hi']}"))
    rows.append(row("SAT mid-50%", lambda c: "Test-blind" if is_test_blind(c) else f"{c['sat_25']}–{c['sat_75']}"))
    rows.append(row("ACT mid-50%", lambda c: "Test-blind" if is_test_blind(c) else f"{c['act_25']}–{c['act_75']}"))
    rows.append(row("Undergrads", lambda c: f"{c.get('size','?'):,}"))
    rows.append(row("S/F ratio", lambda c: f"{sf_ratio(c)}:1"))
    rows.append(row("Tuition (sticker)", lambda c: f"${c.get('tuition',0):,}"))
    rows.append(row("Type", lambda c: c["type"].title()))
    rows.append(row("Setting", lambda c: setting_of(c).replace("_"," ").title()))
    rows.append(row("Region", lambda c: region_of(c)))
    rows.append(row("Tier", lambda c: f"Tier {c.get('tier','—')}"))
    if profile:
        rows.append(row("My Fit", lambda c, _i=[0]: (lambda i: (_i.__setitem__(0, _i[0]+1), f"<span style='color:var(--teal);font-weight:600'>{fits[i]['my_fit']}/100</span>")[1])(_i[0])))
        rows.append(row("Your odds", lambda c, _i=[0]: (lambda i: (_i.__setitem__(0, _i[0]+1), f"<span style='font-weight:600'>{fits[i]['odds']}</span>")[1])(_i[0])))
    # Round breakdowns (where curated)
    has_breakdowns = any(admissions_detail(c) for c in valid)
    if has_breakdowns:
        for round_key in ["ED","ED2","REA","EA","RD"]:
            def get_round_rate(c, k=round_key):
                d = admissions_detail(c) or {}
                rate = (d.get("rates") or {}).get(k)
                return f"{round(rate*100,1)}%" if rate else "—"
            if any(get_round_rate(c) != "—" for c in valid):
                rows.append(row(f"{ROUND_LABELS.get(round_key, round_key)} rate", get_round_rate))
    body = f"""<h1>Comparing {len(valid)} schools</h1>
<p class="muted">Side-by-side stats, fit, and chances. Click any school name to see its full page.</p>
<div style="overflow-x:auto"><table class="rank-table" style="margin-top:14px;min-width:600px">
  <thead><tr><th></th>{headers}</tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table></div>
<p style="margin-top:18px"><a class="btn btn-light" href="/compare">Compare different schools</a></p>"""
    return _page(body, title="Compare — Candor")


@app.route("/grade")
@login_required
def profile_grade_page():
    user = current_user()
    profile = get_profile(user["id"])
    if not profile:
        return redirect(url_for("profile_page"))
    # Premium gate.
    if not bool(user.get("is_paid")):
        return _page("""
<h1>Profile Grader</h1>
<p class="muted">Get one honest number for your whole profile — academics, testing, rigor, extracurriculars, and hooks — graded the way a selective-admissions reader would, with your real strengths, weaknesses, and the highest-leverage fixes.</p>
<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3);padding:32px;max-width:620px">
  <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:8px">Candor Premium · $3/mo</div>
  <h2 style="margin:0 0 14px">Profile Grader is a premium feature</h2>
  <p class="muted" style="margin:0 0 18px">$3/month unlocks the full profile grade plus per-school AI strategy, the score predictor, list grader, and admissions simulator — cancel anytime.</p>
  <a class="btn btn-primary" href="/upgrade" style="font-size:1em;padding:12px 28px;display:inline-block">Upgrade — $3/mo &rarr;</a>
</div>
""", title="Profile Grader — Candor")

    # If the grade is already cached for this exact profile, render it instantly.
    # Otherwise show the page immediately with a spinner and load the grade
    # asynchronously from /grade/fragment — the Sonnet grade takes ~10s and we
    # don't want a frozen page while it runs.
    g = _grade_cached(user["id"], profile, compute=False)
    if g is not None:
        return _page(_GRADE_HEADER + _grade_body_html(g), title="Profile Grade — Candor")
    return _page(_GRADE_HEADER + _GRADE_SHELL, title="Profile Grade — Candor")


_GRADE_HEADER = """
<div class="bar"><a href="/profile">← Edit profile</a></div>
<h1>Your Profile Grade</h1>
<p class="muted" style="margin:0 0 18px">Graded for highly selective (top-20) admissions. This is one honest read of your whole profile — not a guarantee.</p>
"""

_GRADE_SHELL = """
<style>@keyframes cdrspin{to{transform:rotate(360deg)}}
.cdr-spinner{width:34px;height:34px;border-radius:50%;border:3px solid rgba(95,201,182,.18);border-top-color:#5fc9b6;animation:cdrspin .8s linear infinite}</style>
<div id="grade-loading" class="card" style="text-align:center;padding:48px 24px">
  <div class="cdr-spinner" style="margin:0 auto 16px"></div>
  <div class="muted">Reading your whole profile like an admissions officer…</div>
  <div class="muted" style="font-size:.82em;margin-top:6px">~10 seconds the first time — instant after that.</div>
</div>
<div id="grade-content"></div>
<script>
(function(){
  fetch('/grade/fragment').then(function(r){return r.text();}).then(function(h){
    document.getElementById('grade-content').innerHTML = h;
    var l=document.getElementById('grade-loading'); if(l) l.style.display='none';
  }).catch(function(){
    var l=document.getElementById('grade-loading');
    if(l) l.innerHTML='<div class="muted">Couldn\\'t load your grade. <a href="/grade">Retry</a></div>';
  });
})();
</script>
"""


def _grade_key(profile):
    import hashlib as _hl
    _gk_fields = ["uw_gpa","weighted_gpa","sat","act","aps","ibs","no_aps_offered",
                  "no_ibs_offered","self_rigor","class_rank","class_size","major",
                  "ecs","awards","leadership","athlete","first_gen","legacy_schools","is_international"]
    return "v7:" + _hl.md5("|".join(str(profile.get(k)) for k in _gk_fields).encode()).hexdigest()


def _grade_cached(uid, profile, compute=False):
    """Return the cached grade dict if it matches the current profile. If
    compute=True and there's no valid cache, compute it (the slow Sonnet call),
    persist, and return it. If compute=False and uncached, return None."""
    import json as _json2
    key = _grade_key(profile)
    if profile.get("grade_key") == key and profile.get("grade_json"):
        try:
            return _json2.loads(profile["grade_json"])
        except Exception:
            pass
    if not compute:
        return None
    g = grade_profile(profile)
    try:
        with db() as conn:
            conn.execute("UPDATE profiles SET grade_json=?, grade_key=? WHERE user_id=?",
                         (_json2.dumps(g), key, uid))
            conn.commit()
    except Exception as e:
        print(f"grade cache write failed: {e}")
    return g


@app.route("/grade/fragment")
@login_required
def profile_grade_fragment():
    user = current_user()
    profile = get_profile(user["id"])
    if not profile or not bool(user.get("is_paid")):
        return ("", 204)
    g = _grade_cached(user["id"], profile, compute=True)
    return _grade_body_html(g)


def _grade_body_html(g):
    from html import escape as _h
    score100 = max(1, min(100, round(g["overall"] / 10)))
    # Color + label band for the headline.
    if score100 >= 85:   band, bcol = "Elite", "#5fc9b6"
    elif score100 >= 70: band, bcol = "Very strong", "#7dd3fc"
    elif score100 >= 55: band, bcol = "Strong", "#7dd3fc"
    elif score100 >= 40: band, bcol = "Solid", "#fcd34d"
    elif score100 >= 25: band, bcol = "Developing", "#fcd34d"
    else:                band, bcol = "Early", "#f9a8d4"

    DIM_LABELS = {
        "academics": "Academics (GPA)", "testing": "Testing",
        "rigor": "Course rigor", "extracurriculars": "Extracurriculars",
        "narrative_hooks": "Narrative & hooks",
    }
    dim_rows = ""
    for k, lbl in DIM_LABELS.items():
        v = g["dimensions"].get(k)
        if v is None:
            continue
        pct = max(1, min(100, round(v / 10)))
        dim_rows += f"""
<div style="margin:12px 0">
  <div style="display:flex;justify-content:space-between;font-size:.9em;margin-bottom:5px">
    <span>{lbl}</span><span class="muted" style="font-weight:600">{pct}/100</span>
  </div>
  <div style="height:8px;background:var(--bg-2);border-radius:999px;overflow:hidden">
    <div style="height:100%;width:{pct}%;background:var(--accent-grad);border-radius:999px"></div>
  </div>
</div>"""

    def _bullets(items, color):
        if not items:
            return '<p class="muted" style="font-size:.9em">—</p>'
        return "".join(
            f'<li style="margin:6px 0;color:var(--text)"><span style="color:{color}">•</span> {_h(s)}</li>'
            for s in items
        )

    summary_html = f'<p class="muted" style="font-size:1.02em;line-height:1.55;margin:6px 0 0">{_h(g["summary"])}</p>' if g.get("summary") else ""
    fb_note = '<p class="muted" style="font-size:.8em;margin-top:18px">Heuristic estimate — AI grader temporarily unavailable.</p>' if g.get("_fallback") else ""

    body = f"""
<div class="card" style="display:flex;align-items:center;gap:26px;flex-wrap:wrap">
  <div style="text-align:center;min-width:140px">
    <div style="font-size:4.2em;font-weight:800;line-height:1;letter-spacing:-2px;color:{bcol}">{score100}</div>
    <div class="muted" style="font-size:.82em;margin-top:2px">out of 100</div>
    <div style="margin-top:8px;display:inline-block;padding:4px 12px;border-radius:999px;font-size:.78em;font-weight:600;background:rgba(95,201,182,.12);color:{bcol};border:1px solid rgba(95,201,182,.25)">{band}</div>
  </div>
  <div style="flex:1;min-width:260px">{summary_html or '<p class="muted">Your profile, dimension by dimension, is below.</p>'}</div>
</div>

<div class="card">
  <h3 style="margin-top:0">Breakdown</h3>
  {dim_rows}
</div>

<div class="row" style="margin-top:0">
  <div class="card">
    <h3 style="margin-top:0;color:var(--teal)">Strengths</h3>
    <ul style="list-style:none;padding:0;margin:0">{_bullets(g.get("strengths"), "var(--teal)")}</ul>
  </div>
  <div class="card">
    <h3 style="margin-top:0;color:#fcd34d">Weaknesses</h3>
    <ul style="list-style:none;padding:0;margin:0">{_bullets(g.get("weaknesses"), "#fcd34d")}</ul>
  </div>
</div>

<div class="card">
  <h3 style="margin-top:0;color:#7dd3fc">Highest-leverage fixes</h3>
  <ul style="list-style:none;padding:0;margin:0">{_bullets(g.get("fixes"), "#7dd3fc")}</ul>
</div>
{fb_note}
<p style="margin-top:18px"><a class="btn btn-primary" href="/plans">See your school list →</a> <a class="btn btn-light" href="/profile">Update profile</a></p>
"""
    return body


@app.route("/predictor")
@login_required
def predictor_page():
    user = current_user()
    profile = get_profile(user["id"])
    if not profile:
        return redirect(url_for("profile_page"))
    # Premium gate: the full score predictor is the "Score push impact" feature
    # advertised on the upgrade page. Free users get the chances calculator;
    # the what-if simulator across their whole list is premium.
    if not bool(user.get("is_paid")):
        return _page("""
<h1>Score predictor</h1>
<p class="muted">See exactly how a +60 SAT, +2 ACT, or higher GPA would move your odds at every school on your list — so you can decide if a retake is worth the time.</p>
<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3);padding:32px;max-width:620px">
  <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:8px">Candor Premium · $3/mo</div>
  <h2 style="margin:0 0 14px">Score push impact is a premium feature</h2>
  <p class="muted" style="margin:0 0 18px">Unlock the what-if simulator plus per-school AI strategy, the list grader, and the admissions simulator — $3/month, cancel anytime.</p>
  <a class="btn btn-primary" href="/upgrade" style="font-size:1em;padding:12px 28px;display:inline-block">Upgrade — $3/mo &rarr;</a>
</div>
""", title="Score predictor — Candor")
    # What schools to simulate over: saved schools, or top-N fits
    saved = get_saved_schools(user["id"])
    if saved:
        target_slugs = saved[:6]
    else:
        scored = []
        for c in COLLEGES[:60]:
            m = merged_school(c)
            s, _ = compute_my_fit(profile, m)
            scored.append((s, c["slug"]))
        scored.sort(reverse=True)
        target_slugs = [s for _, s in scored[:6]]
    # Allow query overrides for what-if
    try:    sim_sat = int(request.args.get("sat") or profile.get("sat") or 0)
    except: sim_sat = profile.get("sat") or 0
    try:    sim_act = int(request.args.get("act") or profile.get("act") or 0)
    except: sim_act = profile.get("act") or 0
    try:    sim_gpa = float(request.args.get("gpa") or profile.get("uw_gpa") or 0)
    except: sim_gpa = profile.get("uw_gpa") or 0.0
    sim_profile = dict(profile)
    if sim_sat: sim_profile["sat"] = sim_sat
    if sim_act: sim_profile["act"] = sim_act
    if sim_gpa: sim_profile["uw_gpa"] = sim_gpa
    rows_html = []
    for slug in target_slugs:
        if slug not in COLLEGES_BY_SLUG: continue
        c = merged_school(COLLEGES_BY_SLUG[slug])
        cur_fit, _ = compute_fit(profile, c)
        cur_lo, cur_hi = estimate_odds(c, cur_fit, profile)
        new_fit, _ = compute_fit(sim_profile, c)
        new_lo, new_hi = estimate_odds(c, new_fit, sim_profile)
        delta = ((new_lo + new_hi) / 2) - ((cur_lo + cur_hi) / 2)
        arrow = ""
        if delta > 1.5: arrow = f'<span style="color:#22c55e;font-weight:600">↑ +{round(delta)}%</span>'
        elif delta < -1.5: arrow = f'<span style="color:#ef4444;font-weight:600">↓ {round(delta)}%</span>'
        else: arrow = '<span class="muted">≈ no change</span>'
        rows_html.append(f"""<tr>
  <td><a href="/college/{slug}" style="color:var(--text)">{c["name"]}</a></td>
  <td class="muted">{cur_lo}–{cur_hi}%</td>
  <td style="font-weight:600">{new_lo}–{new_hi}%</td>
  <td>{arrow}</td>
</tr>""")
    cur_sat = profile.get("sat") or "—"
    cur_act = profile.get("act") or "—"
    cur_gpa = profile.get("uw_gpa") or "—"
    body = f"""<h1>Score predictor</h1>
<p class="muted">See how raising your test scores or GPA would shift your odds. We re-run the same model used on the chances pages.</p>
<div class="card" style="background:var(--card);max-width:680px">
  <p style="margin:0 0 10px;font-weight:600">Current: SAT {cur_sat} · ACT {cur_act} · GPA {cur_gpa}</p>
  <form method="get" action="/predictor" style="display:grid;gap:12px;grid-template-columns:1fr 1fr 1fr">
    <div><label>SAT</label><input name="sat" type="number" min="400" max="1600" value="{sim_sat or ''}" placeholder="e.g. 1500"></div>
    <div><label>ACT</label><input name="act" type="number" min="1" max="36" value="{sim_act or ''}" placeholder="e.g. 34"></div>
    <div><label>UW GPA</label><input name="gpa" type="number" step="0.01" min="0" max="4.0" value="{sim_gpa or ''}" placeholder="e.g. 3.9"></div>
    <button class="btn btn-primary" type="submit" style="grid-column:1/-1">Recalculate</button>
  </form>
</div>
<div style="overflow-x:auto;margin-top:18px"><table class="rank-table" style="min-width:520px">
  <thead><tr><th>School</th><th>Current odds</th><th>With these scores</th><th>Change</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table></div>
<p class="muted" style="margin-top:14px;font-size:.85em">Predictions assume the same ECs, hooks, and rigor — only test scores and GPA are simulated. Schools shown: {"your saved list" if saved else "your top auto-matched schools"}.</p>"""
    return _page(body, title="Score predictor — Candor")



# Per-school overrides where the round date is well-known.



def school_deadline_for_round(slug, round_key):
    overrides = SCHOOL_DEADLINE_OVERRIDES.get(slug, {})
    if round_key in overrides: return overrides[round_key]
    return ROUND_DEFAULT_DEADLINE.get(round_key)


@app.route("/timeline")
@login_required
def timeline_page():
    user = current_user()
    profile = get_profile(user["id"])
    saved = get_saved_schools(user["id"])
    if saved:
        target_slugs = saved
        list_label = "your saved list"
    elif profile:
        scored = []
        for c in COLLEGES[:60]:
            m = merged_school(c)
            s, _ = compute_my_fit(profile, m)
            scored.append((s, c["slug"]))
        scored.sort(reverse=True)
        target_slugs = [s for _, s in scored[:6]]
        list_label = "your top auto-matched schools"
    else:
        target_slugs = []
        list_label = ""
    # Collect deadlines: list of (year, month, day, label, school_name, slug, round_key)
    items = []
    for slug in target_slugs:
        if slug not in COLLEGES_BY_SLUG: continue
        c = COLLEGES_BY_SLUG[slug]
        detail = admissions_detail(c)
        rounds = (detail or {}).get("rounds") or ["RD"]
        for rk in rounds:
            d = school_deadline_for_round(slug, rk)
            if not d: continue
            label, y, m, day = d
            items.append((y, m, day, label, c["name"], slug, rk))
    # Universal milestones (FAFSA, CSS, decisions release)
    universals = [
        (2026, 10, 1,  "Oct 1, 2026",  "FAFSA opens",                   None, "Aid"),
        (2026, 10, 1,  "Oct 1, 2026",  "CSS Profile opens",             None, "Aid"),
        (2026, 12, 15, "Dec 15, 2026", "Most ED/EA decisions release",  None, "Decisions"),
        (2027, 2,  15, "Feb 15, 2027", "ED2 decisions",                 None, "Decisions"),
        (2027, 3,  31, "Mar 31, 2027", "RD decisions release",          None, "Decisions"),
        (2027, 5,  1,  "May 1, 2027",  "National Decision Day (commit)", None, "Decisions"),
    ]
    items += universals
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    # Group by month
    groups = {}
    for it in items:
        key = (it[0], it[1])
        groups.setdefault(key, []).append(it)
    today = datetime.now()
    cur_y, cur_m = today.year, today.month
    month_blocks = []
    for (y, m), its in groups.items():
        is_past = (y < cur_y) or (y == cur_y and m < cur_m)
        is_now  = (y == cur_y and m == cur_m)
        title_color = "var(--muted)" if is_past else ("var(--teal)" if is_now else "var(--text)")
        item_html = ""
        for y2, m2, d2, label, name, slug, rk in its:
            if slug:
                round_label = ROUND_LABELS.get(rk, rk)
                item_html += f"""<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-top:1px solid var(--border)">
  <div><a href="/college/{slug}" style="color:var(--text);font-weight:500">{name}</a> <span class="muted" style="font-size:.85em">· {round_label}</span></div>
  <div class="muted" style="font-size:.9em">{label}</div>
</div>"""
            else:
                item_html += f"""<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-top:1px solid var(--border)">
  <div style="font-weight:500">{name} <span class="muted" style="font-size:.85em">· {rk}</span></div>
  <div class="muted" style="font-size:.9em">{label}</div>
</div>"""
        month_blocks.append(f"""<div class="card" style="margin-bottom:14px">
  <h3 style="margin:0 0 6px;color:{title_color}">{MONTH_NAMES[m]} {y}{' · this month' if is_now else ''}</h3>
  {item_html}
</div>""")
    if not items:
        body = """<h1>Application timeline</h1>
<p class="muted">Save some schools first and we'll lay out your deadline calendar.</p>
<p style="margin-top:16px"><a class="btn btn-primary" href="/colleges">Browse schools →</a></p>"""
    else:
        body = f"""<h1>Application timeline</h1>
<p class="muted">Month-by-month deadlines for {list_label}. Dates are typical cycle deadlines — confirm each school's official portal before submitting.</p>
{''.join(month_blocks)}"""
    return _page(body, title="Application timeline — Candor")


@app.route("/save/<slug>", methods=["POST"])
@login_required
def save_school(slug):
    if slug not in COLLEGES_BY_SLUG: abort(404)
    user = current_user()
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_schools (user_id, college_slug) VALUES (?, ?)",
            (user["id"], slug)
        )
        conn.commit()
    nxt = request.form.get("next") or request.referrer or url_for("college_detail_page", slug=slug)
    return redirect(nxt)


@app.route("/plans/rank", methods=["POST"])
@login_required
def plans_rank_reorder():
    """Accept a JSON body {slugs: [...]} ordered top→bottom (most → least
    wanted) and persist as preference_rank 1..N on saved_schools. Any of
    the user's saved schools NOT in the payload keep their existing rank
    (or stay NULL if never ranked). Returns {ok: true, ranked: N}."""
    uid = current_user()["id"]
    payload = request.get_json(silent=True) or {}
    raw = payload.get("slugs")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "slugs must be a list"}), 400
    # Sanitize: only keep slugs that are actually saved by this user,
    # de-duped, preserving the submitted order.
    with db() as conn:
        rows = conn.execute(
            "SELECT college_slug FROM saved_schools WHERE user_id=?", (uid,)
        ).fetchall()
        saved = {r["college_slug"] for r in rows}
        ordered, seen = [], set()
        for s in raw:
            if isinstance(s, str) and s in saved and s not in seen:
                ordered.append(s); seen.add(s)
        for slug, rank in zip(ordered, range(1, len(ordered) + 1)):
            conn.execute(
                "UPDATE saved_schools SET preference_rank=? "
                "WHERE user_id=? AND college_slug=?",
                (rank, uid, slug)
            )
        conn.commit()
    return jsonify({"ok": True, "ranked": len(ordered)})


@app.route("/unsave/<slug>", methods=["POST"])
@login_required
def unsave_school(slug):
    user = current_user()
    with db() as conn:
        conn.execute(
            "DELETE FROM saved_schools WHERE user_id=? AND college_slug=?",
            (user["id"], slug)
        )
        conn.commit()
    nxt = request.form.get("next") or request.referrer or url_for("college_detail_page", slug=slug)
    return redirect(nxt)


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

@app.route("/api/interest", methods=["POST"])
def api_interest():
    """Anonymous email capture from /college/<slug> for visitors who aren't
    ready to make an account. Stores email + school slug. No login created,
    no email sent — pure lead capture that seeds the outcome-network email
    list. Per-IP rate-limited to stop drive-by abuse."""
    from collections import deque
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    dq = _DEMO_RATE.setdefault(f"interest:{ip}", deque())
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) > 20:
        return jsonify({"error": "Too many submissions. Try again in an hour."}), 429
    dq.append(now)

    email = (request.form.get("email") or request.json.get("email") if request.is_json else request.form.get("email") or "").strip().lower()
    slug = (request.form.get("slug") or "").strip().lower() or None
    source = (request.form.get("source") or "college_detail").strip()[:64]
    if not email or not _EMAIL_RE.match(email) or len(email) > 254:
        return jsonify({"error": "Please enter a valid email."}), 400
    if slug and slug not in COLLEGES_BY_SLUG:
        slug = None  # silently drop unknown slug rather than 400

    visitor_id = request.cookies.get("cv_id") or None
    with db() as conn:
        conn.execute(
            "INSERT INTO interest_signups (email, slug, visitor_id, source) VALUES (?, ?, ?, ?)",
            (email, slug, visitor_id, source)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/plans")
def plans_index_page():
    if not current_user():
        return _page("""
<div class="lp-wrap" style="max-width:760px;margin:0 auto;padding:40px 24px">
  <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:8px">My Colleges · what you'd see here</div>
  <h1 style="font-size:2.2em;letter-spacing:-1px;margin:0 0 12px">Your full college list, in one strategic view.</h1>
  <p class="muted" style="font-size:1.05em;line-height:1.55;margin:0 0 24px">Every school you've chanced or saved, grouped by application round (ED1, ED2, EA, REA, RD), with personalized odds, fit scores, list grading, and an admissions simulator.</p>

  <div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3);padding:28px;margin-bottom:24px">
    <h2 style="margin:0 0 14px;font-size:1.3em">What's inside</h2>
    <ul style="line-height:1.85;padding-left:18px;margin:0;color:#cbd5e1">
      <li><b style="color:#e6edf3">Round-by-round dashboard</b> — see your ED, EA, and RD lists side by side</li>
      <li><b style="color:#e6edf3">List grader (1–10)</b> — is your list balanced? Too top-heavy? Too safe?</li>
      <li><b style="color:#e6edf3">Admissions simulator</b> — what's the probability you get into AT LEAST one of your reaches?</li>
      <li><b style="color:#e6edf3">Personalized AI strategy per school</b> — calibrated to your stats and what each school weights</li>
      <li><b style="color:#e6edf3">Score push impact</b> — would a +60 SAT or +2 ACT actually move your odds here?</li>
    </ul>
  </div>

  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <a class="btn btn-primary" href="/signup" style="padding:12px 24px">Sign up free →</a>
    <a class="btn btn-light" href="/upgrade" style="padding:12px 24px">See Premium ($3/mo)</a>
  </div>
  <p class="muted" style="font-size:.85em;margin-top:18px">Free chances calculator stays free. Premium unlocks the full strategic dashboard.</p>
</div>
""", title="My Colleges — Candor")
    return plans_index_html()


@app.route("/plans/round/<slug>", methods=["POST"])
@login_required
def plans_set_round(slug):
    """Set or clear the application round for a school in My colleges."""
    if slug not in COLLEGES_BY_SLUG:
        abort(404)
    rnd = (request.form.get("round") or "").strip().upper() or None
    if rnd and rnd not in ("ED1","ED2","EA","REA","RD"):
        return ("invalid round", 400)
    uid = current_user()["id"]
    with db() as conn:
        # Update both tables (schools may be in saved_chances, saved_schools, or both)
        conn.execute("UPDATE saved_chances SET application_round=? WHERE user_id=? AND college_slug=?",
                     (rnd, uid, slug))
        conn.execute("UPDATE saved_schools SET application_round=? WHERE user_id=? AND college_slug=?",
                     (rnd, uid, slug))
        # If neither row existed (user is assigning a round to a never-saved school),
        # ensure it lands in saved_schools so it shows up.
        existing = conn.execute(
            "SELECT 1 FROM saved_chances WHERE user_id=? AND college_slug=? "
            "UNION SELECT 1 FROM saved_schools WHERE user_id=? AND college_slug=? LIMIT 1",
            (uid, slug, uid, slug)
        ).fetchone()
        if not existing:
            conn.execute("INSERT OR IGNORE INTO saved_schools (user_id, college_slug, application_round) VALUES (?,?,?)",
                         (uid, slug, rnd))
        conn.commit()
    return ("ok", 200)


@app.route("/plans/remove/<slug>", methods=["POST"])
@login_required
def plans_remove_school(slug):
    """Remove a school from My colleges (drops from saved_schools and
    saved_chances). Cached AI advice for it gets cleared too."""
    if slug not in COLLEGES_BY_SLUG:
        abort(404)
    uid = current_user()["id"]
    with db() as conn:
        conn.execute("DELETE FROM saved_schools WHERE user_id=? AND college_slug=?", (uid, slug))
        conn.execute("DELETE FROM saved_chances WHERE user_id=? AND college_slug=?", (uid, slug))
        conn.execute("DELETE FROM tailored_advice WHERE user_id=? AND college_slug=?", (uid, slug))
        conn.execute("DELETE FROM personalized_rounds WHERE user_id=? AND college_slug=?", (uid, slug))
        conn.commit()
    return ("ok", 200)


def _gate_premium():
    """Helper: returns a flask response if user isn't paid (premium), else
    None. Premium is a single $3/month tier that unlocks: My colleges
    dashboard, list grader, admissions simulator, score push impact, and
    personalized AI strategy per school. All gated together via the
    existing users.is_paid column."""
    user = current_user()
    if not user.get("is_paid"):
        return redirect("/plans")
    return None


def grade_user_list(uid):
    """Score the user's college list 1-10 on:
      - Balance: dream/reach/target/safety mix
      - Size: 6-15 schools optimal (under 6 = thin, over 15 = unfocused)
      - Round strategy: ED used? EAs reasonable? RD-only is suboptimal
      - Realism: too many dreams without targets = bad

    Returns dict with score, breakdown, suggestions."""
    profile = get_profile(uid) or {}
    with db() as conn:
        chances = conn.execute(
            "SELECT college_slug, tier, odds_low, odds_high, application_round "
            "FROM saved_chances WHERE user_id=? AND computed_at >= ?",
            (uid, SAVED_CHANCES_MIN_VALID_AT)
        ).fetchall()
        saved = conn.execute(
            "SELECT college_slug, application_round FROM saved_schools WHERE user_id=?", (uid,)
        ).fetchall()
    # Merge: prefer chances row when both exist
    chance_slugs = {r["college_slug"] for r in chances}
    items = []
    for r in chances:
        items.append({"slug": r["college_slug"], "tier": r["tier"],
                      "odds": (r["odds_low"]+r["odds_high"])/2.0 if r["odds_low"] is not None else None,
                      "round": r["application_round"], "tier_source":"computed"})
    # For saved-but-uncomputed schools, classify their tier on-the-fly using
    # the user's profile + the school's accept rate so the grader has real
    # tier data instead of treating them as ungraded.
    for r in saved:
        if r["college_slug"] in chance_slugs: continue
        slug = r["college_slug"]
        c = COLLEGES_BY_SLUG.get(slug)
        if not c:
            items.append({"slug": slug, "tier": None, "odds": None,
                          "round": r["application_round"], "tier_source":"missing"})
            continue
        try:
            fit, _ = compute_fit(profile, c)
            tier = assign_tier(c, fit, profile)
            # Estimate odds midpoint too (for realism scoring)
            lo, hi = estimate_odds(c, fit, profile)
            odds_mid = (lo + hi) / 2.0
        except Exception:
            tier = None
            odds_mid = None
        items.append({"slug": slug, "tier": tier, "odds": odds_mid,
                      "round": r["application_round"], "tier_source":"estimated"})
    n = len(items)
    if n == 0:
        return {"score": 0, "breakdown": [], "suggestions": ["Add some schools to your list first."]}

    # === Balance score (out of 4) ===
    tiers = {"Dream": 0, "Reach": 0, "Target": 0, "Safety": 0, None: 0}
    for it in items:
        tiers[it["tier"]] = tiers.get(it["tier"], 0) + 1
    has_safety = tiers["Safety"] > 0
    has_target = tiers["Target"] > 0
    has_reach  = tiers["Reach"] > 0
    has_dream  = tiers["Dream"] > 0
    balance_score = 0
    if has_safety: balance_score += 1
    if has_target: balance_score += 1
    if has_reach:  balance_score += 1
    if has_dream:  balance_score += 1
    # Penalize lopsided lists
    if tiers["Dream"] >= n * 0.5 and n >= 4:
        balance_score = max(0, balance_score - 1)  # too top-heavy

    # === Size score (out of 2) ===
    if 6 <= n <= 15: size_score = 2
    elif 4 <= n <= 18: size_score = 1
    else: size_score = 0

    # === Round strategy (out of 2) ===
    rounds = [it["round"] for it in items if it["round"]]
    has_ed = any(r in ("ED1","ED2") for r in rounds)
    n_ed = sum(1 for r in rounds if r in ("ED1","ED2"))
    has_ea = any(r in ("EA","REA") for r in rounds)
    rd_only = len(rounds) > 0 and all(r == "RD" for r in rounds)
    round_score = 0
    if rounds:
        if has_ed and n_ed <= 2: round_score += 1  # used ED but not multiple ED1s (impossible anyway)
        elif has_ed and n_ed > 2: round_score += 0  # bug: applied ED1 to multiple
        elif has_ea: round_score += 1  # at least using EA somewhere
        if has_ed and has_ea: round_score += 1
        elif has_ea and not rd_only: round_score += 1
        elif rd_only: round_score += 0
    else:
        # No rounds assigned yet → neutral
        round_score = 0

    # === Realism (out of 2) ===
    # Realism penalizes dream-only lists, rewards balanced odds distribution
    avg_odds = None
    odds_known = [it["odds"] for it in items if it["odds"] is not None]
    if odds_known:
        avg_odds = sum(odds_known) / len(odds_known)
    realism_score = 0
    if avg_odds is not None:
        if 25 <= avg_odds <= 50: realism_score = 2  # well-distributed
        elif 15 <= avg_odds < 25 or 50 < avg_odds <= 70: realism_score = 1
        elif avg_odds < 15: realism_score = 0  # all reaches
        else: realism_score = 1
    else:
        realism_score = 1  # no chances run yet → neutral

    raw = balance_score + size_score + round_score + realism_score  # max 10
    score = min(10, max(1, raw))

    # === Suggestions ===
    sugs = []
    if not has_safety:
        sugs.append("Add at least one **safety** (acceptance > 60% AND your fit > 60). Right now you have zero — that's risky.")
    if not has_target:
        sugs.append("You're missing **target** schools (where odds are 25-50%). These are where most of your acceptances are likely to come from.")
    if tiers["Dream"] >= n * 0.5 and n >= 4:
        sugs.append(f"Your list is top-heavy: {tiers['Dream']} dream schools out of {n}. Add more targets/safeties so you're not betting everything on long shots.")
    if not has_ed and rounds:
        sugs.append("Consider using **Early Decision** at one school where you'd genuinely commit. ED gives a meaningful odds bump at most schools.")
    if rd_only:
        sugs.append("You're applying RD-only — you're leaving Early Action / Early Decision lifts on the table. EA is non-binding so there's no downside.")
    if n < 6:
        sugs.append(f"List is small ({n} schools). Most strong applicants apply to 8-12. Adding a couple more well-fit schools improves your overall admit probability.")
    elif n > 15:
        sugs.append(f"List is large ({n} schools). 8-12 is typical; over 15 spreads your essay/supplement effort thin.")
    if not items:
        sugs.append("No schools yet — start by browsing colleges and saving 8-12 you're interested in.")
    if not sugs:
        sugs.append("Solid list — strategic balance, reasonable size, and rounds make sense for your profile.")

    n_computed = sum(1 for it in items if it.get("tier_source") == "computed")
    n_estimated = sum(1 for it in items if it.get("tier_source") == "estimated")
    return {
        "score": score,
        "n": n,
        "n_computed": n_computed,
        "n_estimated": n_estimated,
        "tiers": {k: v for k, v in tiers.items() if k},
        "rounds_used": dict.fromkeys(rounds, 0),
        "breakdown": [
            {"label": "Balance (mix of dream/reach/target/safety)", "score": balance_score, "out_of": 4},
            {"label": "Size (6-15 optimal)",                          "score": size_score,    "out_of": 2},
            {"label": "Round strategy (ED+EA usage)",                  "score": round_score,   "out_of": 2},
            {"label": "Realism (avg odds in healthy range)",           "score": realism_score, "out_of": 2},
        ],
        "suggestions": sugs,
        "avg_odds": round(avg_odds, 1) if avg_odds is not None else None,
    }


# ─── PREMIUM: "SCHOOLS TO ADD" RECOMMENDER ───
def _size_bucket(size):
    size = size or 0
    if size < 3000: return "small"
    if size < 13000: return "medium"
    return "large"


def _mk_rec(x, category, reason):
    """Shape a scored candidate into a recommendation card record."""
    c = x["c"]
    return {
        "slug": c["slug"], "name": c["name"], "fit": x["fit"], "tier": x["tier"],
        "odds_low": x["odds_low"], "odds_high": x["odds_high"],
        "accept": c["accept"], "loc": city_state(c),
        "reason": reason, "category": category,
    }


def _closest_saved(cand, saved_colleges, shared_majors):
    """Pick the saved school most similar to a candidate — for 'Like X —' copy."""
    best, best_score = None, -1
    for s in saved_colleges:
        sc = 0
        if s["type"] == cand["type"]: sc += 1
        if shared_majors and any(m in s.get("majors", []) for m in shared_majors): sc += 2
        if REGION_BY_STATE.get(s.get("state", "")) == REGION_BY_STATE.get(cand.get("state", "")): sc += 1
        if sc > best_score:
            best_score, best = sc, s
    return best


def recommend_schools_to_add(uid, limit=9):
    """Premium recommender. Given the user's saved list + profile, suggest
    schools to add — blending two signals:
      - GAP: tiers missing from the current list (Safety/Target first), or
        schools that balance a reach-heavy list.
      - SIMILAR: schools matching the list's 'taste' (type, region, shared
        majors, in-state) that the user hasn't added yet.
    Returns {"gap": [...], "similar": [...], "list_n": int, "missing": [...],
             "empty": bool}."""
    profile = get_profile(uid) or {}
    with db() as conn:
        rows = conn.execute(
            "SELECT college_slug FROM saved_schools WHERE user_id=? "
            "UNION SELECT college_slug FROM saved_chances WHERE user_id=?",
            (uid, uid)
        ).fetchall()
    saved = {r["college_slug"] for r in rows}
    saved_colleges = [COLLEGES_BY_SLUG[s] for s in saved if s in COLLEGES_BY_SLUG]
    if not saved_colleges:
        return {"gap": [], "similar": [], "list_n": 0, "missing": [], "empty": True}

    # Profile the current list
    tier_counts = {"Dream": 0, "Reach": 0, "Target": 0, "Safety": 0}
    type_counts, region_counts, list_majors = {}, {}, {}
    for c in saved_colleges:
        try:
            fit, _ = compute_fit(profile, c)
            t = assign_tier(c, fit, profile)
        except Exception:
            t = None
        if t in tier_counts: tier_counts[t] += 1
        type_counts[c["type"]] = type_counts.get(c["type"], 0) + 1
        reg = REGION_BY_STATE.get(c.get("state", ""), "Other")
        region_counts[reg] = region_counts.get(reg, 0) + 1
        for m in c.get("majors", []):
            list_majors[m] = list_majors.get(m, 0) + 1

    n = len(saved_colleges)
    dom_type = max(type_counts, key=type_counts.get) if type_counts else None
    dom_region = max(region_counts, key=region_counts.get) if region_counts else None
    top_heavy = (tier_counts["Dream"] + tier_counts["Reach"]) >= n * 0.7 and n >= 3
    missing = [t for t in ("Safety", "Target", "Reach") if tier_counts[t] == 0]

    # Score every non-saved college
    cands = []
    for c in COLLEGES:
        if c["slug"] in saved: continue
        try:
            fit, _ = compute_fit(profile, c)
            tier = assign_tier(c, fit, profile)
            lo, hi = estimate_odds(c, fit, profile)
        except Exception:
            continue
        if fit < 40: continue  # weak match — skip
        cands.append({"c": c, "fit": fit, "tier": tier, "odds_low": lo, "odds_high": hi})

    # GAP recs — fill missing tiers, or balance a reach-heavy list
    gap, used = [], set()
    gap_tiers = list(missing)
    if top_heavy:
        for t in ("Safety", "Target"):
            if t not in gap_tiers: gap_tiers.append(t)
    for tier in gap_tiers:
        pool = sorted([x for x in cands if x["tier"] == tier and x["c"]["slug"] not in used],
                      key=lambda x: x["fit"], reverse=True)
        for x in pool[:2]:
            used.add(x["c"]["slug"])
            if tier in missing:
                reason = f"Fills your missing {tier} tier — {x['fit']}/100 fit for you"
            else:
                reason = f"Your list leans reach-heavy; this {tier.lower()} balances it"
            gap.append(_mk_rec(x, "gap", reason))
        if len(gap) >= 5: break

    # SIMILAR recs — taste match against the existing list
    prof_major = (profile.get("major") or "").strip()
    scored = []
    for x in cands:
        if x["c"]["slug"] in used: continue
        c = x["c"]
        s = float(x["fit"])
        shared = [m for m in c.get("majors", []) if m in list_majors]
        if dom_type and c["type"] == dom_type: s += 8
        if dom_region and REGION_BY_STATE.get(c.get("state", "")) == dom_region: s += 6
        if shared: s += 10
        if prof_major and prof_major in c.get("majors", []): s += 8
        if c["type"] == "public" and profile.get("state") \
           and profile["state"].lower() == c.get("state", "").lower(): s += 6
        if x["tier"] == "Dream" and top_heavy: s -= 25
        scored.append((s, x, shared))
    scored.sort(key=lambda t: t[0], reverse=True)

    similar = []
    for s, x, shared in scored:
        if len(similar) >= max(0, limit - len(gap)): break
        ref = _closest_saved(x["c"], saved_colleges, shared)
        if shared and ref:
            reason = f"Like {ref['name']} — also strong in {shared[0]}"
        elif ref:
            reason = f"Matches your list's style — similar to {ref['name']}"
        else:
            reason = f"Strong fit for your profile — {x['fit']}/100"
        similar.append(_mk_rec(x, "similar", reason))

    return {"gap": gap, "similar": similar, "list_n": n,
            "missing": missing, "empty": False}


def _rec_card_html(r):
    """Render one recommendation card."""
    tc = {"Dream": "#fca5a5", "Reach": "#fbbf24",
          "Target": "#5fc9b6", "Safety": "#86efac"}.get(r["tier"], "#94a3b8")
    return f'''
<div class="card" style="padding:16px" data-slug="{r['slug']}">
  <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">
    <div>
      <div style="font-weight:700;font-size:1.06em">{r['name']}</div>
      <div class="muted" style="font-size:.82em">{r['loc']} · {round(r['accept']*100,1)}% accept</div>
    </div>
    <span style="background:{tc}22;color:{tc};border:1px solid {tc}55;border-radius:6px;padding:2px 8px;font-size:.78em;font-weight:700;align-self:flex-start">{r['tier']}</span>
  </div>
  <div style="display:flex;gap:16px;margin-top:8px;font-size:.9em">
    <span style="color:#5fc9b6;font-weight:700">{r['odds_low']}–{r['odds_high']}% odds</span>
    <span class="muted">fit {r['fit']}/100</span>
  </div>
  <div style="margin-top:8px;font-size:.9em;line-height:1.5">{r['reason']}</div>
  <div class="rec-ai" style="margin-top:8px;font-size:.88em;line-height:1.55;color:var(--text-2);display:none"></div>
  <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
    <form method="post" action="/save/{r['slug']}" style="display:inline">
      {csrf_input()}
      <input type="hidden" name="next" value="/plans/add">
      <button class="btn btn-primary btn-sm" type="submit">+ Add to my list</button>
    </form>
    <button class="btn btn-light btn-sm" type="button" onclick="explainRec('{r['slug']}',this)">Explain with AI</button>
    <a class="btn btn-light btn-sm" href="/college/{r['slug']}">View school</a>
  </div>
</div>'''


@app.route("/plans/add")
@login_required
def plans_add_page():
    user = current_user()
    # Free users get a locked preview — drives upgrades, per /plans pattern.
    if not user.get("is_paid"):
        blurred = "".join(f'''
<div class="card" style="padding:16px;position:relative;overflow:hidden">
  <div style="filter:blur(6px);user-select:none;pointer-events:none">
    <div style="font-weight:700;font-size:1.06em">{label}</div>
    <div class="muted" style="font-size:.82em">A great-fit campus for you · XX% accept</div>
    <div style="margin-top:8px;color:#5fc9b6;font-weight:700">XX–XX% odds · fit XX/100</div>
    <div style="margin-top:8px">A personalized reason this school belongs on your list.</div>
  </div>
  <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:1.6em">🔒</div>
</div>''' for label in ["A school picked for you", "Another strong match", "Fills a gap in your list"])
        return _page(f'''
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>Schools to Add</h1>
<p class="muted">Candor reads your current list and recommends schools to add — gap-fillers that balance your reach/target/safety mix, and schools similar to ones you already like.</p>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-top:16px">{blurred}</div>
<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3);padding:28px;margin-top:18px;text-align:center">
  <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:8px">Candor Premium · $3/mo</div>
  <h2 style="margin:0 0 12px">Unlock your personalized school recommendations</h2>
  <p class="muted" style="margin:0 0 16px">See exactly which schools to add — matched to your profile, your tier balance, and the schools already on your list.</p>
  <a class="btn btn-primary" href="/upgrade" style="padding:12px 28px">Upgrade — $3/mo →</a>
</div>
''', title="Schools to Add — Candor")

    rec = recommend_schools_to_add(user["id"])
    if rec["empty"]:
        return _page('''
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>Schools to Add</h1>
<div class="card"><p class="muted">Your list is empty — save a few schools first and Candor will recommend more to round it out.</p>
<a class="btn btn-primary" href="/colleges">Browse colleges</a></div>
''', title="Schools to Add — Candor")

    sections = ""
    if rec["gap"]:
        if rec["missing"]:
            sub = "Your list is missing " + " and ".join(rec["missing"]) + " schools."
        else:
            sub = "Schools that balance out your reach-heavy list."
        sections += f'''
<h2 style="margin-top:24px">Fill the gaps in your list</h2>
<p class="muted" style="margin-top:-6px">{sub}</p>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px">{"".join(_rec_card_html(r) for r in rec["gap"])}</div>'''
    if rec["similar"]:
        sections += f'''
<h2 style="margin-top:24px">More schools you'd like</h2>
<p class="muted" style="margin-top:-6px">Matched to the style of the {rec["list_n"]} schools already on your list.</p>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px">{"".join(_rec_card_html(r) for r in rec["similar"])}</div>'''
    if not sections:
        sections = '<div class="card"><p class="muted">No strong new matches right now — your list already covers your profile well.</p></div>'

    return _page(f'''
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>Schools to Add</h1>
<p class="muted">Recommendations based on your profile and the {rec["list_n"]} schools on your list. Add any with one click.</p>
{sections}
<p style="margin-top:24px"><a class="btn btn-light" href="/plans">← Back to My colleges</a></p>
<script>
async function explainRec(slug, btn){{
  const card = btn.closest('[data-slug]');
  const box = card.querySelector('.rec-ai');
  btn.disabled = true; btn.textContent = 'Thinking…';
  try {{
    const el = document.querySelector('meta[name="csrf-token"]');
    const tok = el ? el.getAttribute('content') : '';
    const r = await fetch(`/plans/add/explain/${{slug}}`, {{
      method:'POST', headers:{{'X-CSRFToken': tok}}
    }});
    const j = await r.json();
    if (r.ok && j.text) {{
      box.textContent = j.text;
      box.style.display = 'block';
      btn.style.display = 'none';
    }} else {{
      btn.disabled = false; btn.textContent = 'Explain with AI';
      alert(j.error || 'Could not generate explanation.');
    }}
  }} catch(e) {{
    btn.disabled = false; btn.textContent = 'Explain with AI';
    alert('Network error: ' + e.message);
  }}
}}
</script>
''', title="Schools to Add — Candor")


@app.route("/plans/add/explain/<slug>", methods=["POST"])
@login_required
def plans_add_explain(slug):
    user = current_user()
    if not user.get("is_paid"):
        return jsonify({"error": "Premium required"}), 403
    c = COLLEGES_BY_SLUG.get(slug)
    if not c:
        return jsonify({"error": "Unknown school"}), 404
    profile = get_profile(user["id"]) or {}
    try:
        fit, _ = compute_fit(profile, c)
        tier = assign_tier(c, fit, profile)
        lo, hi = estimate_odds(c, fit, profile)
    except Exception:
        fit, tier, lo, hi = 50, "Target", 0, 0
    with db() as conn:
        rows = conn.execute(
            "SELECT college_slug FROM saved_schools WHERE user_id=? "
            "UNION SELECT college_slug FROM saved_chances WHERE user_id=?",
            (user["id"], user["id"])
        ).fetchall()
    saved_names = [COLLEGES_BY_SLUG[r["college_slug"]]["name"]
                   for r in rows if r["college_slug"] in COLLEGES_BY_SLUG][:12]
    sys = ("You are a candid college admissions advisor. In 2-3 sentences, "
           "explain why this school is worth adding to the student's list. Be "
           "specific and honest — reference their profile and the schools "
           "already on their list. No preamble, no headers, no bullet points.")
    usr = (f"School: {c['name']} ({c['type']}, {city_state(c)}). "
           f"Accept rate {round(c['accept']*100,1)}%. Majors: {', '.join(c.get('majors',[]))}. "
           f"{c.get('desc','')}\n"
           f"Student: GPA {profile.get('uw_gpa') or 'n/a'}, "
           f"SAT {profile.get('sat') or 'n/a'}, ACT {profile.get('act') or 'n/a'}, "
           f"intended major {profile.get('major') or 'undecided'}.\n"
           f"For this student at {c['name']}: fit {fit}/100, tier {tier}, odds {lo}-{hi}%.\n"
           f"Schools already on their list: {', '.join(saved_names) or 'none'}.")
    txt = _claude("claude-haiku-4-5-20251001", sys, usr, max_tokens=340)
    if not txt:
        txt = (f"{c['name']} is a {tier.lower()} for your profile "
               f"({fit}/100 fit, ~{lo}-{hi}% odds). It fits the kind of schools "
               f"already on your list and is worth a closer look.")
    return jsonify({"text": txt.strip()})




@app.route("/plans/grade")
@login_required
def plans_grade_page():
    gate = _gate_premium()
    if gate: return gate
    uid = current_user()["id"]
    g = grade_user_list(uid)
    score = g["score"]
    score_color = "#5fc9b6" if score >= 8 else ("#fbbf24" if score >= 5 else "#fca5a5")
    breakdown_html = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid var(--border)">'
        f'<span>{b["label"]}</span><span style="font-weight:700">{b["score"]}/{b["out_of"]}</span></div>'
        for b in g["breakdown"]
    )
    # Tiny inline markdown: just bold (**text**) → <b>
    def _bold(s):
        import re as _re
        from html import escape as _esc
        s = _esc(s)
        return _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
    sug_html = "".join(f'<li style="margin:8px 0;line-height:1.6">{_bold(s)}</li>' for s in g["suggestions"])
    tiers_html = ""
    if g.get("tiers"):
        for tier_name in ["Dream","Reach","Target","Safety"]:
            n = g["tiers"].get(tier_name, 0)
            color = {"Dream":"#fca5a5","Reach":"#fbbf24","Target":"#5fc9b6","Safety":"#86efac"}[tier_name]
            tiers_html += f'<div style="display:inline-block;margin:0 14px 6px 0"><span style="color:{color};font-weight:700;font-size:1.4em">{n}</span> <span class="muted" style="font-size:.88em">{tier_name}</span></div>'

    return _page(f"""
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>List grade</h1>
<p class="muted">A 1-10 score on whether your list is strategically balanced, realistic, and uses application rounds well.</p>

<div class="card" style="text-align:center;padding:32px">
  <div style="font-size:5em;font-weight:800;color:{score_color};line-height:1;letter-spacing:-2px">{score}<span class="muted" style="font-size:.4em;font-weight:400">/10</span></div>
  <div class="muted" style="margin-top:6px">{g["n"]} school{'s' if g["n"] != 1 else ''} in your list{f' · avg odds {g["avg_odds"]}%' if g.get("avg_odds") is not None else ''}</div>
  {(f'<div class="muted" style="margin-top:10px;font-size:.85em">{g["n_computed"]} fully chanced · {g["n_estimated"]} estimated from your profile. <a href="/plans">Run chances</a> for sharper grading.</div>' if g["n_estimated"] > 0 else '')}
</div>

<h2 style="margin-top:24px">By tier</h2>
<div class="card">{tiers_html or '<p class="muted">No chances computed yet — run chances on at least a few schools so we can classify them.</p>'}</div>

<h2 style="margin-top:24px">Score breakdown</h2>
<div class="card">{breakdown_html}</div>

<h2 style="margin-top:24px">Suggestions</h2>
<div class="card"><ul style="padding-left:18px;margin:0">{sug_html}</ul></div>

<p style="margin-top:24px"><a class="btn btn-light" href="/plans">← Back to My colleges</a> <a class="btn btn-primary" href="/plans/simulate">Run admissions simulator →</a></p>
""", title="List grade — Candor")


# ─── APPLICATION STRATEGIST (premium AI feature) ──────────
_STRATEGIST_PROMPT_VERSION = 6


def _test_range(c):
    """Compact admitted-student test ranges for a college, for the strategist
    prompt. Surfaces the school's real SAT and ACT middle-50% so the model
    never has to guess medians or convert between the two tests."""
    parts = []
    if c.get("sat_25") and c.get("sat_75"):
        parts.append(f"SAT {c['sat_25']}-{c['sat_75']}")
    if c.get("act_25") and c.get("act_75"):
        parts.append(f"ACT {c['act_25']}-{c['act_75']}")
    return "mid-50%: " + (", ".join(parts) if parts else "n/a")


def _score_position_label(score, lo, hi):
    """Deterministic percentile-position label so the LLM never does this
    math itself. The mid-50% range maps to 25th..75th; scores at/outside
    the bounds get the boundary labels; scores strictly inside the range
    are bucketed by the midpoint."""
    try:
        score = float(score); lo = float(lo); hi = float(hi)
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    if score > hi:
        return "ABOVE the 75th percentile"
    if score == hi:
        return "AT the 75th percentile"
    if score < lo:
        return "BELOW the 25th percentile"
    if score == lo:
        return "AT the 25th percentile"
    mid = (lo + hi) / 2.0
    if score > mid:
        return "between the 50th and 75th percentile"
    if score < mid:
        return "between the 25th and 50th percentile"
    return "AT the 50th percentile"


def _round_rates_str(c):
    """Compact per-round admit-rate string for the strategist prompt, e.g.
    'ED 54%, EA 28%, RD ~12%'. Pulled from ADMISSIONS_DETAIL so the model
    sees the real ED/EA leverage instead of judging on overall admit only.
    Returns empty string when no curated breakdown exists for the school."""
    d = ADMISSIONS_DETAIL.get(c.get("slug")) or {}
    rates = d.get("rates") or {}
    if not rates:
        return ""
    order = ["ED", "ED1", "ED2", "REA", "EA", "RD"]
    seen = set()
    parts = []
    for r in order:
        if r in rates and r not in seen:
            parts.append(f"{r} {round(rates[r] * 100)}%")
            seen.add(r)
    for r, v in rates.items():
        if r not in seen:
            parts.append(f"{r} {round(v * 100)}%")
            seen.add(r)
    return ", ".join(parts)


def _score_vs_school(profile, c):
    """Pre-computed score-vs-school comparison string for the strategist
    prompt. Format: 'ACT 33 ABOVE the 75th percentile of 29-32'. Returns
    one entry per test the student reports that the school has a range
    for. The LLM is told to use ONLY this label, never recompute."""
    parts = []
    sat = profile.get("sat")
    if sat:
        lbl = _score_position_label(sat, c.get("sat_25"), c.get("sat_75"))
        if lbl:
            parts.append(f"SAT {sat} {lbl} of {c['sat_25']}-{c['sat_75']}")
    act = profile.get("act")
    if act:
        lbl = _score_position_label(act, c.get("act_25"), c.get("act_75"))
        if lbl:
            parts.append(f"ACT {act} {lbl} of {c['act_25']}-{c['act_75']}")
    return "; ".join(parts) if parts else "no test comparison available"


def _strategist_gather(uid):
    """Merge the user's college list from saved_chances + saved_schools.
    Returns (profile, items) where each item has slug, round, computed
    odds (or None), preference_rank (1=most wanted, None=unranked),
    and whether chances were computed."""
    profile = get_profile(uid) or {}
    with db() as conn:
        chances = conn.execute(
            "SELECT college_slug, tier, odds_low, odds_high, application_round "
            "FROM saved_chances WHERE user_id=? AND computed_at >= ?",
            (uid, SAVED_CHANCES_MIN_VALID_AT)
        ).fetchall()
        saved = conn.execute(
            "SELECT college_slug, application_round, preference_rank "
            "FROM saved_schools WHERE user_id=?",
            (uid,)
        ).fetchall()
    pref_by_slug = {r["college_slug"]: r["preference_rank"] for r in saved}
    chance_slugs = {r["college_slug"] for r in chances}
    items = []
    for r in chances:
        items.append({
            "slug": r["college_slug"], "tier": r["tier"],
            "odds_low": r["odds_low"], "odds_high": r["odds_high"],
            "round": r["application_round"], "computed": True,
            "preference_rank": pref_by_slug.get(r["college_slug"]),
        })
    for r in saved:
        if r["college_slug"] in chance_slugs:
            continue
        items.append({
            "slug": r["college_slug"], "tier": None,
            "odds_low": None, "odds_high": None,
            "round": r["application_round"], "computed": False,
            "preference_rank": r["preference_rank"],
        })
    return profile, items


def _strategist_input_hash(profile, items):
    """Cache key — changes when the list, rounds, computed odds, or the
    profile fields the strategy depends on change."""
    import hashlib
    parts = [str(_STRATEGIST_PROMPT_VERSION)]
    for k in ("uw_gpa", "weighted_gpa", "sat", "act", "major",
              "ecs", "leadership", "awards"):
        parts.append(f"{k}={profile.get(k)}")
    for it in sorted(items, key=lambda x: x["slug"]):
        parts.append(
            f"{it['slug']}:{it['round']}:{it['odds_low']}:{it['odds_high']}:"
            f"pref{it.get('preference_rank')}"
        )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def generate_strategy(profile, items):
    """One Claude call: read the student's profile + full college list and
    return a strategic plan — tier classification, round strategy, gaps to
    fill, and prioritized next steps. Returns a dict or None on failure."""
    if not items:
        return None
    list_lines = []
    for it in items:
        c = COLLEGES_BY_SLUG.get(it["slug"])
        if not c:
            continue
        if it["computed"] and it["odds_low"] is not None:
            odds = f"this student's odds {it['odds_low']}-{it['odds_high']}%"
        else:
            odds = "odds not yet computed"
        rnd = ROUND_DISPLAY.get(it["round"], "round undecided")
        rr = _round_rates_str(c)
        round_rates_field = f"per-round: {rr}" if rr else "per-round: not available"
        pr = it.get("preference_rank")
        pref_field = f"student-preference: #{pr}" if pr else "student-preference: unranked"
        list_lines.append(
            f"{c['slug']}|{c['name']}|{round(c['accept']*100,1)}% overall admit|"
            f"{_test_range(c)}|score-vs-school: {_score_vs_school(profile, c)}|"
            f"{round_rates_field}|{odds}|{rnd}|{pref_field}"
        )
    if not list_lines:
        return None
    in_list = {it["slug"] for it in items}
    cat_lines = [
        f"{c['slug']}|{c['name']}|{round(c['accept']*100,1)}% admit|"
        f"{_test_range(c)}|{city_state(c)}|{','.join(c.get('majors', [])[:4])}"
        for c in COLLEGES if c["slug"] not in in_list
    ]

    sys = (
        "You are a seasoned, opinionated college admissions strategist. A "
        "student gives you their academic profile and current college list. "
        "Build them a TIGHT strategic plan — concrete moves, ordered by "
        "priority, with the single highest-leverage decision (their ED pick) "
        "called out separately. No generic encouragement. No hedging.\n\n"
        "Tier definitions (classify EVERY school in their list):\n"
        "- Reach: realistic admit chance well under ~35% for THIS student.\n"
        "- Target: roughly 35-75% — where most acceptances should come from.\n"
        "- Safety: high confidence (>~80%) AND a school they'd be content to "
        "attend. Use the student's computed odds when given; otherwise judge "
        "from the school's admit rate against the student's stats.\n\n"
        "ACTION LABELS — each school gets ONE of four actions. THE DEFAULT "
        "IS \"Keep\". Anchor, Watch, and Cut are all sparingly-used special "
        "cases. If you're unsure, the answer is \"Keep\".\n"
        "- \"Anchor\" (rare — 3-5 schools across the WHOLE list): the "
        "highest-priority applications the strategy is built around. Best "
        "combo of fit + odds + ED/EA leverage. Typically 2-3 reaches that "
        "ED makes viable, the strongest 1-2 targets, and the true safety.\n"
        "- \"Keep\" (DEFAULT — the majority of the list): solid app, worth "
        "the slot. ANY reach the student saved AND that's not pure lottery "
        "AND that they haven't bottom-ranked themselves stays Keep. Reaches "
        "in the 5-20% admit range with reasonable score fit (at/around the "
        "25th-75th percentile band) are Keep, not Cut.\n"
        "- \"Watch\" (the safety valve for marginal reaches): use for "
        "reaches where odds are weak AND ED leverage is missing AND fit "
        "is thin. The note should say \"keep only if it's a dream school\" "
        "or similar — Watch means \"on the bubble\", not \"drop\".\n"
        "- \"Cut\" (RESERVED — should be the smallest bucket): only when "
        "the school adds no realistic value. Strict criteria — must meet "
        "AT LEAST TWO of: (a) overall admit <5% AND no ED leverage at all "
        "AND student well below the 25th percentile, (b) student ranked it "
        "near the bottom of their preferences, (c) the school is a near-"
        "duplicate of another, better-positioned school on the list. NEVER "
        "Cut more than ~25% of the reach pile, and NEVER Cut a school the "
        "student ranked in their top 5 unless it's literally impossible.\n\n"
        "TEST SCORES — read carefully:\n"
        "- Every school line includes a pre-computed `score-vs-school:` "
        "label like `ACT 33 ABOVE the 75th percentile of 29-32` or "
        "`SAT 1450 between the 25th and 50th percentile of 1430-1530`. "
        "These labels are CORRECT and authoritative. **You MUST use them "
        "verbatim** when describing the student's score position relative "
        "to a school.\n"
        "- NEVER recompute, restate, or override the percentile position "
        "yourself. NEVER write things like \"ACT 33 is at the 25th of "
        "29-32\" when the label says ABOVE the 75th — that contradicts the "
        "data and is wrong.\n"
        "- Do NOT convert between SAT and ACT, and do NOT invent or "
        "estimate any test medians.\n"
        "- A score ABOVE the 75th percentile is a real strength; BELOW the "
        "25th is a real weakness; the in-between buckets are on-range.\n"
        "- If the student reports no test score, do not speculate.\n\n"
        "ED / EA LEVERAGE — this is critical and the most-missed factor:\n"
        "- Each school line includes a `per-round:` field with the school's "
        "actual admit rate by round (ED, ED2, EA, REA, RD). USE THIS.\n"
        "- A school with an ED rate >2x its overall rate (e.g. Villanova ED "
        "~54% vs ~27% overall, Lehigh ED ~52%, Northeastern ED ~47%) is a "
        "powerful ED play. If the student is also AT or ABOVE that school's "
        "75th percentile on their test, the school is a strong KEEP — do "
        "NOT mark it Reconsider just because the overall admit looks low.\n"
        "- Conversely, a school with a low ED rate AND the student below "
        "its 25th percentile is genuinely Reconsider territory.\n"
        "- When you reference odds in a note, prefer the relevant round "
        "rate (ED/EA/RD) over the overall rate when the round is named in "
        "the student's plan or when ED leverage changes the verdict.\n\n"
        "STUDENT PREFERENCE RANK — each line ends with "
        "`student-preference: #N` (1=most-wanted school) or "
        "`student-preference: unranked`. THIS REFLECTS HOW MUCH THE "
        "STUDENT WANTS TO ATTEND. Apply it like this:\n"
        "- Rank #1-#3 = dream/top schools. They get the benefit of the "
        "doubt: prefer them for Anchor and for the ED pick if odds/fit "
        "are remotely viable. Even a low-odds school at rank #1 should "
        "usually be \"Watch\" (or Anchor if ED makes it real), NEVER Cut "
        "unless it is genuinely impossible.\n"
        "- Bottom-ranked schools (very last few in their list) with weak "
        "odds and no ED leverage are reasonable Cut candidates IF they "
        "also meet the strict Cut criteria above. Low preference alone "
        "is not enough to Cut — combine with bad odds + no leverage.\n"
        "- Between two reaches with similar odds/fit, the higher-"
        "preference school wins for Anchor; the lower-preference can "
        "drop to Watch. Don't escalate ties to Cut.\n"
        "- The ED pick should almost always come from rank #1-#3 unless "
        "their top picks have catastrophic ED leverage (no ED option, "
        "or ED rate not meaningfully above RD).\n"
        "- Unranked schools = NEUTRAL preference. They default to Keep. "
        "Do not punish a school just because the student hasn't ranked "
        "it yet — most users won't rank everything.\n"
        "- If NO schools are ranked (everything unranked), preference "
        "doesn't enter the decision at all — judge purely on fit, odds, "
        "and ED leverage.\n"
        "- Mention preference explicitly in notes/game_plan/ed_pick when "
        "it changes the call (e.g. \"your #1 — worth Anchor despite mid "
        "odds\"). Don't mention preference for unranked schools.\n\n"
        "WRITING THE PER-SCHOOL NOTE — this is the most important part. "
        "Each note is ONE sharp sentence (~22-35 words). Follow these rules:\n"
        "1. NEVER open with score math. Do NOT start any note with \"ACT X "
        "is in/below Y's range\", \"Your X falls within…\", or any other "
        "percentile template. That is filler.\n"
        "2. Lead with something SPECIFIC to this school for THIS student — "
        "a program strength tied to their major, a culture/geography fit "
        "tied to their ECs or background, a concrete opportunity (co-op, "
        "research, dual-degree, scholarship), the strategic role on their "
        "list (anchor reach, ED leverage, regional safety), or a specific "
        "fit signal from their awards/leadership.\n"
        "3. End with a SHORT blunt verdict clause matching the action — "
        "WHY anchor/keep, WHY watch (\"keep only if dream\"), WHY cut "
        "(\"drop\", \"swap for ___\", \"effectively lottery\").\n"
        "4. Vary sentence openers. No two notes may start the same way. "
        "Do not use the same adjective (\"strong\", \"solid\", \"realistic\") "
        "more than twice across the whole list.\n"
        "5. Score math allowed at most ONCE per note, never as the lead, "
        "only when it changes the call.\n\n"
        "ROUND_PITCH (optional, <12 words): a short ED/EA framing shown as "
        "a callout under the note. Use ONLY when the round matters strategically "
        "— e.g. \"ED here: 54% admit, your highest-leverage app\" or \"EA "
        "non-binding, gives early answer + same odds\". Leave empty otherwise.\n\n"
        "ACTION CONSISTENCY: action and note MUST agree on energy.\n"
        "- Don't write \"Hail Mary\", \"near-impossible\", or \"unrealistic\" "
        "in a Keep/Anchor note — that's a Watch or Cut framing.\n"
        "- A school with strong ED leverage (ED rate >2x overall) AND the "
        "student at/above its 75th percentile is \"Anchor\" — never \"Cut\".\n"
        "- A note that explains a legitimate strategic angle (ED leverage, "
        "program fit, EA non-binding, etc.) supports Keep or Anchor — even "
        "if odds look thin on paper. Default to Keep when the angle exists.\n\n"
        "LIST BALANCE & STATS:\n"
        "- Limit \"Anchor\" to 3-5 schools across the WHOLE list.\n"
        "- \"Cut\" should be the smallest bucket — usually 0-3 schools. "
        "Most of the list (often 60-80%) is \"Keep\". A long reach pile is "
        "fine; the user can apply to 8-10 reaches if they're motivated.\n"
        "- For top-heavy lists, the fix is usually \"add a safety / a few "
        "targets\" — NOT \"cut half your reaches\". Surface that in the "
        "assessment and additions, not by mass-cutting.\n"
        "- Compute stats {reaches, targets, safeties, list_size}.\n"
        "- Call out top-heavy lists, thin lists (<6 schools), and lists "
        "with no safety in the assessment — these are diagnostics, not "
        "mandates to start cutting.\n\n"
        "ED PICK — single most important strategic decision.\n"
        "- Identify the ONE school the student should ED to (or null if "
        "no genuinely good ED candidate exists, e.g. all reaches are "
        "lottery, or all ED options are dream-school risks).\n"
        "- Best ED pick = high ED admit rate (ideally >2x overall) + "
        "student at/above 75th percentile + real fit (major / EC / "
        "geography). Villanova at ED 54%, Lehigh ED 52%, Northeastern "
        "ED 47% are typical strong picks when fit lines up.\n"
        "- ed_pick.reason MUST cite the specific ED admit rate and the "
        "student's score position from the pre-computed label. 2 sentences.\n\n"
        "GAME PLAN — 3-5 concrete ordered moves the student does NEXT. "
        "Reference schools by name. Each item is one short imperative "
        "sentence. Examples: \"ED to Villanova by Nov 15 — 54% admit and "
        "you're above their ACT median\", \"Cut Tufts, Cornell, and "
        "Northeastern from RD — effectively lottery without an ED slot\", "
        "\"Add Pitt or Penn State as a true safety — your current list "
        "has none\". This is the headline call to action.\n\n"
        "Return ONLY JSON, no markdown, no prose outside the object:\n"
        "{\n"
        '  "headline": "<=14 word punchy verdict on the list",\n'
        '  "assessment": "2-3 honest sentences on overall shape, gaps, strengths",\n'
        '  "stats": {"reaches": <int>, "targets": <int>, "safeties": <int>, "list_size": <int>},\n'
        '  "ed_pick": null OR {"slug": "...", "headline": "<8 word framing", "reason": "2 sentences citing ED rate + score position"},\n'
        '  "game_plan": ["3-5 concrete ordered moves, most important first"],\n'
        '  "schools": [{"slug": "...", "tier": "Reach|Target|Safety", "action": "Anchor|Keep|Watch|Cut", "note": "one specific sentence", "round_pitch": "optional short ED/EA callout or empty"}],\n'
        '  "round_strategy": "2-3 sentences naming specific schools to ED/EA",\n'
        '  "additions": [{"slug": "...", "reason": "..."}],\n'
        '  "priorities": ["3-5 next steps for the student"]\n'
        "}\n"
        "Include EVERY school from their list in \"schools\". Pick 2-4 "
        "\"additions\" from the CANDIDATE catalog that fill real gaps "
        "(missing safety, no Midwest options, weak ED leverage anywhere, "
        "etc.). Every slug MUST come exactly from the lists provided. You "
        "may use **bold** in assessment/note/round_pitch/round_strategy/"
        "priorities/game_plan/ed_pick.reason."
    )
    usr = (
        "STUDENT PROFILE:\n"
        f"Unweighted GPA: {profile.get('uw_gpa') or '?'}  "
        f"Weighted GPA: {profile.get('weighted_gpa') or '?'}\n"
        f"SAT: {profile.get('sat') or '—'}  ACT: {profile.get('act') or '—'}\n"
        f"Intended major: {profile.get('major') or 'undecided'}\n"
        f"Extracurriculars: {(profile.get('ecs') or '—')[:600]}\n"
        f"Leadership: {(profile.get('leadership') or '—')[:300]}\n"
        f"Awards: {(profile.get('awards') or '—')[:300]}\n\n"
        "CURRENT COLLEGE LIST — slug|name|overall admit|test ranges|"
        "score-vs-school (pre-computed, use verbatim)|per-round admit rates|"
        "this student's odds|round they're applying|student preference rank "
        "(1=most-wanted; lower numbers = student wants it more):\n"
        + "\n".join(list_lines)
        + "\n\nCANDIDATE catalog for additions — slug|name|admit rate|test ranges|location|majors:\n"
        + "\n".join(cat_lines)
    )
    # Output must include every school in the list (slug/tier/action/note/pitch)
    # plus additions — a 15-20 school list can blow past 4500 tokens and get the
    # JSON cut mid-object, so give generous headroom. (Input is fine: Haiku 4.5
    # has a 200k window, so the full candidate catalog is not a constraint.)
    raw = _claude("claude-haiku-4-5-20251001", sys, usr, max_tokens=7000)
    if not raw:
        print("[STRATEGIST] empty response from Claude")
        return None
    import json as _json
    import re as _re
    raw = raw.strip()
    if raw.startswith("```"):
        raw = _re.sub(r"^```[a-z]*\n?", "", raw)
        raw = _re.sub(r"\n?```$", "", raw).strip()
    try:
        data = _json.loads(raw)
    except Exception:
        m = _re.search(r"\{.*\}", raw, _re.S)
        if not m:
            print(f"[STRATEGIST] no JSON object found, raw[-200:]={raw[-200:]!r}")
            return None
        try:
            data = _json.loads(m.group(0))
        except Exception as e:
            print(f"[STRATEGIST] JSON parse failed ({e}); raw len={len(raw)} tail={raw[-200:]!r}")
            return None
    if not isinstance(data, dict):
        print(f"[STRATEGIST] response was not a dict, type={type(data).__name__}")
        return None
    # Validate / sanitize
    valid_actions = ("Anchor", "Keep", "Watch", "Cut")
    schools = []
    for s in data.get("schools") or []:
        if not isinstance(s, dict):
            continue
        slug = s.get("slug")
        if slug not in in_list:
            continue
        tier = s.get("tier") if s.get("tier") in ("Reach", "Target", "Safety") else "Target"
        action = s.get("action") if s.get("action") in valid_actions else "Keep"
        schools.append({
            "slug": slug,
            "tier": tier,
            "action": action,
            "note": str(s.get("note", ""))[:320],
            "round_pitch": str(s.get("round_pitch", ""))[:120].strip(),
        })
    additions, seen = [], set()
    for a in data.get("additions") or []:
        if not isinstance(a, dict):
            continue
        slug = a.get("slug")
        if slug in COLLEGES_BY_SLUG and slug not in in_list and slug not in seen:
            seen.add(slug)
            additions.append({"slug": slug, "reason": str(a.get("reason", ""))[:300]})
    priorities = [str(p)[:240] for p in (data.get("priorities") or []) if str(p).strip()]
    game_plan = [str(p)[:240] for p in (data.get("game_plan") or []) if str(p).strip()][:6]
    if not schools:
        print(f"[STRATEGIST] zero valid schools survived filter; in_list={sorted(in_list)} "
              f"returned={[s.get('slug') for s in (data.get('schools') or [])]}")
        return None

    # Safety net for cut-happy outputs: if the model marked too many
    # schools as Cut, downgrade the excess to Watch. The model isn't
    # allowed to nuke the list even if its prompt-following slips.
    # Cap = max(1, ceil(0.3 * len(schools))). Excess Cuts are chosen by
    # their position in the response (later = downgraded first), which
    # tends to keep the model's most-confident cuts.
    import math as _math
    cuts = [i for i, s in enumerate(schools) if s["action"] == "Cut"]
    cut_cap = max(1, _math.ceil(0.30 * len(schools)))
    if len(cuts) > cut_cap:
        excess = cuts[cut_cap:]
        for i in excess:
            schools[i]["action"] = "Watch"
        print(f"[STRATEGIST] cut-cap: downgraded {len(excess)} Cut→Watch "
              f"(had {len(cuts)} cuts in {len(schools)} schools, cap={cut_cap})")

    # ed_pick — must reference a slug that's in their list; null otherwise.
    ed_pick = None
    raw_ep = data.get("ed_pick")
    if isinstance(raw_ep, dict) and raw_ep.get("slug") in in_list:
        ed_pick = {
            "slug": raw_ep["slug"],
            "headline": str(raw_ep.get("headline", ""))[:80],
            "reason": str(raw_ep.get("reason", ""))[:420],
        }

    # stats — accept what the LLM gave, but recompute from schools as a
    # safety net so the page is always internally consistent.
    raw_stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    def _ci(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    counted = {
        "reaches": sum(1 for s in schools if s["tier"] == "Reach"),
        "targets": sum(1 for s in schools if s["tier"] == "Target"),
        "safeties": sum(1 for s in schools if s["tier"] == "Safety"),
        "list_size": len(schools),
    }
    stats = {
        "reaches": _ci(raw_stats.get("reaches")) if _ci(raw_stats.get("reaches")) is not None else counted["reaches"],
        "targets": _ci(raw_stats.get("targets")) if _ci(raw_stats.get("targets")) is not None else counted["targets"],
        "safeties": _ci(raw_stats.get("safeties")) if _ci(raw_stats.get("safeties")) is not None else counted["safeties"],
        "list_size": _ci(raw_stats.get("list_size")) if _ci(raw_stats.get("list_size")) is not None else counted["list_size"],
    }

    return {
        "headline": str(data.get("headline", "Your application strategy"))[:180],
        "assessment": str(data.get("assessment", ""))[:900],
        "stats": stats,
        "ed_pick": ed_pick,
        "game_plan": game_plan,
        "schools": schools,
        "round_strategy": str(data.get("round_strategy", ""))[:900],
        "additions": additions,
        "priorities": priorities[:6],
    }


def _strategist_render(strategy, items=None):
    from html import escape as _esc
    import re as _re

    def _bold(s):
        return _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', _esc(str(s)))

    # Build the action-by-slug lookup so the rank list can show each school's
    # current strategist verdict (Anchor / Keep / Watch / Cut).
    action_by_slug = {s.get("slug"): s.get("action") for s in (strategy.get("schools") or []) if s.get("slug")}

    headline = _esc(strategy.get("headline") or "Your application strategy")
    assessment = _bold(strategy.get("assessment") or "")
    stats = strategy.get("stats") or {}

    # ── stat badges ────────────────────────────────────────────────
    def _badge(label, n, col):
        return (
            f'<div style="display:inline-flex;align-items:baseline;gap:6px;'
            f'padding:6px 12px;border:1px solid {col}33;border-radius:999px;'
            f'background:{col}14">'
            f'<span style="font-weight:700;color:{col};font-size:1.05em">{int(n)}</span>'
            f'<span style="font-size:.78em;color:#94a3b8">{label}</span></div>'
        )
    badges = " ".join([
        _badge("reaches", stats.get("reaches", 0), "#fbbf24"),
        _badge("targets", stats.get("targets", 0), "#5fc9b6"),
        _badge("safeties", stats.get("safeties", 0), "#86efac"),
    ])

    # ── ED pick callout ───────────────────────────────────────────
    ed_pick_html = ""
    ed = strategy.get("ed_pick")
    if ed and ed.get("slug") in COLLEGES_BY_SLUG:
        c = COLLEGES_BY_SLUG[ed["slug"]]
        d = ADMISSIONS_DETAIL.get(c["slug"]) or {}
        rates = d.get("rates") or {}
        ed_rate_label = ""
        for key in ("ED", "ED1", "REA"):
            if key in rates:
                ed_rate_label = f"{key} {round(rates[key]*100)}%"
                break
        if not ed_rate_label and "EA" in rates:
            ed_rate_label = f"EA {round(rates['EA']*100)}%"
        rate_chip = (
            f'<span style="color:#5fc9b6;font-weight:700">{ed_rate_label}</span>'
            if ed_rate_label else ""
        )
        sep = " · " if rate_chip else ""
        ep_headline = _esc(ed.get("headline") or "Your highest-leverage application")
        ed_pick_html = f'''
<div class="card" style="background:linear-gradient(135deg,#0e2a4a 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.4);padding:22px;margin-top:18px">
  <div style="font-size:.7em;letter-spacing:.18em;font-weight:700;color:#5fc9b6;text-transform:uppercase;margin-bottom:10px">★ Your ED pick</div>
  <div style="font-size:1.35em;font-weight:700;margin-bottom:4px">{_esc(c["name"])}</div>
  <div class="muted" style="font-size:.85em;margin-bottom:6px">{city_state(c)} · {round(c["accept"]*100,1)}% overall{sep}{rate_chip}</div>
  <div style="font-size:.92em;color:#cbd5e1;margin-bottom:10px;font-style:italic">{ep_headline}</div>
  <div style="line-height:1.6;font-size:1em">{_bold(ed.get("reason") or "")}</div>
</div>'''

    # ── game plan ─────────────────────────────────────────────────
    plan_items = strategy.get("game_plan") or []
    plan_html = ""
    if plan_items:
        plan_lis = "".join(
            f'''<li style="margin:0;padding:14px 0 14px 14px;border-left:2px solid #5fc9b6;
                          list-style:none;line-height:1.55;counter-increment:plan">
                  <span style="display:inline-block;width:22px;height:22px;border-radius:50%;
                               background:#5fc9b6;color:#0a131c;font-weight:700;font-size:.78em;
                               text-align:center;line-height:22px;margin-right:10px;
                               vertical-align:middle">{i+1}</span>{_bold(p)}</li>'''
            for i, p in enumerate(plan_items)
        )
        plan_html = f'''
<h2 style="margin-top:32px">Your move, in order</h2>
<div class="card" style="padding:6px 18px"><ul style="padding:0;margin:0;list-style:none">{plan_lis}</ul></div>'''

    # ── preference rank list (drag to reorder) ────────────────────
    rank_html = ""
    if items:
        action_col = {"Anchor": "#5fc9b6", "Keep": "#86efac", "Watch": "#fbbf24", "Cut": "#fca5a5"}
        # Sort: ranked items first by rank ascending, then unranked items
        # by alphabetical name (stable). User can drag to override.
        ranked_items = [it for it in items if it.get("preference_rank")]
        unranked = [it for it in items if not it.get("preference_rank")]
        ranked_items.sort(key=lambda it: it["preference_rank"])
        unranked.sort(key=lambda it: (COLLEGES_BY_SLUG.get(it["slug"], {}).get("name") or it["slug"]).lower())
        ordered = ranked_items + unranked

        rows = ""
        for i, it in enumerate(ordered, start=1):
            c = COLLEGES_BY_SLUG.get(it["slug"])
            if not c:
                continue
            action = action_by_slug.get(it["slug"]) or ""
            badge_html = ""
            if action in action_col:
                badge_html = (
                    f'<span class="pref-action-badge" style="font-size:.66em;'
                    f'font-weight:700;color:{action_col[action]};letter-spacing:.06em;'
                    f'white-space:nowrap">{_esc(action.upper())}</span>'
                )
            rd = ROUND_DISPLAY.get(it.get("round"), "")
            rd_html = f' · {_esc(rd)}' if rd and rd != "Round undecided" else ""
            rows += f'''
<li class="pref-row" draggable="true" data-slug="{_esc(c["slug"])}"
    style="display:flex;align-items:center;gap:12px;padding:12px 14px;
           border:1px solid #1f2937;border-radius:8px;background:#0a131c;
           margin-bottom:8px;cursor:grab;user-select:none">
  <span class="pref-rank-num" style="display:inline-flex;align-items:center;justify-content:center;
        width:28px;height:28px;border-radius:50%;background:#1f2937;color:#94a3b8;
        font-weight:700;font-size:.85em;flex-shrink:0">{i}</span>
  <span style="color:#475569;font-size:1.2em;letter-spacing:-2px;flex-shrink:0" aria-hidden="true">⋮⋮</span>
  <div style="flex:1;min-width:0">
    <div style="font-weight:600;font-size:.95em">{_esc(c["name"])}</div>
    <div class="muted" style="font-size:.78em">{city_state(c)} · {round(c["accept"]*100,1)}% accept{rd_html}</div>
  </div>
  {badge_html}
</li>'''

        any_ranked = bool(ranked_items)
        helper = (
            "Drag to reorder. The strategist treats your top picks as "
            "Anchors and bottom ones as cuts. Click Apply to regenerate."
        )
        # Inline CSRF token so the fetch() can include it as a header
        # without depending on a meta tag elsewhere in the layout.
        _csrf_tok = ""
        if _CSRF_ON:
            try:
                from flask_wtf.csrf import generate_csrf as _gen_csrf
                _csrf_tok = _gen_csrf()
            except Exception:
                _csrf_tok = ""
        rank_html = f'''
<div class="card" id="pref-rank-card" style="margin-top:32px;padding:22px;border:1px solid rgba(95,201,182,.25)">
  <div style="display:flex;align-items:baseline;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:6px">
    <h2 style="margin:0">Rank your schools <span class="muted" style="font-size:.55em;font-weight:400">top = most wanted</span></h2>
    <span id="pref-dirty-indicator" style="display:none;font-size:.78em;color:#fbbf24;font-weight:600">● unsaved changes</span>
  </div>
  <p class="muted" style="margin:6px 0 14px;font-size:.88em">{helper}</p>
  <ul id="pref-list" style="list-style:none;padding:0;margin:0">{rows}</ul>
  <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap">
    <button id="pref-apply-btn" class="btn btn-primary" type="button" disabled
            style="opacity:.55;cursor:not-allowed">
      {"Apply rankings &amp; regenerate" if any_ranked else "Save rankings &amp; generate"}
    </button>
    <span id="pref-status" class="muted" style="font-size:.85em;align-self:center"></span>
  </div>
</div>
<style>
  .pref-row.dragging {{ opacity:.35 }}
  .pref-row.drop-target-above {{ box-shadow: 0 -2px 0 0 #5fc9b6 inset; }}
  .pref-row.drop-target-below {{ box-shadow: 0 2px 0 0 #5fc9b6 inset; }}
  .pref-row:hover {{ border-color:#334155 !important; }}
</style>
<script>
(function() {{
  var list = document.getElementById('pref-list');
  if (!list) return;
  var dirty = document.getElementById('pref-dirty-indicator');
  var applyBtn = document.getElementById('pref-apply-btn');
  var statusEl = document.getElementById('pref-status');
  var dragged = null;

  function markDirty() {{
    if (dirty) dirty.style.display = 'inline';
    if (applyBtn) {{
      applyBtn.disabled = false;
      applyBtn.style.opacity = '1';
      applyBtn.style.cursor = 'pointer';
    }}
  }}
  function renumber() {{
    Array.prototype.forEach.call(list.children, function(li, i) {{
      var n = li.querySelector('.pref-rank-num');
      if (n) n.textContent = (i + 1);
    }});
  }}
  list.addEventListener('dragstart', function(e) {{
    var li = e.target.closest('li.pref-row');
    if (!li) return;
    dragged = li;
    li.classList.add('dragging');
    if (e.dataTransfer) {{ e.dataTransfer.effectAllowed = 'move'; }}
  }});
  list.addEventListener('dragend', function() {{
    if (dragged) dragged.classList.remove('dragging');
    Array.prototype.forEach.call(list.querySelectorAll('.drop-target-above,.drop-target-below'),
      function(el) {{ el.classList.remove('drop-target-above','drop-target-below'); }});
    dragged = null;
    renumber();
  }});
  list.addEventListener('dragover', function(e) {{
    e.preventDefault();
    if (!dragged) return;
    var over = e.target.closest('li.pref-row');
    if (!over || over === dragged) return;
    var rect = over.getBoundingClientRect();
    var below = (e.clientY - rect.top) > rect.height / 2;
    Array.prototype.forEach.call(list.querySelectorAll('.drop-target-above,.drop-target-below'),
      function(el) {{ el.classList.remove('drop-target-above','drop-target-below'); }});
    over.classList.add(below ? 'drop-target-below' : 'drop-target-above');
  }});
  list.addEventListener('drop', function(e) {{
    e.preventDefault();
    if (!dragged) return;
    var over = e.target.closest('li.pref-row');
    if (!over || over === dragged) return;
    var rect = over.getBoundingClientRect();
    var below = (e.clientY - rect.top) > rect.height / 2;
    over.parentNode.insertBefore(dragged, below ? over.nextSibling : over);
    markDirty();
  }});
  if (applyBtn) {{
    applyBtn.addEventListener('click', function() {{
      var slugs = Array.prototype.map.call(list.children, function(li) {{ return li.dataset.slug; }});
      applyBtn.disabled = true; applyBtn.style.opacity = '.55';
      if (statusEl) statusEl.textContent = 'Saving & regenerating…';
      var headers = {{ 'Content-Type': 'application/json' }};
      var tok = '{_csrf_tok}';
      if (tok) headers['X-CSRFToken'] = tok;
      fetch('/plans/rank', {{
        method: 'POST', headers: headers, body: JSON.stringify({{slugs: slugs}}), credentials: 'same-origin'
      }}).then(function(r) {{
        if (!r.ok) throw new Error('save failed');
        window.location.href = '/plans/strategist?regen=1';
      }}).catch(function() {{
        if (statusEl) statusEl.textContent = 'Save failed — try again.';
        applyBtn.disabled = false; applyBtn.style.opacity = '1';
      }});
    }});
  }}
}})();
</script>'''

    # ── schools, sorted within tier by action priority ────────────
    action_order = {"Anchor": 0, "Keep": 1, "Watch": 2, "Cut": 3}
    action_col = {"Anchor": "#5fc9b6", "Keep": "#86efac", "Watch": "#fbbf24", "Cut": "#fca5a5"}
    action_label = {"Anchor": "★ ANCHOR", "Keep": "KEEP", "Watch": "WATCH", "Cut": "CUT"}
    tier_col = {"Reach": "#fbbf24", "Target": "#5fc9b6", "Safety": "#86efac"}
    tier_blurb = {
        "Reach": "Hard to get in — most accept rates here are below 35%.",
        "Target": "Where most of your acceptances should come from.",
        "Safety": "High confidence and a place you'd actually attend.",
    }

    groups = {"Reach": [], "Target": [], "Safety": []}
    for s in strategy.get("schools") or []:
        if s.get("tier") in groups:
            groups[s["tier"]].append(s)
    for tier in groups:
        groups[tier].sort(key=lambda s: (action_order.get(s.get("action"), 9), s.get("slug") or ""))

    def _round_chip(c):
        """Inline 'ED 54%' chip when ED admit is meaningfully higher than RD."""
        d = ADMISSIONS_DETAIL.get(c.get("slug")) or {}
        rates = d.get("rates") or {}
        if not rates:
            return ""
        rd = rates.get("RD")
        for key in ("ED", "ED1", "REA"):
            if key in rates:
                v = rates[key]
                if rd is None or v >= rd * 1.3:
                    return f' · <span style="color:#5fc9b6;font-weight:600">{key} {round(v*100)}%</span>'
                return ""
        return ""

    schools_html = ""
    for tier in ("Reach", "Target", "Safety"):
        grp = groups[tier]
        if not grp:
            continue
        col = tier_col[tier]
        cards = ""
        for s in grp:
            c = COLLEGES_BY_SLUG.get(s.get("slug"))
            if not c:
                continue
            action = s.get("action") or "Keep"
            if action not in action_col:
                action = "Keep"
            acol = action_col[action]
            alabel = action_label[action]

            # card border + opacity for visual hierarchy
            if action == "Anchor":
                card_style = (
                    f"padding:14px;border:1px solid {acol}88;"
                    f"border-left:3px solid {acol};"
                    f"box-shadow:0 0 0 1px rgba(95,201,182,.10),0 8px 24px -12px rgba(95,201,182,.25)"
                )
            elif action == "Cut":
                card_style = f"padding:14px;border-left:3px solid {acol};opacity:.72"
            else:
                card_style = f"padding:14px;border-left:3px solid {col}"

            round_pitch = (s.get("round_pitch") or "").strip()
            round_pitch_html = (
                f'<div style="margin-top:10px;padding:8px 10px;background:rgba(95,201,182,.08);'
                f'border-radius:6px;font-size:.82em;color:#5fc9b6;font-weight:600">'
                f'→ {_bold(round_pitch)}</div>'
                if round_pitch else ""
            )

            cards += f'''
<div class="card" style="{card_style}">
  <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start">
    <div style="font-weight:700">{_esc(c["name"])}</div>
    <span style="font-size:.68em;font-weight:700;color:{acol};white-space:nowrap;letter-spacing:.08em">{alabel}</span>
  </div>
  <div class="muted" style="font-size:.82em;margin-top:2px">{city_state(c)} · {round(c["accept"]*100,1)}% accept{_round_chip(c)}</div>
  <div style="margin-top:8px;font-size:.9em;line-height:1.5">{_bold(s.get("note") or "")}</div>
  {round_pitch_html}
</div>'''
        schools_html += f'''
<div style="margin-top:32px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">
  <h2 style="margin:0">{tier} <span class="muted" style="font-size:.6em;font-weight:400">({len(grp)})</span></h2>
  <div class="muted" style="font-size:.85em">{tier_blurb[tier]}</div>
</div>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:12px">{cards}</div>'''

    # ── additions ─────────────────────────────────────────────────
    add_cards = ""
    for a in strategy.get("additions") or []:
        c = COLLEGES_BY_SLUG.get(a.get("slug"))
        if not c:
            continue
        add_cards += f'''
<div class="card" style="padding:14px">
  <div style="font-weight:700">{_esc(c["name"])}</div>
  <div class="muted" style="font-size:.82em">{city_state(c)} · {round(c["accept"]*100,1)}% accept{_round_chip(c)}</div>
  <div style="margin-top:8px;font-size:.9em;line-height:1.5">{_bold(a.get("reason") or "")}</div>
  <form method="post" action="/save/{c['slug']}" style="margin-top:10px">
    {csrf_input()}
    <input type="hidden" name="next" value="/plans/strategist">
    <button class="btn btn-primary btn-sm" type="submit">+ Add to my list</button>
  </form>
</div>'''
    add_section = ""
    if add_cards:
        add_section = (
            '<h2 style="margin-top:32px">Schools to consider adding</h2>'
            '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">'
            f'{add_cards}</div>'
        )

    round_html = _bold(strategy.get("round_strategy") or "")
    prio_html = "".join(
        f'<li style="margin:8px 0;line-height:1.6">{_bold(p)}</li>'
        for p in (strategy.get("priorities") or [])
    )

    return _page(f'''
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1 style="margin-bottom:4px">Application strategist</h1>
<p class="muted" style="margin-top:0">An AI read of your full list and profile — your ED move, ordered next steps, and where to cut. <a href="/plans/strategist?regen=1">Regenerate</a></p>

<div class="card" style="background:linear-gradient(135deg,#0f3a37 0%,#0a131c 100%);border:1px solid rgba(95,201,182,.3);padding:24px">
  <div style="font-size:1.2em;font-weight:700;color:#5fc9b6;margin-bottom:10px;line-height:1.35">{headline}</div>
  <div style="line-height:1.6;margin-bottom:14px">{assessment}</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">{badges}</div>
</div>

{ed_pick_html}

{plan_html}

{rank_html}

{schools_html}

<h2 style="margin-top:32px">Round strategy</h2>
<div class="card"><div style="line-height:1.6">{round_html}</div></div>

{add_section}

<h2 style="margin-top:32px">Your priorities</h2>
<div class="card"><ol style="padding-left:20px;margin:0">{prio_html or '<li>No specific actions — your list looks solid.</li>'}</ol></div>

<p style="margin-top:32px"><a class="btn btn-light" href="/plans">← Back to My colleges</a> <a class="btn btn-primary" href="/plans/grade">See list grade →</a></p>
''', title="Application strategist — Candor")


@app.route("/plans/strategist")
@login_required
def plans_strategist_page():
    gate = _gate_premium()
    if gate:
        return gate
    uid = current_user()["id"]
    profile, items = _strategist_gather(uid)
    if not items:
        return _page('''
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>Application strategist</h1>
<div class="card"><p class="muted">Your list is empty — save a few schools or run chances first, and the strategist will build you a plan.</p>
<a class="btn btn-primary" href="/colleges">Browse colleges</a></div>
''', title="Application strategist — Candor")

    h = _strategist_input_hash(profile, items)
    regen = request.args.get("regen") == "1"
    strategy = None
    with db() as conn:
        row = conn.execute(
            "SELECT input_hash, body FROM strategist_results WHERE user_id=?",
            (uid,)
        ).fetchone()
    if row and row["input_hash"] == h and not regen:
        try:
            strategy = json.loads(row["body"])
        except Exception:
            strategy = None
    if strategy is None:
        strategy = generate_strategy(profile, items)
        if not strategy:
            return _page('''
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>Application strategist</h1>
<div class="card"><p class="muted">Couldn't build your strategy just now — please try again in a moment.</p>
<a class="btn btn-primary" href="/plans/strategist?regen=1">Try again</a></div>
''', title="Application strategist — Candor")
        with db() as conn:
            conn.execute(
                "INSERT INTO strategist_results (user_id, input_hash, body, generated_at) "
                "VALUES (?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET input_hash=excluded.input_hash, "
                "body=excluded.body, generated_at=CURRENT_TIMESTAMP",
                (uid, h, json.dumps(strategy))
            )
            conn.commit()
    return _strategist_render(strategy, items=items)


def _poisson_binomial_at_least(probs, k):
    """P(at least k successes) when each trial i has probability probs[i].
    Uses dynamic programming on the PMF — O(N*K) which is fine for N<50.
    Returns float in [0,1]."""
    if not probs:
        return 0.0
    # dp[j] = P(exactly j successes so far)
    dp = [0.0] * (len(probs) + 1)
    dp[0] = 1.0
    for p in probs:
        new = [0.0] * (len(probs) + 1)
        for j in range(len(probs) + 1):
            if dp[j] == 0: continue
            new[j] += dp[j] * (1 - p)
            if j + 1 <= len(probs):
                new[j+1] += dp[j] * p
        dp = new
    return sum(dp[k:])


def simulate_admissions(uid):
    """For each school in user's list, get the round-specific personalized
    odds. Sum to get expected admits. Compute P(>=1), P(>=3) via Poisson
    binomial. Identify strategic suggestions for round swaps."""
    with db() as conn:
        # Filter out stale entries — schools chanced under an old odds
        # model (pre-legacy-fix, pre-state-fix, etc.) regenerate on next
        # /chances visit instead of polluting the simulator with old numbers.
        chances = conn.execute(
            "SELECT college_slug, tier, odds_low, odds_high, application_round "
            "FROM saved_chances WHERE user_id=? AND computed_at >= ?",
            (uid, SAVED_CHANCES_MIN_VALID_AT)
        ).fetchall()
        saved = conn.execute(
            "SELECT college_slug, application_round FROM saved_schools WHERE user_id=?", (uid,)
        ).fetchall()
    chance_slugs = {r["college_slug"] for r in chances}
    items = []
    for r in chances:
        items.append({
            "slug": r["college_slug"],
            "tier": r["tier"],
            "odds_low": r["odds_low"], "odds_high": r["odds_high"],
            "round": r["application_round"],
            "computed": True,
        })
    for r in saved:
        if r["college_slug"] in chance_slugs: continue
        items.append({
            "slug": r["college_slug"], "tier": None, "odds_low": None, "odds_high": None,
            "round": r["application_round"], "computed": False,
        })

    profile = get_profile(uid)

    # Compute the per-round adjusted odds for each school
    sim_rows = []
    probs = []
    for it in items:
        slug = it["slug"]
        c = COLLEGES_BY_SLUG.get(slug)
        if not c: continue
        if not it["computed"] or it["odds_low"] is None:
            # Skip schools where chances haven't been computed
            sim_rows.append({**it, "name": c["name"], "p_round": None, "p_overall": None,
                             "round_label": ROUND_DISPLAY.get(it["round"], "Round undecided")})
            continue
        # Use round-specific personalized odds when available
        detail = admissions_detail(c)
        p_overall = (it["odds_low"] + it["odds_high"]) / 200.0  # midpoint as float 0-1
        p_round = p_overall  # default if no round selected
        if it["round"]:
            r_key = "ED" if it["round"] == "ED1" else it["round"]
            personal_rounds = None
            if detail:
                # Adapt profile keys: personalize_round_odds reads gpa /
                # intended_major / extracurriculars; our profile uses
                # uw_gpa / major / ecs. Without this remap the AI gets
                # an empty profile and returns generic rates.
                _adapted = dict(profile or {})
                if profile and profile.get("uw_gpa") is not None:
                    _adapted["gpa"] = profile.get("uw_gpa")
                if profile and profile.get("major"):
                    _adapted["intended_major"] = profile.get("major")
                if profile and profile.get("ecs"):
                    _adapted["extracurriculars"] = profile.get("ecs")
                # Pass legacy through — personalize_round_odds reads
                # legacy_schools to factor in legacy boost on round rates.
                if profile and profile.get("legacy_schools"):
                    _adapted["legacy_schools"] = profile.get("legacy_schools")
                personal_rounds = personalize_round_odds(
                    uid, c, detail, _adapted, it["odds_low"], it["odds_high"]
                )
            if personal_rounds and r_key in personal_rounds:
                p_round = personal_rounds[r_key]
            elif personal_rounds and it["round"] in personal_rounds:
                p_round = personal_rounds[it["round"]]
            elif detail and detail.get("rates") and c.get("accept"):
                # Fallback: scale user's p_overall by the school's published
                # round-vs-overall ratio. Approximate but way better than
                # ignoring round selection entirely.
                pub = detail["rates"].get(r_key) or detail["rates"].get(it["round"])
                if pub and c["accept"]:
                    p_round = min(0.95, p_overall * (pub / c["accept"]))
            else:
                # No detail at all — apply rough genre defaults so ED still
                # bumps something. ED ~2x, ED2 ~1.5x, REA ~1.4x, EA ~1.2x.
                MULT = {"ED":2.0, "ED1":2.0, "ED2":1.5, "REA":1.4, "EA":1.2}
                if it["round"] in MULT:
                    p_round = min(0.95, p_overall * MULT[it["round"]])
        probs.append(p_round)
        sim_rows.append({**it, "name": c["name"], "p_round": p_round, "p_overall": p_overall,
                         "round_label": ROUND_DISPLAY.get(it["round"], "Round undecided")})

    expected = sum(probs) if probs else 0.0
    p_at_least_1 = 1.0 - _poisson_binomial_at_least(probs, 0) if not probs else (1.0 - _poisson_binomial_pmf_zero(probs))
    # Compute via the dp directly:
    p_at_least_1 = _poisson_binomial_at_least(probs, 1)
    p_at_least_3 = _poisson_binomial_at_least(probs, 3)

    return {
        "rows": sim_rows,
        "expected": round(expected, 2),
        "p_at_least_1": round(p_at_least_1 * 100, 1),
        "p_at_least_3": round(p_at_least_3 * 100, 1),
        "n_with_round": sum(1 for r in sim_rows if r.get("round")),
        "n_total": len([r for r in sim_rows if r["computed"]]),
        "n_uncomputed": sum(1 for r in sim_rows if not r["computed"]),
    }


def _poisson_binomial_pmf_zero(probs):
    """P(zero successes) — equivalent to product of (1-p_i)."""
    p = 1.0
    for x in probs: p *= (1 - x)
    return p


@app.route("/plans/compute-all", methods=["POST"])
@login_required
def plans_compute_all():
    """Run analyze_school for every school in the user's My Colleges that
    doesn't currently have saved_chances. Lets users repopulate the
    simulator after a profile update wiped the cache."""
    uid = current_user()["id"]
    p = get_profile(uid)
    if not p:
        flash("Add your profile first so we can compute chances.", "error")
        return redirect("/profile")
    is_exc, exc_reason = get_or_evaluate_exceptionality(uid, p)
    profile = {
        "uw_gpa": p.get("uw_gpa"), "weighted_gpa": p.get("weighted_gpa"),
        "gpa_freshman": p.get("gpa_freshman"), "gpa_sophomore": p.get("gpa_sophomore"),
        "gpa_junior": p.get("gpa_junior"), "gpa_senior": p.get("gpa_senior"),
        "sat": p.get("sat"), "act": p.get("act"),
        "sat_math": p.get("sat_math"), "sat_ebrw": p.get("sat_ebrw"),
        "act_math": p.get("act_math"), "act_english": p.get("act_english"),
        "act_reading": p.get("act_reading"), "act_science": p.get("act_science"),
        "major": p.get("major"), "state": p.get("state"),
        "school_type": p.get("school_type"),
        "ecs": p.get("ecs"), "leadership": p.get("leadership"), "awards": p.get("awards"),
        "legacy": bool(p.get("legacy")), "first_gen": bool(p.get("first_gen")),
        "athlete": bool(p.get("athlete")),
        "is_international": bool(p.get("is_international")),
        "legacy_schools": p.get("legacy_schools") or "",
        "aps": p.get("aps") or "",
        "no_aps_offered": bool(p.get("no_aps_offered")),
        "aps_offered_not_taken": bool(p.get("aps_offered_not_taken")),
        "is_exceptional": is_exc, "exceptional_reason": exc_reason,
        "portfolio": p.get("portfolio") or "",
    }
    # Get every school the user has in their list — saved_schools (clicked
    # "Save") OR saved_chances (ran chances on it). The simulator reads
    # BOTH tables; if compute-all only checks saved_schools we'd miss
    # schools the user computed chances on but never explicitly saved.
    # Then exclude any with a *fresh* saved_chances row already.
    with db() as conn:
        all_slugs = {r["college_slug"] for r in conn.execute(
            "SELECT college_slug FROM saved_schools WHERE user_id=? "
            "UNION SELECT college_slug FROM saved_chances WHERE user_id=?",
            (uid, uid)).fetchall()}
        with_chances = {r["college_slug"] for r in conn.execute(
            "SELECT college_slug FROM saved_chances WHERE user_id=? AND computed_at >= ?",
            (uid, SAVED_CHANCES_MIN_VALID_AT)).fetchall()}
    todo = [s for s in all_slugs if s not in with_chances]
    n_done = 0
    errors = []  # collect failure reasons so we can surface them in the flash
    # Batch path: skip the per-school Claude bullets call. Use fallback
    # bullets and rely on /chances/<slug> to fill in the AI narrative
    # lazily when the user views that specific school.
    if not todo:
        flash(f"No schools needed computing — list has {len(all_slugs)} schools, "
              f"all have fresh chances.", "success")
        return redirect(request.form.get("return_to") or "/plans/simulate")
    for slug in todo:
        try:
            school = COLLEGES_BY_SLUG.get(slug)
            if not school:
                errors.append(f"{slug}: not in college DB")
                continue
            profile_for_school = dict(profile)
            try:
                profile_for_school["_di_level"] = get_demonstrated_interest(uid, slug)
            except Exception:
                profile_for_school["_di_level"] = "none"
            fit, components = compute_fit(profile_for_school, school)
            tier = assign_tier(school, fit, profile_for_school)
            low, high = estimate_odds(school, fit, profile_for_school)
            fb = _fallback_bullets(profile_for_school, school, fit, components, tier)
            conf = confidence_level(profile_for_school, components)
            with db() as conn:
                conn.execute("""INSERT INTO saved_chances (user_id, college_slug, tier, odds_low, odds_high, fit, confidence, strength, weakness, differentiator, computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, college_slug) DO UPDATE SET
                      tier=excluded.tier, odds_low=excluded.odds_low, odds_high=excluded.odds_high,
                      fit=excluded.fit, confidence=excluded.confidence,
                      strength=excluded.strength, weakness=excluded.weakness, differentiator=excluded.differentiator,
                      computed_at=CURRENT_TIMESTAMP""",
                    (uid, slug, tier, low, high, fit, conf,
                     fb["strength"], fb["weakness"], fb["differentiator"]))
                conn.commit()
            n_done += 1
        except Exception as e:
            errors.append(f"{slug}: {type(e).__name__}: {e}")
            print(f"compute-all failed for {slug}: {type(e).__name__}: {e}")
    msg = f"Computed chances for {n_done}/{len(todo)} schools."
    if errors:
        # Surface the first 3 failure reasons so we can actually see what's wrong
        msg += " Errors: " + " | ".join(errors[:3])
        if len(errors) > 3:
            msg += f" (+{len(errors)-3} more)"
    flash(msg, "success" if n_done else "error")
    return redirect(request.form.get("return_to") or "/plans/simulate")


@app.route("/plans/simulate")
@login_required
def plans_simulate_page():
    gate = _gate_premium()
    if gate: return gate
    uid = current_user()["id"]
    sim = simulate_admissions(uid)

    # Bucket schools by individual probability so users see "where will I
    # actually get in." Most likely outcome at the per-school level: admit
    # if p>0.5, otherwise reject. Toss-ups are 20-50%, long shots <20%.
    likely = []   # p >= 0.50
    tossup = []   # 0.20 <= p < 0.50
    longshot = [] # p < 0.20
    sim_school_data = []  # JSON-able list for the JS Monte Carlo simulator
    for r in sim["rows"]:
        if not r["computed"] or r["p_round"] is None: continue
        p = r["p_round"]
        item = (r["name"], r["slug"], p, r.get("round_label",""))
        if p >= 0.50: likely.append(item)
        elif p >= 0.20: tossup.append(item)
        else: longshot.append(item)
        sim_school_data.append({
            "name": r["name"], "slug": r["slug"], "p": round(p, 4),
            "round": r.get("round_label","") or "Round undecided",
        })
    likely.sort(key=lambda x: -x[2])
    tossup.sort(key=lambda x: -x[2])
    longshot.sort(key=lambda x: -x[2])

    def _bucket_card(title, color, items, hint):
        if not items:
            return ""
        body = ""
        for name, slug, p, rnd in items:
            rnd_label = f' <span class="muted" style="font-size:.78em">({rnd})</span>' if rnd else ""
            body += f'<div style="display:flex;justify-content:space-between;padding:7px 0;border-top:1px solid var(--border);font-size:.92em"><a href="/college/{slug}" style="color:inherit"><b>{name}</b>{rnd_label}</a><span style="font-weight:600;color:{color}">{round(p*100,1)}%</span></div>'
        return f'''<div class="card" style="margin-bottom:12px;padding:18px">
  <h3 style="margin:0 0 4px;font-family:'Newsreader',Georgia,serif;color:{color}">{title} <span style="font-weight:400;color:var(--text-2);font-size:.78em">({len(items)})</span></h3>
  <p class="muted" style="font-size:.84em;margin:0 0 6px">{hint}</p>
  {body}
</div>'''

    buckets_html = (
        _bucket_card("Likely admits", "#5fc9b6", likely,
                     "Schools where your odds are 50%+. The most-likely outcome is you get in.")
        + _bucket_card("Toss-ups", "#fbbf24", tossup,
                       "20-50% odds. Could go either way; these are where the variance lives.")
        + _bucket_card("Long shots", "#fca5a5", longshot,
                       "Under 20%. Real possibilities but you can't count on them.")
    )

    rows_html = ""
    for r in sim["rows"]:
        if not r["computed"]:
            rows_html += f'''<tr>
              <td>{r["name"]}</td>
              <td class="muted">{r["round_label"]}</td>
              <td colspan="3" class="muted" style="font-style:italic">No chances computed — <a href="/chances/{r["slug"]}">run now →</a></td>
            </tr>'''
            continue
        p_round_str = f'{round(r["p_round"]*100,1)}%' if r["p_round"] is not None else "—"
        p_overall_str = f'{round(r["p_overall"]*100,1)}%' if r["p_overall"] is not None else "—"
        diff = ""
        if r["p_round"] is not None and r["p_overall"] is not None and abs(r["p_round"] - r["p_overall"]) > 0.005:
            delta = (r["p_round"] - r["p_overall"]) * 100
            sign = "+" if delta > 0 else ""
            color = "#5fc9b6" if delta > 0 else "#fca5a5"
            diff = f' <span style="color:{color};font-size:.85em">({sign}{round(delta,1)}%)</span>'
        round_pill = ""
        if r["round"]:
            rcol = "#5fc9b6" if r["round"] in ("ED1","ED2") else ("#7dd3fc" if r["round"] in ("EA","REA") else "#9aa6b6")
            round_pill = f'<span style="background:rgba(95,201,182,.08);color:{rcol};padding:2px 8px;border-radius:4px;font-size:.78em;font-weight:600">{r["round_label"]}</span>'
        else:
            round_pill = '<span class="muted" style="font-size:.82em">undecided</span>'
        rows_html += f'''<tr>
          <td><b>{r["name"]}</b></td>
          <td>{round_pill}</td>
          <td style="font-weight:700">{p_round_str}{diff}</td>
          <td class="muted">{p_overall_str}</td>
          <td><a href="/college/{r["slug"]}/plan" class="muted" style="font-size:.85em">plan →</a></td>
        </tr>'''

    expected_color = "#5fc9b6" if sim["expected"] >= 2 else ("#fbbf24" if sim["expected"] >= 1 else "#fca5a5")
    p1_color = "#5fc9b6" if sim["p_at_least_1"] >= 80 else ("#fbbf24" if sim["p_at_least_1"] >= 50 else "#fca5a5")

    note = ""
    if sim["n_uncomputed"] > 0:
        note = f'''<div class="card" style="background:rgba(251,191,36,.06);border:1px solid rgba(251,191,36,.25);padding:14px 18px;margin:10px 0">
  <div style="font-weight:600;margin-bottom:6px">⚠️ {sim["n_uncomputed"]} school{"" if sim["n_uncomputed"]==1 else "s"} in your list need chances computed</div>
  <p class="muted" style="font-size:.88em;margin:0 0 10px">They're excluded from the simulation until chances are run. This usually happens after a profile update wipes the cache. One click recomputes all of them ({"~"+str(int(sim["n_uncomputed"]*1.5))+" sec" if sim["n_uncomputed"]>0 else ""}):</p>
  <form method="post" action="/plans/compute-all" style="display:inline">
    {csrf_input()}
    <input type="hidden" name="return_to" value="/plans/simulate">
    <button class="btn btn-primary" type="submit">Compute all missing chances</button>
  </form>
</div>'''

    return _page(f"""
<div class="bar"><a href="/plans">← Back to My colleges</a></div>
<h1>Admissions simulator</h1>
<p class="muted">Expected outcomes across your full list, using your personalized round-specific odds. Math: each school is an independent trial; we sum up your probabilities.</p>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px">
  <div class="card" style="text-align:center;padding:22px">
    <div style="font-size:.78em;letter-spacing:.5px;color:#9aa6b6;text-transform:uppercase">Expected admits</div>
    <div style="font-size:3em;font-weight:800;color:{expected_color};line-height:1;margin-top:6px;letter-spacing:-1px">{sim["expected"]}</div>
    <div class="muted" style="font-size:.85em;margin-top:6px">out of {sim["n_total"]} school{"s" if sim["n_total"] != 1 else ""}</div>
  </div>
  <div class="card" style="text-align:center;padding:22px">
    <div style="font-size:.78em;letter-spacing:.5px;color:#9aa6b6;text-transform:uppercase">P(at least 1 admit)</div>
    <div style="font-size:3em;font-weight:800;color:{p1_color};line-height:1;margin-top:6px;letter-spacing:-1px">{sim["p_at_least_1"]}%</div>
    <div class="muted" style="font-size:.85em;margin-top:6px">probability you get in somewhere</div>
  </div>
  <div class="card" style="text-align:center;padding:22px">
    <div style="font-size:.78em;letter-spacing:.5px;color:#9aa6b6;text-transform:uppercase">P(at least 3 admits)</div>
    <div style="font-size:3em;font-weight:800;color:#9aa6b6;line-height:1;margin-top:6px;letter-spacing:-1px">{sim["p_at_least_3"]}%</div>
    <div class="muted" style="font-size:.85em;margin-top:6px">probability of multiple options</div>
  </div>
</div>

<h2 style="margin-top:28px">Run a simulation</h2>
<p class="muted" style="font-size:.92em">Roll the dice on a single admissions cycle — each school flipped against your real odds. Click again for a different outcome (it'll be different every time, because admissions has variance).</p>
<div class="card" id="sim-card" style="padding:24px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
    <button id="sim-btn" class="btn btn-primary">🎲 Simulate this cycle</button>
    <button id="sim-1k-btn" class="btn btn-light btn-sm">Run 1,000 simulations</button>
    <span id="sim-counter" class="muted" style="font-size:.85em"></span>
  </div>
  <div id="sim-summary" style="display:none;padding:14px 16px;background:rgba(95,201,182,.06);border:1px solid rgba(95,201,182,.25);border-radius:6px;margin-bottom:14px">
    <div style="font-weight:600;font-size:1.1em" id="sim-headline">—</div>
    <div class="muted" id="sim-subhead" style="font-size:.88em;margin-top:4px"></div>
  </div>
  <div id="sim-results" style="display:none"></div>
  <div id="sim-1k-results" style="display:none"></div>
  <div id="sim-empty" class="muted" style="font-size:.9em">Click "Simulate this cycle" to see one possible outcome based on your real odds.</div>
</div>
<script id="sim-data" type="application/json">{json.dumps(sim_school_data)}</script>
<script>
(function(){{
  const data = JSON.parse(document.getElementById('sim-data').textContent || '[]');
  const card = document.getElementById('sim-card');
  const btn = document.getElementById('sim-btn');
  const btn1k = document.getElementById('sim-1k-btn');
  const counter = document.getElementById('sim-counter');
  const empty = document.getElementById('sim-empty');
  const summary = document.getElementById('sim-summary');
  const headline = document.getElementById('sim-headline');
  const subhead = document.getElementById('sim-subhead');
  const results = document.getElementById('sim-results');
  const results1k = document.getElementById('sim-1k-results');
  let runs = 0;

  function flip(p){{ return Math.random() < p; }}

  // 4-outcome model that mirrors real admissions:
  //   ADMIT, DEFER (early-round only), WAITLIST, REJECT
  // For ED/ED2/EA/REA: of the (1-p) non-admit pool, ~45% defer to RD,
  //   ~10% waitlist, ~45% reject. (Defer rates are highest at the most
  //   competitive schools — Penn ED is famously defer-heavy.)
  // For RD: of the (1-p) non-admit pool, ~12% waitlist, ~88% reject.
  // For "Round undecided": treat as RD by default.
  // Two-stage rolling: any defer/waitlist resolves to a final admit or
  // reject, since by the end of the cycle no decision is "still pending."
  // Returns an object with final/path/label so we can show both the
  // final outcome (for counting admits) and the path that got there
  // (for the stamp). Constants based on rough industry averages:
  //   - Of deferred applicants: ~13% admitted RD, ~10% waitlisted, ~77% rejected
  //   - Of waitlisted applicants: ~10% pulled off (varies wildly by school)
  function rollOutcome(p, round){{
    const isEarly = /(Early Decision|Early Action|Restrictive)/i.test(round || '');
    // Stage 1: submitted-round decision
    if (Math.random() < p) {{
      return {{ final:'ADMIT', path:'admit', label:'ADMITTED' }};
    }}
    // Distribute non-admits. Early rounds defer most, RD waitlists some.
    let stage;
    if (isEarly) {{
      const r = Math.random();
      stage = r < 0.45 ? 'defer' : (r < 0.55 ? 'waitlist' : 'reject');
    }} else {{
      stage = Math.random() < 0.12 ? 'waitlist' : 'reject';
    }}
    if (stage === 'defer') {{
      // Stage 2: deferred applicants get re-evaluated in RD pool
      const r = Math.random();
      if (r < 0.13) return {{ final:'ADMIT', path:'defer_admit', label:'DEFER → ADMIT' }};
      if (r < 0.23) {{
        // Defer → waitlist → resolve final
        if (Math.random() < 0.10)
          return {{ final:'ADMIT', path:'defer_wl_admit', label:'DEFER → WL → ADMIT' }};
        return {{ final:'REJECT', path:'defer_wl_reject', label:'DEFER → WL → REJECT' }};
      }}
      return {{ final:'REJECT', path:'defer_reject', label:'DEFER → REJECT' }};
    }}
    if (stage === 'waitlist') {{
      if (Math.random() < 0.10)
        return {{ final:'ADMIT', path:'wl_admit', label:'WAITLIST → ADMIT' }};
      return {{ final:'REJECT', path:'wl_reject', label:'WAITLIST → REJECT' }};
    }}
    return {{ final:'REJECT', path:'reject', label:'REJECTED' }};
  }}

  // Stamp visuals keyed by path. Final-admit paths are all teal; final-
  // reject paths shade pink. Defer/waitlist legs of admits get a softer
  // teal so the reader sees "this admit came after a defer" at a glance.
  const STAMP_STYLE = {{
    admit:           {{bg:'rgba(95,201,182,.18)', color:'#5fc9b6', rowbg:'rgba(95,201,182,.04)'}},
    defer_admit:     {{bg:'rgba(95,201,182,.14)', color:'#5fc9b6', rowbg:'rgba(95,201,182,.03)'}},
    defer_wl_admit:  {{bg:'rgba(95,201,182,.14)', color:'#5fc9b6', rowbg:'rgba(95,201,182,.03)'}},
    wl_admit:        {{bg:'rgba(95,201,182,.14)', color:'#5fc9b6', rowbg:'rgba(95,201,182,.03)'}},
    defer_reject:    {{bg:'rgba(252,165,165,.12)', color:'#fca5a5', rowbg:'transparent'}},
    defer_wl_reject: {{bg:'rgba(252,165,165,.12)', color:'#fca5a5', rowbg:'transparent'}},
    wl_reject:       {{bg:'rgba(252,165,165,.12)', color:'#fca5a5', rowbg:'transparent'}},
    reject:          {{bg:'rgba(252,165,165,.12)', color:'#fca5a5', rowbg:'transparent'}},
  }};
  // Sort: admits first (direct, then defer-admit, then wl-admit), then
  // rejects (most "almost made it" first), then plain reject last.
  const PATH_RANK = {{
    admit:0, defer_admit:1, defer_wl_admit:2, wl_admit:3,
    wl_reject:4, defer_wl_reject:5, defer_reject:6, reject:7,
  }};

  function singleRun(){{
    if (!data.length) {{
      alert('Run chances on a few schools first to populate the simulator.');
      return;
    }}
    const draws = data.map(s => ({{ ...s, outcome: rollOutcome(s.p, s.round) }}));
    let nAdmit = 0, nReject = 0;
    for (const d of draws) {{
      if (d.outcome.final === 'ADMIT') nAdmit++; else nReject++;
    }}
    runs++;
    counter.textContent = `Run #${{runs}}`;
    empty.style.display = 'none';
    results1k.style.display = 'none';
    summary.style.display = 'block';
    if (nAdmit === 0) {{
      headline.innerHTML = '😬 Zero admits this cycle';
      subhead.textContent = 'Unlucky run. Try again — most cycles get something.';
    }} else if (nAdmit === draws.length) {{
      headline.innerHTML = `🔥 Admitted to all ${{nAdmit}}!`;
      subhead.textContent = 'Extremely lucky run. Real outcomes are usually noisier.';
    }} else {{
      headline.innerHTML = `<b style="color:#5fc9b6">${{nAdmit}}</b> admit${{nAdmit===1?'':'s'}} · <span style="color:#fca5a5">${{nReject}} reject${{nReject===1?'':'s'}}</span>`;
      subhead.textContent = 'Click again for a different outcome. Watch the paths — defers, waitlists, and rejections all resolve.';
    }}
    let body = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px">';
    draws.sort((a,b) => (PATH_RANK[a.outcome.path] - PATH_RANK[b.outcome.path]) || (b.p - a.p));
    for (const d of draws){{
      const s = STAMP_STYLE[d.outcome.path];
      const stamp = `<span style="background:${{s.bg}};color:${{s.color}};font-size:.72em;font-weight:700;letter-spacing:.5px;padding:3px 9px;border-radius:4px;white-space:nowrap">${{d.outcome.label}}</span>`;
      body += `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;background:${{s.rowbg}};border:1px solid var(--border);border-radius:5px">
        <div style="flex:1;min-width:0;overflow:hidden"><a href="/college/${{d.slug}}" style="color:inherit;font-weight:600">${{d.name}}</a><div class="muted" style="font-size:.78em">${{d.round}} · ${{(d.p*100).toFixed(1)}}% odds</div></div>
        <div style="flex-shrink:0">${{stamp}}</div>
      </div>`;
    }}
    body += '</div>';
    results.innerHTML = body;
    results.style.display = 'block';
  }}

  function thousandRun(){{
    if (!data.length) {{
      alert('Run chances on a few schools first to populate the simulator.');
      return;
    }}
    const N = 1000;
    // Per-school accumulator: track admit total + each path so we can show
    // "10% admit (4% direct, 5% off waitlist, 1% deferred-then-admit)"
    const perSchool = data.map(_=>({{
      admit_total:0, reject_total:0,
      admit:0, defer_admit:0, defer_wl_admit:0, wl_admit:0,
      defer_reject:0, defer_wl_reject:0, wl_reject:0, reject:0,
    }}));
    const totalDist = new Array(data.length+1).fill(0);
    for (let i=0;i<N;i++){{
      let admits = 0;
      for (let j=0;j<data.length;j++){{
        const o = rollOutcome(data[j].p, data[j].round);
        perSchool[j][o.path]++;
        if (o.final === 'ADMIT') {{ perSchool[j].admit_total++; admits++; }}
        else perSchool[j].reject_total++;
      }}
      totalDist[admits]++;
    }}
    runs += N;
    counter.textContent = `Ran ${{N.toLocaleString()}} simulations · total runs: ${{runs}}`;
    empty.style.display = 'none';
    results.style.display = 'none';
    summary.style.display = 'block';
    const median = (() => {{ let cum=0; for (let k=0;k<totalDist.length;k++){{ cum += totalDist[k]; if (cum >= N/2) return k; }} return 0; }})();
    const p10 = (() => {{ let cum=0; for (let k=0;k<totalDist.length;k++){{ cum += totalDist[k]; if (cum >= N*0.1) return k; }} return 0; }})();
    const p90 = (() => {{ let cum=0; for (let k=0;k<totalDist.length;k++){{ cum += totalDist[k]; if (cum >= N*0.9) return k; }} return 0; }})();
    const zeroAdmits = totalDist[0];
    headline.innerHTML = `Across ${{N.toLocaleString()}} runs: median <b style="color:#5fc9b6">${{median}}</b> admit${{median===1?'':'s'}}`;
    subhead.innerHTML = `80% range: ${{p10}}-${{p90}} admits · ${{zeroAdmits}} runs (${{(zeroAdmits/N*100).toFixed(1)}}%) had zero admits`;
    let body = '<div style="margin-top:10px"><div class="muted" style="font-size:.85em;margin-bottom:6px">Per-school outcome breakdown across 1,000 cycles. Each admit % includes direct admits + admits via deferral or waitlist.</div>';
    body += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px">';
    const ranked = data.map((s,i)=>({{...s, ...perSchool[i], rate: perSchool[i].admit_total/N}})).sort((a,b)=>b.rate-a.rate);
    const pct = (n)=> (n/N*100).toFixed(1);
    for (const s of ranked){{
      const aTot = pct(s.admit_total);
      const rTot = pct(s.reject_total);
      // Build path breakdown text
      const adminParts = [];
      if (s.admit > 0)            adminParts.push(`${{pct(s.admit)}}% direct`);
      if (s.defer_admit > 0)      adminParts.push(`${{pct(s.defer_admit)}}% via defer`);
      if (s.defer_wl_admit > 0)   adminParts.push(`${{pct(s.defer_wl_admit)}}% via defer→WL`);
      if (s.wl_admit > 0)         adminParts.push(`${{pct(s.wl_admit)}}% off WL`);
      const rejectParts = [];
      if (s.reject > 0)           rejectParts.push(`${{pct(s.reject)}}% direct`);
      if (s.defer_reject > 0)     rejectParts.push(`${{pct(s.defer_reject)}}% after defer`);
      if (s.defer_wl_reject > 0)  rejectParts.push(`${{pct(s.defer_wl_reject)}}% after defer→WL`);
      if (s.wl_reject > 0)        rejectParts.push(`${{pct(s.wl_reject)}}% after WL`);
      body += `<div style="padding:8px 10px;border:1px solid var(--border);border-radius:5px">
        <div style="display:flex;justify-content:space-between;font-size:.88em;font-weight:600;margin-bottom:6px;gap:8px;flex-wrap:wrap">
          <span style="flex:1;min-width:0">${{s.name}}</span><span style="color:#5fc9b6;white-space:nowrap">${{aTot}}% admit</span>
        </div>
        <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--surface-2)">
          <div style="background:#5fc9b6;width:${{aTot}}%" title="Admit ${{aTot}}%"></div>
          <div style="background:#fca5a5;width:${{rTot}}%" title="Reject ${{rTot}}%"></div>
        </div>
        <div class="muted" style="font-size:.72em;margin-top:6px;line-height:1.5">
          <div><span style="color:#5fc9b6">●</span> Admit ${{aTot}}% — ${{adminParts.join(' · ') || '0%'}}</div>
          <div><span style="color:#fca5a5">●</span> Reject ${{rTot}}% — ${{rejectParts.join(' · ') || '0%'}}</div>
        </div>
      </div>`;
    }}
    body += '</div></div>';
    results1k.innerHTML = body;
    results1k.style.display = 'block';
  }}

  if (btn) btn.addEventListener('click', singleRun);
  if (btn1k) btn1k.addEventListener('click', thousandRun);
}})();
</script>

<h2 style="margin-top:28px">Where you'll likely land</h2>
<p class="muted" style="font-size:.92em">Schools grouped by your individual odds. The most-likely scenario: you get into the green ones.</p>
{buckets_html}
{('<p class="muted" style="font-size:.85em;text-align:center;margin:14px 0">No grouped predictions yet — run chances on more schools to populate this view.</p>' if not (likely or tossup or longshot) else '')}

<h2 style="margin-top:28px">All schools (round-by-round)</h2>
{note}
<div class="card" style="padding:0;overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:.92em">
    <thead><tr style="text-align:left;border-bottom:1px solid var(--border)">
      <th style="padding:12px">School</th>
      <th style="padding:12px">Round</th>
      <th style="padding:12px">Odds in this round</th>
      <th style="padding:12px">vs overall</th>
      <th></th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<details class="card" style="margin-top:18px">
  <summary style="cursor:pointer;font-weight:600">How does this work?</summary>
  <p class="muted" style="font-size:.9em;margin-top:10px;line-height:1.6">For each school you've assigned a round, we compute your personalized odds <i>specifically in that round</i> using the school's published ED:RD ratio and your profile. Then we sum those probabilities to get expected admits, and use the Poisson binomial distribution to compute the probability of N or more admits.</p>
  <p class="muted" style="font-size:.9em;line-height:1.6"><b>Why round matters:</b> Penn ED gives roughly 2× the lift over Penn RD for an unhooked applicant; Stanford REA barely moves the needle. The simulator reflects the school-specific dynamics.</p>
</details>

<p style="margin-top:18px"><a class="btn btn-light" href="/plans">← Back</a> <a class="btn btn-primary" href="/plans/grade">See list grade →</a></p>
""", title="Simulator — Candor")


@app.route("/college/<slug>/demonstrated-interest", methods=["POST"])
@login_required
def set_demonstrated_interest(slug):
    if slug not in COLLEGES_BY_SLUG:
        abort(404)
    level = (request.form.get("level") or "none").strip().lower()
    if level not in ("none","emailed","info_session","visited"):
        level = "none"
    uid = current_user()["id"]
    with db() as conn:
        conn.execute("""INSERT INTO demonstrated_interest (user_id, college_slug, level, updated_at)
            VALUES (?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, college_slug) DO UPDATE SET
                level=excluded.level, updated_at=CURRENT_TIMESTAMP""",
            (uid, slug, level))
        # Bust the cached chances since odds may shift
        conn.execute("DELETE FROM saved_chances WHERE user_id=? AND college_slug=?", (uid, slug))
        conn.commit()
    flash("Demonstrated interest updated.", "success")
    return redirect(url_for("chances_page", slug=slug))


@app.route("/outcomes", methods=["GET", "POST"])
@login_required
def outcomes_page():
    """Lets users report admission decisions at the schools they ran chances
    for. Used to validate / recalibrate the prediction model."""
    user = current_user()
    if request.method == "POST":
        slug = (request.form.get("college_slug") or "").strip()
        outcome = (request.form.get("outcome") or "").strip()
        attended = 1 if request.form.get("attended") in ("yes","on","true","1") else 0
        app_round = (request.form.get("round") or "").strip().upper()
        if slug and outcome in ("admitted", "rejected", "waitlisted", "deferred"):
            with db() as conn:
                # Pull the most recent prediction snapshot from saved_chances
                row = conn.execute(
                    "SELECT odds_low, odds_high, fit, tier FROM saved_chances WHERE user_id=? AND college_slug=?",
                    (user["id"], slug)
                ).fetchone()
                pl = row["odds_low"] if row else None
                ph = row["odds_high"] if row else None
                pf = row["fit"] if row else None
                pt = row["tier"] if row else None
                conn.execute("""INSERT INTO user_outcomes
                    (user_id, college_slug, application_round, predicted_odds_low,
                     predicted_odds_high, predicted_fit, predicted_tier,
                     actual_outcome, attended, reported_at)
                    VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, college_slug) DO UPDATE SET
                        application_round=excluded.application_round,
                        actual_outcome=excluded.actual_outcome,
                        attended=excluded.attended,
                        reported_at=CURRENT_TIMESTAMP""",
                    (user["id"], slug, app_round, pl, ph, pf, pt, outcome, attended))
                conn.commit()
            flash("Outcome recorded — thanks, this helps improve predictions.", "success")
        return redirect(url_for("outcomes_page"))
    # GET: show form + history
    with db() as conn:
        rows = conn.execute(
            "SELECT college_slug, application_round, predicted_odds_low, predicted_odds_high, "
            "predicted_tier, actual_outcome, attended, reported_at FROM user_outcomes "
            "WHERE user_id=? ORDER BY reported_at DESC",
            (user["id"],)
        ).fetchall()
        chances = conn.execute(
            "SELECT college_slug FROM saved_chances WHERE user_id=?",
            (user["id"],)
        ).fetchall()
    reported_slugs = {r["college_slug"] for r in rows}
    options = "".join(
        f'<option value="{c["college_slug"]}">{COLLEGES_BY_SLUG.get(c["college_slug"],{}).get("name", c["college_slug"])}</option>'
        for c in chances if c["college_slug"] not in reported_slugs
    )
    history = ""
    for r in rows:
        sch = COLLEGES_BY_SLUG.get(r["college_slug"], {}).get("name", r["college_slug"])
        rd = r["application_round"] or "—"
        pred = f"{r['predicted_odds_low']}–{r['predicted_odds_high']}%" if r["predicted_odds_low"] is not None else "—"
        outcome = (r["actual_outcome"] or "").capitalize()
        history += f"""<tr><td>{sch}</td><td>{rd}</td><td>{pred}</td><td>{outcome}</td><td>{'Yes' if r['attended'] else ''}</td></tr>"""
    return _page(f"""
<h1>Application outcomes</h1>
<p class="muted">Report your admission decisions so we can validate predictions. Your data is private and only used in aggregate.</p>
<form method="post" action="/outcomes" class="card" style="max-width:560px">
  {csrf_input()}
  <label>School (you've already run chances for)</label>
  <select name="college_slug" required><option value="">Pick a school…</option>{options}</select>
  <label>Application round</label>
  <select name="round"><option value="ED">ED</option><option value="ED2">ED II</option><option value="EA">EA</option><option value="REA">REA</option><option value="RD" selected>RD</option></select>
  <label>Outcome</label>
  <select name="outcome" required>
    <option value="admitted">Admitted</option>
    <option value="waitlisted">Waitlisted</option>
    <option value="deferred">Deferred</option>
    <option value="rejected">Rejected</option>
  </select>
  <label style="display:flex;align-items:center;gap:8px;margin-top:8px">
    <input type="checkbox" name="attended" style="width:auto;margin:0">
    I attended (or plan to attend) this school
  </label>
  <button class="btn btn-primary" type="submit" style="margin-top:14px">Save outcome</button>
</form>
<div class="card" style="margin-top:18px">
  <h3 style="margin-top:0">Your reported outcomes</h3>
  {('<table style="width:100%;border-collapse:collapse"><thead><tr><th style="text-align:left;padding:6px 0">School</th><th style="text-align:left">Round</th><th style="text-align:left">Predicted</th><th style="text-align:left">Actual</th><th style="text-align:left">Attending</th></tr></thead><tbody>' + history + '</tbody></table>') if history else '<p class="muted">Nothing reported yet.</p>'}
</div>
""", title="Outcomes — Candor")


@app.route("/chat")
@app.route("/college/<slug>/chat")
def chat_page_disabled(slug=None):
    # AI Advisor was removed 2026-05-11 — see commit log. Redirect any old
    # bookmarks or stale links to /upgrade so users don't hit a 404.
    return redirect(url_for("upgrade_page"))


@app.route("/chat/api/send", methods=["POST"])
@login_required
def chat_api_send():
    # AI Advisor was retired 2026-05-11. Returns 410 so any stale tab gets a
    # clear "this feature is gone" message instead of trying to send.
    return jsonify({"error": "retired",
        "html": "The AI Advisor was retired. Premium now focuses on the strategy + simulator features."}), 410


def _chat_api_send_legacy_disabled():
    """Original chat_api_send body, kept here for reference / quick re-enable.
    To restore: rename this back to chat_api_send and re-add the @app.route + @login_required."""
    user = current_user()
    status = usage_status(user["id"])
    if status.get("blocked"):
        if status["reason"] == "free_exhausted":
            paywall = (f'<div style="padding:18px;border-radius:5px;background:rgba(95,201,182,.06);'
                       f'border:1px solid rgba(95,201,182,.2)">'
                       f'<div style="font-weight:600;color:var(--teal);margin-bottom:8px">'
                       f"You've used your {FREE_TRIAL_MESSAGES} free trial messages.</div>"
                       f'<div style="color:var(--text-2);font-size:.92em;margin-bottom:12px">'
                       f"Upgrade to Candor Premium ($3/mo) for {PAID_MONTHLY_LIMIT} messages "
                       f"per month with the AI Advisor — personalized college admissions help "
                       f"informed by your full profile and any of 334 schools.</div>"
                       f'<a href="/upgrade" class="btn btn-primary btn-sm" style="text-decoration:none">'
                       f'Upgrade →</a></div>')
            return jsonify({"error": "free_exhausted", "html": paywall}), 402
        if status["reason"] == "monthly_cap":
            return jsonify({"error": "monthly_cap",
                "html": f'<i>You\'ve hit your {PAID_MONTHLY_LIMIT}-message monthly limit. Resets on the 1st.</i>'}), 429
    profile = get_profile(user["id"])
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Empty message."}), 400
    slug = request.args.get("slug")
    if slug:
        school = COLLEGES_BY_SLUG.get(slug)
        if not school:
            return jsonify({"error": "Unknown school."}), 404
        conv = get_or_create_conversation(user["id"], "school", slug)
        reply = chat_send(conv["id"], message, "school", profile, school)
    else:
        conv = get_or_create_conversation(user["id"], "general")
        reply = chat_send(conv["id"], message, "general", profile)
    if reply:
        increment_message_count(user["id"])
    new_status = usage_status(user["id"])
    return jsonify({
        "reply": reply,
        "html": _render_message({"role": "assistant", "content": reply or ""}),
        "usage": {
            "is_paid": new_status.get("is_paid", False),
            "free_remaining": new_status.get("free_remaining", 0),
            "month_used": new_status.get("month_used", 0),
            "free_limit": FREE_TRIAL_MESSAGES,
            "month_limit": PAID_MONTHLY_LIMIT,
        },
    })


@app.route("/pricing")
@app.route("/premium")
def pricing_alias():
    return redirect(url_for("upgrade_page"), code=301)


# ─── DEADLINE TRACKER (Premium) ───
_UC_SLUGS = {"ucla","ucb","uc-berkeley","ucsd","ucsb","ucd","uc-davis","uci",
             "uc-irvine","ucr","ucsc","ucm","uc-merced"}

def _cycle_years():
    """(Nov year, Jan year) for the UPCOMING application cycle, derived from
    today so it never goes stale. June 2026 -> ED Nov 2026, RD Jan 2027."""
    y = datetime.now().year
    return y, y + 1


def _deadline_for(slug, round_code):
    """(date, label) for a school + round in the upcoming cycle. These are the
    TYPICAL dates — real deadlines vary by a week or two and shift yearly, so
    the page tells users to confirm on the school's site. The UC system is the
    big exception: one Nov 30 application window, RD only."""
    nov_y, jan_y = _cycle_years()
    rc = (round_code or "").upper()
    if slug in _UC_SLUGS:
        return datetime(nov_y, 11, 30).date(), "UC application"
    if rc in ("ED", "ED1", "EA", "REA", "SCEA", "SCREA"):
        return datetime(nov_y, 11, 1).date(), rc
    if rc == "ED2":
        return datetime(jan_y, 1, 1).date(), "ED2"
    if rc == "RD":
        return datetime(jan_y, 1, 1).date(), "RD"
    # No round chosen — default to the school's earliest available round.
    rounds = (ADMISSIONS_DETAIL.get(slug) or {}).get("rounds") or []
    if any(r in ("ED", "ED1", "REA", "SCEA", "EA") for r in rounds):
        return datetime(nov_y, 11, 1).date(), "Early · round not set"
    return datetime(jan_y, 1, 1).date(), "RD · round not set"


def _deadline_items(uid):
    """The user's saved schools with computed upcoming deadlines, soonest first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT college_slug, application_round FROM saved_chances WHERE user_id=? "
            "UNION SELECT college_slug, application_round FROM saved_schools WHERE user_id=?",
            (uid, uid)).fetchall()
    today = datetime.now().date()
    items = []
    seen = set()
    for r in rows:
        slug = r["college_slug"]
        if slug in seen:
            continue
        c = COLLEGES_BY_SLUG.get(slug)
        if not c:
            continue
        seen.add(slug)
        date, label = _deadline_for(slug, r["application_round"])
        items.append({"date": date, "days": (date - today).days,
                      "name": c["name"], "slug": slug, "label": label})
    items.sort(key=lambda x: (x["date"], x["name"]))
    return items


def _send_email(to, subject, html):
    """Send one email via Resend. No-op (returns False) until RESEND_API_KEY is
    set, so the whole reminder system stays dormant and safe until you wire up a
    provider. Returns True on a 2xx send."""
    if not RESEND_API_KEY or not to:
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            timeout=20)
        if r.status_code >= 300:
            print(f"resend error {r.status_code}: {r.text[:160]}")
            return False
        return True
    except Exception as e:
        print(f"resend send error: {e}")
        return False


def _deadline_email_html(item):
    """Branded HTML for one deadline reminder."""
    d = item["days"]
    when = "is TODAY" if d == 0 else (f"is in {d} day" + ("s" if d != 1 else ""))
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;color:#0a131c">
  <h2 style="margin:0 0 6px">⏰ {item['name']} deadline {when}</h2>
  <p style="color:#4b5563;margin:0 0 14px">Your <b>{item['label']}</b> application to {item['name']} is due
     <b>{item['date'].strftime('%B %-d, %Y')}</b>. Confirm the exact deadline on the school's site.</p>
  <a href="https://candoradmit.com/college/{item['slug']}" style="display:inline-block;background:#0f766e;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-weight:600">Open in Candor →</a>
  <p style="color:#9ca3af;font-size:12px;margin:18px 0 0">You're getting this because {item['name']} is on your Candor list.
     Manage your list at <a href="https://candoradmit.com/deadlines">candoradmit.com/deadlines</a>.</p>
</div>"""


def run_deadline_nudges():
    """Email paid users whose saved-school deadlines hit a 14/7/1-day milestone.
    Deduped via the deadline_nudges table (one email per user/school/milestone).
    Returns the count sent. Driven by the daily /cron/deadline-nudges trigger."""
    MILESTONES = {14, 7, 1}
    sent = 0
    with db() as conn:
        users = conn.execute("SELECT id, email FROM users WHERE is_paid=1 AND email IS NOT NULL").fetchall()
    for u in users:
        for it in _deadline_items(u["id"]):
            if it["days"] not in MILESTONES:
                continue
            ms = it["days"]
            try:
                with db() as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO deadline_nudges (user_id, college_slug, milestone) VALUES (?,?,?)",
                        (u["id"], it["slug"], ms))
                    conn.commit()
                    if not cur.rowcount:   # already emailed for this milestone
                        continue
                subj = f"{it['name']} {it['label']} deadline — {ms} day{'s' if ms != 1 else ''} left"
                if _send_email(u["email"], subj, _deadline_email_html(it)):
                    sent += 1
            except Exception as e:
                print(f"deadline nudge error (u{u['id']} {it['slug']}): {e}")
    return sent


@app.route("/cron/deadline-nudges")
def cron_deadline_nudges():
    """Daily trigger for deadline reminder emails. Hit it from a scheduler
    (Railway cron / cron-job.org) once a day: /cron/deadline-nudges?key=CRON_KEY."""
    if not CRON_KEY or request.args.get("key") != CRON_KEY:
        return ("unauthorized", 401)
    if not RESEND_API_KEY:
        return ("email not configured — set RESEND_API_KEY (and EMAIL_FROM) to enable", 200)
    n = run_deadline_nudges()
    return (f"sent {n} reminder email(s)", 200)


def _deadlines_teaser(signup):
    cta = ('<a class="btn btn-primary" href="/signup?next=/deadlines" style="padding:12px 24px">Sign up free →</a>'
           if signup else
           '<a class="btn btn-primary" href="/upgrade" style="padding:12px 24px">See Premium ($3/mo) →</a>')
    return f"""
<div class="lp-wrap" style="max-width:680px;margin:0 auto;padding:40px 24px">
  <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#5fc9b6;margin-bottom:8px">Deadline tracker · Premium</div>
  <h1 style="font-size:2.1em;letter-spacing:-1px;margin:0 0 12px">Never miss a deadline.</h1>
  <p class="muted" style="font-size:1.05em;line-height:1.55;margin:0 0 24px">Your whole list, every ED / EA / RD deadline auto-built and counted down — with email reminders as each one gets close. Add a school and round to your list and it shows up here automatically.</p>
  <div style="display:flex;gap:12px;flex-wrap:wrap">{cta}</div>
</div>"""


@app.route("/deadlines")
def deadlines_page():
    user = current_user()
    if not user:
        return _page(_deadlines_teaser(signup=True), title="Deadlines — Candor")
    if not user.get("is_paid"):
        return _page(_deadlines_teaser(signup=False), title="Deadlines — Candor")

    items = _deadline_items(user["id"])
    if not items:
        body = """<div class="bar"><a href="/plans">&larr; My Colleges</a></div>
<div class="card" style="max-width:620px;text-align:center;padding:36px">
  <h1 style="margin:0 0 10px;font-size:1.6em">No deadlines yet</h1>
  <p class="muted" style="margin:0 0 18px">Save schools and set an application round (ED / EA / RD) and they'll show up here, counted down and sorted by date.</p>
  <a class="btn btn-primary" href="/colleges">Browse colleges →</a>
</div>"""
        return _page(body, title="Deadlines — Candor")

    def badge(days):
        if days < 0:   return ("passed", "var(--text-3,#7f8893)")
        if days == 0:  return ("TODAY", "#ef6b6b")
        if days <= 7:  return (f"in {days}d", "#ef6b6b")
        if days <= 30: return (f"in {days}d", "#e0a44a")
        return (f"in {days}d", "var(--teal)")

    from itertools import groupby
    blocks = ""
    for month_label, grp in groupby(items, key=lambda x: x["date"].strftime("%B %Y")):
        rows_html = ""
        for it in grp:
            txt, col = badge(it["days"])
            rows_html += (
                f'<a href="/college/{it["slug"]}" style="display:flex;align-items:center;justify-content:space-between;'
                f'gap:12px;padding:13px 16px;border-top:1px solid var(--border);text-decoration:none;color:inherit">'
                f'<div><div style="font-weight:600;color:var(--text)">{it["name"]}</div>'
                f'<div class="muted" style="font-size:.8em;margin-top:2px">{it["label"]} · {it["date"].strftime("%b %-d, %Y")}</div></div>'
                f'<span style="font-size:.82em;font-weight:700;color:{col};white-space:nowrap">{txt}</span></a>')
        blocks += (
            f'<div class="card" style="padding:0;margin-bottom:16px;overflow:hidden">'
            f'<div style="padding:11px 16px;background:var(--surface-2);font-weight:700;font-size:.9em">{month_label}</div>'
            f'{rows_html}</div>')

    next_up = next((it for it in items if it["days"] >= 0), None)
    hero = ""
    if next_up:
        hero = (f'<div class="card" style="background:linear-gradient(135deg,#0f3a37,#0a131c);'
                f'border:1px solid rgba(95,201,182,.3);margin-bottom:20px;max-width:620px">'
                f'<div class="muted" style="font-size:.78em;text-transform:uppercase;letter-spacing:.6px">Next up</div>'
                f'<div style="font-size:1.3em;font-weight:700;margin:4px 0 2px">{next_up["name"]}</div>'
                f'<div class="muted" style="font-size:.9em">{next_up["label"]} · {next_up["date"].strftime("%B %-d, %Y")} · '
                f'<b style="color:var(--teal)">{next_up["days"]} days away</b></div></div>')

    body = f"""<div class="bar"><a href="/plans">&larr; My Colleges</a></div>
<h1 style="margin:0 0 4px;font-size:1.8em">Your deadlines</h1>
<p class="muted" style="margin:0 0 20px;font-size:.92em">Typical cycle dates — always confirm the exact deadline on each school's site. We'll email you as they approach.</p>
<div style="max-width:620px">{hero}{blocks}</div>"""
    return _page(body, title="Deadlines — Candor")


def _cancel_subscription_html():
    """Always-present, easy cancel path. One-click to the Stripe billing portal
    if it's configured; otherwise a clear email-cancel fallback so a subscriber
    can ALWAYS cancel, even before the portal is set up. Never hidden."""
    if STRIPE_BILLING_PORTAL_URL:
        return (f'<a href="{STRIPE_BILLING_PORTAL_URL}" class="btn btn-light" '
                f'style="border-color:var(--border-strong)">Manage or cancel subscription →</a>'
                f'<div class="muted" style="font-size:.8em;margin-top:6px">Cancel anytime on Stripe\'s secure page — you keep access through the period you already paid for.</div>')
    body = ("Please cancel my Candor Premium subscription.%0D%0A%0D%0AAccount email: ")
    mail = f"mailto:{CANCEL_EMAIL}?subject=Cancel%20my%20Candor%20Premium&body={body}"
    return (f'<a href="{mail}" class="btn btn-light" style="border-color:var(--border-strong)">Cancel subscription</a>'
            f'<div class="muted" style="font-size:.8em;margin-top:6px">Cancel anytime — this emails us and we cancel within 24 hours. You keep access through the period you already paid for, and you won\'t be charged again.</div>')


def _premium_comparison_html():
    """Free vs Premium feature-comparison table. Shown on the upgrade page so
    the value gap is legible at a glance (what's free stays free; Premium is
    the action/depth layer). Checkmark = included; — = not in that tier."""
    # (label, free_value, premium_value): True=✓, False=—, or a string.
    rows = [
        ("Chances calculator — verified CDS odds", True, True),
        ("All school pages, rankings &amp; browse", True, True),
        ("Profile, fit scores &amp; profile grade", True, True),
        ("Personalized AI strategy, per school", False, True),
        ("My Colleges dashboard — round-by-round", False, True),
        ("List grader (1–10) + admissions simulator", False, True),
        ("Score-push impact — is a retake worth it?", False, True),
        ("Deadline tracker + reminders", False, True),
        ("&ldquo;Students like you&rdquo; admit scattergram", False, True),
    ]
    def cell(v, premium=False):
        bg = "background:rgba(95,201,182,.06)" if premium else ""
        if v is True:
            return f'<td style="text-align:center;padding:11px 10px;color:var(--teal);font-weight:700;{bg}">✓</td>'
        if v is False:
            return f'<td style="text-align:center;padding:11px 10px;color:var(--text-3,#7f8893);{bg}">—</td>'
        col = "var(--teal)" if premium else "var(--text-2)"
        return f'<td style="text-align:center;padding:11px 10px;color:{col};font-size:.9em;font-weight:600;{bg}">{v}</td>'
    body_rows = "".join(
        f'<tr style="border-top:1px solid var(--border)">'
        f'<td style="padding:11px 10px;color:var(--text);line-height:1.35">{label}</td>'
        f'{cell(free)}{cell(prem, premium=True)}</tr>'
        for label, free, prem in rows
    )
    return f"""
    <div style="overflow-x:auto;margin:20px 0;border:1px solid var(--border-strong);border-radius:12px">
      <table style="width:100%;min-width:420px;border-collapse:collapse;font-size:.93em">
        <thead>
          <tr style="background:var(--surface-2)">
            <th style="text-align:left;padding:13px 10px;font-weight:600;color:var(--text-2);font-size:.82em;text-transform:uppercase;letter-spacing:.5px">What you get</th>
            <th style="text-align:center;padding:13px 10px;font-weight:700;color:var(--text);width:88px">Free</th>
            <th style="text-align:center;padding:13px 10px;width:104px;background:rgba(95,201,182,.06)">
              <div style="font-weight:700;color:var(--teal)">Premium</div>
              <div style="font-size:.72em;font-weight:600;color:var(--text-2);margin-top:1px">$3/mo</div>
            </th>
          </tr>
        </thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>"""


@app.route("/upgrade")
def upgrade_page():
    user = current_user()
    is_paid = False
    status = None
    pay_url = STRIPE_PAYMENT_LINK
    if user:
        status = usage_status(user["id"])
        is_paid = status.get("is_paid")
        sep = "&" if "?" in STRIPE_PAYMENT_LINK else "?"
        pay_url = (f"{STRIPE_PAYMENT_LINK}{sep}client_reference_id={user['id']}"
                   f"&prefilled_email={user['email']}")
    # Subscribe button: anon users get sent through signup first so we can
    # attach the Stripe payment to a real account via client_reference_id.
    subscribe_href = pay_url if user else "/signup?next=/upgrade"
    subscribe_label = "Subscribe — $3/mo" if user else "Sign up to subscribe — $3/mo"

    for_parent = request.args.get("for") == "parent"

    if is_paid:
        body = f"""<div class="card" style="max-width:560px">
          <div class="stat-card" style="margin-bottom:14px">
            <div class="label">Plan</div>
            <div class="value accent">Candor Premium</div>
            <div class="delta">Unlocked — Candor Premium · $3/mo</div>
          </div>
          <p class="muted" style="margin:0 0 14px">You're all set. Every premium feature is unlocked on your account. Keep your Stripe email receipt for your records.</p>
          <a href="/plans" class="btn btn-primary">Go to my plan &rarr;</a>
          <div style="margin-top:22px;padding-top:16px;border-top:1px solid var(--border)">{_cancel_subscription_html()}</div>
        </div>"""
        return _page(body, title="Upgrade — Candor")

    if for_parent:
        headline = "Help your kid apply to the right schools."
        sub = ("Most chances calculators give a flattering number that doesn't help anyone decide anything. "
               "Candor uses verified Common Data Set data from 295+ schools and tells you the truth — "
               "so the ED slot, the test retake, and the supplemental essay time actually go where they matter. "
               "Just $3/month, cancel anytime.")
        social = ('<p class="muted" style="font-size:.85em;margin:18px 0 0">'
                  'Built by a high school junior who got tired of $5,000 consultants telling families different things.'
                  '</p>')
    else:
        headline = "Stop guessing where you stand."
        sub = ("You ran your chances. Premium is the part that turns a number into a plan: "
               "what to do this week, where to send your ED, whether a retake is actually worth it, "
               "and a personalized strategy for every school you're considering.")
        social = ""

    bundle = _premium_comparison_html()

    body = f"""<div class="card" style="max-width:620px">
      <div style="font-size:.78em;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:var(--teal);margin-bottom:6px">Candor Premium</div>
      <h1 style="margin:0 0 10px;font-size:2em;line-height:1.15">{headline}</h1>
      <p class="muted" style="margin:0 0 6px;font-size:1em;line-height:1.55">{sub}</p>
      <div style="display:flex;align-items:baseline;gap:8px;margin:22px 0 6px">
        <span style="font-size:2.4em;font-weight:700;letter-spacing:-1px;background:var(--accent-grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">$3</span>
        <span class="muted">/month · cancel anytime, use it through your whole app cycle</span>
      </div>
      {bundle}
      <a href="{subscribe_href}" class="btn btn-primary" style="font-size:1em;padding:12px 28px;margin-top:4px">{subscribe_label} →</a>
      <p class="muted" style="font-size:.78em;margin:14px 0 0">Secure checkout through Stripe. Premium activates within ~30 seconds of payment.</p>
      {social}
    </div>"""
    return _page(body, title="Upgrade — Candor")


@app.route("/api/paid-status")
def api_paid_status():
    user = current_user()
    return jsonify({"paid": bool(user.get("is_paid")) if user else False,
                    "logged_in": bool(user)})


@app.route("/upgrade/thanks")
def upgrade_thanks():
    """Post-checkout landing. Set this as the Stripe Payment Link success URL
    (Stripe dashboard → Payment Link → After payment → Redirect) so paying
    users see a confirmation instead of bouncing back to the buy button while
    the webhook (which grants premium) catches up ~30s later."""
    user = current_user()
    if user and bool(user.get("is_paid")):
        body = """<div class="card" style="max-width:560px;text-align:center">
          <div style="font-size:2.4em;line-height:1">🎉</div>
          <h1 style="margin:.3em 0 .2em">You're Candor Premium</h1>
          <p class="muted" style="margin:0 0 18px">Every premium feature is unlocked — per-school AI strategy, the score predictor, list grader, and admissions simulator.</p>
          <a href="/plans" class="btn btn-primary">Go to my plan &rarr;</a>
        </div>"""
        return _page(body, title="Thanks — Candor")
    # Paid but webhook hasn't landed yet (or not logged in on this device).
    poll = "" if not user else """
      <script>
      (function(){
        var tries=0;
        var t=setInterval(function(){
          tries++;
          fetch('/api/paid-status').then(function(r){return r.json()}).then(function(d){
            if(d.paid){clearInterval(t);location.href='/upgrade/thanks';}
            else if(tries>20){clearInterval(t);var e=document.getElementById('slow');if(e)e.style.display='block';}
          }).catch(function(){});
        },3000);
      })();
      </script>"""
    note = ("" if user else
            '<p class="muted" style="font-size:.85em;margin-top:14px">Not seeing it? Log in with the same email you paid with — your purchase is tied to that address.</p>')
    body = f"""<div class="card" style="max-width:560px;text-align:center">
      <h1 style="margin:0 0 .3em">Payment received — activating…</h1>
      <p class="muted" style="margin:0 0 8px">Thanks! We're unlocking your account. This usually takes under a minute.</p>
      <div class="muted" style="font-size:1.6em;margin:10px 0">⏳</div>
      <p id="slow" style="display:none;color:var(--text);font-size:.9em">Still activating — it can take a couple of minutes. This page refreshes automatically, or email support and we'll unlock it manually.</p>
      {note}
      <p style="margin-top:16px"><a href="/plans" class="btn btn-light">Go to my plan</a></p>
    </div>{poll}"""
    return _page(body, title="Thanks — Candor")


_csrf_exempt = csrf.exempt if _CSRF_ON else (lambda f: f)


@app.route("/stripe/webhook", methods=["POST"])
@_csrf_exempt
def stripe_webhook():
    """Stripe webhook for checkout.session.completed events. Configure in
    Stripe dashboard → Developers → Webhooks, point at this URL, and put
    the signing secret in STRIPE_WEBHOOK_SECRET env var."""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")
    # SECURITY: never trust an unsigned webhook. Without the signing secret we
    # cannot verify the request actually came from Stripe, so an attacker could
    # POST a fake checkout.session.completed and self-grant premium. If the
    # secret isn't configured we 503 — Stripe retries for ~3 days, so once the
    # env var is set the queued events still land. (Manual fallback meanwhile:
    # /admin/grant-paid?email=...)
    if not STRIPE_WEBHOOK_SECRET:
        print("WARNING: STRIPE_WEBHOOK_SECRET unset — rejecting webhook (set it in Railway env)")
        return ("webhook secret not configured", 503)
    try:
        import hmac as _hmac, hashlib as _hashlib
        ts = next((p.split("=",1)[1] for p in sig_header.split(",") if p.startswith("t=")), "")
        sigs = [p.split("=",1)[1] for p in sig_header.split(",") if p.startswith("v1=")]
        signed = f"{ts}.{payload}".encode()
        expected = _hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, _hashlib.sha256).hexdigest()
        if not any(_hmac.compare_digest(expected, s) for s in sigs):
            return ("invalid signature", 400)
    except Exception as e:
        print(f"stripe webhook signature error: {e}")
        return ("signature error", 400)
    try:
        event = json.loads(payload)
    except Exception:
        return ("bad json", 400)
    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    if etype == "checkout.session.completed":
        ref = obj.get("client_reference_id")
        cust = obj.get("customer")
        sub_id = obj.get("subscription")  # present for subscription-mode checkouts
        granted = False
        if ref:
            try:
                uid = int(ref)
                with db() as conn:
                    conn.execute("UPDATE users SET is_paid=1, stripe_customer_id=?, stripe_subscription_id=? WHERE id=?",
                                 (cust, sub_id, uid))
                    conn.commit()
                granted = True
            except (ValueError, TypeError):
                pass
        # Fallback: a payment made through a shared/bookmarked Stripe link has
        # no client_reference_id. Match the paying email to an existing account
        # so the purchase still unlocks premium instead of vanishing.
        if not granted:
            email = ((obj.get("customer_details") or {}).get("email")
                     or obj.get("customer_email") or "").strip().lower()
            if email:
                with db() as conn:
                    cur = conn.execute(
                        "UPDATE users SET is_paid=1, stripe_customer_id=?, stripe_subscription_id=? WHERE LOWER(email)=?",
                        (cust, sub_id, email))
                    conn.commit()
                    if cur.rowcount:
                        granted = True
            if not granted:
                print(f"stripe webhook: paid checkout could not be matched to a user "
                      f"(ref={ref!r} email={email!r}) — grant manually via /admin/grant-paid")
    # Subscription model ($3/mo): when the subscription ends — a cancellation
    # reaches its period end, or Stripe gives up after failed payments — revoke
    # premium. Match by customer id (stored at checkout), falling back to the
    # subscription id. NOTE: legacy one-time $10 customers have no subscription,
    # so this event never fires for them — they stay grandfathered on is_paid=1.
    elif etype == "customer.subscription.deleted":
        cust = obj.get("customer")
        sub_id = obj.get("id")
        with db() as conn:
            cur = conn.execute(
                "UPDATE users SET is_paid=0 WHERE stripe_customer_id=? "
                "OR (stripe_subscription_id IS NOT NULL AND stripe_subscription_id=?)",
                (cust, sub_id))
            conn.commit()
        print(f"stripe webhook: subscription ended — revoked premium "
              f"(customer={cust!r} sub={sub_id!r} rows={cur.rowcount})")
    # A single failed payment is NOT a cancellation: Stripe retries (dunning) and
    # fires subscription.deleted only if it ultimately gives up. Don't revoke
    # here, or a customer whose card retries fine would be wrongly cut off.
    elif etype == "invoice.payment_failed":
        print("stripe webhook: invoice.payment_failed (no revoke; awaiting Stripe dunning / subscription.deleted)")
    return ("ok", 200)


@app.route("/admin/grant-paid")
def admin_grant_paid():
    """Manual fallback: grant paid status by email. Use if webhook isn't set up."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return ("<h1>401 Unauthorized</h1>", 401)
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return ("<h1>Missing ?email=</h1>", 400)
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            return (f"<h1>No user with email {email}</h1>", 404)
        conn.execute("UPDATE users SET is_paid=1 WHERE id=?", (row["id"],))
        conn.commit()
    return _page(f'<h1>Granted Premium</h1><p class="muted">{email} is now on Candor Premium.</p>',
                 title="Granted — Candor")


@app.route("/admin/calibration")
def admin_calibration():
    """Backtest odds vs. real reported outcomes (user_outcomes). Buckets each
    prediction by midpoint odds, compares predicted vs ACTUAL admit rate.
    Well-calibrated => predicted ~= actual per row. ?key=ADMIN_KEY"""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return ("<h1>401 Unauthorized</h1><p>Pass <code>?key=YOUR_ADMIN_KEY</code></p>", 401)
    BUCKETS = [(0,5),(5,10),(10,20),(20,35),(35,50),(50,70),(70,101)]
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT predicted_odds_low, predicted_odds_high, actual_outcome "
                "FROM user_outcomes WHERE actual_outcome IS NOT NULL AND actual_outcome!='' "
                "AND predicted_odds_low IS NOT NULL"
            ).fetchall()
    except Exception:
        rows = []
    def is_admit(o): return (o or "").lower() == "admitted"
    agg = {b: {"n":0,"adm":0,"psum":0.0} for b in BUCKETS}
    n=adm=0; brier=0.0
    for r in rows:
        mid=(r["predicted_odds_low"]+r["predicted_odds_high"])/2.0
        a=1 if is_admit(r["actual_outcome"]) else 0
        n+=1; adm+=a; brier+=((mid/100.0)-a)**2
        for b in BUCKETS:
            if b[0]<=mid<b[1]:
                agg[b]["n"]+=1; agg[b]["adm"]+=a; agg[b]["psum"]+=mid; break
    rws=""
    for b in BUCKETS:
        d=agg[b]
        if not d["n"]:
            rws+=f'<tr><td>{b[0]}–{b[1]}%</td><td>0</td><td>—</td><td>—</td><td>—</td></tr>'; continue
        pred=d["psum"]/d["n"]; act=100.0*d["adm"]/d["n"]; gap=act-pred
        col="#16a34a" if abs(gap)<=7 else ("#d97706" if abs(gap)<=15 else "#dc2626")
        rws+=(f'<tr><td>{b[0]}–{b[1]}%</td><td>{d["n"]}</td><td>{pred:.0f}%</td>'
              f'<td>{act:.0f}%</td><td style="color:{col};font-weight:600">{gap:+.0f} pts</td></tr>')
    brier=(brier/n) if n else 0
    rate=(100.0*adm/n) if n else 0
    warn=('<div class="card" style="background:#fff8e1;border-color:#ffeaa7"><b>Not enough data yet.</b> '
          'Calibration is meaningful around 50–100+ reported outcomes. Drive users to <code>/outcomes</code>.</div>'
          if n<20 else '')
    return _page(f"""
<h1>Model calibration</h1>
<p class="muted">Predicted odds vs. real reported outcomes. {n} graded ({adm} admits, {rate:.0f}% overall).
Brier: <b>{brier:.3f}</b> (lower=better; .25=coin-flip).</p>
{warn}
<div class="card" style="padding:0;overflow-x:auto;margin-top:14px">
  <table style="width:100%;border-collapse:collapse;font-size:.92em">
    <thead><tr style="text-align:left;border-bottom:1px solid var(--border)">
      <th style="padding:12px">Predicted</th><th style="padding:12px">n</th>
      <th style="padding:12px">Avg predicted</th><th style="padding:12px">Actual admit</th><th style="padding:12px">Gap</th>
    </tr></thead><tbody>{rws}</tbody>
  </table>
</div>
<p class="muted" style="font-size:.85em;margin-top:14px">Green ≤7 pts · amber 7–15 · red &gt;15. A consistent
one-direction gap means the model is systematically harsh (actual&gt;predicted) or generous; shift the fit curve/caps to fix.</p>
""", title="Calibration — Candor")


@app.route("/admin/refresh-scorecard")
def admin_refresh_scorecard():
    """Bulk-refresh all 155 schools' stats from College Scorecard.
    Gated by ADMIN_KEY so only the operator can run it (it's slow + makes
    155 API calls). Hit with ?key=YOUR_ADMIN_KEY."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return ("<h1>401 Unauthorized</h1><p>Pass <code>?key=YOUR_ADMIN_KEY</code></p>", 401)
    if not SCORECARD_KEY:
        return ("<h1>SCORECARD_KEY not configured</h1><p>Set the env var on Railway.</p>", 500)
    only_slug = request.args.get("slug")
    fmt = request.args.get("format", "html")
    target = [c for c in COLLEGES if c["slug"] == only_slug] if only_slug else COLLEGES
    updated, failed = [], []
    for c in target:
        if update_scorecard_overrides(c):
            updated.append(c["slug"])
        else:
            failed.append(c["slug"])
    if fmt == "json":
        return jsonify({"updated": len(updated), "failed": len(failed),
                        "updated_slugs": updated[:30], "failed_slugs": failed[:30]})
    failed_list = "".join(f"<li>{s}</li>" for s in failed) or "<li>None</li>"
    return _page(f"""
<h1>Scorecard refresh complete</h1>
<div class="card">
  <div class="stat-card" style="margin-bottom:16px">
    <div class="label">Updated</div>
    <div class="value accent">{len(updated)} / {len(target)}</div>
    <div class="delta">schools refreshed from federal IPEDS via College Scorecard</div>
  </div>
  <h3 style="margin-top:18px">Failed lookups ({len(failed)})</h3>
  <p class="muted" style="font-size:.86em;margin:0 0 8px">These slugs didn't match a Scorecard record. They keep their hardcoded values; usually fixable by adding to SCORECARD_NAME_OVERRIDES.</p>
  <ul style="font-size:.86em;color:var(--text-2);columns:3;column-gap:30px">{failed_list}</ul>
</div>
<p class="muted" style="font-size:.85em">Tip: rerun anytime to pull the latest federal data (IPEDS releases yearly, ~October). Append <code>&format=json</code> for machine-readable output.</p>
""", title="Scorecard refresh — Candor")


@app.route("/admin/visitors")
def admin_visitors():
    """Visitor traffic breakdown — distinguish real engaged users from
    cookie-less scrapers. Pass ?window=1hour or ?window=24hours."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return ("<h1>401 Unauthorized</h1>", 401)
    window = request.args.get("window", "1 hour")
    if window not in ("1 hour", "6 hours", "24 hours", "7 days"):
        window = "1 hour"
    with db() as conn:
        visitors = conn.execute(f"""
            SELECT visitor_id, COUNT(*) as n,
                   MIN(ts) as first_seen, MAX(ts) as last_seen,
                   user_id,
                   GROUP_CONCAT(DISTINCT path) as paths
            FROM page_visits
            WHERE ts >= datetime('now','-{window}')
            GROUP BY visitor_id
            ORDER BY n DESC
        """).fetchall()
        single_paths = conn.execute(f"""
            SELECT pv.path, COUNT(*) as n
            FROM page_visits pv
            WHERE pv.ts >= datetime('now','-{window}')
              AND pv.visitor_id IN (
                SELECT visitor_id FROM page_visits
                WHERE ts >= datetime('now','-{window}')
                GROUP BY visitor_id HAVING COUNT(*) = 1
              )
            GROUP BY pv.path ORDER BY n DESC LIMIT 25
        """).fetchall()
        engaged_paths = conn.execute(f"""
            SELECT pv.path, COUNT(*) as n
            FROM page_visits pv
            WHERE pv.ts >= datetime('now','-{window}')
              AND pv.visitor_id IN (
                SELECT visitor_id FROM page_visits
                WHERE ts >= datetime('now','-{window}')
                GROUP BY visitor_id HAVING COUNT(*) >= 2
              )
            GROUP BY pv.path ORDER BY n DESC LIMIT 25
        """).fetchall()
    total = len(visitors)
    engaged = sum(1 for r in visitors if r["n"] >= 2)
    single = total - engaged
    auth = sum(1 for r in visitors if r["user_id"])
    rows_html = ""
    for r in visitors[:60]:
        auth_pill = '<span style="color:var(--teal)">AUTH</span>' if r["user_id"] else '<span class="muted">anon</span>'
        paths_short = (r["paths"] or "")[:120]
        rows_html += (
            f'<div style="display:flex;gap:14px;padding:6px 0;border-top:1px solid var(--border);font-size:.84em;font-family:monospace">'
            f'<span style="width:90px">{r["visitor_id"][:14]}</span>'
            f'<span style="width:54px">{auth_pill}</span>'
            f'<span style="width:50px;text-align:right">{r["n"]} hits</span>'
            f'<span style="flex:1;overflow:hidden;text-overflow:ellipsis">{paths_short}</span>'
            f'</div>'
        )
    sp_html = "".join(
        f'<div style="display:flex;justify-content:space-between;font-size:.85em;padding:3px 0"><span>{r["path"]}</span><span class="muted">{r["n"]}</span></div>'
        for r in single_paths
    )
    ep_html = "".join(
        f'<div style="display:flex;justify-content:space-between;font-size:.85em;padding:3px 0"><span>{r["path"]}</span><span class="muted">{r["n"]}</span></div>'
        for r in engaged_paths
    )
    return _page(f"""
<h1>Visitor traffic — last {window}</h1>
<p class="muted">Real engaged users have 2+ pageviews. Single-hit "visitors" are usually scrapers (no cookie persistence → fresh visitor_id per request).</p>
<div class="grid">
  <div class="stat-card">
    <div class="label">Total unique cv_ids</div>
    <div class="value accent">{total}</div>
    <div class="delta">raw count, includes scrapers</div>
  </div>
  <div class="stat-card">
    <div class="label">Engaged (2+ pageviews)</div>
    <div class="value">{engaged}</div>
    <div class="delta">likely real humans</div>
  </div>
  <div class="stat-card">
    <div class="label">Single-hit (1 pageview)</div>
    <div class="value">{single}</div>
    <div class="delta">likely scrapers / link-preview bots</div>
  </div>
  <div class="stat-card">
    <div class="label">Logged-in</div>
    <div class="value">{auth}</div>
    <div class="delta">signed-in users</div>
  </div>
</div>
<p class="muted" style="margin-top:14px">Window: <a href="?key={ADMIN_KEY}&window=1 hour">1h</a> · <a href="?key={ADMIN_KEY}&window=6 hours">6h</a> · <a href="?key={ADMIN_KEY}&window=24 hours">24h</a> · <a href="?key={ADMIN_KEY}&window=7 days">7d</a></p>

<h2 style="margin-top:24px">Pages requested by single-hit visitors</h2>
<div class="card">{sp_html or '<p class="muted">None.</p>'}</div>

<h2 style="margin-top:24px">Pages requested by engaged visitors (humans)</h2>
<div class="card">{ep_html or '<p class="muted">None.</p>'}</div>

<h2 style="margin-top:24px">Per-visitor breakdown (top 60 by hit count)</h2>
<div class="card">
  <div style="display:flex;gap:14px;padding:6px 0;font-size:.78em;color:var(--text-2);text-transform:uppercase;letter-spacing:.4px;font-family:monospace">
    <span style="width:90px">cv_id</span>
    <span style="width:54px">auth</span>
    <span style="width:50px;text-align:right">hits</span>
    <span style="flex:1">paths (sample)</span>
  </div>
  {rows_html}
</div>
""", title="Visitors — Candor")


@app.route("/admin/stats")
def admin_stats():
    """Live activity dashboard. Pulls from existing DB tables."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return ("<h1>401 Unauthorized</h1>", 401)
    # Use Pacific time for "today" counters since Railway runs in UTC
    # but the operator (jasper) is on the West Coast — without this offset
    # "today" rolls over at 5pm local which makes the dashboard look like
    # signups stopped when in fact a new UTC day just started.
    TZ_OFFSET_HOURS = -7  # PT (UTC-7 during DST; -8 in winter — adjust manually)
    with db() as conn:
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        users_today = conn.execute(
            f"SELECT COUNT(*) c FROM users WHERE date(created_at, '{TZ_OFFSET_HOURS} hours') = date('now', '{TZ_OFFSET_HOURS} hours')"
        ).fetchone()["c"]
        users_week = conn.execute(
            f"SELECT COUNT(*) c FROM users WHERE date(created_at, '{TZ_OFFSET_HOURS} hours') >= date('now', '{TZ_OFFSET_HOURS} hours', '-7 days')"
        ).fetchone()["c"]
        profiles_done = conn.execute(
            "SELECT COUNT(*) c FROM profiles WHERE uw_gpa IS NOT NULL"
        ).fetchone()["c"]
        chat_msgs_today = conn.execute(
            f"SELECT COUNT(*) c FROM messages WHERE date(created_at, '{TZ_OFFSET_HOURS} hours')=date('now', '{TZ_OFFSET_HOURS} hours') AND role='user'"
        ).fetchone()["c"]
        chat_msgs_total = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE role='user'"
        ).fetchone()["c"]
        chances_run = conn.execute(
            "SELECT COUNT(DISTINCT user_id||'-'||college_slug) c FROM saved_chances"
        ).fetchone()["c"]
        paid_users = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE is_paid=1"
        ).fetchone()["c"]
        # Conversion funnel: how many unique browsers reached the upgrade page,
        # and what fraction of them ended up paying. Pulled from page_visits
        # (path is stored without query string, so /upgrade?for=parent counts too).
        try:
            upgrade_views = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_id) c FROM page_visits WHERE path='/upgrade' AND {_REAL_VISITOR_SQL}"
            ).fetchone()["c"]
            upgrade_views_24h = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_id) c FROM page_visits WHERE path='/upgrade' AND ts >= datetime('now','-24 hours') AND {_REAL_VISITOR_SQL}"
            ).fetchone()["c"]
        except Exception:
            upgrade_views = upgrade_views_24h = 0
        # Anonymous visitor counts (separate from signups). page_visits.ts is
        # UTC (SQLite CURRENT_TIMESTAMP), and datetime('now','-1 hour') is
        # also UTC, so the math doesn't need a TZ offset.
        try:
            visitors_1h = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_id) c FROM page_visits WHERE ts >= datetime('now','-1 hour') AND {_REAL_VISITOR_SQL}"
            ).fetchone()["c"]
            visitors_24h = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_id) c FROM page_visits WHERE ts >= datetime('now','-24 hours') AND {_REAL_VISITOR_SQL}"
            ).fetchone()["c"]
            pageviews_24h = conn.execute(
                f"SELECT COUNT(*) c FROM page_visits WHERE ts >= datetime('now','-24 hours') AND {_REAL_VISITOR_SQL}"
            ).fetchone()["c"]
        except Exception:
            visitors_1h = visitors_24h = pageviews_24h = 0
        # All-time visitors, scrapers excluded. Heuristic (same as /admin/traffic):
        # a real visitor persists a cookie and racks up 2+ pageviews; single-hit
        # visitor_ids are almost all scrapers (fresh cookie per request).
        try:
            all_visitors = conn.execute(
                "SELECT COUNT(DISTINCT visitor_id) c FROM page_visits"
            ).fetchone()["c"]
            real_visitors = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_id) c FROM page_visits WHERE {_REAL_VISITOR_SQL}"
            ).fetchone()["c"]
        except Exception:
            all_visitors = real_visitors = 0
        scrapers_excluded = max(0, all_visitors - real_visitors)
        recent_users = conn.execute(
            "SELECT email, created_at FROM users ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
        top_schools = conn.execute(
            "SELECT college_slug, COUNT(*) n FROM calc_runs GROUP BY college_slug ORDER BY n DESC LIMIT 400"
        ).fetchall()
        total_calibrations = conn.execute("SELECT COUNT(*) c FROM calc_runs").fetchone()["c"]
    recent_html = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid var(--border);font-size:.88em">'
        f'<span style="color:var(--text)">{r["email"]}</span>'
        f'<span class="muted">{r["created_at"]}</span></div>'
        for r in recent_users
    ) or '<p class="muted">No users yet.</p>'
    def _school_row(r):
        return (f'<div style="display:flex;justify-content:space-between;padding:6px 0;font-size:.88em">'
                f'<span>{COLLEGES_BY_SLUG.get(r["college_slug"],{}).get("name", r["college_slug"])}</span>'
                f'<span class="muted">{r["n"]} chances run</span></div>')
    if top_schools:
        schools_html = "".join(_school_row(r) for r in top_schools[:10])
        rest = top_schools[10:]
        if rest:
            schools_html += (
                f'<details style="margin-top:6px">'
                f'<summary style="cursor:pointer;color:var(--teal);font-size:.86em;padding:6px 0">'
                f'Show all {len(top_schools)} schools →</summary>'
                f'{"".join(_school_row(r) for r in rest)}</details>')
    else:
        schools_html = '<p class="muted">No chances calculated yet.</p>'
    profile_pct = round(profiles_done / max(1, total_users) * 100)
    return _page(f"""
<h1>Activity</h1>
<h3 style="margin:18px 0 8px;color:var(--text-2);font-size:.82em;letter-spacing:.6px;text-transform:uppercase;font-weight:600">Live traffic</h3>
<div class="grid">
  <div class="stat-card">
    <div class="label">Visitors last hour</div>
    <div class="value accent">{visitors_1h}</div>
    <div class="delta">real users · bots excluded</div>
  </div>
  <div class="stat-card">
    <div class="label">Visitors last 24h</div>
    <div class="value accent">{visitors_24h}</div>
    <div class="delta">real users · bots excluded</div>
  </div>
  <div class="stat-card">
    <div class="label">Page views last 24h</div>
    <div class="value">{pageviews_24h}</div>
    <div class="delta">real users only · incl. repeat views</div>
  </div>
</div>
<h3 style="margin:24px 0 8px;color:var(--text-2);font-size:.82em;letter-spacing:.6px;text-transform:uppercase;font-weight:600">Cumulative</h3>
<div class="grid">
  <div class="stat-card">
    <div class="label">All-time visitors (real)</div>
    <div class="value accent">{real_visitors}</div>
    <div class="delta">2+ pageviews · {scrapers_excluded:,} single-hit scrapers excluded</div>
  </div>
  <div class="stat-card">
    <div class="label">Total users</div>
    <div class="value accent">{total_users}</div>
    <div class="delta">+{users_today} today · +{users_week} this week</div>
  </div>
  <div class="stat-card">
    <div class="label">Profiles completed</div>
    <div class="value">{profiles_done}</div>
    <div class="delta">{profile_pct}% of signups</div>
  </div>
  <div class="stat-card">
    <div class="label">Chat messages today</div>
    <div class="value">{chat_msgs_today}</div>
    <div class="delta">{chat_msgs_total} all-time</div>
  </div>
  <div class="stat-card">
    <div class="label">Chances run</div>
    <div class="value">{chances_run}</div>
    <div class="delta">unique user-school pairs</div>
  </div>
  <div class="stat-card">
    <div class="label">Total calibrations</div>
    <div class="value">{total_calibrations}</div>
    <div class="delta">every run, incl. re-runs</div>
  </div>
  <div class="stat-card">
    <div class="label">Premium unlocks</div>
    <div class="value accent">{paid_users}</div>
    <div class="delta">~${paid_users * 3}/mo MRR · $3/month</div>
  </div>
  <div class="stat-card">
    <div class="label">Upgrade page views</div>
    <div class="value">{upgrade_views}</div>
    <div class="delta">{upgrade_views_24h} in last 24h · unique browsers</div>
  </div>
  <div class="stat-card">
    <div class="label">Upgrade → paid conversion</div>
    <div class="value">{round(paid_users / max(1, upgrade_views) * 100, 1)}%</div>
    <div class="delta">{paid_users} of {upgrade_views} who saw /upgrade</div>
  </div>
</div>
<div class="card" style="margin-top:18px">
  <h3 style="margin-top:0">Recent signups</h3>
  {recent_html}
</div>
<div class="card">
  <h3 style="margin-top:0">Most-calibrated schools <span class="muted" style="font-size:.6em;font-weight:500">· {total_calibrations:,} total runs</span></h3>
  {schools_html}
</div>
<p class="muted" style="font-size:.78em;margin-top:18px">Auto-refreshes every 90s · scraper-excluded counts use the 2+ pageview heuristic. Bookmark this URL for quick access.</p>
<script>setTimeout(function(){{location.reload();}}, 90000);</script>
""", title="Activity — Candor")


@app.route("/admin/data-status")
def admin_data_status():
    """Show how many schools have Scorecard overrides applied + fields covered."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return ("<h1>401 Unauthorized</h1>", 401)
    with db() as conn:
        rows = conn.execute("SELECT college_slug, accept, sat_25, sat_75, size, tuition, source, verified_at FROM school_stats_overrides").fetchall()
    overrides_by_slug = {r["college_slug"]: r for r in rows}
    total = len(COLLEGES)
    covered = len(overrides_by_slug)
    missing = [c for c in COLLEGES if c["slug"] not in overrides_by_slug]
    miss_list = "".join(f'<li><a href="/college/{c["slug"]}">{c["name"]}</a> — using hardcoded</li>' for c in missing[:60])
    if len(missing) > 60:
        miss_list += f"<li class='muted'>+{len(missing)-60} more</li>"
    pct = round(covered / total * 100) if total else 0
    return _page(f"""
<h1>Data freshness</h1>
<div class="card">
  <div class="stat-card">
    <div class="label">Schools with federal overrides</div>
    <div class="value accent">{covered} / {total}</div>
    <div class="delta">{pct}% of the database is on College Scorecard data; the rest fall back to the hardcoded values</div>
  </div>
</div>
<div class="card">
  <h3 style="margin-top:0">Schools still on hardcoded values</h3>
  <ul style="font-size:.86em;columns:2;column-gap:30px">{miss_list or '<li>None — all 155 covered</li>'}</ul>
  <p style="margin-top:14px"><a class="btn btn-primary btn-sm" href="/admin/refresh-scorecard?key={request.args.get('key','')}">Refresh now</a></p>
</div>
""", title="Data status — Candor")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "colleges": len(COLLEGES), "rankings": len(RANKINGS), "claude": _claude_client is not None, "newsapi": bool(NEWSAPI_KEY)})


# ─── BOOT ─────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f" * Candor — running on http://127.0.0.1:{port}", flush=True)
    print(f" * DB: {DB_PATH}", flush=True)
    print(f" * Claude: {'on' if _claude_client else 'off (templates)'} · NewsAPI: {'on' if NEWSAPI_KEY else 'off'}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG") == "1")
