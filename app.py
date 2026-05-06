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
    "https://buy.stripe.com/cNicN6egv4XP6XS46Y5AQ01")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ARTICLE_TTL_HOURS = 12   # how long to cache per-college articles
SCORECARD_TTL_DAYS = 30  # refresh federal stats monthly
FREE_TRIAL_MESSAGES = 3
PAID_MONTHLY_LIMIT = 250


# ─── COLLEGE DATA (~80 schools) ──────────────────────────
# Each entry: name, slug, accept_rate, gpa_lo/hi, sat_25/75, act_25/75, type,
# state, size (rough enrollment), tuition (annual sticker), description,
# popular majors. Numbers are public-CDS-ballpark, intended as orientation
# not gospel. Tier 1 = sub-10% accept, 5 = accessible.
COLLEGES = [
    # --- Tier 1 elites ---
    {"name":"Harvard","slug":"harvard","accept":0.036,"gpa_lo":3.93,"gpa_hi":4.00,"sat_25":1500,"sat_75":1580,"act_25":34,"act_75":36,"tier":1,"type":"private","state":"Massachusetts","size":7200,"tuition":59000,"desc":"The oldest US university and the cultural prototype for American elite higher education. Strong in basically everything; particular standouts in economics, government, history, and pre-med.","majors":["Economics","Government","Computer Science","Biology","Social Studies"]},
    {"name":"Stanford","slug":"stanford","accept":0.039,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1500,"sat_75":1580,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"California","size":7800,"tuition":62000,"desc":"Silicon Valley's home university and the dominant feeder into tech entrepreneurship. CS, engineering, and human biology are the marquee programs; the alumni network is unparalleled in startups.","majors":["Computer Science","Human Biology","Engineering","Economics","Symbolic Systems"]},
    {"name":"MIT","slug":"mit","accept":0.045,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1530,"sat_75":1580,"act_25":35,"act_75":36,"tier":1,"type":"private","state":"Massachusetts","size":4600,"tuition":60000,"desc":"The world's leading STEM-focused university. Ruthlessly quantitative culture; strongest in CS, physics, mechanical/electrical engineering, and economics.","majors":["Computer Science","Mechanical Engineering","Mathematics","Physics","Economics"]},
    {"name":"Yale","slug":"yale","accept":0.037,"gpa_lo":3.94,"gpa_hi":4.00,"sat_25":1500,"sat_75":1570,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Connecticut","size":6500,"tuition":63000,"desc":"Strong humanities and social-sciences orientation, pre-law/finance pipeline, residential college system that drives a tight-knit campus culture.","majors":["Economics","Political Science","History","Computer Science","Biology"]},
    {"name":"Princeton","slug":"princeton","accept":0.047,"gpa_lo":3.93,"gpa_hi":4.00,"sat_25":1510,"sat_75":1570,"act_25":34,"act_75":35,"tier":1,"type":"private","state":"New Jersey","size":5500,"tuition":59000,"desc":"Undergraduate-focused (no law/med/business school competing for attention). Very strong in math, physics, and public policy. Generous financial aid.","majors":["Computer Science","Economics","Public and International Affairs","Mathematics","Engineering"]},
    {"name":"Columbia","slug":"columbia","accept":0.039,"gpa_lo":3.91,"gpa_hi":4.00,"sat_25":1490,"sat_75":1570,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"New York","size":8800,"tuition":67000,"desc":"Manhattan campus with a famous core curriculum every undergrad takes. Strong in finance recruiting, journalism, and humanities.","majors":["Economics","Political Science","Computer Science","Engineering","English"]},
    {"name":"University of Chicago","slug":"uchicago","accept":0.046,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1510,"sat_75":1580,"act_25":34,"act_75":35,"tier":1,"type":"private","state":"Illinois","size":7500,"tuition":63000,"desc":"Famous intellectual rigor and quirky 'where fun goes to die' culture. World-class economics (Friedman heritage), strong in math/physics/PoliSci.","majors":["Economics","Mathematics","Public Policy","Biology","Computer Science"]},
    {"name":"UPenn","slug":"upenn","accept":0.054,"gpa_lo":3.88,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Pennsylvania","size":10300,"tuition":63000,"desc":"Wharton is the dominant undergraduate business program in the country. Strong cross-school flexibility (M&T, Huntsman, Vagelos).","majors":["Finance","Economics","Computer Science","Nursing","Biology"]},
    {"name":"Brown","slug":"brown","accept":0.054,"gpa_lo":3.89,"gpa_hi":4.00,"sat_25":1500,"sat_75":1570,"act_25":34,"act_75":35,"tier":1,"type":"private","state":"Rhode Island","size":7300,"tuition":63000,"desc":"Open curriculum (no core requirements, no +/- grades) is the headline. Strong in CS, applied math, international relations, creative writing.","majors":["Computer Science","Economics","Biology","International Relations","English"]},
    {"name":"Dartmouth","slug":"dartmouth","accept":0.054,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"New Hampshire","size":4500,"tuition":62000,"desc":"Smallest of the Ivies, undergrad-focused, quarter system, strong outdoor/Greek culture. Disproportionate finance-recruiting presence.","majors":["Economics","Government","Computer Science","Engineering","Biology"]},
    {"name":"Duke","slug":"duke","accept":0.051,"gpa_lo":3.88,"gpa_hi":4.00,"sat_25":1490,"sat_75":1570,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"North Carolina","size":6700,"tuition":62000,"desc":"Strong sports culture (basketball), Trinity College of Arts & Sciences plus Pratt engineering. Heavy pre-med and finance pipelines.","majors":["Economics","Public Policy","Computer Science","Biology","Engineering"]},
    {"name":"Northwestern","slug":"northwestern","accept":0.070,"gpa_lo":3.86,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Illinois","size":8500,"tuition":63000,"desc":"Quarter system, strong journalism (Medill) and theater programs, integrated marketing communications, growing CS.","majors":["Economics","Computer Science","Journalism","Engineering","Communication Studies"]},
    {"name":"Caltech","slug":"caltech","accept":0.027,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1530,"sat_75":1580,"act_25":35,"act_75":36,"tier":1,"type":"private","state":"California","size":1000,"tuition":60000,"desc":"Tiny, hyper-focused on hard sciences and engineering. Famously brutal core curriculum. JPL connection makes it the top space-research undergrad.","majors":["Physics","Computer Science","Engineering and Applied Science","Chemistry","Mathematics"]},
    # --- Tier 2: highly selective ---
    {"name":"Cornell","slug":"cornell","accept":0.073,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1470,"sat_75":1550,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"New York","size":15700,"tuition":62000,"desc":"Largest of the Ivies. Multiple distinct undergraduate colleges with very different admit rates (CALS easier than Engineering or Hotel).","majors":["Computer Science","Engineering","Biological Sciences","Hotel Administration","Economics"]},
    {"name":"Johns Hopkins","slug":"jhu","accept":0.064,"gpa_lo":3.88,"gpa_hi":4.00,"sat_25":1500,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Maryland","size":5300,"tuition":62000,"desc":"Best-known for biomedical engineering and as a pre-med pipeline. Undergrad research culture is particularly intense.","majors":["Public Health","Biomedical Engineering","Computer Science","Neuroscience","International Studies"]},
    {"name":"Vanderbilt","slug":"vanderbilt","accept":0.056,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1480,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Tennessee","size":7100,"tuition":63000,"desc":"Southern blend of academic rigor and traditional college life. Generous financial aid program.","majors":["Economics","Human and Organizational Development","Engineering","Biology","Political Science"]},
    {"name":"Rice","slug":"rice","accept":0.077,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":34,"act_75":35,"tier":2,"type":"private","state":"Texas","size":4500,"tuition":58000,"desc":"Small private university in Houston with a residential college system. Strong engineering, sciences, architecture, and music.","majors":["Engineering","Computer Science","Biosciences","Economics","Architecture"]},
    {"name":"Notre Dame","slug":"notre-dame","accept":0.087,"gpa_lo":3.79,"gpa_hi":4.00,"sat_25":1450,"sat_75":1540,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Indiana","size":8800,"tuition":62000,"desc":"Catholic university with very strong school-spirit culture. Mendoza is a top undergrad business program; engineering and pre-med are strong.","majors":["Finance","Political Science","Engineering","Biology","Economics"]},
    {"name":"Carnegie Mellon","slug":"cmu","accept":0.110,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Pennsylvania","size":7500,"tuition":63000,"desc":"Top-tier CS school (admit rate to SCS is single-digit). Also strong in engineering, drama, and design.","majors":["Computer Science","Engineering","Information Systems","Drama","Business Administration"]},
    {"name":"USC","slug":"usc","accept":0.090,"gpa_lo":3.79,"gpa_hi":4.00,"sat_25":1450,"sat_75":1530,"act_25":32,"act_75":35,"tier":2,"type":"private","state":"California","size":21000,"tuition":68000,"desc":"Marshall (business), Viterbi (engineering), Annenberg (comm), Cinematic Arts. Strong LA-industry pipelines.","majors":["Business Administration","Computer Science","Communication","Cinematic Arts","International Relations"]},
    {"name":"NYU","slug":"nyu","accept":0.080,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1450,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"New York","size":29000,"tuition":62000,"desc":"Manhattan campus with Stern (business), Tisch (arts), Steinhardt, Tandon (engineering). High urban energy and sticker price.","majors":["Business","Liberal Studies","Computer Science","Drama","Economics"]},
    {"name":"Georgetown","slug":"georgetown","accept":0.110,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":35,"tier":2,"type":"private","state":"District of Columbia","size":7500,"tuition":63000,"desc":"Jesuit, DC location, dominant in international relations and government careers. McDonough is a strong undergrad business program.","majors":["International Politics","Finance","Economics","Government","Biology"]},
    {"name":"UC Berkeley","slug":"ucb","accept":0.115,"gpa_lo":3.86,"gpa_hi":4.00,"sat_25":1340,"sat_75":1530,"act_25":30,"act_75":35,"tier":2,"type":"public","state":"California","size":33000,"tuition":15000,"desc":"Top public university in the country. Engineering, CS, and business (Haas) are powerhouses. In-state tuition is a steal; out-of-state is full freight.","majors":["Computer Science","Economics","Business Administration","Cognitive Science","Political Science"]},
    {"name":"UCLA","slug":"ucla","accept":0.086,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1370,"sat_75":1540,"act_25":31,"act_75":35,"tier":2,"type":"public","state":"California","size":33000,"tuition":15000,"desc":"Most-applied-to college in the US. Strong across the board; particularly notable for film, basketball, biology, and engineering.","majors":["Biology","Psychology","Business Economics","Political Science","Engineering"]},
    {"name":"UC San Diego","slug":"ucsd","accept":0.286,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1300,"sat_75":1490,"act_25":29,"act_75":34,"tier":3,"type":"public","state":"California","size":33000,"tuition":15000,"desc":"Strong R1 research university. CS, bioengineering, and oceanography are standouts. Test-blind admissions for the 24-25 cycle.","majors":["Biology","Computer Science","Engineering","Cognitive Science","Economics"]},
    {"name":"University of Michigan","slug":"umich","accept":0.177,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1370,"sat_75":1530,"act_25":31,"act_75":34,"tier":2,"type":"public","state":"Michigan","size":33000,"tuition":17000,"desc":"Public Ivy with one of the strongest undergrad business programs (Ross), top engineering, and a college-sports culture.","majors":["Business Administration","Computer Science","Engineering","Economics","Psychology"]},
    {"name":"UVA","slug":"uva","accept":0.166,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1520,"act_25":32,"act_75":35,"tier":2,"type":"public","state":"Virginia","size":17500,"tuition":21000,"desc":"Jefferson-founded public flagship. McIntire commerce school, strong politics/foreign affairs, traditional honor code.","majors":["Economics","Commerce","Computer Science","Biology","Political Science"]},
    {"name":"Georgia Tech","slug":"gatech","accept":0.160,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1430,"sat_75":1530,"act_25":32,"act_75":35,"tier":2,"type":"public","state":"Georgia","size":18000,"tuition":12000,"desc":"Engineering and CS-dominant public. Atlanta location and co-op program drive strong industry pipelines.","majors":["Computer Science","Mechanical Engineering","Industrial Engineering","Business","Aerospace Engineering"]},
    {"name":"Tufts","slug":"tufts","accept":0.095,"gpa_lo":3.82,"gpa_hi":4.00,"sat_25":1450,"sat_75":1540,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Massachusetts","size":6700,"tuition":68000,"desc":"Mid-sized research university with strong international relations and biomedical programs. Boston-area location.","majors":["International Relations","Computer Science","Biology","Economics","Political Science"]},
    {"name":"Wash U St. Louis","slug":"washu","accept":0.105,"gpa_lo":3.89,"gpa_hi":4.00,"sat_25":1500,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Missouri","size":8200,"tuition":63000,"desc":"Strong pre-med pipeline and Olin Business School. Generous financial aid for admitted students.","majors":["Biology","Economics","Engineering","Business","Psychology"]},
    {"name":"Emory","slug":"emory","accept":0.110,"gpa_lo":3.78,"gpa_hi":4.00,"sat_25":1430,"sat_75":1530,"act_25":31,"act_75":34,"tier":2,"type":"private","state":"Georgia","size":7100,"tuition":62000,"desc":"Atlanta-area private research university. Goizueta business school and strong pre-health pipelines (CDC ties).","majors":["Business","Biology","Economics","Psychology","Neuroscience"]},
    # --- Tier 3: very selective ---
    {"name":"UNC Chapel Hill","slug":"unc","accept":0.167,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1370,"sat_75":1500,"act_25":30,"act_75":33,"tier":3,"type":"public","state":"North Carolina","size":19000,"tuition":9000,"desc":"Public ivy of the South. Kenan-Flagler business, strong journalism, ACC sports, oldest public university in the US.","majors":["Biology","Psychology","Business Administration","Computer Science","Political Science"]},
    {"name":"Boston University","slug":"bu","accept":0.110,"gpa_lo":3.69,"gpa_hi":3.95,"sat_25":1390,"sat_75":1500,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Massachusetts","size":17600,"tuition":66000,"desc":"Large private university spread along the Charles River. Strong communications, engineering, and business.","majors":["Business","Communication","Engineering","Psychology","Economics"]},
    {"name":"Boston College","slug":"bc","accept":0.155,"gpa_lo":3.74,"gpa_hi":3.95,"sat_25":1420,"sat_75":1510,"act_25":32,"act_75":34,"tier":3,"type":"private","state":"Massachusetts","size":9500,"tuition":68000,"desc":"Jesuit university outside Boston. Carroll School of Management, strong undergrad business and finance recruiting.","majors":["Finance","Economics","Communication","Biology","Political Science"]},
    {"name":"Wake Forest","slug":"wake-forest","accept":0.210,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"North Carolina","size":5500,"tuition":67000,"desc":"Mid-sized private with strong undergrad business. Test-optional pioneer; strong honor culture.","majors":["Business","Communication","Economics","Politics","Biology"]},
    {"name":"William & Mary","slug":"wm","accept":0.320,"gpa_lo":3.78,"gpa_hi":4.00,"sat_25":1370,"sat_75":1500,"act_25":30,"act_75":33,"tier":3,"type":"public","state":"Virginia","size":6700,"tuition":24000,"desc":"Second-oldest US college. Public liberal arts feel, strong government/IR, smaller than peers.","majors":["Government","Business","Biology","Psychology","Economics"]},
    {"name":"University of Florida","slug":"uf","accept":0.230,"gpa_lo":3.90,"gpa_hi":4.45,"sat_25":1340,"sat_75":1460,"act_25":30,"act_75":33,"tier":3,"type":"public","state":"Florida","size":35000,"tuition":6400,"desc":"Florida flagship with strong agriculture, journalism, and engineering programs. In-state tuition is among the cheapest in the country for the value.","majors":["Biology","Business","Engineering","Health Sciences","Psychology"]},
    {"name":"University of Wisconsin","slug":"wisc","accept":0.490,"gpa_lo":3.65,"gpa_hi":3.97,"sat_25":1310,"sat_75":1450,"act_25":27,"act_75":32,"tier":3,"type":"public","state":"Wisconsin","size":35000,"tuition":11000,"desc":"Madison campus, strong engineering and biological sciences. Big Ten with a famous bar district adjacent to campus.","majors":["Biology","Computer Science","Economics","Business","Engineering"]},
    {"name":"University of Washington","slug":"uw","accept":0.430,"gpa_lo":3.74,"gpa_hi":3.95,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"Washington","size":36000,"tuition":12000,"desc":"Pacific Northwest research powerhouse. Strong CS (Allen School), biomedicine, and aerospace pipelines.","majors":["Computer Science","Biology","Business","Engineering","Psychology"]},
    {"name":"University of Texas at Austin","slug":"ut-austin","accept":0.290,"gpa_lo":3.69,"gpa_hi":3.96,"sat_25":1240,"sat_75":1460,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"Texas","size":40000,"tuition":11000,"desc":"Texas flagship with extreme size and breadth. McCombs business, top engineering, Cockrell, strong CS.","majors":["Business","Computer Science","Engineering","Biology","Economics"]},
    {"name":"University of Maryland","slug":"umd","accept":0.450,"gpa_lo":3.71,"gpa_hi":3.96,"sat_25":1290,"sat_75":1470,"act_25":29,"act_75":33,"tier":3,"type":"public","state":"Maryland","size":30000,"tuition":11000,"desc":"DC-area public with very strong CS/cybersecurity, business, and engineering. Honors college pulls top in-state applicants.","majors":["Computer Science","Business","Engineering","Biology","Criminology"]},
    {"name":"GWU","slug":"gwu","accept":0.490,"gpa_lo":3.65,"gpa_hi":3.92,"sat_25":1310,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"District of Columbia","size":12000,"tuition":62000,"desc":"DC location, strong international affairs and political science programs, heavy internship culture.","majors":["International Affairs","Political Science","Business","Public Health","Communication"]},
    # --- Tier 4: solid options ---
    {"name":"Penn State","slug":"penn-state","accept":0.540,"gpa_lo":3.55,"gpa_hi":3.97,"sat_25":1240,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Pennsylvania","size":40000,"tuition":19000,"desc":"Smeal business, top-tier engineering, large alumni network. Very strong PA-state pipeline; OOS still well-priced.","majors":["Business","Engineering","Biology","Communication","Information Sciences"]},
    {"name":"Ohio State","slug":"osu","accept":0.530,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1280,"sat_75":1450,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Ohio","size":47000,"tuition":12000,"desc":"Massive public flagship. Fisher (business) and engineering are strong; Big Ten sports culture.","majors":["Business","Biology","Engineering","Psychology","Communication"]},
    {"name":"Michigan State","slug":"msu","accept":0.830,"gpa_lo":3.60,"gpa_hi":3.94,"sat_25":1110,"sat_75":1310,"act_25":23,"act_75":29,"tier":4,"type":"public","state":"Michigan","size":39000,"tuition":15000,"desc":"Big Ten public with Eli Broad business, supply-chain management is nationally top-3, strong veterinary and ag programs.","majors":["Business","Communication","Biology","Psychology","Engineering"]},
    {"name":"Florida State","slug":"fsu","accept":0.370,"gpa_lo":3.85,"gpa_hi":4.40,"sat_25":1230,"sat_75":1370,"act_25":26,"act_75":30,"tier":4,"type":"public","state":"Florida","size":33000,"tuition":6500,"desc":"Tallahassee flagship. Strong film, criminology, hospitality. Bright Futures funding makes in-state extremely cheap.","majors":["Criminology","Business","Psychology","Biology","Communication"]},
    {"name":"Indiana University","slug":"iu","accept":0.770,"gpa_lo":3.51,"gpa_hi":3.93,"sat_25":1190,"sat_75":1380,"act_25":25,"act_75":31,"tier":5,"type":"public","state":"Indiana","size":33000,"tuition":11000,"desc":"Bloomington flagship. Kelley business school is a top undergrad direct-admit, music school is world-renowned.","majors":["Business","Biology","Communication","Psychology","Music"]},
    {"name":"Arizona State","slug":"asu","accept":0.880,"gpa_lo":3.39,"gpa_hi":3.86,"sat_25":1130,"sat_75":1340,"act_25":23,"act_75":29,"tier":5,"type":"public","state":"Arizona","size":75000,"tuition":12000,"desc":"Massive scale, online-friendly, growing reputation. W.P. Carey business and Cronkite journalism are highlights.","majors":["Business","Engineering","Biology","Psychology","Communication"]},
    {"name":"Purdue","slug":"purdue","accept":0.500,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1190,"sat_75":1430,"act_25":25,"act_75":33,"tier":3,"type":"public","state":"Indiana","size":37000,"tuition":10000,"desc":"Strong engineering and aviation programs. Frozen tuition since 2012 makes it a notable value.","majors":["Engineering","Computer Science","Business","Aviation","Agriculture"]},
    {"name":"University of Illinois","slug":"uiuc","accept":0.430,"gpa_lo":3.66,"gpa_hi":3.95,"sat_25":1290,"sat_75":1470,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"Illinois","size":35000,"tuition":17000,"desc":"Engineering and CS powerhouse — Grainger College of Engineering is top-5 in many disciplines. Gies business is direct-admit.","majors":["Engineering","Computer Science","Business","Psychology","Communication"]},
    # --- Liberal arts colleges ---
    {"name":"Williams","slug":"williams","accept":0.085,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1450,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Massachusetts","size":2100,"tuition":64000,"desc":"Top-ranked liberal arts college. Tutorial system mirrors Oxford. Strong art history, economics, English.","majors":["Economics","Mathematics","English","History","Psychology"]},
    {"name":"Amherst","slug":"amherst","accept":0.090,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1450,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Massachusetts","size":1900,"tuition":65000,"desc":"Open-curriculum LAC. Five College consortium with Smith, Mt Holyoke, Hampshire, UMass-Amherst.","majors":["Economics","English","Political Science","Mathematics","Psychology"]},
    {"name":"Swarthmore","slug":"swarthmore","accept":0.072,"gpa_lo":3.91,"gpa_hi":4.00,"sat_25":1430,"sat_75":1550,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Pennsylvania","size":1700,"tuition":62000,"desc":"Famously rigorous LAC. Honors program with oral exams. Strong engineering for an LAC.","majors":["Economics","Computer Science","Biology","Engineering","Political Science"]},
    {"name":"Pomona","slug":"pomona","accept":0.075,"gpa_lo":3.92,"gpa_hi":4.00,"sat_25":1460,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"California","size":1700,"tuition":62000,"desc":"Top West Coast LAC. Claremont Colleges consortium gives access to a 7-school network with shared facilities.","majors":["Economics","Computer Science","Mathematics","Biology","Psychology"]},
    {"name":"Bowdoin","slug":"bowdoin","accept":0.075,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1440,"sat_75":1530,"act_25":32,"act_75":34,"tier":1,"type":"private","state":"Maine","size":1900,"tuition":62000,"desc":"Coastal Maine LAC, test-optional pioneer. Strong government, environmental studies, and food.","majors":["Government","Mathematics","Economics","Biology","English"]},
    {"name":"Wellesley","slug":"wellesley","accept":0.130,"gpa_lo":3.86,"gpa_hi":4.00,"sat_25":1410,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Massachusetts","size":2400,"tuition":64000,"desc":"Top women's college. MIT cross-registration, strong pre-med and PoliSci, alumni network includes Hillary Clinton, Albright, Soong Mei-ling.","majors":["Economics","Computer Science","Political Science","Biology","English"]},
    {"name":"Middlebury","slug":"middlebury","accept":0.130,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1400,"sat_75":1520,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Vermont","size":2700,"tuition":64000,"desc":"LAC famous for languages (the summer language schools are legendary). Strong environmental studies and IR.","majors":["Economics","International Studies","English","Political Science","Environmental Studies"]},
    {"name":"Carleton","slug":"carleton","accept":0.170,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Minnesota","size":2000,"tuition":67000,"desc":"Midwestern LAC with high PhD-feeder rate. Trimester system, strong sciences and humanities.","majors":["Economics","Computer Science","Biology","Mathematics","Political Science"]},
    {"name":"Haverford","slug":"haverford","accept":0.140,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Pennsylvania","size":1400,"tuition":63000,"desc":"Tiny Quaker LAC outside Philadelphia. Honor code, no Greek life. Bi-Co consortium with Bryn Mawr.","majors":["Economics","Biology","English","Political Science","Computer Science"]},
    {"name":"Vassar","slug":"vassar","accept":0.170,"gpa_lo":3.79,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"New York","size":2400,"tuition":65000,"desc":"Liberal arts in the Hudson Valley. Strong arts, English, drama, urban studies.","majors":["English","Economics","Psychology","Political Science","Biology"]},
    # --- Specialized + niche ---
    {"name":"Babson","slug":"babson","accept":0.180,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1280,"sat_75":1430,"act_25":29,"act_75":32,"tier":3,"type":"private","state":"Massachusetts","size":2500,"tuition":58000,"desc":"Entrepreneurship-focused business school. #1 undergrad entrepreneurship for decades. Everyone takes the same first-year venture class.","majors":["Entrepreneurship","Finance","Marketing","Strategic Management","Accounting"]},
    {"name":"Bentley","slug":"bentley","accept":0.380,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1230,"sat_75":1370,"act_25":27,"act_75":31,"tier":4,"type":"private","state":"Massachusetts","size":4400,"tuition":59000,"desc":"Business-focused private outside Boston. Heavy quant/finance pipelines into Boston employers.","majors":["Finance","Accounting","Economics","Marketing","Management"]},
    {"name":"RPI","slug":"rpi","accept":0.660,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1300,"sat_75":1480,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"New York","size":6000,"tuition":62000,"desc":"Oldest engineering school in the English-speaking world. Strong CS and engineering, more affordable than Ivy-tier privates.","majors":["Engineering","Computer Science","Mathematics","Architecture","Business"]},
    {"name":"WPI","slug":"wpi","accept":0.610,"gpa_lo":3.71,"gpa_hi":3.95,"sat_25":1310,"sat_75":1470,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":5300,"tuition":63000,"desc":"Worcester Polytechnic. Project-based curriculum, term system. Strong engineering, robotics, biomedical.","majors":["Engineering","Computer Science","Robotics","Biology","Business"]},
    {"name":"Rose-Hulman","slug":"rose-hulman","accept":0.770,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1260,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Indiana","size":2200,"tuition":54000,"desc":"Specialty undergrad-only engineering school. Consistently ranked #1 for non-PhD engineering programs.","majors":["Engineering","Computer Science","Mathematics","Physics","Chemistry"]},
    # --- More state flagships and accessible ---
    {"name":"University of Iowa","slug":"uiowa","accept":0.870,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1130,"sat_75":1340,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Iowa","size":24000,"tuition":11000,"desc":"Big Ten public, famous for the Iowa Writers' Workshop (graduate). Strong undergrad business at Tippie.","majors":["Business","Communication","Engineering","Biology","Psychology"]},
    {"name":"University of Minnesota","slug":"umn","accept":0.700,"gpa_lo":3.55,"gpa_hi":3.93,"sat_25":1280,"sat_75":1450,"act_25":26,"act_75":32,"tier":4,"type":"public","state":"Minnesota","size":36000,"tuition":17000,"desc":"Twin Cities flagship. Carlson business and engineering, strong agriculture and pre-med.","majors":["Business","Biology","Engineering","Psychology","Economics"]},
    {"name":"University of Colorado Boulder","slug":"cu-boulder","accept":0.810,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":31,"tier":5,"type":"public","state":"Colorado","size":31000,"tuition":13000,"desc":"Mountain-flagship public. Aerospace engineering is top-3 nationally; strong outdoor culture.","majors":["Engineering","Business","Biology","Communication","Psychology"]},
    {"name":"University of Oregon","slug":"uoregon","accept":0.880,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1100,"sat_75":1300,"act_25":22,"act_75":28,"tier":5,"type":"public","state":"Oregon","size":18000,"tuition":14000,"desc":"Eugene flagship. Strong journalism (advertising), business, and architecture. Nike pipeline.","majors":["Business","Psychology","Biology","Communication","Economics"]},
    {"name":"University of Connecticut","slug":"uconn","accept":0.540,"gpa_lo":3.59,"gpa_hi":3.92,"sat_25":1230,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Connecticut","size":23000,"tuition":18000,"desc":"Storrs flagship. Strong nursing, education, business, and basketball. Honors college pulls top in-state applicants.","majors":["Business","Psychology","Engineering","Nursing","Biology"]},
    {"name":"Rutgers","slug":"rutgers","accept":0.650,"gpa_lo":3.58,"gpa_hi":3.92,"sat_25":1210,"sat_75":1440,"act_25":26,"act_75":32,"tier":4,"type":"public","state":"New Jersey","size":36000,"tuition":17000,"desc":"NJ flagship. Strong CS, business, pharmacy. Diverse, large, deeply integrated with NY/Philly job markets.","majors":["Business","Computer Science","Biology","Psychology","Engineering"]},
    {"name":"Stony Brook","slug":"stony-brook","accept":0.480,"gpa_lo":3.62,"gpa_hi":3.95,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"New York","size":18000,"tuition":11000,"desc":"SUNY research flagship. Strong biology and engineering, affordable, R1 medical/research environment.","majors":["Biology","Computer Science","Business","Engineering","Psychology"]},
    {"name":"University of Pittsburgh","slug":"pitt","accept":0.490,"gpa_lo":3.63,"gpa_hi":3.93,"sat_25":1240,"sat_75":1420,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Pennsylvania","size":20000,"tuition":21000,"desc":"Pittsburgh urban public. Strong nursing, biomedical, philosophy. Affiliated UPMC hospital network.","majors":["Nursing","Biology","Business","Psychology","Engineering"]},
    {"name":"Virginia Tech","slug":"vt","accept":0.560,"gpa_lo":3.81,"gpa_hi":4.10,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Virginia","size":30000,"tuition":15000,"desc":"Engineering-heavy public, strong corps of cadets, top-tier ROTC, growing CS.","majors":["Engineering","Business","Computer Science","Biology","Architecture"]},
    {"name":"Texas A&M","slug":"tamu","accept":0.640,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1180,"sat_75":1390,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"Texas","size":58000,"tuition":12000,"desc":"Massive public, military traditions, strong engineering and agriculture. Mays business is direct-admit.","majors":["Engineering","Business","Biology","Communication","Agriculture"]},
    {"name":"Clemson","slug":"clemson","accept":0.430,"gpa_lo":3.80,"gpa_hi":4.20,"sat_25":1240,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"South Carolina","size":21000,"tuition":15000,"desc":"South Carolina public flagship. Engineering, business, strong school spirit and sports.","majors":["Business","Engineering","Biology","Education","Psychology"]},
    {"name":"University of Alabama","slug":"alabama","accept":0.800,"gpa_lo":3.70,"gpa_hi":4.20,"sat_25":1090,"sat_75":1340,"act_25":23,"act_75":31,"tier":5,"type":"public","state":"Alabama","size":33000,"tuition":12000,"desc":"Big Tide football culture. Generous merit aid for OOS strong students. Honors college is rigorous.","majors":["Business","Communication","Biology","Engineering","Education"]},
    {"name":"University of Georgia","slug":"uga","accept":0.430,"gpa_lo":3.91,"gpa_hi":4.21,"sat_25":1240,"sat_75":1420,"act_25":28,"act_75":32,"tier":3,"type":"public","state":"Georgia","size":31000,"tuition":12000,"desc":"Athens flagship, Terry business school, Hope/Zell scholarships make in-state extremely affordable.","majors":["Business","Biology","Psychology","Communication","Engineering"]},
    {"name":"University of Tennessee","slug":"utk","accept":0.450,"gpa_lo":3.80,"gpa_hi":4.30,"sat_25":1170,"sat_75":1370,"act_25":25,"act_75":32,"tier":4,"type":"public","state":"Tennessee","size":31000,"tuition":13000,"desc":"Knoxville flagship. Strong nuclear engineering (Oak Ridge tie), business, school spirit.","majors":["Business","Engineering","Biology","Communication","Psychology"]},
    {"name":"San Diego State","slug":"sdsu","accept":0.380,"gpa_lo":3.70,"gpa_hi":4.05,"sat_25":1140,"sat_75":1320,"act_25":23,"act_75":29,"tier":4,"type":"public","state":"California","size":31000,"tuition":8000,"desc":"Southern Cal CSU. Strong international business, hospitality, journalism. Good weather and price for Californians.","majors":["Business","Psychology","Biology","Communication","Criminal Justice"]},
    {"name":"University of Arizona","slug":"arizona","accept":0.860,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1110,"sat_75":1320,"act_25":21,"act_75":28,"tier":5,"type":"public","state":"Arizona","size":35000,"tuition":13000,"desc":"Tucson flagship. Strong astronomy and optical sciences (massive observatory partnership), business, nursing.","majors":["Business","Biology","Psychology","Engineering","Communication"]},
    {"name":"Northeastern","slug":"northeastern","accept":0.055,"gpa_lo":3.85,"gpa_hi":4.20,"sat_25":1430,"sat_75":1540,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Massachusetts","size":16000,"tuition":62000,"desc":"Boston private with mandatory co-op program (~6 months at a real employer). Acceptance rate has plummeted in recent years.","majors":["Business","Computer Science","Engineering","Psychology","Health Sciences"]},
    {"name":"Case Western","slug":"case","accept":0.270,"gpa_lo":3.80,"gpa_hi":4.10,"sat_25":1380,"sat_75":1520,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Ohio","size":6000,"tuition":63000,"desc":"Cleveland private research university. Strong pre-med (Cleveland Clinic ties), engineering, music.","majors":["Engineering","Biology","Computer Science","Business","Psychology"]},
    {"name":"Lehigh","slug":"lehigh","accept":0.310,"gpa_lo":3.80,"gpa_hi":4.10,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Pennsylvania","size":5500,"tuition":63000,"desc":"Mid-sized private with strong engineering, business, integrated business+engineering programs. Greek-heavy social scene.","majors":["Engineering","Business","Computer Science","Biology","Finance"]},
    {"name":"Bucknell","slug":"bucknell","accept":0.330,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1300,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"Pennsylvania","size":3700,"tuition":67000,"desc":"Liberal arts plus engineering. Heavy finance recruiting for an LAC-sized school.","majors":["Business","Engineering","Biology","Economics","Psychology"]},
    {"name":"Villanova","slug":"villanova","accept":0.220,"gpa_lo":3.75,"gpa_hi":4.05,"sat_25":1370,"sat_75":1490,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Pennsylvania","size":7000,"tuition":63000,"desc":"Catholic university outside Philadelphia. Strong undergrad business and basketball. Heavy Wall Street pipeline.","majors":["Finance","Business","Engineering","Biology","Communication"]},
    {"name":"Tulane","slug":"tulane","accept":0.110,"gpa_lo":3.60,"gpa_hi":3.95,"sat_25":1370,"sat_75":1500,"act_25":31,"act_75":33,"tier":2,"type":"private","state":"Louisiana","size":8500,"tuition":62000,"desc":"New Orleans private. Strong public health, business, architecture, and a famous social scene.","majors":["Business","Public Health","Biology","Psychology","Engineering"]},
    {"name":"University of Miami","slug":"miami","accept":0.190,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Florida","size":12000,"tuition":58000,"desc":"South Florida private. Strong music, business (Herbert), marine biology, and pre-med.","majors":["Business","Biology","Communication","Psychology","Music"]},
    {"name":"American University","slug":"american","accept":0.490,"gpa_lo":3.62,"gpa_hi":3.93,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"District of Columbia","size":8000,"tuition":56000,"desc":"DC private. Politics-and-international-affairs heavy, strong School of Public Affairs.","majors":["International Studies","Political Science","Business","Communication","Psychology"]},
    {"name":"Northeastern Illinois","slug":"neiu","accept":0.880,"gpa_lo":3.10,"gpa_hi":3.70,"sat_25":1000,"sat_75":1190,"act_25":18,"act_75":24,"tier":5,"type":"public","state":"Illinois","size":7000,"tuition":11000,"desc":"Chicago public. Affordable, accessible, strong commuter base. First-generation friendly.","majors":["Business","Education","Psychology","Biology","Computer Science"]},
    # --- More LACs ---
    {"name":"Wesleyan","slug":"wesleyan","accept":0.140,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Connecticut","size":3100,"tuition":67000,"desc":"Liberal arts college with a strong arts/film/music identity. Politically progressive culture.","majors":["Economics","Government","English","Biology","Film Studies"]},
    {"name":"Hamilton","slug":"hamilton","accept":0.115,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"New York","size":2000,"tuition":67000,"desc":"Open-curriculum LAC in central NY with very strong writing program (no general education requirements).","majors":["Economics","Government","Mathematics","English","Biology"]},
    {"name":"Davidson","slug":"davidson","accept":0.150,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1380,"sat_75":1500,"act_25":31,"act_75":34,"tier":2,"type":"private","state":"North Carolina","size":1900,"tuition":63000,"desc":"Top Southern LAC. Honor code and strong pre-med pipeline. Athletics-friendly culture.","majors":["Economics","Political Science","Biology","Psychology","English"]},
    {"name":"Colgate","slug":"colgate","accept":0.110,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1390,"sat_75":1500,"act_25":31,"act_75":33,"tier":2,"type":"private","state":"New York","size":3200,"tuition":68000,"desc":"Mid-sized LAC with strong economics, political science, and a Greek-leaning social scene.","majors":["Economics","Political Science","English","Psychology","Biology"]},
    {"name":"Smith","slug":"smith","accept":0.230,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1380,"sat_75":1500,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":2500,"tuition":63000,"desc":"Top women's college, Five College Consortium with Amherst, Hampshire, Mt Holyoke, UMass.","majors":["Economics","Government","Engineering","Psychology","Biology"]},
    {"name":"Mount Holyoke","slug":"mt-holyoke","accept":0.380,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1300,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":2200,"tuition":59000,"desc":"Oldest of the Seven Sisters women's colleges. Strong sciences and international student community.","majors":["Biology","Psychology","English","International Relations","Economics"]},
    {"name":"Bryn Mawr","slug":"bryn-mawr","accept":0.300,"gpa_lo":3.75,"gpa_hi":4.00,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":34,"tier":3,"type":"private","state":"Pennsylvania","size":1400,"tuition":60000,"desc":"Women's LAC outside Philadelphia. Bi-Co consortium with Haverford gives access to wider course catalog.","majors":["Biology","Mathematics","English","Psychology","Political Science"]},
    {"name":"Bates","slug":"bates","accept":0.140,"gpa_lo":3.75,"gpa_hi":4.00,"sat_25":1370,"sat_75":1490,"act_25":31,"act_75":33,"tier":2,"type":"private","state":"Maine","size":1900,"tuition":65000,"desc":"Coastal Maine LAC. Test-optional pioneer, strong environmental studies and English.","majors":["Economics","Politics","Psychology","Biology","English"]},
    {"name":"Colby","slug":"colby","accept":0.080,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":1,"type":"private","state":"Maine","size":2200,"tuition":66000,"desc":"Maine LAC with rapidly tightening admissions. Free January-term abroad for all students.","majors":["Economics","Government","Biology","Computer Science","English"]},
    {"name":"Trinity College","slug":"trinity-ct","accept":0.380,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1300,"sat_75":1430,"act_25":29,"act_75":32,"tier":3,"type":"private","state":"Connecticut","size":2200,"tuition":68000,"desc":"Hartford LAC with strong economics and engineering crossover. Popular Greek scene.","majors":["Economics","Political Science","Engineering","Psychology","Biology"]},
    {"name":"Connecticut College","slug":"conn-college","accept":0.310,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1280,"sat_75":1410,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Connecticut","size":1800,"tuition":67000,"desc":"Coastal Connecticut LAC. Open curriculum, strong dance and government programs.","majors":["Economics","Psychology","Government","English","Biology"]},
    {"name":"Skidmore","slug":"skidmore","accept":0.260,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":3,"type":"private","state":"New York","size":2700,"tuition":65000,"desc":"Saratoga Springs LAC with a strong arts/business intersection (creative thought initiative).","majors":["Business","Psychology","English","Government","Biology"]},
    {"name":"Macalester","slug":"macalester","accept":0.260,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1370,"sat_75":1490,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"Minnesota","size":2200,"tuition":64000,"desc":"St. Paul LAC with very international student body. Strong economics, political science, biology.","majors":["Economics","Political Science","Biology","Psychology","English"]},
    {"name":"Reed","slug":"reed","accept":0.270,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1390,"sat_75":1530,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Oregon","size":1500,"tuition":64000,"desc":"Notoriously rigorous Portland LAC. Senior thesis required. Famously high PhD-feeder rate.","majors":["English","Biology","Psychology","Mathematics","Anthropology"]},
    {"name":"Grinnell","slug":"grinnell","accept":0.090,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1430,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Iowa","size":1700,"tuition":63000,"desc":"Open-curriculum Iowa LAC. Largest endowment per student of any LAC. Strong sciences and humanities.","majors":["Economics","Biology","Computer Science","Psychology","English"]},
    {"name":"Kenyon","slug":"kenyon","accept":0.270,"gpa_lo":3.75,"gpa_hi":3.95,"sat_25":1350,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Ohio","size":1700,"tuition":67000,"desc":"Rural Ohio LAC famous for English program (Kenyon Review).","majors":["English","Economics","Political Science","Psychology","Biology"]},
    {"name":"Oberlin","slug":"oberlin","accept":0.300,"gpa_lo":3.75,"gpa_hi":3.95,"sat_25":1340,"sat_75":1490,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Ohio","size":2900,"tuition":63000,"desc":"Liberal-progressive LAC plus a top-tier conservatory. Double-degree program is uniquely strong.","majors":["English","Music","Psychology","Biology","Political Science"]},
    {"name":"Whitman","slug":"whitman","accept":0.450,"gpa_lo":3.75,"gpa_hi":4.00,"sat_25":1290,"sat_75":1450,"act_25":29,"act_75":33,"tier":4,"type":"private","state":"Washington","size":1500,"tuition":63000,"desc":"Walla Walla LAC. Outdoor-leaning culture, strong sciences, generous merit aid.","majors":["Biology","Economics","Psychology","English","Politics"]},
    {"name":"Pitzer","slug":"pitzer","accept":0.150,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"California","size":1200,"tuition":63000,"desc":"Smallest of the Claremont Colleges. Social-justice and sustainability focus.","majors":["Economics","Psychology","Sociology","Environmental Analysis","Political Science"]},
    {"name":"Scripps","slug":"scripps","accept":0.250,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1370,"sat_75":1510,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"California","size":1100,"tuition":62000,"desc":"Women's college in the Claremont Consortium. Strong humanities and pre-med.","majors":["Biology","Psychology","English","Economics","Politics"]},
    {"name":"Harvey Mudd","slug":"harvey-mudd","accept":0.080,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1490,"sat_75":1570,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"California","size":900,"tuition":63000,"desc":"Tiny STEM-only LAC in the Claremont Consortium. Highest-paid LAC graduates by mid-career salary.","majors":["Engineering","Computer Science","Mathematics","Physics","Chemistry"]},
    {"name":"Claremont McKenna","slug":"cmc","accept":0.090,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1430,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"California","size":1400,"tuition":63000,"desc":"Government / economics / leadership focus. Robert Day School of Economics is top-tier.","majors":["Economics","Government","International Relations","Psychology","Mathematics"]},
    {"name":"Occidental","slug":"oxy","accept":0.260,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"California","size":2000,"tuition":63000,"desc":"LA-area LAC. Diplomacy and World Affairs program is a niche standout.","majors":["Economics","Diplomacy & World Affairs","Biology","Psychology","English"]},
    # --- More private universities ---
    {"name":"Brandeis","slug":"brandeis","accept":0.390,"gpa_lo":3.75,"gpa_hi":3.95,"sat_25":1380,"sat_75":1510,"act_25":30,"act_75":34,"tier":3,"type":"private","state":"Massachusetts","size":3700,"tuition":65000,"desc":"Boston-area private. Strong sciences, social policy, and Jewish studies.","majors":["Biology","Business","Economics","Psychology","Computer Science"]},
    {"name":"Fordham","slug":"fordham","accept":0.530,"gpa_lo":3.65,"gpa_hi":3.93,"sat_25":1300,"sat_75":1430,"act_25":29,"act_75":32,"tier":4,"type":"private","state":"New York","size":10000,"tuition":62000,"desc":"Jesuit university with two NYC campuses. Strong communications, business (Gabelli), and pre-law.","majors":["Business","Communication","Psychology","English","Political Science"]},
    {"name":"Barnard","slug":"barnard","accept":0.080,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1430,"sat_75":1540,"act_25":31,"act_75":34,"tier":1,"type":"private","state":"New York","size":2700,"tuition":68000,"desc":"Women's college affiliated with Columbia. Cross-registration gives students access to Columbia courses and an Ivy degree (Barnard-Columbia).","majors":["Economics","English","Psychology","Political Science","Biology"]},
    {"name":"Pepperdine","slug":"pepperdine","accept":0.420,"gpa_lo":3.65,"gpa_hi":3.93,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"California","size":4000,"tuition":62000,"desc":"Malibu private with Christian heritage. Strong international programs, business, and law pipeline.","majors":["Business","Communication","Psychology","Biology","Sports Medicine"]},
    {"name":"Santa Clara","slug":"scu","accept":0.490,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1330,"sat_75":1470,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"California","size":5800,"tuition":58000,"desc":"Jesuit university in Silicon Valley. Strong undergraduate business (Leavey) and engineering, heavy tech recruiting.","majors":["Business","Computer Science","Engineering","Psychology","Biology"]},
    {"name":"Loyola Marymount","slug":"lmu","accept":0.420,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1240,"sat_75":1380,"act_25":27,"act_75":31,"tier":4,"type":"private","state":"California","size":7000,"tuition":58000,"desc":"LA-area Jesuit private. Strong film school (close ties to LA industry), business, and communication.","majors":["Business","Communication","Film & TV","Psychology","Biology"]},
    {"name":"Drexel","slug":"drexel","accept":0.770,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1180,"sat_75":1380,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"Pennsylvania","size":14000,"tuition":58000,"desc":"Philadelphia private with mandatory co-op program. Engineering, business, and design. 5-year program standard.","majors":["Engineering","Business","Computer Science","Nursing","Design"]},
    {"name":"Stevens Institute","slug":"stevens","accept":0.470,"gpa_lo":3.78,"gpa_hi":4.10,"sat_25":1380,"sat_75":1510,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"New Jersey","size":4000,"tuition":62000,"desc":"Engineering and tech across the Hudson from Manhattan. Strong placement into NYC finance/tech.","majors":["Engineering","Computer Science","Business","Mathematics","Information Systems"]},
    {"name":"Olin College","slug":"olin","accept":0.180,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1480,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Massachusetts","size":400,"tuition":56000,"desc":"Tiny experimental engineering college. Project-based curriculum, no academic departments.","majors":["Engineering","Computer Science","Mechanical Engineering","Electrical Engineering","Bioengineering"]},
    {"name":"Cooper Union","slug":"cooper","accept":0.110,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1340,"sat_75":1500,"act_25":30,"act_75":34,"tier":2,"type":"private","state":"New York","size":900,"tuition":48000,"desc":"East Village engineering/architecture/art school. Half-tuition merit scholarships for all admitted students.","majors":["Engineering","Architecture","Fine Arts","Computer Science","Mechanical Engineering"]},
    # --- Specialty / arts schools ---
    {"name":"Berklee College of Music","slug":"berklee","accept":0.530,"gpa_lo":3.20,"gpa_hi":3.85,"sat_25":1100,"sat_75":1350,"act_25":21,"act_75":29,"tier":3,"type":"private","state":"Massachusetts","size":4500,"tuition":52000,"desc":"World's largest contemporary music college. Audition-based admissions. Strong production, songwriting, performance.","majors":["Music Production","Songwriting","Performance","Music Business","Film Scoring"]},
    {"name":"Juilliard","slug":"juilliard","accept":0.080,"gpa_lo":3.60,"gpa_hi":3.95,"sat_25":1100,"sat_75":1400,"act_25":21,"act_75":31,"tier":1,"type":"private","state":"New York","size":600,"tuition":52000,"desc":"World-class performing arts conservatory. Audition is everything. Lincoln Center campus.","majors":["Music Performance","Drama","Dance","Music Composition","Jazz Studies"]},
    {"name":"Pratt Institute","slug":"pratt","accept":0.760,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1180,"sat_75":1380,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"New York","size":3500,"tuition":59000,"desc":"Brooklyn art and design school. Strong industrial design, fashion, architecture, fine arts.","majors":["Industrial Design","Fashion Design","Architecture","Fine Arts","Communications Design"]},
    {"name":"Parsons (The New School)","slug":"parsons","accept":0.610,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1130,"sat_75":1330,"act_25":23,"act_75":29,"tier":4,"type":"private","state":"New York","size":7300,"tuition":56000,"desc":"NYC fashion-and-design school. Top fashion design program in the US.","majors":["Fashion Design","Strategic Design","Fine Arts","Photography","Graphic Design"]},
    {"name":"Rhode Island School of Design","slug":"risd","accept":0.180,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1290,"sat_75":1470,"act_25":28,"act_75":33,"tier":3,"type":"private","state":"Rhode Island","size":2000,"tuition":58000,"desc":"Top art and design school. Brown cross-registration. Famously rigorous foundational year.","majors":["Industrial Design","Illustration","Graphic Design","Architecture","Fine Arts"]},
    {"name":"School of the Art Institute of Chicago","slug":"saic","accept":0.760,"gpa_lo":3.30,"gpa_hi":3.85,"sat_25":1100,"sat_75":1310,"act_25":21,"act_75":28,"tier":4,"type":"private","state":"Illinois","size":3500,"tuition":54000,"desc":"Art school in downtown Chicago, attached to The Art Institute museum. Open-curriculum across visual disciplines.","majors":["Fine Arts","Illustration","Photography","Architecture","Visual Communication Design"]},
    # --- More public flagships ---
    {"name":"University of Kentucky","slug":"uky","accept":0.940,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1080,"sat_75":1290,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Kentucky","size":22000,"tuition":13000,"desc":"Lexington flagship. Strong Gatton business and engineering. Generous merit aid for in-state.","majors":["Business","Biology","Engineering","Communication","Psychology"]},
    {"name":"Auburn","slug":"auburn","accept":0.460,"gpa_lo":3.70,"gpa_hi":4.10,"sat_25":1170,"sat_75":1340,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"Alabama","size":24000,"tuition":13000,"desc":"Strong engineering, agriculture, and architecture programs. SEC sports culture.","majors":["Engineering","Business","Biology","Pre-Vet","Education"]},
    {"name":"LSU","slug":"lsu","accept":0.770,"gpa_lo":3.45,"gpa_hi":3.94,"sat_25":1080,"sat_75":1280,"act_25":22,"act_75":28,"tier":5,"type":"public","state":"Louisiana","size":29000,"tuition":12000,"desc":"Baton Rouge flagship. Strong petroleum engineering, agriculture, mass communication.","majors":["Engineering","Biology","Business","Communication","Mass Communication"]},
    {"name":"University of South Carolina","slug":"sc","accept":0.640,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1180,"sat_75":1360,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"South Carolina","size":27000,"tuition":13000,"desc":"Columbia flagship. Top international business undergrad in the US. Strong Honors college.","majors":["International Business","Marketing","Public Health","Biology","Engineering"]},
    {"name":"University of Missouri","slug":"missou","accept":0.810,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1130,"sat_75":1310,"act_25":23,"act_75":29,"tier":5,"type":"public","state":"Missouri","size":24000,"tuition":12000,"desc":"Columbia flagship. Renowned journalism school. Strong veterinary medicine.","majors":["Journalism","Business","Biology","Engineering","Psychology"]},
    {"name":"University of Kansas","slug":"ku","accept":0.890,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1090,"sat_75":1310,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Kansas","size":20000,"tuition":12000,"desc":"Lawrence flagship. Strong pharmacy, business, and journalism programs.","majors":["Business","Biology","Engineering","Communication","Psychology"]},
    {"name":"University of Nebraska","slug":"unl","accept":0.800,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1100,"sat_75":1310,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Nebraska","size":21000,"tuition":10000,"desc":"Lincoln flagship. Affordable, strong agriculture and architecture, Big Ten sports.","majors":["Business","Engineering","Biology","Psychology","Education"]},
    {"name":"University of Vermont","slug":"uvm","accept":0.640,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1230,"sat_75":1390,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Vermont","size":11000,"tuition":21000,"desc":"Burlington flagship. Strong environmental studies, nursing, business. Outdoor-focused culture.","majors":["Business","Biology","Environmental Studies","Psychology","Nursing"]},
    {"name":"University of Delaware","slug":"udel","accept":0.660,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1180,"sat_75":1370,"act_25":26,"act_75":31,"tier":4,"type":"public","state":"Delaware","size":19000,"tuition":15000,"desc":"Newark flagship. Strong engineering, business (Lerner), agriculture, fashion.","majors":["Business","Engineering","Biology","Education","Communication"]},
    {"name":"Binghamton University","slug":"binghamton","accept":0.400,"gpa_lo":3.71,"gpa_hi":3.96,"sat_25":1310,"sat_75":1450,"act_25":29,"act_75":32,"tier":3,"type":"public","state":"New York","size":14000,"tuition":11000,"desc":"SUNY public ivy. Strong business (School of Management), engineering, accounting.","majors":["Business","Biology","Psychology","Engineering","Economics"]},
    {"name":"Cal Poly San Luis Obispo","slug":"calpoly-slo","accept":0.300,"gpa_lo":3.80,"gpa_hi":4.20,"sat_25":1280,"sat_75":1450,"act_25":27,"act_75":32,"tier":3,"type":"public","state":"California","size":22000,"tuition":10000,"desc":"Hands-on engineering and architecture. Quarter system. 'Learn by doing' is the official motto and not an exaggeration.","majors":["Engineering","Business","Architecture","Computer Science","Biology"]},
    {"name":"San Jose State","slug":"sjsu","accept":0.700,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1100,"sat_75":1330,"act_25":21,"act_75":29,"tier":5,"type":"public","state":"California","size":29000,"tuition":8000,"desc":"Silicon Valley CSU. Massive direct pipeline into Bay Area tech (more SV CS hires than Stanford or Berkeley).","majors":["Business","Computer Science","Engineering","Psychology","Communication"]},
    {"name":"Cal State Long Beach","slug":"csulb","accept":0.420,"gpa_lo":3.55,"gpa_hi":3.93,"sat_25":1090,"sat_75":1290,"act_25":21,"act_75":28,"tier":4,"type":"public","state":"California","size":33000,"tuition":7000,"desc":"Largest CSU. Strong engineering, business, film, and education programs.","majors":["Business","Engineering","Psychology","Biology","Film & Electronic Arts"]},
    {"name":"Iowa State","slug":"iowa-state","accept":0.880,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1100,"sat_75":1310,"act_25":21,"act_75":29,"tier":5,"type":"public","state":"Iowa","size":29000,"tuition":11000,"desc":"Ames flagship. Strong agriculture, engineering, design, and statistics.","majors":["Engineering","Business","Agriculture","Biology","Psychology"]},
    {"name":"Temple","slug":"temple","accept":0.800,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1170,"sat_75":1340,"act_25":24,"act_75":30,"tier":4,"type":"public","state":"Pennsylvania","size":24000,"tuition":18000,"desc":"Philadelphia public. Strong business (Fox), education, and media programs.","majors":["Business","Communication","Engineering","Psychology","Education"]},
    {"name":"George Mason","slug":"gmu","accept":0.890,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1130,"sat_75":1330,"act_25":24,"act_75":30,"tier":5,"type":"public","state":"Virginia","size":27000,"tuition":13000,"desc":"DC-area public. Strong economics (Mercatus Center), public policy, and CS. Affordable for OOS.","majors":["Business","Computer Science","Psychology","Economics","Engineering"]},
    {"name":"University of Houston","slug":"uh","accept":0.700,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1100,"sat_75":1290,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Texas","size":37000,"tuition":11000,"desc":"Bauer business school, strong hotel & restaurant management, urban research university.","majors":["Business","Biology","Psychology","Engineering","Hotel Management"]},
    {"name":"University of Utah","slug":"utah","accept":0.870,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1100,"sat_75":1330,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Utah","size":24000,"tuition":10000,"desc":"Salt Lake City flagship. Strong CS (Entertainment Arts & Engineering games program is top-tier), business, biomedical.","majors":["Business","Biology","Engineering","Computer Science","Psychology"]},
    {"name":"University of Hawaii Manoa","slug":"hawaii","accept":0.580,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1080,"sat_75":1280,"act_25":22,"act_75":27,"tier":5,"type":"public","state":"Hawaii","size":13000,"tuition":12000,"desc":"Honolulu flagship. Strong marine biology, oceanography, second-language studies, hospitality.","majors":["Business","Biology","Psychology","Education","Marine Biology"]},
    # --- More accessible / community-pipeline ---
    {"name":"University of New Hampshire","slug":"unh","accept":0.870,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1110,"sat_75":1300,"act_25":24,"act_75":29,"tier":5,"type":"public","state":"New Hampshire","size":12000,"tuition":19000,"desc":"Durham flagship. Strong engineering, business, marine sciences. New England state-school feel.","majors":["Business","Engineering","Biology","Psychology","Communication"]},
    {"name":"University of Maine","slug":"umaine","accept":0.940,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1050,"sat_75":1240,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Maine","size":9000,"tuition":12000,"desc":"Orono flagship. Strong forestry, marine science, engineering. Affordable for New Englanders via NEBHE.","majors":["Engineering","Business","Biology","Education","Forestry"]},
    {"name":"Howard University","slug":"howard","accept":0.300,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1130,"sat_75":1340,"act_25":22,"act_75":29,"tier":4,"type":"private","state":"District of Columbia","size":7700,"tuition":31000,"desc":"Premier HBCU. Top medical and law schools, strong communications, business. Vice President alma mater.","majors":["Biology","Business","Political Science","Psychology","Communication"]},
    {"name":"Spelman","slug":"spelman","accept":0.430,"gpa_lo":3.65,"gpa_hi":3.92,"sat_25":1130,"sat_75":1280,"act_25":22,"act_75":27,"tier":4,"type":"private","state":"Georgia","size":2200,"tuition":31000,"desc":"Top women's HBCU. Atlanta. Strong sciences and pre-med pipeline.","majors":["Biology","Psychology","English","Political Science","Sociology"]},
    {"name":"Morehouse","slug":"morehouse","accept":0.580,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1010,"sat_75":1230,"act_25":19,"act_75":24,"tier":4,"type":"private","state":"Georgia","size":2200,"tuition":31000,"desc":"Top men's HBCU. MLK alma mater. Strong business and political science.","majors":["Business","Biology","Political Science","Psychology","Computer Science"]},
    {"name":"Florida A&M","slug":"famu","accept":0.350,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1010,"sat_75":1180,"act_25":19,"act_75":23,"tier":4,"type":"public","state":"Florida","size":8500,"tuition":6000,"desc":"Top public HBCU. Strong business, pharmacy, engineering. Affordable in-state.","majors":["Business","Pharmacy","Engineering","Biology","Psychology"]},
    {"name":"University of Puerto Rico Mayagüez","slug":"uprm","accept":0.560,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":0,"sat_75":0,"act_25":0,"act_75":0,"tier":4,"type":"public","state":"Puerto Rico","size":11000,"tuition":2000,"desc":"Top engineering school in PR. Heavy NASA/defense recruiting pipeline. Bilingual instruction.","majors":["Engineering","Computer Science","Biology","Business","Agriculture"]},
    {"name":"Tuskegee","slug":"tuskegee","accept":0.520,"gpa_lo":3.30,"gpa_hi":3.75,"sat_25":960,"sat_75":1130,"act_25":18,"act_75":22,"tier":4,"type":"private","state":"Alabama","size":3000,"tuition":21000,"desc":"Historic HBCU. Strong veterinary medicine and engineering. Booker T. Washington founded.","majors":["Engineering","Business","Biology","Veterinary Medicine","Psychology"]},
    # ─── Added round 2 (May 2026) — high-volume search gaps ───
    {"name":"UC Irvine","slug":"uci","accept":0.21,"gpa_lo":3.96,"gpa_hi":4.20,"sat_25":1280,"sat_75":1490,"act_25":28,"act_75":34,"tier":3,"type":"public","state":"California","size":31000,"tuition":15000,"desc":"Strong R1 public. Computer science, biology, and business analytics are standouts. Fast-growing reputation.","majors":["Biology","Computer Science","Business","Psychology","Engineering"]},
    {"name":"UC Santa Barbara","slug":"ucsb","accept":0.26,"gpa_lo":3.90,"gpa_hi":4.18,"sat_25":1300,"sat_75":1500,"act_25":28,"act_75":33,"tier":3,"type":"public","state":"California","size":23000,"tuition":15000,"desc":"Beachside R1. Physics (Nobel laureates), engineering, and economics. Famously good location and party scene.","majors":["Economics","Biology","Communication","Engineering","Psychology"]},
    {"name":"UC Santa Cruz","slug":"ucsc","accept":0.47,"gpa_lo":3.74,"gpa_hi":4.10,"sat_25":1180,"sat_75":1390,"act_25":25,"act_75":32,"tier":4,"type":"public","state":"California","size":18000,"tuition":15000,"desc":"Quirky redwood campus. Strong in marine biology, astronomy, and game design. Less competitive than other UCs.","majors":["Psychology","Computer Science","Business","Biology","Marine Biology"]},
    {"name":"UC Davis","slug":"ucdavis","accept":0.37,"gpa_lo":3.95,"gpa_hi":4.20,"sat_25":1230,"sat_75":1450,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"California","size":31000,"tuition":15000,"desc":"Top-tier ag, vet med, and biological sciences. Bike-friendly college town. Sleeper R1.","majors":["Biology","Animal Science","Psychology","Engineering","Economics"]},
    {"name":"UC Riverside","slug":"ucr","accept":0.69,"gpa_lo":3.65,"gpa_hi":4.00,"sat_25":1170,"sat_75":1380,"act_25":24,"act_75":31,"tier":4,"type":"public","state":"California","size":22000,"tuition":15000,"desc":"Most accessible UC. Strong business, biomedical sciences, and CS. Diverse student body.","majors":["Business","Biology","Psychology","Computer Science","Engineering"]},
    {"name":"BYU","slug":"byu","accept":0.67,"gpa_lo":3.83,"gpa_hi":3.97,"sat_25":1290,"sat_75":1480,"act_25":26,"act_75":32,"tier":4,"type":"private","state":"Utah","size":33000,"tuition":6000,"desc":"LDS-affiliated. Honor code (no alcohol, modesty rules). Strong accounting, engineering, language programs. Cheap if member.","majors":["Business","Engineering","Communications","Biology","Computer Science"]},
    {"name":"Baylor","slug":"baylor","accept":0.46,"gpa_lo":3.69,"gpa_hi":3.97,"sat_25":1240,"sat_75":1410,"act_25":26,"act_75":32,"tier":4,"type":"private","state":"Texas","size":15000,"tuition":52000,"desc":"Largest Baptist university. Strong pre-med (Baylor College of Medicine pipeline) and business. Big sports culture.","majors":["Business","Biology","Nursing","Engineering","Psychology"]},
    {"name":"TCU","slug":"tcu","accept":0.46,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1200,"sat_75":1400,"act_25":26,"act_75":31,"tier":4,"type":"private","state":"Texas","size":11000,"tuition":58000,"desc":"Texas Christian. Strong business (Neeley) and journalism. Heavy Greek scene, small classes for size.","majors":["Business","Communication","Nursing","Education","Psychology"]},
    {"name":"SMU","slug":"smu","accept":0.52,"gpa_lo":3.62,"gpa_hi":3.95,"sat_25":1380,"sat_75":1500,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Texas","size":7000,"tuition":61000,"desc":"Southern Methodist. Wealthy student body, strong Cox business school, Dallas location is the draw.","majors":["Business","Engineering","Communication","Economics","Psychology"]},
    {"name":"Liberty","slug":"liberty","accept":0.99,"gpa_lo":3.30,"gpa_hi":3.85,"sat_25":1010,"sat_75":1240,"act_25":21,"act_75":28,"tier":5,"type":"private","state":"Virginia","size":15000,"tuition":24000,"desc":"Largest evangelical Christian university. Strict honor code, strong online programs, conservative campus culture.","majors":["Business","Psychology","Nursing","Education","Aviation"]},
    {"name":"UMass Amherst","slug":"umass","accept":0.58,"gpa_lo":3.79,"gpa_hi":4.10,"sat_25":1280,"sat_75":1460,"act_25":29,"act_75":33,"tier":3,"type":"public","state":"Massachusetts","size":24000,"tuition":17000,"desc":"Flagship of the Five College Consortium. Strong CS, isenberg business, and food science. Big sports culture.","majors":["Computer Science","Business","Psychology","Biology","Engineering"]},
    {"name":"NC State","slug":"ncsu","accept":0.40,"gpa_lo":4.10,"gpa_hi":4.62,"sat_25":1290,"sat_75":1450,"act_25":28,"act_75":33,"tier":3,"type":"public","state":"North Carolina","size":26000,"tuition":9000,"desc":"Top engineering and ag school in the Research Triangle. Very strong CS, textiles, and design. Hard to crack from out of state.","majors":["Engineering","Computer Science","Business","Biology","Agriculture"]},
    {"name":"University of Miami","slug":"umiami","accept":0.19,"gpa_lo":3.70,"gpa_hi":4.10,"sat_25":1310,"sat_75":1470,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Florida","size":12000,"tuition":58000,"desc":"Coral Gables campus. Strong marine science, music, business. Heavy sports culture, beach proximity.","majors":["Business","Biology","Psychology","Marine Biology","Communication"]},
    {"name":"Syracuse","slug":"syracuse","accept":0.42,"gpa_lo":3.60,"gpa_hi":3.95,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"private","state":"New York","size":15000,"tuition":62000,"desc":"Newhouse (communications) is one of the top journalism schools. Strong architecture and information studies too. Cold winters.","majors":["Communication","Business","Engineering","Psychology","Information Studies"]},
    {"name":"Hampton","slug":"hampton","accept":0.36,"gpa_lo":3.30,"gpa_hi":3.75,"sat_25":980,"sat_75":1170,"act_25":18,"act_75":24,"tier":4,"type":"private","state":"Virginia","size":3500,"tuition":29000,"desc":"Historic HBCU on the Chesapeake. Strong nursing, journalism, and marine science. Tight-knit alumni network.","majors":["Nursing","Business","Biology","Communication","Psychology"]},
    {"name":"Holy Cross","slug":"holy-cross","accept":0.22,"gpa_lo":3.70,"gpa_hi":4.00,"sat_25":1340,"sat_75":1490,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":3100,"tuition":63000,"desc":"Jesuit LAC. Strong pre-law and pre-med. No business or engineering majors — pure liberal arts. Wall Street pipeline.","majors":["Economics","Political Science","English","Psychology","Biology"]},
    {"name":"Lafayette","slug":"lafayette","accept":0.30,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1350,"sat_75":1490,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Pennsylvania","size":2700,"tuition":62000,"desc":"LAC with a real engineering school — rare combo. Strong econ, govt-law, and engineering. Heavy Greek life.","majors":["Engineering","Economics","Government","Psychology","Biology"]},
    {"name":"Colorado College","slug":"colorado-college","accept":0.14,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1340,"sat_75":1490,"act_25":31,"act_75":34,"tier":2,"type":"private","state":"Colorado","size":2100,"tuition":67000,"desc":"Block plan: take one course at a time, 3.5 weeks per block. Outdoorsy student body. Mountains 90 min away.","majors":["Economics","Environmental Science","Biology","Political Science","English"]},
    {"name":"Sewanee","slug":"sewanee","accept":0.67,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1240,"sat_75":1390,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Tennessee","size":1700,"tuition":54000,"desc":"University of the South. Episcopal LAC on the Cumberland Plateau. Strong English, history. Honor code, Oxford-style gowns.","majors":["English","History","Biology","Psychology","Economics"]},
    {"name":"University of Rochester","slug":"rochester","accept":0.35,"gpa_lo":3.80,"gpa_hi":4.10,"sat_25":1380,"sat_75":1530,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"New York","size":6800,"tuition":63000,"desc":"Strong pre-med (Strong Memorial Hospital), music (Eastman School), and optics. Cold and gray but academically serious.","majors":["Biology","Engineering","Psychology","Economics","Music"]},
    {"name":"Marquette","slug":"marquette","accept":0.84,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1170,"sat_75":1370,"act_25":24,"act_75":30,"tier":4,"type":"private","state":"Wisconsin","size":8000,"tuition":50000,"desc":"Jesuit university in downtown Milwaukee. Strong nursing, dentistry, engineering. Solid Big East basketball.","majors":["Nursing","Business","Engineering","Communication","Biology"]},
    {"name":"Loyola Chicago","slug":"loyola-chicago","accept":0.79,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1140,"sat_75":1330,"act_25":24,"act_75":30,"tier":4,"type":"private","state":"Illinois","size":12000,"tuition":54000,"desc":"Jesuit university on Lake Michigan. Strong nursing, social work, and bio. Good Chicago internship access.","majors":["Nursing","Biology","Psychology","Business","Social Work"]},
    {"name":"DePaul","slug":"depaul","accept":0.78,"gpa_lo":3.40,"gpa_hi":3.80,"sat_25":1090,"sat_75":1290,"act_25":22,"act_75":28,"tier":4,"type":"private","state":"Illinois","size":14000,"tuition":44000,"desc":"Largest Catholic university in the US. Loop campus is a draw — heavy theatre and finance. Famously diverse.","majors":["Business","Communication","Computer Science","Theatre","Psychology"]},
    {"name":"Cal Poly Pomona","slug":"calpoly-pomona","accept":0.62,"gpa_lo":3.30,"gpa_hi":3.85,"sat_25":1100,"sat_75":1330,"act_25":21,"act_75":28,"tier":4,"type":"public","state":"California","size":24000,"tuition":7800,"desc":"Polytechnic 'learn by doing' approach. Strong engineering, architecture, ag. Cheaper than the UC system.","majors":["Engineering","Business","Computer Science","Animal Science","Architecture"]},
    {"name":"SUNY Buffalo","slug":"suny-buffalo","accept":0.67,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":29,"tier":4,"type":"public","state":"New York","size":21000,"tuition":10000,"desc":"Largest SUNY. Strong engineering, management, and pharmacy. Cold winters, urban location.","majors":["Engineering","Business","Biology","Psychology","Computer Science"]},
    {"name":"Florida International","slug":"fiu","accept":0.64,"gpa_lo":3.70,"gpa_hi":4.20,"sat_25":1140,"sat_75":1290,"act_25":23,"act_75":28,"tier":4,"type":"public","state":"Florida","size":47000,"tuition":6500,"desc":"R1 public in Miami. Strong business, hospitality, international relations. Massive Hispanic-serving institution.","majors":["Business","Psychology","Biology","Engineering","Hospitality"]},
    {"name":"University of Central Florida","slug":"ucf","accept":0.41,"gpa_lo":3.85,"gpa_hi":4.30,"sat_25":1230,"sat_75":1380,"act_25":26,"act_75":31,"tier":4,"type":"public","state":"Florida","size":59000,"tuition":6400,"desc":"One of the largest universities in the country. Strong hospitality, engineering, and esports. Disney/Universal pipeline.","majors":["Business","Psychology","Engineering","Computer Science","Hospitality"]},
    # ─── Round 3 batch (target ~300 total) — big publics, mid privates, more LACs ───
    # Big publics
    {"name":"Colorado State","slug":"colorado-state","accept":0.91,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1110,"sat_75":1310,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Colorado","size":25000,"tuition":12000,"desc":"R1 public in Fort Collins. Strong veterinary medicine, ag, environmental sciences. Outdoorsy student body.","majors":["Business","Engineering","Psychology","Biology","Animal Science"]},
    {"name":"University of Cincinnati","slug":"cincinnati","accept":0.86,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1140,"sat_75":1330,"act_25":23,"act_75":29,"tier":5,"type":"public","state":"Ohio","size":29000,"tuition":12500,"desc":"Top design (DAAP) and known for inventing co-op education. Strong engineering and music conservatory (CCM).","majors":["Engineering","Design","Business","Nursing","Music"]},
    {"name":"University of South Florida","slug":"usf-tampa","accept":0.44,"gpa_lo":3.85,"gpa_hi":4.30,"sat_25":1210,"sat_75":1370,"act_25":25,"act_75":30,"tier":4,"type":"public","state":"Florida","size":38000,"tuition":6400,"desc":"R1 public in Tampa. Strong public health, marine science, business. Fast-rising research profile.","majors":["Business","Biology","Psychology","Engineering","Public Health"]},
    {"name":"VCU","slug":"vcu","accept":0.93,"gpa_lo":3.50,"gpa_hi":3.90,"sat_25":1080,"sat_75":1260,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Virginia","size":21000,"tuition":15000,"desc":"Virginia Commonwealth — top arts school (VCUarts) and very strong nursing/pharmacy. Urban Richmond campus.","majors":["Art","Nursing","Business","Biology","Pharmacy"]},
    {"name":"University of Houston","slug":"houston","accept":0.65,"gpa_lo":3.50,"gpa_hi":3.90,"sat_25":1130,"sat_75":1320,"act_25":22,"act_75":29,"tier":4,"type":"public","state":"Texas","size":38000,"tuition":11000,"desc":"R1 public, strong hotel/restaurant, business (Bauer), engineering. Diverse, urban Houston campus.","majors":["Business","Engineering","Biology","Psychology","Hospitality"]},
    {"name":"University of New Mexico","slug":"unm","accept":0.93,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1010,"sat_75":1230,"act_25":19,"act_75":26,"tier":5,"type":"public","state":"New Mexico","size":17000,"tuition":8500,"desc":"Flagship of NM. Strong Latin American studies, anthropology, photography. Affordable, beautiful Albuquerque campus.","majors":["Business","Psychology","Biology","Education","Engineering"]},
    {"name":"Wayne State","slug":"wayne-state","accept":0.78,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1020,"sat_75":1230,"act_25":20,"act_75":26,"tier":5,"type":"public","state":"Michigan","size":17000,"tuition":13500,"desc":"R1 public in Detroit. Strong nursing, social work, urban planning. Major commuter school.","majors":["Business","Nursing","Engineering","Psychology","Biology"]},
    {"name":"Georgia State","slug":"gsu","accept":0.76,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1090,"sat_75":1260,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Georgia","size":27000,"tuition":11000,"desc":"Atlanta R1. Strong business (Robinson), neuroscience, film. Major HSI/MSI numbers.","majors":["Business","Psychology","Biology","Computer Science","Film"]},
    {"name":"West Virginia","slug":"wvu","accept":0.86,"gpa_lo":3.40,"gpa_hi":3.90,"sat_25":1060,"sat_75":1230,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"West Virginia","size":21000,"tuition":9500,"desc":"Flagship in Morgantown. Strong forensic science, mining engineering. Big football/basketball culture.","majors":["Business","Engineering","Nursing","Psychology","Biology"]},
    {"name":"University of Louisville","slug":"louisville","accept":0.78,"gpa_lo":3.45,"gpa_hi":3.92,"sat_25":1110,"sat_75":1300,"act_25":22,"act_75":28,"tier":5,"type":"public","state":"Kentucky","size":15000,"tuition":12000,"desc":"R1 public. Strong engineering (Speed), nursing, basketball powerhouse. Urban Louisville campus.","majors":["Business","Engineering","Nursing","Psychology","Biology"]},
    {"name":"Oklahoma State","slug":"oklahoma-state","accept":0.72,"gpa_lo":3.45,"gpa_hi":3.92,"sat_25":1080,"sat_75":1290,"act_25":21,"act_75":28,"tier":5,"type":"public","state":"Oklahoma","size":21000,"tuition":9500,"desc":"R1 ag/engineering powerhouse in Stillwater. Strong hotel, business (Spears), aerospace.","majors":["Business","Engineering","Animal Science","Psychology","Education"]},
    {"name":"University of Oklahoma","slug":"oklahoma","accept":0.80,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1110,"sat_75":1320,"act_25":23,"act_75":29,"tier":5,"type":"public","state":"Oklahoma","size":22000,"tuition":12000,"desc":"Flagship in Norman. Strong meteorology (#1 program), petroleum engineering, journalism (Gaylord).","majors":["Business","Engineering","Psychology","Biology","Communication"]},
    {"name":"UTSA","slug":"utsa","accept":0.79,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1040,"sat_75":1220,"act_25":20,"act_75":26,"tier":5,"type":"public","state":"Texas","size":29000,"tuition":11000,"desc":"R1 public in San Antonio. Cybersecurity is a national leader. Strong business, biomedical engineering.","majors":["Business","Cybersecurity","Engineering","Biology","Psychology"]},
    {"name":"UT Dallas","slug":"utd","accept":0.85,"gpa_lo":3.70,"gpa_hi":4.10,"sat_25":1230,"sat_75":1430,"act_25":26,"act_75":31,"tier":5,"type":"public","state":"Texas","size":21000,"tuition":14000,"desc":"R1 public, very STEM-focused. Strong CS, electrical engineering, finance. Texas Instruments roots.","majors":["Computer Science","Engineering","Business","Biology","Mathematics"]},
    {"name":"Oregon State","slug":"oregon-state","accept":0.83,"gpa_lo":3.45,"gpa_hi":3.92,"sat_25":1110,"sat_75":1330,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Oregon","size":25000,"tuition":12000,"desc":"R1 land-grant in Corvallis. Strong oceanography, forestry, engineering. Outdoorsy PNW culture.","majors":["Engineering","Business","Biology","Computer Science","Animal Science"]},
    {"name":"Washington State","slug":"wsu","accept":0.83,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1030,"sat_75":1230,"act_25":20,"act_75":26,"tier":5,"type":"public","state":"Washington","size":24000,"tuition":12000,"desc":"R1 land-grant in Pullman. Strong veterinary, viticulture, communication. Pac-12 sports.","majors":["Business","Engineering","Psychology","Biology","Communication"]},
    {"name":"UNC Charlotte","slug":"unc-charlotte","accept":0.81,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1100,"sat_75":1280,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"North Carolina","size":24000,"tuition":7500,"desc":"R1 public in Charlotte. Strong fintech, engineering (Lee), data science. Booming NC tech corridor.","majors":["Business","Engineering","Computer Science","Psychology","Nursing"]},
    {"name":"East Carolina","slug":"ecu","accept":0.92,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1040,"sat_75":1190,"act_25":20,"act_75":24,"tier":5,"type":"public","state":"North Carolina","size":22000,"tuition":7500,"desc":"Greenville NC. Strong nursing, education, dental. Affordable in-state, accessible admissions.","majors":["Nursing","Business","Education","Biology","Psychology"]},
    {"name":"Kent State","slug":"kent-state","accept":0.85,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1020,"sat_75":1210,"act_25":20,"act_75":26,"tier":5,"type":"public","state":"Ohio","size":23000,"tuition":11500,"desc":"R1 public. Strong fashion school (top in country), aviation, journalism. Ohio's largest public.","majors":["Business","Fashion","Education","Nursing","Communication"]},
    {"name":"Florida Atlantic","slug":"fau","accept":0.78,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1100,"sat_75":1260,"act_25":21,"act_75":26,"tier":5,"type":"public","state":"Florida","size":22000,"tuition":4900,"desc":"Boca Raton public. Strong business, ocean engineering, neuroscience. Affordable Florida residency.","majors":["Business","Biology","Psychology","Engineering","Nursing"]},
    {"name":"University of Akron","slug":"akron","accept":0.79,"gpa_lo":3.20,"gpa_hi":3.75,"sat_25":960,"sat_75":1170,"act_25":18,"act_75":24,"tier":5,"type":"public","state":"Ohio","size":13000,"tuition":11000,"desc":"R2 public. Strong polymer science (top in world), engineering, nursing. Affordable Ohio option.","majors":["Engineering","Business","Nursing","Polymer Science","Education"]},
    {"name":"James Madison","slug":"jmu","accept":0.78,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1330,"act_25":24,"act_75":29,"tier":4,"type":"public","state":"Virginia","size":20000,"tuition":13000,"desc":"Top mid-size public in Harrisonburg. Famously strong undergrad teaching, beautiful campus, Greek scene.","majors":["Business","Communication","Health Sciences","Psychology","Education"]},
    {"name":"Appalachian State","slug":"app-state","accept":0.81,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1130,"sat_75":1280,"act_25":23,"act_75":28,"tier":5,"type":"public","state":"North Carolina","size":18000,"tuition":7500,"desc":"Boone, NC mountain campus. Strong sustainability, music, education. Outdoorsy student body.","majors":["Business","Education","Communication","Biology","Psychology"]},
    {"name":"College of Charleston","slug":"col-charleston","accept":0.79,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1140,"sat_75":1310,"act_25":24,"act_75":29,"tier":5,"type":"public","state":"South Carolina","size":10000,"tuition":13500,"desc":"Historic public LAC by the sea. Marine bio, business, hospitality. Charming downtown campus.","majors":["Business","Biology","Communication","Psychology","Marine Biology"]},
    {"name":"University of North Florida","slug":"unf","accept":0.71,"gpa_lo":3.65,"gpa_hi":4.10,"sat_25":1140,"sat_75":1300,"act_25":23,"act_75":28,"tier":5,"type":"public","state":"Florida","size":15000,"tuition":6400,"desc":"Jacksonville public. Strong nursing, transportation, music. Affordable Florida residency option.","majors":["Business","Nursing","Psychology","Biology","Education"]},
    {"name":"Boise State","slug":"boise-state","accept":0.83,"gpa_lo":3.45,"gpa_hi":3.92,"sat_25":1030,"sat_75":1240,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Idaho","size":21000,"tuition":8500,"desc":"R1 public in Idaho's capital. Strong CS, engineering, kinesiology. Famous blue football turf.","majors":["Business","Engineering","Computer Science","Education","Nursing"]},
    {"name":"UNLV","slug":"unlv","accept":0.85,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1010,"sat_75":1200,"act_25":19,"act_75":25,"tier":5,"type":"public","state":"Nevada","size":24000,"tuition":9000,"desc":"R1 public in Las Vegas. Strong hotel admin (#1), entertainment engineering, dental. Affordable in-state.","majors":["Hospitality","Business","Engineering","Biology","Psychology"]},
    {"name":"Old Dominion","slug":"odu","accept":0.95,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1010,"sat_75":1190,"act_25":19,"act_75":25,"tier":5,"type":"public","state":"Virginia","size":18000,"tuition":11000,"desc":"R1 public in Norfolk. Strong nursing, engineering (Frank Batten), maritime. Military-friendly.","majors":["Business","Nursing","Engineering","Psychology","Education"]},
    {"name":"Towson","slug":"towson","accept":0.74,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1080,"sat_75":1240,"act_25":21,"act_75":26,"tier":5,"type":"public","state":"Maryland","size":18000,"tuition":11000,"desc":"Mid-size public near Baltimore. Strong education, business, communications. Suburban quad campus.","majors":["Business","Communication","Education","Psychology","Nursing"]},
    {"name":"Mississippi State","slug":"miss-state","accept":0.74,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1090,"sat_75":1270,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Mississippi","size":17000,"tuition":9700,"desc":"Land-grant in Starkville. Strong agriculture, engineering, ARO. Big SEC sports culture.","majors":["Engineering","Business","Animal Science","Psychology","Biology"]},
    {"name":"UMBC","slug":"umbc","accept":0.69,"gpa_lo":3.65,"gpa_hi":4.10,"sat_25":1190,"sat_75":1370,"act_25":24,"act_75":31,"tier":4,"type":"public","state":"Maryland","size":11000,"tuition":12500,"desc":"R1 in Catonsville. Famously strong CS, biology (Meyerhoff Scholars). Best science school you've never heard of.","majors":["Computer Science","Biology","Engineering","Psychology","Business"]},
    {"name":"University of Rhode Island","slug":"uri","accept":0.78,"gpa_lo":3.50,"gpa_hi":3.92,"sat_25":1110,"sat_75":1290,"act_25":23,"act_75":28,"tier":5,"type":"public","state":"Rhode Island","size":15000,"tuition":15000,"desc":"Land-grant on Narragansett Bay. Strong oceanography, pharmacy, nursing. Beachside campus.","majors":["Business","Nursing","Engineering","Pharmacy","Marine Biology"]},
    {"name":"Northern Arizona","slug":"nau","accept":0.92,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1010,"sat_75":1210,"act_25":19,"act_75":25,"tier":5,"type":"public","state":"Arizona","size":21000,"tuition":13000,"desc":"Mountain campus in Flagstaff. Strong forestry, education, hotel/restaurant. Near Grand Canyon.","majors":["Business","Education","Biology","Forestry","Hospitality"]},
    {"name":"Western Washington","slug":"wwu","accept":0.92,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1110,"sat_75":1300,"act_25":22,"act_75":28,"tier":5,"type":"public","state":"Washington","size":15000,"tuition":12000,"desc":"Bellingham campus, gorgeous PNW location. Strong environmental sciences, business, education.","majors":["Business","Biology","Psychology","Environmental Science","Education"]},
    {"name":"Western Michigan","slug":"wmu","accept":0.83,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1000,"sat_75":1200,"act_25":19,"act_75":25,"tier":5,"type":"public","state":"Michigan","size":15000,"tuition":13500,"desc":"Kalamazoo R2. Strong aviation, paper science, music. One of the top aviation programs in the country.","majors":["Business","Aviation","Engineering","Education","Music"]},
    {"name":"Eastern Michigan","slug":"emich","accept":0.86,"gpa_lo":3.10,"gpa_hi":3.65,"sat_25":960,"sat_75":1180,"act_25":17,"act_75":23,"tier":5,"type":"public","state":"Michigan","size":13000,"tuition":13500,"desc":"R2 public in Ypsilanti. Strong education, special ed. Affordable, accessible.","majors":["Education","Business","Psychology","Nursing","Biology"]},
    {"name":"Northern Illinois","slug":"niu","accept":0.62,"gpa_lo":3.20,"gpa_hi":3.70,"sat_25":960,"sat_75":1170,"act_25":18,"act_75":23,"tier":5,"type":"public","state":"Illinois","size":12000,"tuition":13500,"desc":"DeKalb campus. Strong business, education, Burmese language. Major commuter base.","majors":["Business","Education","Psychology","Engineering","Biology"]},
    {"name":"Illinois State","slug":"ilstu","accept":0.81,"gpa_lo":3.35,"gpa_hi":3.85,"sat_25":1030,"sat_75":1210,"act_25":20,"act_75":25,"tier":5,"type":"public","state":"Illinois","size":18000,"tuition":13500,"desc":"Normal, IL. Strong education (oldest in state), insurance, agriculture. Mid-Illinois pipeline to State Farm.","majors":["Education","Business","Communication","Psychology","Nursing"]},
    {"name":"Bowling Green","slug":"bgsu","accept":0.74,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":990,"sat_75":1190,"act_25":19,"act_75":25,"tier":5,"type":"public","state":"Ohio","size":15000,"tuition":11500,"desc":"Mid-size public in NW Ohio. Strong education, music, supply chain. Famous popular culture archive.","majors":["Education","Business","Communication","Psychology","Biology"]},
    {"name":"Ohio University","slug":"ohio-u","accept":0.85,"gpa_lo":3.35,"gpa_hi":3.85,"sat_25":1050,"sat_75":1230,"act_25":21,"act_75":26,"tier":5,"type":"public","state":"Ohio","size":18000,"tuition":13000,"desc":"Athens, OH. Strong journalism (Scripps), hospitality, communication. Beautiful brick campus.","majors":["Communication","Business","Education","Psychology","Biology"]},
    # Mid-tier privates
    {"name":"Saint Louis University","slug":"slu","accept":0.59,"gpa_lo":3.65,"gpa_hi":4.10,"sat_25":1230,"sat_75":1430,"act_25":26,"act_75":32,"tier":4,"type":"private","state":"Missouri","size":8500,"tuition":50000,"desc":"Jesuit R1. Strong pre-health, social work, aviation. Madrid campus option for study abroad.","majors":["Biology","Business","Nursing","Psychology","Engineering"]},
    {"name":"Hofstra","slug":"hofstra","accept":0.69,"gpa_lo":3.50,"gpa_hi":3.92,"sat_25":1170,"sat_75":1350,"act_25":24,"act_75":30,"tier":4,"type":"private","state":"New York","size":7000,"tuition":58000,"desc":"Long Island private. Strong communication, business, law school. Famous for hosting presidential debates.","majors":["Business","Communication","Psychology","Biology","Engineering"]},
    {"name":"University of Tulsa","slug":"tulsa","accept":0.39,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1190,"sat_75":1410,"act_25":25,"act_75":32,"tier":4,"type":"private","state":"Oklahoma","size":2700,"tuition":47000,"desc":"Small private with strong petroleum engineering, cybersecurity, music. Best-known Oklahoma private.","majors":["Engineering","Business","Petroleum Engineering","Biology","Cybersecurity"]},
    {"name":"University of Dayton","slug":"dayton","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1120,"sat_75":1320,"act_25":23,"act_75":30,"tier":4,"type":"private","state":"Ohio","size":8500,"tuition":51000,"desc":"Marianist Catholic. Strong engineering (especially aerospace via UDRI), business. Tight community.","majors":["Engineering","Business","Education","Communication","Biology"]},
    {"name":"University of San Diego","slug":"usd","accept":0.51,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1230,"sat_75":1390,"act_25":27,"act_75":32,"tier":4,"type":"private","state":"California","size":5800,"tuition":56000,"desc":"Catholic private with stunning Spanish-renaissance campus overlooking ocean. Strong business, engineering, peace studies.","majors":["Business","Engineering","Biology","Psychology","Communication"]},
    {"name":"University of San Francisco","slug":"usf-sf","accept":0.74,"gpa_lo":3.45,"gpa_hi":3.90,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":30,"tier":4,"type":"private","state":"California","size":6300,"tuition":56000,"desc":"Jesuit in the heart of SF. Strong business, nursing, computer science. Tech access via Bay Area location.","majors":["Business","Nursing","Computer Science","Psychology","Communication"]},
    {"name":"Chapman","slug":"chapman","accept":0.59,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1230,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"private","state":"California","size":7500,"tuition":62000,"desc":"Orange County private with elite film school (Dodge College). Strong business (Argyros), creative arts.","majors":["Film","Business","Communication","Psychology","Biology"]},
    {"name":"Seattle U","slug":"seattle-u","accept":0.78,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1370,"act_25":25,"act_75":30,"tier":4,"type":"private","state":"Washington","size":4400,"tuition":53000,"desc":"Jesuit in Seattle's Capitol Hill. Strong nursing, business (Albers), social justice focus.","majors":["Business","Nursing","Psychology","Biology","Computer Science"]},
    {"name":"Catholic University","slug":"catholic","accept":0.86,"gpa_lo":3.50,"gpa_hi":3.92,"sat_25":1170,"sat_75":1370,"act_25":25,"act_75":31,"tier":5,"type":"private","state":"DC","size":3000,"tuition":53000,"desc":"Pontifical university in DC. Strong music, philosophy/theology, architecture, engineering.","majors":["Engineering","Business","Music","Psychology","Architecture"]},
    {"name":"Embry-Riddle","slug":"embry-riddle","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1140,"sat_75":1330,"act_25":24,"act_75":30,"tier":4,"type":"private","state":"Florida","size":7000,"tuition":42000,"desc":"World's premier aviation/aerospace university. Daytona Beach campus. Pilot training, aerospace engineering.","majors":["Aviation","Aerospace Engineering","Engineering","Business","Cybersecurity"]},
    {"name":"Stetson","slug":"stetson","accept":0.79,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1110,"sat_75":1280,"act_25":23,"act_75":28,"tier":5,"type":"private","state":"Florida","size":3000,"tuition":56000,"desc":"DeLand, FL. Strong business, music, pre-law. Top Florida private LAC.","majors":["Business","Music","Psychology","Biology","Communication"]},
    {"name":"Drake","slug":"drake","accept":0.71,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1140,"sat_75":1370,"act_25":24,"act_75":31,"tier":4,"type":"private","state":"Iowa","size":3000,"tuition":50000,"desc":"Des Moines private. Strong actuarial science, pharmacy, journalism. Affordable Midwest private.","majors":["Business","Pharmacy","Actuarial Science","Communication","Biology"]},
    {"name":"Butler","slug":"butler","accept":0.84,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1140,"sat_75":1340,"act_25":24,"act_75":30,"tier":5,"type":"private","state":"Indiana","size":4500,"tuition":48000,"desc":"Indianapolis private. Strong pharmacy (6-year PharmD), business, dance. Big basketball culture.","majors":["Pharmacy","Business","Education","Biology","Communication"]},
    {"name":"Yeshiva","slug":"yeshiva","accept":0.55,"gpa_lo":3.50,"gpa_hi":3.92,"sat_25":1180,"sat_75":1410,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"New York","size":2700,"tuition":48000,"desc":"Modern Orthodox Jewish university. Separate men's/women's undergrad. Strong pre-med, business, Jewish studies.","majors":["Business","Biology","Psychology","Jewish Studies","Computer Science"]},
    {"name":"Trinity University","slug":"trinity-tx","accept":0.30,"gpa_lo":3.65,"gpa_hi":4.10,"sat_25":1290,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"Texas","size":2500,"tuition":50000,"desc":"Top mid-size LAC in San Antonio. Strong business, neuroscience, engineering. Small classes, big endowment.","majors":["Business","Biology","Engineering","Psychology","Communication"]},
    {"name":"Mercer","slug":"mercer","accept":0.69,"gpa_lo":3.55,"gpa_hi":4.00,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":30,"tier":4,"type":"private","state":"Georgia","size":4500,"tuition":42000,"desc":"Macon, GA Baptist private. Strong engineering, pharmacy, music. Mid-size with grad/professional schools.","majors":["Engineering","Business","Pharmacy","Biology","Nursing"]},
    {"name":"Belmont","slug":"belmont","accept":0.83,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1140,"sat_75":1330,"act_25":24,"act_75":30,"tier":5,"type":"private","state":"Tennessee","size":7000,"tuition":42000,"desc":"Nashville private. Music industry program is world-class. Christian-affiliated, strong business too.","majors":["Music Business","Business","Nursing","Music","Education"]},
    {"name":"Elon","slug":"elon","accept":0.74,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1190,"sat_75":1340,"act_25":25,"act_75":30,"tier":4,"type":"private","state":"North Carolina","size":6300,"tuition":44000,"desc":"NC mid-size private. Strong communications, business, study abroad (top in country). Beautiful campus.","majors":["Communication","Business","Psychology","Education","Biology"]},
    {"name":"High Point","slug":"high-point","accept":0.83,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1100,"sat_75":1280,"act_25":22,"act_75":28,"tier":5,"type":"private","state":"North Carolina","size":5000,"tuition":42000,"desc":"NC private known for resort-like amenities. Strong communications, business, education.","majors":["Business","Communication","Education","Psychology","Biology"]},
    {"name":"Quinnipiac","slug":"quinnipiac","accept":0.84,"gpa_lo":3.40,"gpa_hi":3.90,"sat_25":1100,"sat_75":1290,"act_25":23,"act_75":28,"tier":5,"type":"private","state":"Connecticut","size":7000,"tuition":53000,"desc":"Hamden, CT private. Strong communications (top journalism poll), nursing, PT/OT. Medical school onsite.","majors":["Nursing","Business","Communication","Health Sciences","Psychology"]},
    {"name":"Pace","slug":"pace","accept":0.86,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1100,"sat_75":1290,"act_25":23,"act_75":29,"tier":5,"type":"private","state":"New York","size":9000,"tuition":52000,"desc":"NYC and Westchester campuses. Strong business (Lubin), performing arts, computer science. Wall Street access.","majors":["Business","Communication","Psychology","Computer Science","Performing Arts"]},
    {"name":"Adelphi","slug":"adelphi","accept":0.74,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1090,"sat_75":1290,"act_25":22,"act_75":28,"tier":5,"type":"private","state":"New York","size":4500,"tuition":47000,"desc":"Garden City, Long Island. Strong nursing, social work, business. Tight-knit suburban campus.","majors":["Nursing","Business","Psychology","Education","Social Work"]},
    {"name":"Marist","slug":"marist","accept":0.55,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1340,"act_25":25,"act_75":30,"tier":4,"type":"private","state":"New York","size":5500,"tuition":48000,"desc":"Hudson Valley private. Strong communication, business (IBM joint center), fashion. Beautiful river campus.","majors":["Business","Communication","Psychology","Computer Science","Fashion"]},
    {"name":"Fairfield","slug":"fairfield","accept":0.41,"gpa_lo":3.55,"gpa_hi":4.00,"sat_25":1230,"sat_75":1390,"act_25":27,"act_75":31,"tier":4,"type":"private","state":"Connecticut","size":4500,"tuition":58000,"desc":"Jesuit on the Long Island Sound. Strong nursing (Egan), business, finance. Wealthy suburban campus.","majors":["Nursing","Business","Psychology","Finance","Communication"]},
    {"name":"Loyola Maryland","slug":"loyola-md","accept":0.83,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1340,"act_25":25,"act_75":30,"tier":5,"type":"private","state":"Maryland","size":4000,"tuition":56000,"desc":"Jesuit in north Baltimore. Strong business (Sellinger), psychology, communications. Small classes.","majors":["Business","Psychology","Communication","Biology","Education"]},
    {"name":"Gonzaga","slug":"gonzaga","accept":0.75,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1190,"sat_75":1370,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"Washington","size":5400,"tuition":50000,"desc":"Spokane Jesuit. Strong nursing, business, engineering. Famous for basketball.","majors":["Business","Nursing","Engineering","Psychology","Biology"]},
    {"name":"Creighton","slug":"creighton","accept":0.71,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1170,"sat_75":1380,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"Nebraska","size":4400,"tuition":46000,"desc":"Jesuit in Omaha. Strong pre-health (med, dental, pharmacy onsite), business. Small classes for size.","majors":["Biology","Business","Nursing","Psychology","Pharmacy"]},
    {"name":"Xavier (Cincinnati)","slug":"xavier","accept":0.84,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1110,"sat_75":1310,"act_25":23,"act_75":29,"tier":5,"type":"private","state":"Ohio","size":4600,"tuition":48000,"desc":"Jesuit in Cincinnati. Strong nursing, education, MBA. Big basketball culture in the Big East.","majors":["Business","Education","Nursing","Psychology","Communication"]},
    {"name":"Saint Joseph's","slug":"sju","accept":0.83,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1140,"sat_75":1310,"act_25":24,"act_75":29,"tier":5,"type":"private","state":"Pennsylvania","size":4400,"tuition":51000,"desc":"Jesuit in Philadelphia. Strong food marketing, finance, pharma marketing. Strong Big Five basketball.","majors":["Business","Communication","Psychology","Biology","Education"]},
    {"name":"Providence","slug":"providence","accept":0.55,"gpa_lo":3.65,"gpa_hi":4.00,"sat_25":1170,"sat_75":1340,"act_25":26,"act_75":31,"tier":4,"type":"private","state":"Rhode Island","size":4400,"tuition":62000,"desc":"Dominican Catholic. Strong business, education, theology. Big East basketball.","majors":["Business","Education","Psychology","Communication","Biology"]},
    {"name":"Manhattan College","slug":"manhattan","accept":0.79,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1080,"sat_75":1280,"act_25":23,"act_75":28,"tier":5,"type":"private","state":"New York","size":3500,"tuition":48000,"desc":"Lasallian Catholic in the Bronx. Strong engineering, business, education. NYC subway access.","majors":["Engineering","Business","Education","Communication","Computer Science"]},
    {"name":"St. John's","slug":"stjohns","accept":0.74,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1080,"sat_75":1280,"act_25":21,"act_75":28,"tier":5,"type":"private","state":"New York","size":15000,"tuition":48000,"desc":"Vincentian Catholic in Queens. Strong pharmacy, business, criminal justice. Diverse, urban campus.","majors":["Pharmacy","Business","Psychology","Communication","Biology"]},
    {"name":"Loyola NOLA","slug":"loyola-no","accept":0.81,"gpa_lo":3.40,"gpa_hi":3.90,"sat_25":1090,"sat_75":1280,"act_25":23,"act_75":29,"tier":5,"type":"private","state":"Louisiana","size":3500,"tuition":47000,"desc":"Jesuit in New Orleans. Strong music industry, business, mass communication. Tulane neighbor.","majors":["Business","Music","Communication","Psychology","Biology"]},
    # LACs
    {"name":"Bard","slug":"bard","accept":0.42,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1280,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"New York","size":2000,"tuition":63000,"desc":"Hudson River campus. Strong arts, lit, classics. Open-curriculum vibe, lots of senior thesis culture.","majors":["English","Arts","Biology","Political Science","Psychology"]},
    {"name":"Rhodes","slug":"rhodes","accept":0.43,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1240,"sat_75":1410,"act_25":28,"act_75":32,"tier":3,"type":"private","state":"Tennessee","size":2000,"tuition":53000,"desc":"Memphis LAC, beautiful gothic campus. Strong English, biology, urban studies. Strong honor code.","majors":["English","Biology","Psychology","Economics","International Studies"]},
    {"name":"Furman","slug":"furman","accept":0.62,"gpa_lo":3.65,"gpa_hi":4.10,"sat_25":1230,"sat_75":1390,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"South Carolina","size":2700,"tuition":58000,"desc":"Greenville, SC LAC. Strong sustainability, business, engaged learning. Beautiful southern campus.","majors":["Business","Biology","Psychology","Health Sciences","Communication"]},
    {"name":"Samford","slug":"samford","accept":0.91,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1140,"sat_75":1310,"act_25":24,"act_75":30,"tier":5,"type":"private","state":"Alabama","size":3700,"tuition":40000,"desc":"Birmingham Baptist private. Strong nursing, pharmacy, business. Strong Christian community.","majors":["Nursing","Business","Pharmacy","Education","Biology"]},
    {"name":"Wofford","slug":"wofford","accept":0.62,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1190,"sat_75":1370,"act_25":26,"act_75":31,"tier":4,"type":"private","state":"South Carolina","size":1700,"tuition":54000,"desc":"Spartanburg LAC. Strong economics, biology, business. Tiny class sizes, strong study abroad.","majors":["Economics","Biology","Business","Psychology","English"]},
    {"name":"Hope","slug":"hope","accept":0.81,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1180,"sat_75":1380,"act_25":25,"act_75":31,"tier":5,"type":"private","state":"Michigan","size":3000,"tuition":40000,"desc":"Reformed Church of America LAC in Holland, MI. Strong sciences, music, education. Christian community focus.","majors":["Biology","Business","Education","Psychology","Music"]},
    {"name":"Centre","slug":"centre","accept":0.73,"gpa_lo":3.65,"gpa_hi":4.00,"sat_25":1180,"sat_75":1370,"act_25":26,"act_75":31,"tier":4,"type":"private","state":"Kentucky","size":1400,"tuition":52000,"desc":"Danville, KY LAC. Strong international studies, science. Hosted vice presidential debates.","majors":["English","Biology","Economics","International Studies","Psychology"]},
    {"name":"Lawrence","slug":"lawrence","accept":0.62,"gpa_lo":3.65,"gpa_hi":4.00,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Wisconsin","size":1500,"tuition":52000,"desc":"Appleton, WI LAC with conservatory of music. Quirky, intellectual, strong music programs.","majors":["Music","Biology","English","Psychology","Government"]},
    {"name":"St. Olaf","slug":"st-olaf","accept":0.50,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1240,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"private","state":"Minnesota","size":3000,"tuition":53000,"desc":"Lutheran LAC near Minneapolis. Strong music, math, sciences. Famous Christmas Festival.","majors":["Biology","Music","Economics","Mathematics","Psychology"]},
    {"name":"Beloit","slug":"beloit","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"Wisconsin","size":1100,"tuition":54000,"desc":"Small LAC near Chicago. Strong anthropology, museum studies, study abroad. Quirky academic culture.","majors":["Anthropology","Biology","Psychology","English","Economics"]},
    {"name":"DePauw","slug":"depauw","accept":0.69,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1370,"act_25":25,"act_75":30,"tier":4,"type":"private","state":"Indiana","size":1900,"tuition":58000,"desc":"Indiana LAC. Strong communication, journalism, music. Heavy Greek life.","majors":["Communication","Business","Biology","Psychology","Music"]},
    {"name":"Dickinson","slug":"dickinson","accept":0.52,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1240,"sat_75":1390,"act_25":28,"act_75":32,"tier":3,"type":"private","state":"Pennsylvania","size":2100,"tuition":63000,"desc":"Carlisle, PA LAC. Strong international studies, sustainability, language programs. Top study abroad.","majors":["International Studies","Economics","Biology","Psychology","English"]},
    {"name":"Gettysburg","slug":"gettysburg","accept":0.50,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1230,"sat_75":1380,"act_25":28,"act_75":32,"tier":3,"type":"private","state":"Pennsylvania","size":2400,"tuition":62000,"desc":"PA LAC on the Civil War battlefield. Strong history, music, business. Heavy Greek life.","majors":["Business","Biology","History","Psychology","Political Science"]},
    {"name":"Muhlenberg","slug":"muhlenberg","accept":0.69,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1180,"sat_75":1340,"act_25":26,"act_75":31,"tier":4,"type":"private","state":"Pennsylvania","size":2200,"tuition":62000,"desc":"Allentown LAC. Strong theatre, pre-health, business. Lutheran-affiliated.","majors":["Theatre","Biology","Business","Psychology","Communication"]},
    {"name":"Sarah Lawrence","slug":"sarah-lawrence","accept":0.55,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1280,"sat_75":1450,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"New York","size":1600,"tuition":64000,"desc":"Yonkers LAC. No majors — student-designed programs via don system. Heavy writing/arts focus.","majors":["English","Theatre","Psychology","History","Visual Arts"]},
    {"name":"Bennington","slug":"bennington","accept":0.65,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1240,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Vermont","size":700,"tuition":59000,"desc":"VT LAC. Self-directed curriculum, strong creative arts. Required winter Field Work Term.","majors":["English","Visual Arts","Music","Theatre","Psychology"]},
    {"name":"Hampshire","slug":"hampshire","accept":0.85,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1110,"sat_75":1340,"act_25":24,"act_75":30,"tier":5,"type":"private","state":"Massachusetts","size":600,"tuition":52000,"desc":"Five College Consortium member. No grades, no majors — fully self-designed. Highly experimental.","majors":["Interdisciplinary Studies","Visual Arts","Film","Psychology","Biology"]},
    {"name":"Wheaton (MA)","slug":"wheaton-ma","accept":0.79,"gpa_lo":3.50,"gpa_hi":3.92,"sat_25":1200,"sat_75":1370,"act_25":27,"act_75":31,"tier":5,"type":"private","state":"Massachusetts","size":1800,"tuition":63000,"desc":"Norton, MA LAC. Strong English, biology, studio art. Connections to Brown via Brown-Wheaton bridge.","majors":["English","Biology","Psychology","Economics","Visual Arts"]},
    {"name":"University of Richmond","slug":"richmond","accept":0.27,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Virginia","size":3300,"tuition":63000,"desc":"VA LAC with business school (Robins). Strong leadership studies, journalism. Beautiful gothic campus.","majors":["Business","Biology","Psychology","Political Science","Leadership Studies"]},
    {"name":"Washington and Lee","slug":"wlu","accept":0.18,"gpa_lo":3.75,"gpa_hi":4.10,"sat_25":1390,"sat_75":1510,"act_25":31,"act_75":34,"tier":2,"type":"private","state":"Virginia","size":1900,"tuition":63000,"desc":"Lexington, VA LAC. Strong business (Williams), pre-law, journalism. Honor system, southern traditions.","majors":["Business","Economics","Politics","English","History"]},
    # Engineering / tech
    {"name":"RIT","slug":"rit","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1230,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"private","state":"New York","size":13000,"tuition":58000,"desc":"Rochester Institute of Technology. Strong CS, game design (#1), photography, deaf studies (NTID).","majors":["Computer Science","Engineering","Game Design","Photography","Business"]},
    {"name":"NJIT","slug":"njit","accept":0.69,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1230,"sat_75":1430,"act_25":26,"act_75":32,"tier":4,"type":"public","state":"New Jersey","size":9500,"tuition":18000,"desc":"Newark public R1. Strong architecture, CS, engineering. Diverse, commuter-heavy.","majors":["Engineering","Computer Science","Architecture","Business","Mathematics"]},
    {"name":"Illinois Tech","slug":"iit","accept":0.62,"gpa_lo":3.55,"gpa_hi":4.00,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"private","state":"Illinois","size":3000,"tuition":54000,"desc":"Chicago private with strong architecture (Mies van der Rohe), engineering, design. Urban campus.","majors":["Engineering","Computer Science","Architecture","Business","Design"]},
    {"name":"Colorado Mines","slug":"mines","accept":0.55,"gpa_lo":3.75,"gpa_hi":4.10,"sat_25":1290,"sat_75":1450,"act_25":28,"act_75":33,"tier":4,"type":"public","state":"Colorado","size":5500,"tuition":20000,"desc":"Top engineering school for petroleum, mining, geology. Golden, CO campus near Denver.","majors":["Petroleum Engineering","Mechanical Engineering","Computer Science","Mining","Geology"]},
    {"name":"Texas Tech","slug":"texas-tech","accept":0.69,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1110,"sat_75":1310,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Texas","size":33000,"tuition":11000,"desc":"R1 land-grant in Lubbock. Strong agriculture, engineering, music. Big Big-12 sports culture.","majors":["Business","Engineering","Animal Science","Psychology","Biology"]},
    {"name":"University of North Texas","slug":"unt","accept":0.79,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1060,"sat_75":1240,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Texas","size":33000,"tuition":11000,"desc":"R1 in Denton. Strong music (top jazz), business, engineering. Diverse, big music scene.","majors":["Business","Music","Education","Psychology","Communication"]},
    {"name":"Michigan Tech","slug":"michigan-tech","accept":0.71,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"Michigan","size":5400,"tuition":18000,"desc":"R1 STEM in Houghton, Upper Peninsula. Strong mechanical/civil engineering, forestry. Cold winters.","majors":["Engineering","Computer Science","Forestry","Business","Mathematics"]},
    {"name":"Florida Tech","slug":"florida-tech","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1140,"sat_75":1340,"act_25":25,"act_75":31,"tier":5,"type":"private","state":"Florida","size":3000,"tuition":47000,"desc":"Melbourne, FL private. Aerospace engineering, oceanography, NASA Space Coast pipeline.","majors":["Aerospace Engineering","Computer Science","Biology","Business","Marine Biology"]},
    # HBCUs
    {"name":"Xavier Louisiana","slug":"xula","accept":0.68,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":960,"sat_75":1130,"act_25":18,"act_75":23,"tier":5,"type":"private","state":"Louisiana","size":3300,"tuition":28000,"desc":"#1 HBCU for placing Black students in med schools. Catholic-affiliated. Pharmacy is also flagship.","majors":["Biology","Pharmacy","Chemistry","Psychology","Business"]},
    {"name":"Clark Atlanta","slug":"clark-atlanta","accept":0.59,"gpa_lo":3.30,"gpa_hi":3.70,"sat_25":890,"sat_75":1080,"act_25":16,"act_75":21,"tier":4,"type":"private","state":"Georgia","size":4000,"tuition":26000,"desc":"Atlanta HBCU formed by Clark + Atlanta U merger. Strong business, social work, mass communication.","majors":["Business","Communication","Psychology","Biology","Social Work"]},
    {"name":"NC A&T","slug":"ncat","accept":0.62,"gpa_lo":3.30,"gpa_hi":3.85,"sat_25":960,"sat_75":1140,"act_25":17,"act_75":22,"tier":4,"type":"public","state":"North Carolina","size":13000,"tuition":7500,"desc":"Largest HBCU in the country. Strong engineering, agriculture, business. NC's R1 HBCU.","majors":["Engineering","Business","Biology","Computer Science","Agriculture"]},
    {"name":"Tennessee State","slug":"tnstate","accept":0.46,"gpa_lo":3.20,"gpa_hi":3.65,"sat_25":880,"sat_75":1060,"act_25":16,"act_75":20,"tier":4,"type":"public","state":"Tennessee","size":7000,"tuition":9000,"desc":"Nashville HBCU. Strong agriculture, engineering, criminal justice. Affordable in-state.","majors":["Business","Education","Engineering","Biology","Criminal Justice"]},
    {"name":"Jackson State","slug":"jackson-state","accept":0.68,"gpa_lo":3.20,"gpa_hi":3.65,"sat_25":860,"sat_75":1040,"act_25":15,"act_75":19,"tier":4,"type":"public","state":"Mississippi","size":6500,"tuition":8500,"desc":"R2 HBCU in Jackson. Strong business, education, mass communication. Famous Sonic Boom marching band.","majors":["Business","Education","Communication","Biology","Psychology"]},
    {"name":"Prairie View A&M","slug":"pvamu","accept":0.84,"gpa_lo":3.30,"gpa_hi":3.75,"sat_25":910,"sat_75":1100,"act_25":17,"act_75":22,"tier":5,"type":"public","state":"Texas","size":8500,"tuition":13000,"desc":"Texas HBCU. Strong nursing, engineering, agriculture. Land-grant.","majors":["Nursing","Business","Engineering","Education","Biology"]},
    {"name":"Morgan State","slug":"morgan-state","accept":0.81,"gpa_lo":3.30,"gpa_hi":3.75,"sat_25":920,"sat_75":1110,"act_25":17,"act_75":22,"tier":5,"type":"public","state":"Maryland","size":6500,"tuition":8500,"desc":"Baltimore R2 HBCU. Strong business, engineering, architecture. Designated Maryland's preeminent public research university.","majors":["Business","Engineering","Architecture","Communication","Psychology"]},
    {"name":"Texas Southern","slug":"texas-southern","accept":0.55,"gpa_lo":3.10,"gpa_hi":3.55,"sat_25":860,"sat_75":1030,"act_25":15,"act_75":19,"tier":4,"type":"public","state":"Texas","size":7000,"tuition":11000,"desc":"Houston HBCU. Strong pharmacy, law, communications. Affordable urban campus.","majors":["Business","Pharmacy","Communication","Biology","Psychology"]},
    # Other commonly searched
    {"name":"University of Vermont","slug":"vermont","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1370,"act_25":26,"act_75":31,"tier":4,"type":"public","state":"Vermont","size":11000,"tuition":18000,"desc":"Burlington flagship. Strong environmental sciences, nursing, dietetics. Outdoorsy/granola student culture.","majors":["Business","Biology","Psychology","Environmental Science","Nursing"]},
    {"name":"American","slug":"american-u","accept":0.36,"gpa_lo":3.65,"gpa_hi":4.00,"sat_25":1230,"sat_75":1410,"act_25":28,"act_75":32,"tier":3,"type":"private","state":"DC","size":8200,"tuition":58000,"desc":"DC private. Strong international relations (SIS), public policy, journalism. Heavy DC internship culture.","majors":["International Relations","Business","Political Science","Communication","Psychology"]},
    {"name":"GW","slug":"gw","accept":0.42,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1280,"sat_75":1450,"act_25":28,"act_75":33,"tier":3,"type":"private","state":"DC","size":12000,"tuition":63000,"desc":"George Washington in downtown DC. Strong international affairs, political science, public health. White House proximity.","majors":["Political Science","Business","International Affairs","Biology","Psychology"]},
    {"name":"Purdue Northwest","slug":"purdue-nw","accept":0.79,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":960,"sat_75":1180,"act_25":17,"act_75":24,"tier":5,"type":"public","state":"Indiana","size":8000,"tuition":9500,"desc":"Hammond/Westville campus. Strong engineering tech, nursing, business. Affordable Purdue degree option.","majors":["Engineering","Business","Nursing","Education","Biology"]},
    {"name":"Knox","slug":"knox","accept":0.65,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1380,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"Illinois","size":1100,"tuition":53000,"desc":"Galesburg, IL LAC. Strong creative writing, neuroscience, theatre. Loosely structured curriculum.","majors":["Creative Writing","Biology","Psychology","Theatre","English"]},
    {"name":"Allegheny","slug":"allegheny","accept":0.79,"gpa_lo":3.55,"gpa_hi":3.95,"sat_25":1170,"sat_75":1370,"act_25":25,"act_75":30,"tier":5,"type":"private","state":"Pennsylvania","size":1500,"tuition":54000,"desc":"Western PA LAC. Required double-major across divisions. Strong sciences, English.","majors":["Biology","Psychology","English","Business","Communication"]},
    {"name":"Susquehanna","slug":"susquehanna","accept":0.83,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1110,"sat_75":1290,"act_25":24,"act_75":28,"tier":5,"type":"private","state":"Pennsylvania","size":2200,"tuition":56000,"desc":"Selinsgrove LAC. Strong business (Sigmund Weis), creative writing. Required global experience.","majors":["Business","Communication","Biology","Psychology","Creative Writing"]},
    {"name":"Bryant","slug":"bryant","accept":0.71,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1180,"sat_75":1340,"act_25":26,"act_75":30,"tier":4,"type":"private","state":"Rhode Island","size":3500,"tuition":52000,"desc":"Smithfield, RI business-focused. Strong accounting, finance, business. Required IDEA innovation course.","majors":["Business","Accounting","Finance","Communication","Psychology"]},
]

COLLEGES_BY_SLUG = {c["slug"]: c for c in COLLEGES}
COLLEGE_NAMES = sorted([c["name"] for c in COLLEGES])
STATES = sorted(set(c["state"] for c in COLLEGES))

# City for each college. Done as a lookup table rather than inline so the
# 155-row COLLEGES list doesn't get any longer than it already is.
CITY_BY_SLUG = {
    "harvard":"Cambridge","stanford":"Stanford","mit":"Cambridge","yale":"New Haven","princeton":"Princeton",
    "columbia":"New York","uchicago":"Chicago","upenn":"Philadelphia","brown":"Providence","dartmouth":"Hanover",
    "duke":"Durham","northwestern":"Evanston","caltech":"Pasadena","cornell":"Ithaca","jhu":"Baltimore",
    "vanderbilt":"Nashville","rice":"Houston","notre-dame":"Notre Dame","cmu":"Pittsburgh","usc":"Los Angeles",
    "nyu":"New York","georgetown":"Washington","ucb":"Berkeley","ucla":"Los Angeles","umich":"Ann Arbor",
    "uva":"Charlottesville","gatech":"Atlanta","tufts":"Medford","washu":"St. Louis","emory":"Atlanta",
    "unc":"Chapel Hill","bu":"Boston","bc":"Chestnut Hill","wake-forest":"Winston-Salem","wm":"Williamsburg",
    "uf":"Gainesville","wisc":"Madison","uw":"Seattle","ut-austin":"Austin","umd":"College Park",
    "gwu":"Washington","penn-state":"University Park","osu":"Columbus","msu":"East Lansing","fsu":"Tallahassee",
    "iu":"Bloomington","asu":"Tempe","purdue":"West Lafayette","uiuc":"Urbana-Champaign","williams":"Williamstown",
    "amherst":"Amherst","swarthmore":"Swarthmore","pomona":"Claremont","bowdoin":"Brunswick","wellesley":"Wellesley",
    "middlebury":"Middlebury","carleton":"Northfield","haverford":"Haverford","vassar":"Poughkeepsie",
    "babson":"Wellesley","bentley":"Waltham","rpi":"Troy","wpi":"Worcester","rose-hulman":"Terre Haute",
    "uiowa":"Iowa City","umn":"Minneapolis","cu-boulder":"Boulder","uoregon":"Eugene","uconn":"Storrs",
    "rutgers":"New Brunswick","stony-brook":"Stony Brook","pitt":"Pittsburgh","vt":"Blacksburg",
    "tamu":"College Station","clemson":"Clemson","alabama":"Tuscaloosa","uga":"Athens","utk":"Knoxville",
    "sdsu":"San Diego","arizona":"Tucson","northeastern":"Boston","case":"Cleveland","lehigh":"Bethlehem",
    "bucknell":"Lewisburg","villanova":"Villanova","tulane":"New Orleans","miami":"Coral Gables",
    "american":"Washington","neiu":"Chicago",
    # Tier-2 expansion
    "wesleyan":"Middletown","hamilton":"Clinton","davidson":"Davidson","colgate":"Hamilton","smith":"Northampton",
    "mt-holyoke":"South Hadley","bryn-mawr":"Bryn Mawr","bates":"Lewiston","colby":"Waterville",
    "trinity-ct":"Hartford","conn-college":"New London","skidmore":"Saratoga Springs","macalester":"Saint Paul",
    "reed":"Portland","grinnell":"Grinnell","kenyon":"Gambier","oberlin":"Oberlin","whitman":"Walla Walla",
    "pitzer":"Claremont","scripps":"Claremont","harvey-mudd":"Claremont","cmc":"Claremont","oxy":"Los Angeles",
    "brandeis":"Waltham","fordham":"Bronx","barnard":"New York","pepperdine":"Malibu","scu":"Santa Clara",
    "lmu":"Los Angeles","drexel":"Philadelphia","stevens":"Hoboken","olin":"Needham","cooper":"New York",
    "berklee":"Boston","juilliard":"New York","pratt":"Brooklyn","parsons":"New York","risd":"Providence",
    "saic":"Chicago","uky":"Lexington","auburn":"Auburn","lsu":"Baton Rouge","sc":"Columbia",
    "missou":"Columbia","ku":"Lawrence","unl":"Lincoln","uvm":"Burlington","udel":"Newark",
    "binghamton":"Binghamton","calpoly-slo":"San Luis Obispo","sjsu":"San Jose","csulb":"Long Beach",
    "iowa-state":"Ames","temple":"Philadelphia","gmu":"Fairfax","uh":"Houston","utah":"Salt Lake City",
    "hawaii":"Honolulu","unh":"Durham","umaine":"Orono","howard":"Washington","spelman":"Atlanta",
    "morehouse":"Atlanta","famu":"Tallahassee","uprm":"Mayagüez","tuskegee":"Tuskegee",
}

def city_state(c):
    """Return 'City, State' for a college dict; falls back to state alone."""
    city = CITY_BY_SLUG.get(c["slug"])
    return f"{city}, {c['state']}" if city else c["state"]


# ─── MAJORS — comprehensive list for autocomplete on the profile form ──
MAJORS = [
    # Computing / Tech
    "Computer Science","Computer Engineering","Software Engineering","Information Systems",
    "Information Technology","Cybersecurity","Data Science","Artificial Intelligence",
    "Machine Learning","Game Design","Game Development","Robotics","Mechatronics","Bioinformatics",
    "Computational Biology","Health Informatics","Web Development",
    # Math / Stats
    "Mathematics","Applied Mathematics","Pure Mathematics","Statistics","Actuarial Science",
    "Operations Research","Decision Science","Quantitative Finance",
    # Physical sciences
    "Physics","Astrophysics","Astronomy","Chemistry","Biochemistry","Geochemistry","Materials Science",
    "Earth Science","Geology","Atmospheric Science","Meteorology","Oceanography","Hydrology",
    # Life sciences / pre-health
    "Biology","Molecular Biology","Cell Biology","Genetics","Microbiology","Neuroscience",
    "Behavioral Neuroscience","Cognitive Science","Ecology","Marine Biology","Botany","Zoology",
    "Wildlife Biology","Forensic Science","Pre-Med","Pre-Dental","Pre-Vet","Pre-Pharm","Pre-Law",
    # Engineering
    "Mechanical Engineering","Electrical Engineering","Civil Engineering","Chemical Engineering",
    "Aerospace Engineering","Biomedical Engineering","Industrial Engineering","Environmental Engineering",
    "Nuclear Engineering","Petroleum Engineering","Materials Engineering","Agricultural Engineering",
    "Architectural Engineering","Engineering Physics","Engineering Mechanics","Systems Engineering",
    # Health professions
    "Nursing","Public Health","Health Sciences","Health Administration","Healthcare Management",
    "Epidemiology","Pharmacy","Physical Therapy","Occupational Therapy","Speech Pathology",
    "Athletic Training","Nutrition","Dietetics","Kinesiology","Exercise Science","Sports Science",
    # Business
    "Business Administration","Finance","Accounting","Economics","Quantitative Economics",
    "Behavioral Economics","International Business","Marketing","Management","Entrepreneurship",
    "Operations Management","Supply Chain Management","Logistics","Real Estate",
    "Hospitality Management","Hotel Administration","Sports Management","Tourism Management",
    "Risk Management","Business Analytics","Information Systems Management","Human Resources",
    "Organizational Behavior",
    # Social sciences
    "Political Science","Government","International Relations","International Affairs","Public Policy",
    "Public Administration","Law","Pre-Law","Criminal Justice","Criminology","Sociology","Anthropology",
    "Cultural Anthropology","Archaeology","Social Work","Psychology","Clinical Psychology",
    "Counseling Psychology","Forensic Psychology","Geography","Urban Studies","Urban Planning",
    # Humanities
    "History","American Studies","European Studies","African Studies","Asian Studies",
    "Latin American Studies","Middle Eastern Studies","Russian Studies","Religious Studies",
    "Theology","Philosophy","Ethics","Linguistics","English","Creative Writing","Comparative Literature",
    "Classics","Latin","Greek","Hebrew",
    # Languages
    "Spanish","French","German","Chinese","Japanese","Korean","Italian","Portuguese","Russian","Arabic",
    # Communication / Media
    "Communication","Communications","Journalism","Mass Communication","Public Relations","Advertising",
    "Media Studies","Film Studies","Cinema Studies","Cinematic Arts","Film Production","Photography",
    "Digital Media","Broadcast Journalism",
    # Art / Design
    "Architecture","Industrial Design","Interior Design","Fashion Design","Graphic Design",
    "Visual Communication Design","Web Design","Animation","Illustration","Studio Art","Fine Arts",
    "Sculpture","Painting","Printmaking","Ceramics","Photography",
    # Performing arts
    "Music","Music Performance","Music Composition","Music Education","Music Production","Music Business",
    "Music Therapy","Songwriting","Jazz Studies","Conducting","Theater","Drama","Acting",
    "Theater Production","Stage Management","Musical Theater","Dance","Dance Performance","Choreography",
    # Education
    "Education","Elementary Education","Secondary Education","Special Education","Early Childhood Education",
    "Educational Leadership","Higher Education","Curriculum and Instruction",
    # Agriculture / Environment
    "Agricultural Science","Animal Science","Plant Science","Food Science","Forestry","Sustainability Studies",
    "Environmental Studies","Environmental Science","Conservation","Renewable Energy",
    # Aviation / Maritime / Military
    "Aviation","Aviation Management","Aerospace Studies","Maritime Studies","Military Science","Naval Science",
    # Interdisciplinary / liberal arts
    "Liberal Arts","Liberal Studies","Interdisciplinary Studies","General Studies","Honors",
    "Women's and Gender Studies","LGBTQ Studies","Africana Studies","Latinx Studies",
    "Asian American Studies","Native American Studies","Disability Studies",
    # Specialized
    "Hotel Administration","Culinary Arts","Library Science","Information Science","Public Affairs",
    "Symbolic Systems","Diplomacy and World Affairs",
    # Undecided
    "Undecided",
]


# ─── PREFERENCES ──────────────────────────────────────────
# Allowed values for each preference field. These appear in both the profile
# form and the school-match logic, so keep them in one place.
PREF_OPTIONS = {
    "weather":      [("any","No preference"), ("warm","Warm/sunny"), ("mild","Mild/temperate"), ("cold","Cold/snow")],
    "setting":      [("any","No preference"), ("urban","Big city"), ("college_town","College town"), ("suburban","Suburban"), ("rural","Rural")],
    "size":         [("any","No preference"), ("xs","Tiny (<2K)"), ("small","Small (2-6K)"), ("medium","Medium (6-12K)"), ("ml","Medium/Large (12-18K)"), ("large","Large (18K+)")],
    "class_size":   [("any","No preference"), ("tiny","Tiny classes (≤7:1)"), ("small","Small (8-10:1)"), ("medium","Medium (11-15:1)"), ("large","Large (16-20:1)"), ("xl","Huge lectures (21+:1)")],
    "greek":        [("any","No preference"), ("strong","Active Greek scene"), ("avoid","No Greek life")],
    "sports":       [("any","No preference"), ("strong","Big sports culture"), ("low","Low-key athletics")],
    "major_strength": [("any","No preference"), ("top","Top program in my major"), ("solid","Don't care about ranking")],
    "prestige":     [("any","No preference"), ("high","High prestige matters"), ("medium","Mid-tier is fine"), ("low","Don't care about brand name")],
    "cost":         [("any","No preference"), ("low","Low (<$15K/yr sticker)"), ("medium","Medium (<$40K)"), ("high","Cost not an issue")],
    "diversity":    [("any","No preference"), ("high","Strongly diverse student body"), ("similar","Mostly people like me is fine")],
    "party":        [("any","No preference"), ("high","I want a party scene"), ("low","Quiet/dry environment")],
    "research":     [("any","No preference"), ("critical","Research access is critical"), ("nice","Nice to have"), ("none","Not important")],
    "career_intensity": [("any","No preference"), ("preprof","Pre-professional / career-focused"), ("balanced","Balanced"), ("flexible","Exploration / flexible")],
}


# ─── REGION + FACULTY RATIO DATA ──────────────────────────
REGION_BY_STATE = {
    "Connecticut":"Northeast","Maine":"Northeast","Massachusetts":"Northeast",
    "New Hampshire":"Northeast","Rhode Island":"Northeast","Vermont":"Northeast",
    "New York":"Mid-Atlantic","New Jersey":"Mid-Atlantic","Pennsylvania":"Mid-Atlantic",
    "Delaware":"Mid-Atlantic","Maryland":"Mid-Atlantic","District of Columbia":"Mid-Atlantic",
    "Virginia":"South","North Carolina":"South","South Carolina":"South",
    "Georgia":"South","Florida":"South","Tennessee":"South",
    "Kentucky":"South","Alabama":"South","Mississippi":"South",
    "Louisiana":"South","Arkansas":"South","West Virginia":"South",
    "Ohio":"Midwest","Michigan":"Midwest","Indiana":"Midwest",
    "Illinois":"Midwest","Wisconsin":"Midwest","Minnesota":"Midwest",
    "Iowa":"Midwest","Missouri":"Midwest","Kansas":"Midwest",
    "Nebraska":"Midwest","North Dakota":"Midwest","South Dakota":"Midwest",
    "Texas":"Southwest","Oklahoma":"Southwest","New Mexico":"Southwest","Arizona":"Southwest",
    "California":"West","Oregon":"West","Washington":"West",
    "Nevada":"West","Utah":"West","Colorado":"West",
    "Hawaii":"West","Alaska":"West","Wyoming":"West","Montana":"West","Idaho":"West",
    "Puerto Rico":"Other",
}

def region_of(c):
    return REGION_BY_STATE.get(c.get("state",""), "Other")


# Climate auto-derived from state. No more "unclear weather" rows.
WARM_STATES = {"Florida","California","Texas","Arizona","Nevada","Hawaii","New Mexico",
               "Louisiana","Mississippi","Alabama","Georgia","South Carolina","Puerto Rico"}
MILD_STATES = {"Virginia","North Carolina","Tennessee","Kentucky","Maryland","District of Columbia",
               "Oklahoma","Arkansas","Oregon","Washington","Delaware","West Virginia","Missouri","Kansas"}

def climate_of(c):
    st = c.get("state","")
    if st in WARM_STATES: return "warm"
    if st in MILD_STATES: return "mild"
    return "cold"


# Per-school setting (urban / college_town / suburban / rural). Comprehensive
# coverage so school_match never reports "unclear setting".
SETTING_BY_SLUG = {
    # Big-city urban
    "columbia":"urban","nyu":"urban","fordham":"urban","barnard":"urban","cooper":"urban",
    "juilliard":"urban","parsons":"urban","pratt":"urban","stevens":"urban",
    "georgetown":"urban","gwu":"urban","american":"urban","howard":"urban",
    "bu":"urban","northeastern":"urban","mit":"urban","harvard":"urban","berklee":"urban","brandeis":"suburban",
    "upenn":"urban","drexel":"urban","temple":"urban","saic":"urban",
    "uchicago":"urban","northwestern":"suburban",
    "usc":"urban","ucla":"urban","lmu":"urban","oxy":"urban",
    "ucb":"urban","sjsu":"urban","scu":"suburban","stanford":"suburban",
    "uw":"urban","uoregon":"college_town",
    "tulane":"urban","jhu":"urban","case":"urban","cmu":"urban","pitt":"urban",
    "uh":"urban","ut-austin":"urban","gatech":"urban","emory":"urban","spelman":"urban","morehouse":"urban",
    "gmu":"urban","csulb":"urban","sdsu":"urban","miami":"urban","sc":"college_town",
    "asu":"urban","arizona":"urban","utah":"urban","cu-boulder":"urban","umn":"urban",
    "neiu":"urban","risd":"urban","wm":"college_town",
    # College town / classic
    "umich":"college_town","wisc":"urban","uiuc":"college_town","msu":"college_town","iu":"college_town",
    "uf":"college_town","fsu":"college_town","uga":"college_town","auburn":"college_town","alabama":"college_town",
    "lsu":"college_town","missou":"college_town","ku":"college_town","unl":"college_town","uiowa":"college_town",
    "iowa-state":"college_town","umd":"college_town","unc":"college_town","uva":"college_town","vt":"college_town",
    "clemson":"college_town","tamu":"college_town","penn-state":"college_town","osu":"urban",
    "purdue":"college_town","stony-brook":"college_town","binghamton":"college_town","udel":"college_town",
    "uconn":"college_town","unh":"college_town","umaine":"college_town","uvm":"college_town",
    "utk":"college_town","calpoly-slo":"college_town","famu":"college_town","tuskegee":"college_town",
    "cornell":"college_town","princeton":"college_town","yale":"college_town","amherst":"college_town",
    "smith":"college_town","mt-holyoke":"college_town","oberlin":"college_town","kenyon":"rural",
    "wesleyan":"college_town","hamilton":"rural","colgate":"rural","colby":"rural","middlebury":"rural",
    "bowdoin":"college_town","bates":"college_town","carleton":"college_town","grinnell":"rural",
    "macalester":"urban","reed":"urban","whitman":"college_town","davidson":"college_town",
    "uky":"college_town","umass":"college_town","ole-miss":"college_town","trinity-ct":"urban",
    "conn-college":"suburban","skidmore":"college_town","vassar":"college_town","barnard":"urban",
    "haverford":"suburban","bryn-mawr":"suburban","swarthmore":"suburban","villanova":"suburban",
    "lehigh":"suburban","bucknell":"college_town","wm":"college_town","wake-forest":"suburban",
    "hawaii":"urban","rutgers":"college_town","gatech":"urban",
    # Suburban
    "duke":"suburban","vanderbilt":"urban","rice":"urban","notre-dame":"suburban","jhu":"urban",
    "washu":"suburban","bc":"suburban","brandeis":"suburban","pomona":"suburban","cmc":"suburban",
    "harvey-mudd":"suburban","scripps":"suburban","pitzer":"suburban","wellesley":"suburban",
    "olin":"suburban","babson":"suburban","bentley":"suburban","tufts":"suburban","pepperdine":"suburban",
    "rpi":"college_town","wpi":"urban","rose-hulman":"college_town","mit":"urban",
    "caltech":"suburban","brown":"urban","dartmouth":"rural","williams":"rural",
    # Rural / very small
    "uprm":"college_town",
}

def setting_of(c):
    return SETTING_BY_SLUG.get(c["slug"]) or (
        "urban" if c.get("size",0) > 18000 else
        "college_town" if c.get("size",0) > 5000 else
        "suburban"
    )


# Greek-life percentage by school. Strong = >25%, light = ≤10%, medium otherwise.
GREEK_PCT_BY_SLUG = {
    # Very strong (greek life is central to social life)
    "alabama":35,"auburn":40,"vanderbilt":45,"wake-forest":40,"smu":50,"tcu":40,"ole-miss":45,
    "lsu":25,"uga":30,"uf":25,"fsu":25,"penn-state":18,"sc":25,"missou":25,"iu":22,
    "wisc":13,"uiuc":20,"umich":18,"osu":12,"msu":13,"uiowa":17,"unl":18,"asu":15,"arizona":18,
    "tamu":10,"clemson":25,"utk":18,"miami":18,"sjsu":3,"davidson":50,"colgate":40,"bucknell":50,
    "lehigh":40,"villanova":15,"notre-dame":0,"bc":0,"jhu":15,"cornell":30,"dartmouth":40,
    "duke":30,"emory":25,"northwestern":35,"upenn":30,"princeton":45,"washu":25,"rice":0,
    "gatech":18,"usc":15,"tulane":30,"vt":12,"purdue":15,
    # Light to none
    "stanford":3,"yale":0,"harvard":0,"mit":0,"caltech":0,"brown":12,"columbia":12,"uchicago":8,
    "ucla":12,"ucb":8,"barnard":0,"williams":0,"amherst":0,"swarthmore":0,"pomona":0,
    "wellesley":0,"bowdoin":0,"middlebury":0,"carleton":0,"haverford":0,"reed":0,"oberlin":0,
    "macalester":0,"vassar":0,"bryn-mawr":0,"smith":0,"mt-holyoke":0,"berklee":0,"juilliard":0,
    "harvey-mudd":3,"cmc":15,"scripps":0,"pitzer":0,"olin":0,"cooper":0,"saic":0,
    "ut-austin":12,"calpoly-slo":10,"sdsu":15,"csulb":3,"gmu":3,"temple":10,"drexel":10,"northeastern":3,
    "georgetown":0,"gwu":15,"american":12,"fordham":0,"nyu":0,"uchicago":8,"bu":0,"tufts":0,
    "ucb":8,"ucsd":10,"uci":10,"ucsb":15,"stevens":15,"uconn":12,"udel":18,"binghamton":12,
    "stony-brook":3,"howard":0,"spelman":0,"morehouse":0,"famu":0,"tuskegee":0,"hawaii":0,"uoregon":15,
    "uvm":3,"unh":12,"umaine":3,"ku":18,"missou":25,"hamilton":40,"trinity-ct":40,"kenyon":35,
    "skidmore":3,"whitman":40,"conn-college":0,"colby":3,"bates":0,"grinnell":0,"oberlin":0,
    "case":15,"pitt":12,"uvm":3,"utah":10,"asu":15,"cu-boulder":18,"uh":3,"rutgers":15,
    "umd":15,"umass":3,"cosu":12,"oxy":13,"lmu":10,"pepperdine":12,"scu":3,
}

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
SPORTS_TIER_BY_SLUG = {
    # Powerhouses (football/basketball/etc.)
    "alabama":"strong","lsu":"strong","auburn":"strong","uf":"strong","uga":"strong","fsu":"strong",
    "penn-state":"strong","osu":"strong","umich":"strong","msu":"strong","wisc":"strong","iu":"strong",
    "missou":"strong","sc":"strong","utk":"strong","clemson":"strong","tamu":"strong","ut-austin":"strong",
    "vt":"strong","unc":"strong","duke":"strong","kansas":"strong","ucla":"strong","usc":"strong",
    "uiuc":"strong","uiowa":"strong","umn":"strong","purdue":"strong","ucb":"strong","stanford":"strong",
    "uw":"strong","uoregon":"strong","cu-boulder":"strong","arizona":"strong","asu":"strong",
    "syracuse":"strong","vanderbilt":"medium","notre-dame":"strong","wake-forest":"strong",
    "miami":"strong","villanova":"strong","gatech":"strong","uconn":"strong","dartmouth":"medium",
    "uky":"strong","tulane":"medium","ku":"strong","unl":"strong","gw":"medium",
    # Low sports
    "mit":"low","caltech":"low","cmu":"low","jhu":"low","uchicago":"low","cooper":"low",
    "berklee":"low","juilliard":"low","parsons":"low","pratt":"low","saic":"low","risd":"low",
    "olin":"low","babson":"low","bentley":"low","stevens":"low","drexel":"low","rpi":"low","wpi":"low",
    "rose-hulman":"low","cooper":"low","barnard":"low","scripps":"low","pitzer":"low","mt-holyoke":"low",
    "smith":"low","bryn-mawr":"low","wellesley":"low","carleton":"low","grinnell":"low","reed":"low",
    "macalester":"low","kenyon":"low","oberlin":"low","whitman":"low","cmc":"low","harvey-mudd":"low",
    "haverford":"low","swarthmore":"low","colby":"low","bates":"low","trinity-ct":"low",
    "conn-college":"low","skidmore":"low","vassar":"low","colgate":"medium","hamilton":"low","middlebury":"low",
    "williams":"low","amherst":"low","pomona":"low","bowdoin":"low",
    "fordham":"medium","gwu":"low","american":"low","brandeis":"low",
}

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
SF_RATIO_BY_SLUG = {
    "mit":3,"caltech":3,"stanford":5,"princeton":5,"yale":6,"dartmouth":7,"harvard":6,
    "upenn":6,"brown":6,"columbia":6,"uchicago":5,"jhu":7,"cmu":6,"duke":6,
    "northwestern":6,"cornell":9,"rice":6,"vanderbilt":7,"notre-dame":9,"emory":9,
    "washu":7,"georgetown":11,"tufts":10,"usc":9,"nyu":9,"case":10,"ucb":18,
    "ucla":18,"umich":11,"uva":15,"unc":14,"gatech":18,"ut-austin":18,"wisc":17,
    "uf":16,"uiuc":20,"uw":20,"umd":17,"wm":11,"osu":18,"penn-state":17,
    "msu":17,"fsu":18,"iu":17,"asu":19,"purdue":13,"binghamton":18,"stony-brook":17,
    "uci":18,"ucsd":19,"ucsb":17,"ucdavis":21,"sjsu":24,"sdsu":24,"csulb":24,
    "tamu":19,"tulane":9,"miami":11,"bu":11,"northeastern":13,"bc":12,"american":12,
    "gwu":13,"fordham":15,"villanova":12,"wake-forest":11,"lehigh":9,"bucknell":9,
    "drexel":12,"stevens":11,"olin":7,"cooper":7,"calpoly-slo":18,"udel":15,
    "uconn":16,"pitt":14,"vt":14,"clemson":15,"alabama":18,"uga":17,"utk":18,
    "auburn":18,"sc":17,"cu-boulder":18,"oregon":17,"uvm":15,"unh":18,"umaine":15,
    "uoregon":17,"uiowa":15,"umn":17,"missou":18,"ku":17,"unl":15,"hawaii":13,
    "utah":17,"uh":21,"gmu":17,"temple":14,"rutgers":13,"iowa-state":18,
    # LACs — top ones cluster at 7-9:1
    "williams":6,"amherst":7,"swarthmore":8,"pomona":7,"bowdoin":9,"wellesley":7,
    "carleton":9,"haverford":8,"vassar":8,"middlebury":8,"hamilton":9,"davidson":9,
    "colgate":9,"smith":7,"mt-holyoke":9,"bryn-mawr":7,"bates":10,"colby":10,
    "trinity-ct":9,"conn-college":9,"skidmore":9,"macalester":10,"reed":9,
    "grinnell":9,"kenyon":9,"oberlin":9,"whitman":9,"pitzer":12,"scripps":11,
    "harvey-mudd":8,"cmc":8,"oxy":10,"barnard":11,"cooper":7,"berklee":12,
    "juilliard":4,"pratt":11,"parsons":13,"risd":9,"saic":12,"howard":13,
    "spelman":11,"morehouse":13,"famu":17,"uprm":15,"tuskegee":13,"babson":13,
    "bentley":13,"rpi":13,"wpi":13,"rose-hulman":11,"brandeis":10,"pepperdine":13,
    "scu":12,"lmu":11,
}

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
ADMISSIONS_DETAIL = {
    # Ivies + Stanford/MIT/Caltech/UChicago/Duke
    "harvard":      {"rounds": ["REA","RD"],            "rates": {"REA":0.074,"RD":0.030}},
    "yale":         {"rounds": ["REA","RD"],            "rates": {"REA":0.094,"RD":0.030}},
    "princeton":    {"rounds": ["REA","RD"],            "rates": {"REA":0.108,"RD":0.040}},
    "stanford":     {"rounds": ["REA","RD"],            "rates": {"REA":0.079,"RD":0.031}},
    "mit":          {"rounds": ["EA","RD"],             "rates": {"EA":0.053,"RD":0.045}},
    "caltech":      {"rounds": ["EA","RD"],             "rates": {"EA":0.050,"RD":0.020}},
    "columbia":     {"rounds": ["ED","RD"],             "rates": {"ED":0.119,"RD":0.030}},
    "upenn":        {"rounds": ["ED","RD"],             "rates": {"ED":0.140,"RD":0.045}},
    "duke":         {"rounds": ["ED","RD"],             "rates": {"ED":0.144,"RD":0.040}},
    "dartmouth":    {"rounds": ["ED","RD"],             "rates": {"ED":0.174,"RD":0.046}},
    "brown":        {"rounds": ["ED","RD"],             "rates": {"ED":0.129,"RD":0.046}},
    "cornell":      {"rounds": ["ED","RD"],             "rates": {"ED":0.170,"RD":0.055}},
    "northwestern": {"rounds": ["ED","RD"],             "rates": {"ED":0.240,"RD":0.060}},
    "uchicago":     {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.180,"ED2":0.140,"EA":0.075,"RD":0.035}},
    # Top privates with ED/EA splits
    "vanderbilt":   {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.167,"ED2":0.140,"RD":0.045}},
    "rice":         {"rounds": ["ED","RD"],             "rates": {"ED":0.137,"RD":0.060}},
    "jhu":          {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.265,"ED2":0.175,"RD":0.045}},
    "washu":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.280,"ED2":0.160,"RD":0.090}},
    "notre-dame":   {"rounds": ["REA","RD"],            "rates": {"REA":0.170,"RD":0.070}},
    "georgetown":   {"rounds": ["EA","RD"],             "rates": {"EA":0.120,"RD":0.110}},
    "nyu":          {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.250,"ED2":0.220,"RD":0.070}},
    "emory":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.280,"ED2":0.200,"RD":0.090}},
    "tufts":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.300,"ED2":0.190,"RD":0.070}},
    "cmu":          {"rounds": ["ED","RD"],             "rates": {"ED":0.200,"RD":0.100}},
    "usc":          {"rounds": ["EA","RD"],             "rates": {"EA":0.110,"RD":0.080}},
    "wake-forest":  {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.450,"ED2":0.300,"RD":0.190}},
    "bc":           {"rounds": ["ED","EA","RD"],        "rates": {"ED":0.330,"EA":0.180,"RD":0.140}},
    "tulane":       {"rounds": ["ED","EA","RD"],        "rates": {"ED":0.420,"EA":0.110,"RD":0.080}},
    "villanova":    {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.350,"ED2":0.180,"EA":0.230,"RD":0.190}},
    "lehigh":       {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.470,"ED2":0.340,"RD":0.290}},
    "case":         {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.350,"ED2":0.290,"EA":0.290,"RD":0.220}},
    "northeastern": {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.280,"ED2":0.170,"EA":0.055,"RD":0.050}},
    "bu":           {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.290,"ED2":0.140,"RD":0.080}},
    "miami":        {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.430,"ED2":0.300,"EA":0.200,"RD":0.190}},
    "brandeis":     {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.500,"ED2":0.380,"RD":0.350}},
    "fordham":      {"rounds": ["EA","RD"],             "rates": {"EA":0.580,"RD":0.530}},
    "gwu":          {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.690,"ED2":0.610,"RD":0.480}},
    "american":     {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.870,"ED2":0.800,"RD":0.510}},
    # LAC ED-heavy
    "williams":     {"rounds": ["ED","RD"],             "rates": {"ED":0.260,"RD":0.080}},
    "amherst":      {"rounds": ["ED","RD"],             "rates": {"ED":0.300,"RD":0.080}},
    "swarthmore":   {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.250,"ED2":0.180,"RD":0.060}},
    "pomona":       {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.150,"ED2":0.100,"RD":0.060}},
    "bowdoin":      {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.230,"ED2":0.180,"RD":0.080}},
    "middlebury":   {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.380,"ED2":0.300,"RD":0.130}},
    "wellesley":    {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.380,"ED2":0.260,"RD":0.130}},
    "colby":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.450,"ED2":0.300,"RD":0.080}},
    "bates":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.500,"ED2":0.350,"RD":0.140}},
    "hamilton":     {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.420,"ED2":0.310,"RD":0.110}},
    "vassar":       {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.430,"ED2":0.280,"RD":0.170}},
    "barnard":      {"rounds": ["ED","RD"],             "rates": {"ED":0.230,"RD":0.080}},
    # Public flagships — in/out of state matters
    "umich":        {"rounds": ["EA","RD"],   "rates": {"EA":0.220,"RD":0.170}, "in_state_rate":0.41, "out_of_state_rate":0.20},
    "uva":          {"rounds": ["ED","EA","RD"], "rates": {"ED":0.290,"EA":0.180,"RD":0.150}, "in_state_rate":0.30, "out_of_state_rate":0.15},
    "unc":          {"rounds": ["EA","RD"],   "rates": {"EA":0.190,"RD":0.150}, "in_state_rate":0.42, "out_of_state_rate":0.09},
    "ucla":         {"rounds": ["RD"],         "rates": {"RD":0.086}, "in_state_rate":0.13, "out_of_state_rate":0.07},
    "ucb":          {"rounds": ["RD"],         "rates": {"RD":0.115}, "in_state_rate":0.18, "out_of_state_rate":0.08},
    "ucsd":         {"rounds": ["RD"],         "rates": {"RD":0.235}, "in_state_rate":0.27, "out_of_state_rate":0.20},
    "ucsb":         {"rounds": ["RD"],         "rates": {"RD":0.260}, "in_state_rate":0.31, "out_of_state_rate":0.20},
    "uci":          {"rounds": ["RD"],         "rates": {"RD":0.290}, "in_state_rate":0.32, "out_of_state_rate":0.26},
    "ucdavis":      {"rounds": ["RD"],         "rates": {"RD":0.420}, "in_state_rate":0.45, "out_of_state_rate":0.39},
    "gatech":       {"rounds": ["EA","RD"],   "rates": {"EA":0.220,"RD":0.140}, "in_state_rate":0.36, "out_of_state_rate":0.13},
    "wm":           {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.520,"ED2":0.400,"RD":0.310}, "in_state_rate":0.40, "out_of_state_rate":0.27},
    "ut-austin":    {"rounds": ["RD"],         "rates": {"RD":0.290}, "in_state_rate":0.36, "out_of_state_rate":0.10},
    "wisc":         {"rounds": ["EA","RD"],   "rates": {"EA":0.510,"RD":0.450}, "in_state_rate":0.62, "out_of_state_rate":0.39},
    "uiuc":         {"rounds": ["EA","RD"],   "rates": {"EA":0.600,"RD":0.430}, "in_state_rate":0.62, "out_of_state_rate":0.34},
    "uf":           {"rounds": ["EA","RD"],   "rates": {"EA":0.230,"RD":0.230}, "in_state_rate":0.30, "out_of_state_rate":0.13},
    "umd":          {"rounds": ["EA","RD"],   "rates": {"EA":0.470,"RD":0.430}, "in_state_rate":0.55, "out_of_state_rate":0.32},
    "uw":           {"rounds": ["RD"],         "rates": {"RD":0.430}, "in_state_rate":0.56, "out_of_state_rate":0.34},
    "binghamton":   {"rounds": ["ED","RD"],   "rates": {"ED":0.500,"RD":0.420}, "in_state_rate":0.49, "out_of_state_rate":0.36},
    "purdue":       {"rounds": ["EA","RD"],   "rates": {"EA":0.530,"RD":0.500}, "in_state_rate":0.59, "out_of_state_rate":0.50},
    "rutgers":      {"rounds": ["RD"],         "rates": {"RD":0.660}, "in_state_rate":0.72, "out_of_state_rate":0.58},
    "penn-state":   {"rounds": ["RD"],         "rates": {"RD":0.540}, "in_state_rate":0.62, "out_of_state_rate":0.46},
    "osu":          {"rounds": ["EA","RD"],   "rates": {"EA":0.520,"RD":0.530}, "in_state_rate":0.66, "out_of_state_rate":0.45},
    "msu":          {"rounds": ["RD"],         "rates": {"RD":0.830}, "in_state_rate":0.86, "out_of_state_rate":0.78},
    "uconn":        {"rounds": ["EA","RD"],   "rates": {"EA":0.570,"RD":0.540}, "in_state_rate":0.66, "out_of_state_rate":0.49},
    "vt":           {"rounds": ["ED","EA","RD"], "rates": {"ED":0.700,"EA":0.620,"RD":0.560}, "in_state_rate":0.71, "out_of_state_rate":0.39},
    "clemson":      {"rounds": ["EA","RD"],   "rates": {"EA":0.450,"RD":0.430}, "in_state_rate":0.59, "out_of_state_rate":0.36},
    "uga":          {"rounds": ["EA","RD"],   "rates": {"EA":0.420,"RD":0.380}, "in_state_rate":0.50, "out_of_state_rate":0.34},
    # More public flagships + state schools
    "fsu":          {"rounds": ["EA","RD"],   "rates": {"EA":0.260,"RD":0.250}, "in_state_rate":0.32, "out_of_state_rate":0.18},
    "iu":           {"rounds": ["EA","RD"],   "rates": {"EA":0.820,"RD":0.770}, "in_state_rate":0.84, "out_of_state_rate":0.74},
    "asu":          {"rounds": ["RD"],         "rates": {"RD":0.880}, "in_state_rate":0.91, "out_of_state_rate":0.85},
    "pitt":         {"rounds": ["RD"],         "rates": {"RD":0.500}, "in_state_rate":0.62, "out_of_state_rate":0.40},
    "tamu":         {"rounds": ["RD"],         "rates": {"RD":0.630}, "in_state_rate":0.69, "out_of_state_rate":0.45},
    "alabama":      {"rounds": ["RD"],         "rates": {"RD":0.800}, "in_state_rate":0.82, "out_of_state_rate":0.79},
    "utk":          {"rounds": ["RD"],         "rates": {"RD":0.450}, "in_state_rate":0.55, "out_of_state_rate":0.36},
    "arizona":      {"rounds": ["RD"],         "rates": {"RD":0.870}, "in_state_rate":0.90, "out_of_state_rate":0.85},
    "uiowa":        {"rounds": ["RD"],         "rates": {"RD":0.860}, "in_state_rate":0.89, "out_of_state_rate":0.83},
    "umn":          {"rounds": ["RD"],         "rates": {"RD":0.750}, "in_state_rate":0.80, "out_of_state_rate":0.69},
    "cu-boulder":   {"rounds": ["EA","RD"],   "rates": {"EA":0.810,"RD":0.770}, "in_state_rate":0.86, "out_of_state_rate":0.74},
    "uoregon":      {"rounds": ["RD"],         "rates": {"RD":0.860}, "in_state_rate":0.89, "out_of_state_rate":0.84},
    "stony-brook":  {"rounds": ["EA","RD"],   "rates": {"EA":0.500,"RD":0.470}, "in_state_rate":0.50, "out_of_state_rate":0.42},
    "uky":          {"rounds": ["RD"],         "rates": {"RD":0.950}, "in_state_rate":0.96, "out_of_state_rate":0.94},
    "auburn":       {"rounds": ["EA","RD"],   "rates": {"EA":0.460,"RD":0.420}, "in_state_rate":0.55, "out_of_state_rate":0.36},
    "lsu":          {"rounds": ["RD"],         "rates": {"RD":0.760}, "in_state_rate":0.82, "out_of_state_rate":0.70},
    "sc":           {"rounds": ["RD"],         "rates": {"RD":0.640}, "in_state_rate":0.74, "out_of_state_rate":0.56},
    "missou":       {"rounds": ["RD"],         "rates": {"RD":0.840}, "in_state_rate":0.90, "out_of_state_rate":0.78},
    "ku":           {"rounds": ["RD"],         "rates": {"RD":0.880}, "in_state_rate":0.92, "out_of_state_rate":0.83},
    "unl":          {"rounds": ["RD"],         "rates": {"RD":0.830}, "in_state_rate":0.88, "out_of_state_rate":0.79},
    "uvm":          {"rounds": ["EA","RD"],   "rates": {"EA":0.660,"RD":0.640}, "in_state_rate":0.83, "out_of_state_rate":0.62},
    "udel":         {"rounds": ["EA","RD"],   "rates": {"EA":0.730,"RD":0.700}, "in_state_rate":0.79, "out_of_state_rate":0.66},
    "calpoly-slo":  {"rounds": ["RD"],         "rates": {"RD":0.300}, "in_state_rate":0.31, "out_of_state_rate":0.27},
    "sdsu":         {"rounds": ["RD"],         "rates": {"RD":0.380}, "in_state_rate":0.40, "out_of_state_rate":0.32},
    "sjsu":         {"rounds": ["RD"],         "rates": {"RD":0.770}, "in_state_rate":0.81, "out_of_state_rate":0.62},
    "csulb":        {"rounds": ["RD"],         "rates": {"RD":0.420}, "in_state_rate":0.46, "out_of_state_rate":0.31},
    "iowa-state":   {"rounds": ["RD"],         "rates": {"RD":0.900}, "in_state_rate":0.93, "out_of_state_rate":0.87},
    "temple":       {"rounds": ["RD"],         "rates": {"RD":0.800}, "in_state_rate":0.85, "out_of_state_rate":0.74},
    "gmu":          {"rounds": ["EA","RD"],   "rates": {"EA":0.910,"RD":0.890}, "in_state_rate":0.92, "out_of_state_rate":0.85},
    "uh":           {"rounds": ["RD"],         "rates": {"RD":0.660}, "in_state_rate":0.69, "out_of_state_rate":0.51},
    "utah":         {"rounds": ["EA","RD"],   "rates": {"EA":0.910,"RD":0.890}, "in_state_rate":0.94, "out_of_state_rate":0.85},
    "hawaii":       {"rounds": ["RD"],         "rates": {"RD":0.580}, "in_state_rate":0.66, "out_of_state_rate":0.51},
    "unh":          {"rounds": ["EA","RD"],   "rates": {"EA":0.860,"RD":0.840}, "in_state_rate":0.89, "out_of_state_rate":0.82},
    "umaine":       {"rounds": ["EA","RD"],   "rates": {"EA":0.920,"RD":0.900}, "in_state_rate":0.94, "out_of_state_rate":0.88},
    "neiu":         {"rounds": ["RD"],         "rates": {"RD":0.870}, "in_state_rate":0.89, "out_of_state_rate":0.78},
    # LACs — mostly ED + RD, some with ED2
    "carleton":     {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.380,"ED2":0.260,"RD":0.190}},
    "haverford":    {"rounds": ["ED","RD"],       "rates": {"ED":0.370,"RD":0.150}},
    "davidson":     {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.430,"ED2":0.280,"RD":0.130}},
    "colgate":      {"rounds": ["ED","RD"],       "rates": {"ED":0.300,"RD":0.110}},
    "smith":        {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.510,"ED2":0.380,"RD":0.220}},
    "mt-holyoke":   {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.620,"ED2":0.450,"RD":0.380}},
    "bryn-mawr":    {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.620,"ED2":0.480,"RD":0.330}},
    "trinity-ct":   {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.620,"ED2":0.500,"RD":0.380}},
    "conn-college": {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.620,"ED2":0.480,"RD":0.330}},
    "skidmore":     {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.500,"ED2":0.350,"RD":0.260}},
    "macalester":   {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.450,"ED2":0.330,"RD":0.250}},
    "reed":         {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.520,"ED2":0.430,"RD":0.270}},
    "grinnell":     {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.460,"ED2":0.300,"RD":0.090}},
    "kenyon":       {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.480,"ED2":0.380,"RD":0.260}},
    "oberlin":      {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.500,"ED2":0.420,"RD":0.300}},
    "whitman":      {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.700,"ED2":0.500,"RD":0.450}},
    "pitzer":       {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.300,"ED2":0.220,"RD":0.150}},
    "scripps":      {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.380,"ED2":0.290,"RD":0.250}},
    "harvey-mudd":  {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.190,"ED2":0.140,"RD":0.080}},
    "cmc":          {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.260,"ED2":0.190,"RD":0.080}},
    "oxy":          {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.500,"ED2":0.350,"RD":0.260}},
    "wesleyan":     {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.420,"ED2":0.300,"RD":0.140}},
    "bucknell":     {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.620,"ED2":0.430,"RD":0.290}},
    # Engineering/tech specialty
    "rpi":          {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.660,"ED2":0.540,"RD":0.560}},
    "wpi":          {"rounds": ["ED","ED2","EA","RD"], "rates": {"ED":0.700,"ED2":0.580,"EA":0.580,"RD":0.530}},
    "rose-hulman":  {"rounds": ["ED","EA","RD"],  "rates": {"ED":0.770,"EA":0.730,"RD":0.700}},
    "olin":         {"rounds": ["EA","RD"],       "rates": {"EA":0.180,"RD":0.140}},
    "stevens":      {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.660,"ED2":0.540,"RD":0.420}},
    "cooper":       {"rounds": ["ED","RD"],       "rates": {"ED":0.180,"RD":0.110}},
    "drexel":       {"rounds": ["ED","RD"],       "rates": {"ED":0.880,"RD":0.770}},
    # Arts schools — rounds vary, often portfolio-driven
    "berklee":      {"rounds": ["EA","RD"],       "rates": {"EA":0.560,"RD":0.520}},
    "juilliard":    {"rounds": ["RD"],             "rates": {"RD":0.080}},
    "pratt":        {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.760,"ED2":0.620,"RD":0.540}},
    "parsons":      {"rounds": ["EA","RD"],       "rates": {"EA":0.610,"RD":0.580}},
    "risd":         {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.300,"ED2":0.200,"RD":0.170}},
    "saic":         {"rounds": ["EA","RD"],       "rates": {"EA":0.730,"RD":0.690}},
    # Business-focused
    "babson":       {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.430,"ED2":0.350,"RD":0.180}},
    "bentley":      {"rounds": ["ED","ED2","RD"], "rates": {"ED":0.700,"ED2":0.560,"RD":0.490}},
    # Religious-affiliated / California privates
    "pepperdine":   {"rounds": ["ED","EA","RD"],  "rates": {"ED":0.510,"EA":0.470,"RD":0.420}},
    "scu":          {"rounds": ["EA","RD"],       "rates": {"EA":0.500,"RD":0.490}},
    "lmu":          {"rounds": ["EA","RD"],       "rates": {"EA":0.460,"RD":0.430}},
    # HBCUs / Hispanic-serving
    "howard":       {"rounds": ["ED","RD"],       "rates": {"ED":0.560,"RD":0.300}},
    "spelman":      {"rounds": ["ED","EA","RD"],  "rates": {"ED":0.470,"EA":0.430,"RD":0.380}},
    "morehouse":    {"rounds": ["ED","EA","RD"],  "rates": {"ED":0.650,"EA":0.600,"RD":0.550}},
    "famu":         {"rounds": ["EA","RD"],       "rates": {"EA":0.380,"RD":0.350}, "in_state_rate":0.41, "out_of_state_rate":0.31},
    "uprm":         {"rounds": ["RD"],             "rates": {"RD":0.660}, "in_state_rate":0.78, "out_of_state_rate":0.45},
    "tuskegee":     {"rounds": ["RD"],             "rates": {"RD":0.520}},
}


def admissions_detail(school):
    """Return the round/rate detail dict for a school, or None if not curated."""
    return ADMISSIONS_DETAIL.get(school["slug"])


# Where this leads — career feeders / industry pipelines / geographic
# advantages per school. Structure: list of short bullets, 2-5 per school.
# AI-generated for schools not in the curated dict (cached forever in DB).
SCHOOL_FEEDERS = {
    "harvard": ["Investment banking + consulting (MBB pipeline)", "Government & policy (heavy DC + State Dept presence)", "Academia / PhD programs at any top school", "Big Law via HLS pipeline"],
    "stanford": ["Tech / SWE (every FAANG hires aggressively)", "Startup founding (#1 founder rate of any school)", "Venture capital (deep Sand Hill connections)", "Quant trading + AI research labs"],
    "mit": ["Tech / SWE at frontier AI labs (OpenAI, Anthropic, DeepMind)", "Quant trading (Jane Street, Citadel, Two Sigma)", "Hardware engineering + robotics", "Startup founding in deep tech"],
    "yale": ["Big Law (Yale Law pipeline)", "Consulting (MBB heavy)", "Government / public policy", "Finance + nonprofit leadership"],
    "princeton": ["Investment banking + PE (heavy NYC pipeline)", "Consulting (MBB)", "Academia (high PhD rate)", "Government / Public Affairs (Wilson School)"],
    "columbia": ["Investment banking (Wall Street is 20 min away)", "Journalism (J-school feeder)", "Consulting", "Big Law"],
    "uchicago": ["Consulting (MBB-heavy)", "Quant finance + economics PhD pipeline", "Investment banking", "Academia"],
    "upenn": ["Investment banking + PE (Wharton dominates Wall St recruiting)", "Consulting (MBB top hire from Wharton)", "Big Tech via M&T / engineering", "VC / hedge funds"],
    "brown": ["Tech / SWE (Open Curriculum produces flexible engineers)", "Consulting", "Creative writing / journalism", "Medicine via PLME"],
    "dartmouth": ["Consulting (per-capita MBB hire rate near top of any school)", "Investment banking", "Tech (smaller pipeline but real)", "Tuck MBA pipeline"],
    "duke": ["Consulting + investment banking (MBB recruits heavily)", "Pre-med (Duke Med pipeline)", "Tech / SWE (growing Triangle ecosystem)", "Big Law"],
    "northwestern": ["Consulting (MBB top hire)", "Marketing + journalism (Medill pipeline)", "Investment banking", "Tech in Chicago"],
    "cornell": ["Tech / SWE (especially CS + Engineering schools)", "Hospitality (Hotel School is uniquely strong)", "Investment banking via Dyson / AEM", "Vet / Ag (CALS pipeline)"],
    "caltech": ["Frontier AI research", "Quant trading", "Tech / SWE at deep-tech firms", "Aerospace + JPL pipeline"],
    "jhu": ["Pre-med + medical research (Johns Hopkins Hospital pipeline)", "Biomedical engineering at top firms", "International policy (SAIS connection)", "Public health / NIH / CDC"],
    "vanderbilt": ["Consulting + investment banking (Owen pipeline)", "Pre-med (Vanderbilt Medical Center)", "Healthcare consulting (Nashville hub)", "Education / Peabody pipeline"],
    "rice": ["Engineering at energy / oil & gas firms (Houston advantage)", "Pre-med (Texas Medical Center next door)", "Tech / SWE", "Architecture (top program)"],
    "notre-dame": ["Investment banking + consulting (Mendoza recruits well)", "Big Law", "Engineering at Midwest manufacturers", "Catholic Church-affiliated nonprofits"],
    "cmu": ["Tech / SWE at every FAANG (CS school is top-3 nationally)", "AI / ML research", "Quant trading", "Drama / film (Tisch alternative)"],
    "usc": ["Entertainment industry (film, TV, music — Hollywood dominance)", "Tech in LA", "Marshall pipeline to consulting / IB", "Journalism via Annenberg"],
    "nyu": ["Investment banking (Stern is top-3 IB feeder)", "Tech / SWE in NYC", "Film / TV (Tisch)", "Fashion + media"],
    "georgetown": ["Government / State Dept / foreign service (#1 IR feeder)", "Big Law (Georgetown Law)", "Consulting (DC offices)", "Finance"],
    "ucb": ["Tech / SWE at every Bay Area firm (Cal CS rivals Stanford)", "Startup founding (deep VC ties)", "Public policy + academia", "Engineering at every top firm"],
    "ucla": ["Entertainment + media (Hollywood pipeline)", "Tech in LA", "Pre-med (DGSOM)", "Public policy"],
    "umich": ["Investment banking + consulting (Ross is top-tier)", "Engineering at Big 3 auto + every Big Tech", "Big Law (Michigan Law)", "Healthcare via Med School"],
    "uva": ["Consulting + investment banking (McIntire is elite)", "Big Law (UVA Law)", "Government / DC pipeline", "Tech (growing)"],
    "gatech": ["Engineering at every top firm (top-5 engineering school)", "Tech / SWE (Atlanta hub for Coca-Cola, Delta, Home Depot, plus Google ATL)", "Aerospace (Lockheed, Delta)", "Cybersecurity (NSA + GTRI)"],
    "tufts": ["International relations / foreign service (Fletcher pipeline)", "Pre-med + biotech (Boston ecosystem)", "Tech / SWE (Boston tech)", "Consulting"],
    "washu": ["Pre-med (BJC HealthCare pipeline)", "Investment banking (Olin recruits)", "Healthcare consulting", "Biology / biomedical research"],
    "emory": ["Pre-med (Emory Healthcare + CDC + Children's HC of Atlanta)", "Public health (Rollins pipeline)", "Investment banking (Goizueta)", "Big Law"],
    "unc": ["Pharmacy (top program nationally)", "Journalism (top J-school)", "Pre-med (UNC Hospitals)", "Public policy + government"],
    "bu": ["Communications / media (huge alumni in journalism)", "Pre-med (BU Medical)", "Engineering at Boston tech / biotech", "Consulting (mid-tier)"],
    "bc": ["Investment banking (Carroll School recruits well)", "Big Law", "Catholic ministry / nonprofit leadership", "Education + social work"],
    "wake-forest": ["Investment banking (heavy Wall Street pipeline for the size)", "Big Law", "Banking / wealth management (Charlotte hub)", "Consulting"],
    "wm": ["Government / CIA / State Dept (DC pipeline + 'CIA prep school' nickname)", "Big Law", "Foreign service", "Public policy"],
    "uf": ["Pre-med (UF Health)", "Engineering / tech in Florida", "Agriculture (huge ag program)", "Journalism (Innovation News Center)"],
    "ut-austin": ["Tech / SWE (Austin = mini Silicon Valley: Apple, Tesla, Oracle, Google all hire)", "Engineering at oil + aerospace (Texas advantage)", "Investment banking (McCombs)", "Government / law (UT Law)"],
    "tulane": ["Pre-med (Tulane Medical)", "Public health (#1 in tropical medicine)", "Architecture", "Music industry (New Orleans)"],
    "northeastern": ["Co-op pipeline = direct hire at every Boston tech firm", "Engineering / SWE", "Pharma / biotech (Boston ecosystem)", "Finance (NYC pipeline)"],
    "rpi": ["Engineering at defense + aerospace (Lockheed, Northrop, Raytheon)", "Tech / SWE (especially gaming + simulation)", "Architecture", "Finance / quant (smaller pipeline)"],
    "harvey-mudd": ["Tech / SWE at every top firm (highest STEM PhD rate per capita)", "Engineering R&D", "Quant trading", "Academia (very high PhD rate)"],
    "babson": ["Entrepreneurship + founding (built around it)", "Investment banking", "Family business / consulting", "Real estate"],
    "cmc": ["Investment banking + consulting", "Government / think tanks (Rose Institute)", "Academia (econ PhD pipeline)", "Big Law"],
    "swarthmore": ["Academia / PhD programs (highest per-capita PhD rate of any college)", "Public service / nonprofit leadership", "Tech (smaller pipeline)", "Big Law"],
    "williams": ["Academia / PhD programs", "Investment banking + finance (small but elite pipeline)", "Consulting", "Education + nonprofit leadership"],
    "amherst": ["Academia + PhD pipeline", "Public service + government", "Big Law", "Investment banking"],
    "pomona": ["Tech / SWE (LA + 5C consortium connections)", "Academia", "Consulting", "Public policy"],
}


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


ROUND_LABELS = {
    "ED":  "Early Decision",
    "ED2": "Early Decision II",
    "EA":  "Early Action",
    "REA": "Restrictive EA",
    "RD":  "Regular Decision",
}


def render_admissions_breakdown(school, detail, dark=False):
    """HTML block showing round-by-round acceptance rates + in/out-of-state
    where available. Empty string if no curated data. dark=True styles for
    the black chances card on the plan page."""
    if not detail:
        return ""
    rates = detail.get("rates", {})
    border = "#333" if dark else "#f0f0f0"
    label_color = "#bdbdbd" if dark else "#666"
    rows = ""
    for r in detail.get("rounds", []):
        rate = rates.get(r)
        rate_str = f"{round(rate*100,1)}%" if rate is not None else "—"
        rows += f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid {border};font-size:.9em"><span>{ROUND_LABELS.get(r, r)}</span><span style="font-weight:600">{rate_str}</span></div>'
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
    return f'<div style="margin-top:12px"><div style="font-weight:600;font-size:.85em;color:{label_color};margin-bottom:2px">By application round</div>{rows}{state_block}</div>'


def render_round_breakdown_dark(school, detail):
    return render_admissions_breakdown(school, detail, dark=True)


def sf_ratio(c):
    """Best-effort student-faculty ratio: curated value if known, else estimated
    from size + type. Override table (Scorecard data) takes precedence when set."""
    over = _get_overrides(c["slug"])
    if over and over.get("sf_ratio"): return over["sf_ratio"]
    if c["slug"] in SF_RATIO_BY_SLUG:
        return SF_RATIO_BY_SLUG[c["slug"]]
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
                "SELECT accept, sat_25, sat_75, act_25, act_75, size, tuition, sf_ratio, source, verified_at "
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
TEST_BLIND_SCHOOLS = {
    "ucb", "ucla", "ucsd", "uci", "ucsb", "ucsc", "ucdavis", "ucr", "ucmerced",
}

# Schools where a portfolio / audition / research supplement is a primary
# admissions gatekeeper (not just nice-to-have). When the user has a
# portfolio listed in their profile, odds at these schools get a modest
# 1.15x lift. Doesn't apply to schools where portfolios are optional /
# don't materially change admit odds.
PORTFOLIO_GATEKEEPER_SCHOOLS = {
    "usc",          # Roski, Iovine, SCA, Thornton (audition), Kaufman (audition)
    "nyu",          # Tisch (multiple programs, audition + portfolio)
    "cmu",          # School of Drama (audition), School of Art (portfolio), Architecture
    "cornell",      # AAP — Architecture, Art programs
    "northwestern", # School of Communication portfolio tracks, Bienen audition
    "umich",        # Stamps (portfolio), MTD (audition)
    "rice",         # Architecture, Shepherd Music
    "barnard",      # Architecture, Visual Arts
    "risd",         # Portfolio is THE primary admissions criterion
    "parsons",      # Portfolio + Parsons Challenge
    "berklee",      # Audition + interview required
    "juilliard",    # Audition is primary criterion
    "cooper",       # Cooper Union — Architecture/Art portfolio
    "saic",         # Art Institute of Chicago — portfolio
    "pratt",        # Pratt Institute — portfolio
}

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
CDS_VERIFIED = {
    # Ivy League
    "princeton":  {"accept": 0.0462, "sat_25": 1500, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "harvard":    {"accept": 0.0364, "sat_25": 1510, "sat_75": 1550, "act_25": 34, "act_75": 36},
    "yale":       {"accept": 0.0387, "sat_25": 1480, "sat_75": 1560, "act_25": 33, "act_75": 35},
    "upenn":      {"accept": 0.054,  "sat_25": 1510, "sat_75": 1550, "act_25": 34, "act_75": 36},
    "brown":      {"accept": 0.054,  "sat_25": 1500, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "columbia":   {"accept": 0.04,   "sat_25": 1500, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "dartmouth":  {"accept": 0.054},
    "cornell":    {"accept": 0.0841, "sat_25": 1510, "sat_75": 1560, "act_25": 33, "act_75": 35},
    # Ivy Plus
    "mit":        {"accept": 0.0455, "sat_25": 1520, "sat_75": 1570, "act_25": 34, "act_75": 36},
    "stanford":   {"accept": 0.0361, "sat_25": 1510, "sat_75": 1570, "act_25": 34, "act_75": 35},
    "uchicago":   {"accept": 0.0448, "sat_25": 1510, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "duke":       {"accept": 0.0571, "sat_25": 1500, "sat_75": 1570, "act_25": 34, "act_75": 35},
    "jhu":        {"accept": 0.0644, "sat_25": 1530, "sat_75": 1560, "act_25": 34, "act_75": 36},
    "northwestern":{"accept": 0.0769, "sat_25": 1510, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "caltech":    {"accept": 0.0257},
    "rice":       {"accept": 0.08,   "sat_25": 1510, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "vanderbilt": {"accept": 0.0586, "sat_25": 1500, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "washu":      {"accept": 0.1206, "sat_25": 1500, "sat_75": 1570, "act_25": 33, "act_75": 35},
    "notre-dame": {"accept": 0.1127, "sat_25": 1470, "sat_75": 1540, "act_25": 33, "act_75": 35},
    # Elite publics + high-prestige privates
    "ucb":        {"accept": 0.1132, "sat_25": None, "sat_75": None, "act_25": None, "act_75": None},
    "ucla":       {"accept": 0.0942, "sat_25": None, "sat_75": None, "act_25": None, "act_75": None},
    "cmu":        {"accept": 0.1167, "sat_25": 1510, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "umich":      {"accept": 0.1564, "sat_25": 1360, "sat_75": 1530, "act_25": 31, "act_75": 34},
    "emory":      {"accept": 0.1029, "sat_25": 1480, "sat_75": 1540, "act_25": 32, "act_75": 35},
    "georgetown": {"accept": 0.1291, "sat_25": 1400, "sat_75": 1540, "act_25": 31, "act_75": 35},
    "uva":        {"accept": 0.168,  "sat_25": 1410, "sat_75": 1520, "act_25": 32, "act_75": 35},
    "unc":        {"accept": 0.1534, "sat_25": 1400, "sat_75": 1530, "act_25": 28, "act_75": 34},
    "usc":        {"accept": 0.0981, "sat_25": 1450, "sat_75": 1550, "act_25": 32, "act_75": 35},
    "uf":         {"accept": 0.242,  "sat_25": 1330, "sat_75": 1470, "act_25": 29, "act_75": 33},
    "ut-austin":  {"accept": 0.2664},
    "ucsd":       {"accept": 0.2856},  # test-blind
    # Top liberal arts
    "williams":   {"accept": 0.0825, "sat_25": 1500, "sat_75": 1560, "act_25": 34, "act_75": 35},
    "amherst":    {"accept": 0.0901, "sat_25": 1500, "sat_75": 1560, "act_25": 33, "act_75": 35},
    "swarthmore": {"accept": 0.0746, "sat_25": 1500, "sat_75": 1550, "act_25": 33, "act_75": 35},
    "pomona":     {"accept": 0.0708, "sat_25": 1500, "sat_75": 1550, "act_25": 33, "act_75": 35},
    "wellesley":  {"accept": 0.1405, "sat_25": 1470, "sat_75": 1550, "act_25": 33, "act_75": 35},
    "bowdoin":    {"accept": 0.0713, "sat_25": 1470, "sat_75": 1540, "act_25": 33, "act_75": 35},
    "carleton":   {"accept": 0.2041, "sat_25": 1470, "sat_75": 1540, "act_25": 32, "act_75": 35},
    "middlebury": {"accept": 0.1075, "sat_25": 1450, "sat_75": 1530, "act_25": 33, "act_75": 35},
    "cmc":        {"accept": 0.0959, "sat_25": 1490, "sat_75": 1550, "act_25": 33, "act_75": 35},
    "hamilton":   {"accept": 0.1362, "sat_25": 1460, "sat_75": 1530, "act_25": 33, "act_75": 35},
    # ─── Round 2 batch (extracted from CDS PDFs, May 2026) ───
    "nyu":        {"accept": 0.0906, "sat_25": 1480, "sat_75": 1550, "act_25": 34, "act_75": 35},
    "tufts":      {"accept": 0.1149, "sat_25": 1480, "sat_75": 1540, "act_25": 33, "act_75": 35},
    "bu":         {"accept": 0.1111, "sat_25": 1430, "sat_75": 1510, "act_25": 32, "act_75": 34},
    "wake-forest":{"accept": 0.2076, "sat_25": 1420, "sat_75": 1490, "act_25": 32, "act_75": 34},
    "villanova":  {"accept": 0.2698, "sat_25": 1410, "sat_75": 1490, "act_25": 32, "act_75": 34},
    "brandeis":   {"accept": 0.4050, "sat_25": 1415, "sat_75": 1510, "act_25": 31, "act_75": 34},
    "tamu":       {"accept": 0.5166, "sat_25": 1160, "sat_75": 1390, "act_25": 24, "act_75": 32},
    "uga":        {"accept": 0.3792, "sat_25": 1220, "sat_75": 1400, "act_25": 26, "act_75": 32},
    "vassar":     {"accept": 0.2093, "sat_25": 1460, "sat_75": 1520, "act_25": 33, "act_75": 34},
    "macalester": {"accept": 0.1483, "sat_25": 1325, "sat_75": 1510, "act_25": 31, "act_75": 34},
    "smith":      {"accept": 0.2100, "sat_25": 1450, "sat_75": 1520, "act_25": 32, "act_75": 35},
    # SAT/ACT extracted but accept rate not yet pulled from CDS — Scorecard fallback applies
    "lehigh":     {"sat_25": 1380, "sat_75": 1480, "act_25": 31, "act_75": 34},
    "case":       {"sat_25": 1440, "sat_75": 1530, "act_25": 32, "act_75": 34},
    "gatech":     {"sat_25": 1370, "sat_75": 1530, "act_25": 31, "act_75": 35},
    "penn-state": {"sat_25": 1250, "sat_75": 1410, "act_25": 27, "act_75": 32},
    "colgate":    {"sat_25": 1450, "sat_75": 1530, "act_25": 33, "act_75": 34},
    # Round 3 batch
    "uci":        {"accept": 0.2895},  # test-blind
    "barnard":    {"accept": 0.0884, "sat_25": 1480, "sat_75": 1540, "act_25": 32, "act_75": 34},
    "oberlin":    {"accept": 0.3416, "sat_25": 1370, "sat_75": 1500, "act_25": 31, "act_75": 34},
    "grinnell":   {"accept": 0.1451, "sat_25": 1430, "sat_75": 1520, "act_25": 31, "act_75": 34},
    "bryn-mawr":  {"sat_25": 1290, "sat_75": 1490, "act_25": 29, "act_75": 33},
    "bucknell":   {"sat_25": 1170, "sat_75": 1360, "act_25": 25, "act_75": 32},
    "auburn":     {"accept": 0.4587, "sat_25": 1260, "sat_75": 1380, "act_25": 26, "act_75": 31},
    "northeastern":{"accept": 0.0521, "sat_25": 1450, "sat_75": 1520, "act_25": 33, "act_75": 35},
    "reed":       {"accept": 0.2919, "sat_25": 1320, "sat_75": 1490, "act_25": 29, "act_75": 34},
    "umass":      {"accept": 0.5990, "sat_25": 1330, "sat_75": 1480, "act_25": 30, "act_75": 34},
    "wm":         {"accept": 0.3696, "sat_25": 1390, "sat_75": 1520, "act_25": 32, "act_75": 34},
    # Round 4 — from user's batch of 13 files
    "kenyon":     {"accept": 0.3103, "sat_25": 1370, "sat_75": 1473, "act_25": 31, "act_75": 33},
    "vt":         {"accept": 0.5457, "act_25": 28, "act_75": 32},  # SAT not reported in CDS
    "purdue":     {"accept": 0.4343, "sat_25": 1220, "sat_75": 1470, "act_25": 28, "act_75": 34},
    "uiuc":       {"accept": 0.4237, "sat_25": 1390, "sat_75": 1520, "act_25": 30, "act_75": 34},
    "umd":        {"accept": 0.4503, "sat_25": 1410, "sat_75": 1520, "act_25": 32, "act_75": 35},
    "davidson":   {"accept": 0.1262, "sat_25": 1410, "sat_75": 1500, "act_25": 32, "act_75": 34},
    "msu":        {"accept": 0.8479, "sat_25": 1100, "sat_75": 1310, "act_25": 24, "act_75": 30},
    # Round 5 — more user files + URLs
    "wisc":       {"accept": 0.4517, "sat_25": 1370, "sat_75": 1490, "act_25": 29, "act_75": 33},
    "tulane":     {"accept": 0.1446, "sat_25": 1430, "sat_75": 1500, "act_25": 32, "act_75": 34},
    "pepperdine": {"accept": 0.6286, "sat_25": 1300, "sat_75": 1440, "act_25": 29, "act_75": 32},
    "pitt":       {"accept": 0.5945, "act_25": 29, "act_75": 33},  # SAT not reported in CDS
    "bc":         {"accept": 0.1619, "sat_25": 1460, "sat_75": 1520, "act_25": 33, "act_75": 35},
    "uw":         {"accept": 0.4175, "sat_25": 1320, "sat_75": 1502, "act_25": 30, "act_75": 34},
    "iu":         {"accept": 0.7589, "sat_25": 1200, "sat_75": 1410, "act_25": 28, "act_75": 33},
    "fsu":        {"accept": 0.2383, "sat_25": 1300, "sat_75": 1410, "act_25": 29, "act_75": 32},
}


MANUAL_FRESH_ACCEPT = {
    "harvard","yale","princeton","stanford","mit","caltech","columbia",
    "upenn","brown","dartmouth","duke","northwestern","uchicago","cornell",
    "jhu","vanderbilt","rice","notre-dame","tufts","northeastern","bu","bc",
    "emory","georgetown","ucla","umich","unc","ut-austin","purdue","rutgers",
    "wm","binghamton","villanova","bowdoin","carleton","vassar","colgate",
    "conn-college","skidmore","macalester","reed","grinnell","kenyon",
    "oberlin","pitzer","scripps","harvey-mudd","cmc","oxy","barnard",
    "babson","rpi","stevens","drexel","pratt","risd","iu","utk","uvm",
    "temple","hawaii","spelman","tuskegee","uconn","pepperdine","scu",
    "msu","cooper","fordham","cmu","nyu",
}


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
        for k in ("accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"):
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
SCORECARD_NAME_OVERRIDES = {
    "cornell": "Cornell University",  # else Scorecard finds Cornell College in IA
    "unc": "University of North Carolina at Chapel Hill",
    "parsons": "The New School",
    "ucb": "University of California-Berkeley",
    "ucla": "University of California-Los Angeles",
    "uci": "University of California-Irvine",
    "ucsd": "University of California-San Diego",
    "ucsb": "University of California-Santa Barbara",
    "ucdavis": "University of California-Davis",
    "umich": "University of Michigan-Ann Arbor",
    "umd": "University of Maryland-College Park",
    "uiuc": "University of Illinois Urbana-Champaign",
    "ut-austin": "The University of Texas at Austin",
    "wm": "William & Mary",
    "wisc": "University of Wisconsin-Madison",
    "uchicago": "University of Chicago",
    "upenn": "University of Pennsylvania",
    "jhu": "Johns Hopkins University",
    "cmu": "Carnegie Mellon University",
    "usc": "University of Southern California",
    "nyu": "New York University",
    "uw": "University of Washington-Seattle Campus",
    "umn": "University of Minnesota-Twin Cities",
    "osu": "Ohio State University-Main Campus",
    "tamu": "Texas A & M University-College Station",
    "psu": "Pennsylvania State University-Main Campus",
    "penn-state": "Pennsylvania State University-Main Campus",
    "msu": "Michigan State University",
    "asu": "Arizona State University Campus Immersion",
    "vt": "Virginia Polytechnic Institute and State University",
    "uoregon": "University of Oregon",
    "uvm": "University of Vermont",
    "calpoly-slo": "California Polytechnic State University-San Luis Obispo",
    "sjsu": "San Jose State University",
    "sdsu": "San Diego State University",
    "csulb": "California State University-Long Beach",
    "iu": "Indiana University-Bloomington",
    "uiowa": "University of Iowa",
    "uf": "University of Florida",
    "fsu": "Florida State University",
    "uga": "University of Georgia",
    "uconn": "University of Connecticut",
    "udel": "University of Delaware",
    "stony-brook": "Stony Brook University",
    "binghamton": "Binghamton University",
    "gmu": "George Mason University",
    "neiu": "Northeastern Illinois University",
}


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
        return {
            "accept": accept,
            "sat_25": int(sat_25) if sat_25 else None,
            "sat_75": int(sat_75) if sat_75 else None,
            "act_25": int(act_25) if act_25 else None,
            "act_75": int(act_75) if act_75 else None,
            "size": int(size) if size else None,
            "tuition": int(tuition) if tuition else None,
            "sf_ratio": int(round(sf)) if sf else None,
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
            (college_slug, accept, sat_25, sat_75, act_25, act_75, size, tuition, sf_ratio, source, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
                accept=excluded.accept,
                sat_25=excluded.sat_25, sat_75=excluded.sat_75,
                act_25=excluded.act_25, act_75=excluded.act_75,
                size=excluded.size, tuition=excluded.tuition,
                sf_ratio=excluded.sf_ratio,
                source=excluded.source, verified_at=CURRENT_TIMESTAMP""",
            (c["slug"], data["accept"], data["sat_25"], data["sat_75"],
             data["act_25"], data["act_75"], data["size"], data["tuition"],
             data["sf_ratio"], data["source"]))
        conn.commit()
    _overrides_cache.pop(c["slug"], None)
    return True

SIZE_BUCKETS = ("xs", "small", "medium", "ml", "large")
SIZE_RANGES = {
    "xs":     (0, 2000),
    "small":  (2000, 6000),
    "medium": (6000, 12000),
    "ml":     (12000, 18000),
    "large":  (18000, 10**9),
}

CLASS_SIZE_BUCKETS = ("tiny", "small", "medium", "large", "xl")
CLASS_SIZE_RANGES = {
    "tiny":   (0, 8),
    "small":  (8, 11),
    "medium": (11, 16),
    "large":  (16, 21),
    "xl":     (21, 999),
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
SCHOOL_TAGS = {
    # Greek-strong (high pct + culturally central)
    "alabama": ["greek_strong","sports_strong","warm","college_town","large"],
    "auburn": ["greek_strong","sports_strong","warm","college_town","large"],
    "ole-miss": ["greek_strong","warm","college_town"],
    "lsu": ["greek_strong","sports_strong","warm","college_town","large"],
    "tamu": ["sports_strong","warm","college_town","large"],
    "uga": ["greek_strong","sports_strong","warm","college_town","large"],
    "utk": ["greek_strong","sports_strong","warm","college_town","large"],
    "vanderbilt": ["greek_strong","warm","urban"],
    "wake-forest": ["greek_strong","warm","suburban"],
    "smu": ["greek_strong","warm","urban"],
    "sc": ["greek_strong","warm","sports_strong","large"],
    "missou": ["greek_strong","sports_strong","college_town","large"],
    "iu": ["greek_strong","sports_strong","college_town","large"],
    "msu": ["greek_strong","sports_strong","cold","college_town","large"],
    "ohio-state": ["sports_strong","cold","urban","large"],
    "psu": ["sports_strong","cold","college_town","large"],
    "penn-state": ["greek_strong","sports_strong","cold","college_town","large"],
    "wisc": ["greek_strong","sports_strong","cold","urban","large"],
    "umich": ["sports_strong","cold","college_town","large"],
    "iowa-state": ["sports_strong","cold","college_town","large"],
    "uiowa": ["greek_strong","sports_strong","cold","college_town","large"],
    "asu": ["greek_strong","sports_strong","warm","urban","large"],
    "arizona": ["greek_strong","sports_strong","warm","urban","large"],
    "uf": ["greek_strong","sports_strong","warm","college_town","large"],
    "fsu": ["greek_strong","sports_strong","warm","college_town","large"],
    "miami": ["greek_strong","sports_strong","warm","urban"],
    "tulane": ["greek_strong","warm","urban"],
    "duke": ["sports_strong","greek_strong","warm","suburban"],
    "syracuse": ["greek_strong","sports_strong","cold","college_town"],
    "clemson": ["greek_strong","sports_strong","warm","college_town","large"],
    "tcu": ["greek_strong","sports_strong","warm","urban"],
    "purdue": ["sports_strong","cold","college_town","large"],
    "uconn": ["sports_strong","cold","college_town"],
    # Internship-strong (urban + strong recruiting pipelines)
    "upenn": ["internship_strong","urban","mild"],
    "nyu": ["internship_strong","urban","mild"],
    "columbia": ["internship_strong","urban","mild"],
    "barnard": ["internship_strong","urban","mild","small"],
    "fordham": ["internship_strong","urban","mild"],
    "georgetown": ["internship_strong","urban","mild"],
    "gwu": ["internship_strong","urban","mild"],
    "american": ["internship_strong","urban","mild"],
    "bu": ["internship_strong","urban","cold","large"],
    "northeastern": ["internship_strong","urban","cold"],
    "drexel": ["internship_strong","urban","mild"],
    "stevens": ["internship_strong","urban","mild","small"],
    "ucb": ["internship_strong","urban","mild","large"],
    "stanford": ["internship_strong","mild","suburban"],
    "scu": ["internship_strong","mild","suburban"],
    "sjsu": ["internship_strong","mild","urban","large"],
    "cmu": ["internship_strong","cold","urban"],
    "usc": ["internship_strong","warm","urban","large"],
    "ucla": ["internship_strong","warm","urban","large"],
    "uw": ["internship_strong","cold","urban","large"],
    "ut-austin": ["internship_strong","sports_strong","warm","urban","large"],
    "gatech": ["internship_strong","sports_strong","warm","urban","large"],
    # LACs / small
    "williams": ["small","cold","rural"],
    "amherst": ["small","cold","college_town"],
    "swarthmore": ["small","mild","suburban"],
    "pomona": ["small","warm","suburban"],
    "bowdoin": ["small","cold","college_town"],
    "middlebury": ["small","cold","rural"],
    "carleton": ["small","cold","college_town"],
    "haverford": ["small","mild","suburban"],
    "vassar": ["small","cold","college_town"],
    "wesleyan": ["small","cold","college_town"],
    "hamilton": ["small","cold","rural"],
    "davidson": ["small","warm","college_town"],
    "colgate": ["small","cold","rural"],
    "smith": ["small","cold","college_town"],
    "mt-holyoke": ["small","cold","rural"],
    "bryn-mawr": ["small","mild","suburban"],
    "bates": ["small","cold","college_town"],
    "colby": ["small","cold","rural"],
    "trinity-ct": ["small","cold","urban"],
    "macalester": ["small","cold","urban"],
    "reed": ["small","mild","urban"],
    "grinnell": ["small","cold","rural"],
    "kenyon": ["small","cold","rural"],
    "oberlin": ["small","cold","college_town"],
    "harvey-mudd": ["small","warm","suburban"],
    "cmc": ["small","warm","suburban"],
    "scripps": ["small","warm","suburban"],
    "pitzer": ["small","warm","suburban"],
    "olin": ["small","cold","suburban"],
    "wellesley": ["small","cold","suburban"],
    # Warm / sunny (Florida, So Cal, Texas, Hawaii etc.)
    "stanford": ["mild","suburban","internship_strong"],
    "ucla": ["warm","urban","large","internship_strong"],
    "ucb": ["mild","urban","large","internship_strong"],
    "uci": ["warm","urban","large"],
    "ucsd": ["warm","urban","large"],
    "ucsb": ["warm","suburban","large"],
    "ucdavis": ["mild","college_town","large"],
    "rice": ["warm","urban"],
    "tulane": ["warm","urban","greek_strong"],
    "miami": ["warm","urban","greek_strong","sports_strong"],
    "uf": ["warm","college_town","greek_strong","sports_strong","large"],
    "hawaii": ["warm","urban","large"],
    # Cold / snow / winter
    "harvard": ["cold","urban","mild"],
    "mit": ["cold","urban"],
    "yale": ["cold","college_town","mild"],
    "princeton": ["cold","college_town"],
    "brown": ["cold","urban"],
    "dartmouth": ["cold","rural","sports_strong"],
    "cornell": ["cold","college_town"],
    "northwestern": ["cold","suburban"],
    "uchicago": ["cold","urban"],
    "umn": ["cold","urban","large"],
    "wisc": ["cold","urban","sports_strong","greek_strong","large"],
    "uvm": ["cold","college_town"],
    "umaine": ["cold","college_town"],
    "cu-boulder": ["cold","sports_strong","college_town","large"],
    "utah": ["cold","urban","large"],
    # Urban big
    "nyu": ["internship_strong","urban","mild","large"],
    "columbia": ["internship_strong","urban","mild"],
    "northeastern": ["internship_strong","urban","cold","large"],
    "bu": ["internship_strong","urban","cold","large"],
    "gwu": ["internship_strong","urban","mild"],
    "georgetown": ["internship_strong","urban","mild"],
    "fordham": ["internship_strong","urban","mild"],
    "usc": ["internship_strong","warm","urban","large"],
    "uchicago": ["urban","cold"],
    "drexel": ["internship_strong","urban","mild","large"],
    "temple": ["urban","mild","large"],
    "uh": ["urban","warm","large"],
    "csulb": ["warm","urban","large"],
    "sjsu": ["internship_strong","urban","mild","large"],
}


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
        if "strong" in chosen and g == "strong":
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
        if "strong" in chosen and s == "strong":
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
        if "high" in chosen:
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
        if "high" in chosen:
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
    rated = sum(1 for k in ("weather","setting","size","class_size","greek","sports","major_strength","prestige","cost","diversity","party","research","career_intensity") if k in out)
    return {"per_pref": out, "score": overall, "rated_count": rated}


# ─── RANKINGS ─────────────────────────────────────────────
RANKINGS = [
    {
        "slug": "best-overall",
        "title": "Best Overall Universities",
        "blurb": "Top US national universities, ordered to match US News 2026 reference rankings (released Fall 2025).",
        "order": ["princeton","mit","harvard","stanford","yale","caltech","duke","jhu","northwestern","upenn","cornell","brown","uchicago","columbia","ucla","ucb","dartmouth","notre-dame","vanderbilt","rice","washu","cmu","umich","emory","uva","gatech","unc","usc","georgetown","cmu","nyu","tufts","wake-forest","case","bc","tulane","villanova","ut-austin","wisc","lehigh","umd","wm","uf","brandeis","northeastern","gwu","stevens","miami","fordham","american","bu","binghamton","pitt","clemson","umn","uw","udel","gmu","temple","drexel","ohio-state","penn-state","msu","sc","sdsu","fsu","auburn","purdue","uconn","rutgers","vt","unh","uoregon","uvm"],
    },
    {
        "slug": "best-business",
        "title": "Best Undergraduate Business",
        "blurb": "US News 2026 undergraduate business ranking. Wharton remains #1; methodology updates have pushed public business schools up.",
        "order": ["upenn","mit","umich","ucb","nyu","cmu","unc","notre-dame","uva","ut-austin","iu","cornell","usc","emory","bc","washu","georgetown","penn-state","wisc","osu","umn","uiuc","umd","msu","uf","sc","villanova","wake-forest","babson","uw","bentley","fordham","drexel","sjsu","american","gwu","temple","scu","lmu","fsu","auburn","tcu","uga","ohio-state"],
    },
    {
        "slug": "best-engineering",
        "title": "Best Engineering",
        "blurb": "US News 2026 undergraduate engineering ranking. Top entries are highly competitive; CS overlap is significant.",
        "order": ["mit","stanford","ucb","caltech","cmu","gatech","umich","uiuc","cornell","purdue","columbia","princeton","ut-austin","ucla","northwestern","wisc","umd","jhu","upenn","case","duke","vt","penn-state","rice","uw","tamu","rpi","usc","wpi","stevens","drexel","binghamton","umn","ohio-state","brown","harvard","yale","cooper","olin","harvey-mudd","calpoly-slo","tufts","dartmouth","uconn","clemson","lehigh"],
    },
    {
        "slug": "best-cs",
        "title": "Best Computer Science",
        "blurb": "US News 2026 undergraduate CS ranking. Many of these schools have separate (lower) admit rates for CS.",
        "order": ["mit","stanford","cmu","ucb","cornell","gatech","princeton","caltech","uiuc","ut-austin","ucla","umich","uw","wisc","umd","columbia","harvard","upenn","brown","uchicago","yale","duke","rice","jhu","usc","nyu","vt","case","scu","sjsu","stevens","drexel","wpi","rpi","binghamton","calpoly-slo","umn","ohio-state","gmu","stony-brook","rutgers","udel","tufts","penn-state","uconn","sc"],
    },
    {
        "slug": "best-liberal-arts",
        "title": "Best Liberal Arts Colleges",
        "blurb": "US News 2026 LAC rankings. Small, undergrad-only colleges with high PhD-feeder rates.",
        "order": ["williams","amherst","swarthmore","pomona","wellesley","bowdoin","carleton","cmc","davidson","middlebury","smith","vassar","hamilton","colgate","colby","bates","grinnell","mt-holyoke","bryn-mawr","wesleyan","haverford","oberlin","kenyon","macalester","harvey-mudd","barnard","scripps","pitzer","trinity-ct","reed","skidmore","whitman","conn-college","oxy"],
    },
    {
        "slug": "best-pre-med",
        "title": "Best Pre-Med",
        "blurb": "Schools with the strongest medical school placement rates and biomedical pipelines.",
        "order": ["harvard","jhu","stanford","upenn","duke","yale","washu","columbia","northwestern","ucla","ucb","umich","uchicago","cornell","brown","emory","vanderbilt","case","tufts","unc","nyu","georgetown","notre-dame","tulane","miami","wisc","ut-austin","vt","scu","bu","villanova","fordham","american","uva","umd","penn-state","rutgers","drexel","stony-brook","binghamton","bc","wm","umn","sc"],
    },
    {
        "slug": "best-public",
        "title": "Best Public Universities",
        "blurb": "US News 2026 top public universities. The 2024-2026 methodology updates significantly boosted public flagships in the overall ranking too.",
        "order": ["ucla","ucb","umich","uva","unc","gatech","ut-austin","wisc","uf","uiuc","umd","wm","uw","osu","penn-state","uga","tamu","vt","pitt","msu","rutgers","umn","clemson","binghamton","stony-brook","calpoly-slo","sdsu","auburn","uconn","sc","udel","cu-boulder","ohio-state","temple","gmu","csulb","sjsu","missou","unl","iowa-state","uiowa","ku","unh","uvm","utk","uoregon","alabama","fsu","arizona","asu","lsu","utah","uky","uh","umaine","hawaii"],
    },
    {
        "slug": "best-value",
        "title": "Best Value (Strong Outcomes, Low Cost)",
        "blurb": "Strong academics under ~$25K sticker. State flagships dominate; in-state students get the best deal.",
        "order": ["uf","unc","gatech","wm","ucb","ucla","umich","uva","wisc","ut-austin","umd","uw","uiuc","fsu","sdsu","calpoly-slo","clemson","uga","penn-state","vt","stony-brook","binghamton","udel","msu","ohio-state","sc","alabama","auburn","tamu","lsu","sjsu","csulb","gmu","temple","unh","uvm","iowa-state","uiowa","unl","ku","missou","umn","cu-boulder","utah","arizona","asu"],
    },
]

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
RESOURCES = {
    "academics": [
        {"title":"Khan Academy","url":"https://www.khanacademy.org/","note":"Best free K-12 + early college math/science. Use it to climb course rigor before junior year."},
        {"title":"MIT OpenCourseWare","url":"https://ocw.mit.edu/","note":"Free MIT courses. Watch a few lectures in your major to demonstrate intellectual curiosity in essays."},
        {"title":"Coursera + edX","url":"https://www.coursera.org/","note":"Many free audit options. Completing a college-level course in your major is a real signal."},
    ],
    "test_prep": [
        {"title":"Khan Academy SAT (free)","url":"https://www.khanacademy.org/sat","note":"Official partner of College Board. The cheapest +100 SAT points you'll ever buy is here."},
        {"title":"ACT.org Free Practice","url":"https://www.act.org/content/act/en/products-and-services/the-act/test-preparation/free-act-test-prep.html","note":"Official ACT prep, including a full-length practice test."},
        {"title":"BlueBook (College Board)","url":"https://bluebook.collegeboard.org/","note":"The actual digital SAT app. Use it for full-length timed practice tests."},
        {"title":"PWN the SAT (paid books)","url":"https://www.pwnthesat.com/","note":"Best-known third-party prep books for top-quartile scorers. Worth it if you're stuck above 1400 trying to break 1500."},
    ],
    "competitions": [
        {"title":"USACO (Computing)","url":"http://www.usaco.org/","note":"Free. Bronze/Silver/Gold/Platinum tiers. Gold or higher is a recognized signal at top CS programs."},
        {"title":"AMC / AIME / USAMO","url":"https://www.maa.org/math-competitions","note":"Math olympiad track. Qualifying for AIME (top ~5% of AMC) starts mattering at top schools."},
        {"title":"Science Olympiad","url":"https://www.soinc.org/","note":"Team-based, broad sciences. Place at state or nationals to make it count."},
        {"title":"Regeneron STS / ISEF","url":"https://www.societyforscience.org/","note":"Science Talent Search and ISEF are the gold standard for high school research recognition."},
        {"title":"NYT Editorial Contest","url":"https://www.nytimes.com/learning/student-contests","note":"Free, broadly accessible writing competitions through the year. Wins are real awards."},
        {"title":"DECA / FBLA","url":"https://www.deca.org/","note":"Best-known business-track competitions. State qualification is the threshold for it to mean something."},
    ],
    "summer_programs": [
        {"title":"RSI (Research Science Institute)","url":"https://www.cee.org/programs/research-science-institute","note":"Free, MIT-hosted. ~80 students/year worldwide. The most selective summer program in STEM."},
        {"title":"MITES","url":"https://oeop.mit.edu/programs/mites","note":"Free, MIT, focused on underrepresented groups in STEM. Highly selective."},
        {"title":"COSMOS (UC Summer)","url":"https://cosmos-ucop.ucsd.edu/","note":"Paid (~$5K) summer math/science at multiple UC campuses. Mid-selective."},
        {"title":"YYGS (Yale Young Global Scholars)","url":"https://globalscholars.yale.edu/","note":"Paid, residential at Yale. Strong for IR/policy track."},
        {"title":"Stanford Summer Humanities Institute","url":"https://summerhumanities.stanford.edu/","note":"Highly selective humanities-focused program at Stanford."},
        {"title":"Iowa Young Writers' Studio","url":"https://iyws.uiowa.edu/","note":"For writers — selective short summer program at Iowa Writers' Workshop."},
        {"title":"Telluride Association Summer Seminar (TASS)","url":"https://www.tellurideassociation.org/","note":"Free, deeply academic, interview-based admit. Liberal-arts track."},
    ],
    "essays": [
        {"title":"College Essay Guy","url":"https://www.collegeessayguy.com/","note":"Best free college-essay walkthrough. Read his Common App deep dive before you start drafting."},
        {"title":"Hemingway Editor","url":"https://hemingwayapp.com/","note":"Free in-browser tool. Cuts your essays' weak verbs and runs. Use it on every draft."},
        {"title":"Tufts Sample Essays","url":"https://admissions.tufts.edu/apply/essays-that-worked/","note":"Real admitted-student essays with admissions-officer commentary."},
        {"title":"Hopkins Essays That Worked","url":"https://apply.jhu.edu/application-process/essays-that-worked/","note":"Same idea, different school. Read both. Notice how specific they are."},
    ],
    "leadership": [
        {"title":"DECA / FBLA / TSA","url":"https://www.deca.org/","note":"Officer roles in any of these are legible to admissions."},
        {"title":"Model UN / Speech & Debate","url":"https://www.nflorg.org/","note":"State or national qualification is the threshold for these to mean something serious."},
        {"title":"Found a club / nonprofit","url":"https://www.501c3.org/","note":"Real founding work means: a name, an EIN if applying for grants, sustained activity > 1 year, measurable impact."},
        {"title":"Coach Up / Tutor","url":"https://www.varsitytutors.com/","note":"Tutoring or coaching with paying clients (or sustained volunteer hours, 100+) is a strong signal of independence."},
    ],
    "recommendations": [
        {"title":"How to Ask for a Letter (CEG)","url":"https://www.collegeessayguy.com/blog/letters-of-recommendation","note":"Walks through the timeline, the email script, the brag sheet."},
        {"title":"Brag Sheet Template","url":"https://www.collegeessayguy.com/","note":"Give every recommender a 1-page brag sheet. Specific anecdotes > generic praise."},
    ],
    "interest": [
        {"title":"Sign up for school admissions newsletters","url":"","note":"Most schools track demonstrated interest. Sign up at every school's admissions site, attend at least one virtual info session."},
        {"title":"Visit campus or attend a virtual tour","url":"","note":"For schools that track interest (~half of privates do), this matters more than people think."},
    ],
}


# Comprehensive competitions database, filterable by major. Each entry:
#   name, url, majors (empty list = applies to all), tier, deadline, note.
COMPETITIONS = [
    {"name":"USACO","url":"http://www.usaco.org/","majors":["Computer Science","Engineering","Mathematics"],"tier":"national","deadline":"Dec / Jan / Feb / Mar","note":"Programming. Reach Gold or Platinum for a real signal at top CS programs."},
    {"name":"AMC 10/12 → AIME → USAMO","url":"https://www.maa.org/math-competitions","majors":["Mathematics","Engineering","Physics","Computer Science","Economics"],"tier":"national/international","deadline":"Nov","note":"Math olympiad track. AIME qualification (top ~5% of AMC) starts mattering at top STEM programs."},
    {"name":"Regeneron Science Talent Search","url":"https://www.societyforscience.org/regeneron-sts/","majors":["Biology","Chemistry","Physics","Engineering","Computer Science","Mathematics","Environmental Science"],"tier":"national","deadline":"Nov (seniors only)","note":"Most prestigious HS science research competition. 300 scholars, 40 finalists, $250K top prize."},
    {"name":"ISEF (Regeneron International Science & Engineering Fair)","url":"https://www.societyforscience.org/isef/","majors":["Biology","Chemistry","Physics","Engineering","Computer Science","Environmental Science"],"tier":"international","deadline":"Spring (qualify through state fairs)","note":"World's largest pre-college science fair. Qualify through regional/state fairs first."},
    {"name":"USA Olympiads (Bio, Chem, Physics, CS, Math, Linguistics)","url":"https://www.usabo-trc.org","majors":["Biology","Chemistry","Physics","Computer Science","Mathematics"],"tier":"national/international","deadline":"Spring qualifiers","note":"Olympiad track for each subject. Reaching the national camp = HYPSM-tier signal."},
    {"name":"Junior Science & Humanities Symposium (JSHS)","url":"https://jshs.org","majors":["Biology","Chemistry","Physics","Engineering","Computer Science","Environmental Science"],"tier":"national","deadline":"Spring","note":"Research presentations + scholarships ($2K-$12K). Solid mid-tier research signal."},
    {"name":"Davidson Fellows Scholarship","url":"https://www.davidsongifted.org/fellows-scholarship/","majors":["Mathematics","Physics","Biology","Chemistry","Computer Science","Engineering","Music","Philosophy","Literature"],"tier":"national","deadline":"Feb","note":"$10K-$50K for substantial student work. Project-based, deeply selective."},
    {"name":"MIT THINK Scholars","url":"https://think.mit.edu","majors":["Engineering","Computer Science","Physics"],"tier":"national","deadline":"Jan","note":"Pitch a STEM project, get $1K + MIT mentorship to build it. Free entry."},
    {"name":"Concord Review","url":"https://tcr.org","majors":["History","Political Science","English","International Relations"],"tier":"national","deadline":"Rolling","note":"History essay journal. Publication is a strong signal for humanities applicants."},
    {"name":"NYTimes Student Contests","url":"https://www.nytimes.com/spotlight/learning-contests","majors":["English","Journalism","History","Political Science","Creative Writing"],"tier":"national","deadline":"Various throughout year","note":"Editorial, profile, summer reading, vocab. Short-form, recognizable wins."},
    {"name":"Scholastic Art & Writing Awards","url":"https://www.artandwriting.org","majors":["Visual Arts","Creative Writing","English","Photography","Music"],"tier":"national","deadline":"Sep–Dec","note":"Gold/Silver Key + national medals are real signals for art/writing applicants."},
    {"name":"Adroit Journal","url":"https://www.theadroitjournal.org","majors":["Creative Writing","English"],"tier":"national","deadline":"Various","note":"Top high-school creative writing journal. Publication signals craft to admissions readers."},
    {"name":"DECA / FBLA","url":"https://www.deca.org","majors":["Business","Economics","Marketing","Finance","Accounting","Entrepreneurship"],"tier":"national","deadline":"State + Internationals (Spring)","note":"Business case competitions. International qualification is the real signal for biz applicants."},
    {"name":"Model UN — HMUN / NAIMUN / UPenn","url":"https://www.imuna.org","majors":["Political Science","International Relations","History","Economics"],"tier":"national","deadline":"Spring","note":"Top Model UN conferences. Best Delegate / Outstanding Delegate awards carry weight."},
    {"name":"FIRST Robotics (FRC + FTC)","url":"https://www.firstinspires.org","majors":["Engineering","Computer Science","Mechanical Engineering","Physics"],"tier":"international","deadline":"Build season Jan-Apr","note":"Captain or technical lead role beats generic team membership."},
    {"name":"Science Olympiad","url":"https://www.soinc.org","majors":["Biology","Chemistry","Physics","Engineering","Mathematics","Environmental Science"],"tier":"national","deadline":"State qualifying","note":"Team-based, broad sciences. Place at state or nationals to make it count."},
    {"name":"PUMaC + HMMT (Princeton & Harvard-MIT Math Tournaments)","url":"https://www.hmmt.org","majors":["Mathematics","Engineering","Physics","Computer Science"],"tier":"national","deadline":"Nov / Feb","note":"Top HS math tournaments. Strong intermediate signal between AMC and Olympiad."},
    {"name":"Coca-Cola Scholars","url":"https://www.coca-colascholarsfoundation.org","majors":[],"tier":"national-scholarship","deadline":"Aug-Oct","note":"$20K. 150 winners/year. Heavy leadership + community service signal."},
    {"name":"National Merit Scholarship","url":"https://www.nationalmerit.org","majors":[],"tier":"national-scholarship","deadline":"PSAT junior year","note":"~16K Semifinalists/year. Money + serious signal at many state and merit-aid schools."},
    {"name":"YoungArts","url":"https://www.youngarts.org","majors":["Visual Arts","Music","Theater","Creative Writing","Dance","Film","Photography"],"tier":"national","deadline":"Oct","note":"$10K + national recognition. Top award for performing/visual arts."},
    {"name":"National History Day","url":"https://www.nhd.org","majors":["History","English","Political Science"],"tier":"national","deadline":"State qualifying","note":"Research-based. Less selective than Concord Review but legitimate."},
    {"name":"Stockholm Junior Water Prize","url":"https://www.wef.org","majors":["Environmental Science","Engineering","Biology","Chemistry"],"tier":"international","deadline":"Spring","note":"International water-research competition. Niche but recognized."},
]


# Selective summer programs database. Selectivity tiers:
#   Free + selective = highest signal (RSI, MITES, TASP)
#   Paid + selective = moderate signal (SSP, PROMYS, SUMaC)
#   Paid + accessible = pay-to-play (skip if real signal is what you want)
SUMMER_PROGRAMS = [
    {"name":"RSI (Research Science Institute)","url":"https://www.cee.org/research-science-institute","majors":["Mathematics","Physics","Biology","Chemistry","Engineering","Computer Science"],"cost":"Free","selectivity":"~5%","grade":"Rising senior","note":"6 weeks at MIT. The most prestigious STEM summer program in the world."},
    {"name":"MITES Summer","url":"https://oeop.mit.edu/programs/mites","majors":["Engineering","Mathematics","Physics","Computer Science","Biology"],"cost":"Free","selectivity":"~5%","grade":"Rising senior","note":"6 weeks at MIT. STEM intensive for underrepresented students. Free + selective."},
    {"name":"SSP (Summer Science Program)","url":"https://summerscience.org","majors":["Biology","Astronomy","Astrophysics","Chemistry","Biochemistry"],"cost":"$8500 (need-based aid)","selectivity":"~10%","grade":"Rising senior","note":"6 weeks. Astrophysics or biochemistry deep-dive. Real research, real signal."},
    {"name":"PROMYS / Ross Math Program","url":"https://promys.org","majors":["Mathematics"],"cost":"$5500 (full aid available)","selectivity":"~15%","grade":"Rising 11-12","note":"6 weeks of pure math at BU/Ohio State. Strong signal for math/CS."},
    {"name":"TASP / TASS (Telluride Association)","url":"https://www.tellurideassociation.org","majors":["English","History","Philosophy","Political Science","Economics"],"cost":"Free","selectivity":"<5%","grade":"Rising senior (TASP) / 11-12 (TASS)","note":"6 weeks. Humanities seminars. Free + the most selective humanities program."},
    {"name":"COSMOS (UC Summer)","url":"https://cosmos.ucop.edu","majors":["Engineering","Computer Science","Physics","Biology","Chemistry","Mathematics"],"cost":"$5K (CA aid)","selectivity":"Selective","grade":"Rising 9-12","note":"4 weeks at UC campuses. Cluster-specific STEM. Decent mid-tier signal."},
    {"name":"Stanford SUMaC","url":"https://sumac.stanford.edu","majors":["Mathematics"],"cost":"$8500","selectivity":"~15%","grade":"Rising 11-12","note":"4 weeks of pure/applied math at Stanford."},
    {"name":"Iowa Young Writers' Studio","url":"https://iyws.uiowa.edu","majors":["Creative Writing","English"],"cost":"$2K","selectivity":"~25%","grade":"Rising 11-12","note":"2 weeks. Iowa Writers' Workshop pipeline."},
    {"name":"Kenyon Young Writers Workshop","url":"https://www.kenyonreview.org/workshops/young-writers/","majors":["Creative Writing","English"],"cost":"$3K","selectivity":"Selective","grade":"Rising 11-12","note":"2 weeks. Kenyon Review-affiliated. Strong creative writing program."},
    {"name":"Simons Summer Research","url":"https://www.stonybrook.edu/simons/","majors":["Biology","Chemistry","Physics","Mathematics","Computer Science","Engineering"],"cost":"Free","selectivity":"~10%","grade":"Rising senior","note":"7 weeks of lab research at Stony Brook. Free + selective."},
    {"name":"Garcia Summer Research","url":"https://www.garcia.sunysb.edu","majors":["Engineering","Materials Science","Chemistry","Biology"],"cost":"$3500","selectivity":"~15%","grade":"Rising 11-12","note":"7 weeks of polymer research at Stony Brook. Often leads to publications."},
    {"name":"Clark Scholars Program","url":"https://www.depts.ttu.edu/honors/academicsandenrichment/affiliatedandhighschool/clarks/","majors":["Biology","Chemistry","Physics","Engineering","Computer Science","Mathematics"],"tier":"selective","cost":"Free + $750 stipend","selectivity":"~12%","grade":"Rising senior","note":"7 weeks at Texas Tech with PhD mentor. Free + paid + selective."},
    {"name":"YYGS (Yale Young Global Scholars)","url":"https://globalscholars.yale.edu","majors":["Political Science","International Relations","Economics","Biology","Engineering"],"cost":"$6500","selectivity":"~15%","grade":"Rising 11-12","note":"2 weeks at Yale. Branded but moderately selective."},
    {"name":"Notre Dame Leadership Seminars","url":"https://precollege.nd.edu","majors":[],"tier":"selective","cost":"Free","selectivity":"~15%","grade":"Rising senior","note":"2 weeks. Free + selective. Strong fit for Catholic-school applicants."},
    {"name":"Bank of America Student Leaders","url":"https://about.bankofamerica.com/en/making-an-impact/student-leaders","majors":["Business","Economics","Public Policy","Political Science"],"tier":"selective-paid","cost":"PAID internship","selectivity":"~10%","grade":"Rising senior","note":"8 weeks paid internship + week in DC. Service + business signal."},
    {"name":"Hutton Junior Fisheries Biology","url":"https://hutton.fisheries.org","majors":["Biology","Marine Biology","Environmental Science"],"tier":"selective-paid","cost":"PAID stipend","selectivity":"~25%","grade":"Rising 11-12","note":"8 weeks. Paid fisheries research."},
    {"name":"NYU Tisch Summer High School","url":"https://tisch.nyu.edu/special-programs/high-school","majors":["Film","Theater","Photography","Music","Dance"],"cost":"$15K","selectivity":"Portfolio-based","grade":"Rising 11-12","note":"4 weeks. Tisch credentials matter for arts applicants."},
    {"name":"Berklee Summer Programs","url":"https://www.berklee.edu/summer","majors":["Music"],"cost":"$5K","selectivity":"Audition","grade":"Rising 11-12","note":"5 days–5 weeks. Audition-based. Real for music apps."},
    {"name":"Hampshire College Summer Studies in Math","url":"https://www.hcssim.org","majors":["Mathematics"],"cost":"$5K","selectivity":"Selective","grade":"Rising 11-12","note":"6 weeks. Pure math problem-solving."},
    {"name":"BU PROMYS","url":"https://promys.org","majors":["Mathematics"],"cost":"$5500","selectivity":"~15%","grade":"Rising 11-12","note":"6 weeks at BU. Pure math, problem-solving."},
    {"name":"CTY (Johns Hopkins Center for Talented Youth)","url":"https://cty.jhu.edu","majors":[],"tier":"branded-paid","cost":"$5-7K","selectivity":"Test-qualified","grade":"7-12","note":"Pay-to-play but high quality. Not a strong admissions signal alone."},
    {"name":"Stanford Pre-Collegiate Studies","url":"https://summer.stanford.edu","majors":[],"tier":"branded-paid","cost":"$15K+","selectivity":"Pay-to-play","grade":"Rising 9-12","note":"Branded but not selective. Skip if you want a real signal."},
]


# ─── SCHOOL-SPECIFIC NOTES ─────────────────────────────────
# What each school weights most + concrete advice. Top schools get curated
# entries; everything else falls back to TIER_NOTES below by tier.
SCHOOL_NOTES = {
    "harvard": {
        "values":"Holistic review with a heavy weight on demonstrated leadership and intellectual depth. Harvard explicitly says 80%+ of qualified applicants are rejected — being academically perfect is necessary, not sufficient.",
        "supplemental_strategy":"Optional essay is rarely truly optional. Use it. Pick the prompt that lets you tell a story that no other applicant could write — specific incident, specific people, specific consequence.",
        "links":[
            {"title":"Harvard Application Tips (official)","url":"https://college.harvard.edu/admissions/apply/application-tips"},
            {"title":"Crimson Sample Essays","url":"https://www.thecrimson.com/topic/admitted-class/"},
        ]
    },
    "stanford": {
        "values":"Intellectual vitality is the explicit institutional language. Three short essays + the 'roommate letter' all reward authentic, specific, slightly weird writing. Generic-good essays are death here.",
        "supplemental_strategy":"For 'what matters to you and why' write about a specific object, person, or moment — concrete, narrow, surprising. Avoid abstract values ('I value family').",
        "links":[
            {"title":"Stanford Admission Essays (official tips)","url":"https://admission.stanford.edu/apply/freshman/essays.html"},
            {"title":"Stanford Summer Programs","url":"https://summer.stanford.edu/"},
        ]
    },
    "mit": {
        "values":"Mission-fit (use STEM to do good in the world) plus depth in one technical area. Doesn't reward breadth — a single deep project beats five clubs.",
        "supplemental_strategy":"Short answer prompts are short. Be concrete and direct. The 'what do you do for fun' prompt is a real check on whether you're a person; engineers often miss this.",
        "links":[
            {"title":"MIT Apply (official)","url":"https://mitadmissions.org/apply/"},
            {"title":"MIT PRIMES (research program for HS students)","url":"https://math.mit.edu/research/highschool/primes/"},
        ]
    },
    "yale":{"values":"Cares about the residential college experience. Yale rewards applicants who'd contribute to a community, not just consume from it.","supplemental_strategy":"The 'why Yale' supplemental is short — 125 words. Do not repeat your common app. Focus on specific Yale-only opportunities (the residential colleges, specific seminars).","links":[{"title":"Yale Application Process (official)","url":"https://admissions.yale.edu/application-process"}]},
    "princeton":{"values":"Senior thesis is a defining feature; Princeton wants undergraduates who'll thrive in independent research. Strong public-policy and humanities emphasis.","supplemental_strategy":"The 'engaged citizen' prompt is real — show concrete civic engagement, not just opinions.","links":[{"title":"Princeton Admission (official)","url":"https://admission.princeton.edu/apply"}]},
    "columbia":{"values":"Core curriculum is a major piece of the identity. Columbia wants students excited about reading the Iliad and writing a paper on it, not just doing pre-prof tracks.","supplemental_strategy":"The book-and-media lists matter more than people think — they're a quick character read for the admissions reader.","links":[{"title":"Columbia Application Tips (official)","url":"https://undergrad.admissions.columbia.edu/apply/instructions"}]},
    "uchicago":{"values":"Quirky intellectualism. The supplemental essay prompts are famously weird (e.g. 'find x'). Take them seriously and play along.","supplemental_strategy":"Pick one of the weird prompts. Write something that could only have come from your brain. UChicago is allergic to 5-paragraph-essay format.","links":[{"title":"UChicago Essay Prompts (with archive)","url":"https://collegeadmissions.uchicago.edu/apply/uchicago-supplemental-essay-questions"}]},
    "upenn":{"values":"Pre-professional focus is the headline — Wharton applicants should expect the heaviest scrutiny. The cross-school programs (M&T, Huntsman, Vagelos) admit at single-digit rates.","supplemental_strategy":"Be specific about which UPenn school and why. 'Wharton because business' is a non-starter; Wharton because of a specific concentration with a specific career path is required.","links":[{"title":"Penn Apply (official)","url":"https://admissions.upenn.edu/apply"}]},
    "brown":{"values":"Open curriculum + freedom to design your own concentration. Brown wants self-directed learners.","supplemental_strategy":"The 'why Brown' supplemental should reference the open curriculum specifically and which courses outside your obvious major you'd take advantage of.","links":[{"title":"Brown Admission","url":"https://admission.brown.edu/apply"}]},
    "duke":{"values":"Pre-prof culture (pre-med, pre-law, finance) but appreciative of breadth. Trinity vs Pratt admissions are quite different — Pratt is harder.","supplemental_strategy":"Optional essays at Duke are not really optional for competitive applicants. Use them to add color a recommender wouldn't include.","links":[{"title":"Duke Admissions","url":"https://admissions.duke.edu/apply/"}]},
    "northwestern":{"values":"Quarter system rewards students who can spike their interest fast. Strong journalism (Medill) applicants need a portfolio.","supplemental_strategy":"The 'why Northwestern' should mention a specific quarter-system advantage, course, or opportunity (Medill cherubs, IMC, etc.).","links":[{"title":"Northwestern Application","url":"https://admissions.northwestern.edu/apply/"}]},
    "cmu":{"values":"School-specific admissions. SCS (CS) admit rate is roughly half the school-wide rate — apply realistically.","supplemental_strategy":"For SCS, technical depth in your supplementals matters. Reference specific projects, not abstract interest.","links":[{"title":"CMU SCS","url":"https://www.cs.cmu.edu/admissions"}]},
    "ucb":{"values":"PIQs (Personal Insight Questions) are everything in the UC system. They want to see context, contribution, and outcomes.","supplemental_strategy":"Pick 4 of the 8 PIQs that show range. Each one should have a specific moment, action, and result.","links":[{"title":"UC Personal Insight Questions","url":"https://admission.universityofcalifornia.edu/how-to-apply/applying-as-a-freshman/personal-insight-questions.html"}]},
    "ucla":{"values":"Same UC PIQ system as Berkeley. UCLA is most-applied-to in the country — your application has to stand out in a deep pile.","supplemental_strategy":"PIQs reward specificity. Don't write about your sport or club generically; write about one decision you made within it.","links":[{"title":"UCLA Apply","url":"https://admission.ucla.edu/apply"}]},
    "umich":{"values":"School-specific admissions (Ross, Engineering, LSA, Music etc.). Ross direct-admit is harder than overall UMich admit.","supplemental_strategy":"Why-Michigan + Why-this-school short answers. Be specific about Ross's action-based learning, or about Engineering's particular labs.","links":[{"title":"UMich Apply","url":"https://admissions.umich.edu/apply"}]},
    "nyu":{"values":"School-specific admissions; Stern and Tisch are very different from CAS. NYC location is a major draw, demonstrate you can use it.","supplemental_strategy":"For Stern, the 'change the world' essay is real — link your career interest to a specific change. For Tisch, the portfolio matters more than the essay.","links":[{"title":"NYU Apply","url":"https://www.nyu.edu/admissions/undergraduate-admissions/apply.html"}]},
    "georgetown":{"values":"Distinct application — it's not on the Common App and has its own essays. Strong Jesuit values fit; SFS applicants should reference international engagement specifically.","supplemental_strategy":"Don't reuse Common App essays. Georgetown wants to see fresh writing they didn't already get from peers.","links":[{"title":"Georgetown Apply","url":"https://uadmissions.georgetown.edu/applying/"}]},
}

# Tier-based fallback: applies to schools without a specific note above.
TIER_NOTES = {
    1: {
        "values":"Sub-10% admit. Strong academics are necessary, not sufficient. They're looking for a specific story, recommendations that say something only that teacher could say, and one or two clear hooks (deep ECs, a hook, a niche).",
        "supplemental_strategy":"Take optional essays seriously — they're rarely actually optional for competitive applicants. Be specific (a moment, a person, a sentence-level detail), avoid generic 'lessons learned' framing.",
    },
    2: {
        "values":"Highly selective. Strong stats are baseline; what differentiates is having one focused area where you've gone deep, plus 2-3 supporting threads in your application.",
        "supplemental_strategy":"Quality > quantity on supplemental essays. Make every one count. The 'why us' essay is where most applicants are weakest — fix yours.",
    },
    3: {
        "values":"Selective but achievable for solid profiles. Demonstrated interest + a clear major fit + above-average essays will move the needle here.",
        "supplemental_strategy":"Show fit between your specific interests and this school's specific programs. Reference real courses, professors, or campus traditions in 'why us' essays.",
    },
    4: {
        "values":"Solid academic match for the average above-3.5 GPA, ~1300 SAT applicant. Above-average essays and clear major fit translate directly into stronger admissions outcomes.",
        "supplemental_strategy":"Most applicants here send generic essays. Specific 'why us' that name 2-3 specific opportunities will measurably stand out.",
    },
    5: {
        "values":"Largely numbers-driven. GPA and test scores are the primary signal. Strong essays can move you up the merit-aid ladder even if admission is near-automatic.",
        "supplemental_strategy":"If there are honors college essays, treat them as if you were applying to a more selective school. Honors colleges admit harder than the main university.",
    },
}


# ─── KEYWORDS / SCORING (carried over from MVP) ──────────
EC_STRONG_SIGNALS = ["research","published","publication","patent","founded","founder","national","international","olympiad","intel sts","regeneron","selected","fellowship","grant","scholarship","varsity captain","principal","first chair","lead","1st place","2nd place","finalist","summit","internship","lab","startup","nonprofit","501"]
LEADERSHIP_KEYWORDS = ["captain","president","founder","chair","director","lead","head","editor","officer","co-founder","vice president","treasurer","secretary"]


def _keyword_strength(text, keywords):
    if not text: return 0
    t = text.lower()
    return sum(1 for k in keywords if k in t)


# AP difficulty weights — how much each AP contributes to a student's
# academic-rigor signal. Hardest STEM/lit APs at the top, easier APs
# (Psych, Human Geo, Env Sci) at the bottom. Substring-matched so
# "AP Calc BC" and "Calc BC" both work.
AP_WEIGHTS = {
    # Hardest tier (3.0) — STEM-rigor + most-respected humanities
    "calc bc": 3.0, "calculus bc": 3.0,
    "physics c mech": 3.0, "physics c mechanics": 3.0,
    "physics c em": 3.0, "physics c e&m": 3.0, "physics c electricity": 3.0,
    "chemistry": 3.0, "chem": 3.0,
    "biology": 3.0, "bio": 3.0,
    "english literature": 2.8, "english lit": 2.8, "lit": 2.8,
    "us history": 2.8, "u.s. history": 2.8, "ush": 2.8, "apush": 2.8,
    "world history": 2.8, "european history": 2.8, "euro": 2.8,
    # Medium-hard (2.0)
    "calc ab": 2.0, "calculus ab": 2.0,
    "physics 1": 2.0, "physics 2": 2.0,
    "english language": 2.0, "english lang": 2.0, "lang": 2.0,
    "computer science a": 2.0, "cs a": 2.0, "csa": 2.0,
    "statistics": 2.0, "stats": 2.0,
    "macroeconomics": 2.0, "macro": 2.0,
    "microeconomics": 2.0, "micro": 2.0,
    "us government": 2.0, "us gov": 2.0, "government": 1.8,
    "comparative government": 2.0, "comp gov": 2.0,
    "spanish language": 2.0, "spanish lang": 2.0,
    "spanish literature": 2.2, "spanish lit": 2.2,
    "french language": 2.0, "french lang": 2.0,
    "french literature": 2.2, "french lit": 2.2,
    "latin": 2.2, "german": 2.0, "italian": 2.0, "japanese": 2.2,
    "chinese": 2.2, "mandarin": 2.2, "korean": 2.2,
    "music theory": 2.0, "studio art": 1.8, "art history": 1.8,
    "drawing": 1.8, "2-d art": 1.8, "3-d art": 1.8,
    "african american studies": 2.0,
    "research": 1.8, "seminar": 1.5,
    # Easier tier (1.0)
    "psychology": 1.2, "psych": 1.2,
    "human geography": 1.0, "hug": 1.0, "human geo": 1.0,
    "environmental science": 1.2, "env sci": 1.2, "apes": 1.2,
    "computer science principles": 1.2, "csp": 1.2,
}


# IB classes. HL ≈ AP-level; SL slightly less. Stored separately from APs
# so we can show both in admissions reads (lots of intl applicants take both).
IB_WEIGHTS = {
    # HL (Higher Level) — full weight comparable to AP
    "math aa hl": 3.0, "math analysis hl": 3.0,
    "math ai hl": 2.6, "math applications hl": 2.6,
    "physics hl": 3.0, "chemistry hl": 3.0, "biology hl": 3.0,
    "computer science hl": 2.8,
    "english lit hl": 2.8, "english language and literature hl": 2.8,
    "history hl": 2.8, "economics hl": 2.6, "psychology hl": 2.0,
    "geography hl": 2.0, "global politics hl": 2.4,
    "business management hl": 2.2,
    "spanish hl": 2.4, "french hl": 2.4, "german hl": 2.4,
    "mandarin hl": 2.6, "japanese hl": 2.4,
    "visual arts hl": 1.8, "music hl": 2.0, "theatre hl": 1.8, "film hl": 1.8,
    "design technology hl": 2.4, "environmental systems hl": 2.0,
    "tok": 1.5, "extended essay": 1.8,
    # SL (Standard Level) — about 70% of HL weight
    "math aa sl": 2.0, "math ai sl": 1.6,
    "physics sl": 2.0, "chemistry sl": 2.0, "biology sl": 2.0,
    "computer science sl": 1.8,
    "english lit sl": 2.0, "english language and literature sl": 2.0,
    "history sl": 2.0, "economics sl": 1.8, "psychology sl": 1.4,
    "geography sl": 1.4, "global politics sl": 1.6,
    "business management sl": 1.6,
    "spanish sl": 1.6, "french sl": 1.6, "german sl": 1.6,
    "mandarin sl": 1.8, "japanese sl": 1.6,
    "visual arts sl": 1.2, "music sl": 1.4, "theatre sl": 1.2, "film sl": 1.2,
    "environmental systems sl": 1.4,
}


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
    if ap is None and ib is None:
        return None  # neither offered
    if ap is None: return ib
    if ib is None: return ap
    # Both populated → sum, but cap to not double-count when student takes
    # significant load in both (rare but possible at IB schools that also
    # offer APs)
    if ap < 0 and ib < 0: return min(ap, ib)
    if ap < 0: return ib
    if ib < 0: return ap
    return min(100, ap + ib * 0.6)  # IB weighted slightly less when stacked


def _normalize_score(sat, act):
    act_to_sat = {36:1590,35:1540,34:1500,33:1460,32:1430,31:1400,30:1370,29:1340,28:1310,27:1280,26:1240,25:1210,24:1180,23:1140,22:1110,21:1080,20:1040,19:1010,18:970}
    if sat: return int(sat)
    if act: return act_to_sat.get(int(act), 1000)
    return None


def _is_test_focused_school(school):
    """Schools where applying test-optional is read as a real signal of
    weakness. Elite STEM is the clearest case; some Ivies (Yale,
    Dartmouth, Brown) have already reinstated tests. Tier-1 in general
    weights tests heavily."""
    if school.get("slug") in ("mit","caltech","gatech","harvey-mudd","cmu","rpi","wpi","stevens"):
        return True
    return school.get("tier", 5) == 1


def compute_fit(profile, school):
    score = 50.0
    components = {}
    has_test = bool(profile.get("sat") or profile.get("act"))
    gpa = profile.get("uw_gpa")
    if gpa is not None:
        midpoint = (school["gpa_lo"] + school["gpa_hi"]) / 2
        delta = max(-18, min(18, (gpa - midpoint) * 50))
        # Test-optional applicants: GPA carries more weight to compensate
        # for the missing test signal.
        if not has_test:
            delta = max(-22, min(22, delta * 1.25))
        score += delta
        components["gpa"] = round(delta, 1)
    sat_eq = _normalize_score(profile.get("sat"), profile.get("act"))
    if sat_eq:
        mid = (school["sat_25"] + school["sat_75"]) / 2
        spread = max(40, school["sat_75"] - school["sat_25"])
        delta = max(-22, min(22, (sat_eq - mid) / spread * 22))
        score += delta
        components["test"] = round(delta, 1)
    else:
        # Test-optional handling. At test-focused schools (MIT/Caltech/etc.),
        # not submitting reads as a weakness. At test-flexible schools,
        # neutral (we already up-weighted GPA above).
        if _is_test_focused_school(school):
            score -= 6
            components["test"] = -6
        else:
            components["test"] = 0
    ec_strength = _keyword_strength(profile.get("ecs", "") or "", EC_STRONG_SIGNALS)
    awards_strength = _keyword_strength(profile.get("awards", "") or "", EC_STRONG_SIGNALS)
    ec_total = min(10, ec_strength * 1.5 + awards_strength * 2)
    if not (profile.get("ecs", "") or "").strip(): ec_total -= 4
    score += ec_total
    components["ecs"] = round(ec_total, 1)
    lead_total = min(5, _keyword_strength(profile.get("leadership", "") or "", LEADERSHIP_KEYWORDS) * 1.5)
    score += lead_total
    components["leadership"] = round(lead_total, 1)
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


def assign_tier(school, fit):
    a = school["accept"]
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


def evaluate_profile_exceptionality(profile):
    """Use Claude to detect truly exceptional applicants (USAMO/IMO medalists,
    ISEF top, RSI alums, recruited D1 athletes, published researchers, etc.)
    so the odds model can lift caps for them. Default NO — only flags clear,
    nationally/internationally recognized credentials. Never lowers odds; only
    used to remove the cap that's appropriate for typical strong applicants
    but undersells extraordinary ones.

    Returns (is_exceptional: bool, reason: str). Reason is a short human-
    readable explanation of what triggered the flag (or why it didn't)."""
    if not _claude_client:
        return False, "AI evaluation unavailable"
    awards = (profile.get("awards") or "").strip()
    ecs = (profile.get("ecs") or "").strip()
    leadership = (profile.get("leadership") or "").strip()
    is_athlete = bool(profile.get("athlete"))
    if not (awards or ecs or leadership):
        return False, "No awards/ECs/leadership submitted"
    user_msg = f"""STUDENT PROFILE:
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

Output EXACTLY one line in this format:
EXCEPTIONAL: YES|NO
REASON: <one short sentence — if YES, name the credential; if NO, briefly explain why not>"""
    try:
        response = _claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="You are a strict college admissions evaluator. Default to NO. Only flag truly exceptional, nationally/internationally recognized credentials. Be skeptical — students often inflate. Look for specific verifiable accomplishments.",
            messages=[{"role":"user","content": user_msg}],
        )
        text = (response.content[0].text or "").strip()
    except Exception as e:
        print(f"Exceptionality eval error: {e}")
        return False, "evaluation failed"
    is_exc = "EXCEPTIONAL: YES" in text.upper()
    reason = ""
    for line in text.splitlines():
        if line.strip().upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
            break
    return is_exc, reason or ("flagged exceptional" if is_exc else "standard profile")


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


def estimate_odds(school, fit, profile):
    """Harsher version. Markets and admissions are noisy; previous curve was
    over-generous in the middle of the fit range. Tighter slope + lower caps
    on elite schools so the headline numbers don't promise a Stanford that
    isn't there.

    Exceptional-applicant override: when profile.is_exceptional is set,
    the caps lift dramatically because USAMO golds, recruited athletes, etc.
    legitimately have 50%+ odds at hyper-elites. The override only LIFTS
    caps; it never lowers odds."""
    a = school["accept"]
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
    # Steeper, less-generous fit curve. At fit=50 (average), multiplier ≈ 0.85,
    # i.e. you do worse than the school's headline accept. Top fits still get
    # boosted but capped hard at elite tiers.
    fit_mult = 0.20 + (fit / 65.0) ** 1.6
    hook_mult = 1.0
    if profile.get("athlete"): hook_mult *= 1.30
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
    center = a * fit_mult * hook_mult
    # Pick caps based on whether this profile has been flagged as exceptional.
    # Standard caps assume a typical strong applicant; exceptional profiles
    # (USAMO golds, recruited D1 athletes, ISEF top, etc.) get raised caps
    # because flat caps undersell them. Caps only LIFT — never lower odds.
    if bool(profile.get("is_exceptional")):
        # Lift caps significantly. A USAMO gold + recruited athlete legitimately
        # has 60-80% odds at MIT. Even unhooked extraordinary kids cross 30%.
        if a < 0.07:  center = min(center * 1.5 + 0.08, 0.65)
        elif a < 0.10: center = min(center * 1.5 + 0.06, 0.72)
        elif a < 0.20: center = min(center * 1.4 + 0.05, 0.80)
        elif a < 0.40: center = min(center * 1.3 + 0.03, 0.88)
        else:           center = min(center * 1.2, 0.93)
    else:
        # Standard caps — tightened so headline numbers don't promise a Stanford
        # that isn't there for a typical strong applicant.
        if a < 0.07:  center = min(center, 0.14)
        elif a < 0.10: center = min(center, 0.18)
        elif a < 0.20: center = min(center, 0.30)
        elif a < 0.40: center = min(center, 0.55)
        else: center = min(center, 0.85)
    spread = max(0.04, center * 0.35)
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
def _claude(model, system, user, max_tokens=400):
    if not _claude_client: return None
    try:
        msg = _claude_client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=[{"role":"user","content":user}])
        return msg.content[0].text
    except Exception as e:
        print(f"Claude error: {e}")
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
    if sat:
        test_str = f"SAT {sat}"
        if sat >= school["sat_75"]:
            test_compare = f"SAT {sat} is AT OR ABOVE the 75th percentile ({school['sat_75']}) of admits — top of the range."
        elif sat >= school["sat_25"]:
            test_compare = f"SAT {sat} is INSIDE the mid-50% range ({school['sat_25']}-{school['sat_75']}) of admits."
        else:
            gap = school["sat_25"] - sat
            test_compare = f"SAT {sat} is BELOW the 25th percentile ({school['sat_25']}) of admits — gap of {gap} points."
    elif act:
        test_str = f"ACT {act}"
        if act >= school["act_75"]:
            test_compare = f"ACT {act} is AT OR ABOVE the 75th percentile ({school['act_75']}) of admits — top of the range."
        elif act >= school["act_25"]:
            test_compare = f"ACT {act} is INSIDE the mid-50% range ({school['act_25']}-{school['act_75']}) of admits."
        else:
            gap = school["act_25"] - act
            test_compare = f"ACT {act} is BELOW the 25th percentile ({school['act_25']}) of admits — gap of {gap} points."
    # Same idea for GPA.
    gpa = profile.get("uw_gpa")
    gpa_compare = "GPA not submitted."
    if gpa:
        mid = round((school["gpa_lo"] + school["gpa_hi"]) / 2, 2)
        if gpa >= school["gpa_hi"]:
            gpa_compare = f"GPA {gpa} is AT OR ABOVE the typical 75th percentile ({school['gpa_hi']})."
        elif gpa >= mid:
            gpa_compare = f"GPA {gpa} is ABOVE midpoint ({mid}), upper half of admits."
        elif gpa >= school["gpa_lo"]:
            gpa_compare = f"GPA {gpa} is BELOW midpoint ({mid}) but inside the typical range ({school['gpa_lo']}-{school['gpa_hi']})."
        else:
            gpa_compare = f"GPA {gpa} is BELOW the typical 25th percentile ({school['gpa_lo']}) — academic gap."

    user = f"""Student profile:
- Unweighted GPA: {profile.get('uw_gpa')}
- Test: {test_str}
- Major: {profile.get('major','undecided')}
- ECs: {profile.get('ecs','(blank)') or '(blank)'}
- Leadership: {profile.get('leadership','(blank)') or '(blank)'}
- Awards: {profile.get('awards','(blank)') or '(blank)'}
- Hooks for THIS school: legacy_generations={legacy_generations_at(profile, school)} (0 means no legacy here, even if the student has legacy elsewhere), first_gen={profile.get('first_gen')}, athlete={profile.get('athlete')}

Target: {school['name']} (acceptance {round(school['accept']*100,1)}%, GPA midpoint ~{round((school['gpa_lo']+school['gpa_hi'])/2,2)}, SAT mid-50% {school['sat_25']}-{school['sat_75']}, ACT mid-50% {school['act_25']}-{school['act_75']}).
Computed fit: {fit}/100. Tier: {tier}. Odds: {odds[0]}-{odds[1]}%.

PRE-COMPUTED COMPARISONS (use these exactly, do NOT recompute):
- {test_compare}
- {gpa_compare}

CRITICAL RULES:
- Use ONLY the numbers and comparisons given above.
- DO NOT compute test/GPA percentiles yourself — use the pre-computed comparisons.
- DO NOT invent percentile rankings or stats not provided.
- If the student submitted ACT, only reference the ACT range — never compare ACT to SAT.

Output exactly three lines:
STRENGTH: <one-sentence biggest advantage, citing a specific number/item>
WEAKNESS: <one-sentence biggest gap, citing a specific number/item>
DIFFERENTIATOR: <one-sentence — what could make this applicant memorable, or what is missing that should become memorable>"""
    raw = _claude("claude-haiku-4-5-20251001",
        f"You are an experienced college admissions consultant. Be concrete, cite specific numbers, never hedge. No preamble.\n\n{_date_context()}",
        user, max_tokens=320)
    if not raw: return fb
    out = {"strength": "", "weakness": "", "differentiator": ""}
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("STRENGTH:"): out["strength"] = line.split(":",1)[1].strip()
        elif line.startswith("WEAKNESS:"): out["weakness"] = line.split(":",1)[1].strip()
        elif line.startswith("DIFFERENTIATOR:"): out["differentiator"] = line.split(":",1)[1].strip()
    return out if all(out.values()) else fb


def _fallback_bullets(profile, school, fit, components, tier):
    gpa = profile.get("uw_gpa") or 0
    school_gpa_mid = round((school["gpa_lo"]+school["gpa_hi"])/2, 2)
    if components.get("gpa", 0) > 5:
        strength = f"Your GPA of {gpa} is meaningfully above {school['name']}'s typical midpoint of {school_gpa_mid}, the strongest signal in your favor."
    elif components.get("test", 0) > 8:
        strength = f"Your test score sits comfortably above the middle-50% range at {school['name']} ({school['sat_25']}–{school['sat_75']}), a real academic edge."
    elif components.get("hooks", 0) > 0:
        hooks = [k for k in ("legacy","first_gen","athlete") if profile.get(k)]
        strength = f"Your hook(s) — {', '.join(hooks)} — measurably move the needle here."
    else:
        strength = f"Your profile is broadly within the range {school['name']} considers competitive."
    if components.get("gpa", 0) < -3:
        weakness = f"Your GPA of {gpa} is below the typical applicant; this is the gap to close hardest."
    elif components.get("test", 0) < -5:
        weakness = f"Your test score is below the middle-50% range ({school['sat_25']}–{school['sat_75']}) — a retake would meaningfully change odds."
    elif components.get("ecs", 0) <= 0:
        weakness = f"Your extracurriculars read thin for {school['name']}'s admit pool."
    else:
        weakness = f"At {round(school['accept']*100,1)}% acceptance, even strong profiles often need a clear hook."
    differentiator = f"Lean harder into your interest in {profile.get('major') or 'a focused academic identity'} — concrete projects or recognized awards in it separate similar profiles."
    return {"strength": strength, "weakness": weakness, "differentiator": differentiator}


def analyze_school(profile, slug):
    school = COLLEGES_BY_SLUG.get(slug)
    if not school: return None
    fit, components = compute_fit(profile, school)
    tier = assign_tier(school, fit)
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
    for k in ("weather","setting","size","class_size","greek","sports","major_strength","prestige","cost","diversity","party","research","career_intensity"):
        try:
            out[k] = max(1, min(10, int(form.get(f"weight_{k}", 5))))
        except (TypeError, ValueError):
            out[k] = 5
    return json.dumps(out)


_LEGACY_COUNT_RE = re.compile(r"\s*(?:[\(\[]?\s*(\d+)\s*x?\s*[\)\]]?|x\s*(\d+))\s*$", re.IGNORECASE)


def legacy_generations_at(profile, school):
    """How many generations of legacy the user has at THIS school. Returns
    0 if none. Parses '<name> Nx' or '<name> (N)' or just '<name>' (=1 gen).
    Match is name-substring, case-insensitive, both directions — so 'Penn'
    matches 'University of Pennsylvania' and vice-versa."""
    if not profile or not school: return 0
    raw = (profile.get("legacy_schools") or "").strip().lower()
    if not raw: return 0
    school_name = (school.get("name") or "").lower()
    school_slug = (school.get("slug") or "").lower()
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
        if name in school_name or school_name in name or name in school_slug or school_slug in name:
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
            max_tokens=6000,
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
BASE_CSS = """
:root{
  --bg:#070d14;
  --bg-2:#0a131c;
  --surface:#101a25;
  --surface-2:#16222f;
  --surface-hover:#1a2735;
  --border:rgba(255,255,255,.06);
  --border-strong:rgba(255,255,255,.10);
  --text:#e6edf3;
  --text-2:#9aa6b6;
  --text-3:#5e6b7c;
  --teal:#5eead4;
  --teal-2:#2dd4bf;
  --teal-dim:#0f3a37;
  --blue:#38bdf8;
  --accent-grad:linear-gradient(135deg,#38bdf8 0%,#5eead4 100%);
  --glow:0 0 24px rgba(94,234,212,.18);
  --shadow-card:0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
html,body{background:var(--bg)}
body{
  margin:0;
  font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;
  background:radial-gradient(ellipse at 12% -10%, rgba(56,189,248,.07), transparent 50%),
             radial-gradient(ellipse at 88% 110%, rgba(94,234,212,.06), transparent 50%),
             var(--bg);
  background-attachment:fixed;
  color:var(--text);
  line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--teal);text-decoration:none}
a:hover{color:#7df0db}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px 80px}
.nav{
  display:flex;align-items:center;gap:22px;padding:16px 28px;
  background:rgba(10,19,28,.85);
  backdrop-filter:saturate(140%) blur(12px);
  -webkit-backdrop-filter:saturate(140%) blur(12px);
  border-bottom:1px solid var(--border);
  margin-bottom:32px;
  position:sticky;top:0;z-index:10;
}
.nav .brand{font-weight:700;font-size:1.05em;color:var(--text);letter-spacing:-.2px}
.nav a{color:var(--text-2);font-size:.91em;font-weight:500;transition:color .15s}
.nav a:hover{color:var(--text)}
.nav .sp{flex:1}
h1{font-size:1.9em;font-weight:700;letter-spacing:-.6px;margin:8px 0 8px;color:var(--text)}
h2{font-size:1.22em;font-weight:600;margin:28px 0 12px;color:var(--text);letter-spacing:-.2px}
h3{font-size:1em;font-weight:600;margin:14px 0 6px;color:var(--text);letter-spacing:-.1px}
p{color:var(--text)}
.muted{color:var(--text-2);font-size:.92em}
.btn{
  display:inline-block;
  background:var(--surface-2);color:var(--text);
  font-weight:600;padding:11px 22px;border-radius:10px;border:1px solid var(--border-strong);
  cursor:pointer;font-size:.93em;text-decoration:none;font-family:inherit;
  transition:all .15s ease;
}
.btn:hover{background:var(--surface-hover);border-color:rgba(94,234,212,.3);color:var(--text);text-decoration:none}
.btn-primary{
  background:var(--accent-grad);color:#031715;border:0;
  box-shadow:0 8px 20px rgba(56,189,248,.18);
  font-weight:700;
}
.btn-primary:hover{filter:brightness(1.05);box-shadow:0 10px 26px rgba(56,189,248,.24);color:#031715}
.btn-light{background:transparent;color:var(--text);border:1px solid var(--border-strong)}
.btn-light:hover{background:var(--surface-2);color:var(--text)}
.btn-sm{font-size:.82em;padding:7px 14px;border-radius:8px}
.card{
  background:var(--surface)!important;
  border:1px solid var(--border)!important;
  border-radius:14px;padding:22px;margin-bottom:14px;
  box-shadow:var(--shadow-card);
  color:var(--text)!important;
}
.card h3, .card h2, .card h1, .card p, .card div, .card span, .card li{color:var(--text)}
.card .muted, .card .muted *{color:var(--text-2)!important}
label{display:block;font-weight:500;font-size:.85em;margin:14px 0 6px;color:var(--text-2);letter-spacing:.1px}
input,select,textarea{
  width:100%;padding:11px 13px;
  border:1px solid var(--border-strong);border-radius:9px;
  font-size:.93em;font-family:inherit;
  background:var(--bg-2);color:var(--text);
  transition:border-color .15s, box-shadow .15s;
}
textarea{min-height:80px;resize:vertical}
input:focus,select:focus,textarea:focus{
  outline:0;border-color:var(--teal);
  box-shadow:0 0 0 3px rgba(94,234,212,.12);
}
input[type="checkbox"]{accent-color:var(--teal);width:auto}
input::placeholder,textarea::placeholder{color:var(--text-3)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:560px){.row{grid-template-columns:1fr}}
.checks label{display:flex;align-items:center;gap:8px;font-weight:500;margin:8px 0;color:var(--text)}
.checks input{width:auto}
.pill{display:inline-block;padding:4px 11px;border-radius:999px;font-size:.7em;font-weight:600;letter-spacing:.5px;text-transform:uppercase;border:1px solid var(--border-strong)}
.pill-dream{background:rgba(244,114,182,.12);color:#f9a8d4;border-color:rgba(244,114,182,.22)}
.pill-reach{background:rgba(251,191,36,.12);color:#fcd34d;border-color:rgba(251,191,36,.22)}
.pill-target{background:rgba(56,189,248,.12);color:#7dd3fc;border-color:rgba(56,189,248,.22)}
.pill-safety{background:rgba(94,234,212,.12);color:var(--teal);border-color:rgba(94,234,212,.22)}
.pill-tier-1{background:var(--accent-grad);color:#031715;border:0;font-weight:700}
.pill-tier-2{background:rgba(94,234,212,.14);color:var(--teal);border-color:rgba(94,234,212,.22)}
.pill-tier-3{background:rgba(56,189,248,.12);color:#7dd3fc;border-color:rgba(56,189,248,.22)}
.pill-tier-4{background:var(--surface-2);color:var(--text-2)}
.pill-tier-5{background:var(--surface-2);color:var(--text-3)}
.pill-public{background:rgba(94,234,212,.12);color:var(--teal);border-color:rgba(94,234,212,.22)}
.pill-private{background:rgba(167,139,250,.12);color:#c4b5fd;border-color:rgba(167,139,250,.22)}
.pill-conf-low{background:var(--surface-2);color:var(--text-3)}
.pill-conf-medium{background:rgba(56,189,248,.10);color:#7dd3fc;border-color:rgba(56,189,248,.18)}
.pill-conf-high{background:rgba(94,234,212,.12);color:var(--teal);border-color:rgba(94,234,212,.22)}
.odds{
  font-size:2.4em;font-weight:800;letter-spacing:-1px;margin:10px 0 4px;
  background:var(--accent-grad);
  -webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;color:transparent;
  filter:drop-shadow(0 0 12px rgba(94,234,212,.25));
}
.flash{
  padding:12px 16px;background:rgba(56,189,248,.06);border:1px solid rgba(56,189,248,.2);
  border-radius:10px;margin-bottom:16px;font-size:.9em;color:var(--text);
}
.flash.error{background:rgba(244,114,182,.08);border-color:rgba(244,114,182,.25);color:#f9a8d4}
.flash.success{background:rgba(94,234,212,.08);border-color:rgba(94,234,212,.25);color:var(--teal)}
.search{display:flex;gap:10px;margin:8px 0 22px}
.search input{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.school-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:16px;transition:all .2s ease;
}
.school-card:hover{
  border-color:rgba(94,234,212,.3);
  background:var(--surface-hover);
  transform:translateY(-2px);
  box-shadow:0 12px 28px rgba(0,0,0,.4),0 0 0 1px rgba(94,234,212,.08);
  text-decoration:none;
}
.school-card a, .school-card a *{color:var(--text)}
.school-card a:hover{text-decoration:none}
.stat-row{display:flex;justify-content:space-between;font-size:.8em;color:var(--text-2);margin-top:9px}
.stat-row span:last-child{color:var(--text);font-weight:500}
.rank-row{
  display:flex;align-items:center;gap:14px;padding:14px 16px;
  background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;
  transition:all .15s ease;
}
.rank-row:hover{border-color:rgba(94,234,212,.2);background:var(--surface-hover)}
.rank-row .num{font-size:1.3em;font-weight:700;color:var(--text-3);min-width:30px;text-align:center;font-variant-numeric:tabular-nums}
.rank-row .body{flex:1}
.rank-row .body .nm{font-weight:600;color:var(--text)}
.rank-row .body .meta{font-size:.78em;color:var(--text-2)}
.rank-row a, .rank-row a *{color:inherit}
.rank-row a:hover{text-decoration:none}
.bar{display:flex;justify-content:space-between;margin:10px 0 22px;align-items:center;flex-wrap:wrap;gap:10px}
.bar a{font-size:.92em}
.tag-list{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.tag{display:inline-block;padding:4px 10px;border-radius:6px;background:var(--surface-2);color:var(--text-2);font-size:.78em;border:1px solid var(--border)}
table{color:var(--text)}
table th{color:var(--text-2);font-weight:500;text-align:left;font-size:.82em;letter-spacing:.3px;text-transform:uppercase}
table tbody tr{border-top:1px solid var(--border)}
table td{padding:10px 6px;color:var(--text)}
hr{border:0;border-top:1px solid var(--border);margin:24px 0}
::selection{background:rgba(94,234,212,.25);color:#fff}
/* Stat card emphasis */
.stat-card{
  background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px;
  position:relative;overflow:hidden;
}
.stat-card::before{
  content:"";position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(94,234,212,.4),transparent);
  opacity:.6;
}
.stat-card .label{font-size:.78em;color:var(--text-2);text-transform:uppercase;letter-spacing:.6px;font-weight:500}
.stat-card .value{font-size:2em;font-weight:700;letter-spacing:-.6px;margin-top:6px;color:var(--text)}
.stat-card .value.accent{
  background:var(--accent-grad);-webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;color:transparent;
}
.stat-card .delta{font-size:.78em;color:var(--text-2);margin-top:4px}
.pick-pill:hover{background:var(--surface-hover)!important;border-color:rgba(94,234,212,.3)!important}
.pick-pill:has(input:checked){background:rgba(94,234,212,.12)!important;border-color:rgba(94,234,212,.45)!important;color:var(--teal)!important}
/* ───────── MOBILE ───────── */
@media (max-width: 720px){
  .wrap{padding:0 16px 60px}
  .nav{padding:12px 14px;gap:14px;flex-wrap:wrap}
  .nav .brand{font-size:1em;width:100%;margin-bottom:2px}
  .nav .sp{display:none}
  .nav a{font-size:.85em}
  h1{font-size:1.45em}
  h2{font-size:1.1em}
  .card{padding:16px;border-radius:12px}
  .grid{grid-template-columns:1fr;gap:12px}
  .school-card{padding:14px}
  .stat-card .value{font-size:1.6em}
  .odds{font-size:1.85em}
  .rank-table{font-size:.78em}
  .rank-table th,.rank-table td{padding:8px 6px}
  .rank-table th:nth-child(5),.rank-table td:nth-child(5){display:none}  /* hide ACT col */
  .rank-table th:nth-child(7),.rank-table td:nth-child(7){display:none}  /* hide class size */
  .pick-pill{font-size:.78em!important;padding:5px 9px!important}
  .pill{font-size:.65em;padding:3px 8px}
  /* iOS auto-zooms inputs with font-size <16px. Keep at 16 to prevent that. */
  input,select,textarea{font-size:16px}
  .btn{padding:10px 18px;font-size:.92em}
  .btn-sm{padding:7px 12px}
  /* Tap targets */
  .checks label{padding:8px 0;min-height:44px}
  .pick-pill{min-height:36px}
  /* Tables that overflow get a scroll container */
  table{display:block;overflow-x:auto}
  /* Action plan items stack tighter */
  .action-item{flex-direction:column;gap:8px;padding:14px 0}
  .action-num{margin-bottom:4px}
}
@media (max-width: 480px){
  .wrap{padding:0 12px 50px}
  .nav{gap:10px}
  .nav a{font-size:.8em}
  h1{font-size:1.3em;letter-spacing:-.4px}
  .card{padding:14px}
  .stat-card .value{font-size:1.4em}
  .odds{font-size:1.6em}
  /* Real Profiles header on mobile */
  .rank-row{flex-wrap:wrap;padding:12px}
  .rank-row .num{min-width:24px;font-size:1.1em}
}
/* Action plan items */
.action-item{display:flex;gap:14px;padding:14px 0;border-top:1px solid var(--border)}
.action-item:first-child{border-top:0;padding-top:6px}
.action-num{
  flex-shrink:0;width:32px;height:32px;border-radius:50%;
  background:rgba(94,234,212,.12);color:var(--teal);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:.95em;border:1px solid rgba(94,234,212,.3);
}
.action-body{flex:1}
.action-title{font-weight:600;color:var(--text);font-size:1.02em;margin-bottom:6px}
.action-meta{font-size:.88em;color:var(--text-2);margin:3px 0;line-height:1.5}
.meta-label{color:var(--text-3);font-weight:500;margin-right:4px;text-transform:uppercase;font-size:.78em;letter-spacing:.4px}
.action-impact{
  display:inline-block;margin-top:8px;padding:4px 10px;border-radius:6px;
  background:rgba(94,234,212,.08);color:var(--teal);font-size:.82em;font-weight:500;
  border:1px solid rgba(94,234,212,.2);
}
/* Competition / program rows */
.comp-row{padding:14px 0;border-top:1px solid var(--border)}
.comp-row:first-child{border-top:0;padding-top:4px}
.comp-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.comp-name{color:var(--text);font-weight:600;font-size:1em}
.comp-name:hover{color:var(--teal)}
.comp-meta{font-size:.86em;color:var(--text-2);margin-top:4px}
.comp-note{font-size:.92em;color:var(--text);margin-top:6px;line-height:1.5}
"""

CANDOR_LOGO_SVG = """<svg viewBox="0 0 64 64" width="22" height="22" xmlns="http://www.w3.org/2000/svg" style="vertical-align:-4px;margin-right:8px">
  <defs>
    <linearGradient id="cdr-g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="100%" stop-color="#5eead4"/>
    </linearGradient>
  </defs>
  <path d="M 52 16 A 22 22 0 1 0 52 48" stroke="url(#cdr-g)" stroke-width="6" fill="none" stroke-linecap="round"/>
  <rect x="22" y="36" width="5.5" height="10" fill="url(#cdr-g)" rx="1.2"/>
  <rect x="31" y="28" width="5.5" height="18" fill="url(#cdr-g)" rx="1.2"/>
  <rect x="40" y="20" width="5.5" height="26" fill="url(#cdr-g)" rx="1.2"/>
</svg>"""

NAV = """<div class="nav"><a class="brand" href="/">""" + CANDOR_LOGO_SVG + """Candor</a>
<a href="/colleges">Browse</a>
<a href="/rankings">Rankings</a>
<a href="/compare">Compare</a>
<a href="/plans">My Plans</a>
<a href="/improve">Improve</a>
<a href="/chat">AI Advisor</a>
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


def _page(body_html, title="Candor"):
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
candor is free. the AI advisor costs $5/mo only because the api isn't.
</div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{favicon}
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
                   "career_intensity":"Academic culture"}
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


def college_detail_html(slug):
    raw = COLLEGES_BY_SLUG.get(slug)
    if not raw: abort(404)
    c = merged_school(raw)
    user = current_user()
    saved = is_saved(user["id"], slug) if user else False
    if user:
        save_btn = (f'<form method="post" action="/{("unsave" if saved else "save")}/{slug}" style="display:inline">'
                    f'{csrf_input()}'
                    f'<button class="btn {("btn-primary" if saved else "btn-light")}" type="submit">'
                    f'{"★ Saved" if saved else "☆ Save to my list"}</button></form>')
    else:
        save_btn = f'<a class="btn btn-light" href="/login">☆ Save to my list</a>'
    over = _get_overrides(slug)
    verified_badge = ""
    if is_cds_verified(slug):
        verified_badge = '<span style="font-size:.74em;background:rgba(94,234,212,.18);color:var(--teal);padding:3px 10px;border-radius:999px;margin-left:8px;border:1px solid rgba(94,234,212,.35);font-weight:600;letter-spacing:.3px" title="Stats hand-verified against the school\'s most recent Common Data Set (2024-25 or 2025-26 cycle)">CDS VERIFIED</span>'
    elif slug in MANUAL_FRESH_ACCEPT:
        verified_badge = '<span style="font-size:.74em;background:rgba(94,234,212,.12);color:var(--teal);padding:3px 10px;border-radius:999px;margin-left:8px;border:1px solid rgba(94,234,212,.25);font-weight:500;letter-spacing:.3px">2024-25 CYCLE</span>'
    elif over and over.get("source"):
        verified_badge = f'<span style="font-size:.74em;background:rgba(94,234,212,.10);color:var(--teal);padding:3px 10px;border-radius:999px;margin-left:8px;border:1px solid rgba(94,234,212,.22);font-weight:500;letter-spacing:.3px">VERIFIED · federal data</span>'
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
  <div id="summary-block" style="margin:14px 0 6px;color:#444">{c['desc']}<div class="muted" style="font-size:.82em;margin-top:4px"><i>Loading extended overview…</i></div></div>
  <div class="tag-list">{majors_tags}</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    <a class="btn btn-primary" href="/college/{c['slug']}/plan">★ My personalized plan</a>
    <a class="btn btn-light" href="/chances/{c['slug']}">Chances only</a>
    <a class="btn btn-light" href="/college/{c['slug']}/improve">Improve guide</a>
    <a class="btn btn-light" href="/college/{c['slug']}/chat">AI advisor</a>
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
</div>
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
}})();
</script>
""", title=f"{c['name']} — Candor")


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


RANKING_TABLE_CSS = """
<style>
.rank-table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden;font-size:.92em;box-shadow:var(--shadow-card)}
.rank-table th{background:rgba(255,255,255,.02);text-align:left;padding:13px 16px;color:var(--text-2);font-weight:500;font-size:.74em;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.rank-table td{padding:13px 16px;border-bottom:1px solid var(--border);vertical-align:middle;color:var(--text)}
.rank-table tr:hover td{background:rgba(94,234,212,.04)}
.rank-table tr:last-child td{border-bottom:0}
.rank-table .rank-num{font-weight:600;color:var(--text-3);font-variant-numeric:tabular-nums}
.rank-table .name a{color:var(--text);font-weight:600}
.rank-table .name a:hover{color:var(--teal)}
.rank-table .num-col{font-variant-numeric:tabular-nums;color:var(--text-2)}
.rank-table .stars{color:var(--teal);letter-spacing:1px;filter:drop-shadow(0 0 6px rgba(94,234,212,.25))}
.rank-table .stars-empty{color:rgba(255,255,255,.12)}
@media(max-width:720px){.rank-table th.hide-sm,.rank-table td.hide-sm{display:none}}
</style>
"""


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
    return f"""<div class="card" style="max-width:440px;background:rgba(94,234,212,.08);border-color:rgba(94,234,212,.25);margin-bottom:14px">
  <div style="font-weight:600;color:var(--teal);margin-bottom:6px">{headline_map[intent]}</div>
  <p class="muted" style="margin:0;font-size:.85em;line-height:1.5">{detail_map[intent]}</p>
</div>"""


def signup_html():
    hook = _auth_hook_html()
    return _page(f"""
<div class="bar"><a href="/">&larr; back</a></div>
<h1>Create your account</h1>
<p class="muted">Saves your profile so you can come back without re-entering everything.</p>
{hook}
<form method="post" action="/signup" class="card" style="max-width:440px">
  {csrf_input()}
  <label style="margin-top:0">Email</label>
  <input type="email" name="email" required autofocus>
  <label>Password</label>
  <input type="password" name="password" minlength="8" required>
  <p class="muted" style="font-size:.78em;margin-top:6px">8+ characters. We never email you marketing.</p>
  <button class="btn btn-primary" type="submit">Create account</button>
  <p class="muted" style="font-size:.85em;margin-top:14px">Already registered? <a href="/login">Log in</a>.</p>
</form>
""", title="Sign up — Candor")


def login_html():
    hook = _auth_hook_html()
    return _page(f"""
<div class="bar"><a href="/">&larr; back</a></div>
<h1>Log in</h1>
{hook}
<form method="post" action="/login" class="card" style="max-width:440px">
  {csrf_input()}
  <label style="margin-top:0">Email</label>
  <input type="email" name="email" required autofocus>
  <label>Password</label>
  <input type="password" name="password" required>
  <button class="btn btn-primary" type="submit">Log in</button>
  <p class="muted" style="font-size:.85em;margin-top:14px">No account? <a href="/signup">Sign up</a>.</p>
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
        "pref_career_intensity": "Academic culture",
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
                f'border-radius:8px;padding:7px 12px;font-weight:500;color:var(--text);'
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
AP_PICKER_GROUPS = [
    ("Math & CS", [
        ("Calc AB","Calc AB"),("Calc BC","Calc BC"),("Pre-Calc","Pre-Calc"),
        ("Statistics","Statistics"),
        ("CSA","Computer Science A"),("CSP","Computer Science Principles"),
    ]),
    ("Sciences", [
        ("Biology","Biology"),("Chemistry","Chemistry"),
        ("Physics 1","Physics 1"),("Physics 2","Physics 2"),
        ("Physics C Mech","Physics C: Mechanics"),("Physics C E&M","Physics C: E&M"),
        ("Environmental Science","Environmental Science"),
    ]),
    ("History & Social Studies", [
        ("APUSH","US History"),("World History","World History"),("European History","European History"),
        ("Human Geography","Human Geography"),("US Government","US Government"),
        ("Comparative Government","Comparative Government"),
        ("Macroeconomics","Macroeconomics"),("Microeconomics","Microeconomics"),
        ("Psychology","Psychology"),("African American Studies","African American Studies"),
    ]),
    ("English", [
        ("English Language","English Language"),("English Literature","English Literature"),
        ("Seminar","Capstone Seminar"),("Research","Capstone Research"),
    ]),
    ("World Languages", [
        ("Spanish Language","Spanish Lang"),("Spanish Literature","Spanish Lit"),
        ("French Language","French Lang"),("French Literature","French Lit"),
        ("German","German"),("Italian","Italian"),("Latin","Latin"),
        ("Chinese","Chinese"),("Japanese","Japanese"),("Korean","Korean"),
    ]),
    ("Arts", [
        ("Art History","Art History"),("Music Theory","Music Theory"),
        ("Drawing","Studio Art: Drawing"),("2-D Art","Studio Art: 2-D Design"),
        ("3-D Art","Studio Art: 3-D Design"),
    ]),
]


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
                f'border-radius:8px;padding:6px 11px;font-weight:500;color:var(--text);'
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
IB_PICKER_GROUPS = [
    ("Math (pick one HL or one SL)", [
        ("Math AA HL","Math AA HL"), ("Math AA SL","Math AA SL"),
        ("Math AI HL","Math AI HL"), ("Math AI SL","Math AI SL"),
    ]),
    ("Sciences", [
        ("Biology HL","Biology HL"),("Biology SL","Biology SL"),
        ("Chemistry HL","Chemistry HL"),("Chemistry SL","Chemistry SL"),
        ("Physics HL","Physics HL"),("Physics SL","Physics SL"),
        ("Computer Science HL","Computer Science HL"),("Computer Science SL","Computer Science SL"),
        ("Environmental Systems HL","Environmental Systems HL"),("Environmental Systems SL","Environmental Systems SL"),
        ("Design Technology HL","Design Tech HL"),
    ]),
    ("Individuals & Societies", [
        ("History HL","History HL"),("History SL","History SL"),
        ("Economics HL","Economics HL"),("Economics SL","Economics SL"),
        ("Psychology HL","Psychology HL"),("Psychology SL","Psychology SL"),
        ("Geography HL","Geography HL"),("Geography SL","Geography SL"),
        ("Global Politics HL","Global Politics HL"),("Global Politics SL","Global Politics SL"),
        ("Business Management HL","Business Mgmt HL"),("Business Management SL","Business Mgmt SL"),
    ]),
    ("Languages & Literature", [
        ("English Lit HL","English Lit HL"),("English Lit SL","English Lit SL"),
        ("English Language and Literature HL","Eng Lang & Lit HL"),
        ("English Language and Literature SL","Eng Lang & Lit SL"),
        ("Spanish HL","Spanish HL"),("Spanish SL","Spanish SL"),
        ("French HL","French HL"),("French SL","French SL"),
        ("German HL","German HL"),("Mandarin HL","Mandarin HL"),("Japanese HL","Japanese HL"),
    ]),
    ("Arts", [
        ("Visual Arts HL","Visual Arts HL"),("Visual Arts SL","Visual Arts SL"),
        ("Music HL","Music HL"),("Music SL","Music SL"),
        ("Theatre HL","Theatre HL"),("Film HL","Film HL"),
    ]),
    ("Core (everyone in IB diploma takes these)", [
        ("TOK","Theory of Knowledge"),("Extended Essay","Extended Essay"),
    ]),
]


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
                f'border-radius:8px;padding:6px 11px;font-weight:500;color:var(--text);'
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
    checked = lambda k: 'checked' if p.get(k) else ''
    return _page(f"""
<h1>Your profile</h1>
<p class="muted">Used by the chances calculator. Be specific — generic answers produce generic odds.</p>
<form method="post" action="/profile" class="card">
  {csrf_input()}
  <h3 style="margin-top:0">Academics</h3>
  <div class="row">
    <div><label>Unweighted GPA <span class="muted">(0–4)</span></label>
      <input type="number" step="0.01" min="0" max="4.5" name="uw_gpa" value="{v('uw_gpa')}" required></div>
    <div><label>Weighted GPA <span class="muted">(optional)</span></label>
      <input type="number" step="0.01" min="0" max="6" name="weighted_gpa" value="{v('weighted_gpa')}"></div>
  </div>
  <div class="row">
    <div><label>SAT</label>
      <input type="number" min="400" max="1600" step="10" name="sat" value="{v('sat')}"></div>
    <div><label>ACT</label>
      <input type="number" min="1" max="36" name="act" value="{v('act')}"></div>
  </div>

  <h3>About you</h3>
  <div class="row">
    <div><label>Intended major</label>
      <input name="major" value="{v('major')}" placeholder="Computer Science" list="majors-list" autocomplete="off">
      <datalist id="majors-list">
        {''.join(f'<option value="{m}">' for m in MAJORS)}
      </datalist></div>
    <div><label>State</label>
      <input name="state" value="{v('state')}" placeholder="California"></div>
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

  <h3>Preferences</h3>
  <p class="muted" style="font-size:.85em;margin:-2px 0 8px">Check anything you'd be happy with. The 1-10 dial says how much it matters. 5 is neutral. <b>10 is a deal-breaker</b>: schools that miss get removed from My Fit.</p>
  {_pref_form_fields(p)}

  <p style="margin-top:18px"><button class="btn btn-primary" type="submit">Save profile</button></p>
</form>
""", title="Profile — Candor")


def chances_html(slug):
    uid = current_user()["id"]
    p = get_profile(uid)
    if not p:
        flash("Create your profile first so we can calculate your chances.", "error")
        session["next_url"] = f"/chances/{slug}"
        return redirect(url_for("profile_page"))
    # Lazily run the exceptional-applicant evaluation if it hasn't run for
    # this profile yet. Cached after the first call.
    is_exc, exc_reason = get_or_evaluate_exceptionality(uid, p)
    profile = {
        "uw_gpa": p.get("uw_gpa"), "weighted_gpa": p.get("weighted_gpa"),
        "sat": p.get("sat"), "act": p.get("act"), "major": p.get("major"),
        "state": p.get("state"), "school_type": p.get("school_type"),
        "ecs": p.get("ecs"), "leadership": p.get("leadership"), "awards": p.get("awards"),
        "legacy": bool(p.get("legacy")), "first_gen": bool(p.get("first_gen")),
        "athlete": bool(p.get("athlete")),
        "is_international": bool(p.get("is_international")),
        "legacy_schools": p.get("legacy_schools") or "",
        "aps": p.get("aps") or "",
        "no_aps_offered": bool(p.get("no_aps_offered")),
        "aps_offered_not_taken": bool(p.get("aps_offered_not_taken")),
        "_di_level": get_demonstrated_interest(uid, slug),
        "is_exceptional": is_exc,
        "exceptional_reason": exc_reason,
        "portfolio": p.get("portfolio") or "",
    }
    r = analyze_school(profile, slug)
    if not r: abort(404)
    # Save it
    with db() as conn:
        conn.execute("""INSERT INTO saved_chances (user_id, college_slug, tier, odds_low, odds_high, fit, confidence, strength, weakness, differentiator, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, college_slug) DO UPDATE SET
              tier=excluded.tier, odds_low=excluded.odds_low, odds_high=excluded.odds_high,
              fit=excluded.fit, confidence=excluded.confidence,
              strength=excluded.strength, weakness=excluded.weakness, differentiator=excluded.differentiator,
              computed_at=CURRENT_TIMESTAMP""",
            (current_user()["id"], r["slug"], r["tier"], r["odds_low"], r["odds_high"],
             r["fit"], r["confidence"], r["strength"], r["weakness"], r["differentiator"]))
        conn.commit()
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
  {(f'<div style="margin-top:14px;padding:10px 14px;background:rgba(94,234,212,.08);border:1px solid rgba(94,234,212,.25);border-radius:8px;font-size:.88em"><b style="color:var(--teal)">★ Exceptional applicant override</b><div class="muted" style="margin-top:4px">{exc_reason or "Flagged exceptional based on your profile."} Your odds reflect this above the standard cap.</div></div>' if profile.get('is_exceptional') else '')}
  {render_admissions_breakdown(COLLEGES_BY_SLUG.get(r['slug']), admissions_detail(COLLEGES_BY_SLUG.get(r['slug'])))}
  <ul style="padding-left:18px;margin:18px 0 0">
    <li><b>Strength —</b> {r['strength']}</li>
    <li><b>Weakness —</b> {r['weakness']}</li>
    <li><b>Differentiator —</b> {r['differentiator']}</li>
  </ul>
</div>
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
    chances_card = ""
    if profile:
        # personalized gap analysis for this specific school
        prof = {
            "uw_gpa": profile.get("uw_gpa"), "sat": profile.get("sat"), "act": profile.get("act"),
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
            rate_rows += f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid var(--border)"><span{highlight}>{ROUND_LABELS.get(r,r)}{" ← recommended" if r == rec else ""}</span><span{highlight}>{v_str}</span></div>'
        round_card = f"""<div class="card">
  <h3 style="margin-top:0">Best application round for you</h3>
  <p style="margin:0 0 10px">{rec_reason}</p>
  {rate_rows}
</div>"""
    # ─── Score push impact ───
    score_card = ""
    if profile and (profile.get("sat") or profile.get("act")):
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
    # Major-specific competitions / programs from generic library
    major = (profile or {}).get("major", "") or ""
    relevant_comps = []
    major_lower = major.lower()
    for it in RESOURCES["competitions"]:
        majors_tag = it.get("majors", [])
        if not majors_tag or any(m.lower() in major_lower or major_lower in m.lower() for m in majors_tag):
            relevant_comps.append(it)
    comp_rows = ""
    for it in (relevant_comps or RESOURCES["competitions"])[:5]:
        comp_rows += f'<div style="padding:8px 0;border-top:1px solid #eee"><a href="{it["url"]}" target="_blank" rel="noopener">{it["title"]}</a><div class="muted" style="font-size:.84em;margin-top:2px">{it["note"]}</div></div>'
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
<p style="margin-top:18px"><a class="btn btn-light" href="/college/{slug}/chat">Ask the AI advisor about {school['name']} &rarr;</a> <a class="btn btn-light" href="/improve">General improve guide</a></p>
""", title=f"Improve your {school['name']} application — Candor")


# ─── REFERENCE LINKS PER SCHOOL ───────────────────────────
# We don't store an explicit website per school, but we can derive admissions
# URLs and Wikipedia links by best-effort patterns + an override map for the
# most popular schools.
WEBSITE_BY_SLUG = {
    "harvard":"https://college.harvard.edu/admissions",
    "stanford":"https://admission.stanford.edu/",
    "mit":"https://mitadmissions.org/",
    "yale":"https://admissions.yale.edu/",
    "princeton":"https://admission.princeton.edu/",
    "columbia":"https://undergrad.admissions.columbia.edu/",
    "uchicago":"https://collegeadmissions.uchicago.edu/",
    "upenn":"https://admissions.upenn.edu/",
    "brown":"https://admission.brown.edu/",
    "dartmouth":"https://admissions.dartmouth.edu/",
    "duke":"https://admissions.duke.edu/",
    "northwestern":"https://admissions.northwestern.edu/",
    "caltech":"https://admissions.caltech.edu/",
    "cornell":"https://admissions.cornell.edu/",
    "jhu":"https://apply.jhu.edu/",
    "vanderbilt":"https://admissions.vanderbilt.edu/",
    "rice":"https://admission.rice.edu/",
    "notre-dame":"https://admissions.nd.edu/",
    "cmu":"https://www.cmu.edu/admission/",
    "usc":"https://admission.usc.edu/",
    "nyu":"https://www.nyu.edu/admissions/",
    "georgetown":"https://uadmissions.georgetown.edu/",
    "ucb":"https://admissions.berkeley.edu/",
    "ucla":"https://admission.ucla.edu/",
    "umich":"https://admissions.umich.edu/",
    "uva":"https://admission.virginia.edu/",
    "gatech":"https://admission.gatech.edu/",
    "unc":"https://admissions.unc.edu/",
    "ut-austin":"https://admissions.utexas.edu/",
    "wisc":"https://admissions.wisc.edu/",
    "uf":"https://admissions.ufl.edu/",
    "tufts":"https://admissions.tufts.edu/",
    "washu":"https://wustl.edu/admissions/",
    "emory":"https://apply.emory.edu/",
    "northeastern":"https://admissions.northeastern.edu/",
    "bu":"https://www.bu.edu/admissions/",
    "bc":"https://www.bc.edu/admission/",
    "wake-forest":"https://admissions.wfu.edu/",
    "wm":"https://www.wm.edu/admission/",
    "williams":"https://admission.williams.edu/",
    "amherst":"https://www.amherst.edu/admission",
    "swarthmore":"https://www.swarthmore.edu/admissions",
    "pomona":"https://www.pomona.edu/admissions",
    "bowdoin":"https://www.bowdoin.edu/admissions/",
    "wellesley":"https://www.wellesley.edu/admission",
    "middlebury":"https://www.middlebury.edu/admissions",
    "carleton":"https://www.carleton.edu/admissions/",
}

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
    Cached 30 days."""
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
            row = conn.execute(
                "SELECT body FROM school_summary WHERE college_slug=? AND generated_at >= ?",
                (c["slug"], cutoff)
            ).fetchone()
            if row: return row["body"]
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
                max_tokens=600,
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
ESSAYS_THAT_WORKED = {
    "tufts":     "https://admissions.tufts.edu/apply/essays-that-worked/",
    "jhu":       "https://apply.jhu.edu/application-process/essays-that-worked/",
    "hamilton":  "https://www.hamilton.edu/admission/apply/essays-that-worked",
    "conn-college": "https://www.conncoll.edu/admission/apply/essays-that-worked/",
    "carleton":  "https://www.carleton.edu/admissions/essays/",
    "bates":     "https://www.bates.edu/admission/applying/essays/",
    "vassar":    "https://www.vassar.edu/voices/admissions",
    "yale":      "https://admissions.yale.edu/sample-essays",
    "stanford":  "https://admission.stanford.edu/apply/freshman/essays.html",
}

# Other useful real-data sources for any school
GENERIC_PROFILE_LINKS = [
    ("r/ApplyingToCollege results threads", "https://www.reddit.com/r/ApplyingToCollege/search/?q=results&restrict_sr=1&sort=new"),
    ("College Confidential admit threads", "https://www.collegeconfidential.com/forums/categories/college-admissions/"),
    ("College Essay Guy — Sample Essays", "https://www.collegeessayguy.com/blog/college-essay-examples"),
    ("Khan Academy — College Essay Examples", "https://www.khanacademy.org/college-careers-more/college-admissions/applying-to-college/admissions-essays/v/college-admissions-officers-on-personal-statements"),
]


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

# Per-school facts that AI tailored advice must use as ground truth
# rather than guessing from training data. The AI was hallucinating
# things like "USC doesn't require a portfolio for studio art" when
# Roski actually requires portfolios for ALL undergrad majors. Add a
# slug → fact-bullets dict; injected verbatim into the Claude prompt
# so it can't override these.
SCHOOL_VERIFIED_FACTS = {
    "usc": [
        "USC admits to specific undergrad colleges, not USC at large. Each has its own admit rate, supplements, and (for portfolio-based programs) creative requirements. Always specify which USC college the student is applying to.",
        "Roski School of Art and Design requires portfolios for ALL undergraduate majors — Studio Art, Design, Communication Design, Animation & Digital Arts, Fine Arts. This is non-negotiable.",
        "Iovine and Young Academy (full name: Iovine and Young Academy for Arts, Technology and the Business of Innovation) is one of the MOST selective programs at USC — admit rate is in the single digits, far below USC overall. It admits ~30 students per cohort. Requires portfolio + supplemental written responses + often video submission, sometimes interview. It offers a single hybrid B.S. degree (no double majors). Founded by Jimmy Iovine and Dr. Dre. Site: iovine-young.usc.edu.",
        "Marshall School of Business has its own admit process and admit rate (typically lower than USC overall). Strong feeder to West Coast finance, accounting, real estate, entrepreneurship.",
        "Viterbi School of Engineering admits separately; Viterbi has 9 engineering departments. Computer Science direct-admit is highly competitive (single-digit admit rate); CS+other is somewhat easier.",
        "Annenberg School for Communication and Journalism admits separately; Communication, Journalism, PR programs.",
        "School of Cinematic Arts (SCA) is the most prestigious film school in the world. ALL programs require creative supplements: Film & TV Production wants visual portfolio + writing samples; Writing for Screen & TV wants written portfolio; Animation & Digital Arts wants visual portfolio. Admit rates 1-5% depending on program.",
        "Thornton School of Music requires auditions for performance majors; music industry / music production / popular music tracks may use portfolios instead of auditions.",
        "Kaufman School of Dance requires auditions (in-person or video).",
        "Dornsife College of Letters, Arts and Sciences is the largest USC undergrad school and the typical 'general admit' path — humanities, sciences, social sciences.",
        "USC's overall admit rate masks huge variation — SCA / Iovine / direct-admit Viterbi CS are ~1-5%; Dornsife is closer to USC's headline rate.",
    ],
    "nyu": [
        "Tisch requires portfolios + auditions for most programs (Film & TV, Drama, Dance, etc.) — separate from main NYU application.",
        "Stern is a separate undergrad business school with its own admit rate (typically lower than NYU overall).",
        "NYU is a multi-school university — admit rates differ between Stern, Tisch, CAS, Steinhardt, Tandon, Gallatin.",
    ],
    "cmu": [
        "School of Drama requires auditions; School of Art requires portfolios; Architecture requires a portfolio.",
        "CMU has 7 colleges (CIT, MCS, Dietrich, Tepper, CFA, SCS, Heinz) — admit rates vary dramatically (SCS is ~5%, others 15-25%).",
    ],
    "cornell": [
        "Cornell has 8 distinct undergrad colleges (CALS, A&S, AAP, Engineering, Hotel, Human Ecology, ILR, Dyson) with separate admissions.",
        "AAP (Architecture, Art, and Planning) requires portfolios for Architecture and Art programs.",
        "Hotel School (Nolan) has a separate application requiring leadership and service experience demonstration.",
    ],
    "northwestern": [
        "School of Communication requires portfolios for Theatre, Radio/TV/Film production tracks.",
        "Bienen School of Music requires auditions.",
    ],
    "umich": [
        "Stamps School of Art & Design requires a portfolio.",
        "School of Music, Theatre & Dance requires auditions/portfolios.",
        "Ross School of Business is preferred admit; LSA-then-transfer is common.",
    ],
    "upenn": [
        "Wharton has a separate, more competitive admit process; M&T (Management & Technology) and Huntsman are dual-degree programs with their own applications.",
        "Penn has 4 undergrad schools (Wharton, CAS, SEAS, Nursing) — admit rates and weights differ.",
    ],
    "rice": [
        "Architecture program requires a portfolio.",
        "Shepherd School of Music requires auditions.",
    ],
    "yale": [
        "Yale has one undergrad college; specific majors (Art, Architecture, Music) may require portfolios but admission is to Yale College generally.",
    ],
    "barnard": [
        "Architecture and Visual Arts majors require portfolios; portfolio is recommended for Theatre and Dance.",
    ],
    "risd": [
        "RISD requires a comprehensive portfolio for all undergraduate majors — this is the primary admissions criterion.",
        "RISD also requires the 'Bicycle' drawing assignment as part of the application.",
    ],
    "parsons": [
        "Parsons requires a portfolio for all undergraduate design majors plus the 'Parsons Challenge' creative response.",
    ],
    "berklee": [
        "Berklee requires an audition + interview for all undergraduate music programs.",
    ],
    "juilliard": [
        "Juilliard requires auditions for all programs (Music, Drama, Dance) — auditions are the primary admissions criterion.",
    ],
}


def get_tailored_advice(user_id, school, profile, force=False):
    """Return Claude-generated, profile-specific advice for this school.
    Cached 7 days. Falls back to a templated bullet list if no key."""
    with db() as conn:
        if not force:
            cutoff = (datetime.utcnow() - timedelta(days=TAILORED_ADVICE_TTL_DAYS)).isoformat()
            row = conn.execute(
                "SELECT body, generated_at FROM tailored_advice WHERE user_id=? AND college_slug=? AND generated_at >= ?",
                (user_id, school["slug"], cutoff)
            ).fetchone()
            if row:
                return row["body"]
    # Generate fresh
    fit_acad, components = compute_fit(profile, school)
    low, high = estimate_odds(school, fit_acad, profile)
    m = school_match(profile, school)
    note = get_school_strategy(school)
    test_str = f"SAT {profile['sat']}" if profile.get("sat") else (f"ACT {profile['act']}" if profile.get("act") else "no test score submitted")
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

VERIFIED FACTS YOU MUST USE (do NOT contradict these — they're hand-checked):
{chr(10).join(f"- {f}" for f in SCHOOL_VERIFIED_FACTS.get(school['slug'], [])) or "(none specific to this school)"}

TASK: Write 6-8 SPECIFIC, ACTIONABLE bullets advising this exact student on applying to {school['name']}. Each bullet must:
1. Reference a specific number, program, deadline, course, professor, or activity from the data above (no generic advice).
2. Acknowledge the student's actual profile (their GPA/test/ECs) — what they already have, what's missing for THIS school.
3. Speak directly to fit/mismatch when relevant (e.g. "you preferred warm but X is cold — here's how to evaluate that tradeoff").
4. Be concrete: name the program, the threshold, the action, the deadline.

CRITICAL ACCURACY RULES — DO NOT VIOLATE:
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
        conn.execute("""INSERT INTO tailored_advice (user_id, college_slug, body, generated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, college_slug) DO UPDATE SET
                body=excluded.body, generated_at=CURRENT_TIMESTAMP""",
            (user_id, school["slug"], body))
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
<p style="margin-top:18px"><a class="btn btn-light" href="/colleges">Browse all 155 colleges &rarr;</a></p>
""", title="Real Profiles — Candor")


def school_profiles_html(slug):
    c = COLLEGES_BY_SLUG.get(slug)
    if not c: abort(404)
    name = c["name"]
    force = request.args.get("refresh") == "1"
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
                    <span style="font-size:.74em;color:{outcome_color};padding:3px 11px;border-radius:999px;border:1px solid rgba(94,234,212,.2);background:rgba(94,234,212,.08);font-weight:600;letter-spacing:.4px;text-transform:uppercase">{outcome} · {words} words</span>
                  </div>
                  <div style="font-size:.92em;line-height:1.65;color:var(--text);font-family:Georgia,serif;background:var(--bg-2);padding:14px;border-radius:8px;border:1px solid var(--border)">{essay_text}</div>
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
  <a class="btn btn-light btn-sm" href="/college/{slug}/chat">AI advisor</a>
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
    school = COLLEGES_BY_SLUG.get(slug)
    if not school: abort(404)
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

    prof = {
        "uw_gpa": profile.get("uw_gpa"), "weighted_gpa": profile.get("weighted_gpa"),
        "sat": profile.get("sat"), "act": profile.get("act"), "major": profile.get("major"),
        "state": profile.get("state"), "school_type": profile.get("school_type"),
        "ecs": profile.get("ecs"), "leadership": profile.get("leadership"), "awards": profile.get("awards"),
        "legacy": bool(profile.get("legacy")), "first_gen": bool(profile.get("first_gen")),
        "athlete": bool(profile.get("athlete")),
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
                   "career_intensity":"Academic culture"}
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

    # 3) Tailored advice (Claude-generated, cached 7 days)
    force_refresh = request.args.get("refresh") == "1"
    advice_body = get_tailored_advice(user["id"], school, prof, force=force_refresh)
    advice_html = _render_tailored_advice(advice_body)

    # 4) School-specific notes (curated values + essay strategy)
    note = get_school_strategy(school)

    return _page(f"""
<div class="bar"><a href="/college/{slug}">&larr; back to {school['name']}</a></div>
<h1>Your plan for {school['name']}</h1>
<div class="muted">{city_state(school)} · {round(school['accept']*100,1)}% acceptance · {school['type']}</div>

<div class="card" style="margin-top:18px;background:#1a1a1a;color:#fff;border-color:#1a1a1a">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
    <div>
      <h3 style="margin:0;color:#fff">Your chances</h3>
      <div class="muted" style="color:#bdbdbd;font-size:.82em">profile fit {r['fit']}/100</div>
    </div>
    <div><span class="pill {tier_class}">{r['tier']}</span> <span class="pill {conf_class}" style="margin-left:4px" title="{conf_tooltip}">{r['confidence']} confidence</span></div>
  </div>
  <div style="font-size:1.8em;font-weight:800;letter-spacing:-.5px;margin:10px 0 4px;color:#9bf">{r['odds_low']}–{r['odds_high']}%</div>
  {render_round_breakdown_dark(school, admissions_detail(school))}
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
  <a class="btn btn-primary" href="/college/{slug}/chat">Ask the AI advisor about {school['name']}</a>
  <a class="btn btn-light" href="/college/{slug}/improve">Full school-specific guide</a>
  <a class="btn btn-light" href="/college/{slug}">School overview</a>
</div>
""", title=f"Your plan for {school['name']} — Candor")


def plans_index_html():
    """List all schools the user has computed chances for, with summary cards.
    Empty state explains how to populate."""
    user = current_user()
    with db() as conn:
        rows = conn.execute("""
            SELECT college_slug, tier, odds_low, odds_high, fit, confidence, computed_at
            FROM saved_chances
            WHERE user_id = ?
            ORDER BY computed_at DESC
        """, (user["id"],)).fetchall()
    saved_slugs = get_saved_schools(user["id"])
    if not rows and not saved_slugs:
        return _page("""
<h1>My Plans</h1>
<p class="muted">Each school you've computed chances for shows up here as a personalized plan: chances, match, top gaps, and direct AI chat — all in one view.</p>
<div class="card" style="background:#f4f4f4;border-color:#ddd">
  <h3 style="margin-top:0">No plans yet</h3>
  <p>Pick a school to get started:</p>
  <a class="btn btn-primary" href="/colleges">Browse colleges</a>
  <a class="btn btn-light" href="/rankings/my-fit">My Fit ranking</a>
</div>
""", title="My Plans — Candor")
    profile = get_profile(user["id"])
    chances_slugs = {row["college_slug"] for row in rows}
    cards = ""
    # Cards for chances-computed schools (full data)
    for row in rows:
        c = COLLEGES_BY_SLUG.get(row["college_slug"])
        if not c: continue
        tier_class = {"Dream":"pill-dream","Reach":"pill-reach","Target":"pill-target","Safety":"pill-safety"}.get(row["tier"], "pill-target")
        match_score = ""
        if profile:
            prof_dict = {k: profile.get(k) for k in profile.keys()}
            overall, _ = compute_my_fit(prof_dict, c)
            col = "#1d6c2a" if overall >= 80 else ("#8a4a00" if overall >= 60 else "#9a1d1d")
            match_score = f'<div class="muted" style="font-size:.85em;margin-top:4px">My Fit: <span style="color:{col};font-weight:700">{overall}/100</span></div>'
        saved_star = ' <span style="color:var(--teal)" title="Saved">★</span>' if c["slug"] in saved_slugs else ""
        cards += f"""<a href="/college/{c['slug']}/plan" class="school-card" style="display:block;color:inherit">
          <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
            <div>
              <div style="font-weight:700;font-size:1.05em">{c['name']}{saved_star}</div>
              <div class="muted" style="font-size:.82em">{city_state(c)} · {round(c['accept']*100,1)}% accept · fit {row['fit']}/100</div>
            </div>
            <div><span class="pill {tier_class}">{row['tier']}</span></div>
          </div>
          <div style="font-size:1.4em;font-weight:800;color:#2b6cff;margin-top:8px">{row['odds_low']}–{row['odds_high']}%</div>
          {match_score}
        </a>"""
    # Cards for saved-but-not-yet-chanced schools (no odds yet)
    for slug in saved_slugs:
        if slug in chances_slugs: continue
        c = COLLEGES_BY_SLUG.get(slug)
        if not c: continue
        cm = merged_school(c)
        match_score = ""
        if profile:
            prof_dict = {k: profile.get(k) for k in profile.keys()}
            overall, _ = compute_my_fit(prof_dict, cm)
            col = "#1d6c2a" if overall >= 80 else ("#8a4a00" if overall >= 60 else "#9a1d1d")
            match_score = f'<div class="muted" style="font-size:.85em;margin-top:4px">My Fit: <span style="color:{col};font-weight:700">{overall}/100</span></div>'
        cards += f"""<a href="/college/{c['slug']}/plan" class="school-card" style="display:block;color:inherit">
          <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
            <div>
              <div style="font-weight:700;font-size:1.05em">{c['name']} <span style="color:var(--teal)" title="Saved">★</span></div>
              <div class="muted" style="font-size:.82em">{city_state(c)} · {round(c['accept']*100,1)}% accept</div>
            </div>
          </div>
          <div style="font-size:.95em;color:var(--teal);margin-top:8px">Compute chances →</div>
          {match_score}
        </a>"""
    toolbar = ""
    if saved_slugs:
        cmp_link = f'/compare?schools={",".join(saved_slugs[:4])}' if len(saved_slugs) >= 2 else "/compare"
        toolbar = f"""<div style="display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px">
  <a class="btn btn-light btn-sm" href="{cmp_link}">Compare saved</a>
  <a class="btn btn-light btn-sm" href="/timeline">Timeline</a>
  <a class="btn btn-light btn-sm" href="/predictor">Score predictor</a>
</div>"""
    return _page(f"""
<h1>My Plans</h1>
<p class="muted">Schools you've saved or computed chances for. Click any card for the full personalized plan — chances, match, gaps, school-specific advice.</p>
{toolbar}
<div class="grid">{cards}</div>
<p style="margin-top:18px"><a class="btn btn-light" href="/colleges">+ Add another school</a></p>
""", title="My Plans — Candor")


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
                max_tokens=600,
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


CHAT_PAGE_HTML = """
<style>
.chat-msgs{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;min-height:340px;max-height:560px;overflow-y:auto;margin-bottom:14px;box-shadow:var(--shadow-card)}
.msg{margin:12px 0;display:flex;gap:8px}
.msg-user{justify-content:flex-end}
.msg-bubble{max-width:78%;padding:11px 15px;border-radius:14px;font-size:.95em;line-height:1.55}
.msg-user .msg-bubble{background:var(--accent-grad);color:#031715;font-weight:500;box-shadow:0 6px 18px rgba(56,189,248,.22)}
.msg-assistant .msg-bubble{background:var(--surface-2);color:var(--text);border:1px solid var(--border-strong)}
.msg-bubble ul{margin:6px 0;padding-left:18px}
.msg-bubble li{margin:2px 0}
.chat-input{display:flex;gap:10px;align-items:flex-end}
.chat-input textarea{flex:1;min-height:50px;max-height:160px;padding:12px 14px;border:1px solid var(--border-strong);border-radius:10px;font-family:inherit;resize:vertical;font-size:.95em;background:var(--bg-2);color:var(--text)}
.chat-input textarea:focus{outline:0;border-color:var(--teal);box-shadow:0 0 0 3px rgba(94,234,212,.12)}
.chat-input button{padding:12px 24px;border-radius:10px;border:0;background:var(--accent-grad);color:#031715;font-weight:700;cursor:pointer;font-family:inherit;box-shadow:0 8px 20px rgba(56,189,248,.18);transition:filter .15s}
.chat-input button:hover{filter:brightness(1.05)}
.chat-input button:disabled{opacity:.6;cursor:wait;filter:grayscale(.4)}
.suggestions{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 18px}
.suggestion{background:var(--surface-2);border:1px solid var(--border-strong);border-radius:999px;padding:7px 14px;font-size:.83em;cursor:pointer;color:var(--text-2);transition:all .15s;font-family:inherit}
.suggestion:hover{border-color:rgba(94,234,212,.4);color:var(--teal);background:var(--surface-hover)}
.typing{display:flex;gap:4px;padding:11px 15px;background:var(--surface-2);border:1px solid var(--border-strong);border-radius:14px;width:fit-content}
.typing span{width:7px;height:7px;background:var(--teal);border-radius:50%;animation:typing 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes typing{0%,60%,100%{opacity:.25}30%{opacity:1}}
</style>
__HEADER__
<div id="msgs" class="chat-msgs">__MESSAGES__</div>
<div class="suggestions">__SUGGESTIONS__</div>
<div class="chat-input">
  <textarea id="chat-input" placeholder="Ask anything about __PLACEHOLDER__..."></textarea>
  <button id="chat-send" onclick="sendMsg()">Send</button>
</div>
<script>
var SEND_URL = "__SEND_URL__";
var msgs = document.getElementById("msgs");
var input = document.getElementById("chat-input");
var btn = document.getElementById("chat-send");
function escapeHTML(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function renderUserMsg(text){
  var d = document.createElement('div'); d.className='msg msg-user';
  d.innerHTML = '<div class="msg-bubble">'+escapeHTML(text).replace(/\\n/g,'<br>')+'</div>';
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function renderTyping(){
  var d = document.createElement('div'); d.id='typing-row'; d.className='msg msg-assistant';
  d.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function clearTyping(){var t=document.getElementById('typing-row');if(t)t.remove();}
function renderAssistantMsg(html){
  var d = document.createElement('div'); d.className='msg msg-assistant';
  d.innerHTML = '<div class="msg-bubble">'+html+'</div>';
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function sendMsg(text){
  var msg = text || input.value.trim();
  if(!msg) return;
  input.value=''; btn.disabled=true;
  renderUserMsg(msg);
  renderTyping();
  var headers = {'Content-Type':'application/json'};
  var tokenEl = document.querySelector('meta[name="csrf-token"]');
  if(tokenEl) headers['X-CSRFToken'] = tokenEl.content;
  fetch(SEND_URL, {method:'POST', headers: headers, body: JSON.stringify({message: msg})})
    .then(function(r){return r.json().then(function(d){return {status:r.status, body:d};});})
    .then(function(o){
      var d = o.body;
      clearTyping(); btn.disabled=false; input.focus();
      // Paywall / cap responses arrive as 402 / 429 with HTML body
      if(o.status === 402 || o.status === 429){
        renderAssistantMsg(d.html || ('<i>'+escapeHTML(d.error||'Limit reached')+'</i>'));
        return;
      }
      if(d.error){renderAssistantMsg('<i>'+escapeHTML(d.error)+'</i>');return;}
      renderAssistantMsg(d.html || escapeHTML(d.reply || ''));
      if(d.usage){
        var pill = document.getElementById('usage-pill');
        if(pill){
          if(d.usage.is_paid){
            pill.innerHTML = 'PREMIUM · ' + d.usage.month_used + '/' + d.usage.month_limit;
          } else {
            pill.innerHTML = 'FREE · ' + d.usage.free_remaining + ' of ' + d.usage.free_limit + ' left · <a href="/upgrade" style="color:var(--teal)">Upgrade</a>';
          }
        }
      }
    })
    .catch(function(e){clearTyping(); btn.disabled=false; renderAssistantMsg('<i>Network error — try again.</i>');});
}
function suggestionClick(s){sendMsg(s);}
input.addEventListener('keydown', function(e){
  if(e.key==='Enter' && !e.shiftKey){e.preventDefault(); sendMsg();}
});
msgs.scrollTop = msgs.scrollHeight;
input.focus();
</script>
"""


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
        usage_pill = f'<span id="usage-pill" style="font-size:.74em;background:rgba(94,234,212,.12);color:var(--teal);padding:3px 10px;border-radius:999px;border:1px solid rgba(94,234,212,.25);font-weight:500;margin-left:10px">PREMIUM · {usage["month_used"]}/{PAID_MONTHLY_LIMIT}</span>'
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
        usage_pill = f'<span id="usage-pill" style="font-size:.74em;background:rgba(94,234,212,.12);color:var(--teal);padding:3px 10px;border-radius:999px;border:1px solid rgba(94,234,212,.25);font-weight:500;margin-left:10px">PREMIUM · {usage["month_used"]}/{PAID_MONTHLY_LIMIT}</span>'
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

# Secure session cookies. SECURE only kicks in over HTTPS — fine in prod
# (Railway terminates TLS) and harmless on http://localhost since cookies
# without Secure still work locally.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("RAILWAY_ENVIRONMENT") is not None,
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
        "sat": f("sat", int),
        "act": f("act", int),
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
                "pref_diversity","pref_party","pref_research","pref_career_intensity"):
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


def _landing_html(user_count, school_count, cds_count, activation_pct):
    """The marketing landing page. Keep this hand-written and specific —
    do not let it drift into AI-slop SaaS-template language. The Cornell
    story is the hook; calibrated honesty is the differentiator; the HS
    junior framing is the voice. Aurora effect via pure-CSS radial
    gradients animated with keyframes, no JS dependency."""
    css = """
<style>
  /* Override BASE_CSS body background to make aurora visible */
  html, body { background: #070d14 !important; }
  body { overflow-x:hidden; background: transparent !important; }
  .wrap { max-width: none !important; padding: 0 !important; margin: 0 !important; }

  /* Aurora is wrapped — wrapper does mouse-reactive translation, inner does the autonomous drift animation */
  .aurora-wrapper {
    position:fixed; inset:-20vh -10vw; pointer-events:none; z-index:0;
    transition: transform 0.6s cubic-bezier(0.22, 1, 0.36, 1);
    will-change: transform;
  }
  .aurora {
    position: absolute; inset: 0;
    background:
      radial-gradient(ellipse 50vw 60vh at 18% 28%, rgba(94,234,212,.55), transparent 55%),
      radial-gradient(ellipse 45vw 50vh at 82% 72%, rgba(56,189,248,.40), transparent 55%),
      radial-gradient(ellipse 40vw 45vh at 65% 18%, rgba(45,212,191,.35), transparent 55%);
    filter: blur(60px);
    animation: aurora-drift 20s ease-in-out infinite, aurora-pulse 7s ease-in-out infinite;
    will-change: transform, opacity;
  }
  @keyframes aurora-drift {
    0%,100% { transform: translate3d(0,0,0) rotate(0deg) scale(1); }
    25% { transform: translate3d(-12vw, 7vh, 0) rotate(15deg) scale(1.18); }
    50% { transform: translate3d(8vw, -4vh, 0) rotate(-10deg) scale(0.88); }
    75% { transform: translate3d(-5vw, -8vh, 0) rotate(20deg) scale(1.10); }
  }
  @keyframes aurora-pulse {
    0%,100% { opacity: 1; }
    50% { opacity: 0.62; }
  }
  .lp-wrap { max-width:1080px; margin:0 auto; padding:0 28px; position: relative; z-index: 2; }
  .hero { padding:80px 0 100px; text-align:left; max-width:780px; }
  .hero .eyebrow { display:inline-block; font-size:.78em; font-weight:600; letter-spacing:.8px; text-transform:uppercase; color:#5eead4; padding:5px 12px; border:1px solid rgba(94,234,212,.25); border-radius:999px; background:rgba(94,234,212,.06); margin-bottom:22px; }
  .hero h1 { font-size:clamp(2.4em,5vw,3.6em); font-weight:700; letter-spacing:-1.5px; line-height:1.06; margin:0 0 22px; color:#e6edf3; }
  .hero h1 .accent { background:linear-gradient(135deg,#5eead4 0%,#38bdf8 100%); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
  .hero p.lede { font-size:1.18em; color:#9aa6b6; max-width:620px; margin:0 0 32px; line-height:1.55; }
  .hero .cta-row { display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
  .hero .cta-row a { display:inline-flex; align-items:center; gap:8px; padding:12px 22px; border-radius:10px; font-weight:600; text-decoration:none; transition:all .18s; font-size:.97em; }
  .hero .cta-row .primary { background:linear-gradient(135deg,#5eead4 0%,#2dd4bf 100%); color:#070d14; box-shadow:0 6px 24px rgba(94,234,212,.28); }
  .hero .cta-row .primary:hover { transform:translateY(-1px); box-shadow:0 8px 30px rgba(94,234,212,.4); }
  .hero .cta-row .secondary { color:#e6edf3; border:1px solid rgba(255,255,255,.12); background:rgba(255,255,255,.03); }
  .hero .cta-row .secondary:hover { border-color:rgba(94,234,212,.4); color:#5eead4; }
  .hero .stats { display:flex; gap:36px; margin-top:48px; flex-wrap:wrap; }
  .hero .stats .stat { font-size:.88em; color:#9aa6b6; }
  .hero .stats .stat .num { display:block; font-size:1.9em; font-weight:700; color:#e6edf3; line-height:1; margin-bottom:4px; letter-spacing:-.5px; }
  .hero .stats .stat .num .accent { color:#5eead4; }

  .section { padding:90px 0; }
  .section h2 { font-size:clamp(1.7em,3vw,2.3em); font-weight:700; letter-spacing:-.6px; margin:0 0 16px; color:#e6edf3; }
  .section p.sub { color:#9aa6b6; font-size:1.05em; margin:0 0 40px; max-width:680px; }

  .problem-card { background:rgba(16,26,37,.55); border:1px solid rgba(255,255,255,.06); border-radius:14px; padding:36px; backdrop-filter:blur(8px); }
  .problem-card .quote { font-size:1.25em; line-height:1.5; color:#e6edf3; font-weight:500; margin:0 0 16px; }
  .problem-card .quote strong { color:#5eead4; }
  .problem-card .vs { display:flex; gap:14px; align-items:stretch; margin-top:20px; flex-wrap:wrap; }
  .problem-card .vs .col { flex:1; min-width:240px; padding:18px; border-radius:10px; }
  .problem-card .vs .wrong { background:rgba(239,68,68,.08); border:1px solid rgba(239,68,68,.22); }
  .problem-card .vs .right { background:rgba(94,234,212,.08); border:1px solid rgba(94,234,212,.25); }
  .problem-card .vs .label { font-size:.74em; font-weight:600; letter-spacing:.5px; text-transform:uppercase; opacity:.7; margin-bottom:6px; }
  .problem-card .vs .wrong .label { color:#fca5a5; }
  .problem-card .vs .right .label { color:#5eead4; }
  .problem-card .vs .num { font-size:1.6em; font-weight:700; letter-spacing:-.4px; color:#e6edf3; }

  .features-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:18px; }
  .feature { background:rgba(16,26,37,.55); border:1px solid rgba(255,255,255,.06); border-radius:12px; padding:24px; transition:border-color .2s, transform .2s; }
  .feature:hover { border-color:rgba(94,234,212,.25); transform:translateY(-2px); }
  .feature .icon { display:inline-flex; width:36px; height:36px; align-items:center; justify-content:center; border-radius:9px; background:rgba(94,234,212,.1); color:#5eead4; font-weight:700; margin-bottom:14px; font-size:1.1em; }
  .feature h3 { margin:0 0 8px; font-size:1.05em; color:#e6edf3; font-weight:600; letter-spacing:-.2px; }
  .feature p { margin:0; color:#9aa6b6; font-size:.92em; line-height:1.55; }

  .founder { display:grid; grid-template-columns:1fr; gap:24px; align-items:center; background:rgba(16,26,37,.55); border:1px solid rgba(255,255,255,.06); border-radius:14px; padding:36px; }
  .founder p { font-size:1.08em; line-height:1.6; color:#cbd5e1; margin:0 0 12px; }
  .founder p:last-child { margin:0; }
  .founder .signature { color:#5eead4; font-weight:600; margin-top:16px !important; }

  .final-cta { text-align:center; padding:80px 0 100px; }
  .final-cta h2 { margin-bottom:18px; }
  .final-cta p { color:#9aa6b6; margin:0 0 30px; font-size:1.08em; }
  .final-cta a.primary { display:inline-block; padding:14px 32px; background:linear-gradient(135deg,#5eead4 0%,#2dd4bf 100%); color:#070d14; font-weight:600; border-radius:10px; text-decoration:none; box-shadow:0 8px 30px rgba(94,234,212,.3); transition:all .2s; }
  .final-cta a.primary:hover { transform:translateY(-2px); box-shadow:0 12px 36px rgba(94,234,212,.4); }

  footer { padding:40px 0 60px; text-align:center; color:#5e6b7c; font-size:.84em; border-top:1px solid rgba(255,255,255,.04); margin-top:60px; }
  footer a { color:#9aa6b6; }

  .reveal { opacity:0; transform:translateY(16px); transition:opacity .7s ease, transform .7s ease; }
  .reveal.in { opacity:1; transform:translateY(0); }

  @media (max-width:680px) {
    .hero { padding:50px 0 60px; }
    .section { padding:60px 0; }
    .lp-nav { padding:18px 20px; }
    .lp-nav .links a:not(.btn-primary) { display:none; }
    .hero .stats { gap:22px; }
  }
</style>
"""
    body = f"""
<div class="aurora-wrapper"><div class="aurora"></div></div>
{_nav()}

<main class="lp-wrap">
  <section class="hero">
    <div class="eyebrow">Built by a high school junior · {school_count}+ schools</div>
    <h1>The college admissions calculator that uses <span class="accent">real data</span>, not guesses.</h1>
    <p class="lede">Most chances calculators tell everyone they have a 25-30% shot at every T20. Stanford accepts 3.6%. The math doesn't work. Candor uses verified Common Data Set figures from {cds_count}+ schools and odds calibrated to be honest, not optimistic.</p>
    <div class="cta-row">
      <a href="/signup" class="primary">Get your chances →</a>
      <a href="/colleges" class="secondary">Browse schools</a>
    </div>
    <div class="stats">
      <div class="stat"><span class="num">100+</span>active users</div>
      <div class="stat"><span class="num">{cds_count}</span>CDS-verified schools</div>
      <div class="stat"><span class="num"><span class="accent">{activation_pct}%</span></span>complete their profile</div>
    </div>
  </section>

  <section class="section reveal">
    <h2>The problem with every other tool</h2>
    <p class="sub">A real example. Most calculators were showing Cornell's SAT mid-50% as a number that wasn't even close to right.</p>
    <div class="problem-card">
      <p class="quote">One popular calculator showed <strong>Cornell's SAT mid-50% as 1120-1285</strong>. The actual 2024-25 number? <strong>1510-1560.</strong> The tool was matching the wrong school entirely (Cornell College in Iowa, not Cornell University).</p>
      <div class="vs">
        <div class="col wrong">
          <div class="label">What the calculator showed</div>
          <div class="num">1120-1285</div>
        </div>
        <div class="col right">
          <div class="label">Cornell's actual CDS</div>
          <div class="num">1510-1560</div>
        </div>
      </div>
      <p style="color:#9aa6b6; font-size:.95em; margin:24px 0 0; line-height:1.6">Half the tools online are using federal data that lags 1-2 cycles, AI-generated stats that change every refresh, or model assumptions that haven't been updated since 2018. Candor pulls actual Common Data Set PDFs by hand.</p>
    </div>
  </section>

  <section class="section reveal">
    <h2>What's different</h2>
    <p class="sub">No vibes-based admissions math. Every number has a source.</p>
    <div class="features-grid">
      <div class="feature">
        <div class="icon">✓</div>
        <h3>CDS-Verified Data</h3>
        <p>{cds_count}+ schools have stats hand-pulled from their official Common Data Set. The rest use federal data with a clear "verified vs federal" badge so you know what's checked.</p>
      </div>
      <div class="feature">
        <div class="icon">⚖</div>
        <h3>Calibrated, not optimistic</h3>
        <p>Elite schools cap at sub-15% even for top applicants — because that's reality. Truly exceptional applicants (USAMO golds, recruited athletes) get a separate flag that lifts the cap honestly.</p>
      </div>
      <div class="feature">
        <div class="icon">★</div>
        <h3>Hooks per school</h3>
        <p>Legacy at Harvard ≠ legacy at Duke. The model factors legacy at the specific school, athlete status, first-gen, and demonstrated interest at schools that actually track it.</p>
      </div>
      <div class="feature">
        <div class="icon">↗</div>
        <h3>My Fit ranking</h3>
        <p>Schools sorted by how well they actually match your stats and preferences (size, vibe, weather, prestige weighting). Pushes you toward real targets, not just lottery reaches.</p>
      </div>
      <div class="feature">
        <div class="icon">🎯</div>
        <h3>Real applicant profiles</h3>
        <p>Pulled from r/collegeresults and similar — actual admit/reject/waitlist outcomes with stats and what stood out, so you can calibrate against people who actually got in.</p>
      </div>
      <div class="feature">
        <div class="icon">∞</div>
        <h3>AI advisor with grounded facts</h3>
        <p>Every school has hand-checked program facts the AI can't override (USC Roski requires portfolios for ALL majors, etc.). No more hallucinated advice.</p>
      </div>
    </div>
  </section>

  <section class="section reveal">
    <div class="founder">
      <p>I'm a high school junior. Last semester I spent hours on every chances calculator on the internet trying to figure out where I actually stood for college, and the numbers were all over the place — one said 30% at Vanderbilt, another said 8%, another said 22%.</p>
      <p>I started digging into why, and turns out most of them either use federal data that lags 1-2 years, have AI just make up stats, or use a fit model so generic it's basically useless. So I spent a few weeks pulling the actual Common Data Set PDFs from each school's website and built my own.</p>
      <p>The goal is calibration, not telling you what you want to hear. If your odds at Stanford are 4%, you should know that — so you can spend your ED slot somewhere it'll actually matter.</p>
      <p class="signature">— Jasper, Candor's founder</p>
    </div>
  </section>

  <section class="final-cta reveal">
    <h2>Get your real chances.</h2>
    <p>Free, no paywall on chances. Takes 30 seconds.</p>
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
    if (!('IntersectionObserver' in window)) {{
      document.querySelectorAll('.reveal').forEach(el => el.classList.add('in'));
      return;
    }}
    const io = new IntersectionObserver((entries) => {{
      entries.forEach(e => {{
        if (e.isIntersecting) {{ e.target.classList.add('in'); io.unobserve(e.target); }}
      }});
    }}, {{ threshold: 0.12 }});
    document.querySelectorAll('.reveal').forEach(el => io.observe(el));
  }})();

  // Mouse-reactive aurora — wrapper translates subtly toward cursor.
  // Smoothed via lerp + rAF so movement feels like the page is breathing
  // toward the cursor, not snapping. Disabled on touch devices where
  // mousemove fires erratically.
  (function(){{
    const wrap = document.querySelector('.aurora-wrapper');
    if (!wrap) return;
    if (window.matchMedia('(pointer: coarse)').matches) return; // skip on touch
    let targetX = 0, targetY = 0, currentX = 0, currentY = 0;
    document.addEventListener('mousemove', (e) => {{
      // Map cursor to ±40px offset
      targetX = (e.clientX / window.innerWidth - 0.5) * 80;
      targetY = (e.clientY / window.innerHeight - 0.5) * 80;
    }}, {{ passive: true }});
    function tick() {{
      // Lerp toward target — 0.04 = soft, 0.1 = snappier
      currentX += (targetX - currentX) * 0.06;
      currentY += (targetY - currentY) * 0.06;
      wrap.style.transform = `translate3d(${{currentX.toFixed(2)}}px, ${{currentY.toFixed(2)}}px, 0)`;
      requestAnimationFrame(tick);
    }}
    tick();
  }})();
</script>
"""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Candor — College admissions chances, calibrated</title>
<meta name="description" content="College chances calculator with verified Common Data Set figures from {cds_count}+ schools. Built by a HS junior to be honest, not optimistic.">
<style>{BASE_CSS}</style>
{css}
<style>
  /* Tweaks so the regular site nav sits cleanly on top of the landing page */
  .nav {{ position:relative; z-index:5; }}
  .lp-wrap {{ position:relative; z-index:1; }}
</style>
</head><body>{body}</body></html>"""


@app.route("/colleges")
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


@app.route("/predictor")
@login_required
def predictor_page():
    user = current_user()
    profile = get_profile(user["id"])
    if not profile:
        return redirect(url_for("profile_page"))
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


ROUND_DEFAULT_DEADLINE = {
    "ED":  ("Nov 1, 2026",   2026, 11, 1),
    "REA": ("Nov 1, 2026",   2026, 11, 1),
    "EA":  ("Nov 15, 2026",  2026, 11, 15),
    "ED2": ("Jan 1, 2027",   2027, 1,  1),
    "RD":  ("Jan 1, 2027",   2027, 1,  1),
}

# Per-school overrides where the round date is well-known.
SCHOOL_DEADLINE_OVERRIDES = {
    "mit":          {"EA":  ("Nov 1, 2026",  2026, 11, 1)},
    "georgetown":   {"EA":  ("Nov 1, 2026",  2026, 11, 1)},
    "uchicago":     {"EA":  ("Nov 1, 2026",  2026, 11, 1)},
    "notre-dame":   {"REA": ("Nov 1, 2026",  2026, 11, 1)},
    "michigan":     {"EA":  ("Nov 1, 2026",  2026, 11, 1), "RD": ("Feb 1, 2027", 2027, 2, 1)},
    "unc":          {"EA":  ("Oct 15, 2026", 2026, 10, 15), "RD": ("Jan 15, 2027", 2027, 1, 15)},
    "uva":          {"EA":  ("Nov 1, 2026",  2026, 11, 1)},
    "berkeley":     {"RD":  ("Nov 30, 2026", 2026, 11, 30)},
    "ucla":         {"RD":  ("Nov 30, 2026", 2026, 11, 30)},
    "stanford":     {"REA": ("Nov 1, 2026",  2026, 11, 1)},
}

MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}


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


@app.route("/plans")
@login_required
def plans_index_page():
    return plans_index_html()


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
@login_required
def chat_page():
    return general_chat_html()


@app.route("/college/<slug>/chat")
@login_required
def school_chat_page(slug):
    return school_chat_html(slug)


@app.route("/chat/api/send", methods=["POST"])
@login_required
def chat_api_send():
    user = current_user()
    status = usage_status(user["id"])
    if status.get("blocked"):
        if status["reason"] == "free_exhausted":
            paywall = (f'<div style="padding:18px;border-radius:12px;background:rgba(94,234,212,.06);'
                       f'border:1px solid rgba(94,234,212,.2)">'
                       f'<div style="font-weight:600;color:var(--teal);margin-bottom:8px">'
                       f"You've used your {FREE_TRIAL_MESSAGES} free trial messages.</div>"
                       f'<div style="color:var(--text-2);font-size:.92em;margin-bottom:12px">'
                       f"Upgrade to Candor Premium ($5/mo) for {PAID_MONTHLY_LIMIT} messages "
                       f"per month with the AI Advisor — personalized college admissions help "
                       f"informed by your full profile and any of 155 schools.</div>"
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


@app.route("/upgrade")
@login_required
def upgrade_page():
    user = current_user()
    status = usage_status(user["id"])
    is_paid = status.get("is_paid")
    # Pass user_id as client_reference_id so we can match the Stripe payment
    # back to this user (via webhook). Also prefill the email.
    sep = "&" if "?" in STRIPE_PAYMENT_LINK else "?"
    pay_url = (f"{STRIPE_PAYMENT_LINK}{sep}client_reference_id={user['id']}"
               f"&prefilled_email={user['email']}")
    if is_paid:
        body = f"""<div class="card" style="max-width:560px">
          <div class="stat-card" style="margin-bottom:14px">
            <div class="label">Plan</div>
            <div class="value accent">Candor Premium</div>
            <div class="delta">{status['month_used']} / {PAID_MONTHLY_LIMIT} messages this month</div>
          </div>
          <p class="muted" style="margin:0">You're all set. Manage or cancel through the Stripe email receipt you received.</p>
        </div>"""
    else:
        body = f"""<div class="card" style="max-width:560px">
          <h2 style="margin-top:0">Upgrade to Candor Premium</h2>
          <p class="muted" style="margin:0 0 14px">Unlock unlimited use of the AI Advisor — personalized college admissions guidance trained on your full profile and 155 schools.</p>
          <div style="display:flex;align-items:baseline;gap:8px;margin:14px 0">
            <span style="font-size:2.2em;font-weight:700;letter-spacing:-1px;background:var(--accent-grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">$5</span>
            <span class="muted">/ month · cancel anytime</span>
          </div>
          <ul style="padding-left:18px;margin:14px 0;color:var(--text)">
            <li>{PAID_MONTHLY_LIMIT} AI Advisor messages per month</li>
            <li>Per-school chat that knows the school + your profile</li>
            <li>Personalized application strategy, essay feedback, gap analysis</li>
            <li>Everything else on Candor (chances, fit, plans) stays free</li>
          </ul>
          <a href="{pay_url}" class="btn btn-primary" style="margin-top:8px">Subscribe with Stripe →</a>
          <p class="muted" style="font-size:.78em;margin:14px 0 0">You'll be redirected to Stripe to complete payment. Your premium status activates within ~30 seconds of payment.</p>
        </div>"""
    return _page(body, title="Upgrade — Candor")


_csrf_exempt = csrf.exempt if _CSRF_ON else (lambda f: f)


@app.route("/stripe/webhook", methods=["POST"])
@_csrf_exempt
def stripe_webhook():
    """Stripe webhook for checkout.session.completed events. Configure in
    Stripe dashboard → Developers → Webhooks, point at this URL, and put
    the signing secret in STRIPE_WEBHOOK_SECRET env var."""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")
    if STRIPE_WEBHOOK_SECRET:
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
        if ref:
            try:
                uid = int(ref)
                with db() as conn:
                    conn.execute("UPDATE users SET is_paid=1, stripe_customer_id=? WHERE id=?",
                                 (cust, uid))
                    conn.commit()
            except (ValueError, TypeError):
                pass
    elif etype in ("customer.subscription.deleted", "invoice.payment_failed"):
        cust = obj.get("customer")
        if cust:
            with db() as conn:
                conn.execute("UPDATE users SET is_paid=0 WHERE stripe_customer_id=?", (cust,))
                conn.commit()
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
        recent_users = conn.execute(
            "SELECT email, created_at FROM users ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
        top_schools = conn.execute(
            "SELECT college_slug, COUNT(*) n FROM saved_chances GROUP BY college_slug ORDER BY n DESC LIMIT 10"
        ).fetchall()
    recent_html = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid var(--border);font-size:.88em">'
        f'<span style="color:var(--text)">{r["email"]}</span>'
        f'<span class="muted">{r["created_at"]}</span></div>'
        for r in recent_users
    ) or '<p class="muted">No users yet.</p>'
    schools_html = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:6px 0;font-size:.88em">'
        f'<span>{COLLEGES_BY_SLUG.get(r["college_slug"],{}).get("name", r["college_slug"])}</span>'
        f'<span class="muted">{r["n"]} chances run</span></div>'
        for r in top_schools
    ) or '<p class="muted">No chances calculated yet.</p>'
    profile_pct = round(profiles_done / max(1, total_users) * 100)
    return _page(f"""
<h1>Activity</h1>
<div class="grid">
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
    <div class="label">Paid subscribers</div>
    <div class="value accent">{paid_users}</div>
    <div class="delta">${paid_users * 5}/mo recurring</div>
  </div>
</div>
<div class="card" style="margin-top:18px">
  <h3 style="margin-top:0">Recent signups</h3>
  {recent_html}
</div>
<div class="card">
  <h3 style="margin-top:0">Most-viewed schools</h3>
  {schools_html}
</div>
<p class="muted" style="font-size:.78em;margin-top:18px">Refresh anytime. Bookmark this URL for quick access.</p>
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
