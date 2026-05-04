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
ARTICLE_TTL_HOURS = 12   # how long to cache per-college articles
SCORECARD_TTL_DAYS = 30  # refresh federal stats monthly


# ─── COLLEGE DATA (~80 schools) ──────────────────────────
# Each entry: name, slug, accept_rate, gpa_lo/hi, sat_25/75, act_25/75, type,
# state, size (rough enrollment), tuition (annual sticker), description,
# popular majors. Numbers are public-CDS-ballpark, intended as orientation
# not gospel. Tier 1 = sub-10% accept, 5 = accessible.
COLLEGES = [
    # --- Tier 1 elites ---
    {"name":"Harvard","slug":"harvard","accept":0.034,"gpa_lo":3.93,"gpa_hi":4.00,"sat_25":1500,"sat_75":1580,"act_25":34,"act_75":36,"tier":1,"type":"private","state":"Massachusetts","size":7200,"tuition":59000,"desc":"The oldest US university and the cultural prototype for American elite higher education. Strong in basically everything; particular standouts in economics, government, history, and pre-med.","majors":["Economics","Government","Computer Science","Biology","Social Studies"]},
    {"name":"Stanford","slug":"stanford","accept":0.036,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1500,"sat_75":1580,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"California","size":7800,"tuition":62000,"desc":"Silicon Valley's home university and the dominant feeder into tech entrepreneurship. CS, engineering, and human biology are the marquee programs; the alumni network is unparalleled in startups.","majors":["Computer Science","Human Biology","Engineering","Economics","Symbolic Systems"]},
    {"name":"MIT","slug":"mit","accept":0.041,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1530,"sat_75":1580,"act_25":35,"act_75":36,"tier":1,"type":"private","state":"Massachusetts","size":4600,"tuition":60000,"desc":"The world's leading STEM-focused university. Ruthlessly quantitative culture; strongest in CS, physics, mechanical/electrical engineering, and economics.","majors":["Computer Science","Mechanical Engineering","Mathematics","Physics","Economics"]},
    {"name":"Yale","slug":"yale","accept":0.046,"gpa_lo":3.94,"gpa_hi":4.00,"sat_25":1500,"sat_75":1570,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Connecticut","size":6500,"tuition":63000,"desc":"Strong humanities and social-sciences orientation, pre-law/finance pipeline, residential college system that drives a tight-knit campus culture.","majors":["Economics","Political Science","History","Computer Science","Biology"]},
    {"name":"Princeton","slug":"princeton","accept":0.046,"gpa_lo":3.93,"gpa_hi":4.00,"sat_25":1510,"sat_75":1570,"act_25":34,"act_75":35,"tier":1,"type":"private","state":"New Jersey","size":5500,"tuition":59000,"desc":"Undergraduate-focused (no law/med/business school competing for attention). Very strong in math, physics, and public policy. Generous financial aid.","majors":["Computer Science","Economics","Public and International Affairs","Mathematics","Engineering"]},
    {"name":"Columbia","slug":"columbia","accept":0.039,"gpa_lo":3.91,"gpa_hi":4.00,"sat_25":1490,"sat_75":1570,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"New York","size":8800,"tuition":67000,"desc":"Manhattan campus with a famous core curriculum every undergrad takes. Strong in finance recruiting, journalism, and humanities.","majors":["Economics","Political Science","Computer Science","Engineering","English"]},
    {"name":"University of Chicago","slug":"uchicago","accept":0.054,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1510,"sat_75":1580,"act_25":34,"act_75":35,"tier":1,"type":"private","state":"Illinois","size":7500,"tuition":63000,"desc":"Famous intellectual rigor and quirky 'where fun goes to die' culture. World-class economics (Friedman heritage), strong in math/physics/PoliSci.","majors":["Economics","Mathematics","Public Policy","Biology","Computer Science"]},
    {"name":"UPenn","slug":"upenn","accept":0.058,"gpa_lo":3.88,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Pennsylvania","size":10300,"tuition":63000,"desc":"Wharton is the dominant undergraduate business program in the country. Strong cross-school flexibility (M&T, Huntsman, Vagelos).","majors":["Finance","Economics","Computer Science","Nursing","Biology"]},
    {"name":"Brown","slug":"brown","accept":0.054,"gpa_lo":3.89,"gpa_hi":4.00,"sat_25":1500,"sat_75":1570,"act_25":34,"act_75":35,"tier":1,"type":"private","state":"Rhode Island","size":7300,"tuition":63000,"desc":"Open curriculum (no core requirements, no +/- grades) is the headline. Strong in CS, applied math, international relations, creative writing.","majors":["Computer Science","Economics","Biology","International Relations","English"]},
    {"name":"Dartmouth","slug":"dartmouth","accept":0.066,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"New Hampshire","size":4500,"tuition":62000,"desc":"Smallest of the Ivies, undergrad-focused, quarter system, strong outdoor/Greek culture. Disproportionate finance-recruiting presence.","majors":["Economics","Government","Computer Science","Engineering","Biology"]},
    {"name":"Duke","slug":"duke","accept":0.060,"gpa_lo":3.88,"gpa_hi":4.00,"sat_25":1490,"sat_75":1570,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"North Carolina","size":6700,"tuition":62000,"desc":"Strong sports culture (basketball), Trinity College of Arts & Sciences plus Pratt engineering. Heavy pre-med and finance pipelines.","majors":["Economics","Public Policy","Computer Science","Biology","Engineering"]},
    {"name":"Northwestern","slug":"northwestern","accept":0.072,"gpa_lo":3.86,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Illinois","size":8500,"tuition":63000,"desc":"Quarter system, strong journalism (Medill) and theater programs, integrated marketing communications, growing CS.","majors":["Economics","Computer Science","Journalism","Engineering","Communication Studies"]},
    {"name":"Caltech","slug":"caltech","accept":0.030,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1530,"sat_75":1580,"act_25":35,"act_75":36,"tier":1,"type":"private","state":"California","size":1000,"tuition":60000,"desc":"Tiny, hyper-focused on hard sciences and engineering. Famously brutal core curriculum. JPL connection makes it the top space-research undergrad.","majors":["Physics","Computer Science","Engineering and Applied Science","Chemistry","Mathematics"]},
    # --- Tier 2: highly selective ---
    {"name":"Cornell","slug":"cornell","accept":0.075,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1470,"sat_75":1550,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"New York","size":15700,"tuition":62000,"desc":"Largest of the Ivies. Multiple distinct undergraduate colleges with very different admit rates (CALS easier than Engineering or Hotel).","majors":["Computer Science","Engineering","Biological Sciences","Hotel Administration","Economics"]},
    {"name":"Johns Hopkins","slug":"jhu","accept":0.072,"gpa_lo":3.88,"gpa_hi":4.00,"sat_25":1500,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Maryland","size":5300,"tuition":62000,"desc":"Best-known for biomedical engineering and as a pre-med pipeline. Undergrad research culture is particularly intense.","majors":["Public Health","Biomedical Engineering","Computer Science","Neuroscience","International Studies"]},
    {"name":"Vanderbilt","slug":"vanderbilt","accept":0.068,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1480,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Tennessee","size":7100,"tuition":63000,"desc":"Southern blend of academic rigor and traditional college life. Generous financial aid program.","majors":["Economics","Human and Organizational Development","Engineering","Biology","Political Science"]},
    {"name":"Rice","slug":"rice","accept":0.080,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":34,"act_75":35,"tier":2,"type":"private","state":"Texas","size":4500,"tuition":58000,"desc":"Small private university in Houston with a residential college system. Strong engineering, sciences, architecture, and music.","majors":["Engineering","Computer Science","Biosciences","Economics","Architecture"]},
    {"name":"Notre Dame","slug":"notre-dame","accept":0.117,"gpa_lo":3.79,"gpa_hi":4.00,"sat_25":1450,"sat_75":1540,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Indiana","size":8800,"tuition":62000,"desc":"Catholic university with very strong school-spirit culture. Mendoza is a top undergrad business program; engineering and pre-med are strong.","majors":["Finance","Political Science","Engineering","Biology","Economics"]},
    {"name":"Carnegie Mellon","slug":"cmu","accept":0.110,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1490,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Pennsylvania","size":7500,"tuition":63000,"desc":"Top-tier CS school (admit rate to SCS is single-digit). Also strong in engineering, drama, and design.","majors":["Computer Science","Engineering","Information Systems","Drama","Business Administration"]},
    {"name":"USC","slug":"usc","accept":0.090,"gpa_lo":3.79,"gpa_hi":4.00,"sat_25":1450,"sat_75":1530,"act_25":32,"act_75":35,"tier":2,"type":"private","state":"California","size":21000,"tuition":68000,"desc":"Marshall (business), Viterbi (engineering), Annenberg (comm), Cinematic Arts. Strong LA-industry pipelines.","majors":["Business Administration","Computer Science","Communication","Cinematic Arts","International Relations"]},
    {"name":"NYU","slug":"nyu","accept":0.080,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1450,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"New York","size":29000,"tuition":62000,"desc":"Manhattan campus with Stern (business), Tisch (arts), Steinhardt, Tandon (engineering). High urban energy and sticker price.","majors":["Business","Liberal Studies","Computer Science","Drama","Economics"]},
    {"name":"Georgetown","slug":"georgetown","accept":0.123,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":35,"tier":2,"type":"private","state":"District of Columbia","size":7500,"tuition":63000,"desc":"Jesuit, DC location, dominant in international relations and government careers. McDonough is a strong undergrad business program.","majors":["International Politics","Finance","Economics","Government","Biology"]},
    {"name":"UC Berkeley","slug":"ucb","accept":0.115,"gpa_lo":3.86,"gpa_hi":4.00,"sat_25":1340,"sat_75":1530,"act_25":30,"act_75":35,"tier":2,"type":"public","state":"California","size":33000,"tuition":15000,"desc":"Top public university in the country. Engineering, CS, and business (Haas) are powerhouses. In-state tuition is a steal; out-of-state is full freight.","majors":["Computer Science","Economics","Business Administration","Cognitive Science","Political Science"]},
    {"name":"UCLA","slug":"ucla","accept":0.090,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1370,"sat_75":1540,"act_25":31,"act_75":35,"tier":2,"type":"public","state":"California","size":33000,"tuition":15000,"desc":"Most-applied-to college in the US. Strong across the board; particularly notable for film, basketball, biology, and engineering.","majors":["Biology","Psychology","Business Economics","Political Science","Engineering"]},
    {"name":"University of Michigan","slug":"umich","accept":0.180,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1370,"sat_75":1530,"act_25":31,"act_75":34,"tier":2,"type":"public","state":"Michigan","size":33000,"tuition":17000,"desc":"Public Ivy with one of the strongest undergrad business programs (Ross), top engineering, and a college-sports culture.","majors":["Business Administration","Computer Science","Engineering","Economics","Psychology"]},
    {"name":"UVA","slug":"uva","accept":0.166,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1520,"act_25":32,"act_75":35,"tier":2,"type":"public","state":"Virginia","size":17500,"tuition":21000,"desc":"Jefferson-founded public flagship. McIntire commerce school, strong politics/foreign affairs, traditional honor code.","majors":["Economics","Commerce","Computer Science","Biology","Political Science"]},
    {"name":"Georgia Tech","slug":"gatech","accept":0.160,"gpa_lo":3.83,"gpa_hi":4.00,"sat_25":1430,"sat_75":1530,"act_25":32,"act_75":35,"tier":2,"type":"public","state":"Georgia","size":18000,"tuition":12000,"desc":"Engineering and CS-dominant public. Atlanta location and co-op program drive strong industry pipelines.","majors":["Computer Science","Mechanical Engineering","Industrial Engineering","Business","Aerospace Engineering"]},
    {"name":"Tufts","slug":"tufts","accept":0.099,"gpa_lo":3.82,"gpa_hi":4.00,"sat_25":1450,"sat_75":1540,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Massachusetts","size":6700,"tuition":68000,"desc":"Mid-sized research university with strong international relations and biomedical programs. Boston-area location.","majors":["International Relations","Computer Science","Biology","Economics","Political Science"]},
    {"name":"Wash U St. Louis","slug":"washu","accept":0.105,"gpa_lo":3.89,"gpa_hi":4.00,"sat_25":1500,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Missouri","size":8200,"tuition":63000,"desc":"Strong pre-med pipeline and Olin Business School. Generous financial aid for admitted students.","majors":["Biology","Economics","Engineering","Business","Psychology"]},
    {"name":"Emory","slug":"emory","accept":0.130,"gpa_lo":3.78,"gpa_hi":4.00,"sat_25":1430,"sat_75":1530,"act_25":31,"act_75":34,"tier":2,"type":"private","state":"Georgia","size":7100,"tuition":62000,"desc":"Atlanta-area private research university. Goizueta business school and strong pre-health pipelines (CDC ties).","majors":["Business","Biology","Economics","Psychology","Neuroscience"]},
    # --- Tier 3: very selective ---
    {"name":"UNC Chapel Hill","slug":"unc","accept":0.170,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1370,"sat_75":1500,"act_25":30,"act_75":33,"tier":3,"type":"public","state":"North Carolina","size":19000,"tuition":9000,"desc":"Public ivy of the South. Kenan-Flagler business, strong journalism, ACC sports, oldest public university in the US.","majors":["Biology","Psychology","Business Administration","Computer Science","Political Science"]},
    {"name":"Boston University","slug":"bu","accept":0.140,"gpa_lo":3.69,"gpa_hi":3.95,"sat_25":1390,"sat_75":1500,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Massachusetts","size":17600,"tuition":66000,"desc":"Large private university spread along the Charles River. Strong communications, engineering, and business.","majors":["Business","Communication","Engineering","Psychology","Economics"]},
    {"name":"Boston College","slug":"bc","accept":0.180,"gpa_lo":3.74,"gpa_hi":3.95,"sat_25":1420,"sat_75":1510,"act_25":32,"act_75":34,"tier":3,"type":"private","state":"Massachusetts","size":9500,"tuition":68000,"desc":"Jesuit university outside Boston. Carroll School of Management, strong undergrad business and finance recruiting.","majors":["Finance","Economics","Communication","Biology","Political Science"]},
    {"name":"Wake Forest","slug":"wake-forest","accept":0.210,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"North Carolina","size":5500,"tuition":67000,"desc":"Mid-sized private with strong undergrad business. Test-optional pioneer; strong honor culture.","majors":["Business","Communication","Economics","Politics","Biology"]},
    {"name":"William & Mary","slug":"wm","accept":0.330,"gpa_lo":3.78,"gpa_hi":4.00,"sat_25":1370,"sat_75":1500,"act_25":30,"act_75":33,"tier":3,"type":"public","state":"Virginia","size":6700,"tuition":24000,"desc":"Second-oldest US college. Public liberal arts feel, strong government/IR, smaller than peers.","majors":["Government","Business","Biology","Psychology","Economics"]},
    {"name":"University of Florida","slug":"uf","accept":0.230,"gpa_lo":3.90,"gpa_hi":4.45,"sat_25":1340,"sat_75":1460,"act_25":30,"act_75":33,"tier":3,"type":"public","state":"Florida","size":35000,"tuition":6400,"desc":"Florida flagship with strong agriculture, journalism, and engineering programs. In-state tuition is among the cheapest in the country for the value.","majors":["Biology","Business","Engineering","Health Sciences","Psychology"]},
    {"name":"University of Wisconsin","slug":"wisc","accept":0.490,"gpa_lo":3.65,"gpa_hi":3.97,"sat_25":1310,"sat_75":1450,"act_25":27,"act_75":32,"tier":3,"type":"public","state":"Wisconsin","size":35000,"tuition":11000,"desc":"Madison campus, strong engineering and biological sciences. Big Ten with a famous bar district adjacent to campus.","majors":["Biology","Computer Science","Economics","Business","Engineering"]},
    {"name":"University of Washington","slug":"uw","accept":0.430,"gpa_lo":3.74,"gpa_hi":3.95,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"Washington","size":36000,"tuition":12000,"desc":"Pacific Northwest research powerhouse. Strong CS (Allen School), biomedicine, and aerospace pipelines.","majors":["Computer Science","Biology","Business","Engineering","Psychology"]},
    {"name":"University of Texas at Austin","slug":"ut-austin","accept":0.310,"gpa_lo":3.69,"gpa_hi":3.96,"sat_25":1240,"sat_75":1460,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"Texas","size":40000,"tuition":11000,"desc":"Texas flagship with extreme size and breadth. McCombs business, top engineering, Cockrell, strong CS.","majors":["Business","Computer Science","Engineering","Biology","Economics"]},
    {"name":"University of Maryland","slug":"umd","accept":0.450,"gpa_lo":3.71,"gpa_hi":3.96,"sat_25":1290,"sat_75":1470,"act_25":29,"act_75":33,"tier":3,"type":"public","state":"Maryland","size":30000,"tuition":11000,"desc":"DC-area public with very strong CS/cybersecurity, business, and engineering. Honors college pulls top in-state applicants.","majors":["Computer Science","Business","Engineering","Biology","Criminology"]},
    {"name":"GWU","slug":"gwu","accept":0.490,"gpa_lo":3.65,"gpa_hi":3.92,"sat_25":1310,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"District of Columbia","size":12000,"tuition":62000,"desc":"DC location, strong international affairs and political science programs, heavy internship culture.","majors":["International Affairs","Political Science","Business","Public Health","Communication"]},
    # --- Tier 4: solid options ---
    {"name":"Penn State","slug":"penn-state","accept":0.540,"gpa_lo":3.55,"gpa_hi":3.97,"sat_25":1240,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Pennsylvania","size":40000,"tuition":19000,"desc":"Smeal business, top-tier engineering, large alumni network. Very strong PA-state pipeline; OOS still well-priced.","majors":["Business","Engineering","Biology","Communication","Information Sciences"]},
    {"name":"Ohio State","slug":"osu","accept":0.530,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1280,"sat_75":1450,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Ohio","size":47000,"tuition":12000,"desc":"Massive public flagship. Fisher (business) and engineering are strong; Big Ten sports culture.","majors":["Business","Biology","Engineering","Psychology","Communication"]},
    {"name":"Michigan State","slug":"msu","accept":0.770,"gpa_lo":3.60,"gpa_hi":3.94,"sat_25":1110,"sat_75":1310,"act_25":23,"act_75":29,"tier":4,"type":"public","state":"Michigan","size":39000,"tuition":15000,"desc":"Big Ten public with Eli Broad business, supply-chain management is nationally top-3, strong veterinary and ag programs.","majors":["Business","Communication","Biology","Psychology","Engineering"]},
    {"name":"Florida State","slug":"fsu","accept":0.370,"gpa_lo":3.85,"gpa_hi":4.40,"sat_25":1230,"sat_75":1370,"act_25":26,"act_75":30,"tier":4,"type":"public","state":"Florida","size":33000,"tuition":6500,"desc":"Tallahassee flagship. Strong film, criminology, hospitality. Bright Futures funding makes in-state extremely cheap.","majors":["Criminology","Business","Psychology","Biology","Communication"]},
    {"name":"Indiana University","slug":"iu","accept":0.820,"gpa_lo":3.51,"gpa_hi":3.93,"sat_25":1190,"sat_75":1380,"act_25":25,"act_75":31,"tier":5,"type":"public","state":"Indiana","size":33000,"tuition":11000,"desc":"Bloomington flagship. Kelley business school is a top undergrad direct-admit, music school is world-renowned.","majors":["Business","Biology","Communication","Psychology","Music"]},
    {"name":"Arizona State","slug":"asu","accept":0.880,"gpa_lo":3.39,"gpa_hi":3.86,"sat_25":1130,"sat_75":1340,"act_25":23,"act_75":29,"tier":5,"type":"public","state":"Arizona","size":75000,"tuition":12000,"desc":"Massive scale, online-friendly, growing reputation. W.P. Carey business and Cronkite journalism are highlights.","majors":["Business","Engineering","Biology","Psychology","Communication"]},
    {"name":"Purdue","slug":"purdue","accept":0.530,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1190,"sat_75":1430,"act_25":25,"act_75":33,"tier":3,"type":"public","state":"Indiana","size":37000,"tuition":10000,"desc":"Strong engineering and aviation programs. Frozen tuition since 2012 makes it a notable value.","majors":["Engineering","Computer Science","Business","Aviation","Agriculture"]},
    {"name":"University of Illinois","slug":"uiuc","accept":0.430,"gpa_lo":3.66,"gpa_hi":3.95,"sat_25":1290,"sat_75":1470,"act_25":27,"act_75":33,"tier":3,"type":"public","state":"Illinois","size":35000,"tuition":17000,"desc":"Engineering and CS powerhouse — Grainger College of Engineering is top-5 in many disciplines. Gies business is direct-admit.","majors":["Engineering","Computer Science","Business","Psychology","Communication"]},
    # --- Liberal arts colleges ---
    {"name":"Williams","slug":"williams","accept":0.085,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1450,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Massachusetts","size":2100,"tuition":64000,"desc":"Top-ranked liberal arts college. Tutorial system mirrors Oxford. Strong art history, economics, English.","majors":["Economics","Mathematics","English","History","Psychology"]},
    {"name":"Amherst","slug":"amherst","accept":0.090,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1450,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Massachusetts","size":1900,"tuition":65000,"desc":"Open-curriculum LAC. Five College consortium with Smith, Mt Holyoke, Hampshire, UMass-Amherst.","majors":["Economics","English","Political Science","Mathematics","Psychology"]},
    {"name":"Swarthmore","slug":"swarthmore","accept":0.072,"gpa_lo":3.91,"gpa_hi":4.00,"sat_25":1430,"sat_75":1550,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"Pennsylvania","size":1700,"tuition":62000,"desc":"Famously rigorous LAC. Honors program with oral exams. Strong engineering for an LAC.","majors":["Economics","Computer Science","Biology","Engineering","Political Science"]},
    {"name":"Pomona","slug":"pomona","accept":0.075,"gpa_lo":3.92,"gpa_hi":4.00,"sat_25":1460,"sat_75":1560,"act_25":33,"act_75":35,"tier":1,"type":"private","state":"California","size":1700,"tuition":62000,"desc":"Top West Coast LAC. Claremont Colleges consortium gives access to a 7-school network with shared facilities.","majors":["Economics","Computer Science","Mathematics","Biology","Psychology"]},
    {"name":"Bowdoin","slug":"bowdoin","accept":0.090,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1440,"sat_75":1530,"act_25":32,"act_75":34,"tier":1,"type":"private","state":"Maine","size":1900,"tuition":62000,"desc":"Coastal Maine LAC, test-optional pioneer. Strong government, environmental studies, and food.","majors":["Government","Mathematics","Economics","Biology","English"]},
    {"name":"Wellesley","slug":"wellesley","accept":0.130,"gpa_lo":3.86,"gpa_hi":4.00,"sat_25":1410,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Massachusetts","size":2400,"tuition":64000,"desc":"Top women's college. MIT cross-registration, strong pre-med and PoliSci, alumni network includes Hillary Clinton, Albright, Soong Mei-ling.","majors":["Economics","Computer Science","Political Science","Biology","English"]},
    {"name":"Middlebury","slug":"middlebury","accept":0.130,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1400,"sat_75":1520,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Vermont","size":2700,"tuition":64000,"desc":"LAC famous for languages (the summer language schools are legendary). Strong environmental studies and IR.","majors":["Economics","International Studies","English","Political Science","Environmental Studies"]},
    {"name":"Carleton","slug":"carleton","accept":0.180,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Minnesota","size":2000,"tuition":67000,"desc":"Midwestern LAC with high PhD-feeder rate. Trimester system, strong sciences and humanities.","majors":["Economics","Computer Science","Biology","Mathematics","Political Science"]},
    {"name":"Haverford","slug":"haverford","accept":0.140,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Pennsylvania","size":1400,"tuition":63000,"desc":"Tiny Quaker LAC outside Philadelphia. Honor code, no Greek life. Bi-Co consortium with Bryn Mawr.","majors":["Economics","Biology","English","Political Science","Computer Science"]},
    {"name":"Vassar","slug":"vassar","accept":0.190,"gpa_lo":3.79,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"New York","size":2400,"tuition":65000,"desc":"Liberal arts in the Hudson Valley. Strong arts, English, drama, urban studies.","majors":["English","Economics","Psychology","Political Science","Biology"]},
    # --- Specialized + niche ---
    {"name":"Babson","slug":"babson","accept":0.260,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1280,"sat_75":1430,"act_25":29,"act_75":32,"tier":3,"type":"private","state":"Massachusetts","size":2500,"tuition":58000,"desc":"Entrepreneurship-focused business school. #1 undergrad entrepreneurship for decades. Everyone takes the same first-year venture class.","majors":["Entrepreneurship","Finance","Marketing","Strategic Management","Accounting"]},
    {"name":"Bentley","slug":"bentley","accept":0.380,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1230,"sat_75":1370,"act_25":27,"act_75":31,"tier":4,"type":"private","state":"Massachusetts","size":4400,"tuition":59000,"desc":"Business-focused private outside Boston. Heavy quant/finance pipelines into Boston employers.","majors":["Finance","Accounting","Economics","Marketing","Management"]},
    {"name":"RPI","slug":"rpi","accept":0.570,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1300,"sat_75":1480,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"New York","size":6000,"tuition":62000,"desc":"Oldest engineering school in the English-speaking world. Strong CS and engineering, more affordable than Ivy-tier privates.","majors":["Engineering","Computer Science","Mathematics","Architecture","Business"]},
    {"name":"WPI","slug":"wpi","accept":0.610,"gpa_lo":3.71,"gpa_hi":3.95,"sat_25":1310,"sat_75":1470,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":5300,"tuition":63000,"desc":"Worcester Polytechnic. Project-based curriculum, term system. Strong engineering, robotics, biomedical.","majors":["Engineering","Computer Science","Robotics","Biology","Business"]},
    {"name":"Rose-Hulman","slug":"rose-hulman","accept":0.770,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1260,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Indiana","size":2200,"tuition":54000,"desc":"Specialty undergrad-only engineering school. Consistently ranked #1 for non-PhD engineering programs.","majors":["Engineering","Computer Science","Mathematics","Physics","Chemistry"]},
    # --- More state flagships and accessible ---
    {"name":"University of Iowa","slug":"uiowa","accept":0.870,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1130,"sat_75":1340,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Iowa","size":24000,"tuition":11000,"desc":"Big Ten public, famous for the Iowa Writers' Workshop (graduate). Strong undergrad business at Tippie.","majors":["Business","Communication","Engineering","Biology","Psychology"]},
    {"name":"University of Minnesota","slug":"umn","accept":0.700,"gpa_lo":3.55,"gpa_hi":3.93,"sat_25":1280,"sat_75":1450,"act_25":26,"act_75":32,"tier":4,"type":"public","state":"Minnesota","size":36000,"tuition":17000,"desc":"Twin Cities flagship. Carlson business and engineering, strong agriculture and pre-med.","majors":["Business","Biology","Engineering","Psychology","Economics"]},
    {"name":"University of Colorado Boulder","slug":"cu-boulder","accept":0.810,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1180,"sat_75":1370,"act_25":25,"act_75":31,"tier":5,"type":"public","state":"Colorado","size":31000,"tuition":13000,"desc":"Mountain-flagship public. Aerospace engineering is top-3 nationally; strong outdoor culture.","majors":["Engineering","Business","Biology","Communication","Psychology"]},
    {"name":"University of Oregon","slug":"uoregon","accept":0.880,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1100,"sat_75":1300,"act_25":22,"act_75":28,"tier":5,"type":"public","state":"Oregon","size":18000,"tuition":14000,"desc":"Eugene flagship. Strong journalism (advertising), business, and architecture. Nike pipeline.","majors":["Business","Psychology","Biology","Communication","Economics"]},
    {"name":"University of Connecticut","slug":"uconn","accept":0.560,"gpa_lo":3.59,"gpa_hi":3.92,"sat_25":1230,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Connecticut","size":23000,"tuition":18000,"desc":"Storrs flagship. Strong nursing, education, business, and basketball. Honors college pulls top in-state applicants.","majors":["Business","Psychology","Engineering","Nursing","Biology"]},
    {"name":"Rutgers","slug":"rutgers","accept":0.660,"gpa_lo":3.58,"gpa_hi":3.92,"sat_25":1210,"sat_75":1440,"act_25":26,"act_75":32,"tier":4,"type":"public","state":"New Jersey","size":36000,"tuition":17000,"desc":"NJ flagship. Strong CS, business, pharmacy. Diverse, large, deeply integrated with NY/Philly job markets.","majors":["Business","Computer Science","Biology","Psychology","Engineering"]},
    {"name":"Stony Brook","slug":"stony-brook","accept":0.480,"gpa_lo":3.62,"gpa_hi":3.95,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"New York","size":18000,"tuition":11000,"desc":"SUNY research flagship. Strong biology and engineering, affordable, R1 medical/research environment.","majors":["Biology","Computer Science","Business","Engineering","Psychology"]},
    {"name":"University of Pittsburgh","slug":"pitt","accept":0.490,"gpa_lo":3.63,"gpa_hi":3.93,"sat_25":1240,"sat_75":1420,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Pennsylvania","size":20000,"tuition":21000,"desc":"Pittsburgh urban public. Strong nursing, biomedical, philosophy. Affiliated UPMC hospital network.","majors":["Nursing","Biology","Business","Psychology","Engineering"]},
    {"name":"Virginia Tech","slug":"vt","accept":0.560,"gpa_lo":3.81,"gpa_hi":4.10,"sat_25":1240,"sat_75":1430,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Virginia","size":30000,"tuition":15000,"desc":"Engineering-heavy public, strong corps of cadets, top-tier ROTC, growing CS.","majors":["Engineering","Business","Computer Science","Biology","Architecture"]},
    {"name":"Texas A&M","slug":"tamu","accept":0.640,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1180,"sat_75":1390,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"Texas","size":58000,"tuition":12000,"desc":"Massive public, military traditions, strong engineering and agriculture. Mays business is direct-admit.","majors":["Engineering","Business","Biology","Communication","Agriculture"]},
    {"name":"Clemson","slug":"clemson","accept":0.430,"gpa_lo":3.80,"gpa_hi":4.20,"sat_25":1240,"sat_75":1410,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"South Carolina","size":21000,"tuition":15000,"desc":"South Carolina public flagship. Engineering, business, strong school spirit and sports.","majors":["Business","Engineering","Biology","Education","Psychology"]},
    {"name":"University of Alabama","slug":"alabama","accept":0.800,"gpa_lo":3.70,"gpa_hi":4.20,"sat_25":1090,"sat_75":1340,"act_25":23,"act_75":31,"tier":5,"type":"public","state":"Alabama","size":33000,"tuition":12000,"desc":"Big Tide football culture. Generous merit aid for OOS strong students. Honors college is rigorous.","majors":["Business","Communication","Biology","Engineering","Education"]},
    {"name":"University of Georgia","slug":"uga","accept":0.430,"gpa_lo":3.91,"gpa_hi":4.21,"sat_25":1240,"sat_75":1420,"act_25":28,"act_75":32,"tier":3,"type":"public","state":"Georgia","size":31000,"tuition":12000,"desc":"Athens flagship, Terry business school, Hope/Zell scholarships make in-state extremely affordable.","majors":["Business","Biology","Psychology","Communication","Engineering"]},
    {"name":"University of Tennessee","slug":"utk","accept":0.680,"gpa_lo":3.80,"gpa_hi":4.30,"sat_25":1170,"sat_75":1370,"act_25":25,"act_75":32,"tier":4,"type":"public","state":"Tennessee","size":31000,"tuition":13000,"desc":"Knoxville flagship. Strong nuclear engineering (Oak Ridge tie), business, school spirit.","majors":["Business","Engineering","Biology","Communication","Psychology"]},
    {"name":"San Diego State","slug":"sdsu","accept":0.380,"gpa_lo":3.70,"gpa_hi":4.05,"sat_25":1140,"sat_75":1320,"act_25":23,"act_75":29,"tier":4,"type":"public","state":"California","size":31000,"tuition":8000,"desc":"Southern Cal CSU. Strong international business, hospitality, journalism. Good weather and price for Californians.","majors":["Business","Psychology","Biology","Communication","Criminal Justice"]},
    {"name":"University of Arizona","slug":"arizona","accept":0.860,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1110,"sat_75":1320,"act_25":21,"act_75":28,"tier":5,"type":"public","state":"Arizona","size":35000,"tuition":13000,"desc":"Tucson flagship. Strong astronomy and optical sciences (massive observatory partnership), business, nursing.","majors":["Business","Biology","Psychology","Engineering","Communication"]},
    {"name":"Northeastern","slug":"northeastern","accept":0.060,"gpa_lo":3.85,"gpa_hi":4.20,"sat_25":1430,"sat_75":1540,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Massachusetts","size":16000,"tuition":62000,"desc":"Boston private with mandatory co-op program (~6 months at a real employer). Acceptance rate has plummeted in recent years.","majors":["Business","Computer Science","Engineering","Psychology","Health Sciences"]},
    {"name":"Case Western","slug":"case","accept":0.270,"gpa_lo":3.80,"gpa_hi":4.10,"sat_25":1380,"sat_75":1520,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Ohio","size":6000,"tuition":63000,"desc":"Cleveland private research university. Strong pre-med (Cleveland Clinic ties), engineering, music.","majors":["Engineering","Biology","Computer Science","Business","Psychology"]},
    {"name":"Lehigh","slug":"lehigh","accept":0.310,"gpa_lo":3.80,"gpa_hi":4.10,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Pennsylvania","size":5500,"tuition":63000,"desc":"Mid-sized private with strong engineering, business, integrated business+engineering programs. Greek-heavy social scene.","majors":["Engineering","Business","Computer Science","Biology","Finance"]},
    {"name":"Bucknell","slug":"bucknell","accept":0.330,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1300,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"Pennsylvania","size":3700,"tuition":67000,"desc":"Liberal arts plus engineering. Heavy finance recruiting for an LAC-sized school.","majors":["Business","Engineering","Biology","Economics","Psychology"]},
    {"name":"Villanova","slug":"villanova","accept":0.250,"gpa_lo":3.75,"gpa_hi":4.05,"sat_25":1370,"sat_75":1490,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Pennsylvania","size":7000,"tuition":63000,"desc":"Catholic university outside Philadelphia. Strong undergrad business and basketball. Heavy Wall Street pipeline.","majors":["Finance","Business","Engineering","Biology","Communication"]},
    {"name":"Tulane","slug":"tulane","accept":0.110,"gpa_lo":3.60,"gpa_hi":3.95,"sat_25":1370,"sat_75":1500,"act_25":31,"act_75":33,"tier":2,"type":"private","state":"Louisiana","size":8500,"tuition":62000,"desc":"New Orleans private. Strong public health, business, architecture, and a famous social scene.","majors":["Business","Public Health","Biology","Psychology","Engineering"]},
    {"name":"University of Miami","slug":"miami","accept":0.190,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Florida","size":12000,"tuition":58000,"desc":"South Florida private. Strong music, business (Herbert), marine biology, and pre-med.","majors":["Business","Biology","Communication","Psychology","Music"]},
    {"name":"American University","slug":"american","accept":0.490,"gpa_lo":3.62,"gpa_hi":3.93,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"District of Columbia","size":8000,"tuition":56000,"desc":"DC private. Politics-and-international-affairs heavy, strong School of Public Affairs.","majors":["International Studies","Political Science","Business","Communication","Psychology"]},
    {"name":"Northeastern Illinois","slug":"neiu","accept":0.880,"gpa_lo":3.10,"gpa_hi":3.70,"sat_25":1000,"sat_75":1190,"act_25":18,"act_75":24,"tier":5,"type":"public","state":"Illinois","size":7000,"tuition":11000,"desc":"Chicago public. Affordable, accessible, strong commuter base. First-generation friendly.","majors":["Business","Education","Psychology","Biology","Computer Science"]},
    # --- More LACs ---
    {"name":"Wesleyan","slug":"wesleyan","accept":0.140,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Connecticut","size":3100,"tuition":67000,"desc":"Liberal arts college with a strong arts/film/music identity. Politically progressive culture.","majors":["Economics","Government","English","Biology","Film Studies"]},
    {"name":"Hamilton","slug":"hamilton","accept":0.115,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"New York","size":2000,"tuition":67000,"desc":"Open-curriculum LAC in central NY with very strong writing program (no general education requirements).","majors":["Economics","Government","Mathematics","English","Biology"]},
    {"name":"Davidson","slug":"davidson","accept":0.150,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1380,"sat_75":1500,"act_25":31,"act_75":34,"tier":2,"type":"private","state":"North Carolina","size":1900,"tuition":63000,"desc":"Top Southern LAC. Honor code and strong pre-med pipeline. Athletics-friendly culture.","majors":["Economics","Political Science","Biology","Psychology","English"]},
    {"name":"Colgate","slug":"colgate","accept":0.130,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1390,"sat_75":1500,"act_25":31,"act_75":33,"tier":2,"type":"private","state":"New York","size":3200,"tuition":68000,"desc":"Mid-sized LAC with strong economics, political science, and a Greek-leaning social scene.","majors":["Economics","Political Science","English","Psychology","Biology"]},
    {"name":"Smith","slug":"smith","accept":0.230,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1380,"sat_75":1500,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":2500,"tuition":63000,"desc":"Top women's college, Five College Consortium with Amherst, Hampshire, Mt Holyoke, UMass.","majors":["Economics","Government","Engineering","Psychology","Biology"]},
    {"name":"Mount Holyoke","slug":"mt-holyoke","accept":0.380,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1300,"sat_75":1450,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"Massachusetts","size":2200,"tuition":59000,"desc":"Oldest of the Seven Sisters women's colleges. Strong sciences and international student community.","majors":["Biology","Psychology","English","International Relations","Economics"]},
    {"name":"Bryn Mawr","slug":"bryn-mawr","accept":0.300,"gpa_lo":3.75,"gpa_hi":4.00,"sat_25":1340,"sat_75":1480,"act_25":30,"act_75":34,"tier":3,"type":"private","state":"Pennsylvania","size":1400,"tuition":60000,"desc":"Women's LAC outside Philadelphia. Bi-Co consortium with Haverford gives access to wider course catalog.","majors":["Biology","Mathematics","English","Psychology","Political Science"]},
    {"name":"Bates","slug":"bates","accept":0.140,"gpa_lo":3.75,"gpa_hi":4.00,"sat_25":1370,"sat_75":1490,"act_25":31,"act_75":33,"tier":2,"type":"private","state":"Maine","size":1900,"tuition":65000,"desc":"Coastal Maine LAC. Test-optional pioneer, strong environmental studies and English.","majors":["Economics","Politics","Psychology","Biology","English"]},
    {"name":"Colby","slug":"colby","accept":0.080,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1410,"sat_75":1530,"act_25":32,"act_75":34,"tier":1,"type":"private","state":"Maine","size":2200,"tuition":66000,"desc":"Maine LAC with rapidly tightening admissions. Free January-term abroad for all students.","majors":["Economics","Government","Biology","Computer Science","English"]},
    {"name":"Trinity College","slug":"trinity-ct","accept":0.380,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1300,"sat_75":1430,"act_25":29,"act_75":32,"tier":3,"type":"private","state":"Connecticut","size":2200,"tuition":68000,"desc":"Hartford LAC with strong economics and engineering crossover. Popular Greek scene.","majors":["Economics","Political Science","Engineering","Psychology","Biology"]},
    {"name":"Connecticut College","slug":"conn-college","accept":0.420,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1280,"sat_75":1410,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"Connecticut","size":1800,"tuition":67000,"desc":"Coastal Connecticut LAC. Open curriculum, strong dance and government programs.","majors":["Economics","Psychology","Government","English","Biology"]},
    {"name":"Skidmore","slug":"skidmore","accept":0.330,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":3,"type":"private","state":"New York","size":2700,"tuition":65000,"desc":"Saratoga Springs LAC with a strong arts/business intersection (creative thought initiative).","majors":["Business","Psychology","English","Government","Biology"]},
    {"name":"Macalester","slug":"macalester","accept":0.320,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1370,"sat_75":1490,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"Minnesota","size":2200,"tuition":64000,"desc":"St. Paul LAC with very international student body. Strong economics, political science, biology.","majors":["Economics","Political Science","Biology","Psychology","English"]},
    {"name":"Reed","slug":"reed","accept":0.310,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1390,"sat_75":1530,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"Oregon","size":1500,"tuition":64000,"desc":"Notoriously rigorous Portland LAC. Senior thesis required. Famously high PhD-feeder rate.","majors":["English","Biology","Psychology","Mathematics","Anthropology"]},
    {"name":"Grinnell","slug":"grinnell","accept":0.110,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1430,"sat_75":1540,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"Iowa","size":1700,"tuition":63000,"desc":"Open-curriculum Iowa LAC. Largest endowment per student of any LAC. Strong sciences and humanities.","majors":["Economics","Biology","Computer Science","Psychology","English"]},
    {"name":"Kenyon","slug":"kenyon","accept":0.350,"gpa_lo":3.75,"gpa_hi":3.95,"sat_25":1350,"sat_75":1480,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Ohio","size":1700,"tuition":67000,"desc":"Rural Ohio LAC famous for English program (Kenyon Review).","majors":["English","Economics","Political Science","Psychology","Biology"]},
    {"name":"Oberlin","slug":"oberlin","accept":0.350,"gpa_lo":3.75,"gpa_hi":3.95,"sat_25":1340,"sat_75":1490,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"Ohio","size":2900,"tuition":63000,"desc":"Liberal-progressive LAC plus a top-tier conservatory. Double-degree program is uniquely strong.","majors":["English","Music","Psychology","Biology","Political Science"]},
    {"name":"Whitman","slug":"whitman","accept":0.450,"gpa_lo":3.75,"gpa_hi":4.00,"sat_25":1290,"sat_75":1450,"act_25":29,"act_75":33,"tier":4,"type":"private","state":"Washington","size":1500,"tuition":63000,"desc":"Walla Walla LAC. Outdoor-leaning culture, strong sciences, generous merit aid.","majors":["Biology","Economics","Psychology","English","Politics"]},
    {"name":"Pitzer","slug":"pitzer","accept":0.180,"gpa_lo":3.70,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"California","size":1200,"tuition":63000,"desc":"Smallest of the Claremont Colleges. Social-justice and sustainability focus.","majors":["Economics","Psychology","Sociology","Environmental Analysis","Political Science"]},
    {"name":"Scripps","slug":"scripps","accept":0.290,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1370,"sat_75":1510,"act_25":31,"act_75":34,"tier":3,"type":"private","state":"California","size":1100,"tuition":62000,"desc":"Women's college in the Claremont Consortium. Strong humanities and pre-med.","majors":["Biology","Psychology","English","Economics","Politics"]},
    {"name":"Harvey Mudd","slug":"harvey-mudd","accept":0.110,"gpa_lo":3.90,"gpa_hi":4.00,"sat_25":1490,"sat_75":1570,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"California","size":900,"tuition":63000,"desc":"Tiny STEM-only LAC in the Claremont Consortium. Highest-paid LAC graduates by mid-career salary.","majors":["Engineering","Computer Science","Mathematics","Physics","Chemistry"]},
    {"name":"Claremont McKenna","slug":"cmc","accept":0.110,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1430,"sat_75":1530,"act_25":32,"act_75":34,"tier":2,"type":"private","state":"California","size":1400,"tuition":63000,"desc":"Government / economics / leadership focus. Robert Day School of Economics is top-tier.","majors":["Economics","Government","International Relations","Psychology","Mathematics"]},
    {"name":"Occidental","slug":"oxy","accept":0.350,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1340,"sat_75":1480,"act_25":29,"act_75":33,"tier":3,"type":"private","state":"California","size":2000,"tuition":63000,"desc":"LA-area LAC. Diplomacy and World Affairs program is a niche standout.","majors":["Economics","Diplomacy & World Affairs","Biology","Psychology","English"]},
    # --- More private universities ---
    {"name":"Brandeis","slug":"brandeis","accept":0.390,"gpa_lo":3.75,"gpa_hi":3.95,"sat_25":1380,"sat_75":1510,"act_25":30,"act_75":34,"tier":3,"type":"private","state":"Massachusetts","size":3700,"tuition":65000,"desc":"Boston-area private. Strong sciences, social policy, and Jewish studies.","majors":["Biology","Business","Economics","Psychology","Computer Science"]},
    {"name":"Fordham","slug":"fordham","accept":0.500,"gpa_lo":3.65,"gpa_hi":3.93,"sat_25":1300,"sat_75":1430,"act_25":29,"act_75":32,"tier":4,"type":"private","state":"New York","size":10000,"tuition":62000,"desc":"Jesuit university with two NYC campuses. Strong communications, business (Gabelli), and pre-law.","majors":["Business","Communication","Psychology","English","Political Science"]},
    {"name":"Barnard","slug":"barnard","accept":0.090,"gpa_lo":3.85,"gpa_hi":4.00,"sat_25":1430,"sat_75":1540,"act_25":31,"act_75":34,"tier":1,"type":"private","state":"New York","size":2700,"tuition":68000,"desc":"Women's college affiliated with Columbia. Cross-registration gives students access to Columbia courses and an Ivy degree (Barnard-Columbia).","majors":["Economics","English","Psychology","Political Science","Biology"]},
    {"name":"Pepperdine","slug":"pepperdine","accept":0.470,"gpa_lo":3.65,"gpa_hi":3.93,"sat_25":1280,"sat_75":1430,"act_25":28,"act_75":32,"tier":4,"type":"private","state":"California","size":4000,"tuition":62000,"desc":"Malibu private with Christian heritage. Strong international programs, business, and law pipeline.","majors":["Business","Communication","Psychology","Biology","Sports Medicine"]},
    {"name":"Santa Clara","slug":"scu","accept":0.400,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1330,"sat_75":1470,"act_25":30,"act_75":33,"tier":3,"type":"private","state":"California","size":5800,"tuition":58000,"desc":"Jesuit university in Silicon Valley. Strong undergraduate business (Leavey) and engineering, heavy tech recruiting.","majors":["Business","Computer Science","Engineering","Psychology","Biology"]},
    {"name":"Loyola Marymount","slug":"lmu","accept":0.420,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1240,"sat_75":1380,"act_25":27,"act_75":31,"tier":4,"type":"private","state":"California","size":7000,"tuition":58000,"desc":"LA-area Jesuit private. Strong film school (close ties to LA industry), business, and communication.","majors":["Business","Communication","Film & TV","Psychology","Biology"]},
    {"name":"Drexel","slug":"drexel","accept":0.700,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1180,"sat_75":1380,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"Pennsylvania","size":14000,"tuition":58000,"desc":"Philadelphia private with mandatory co-op program. Engineering, business, and design. 5-year program standard.","majors":["Engineering","Business","Computer Science","Nursing","Design"]},
    {"name":"Stevens Institute","slug":"stevens","accept":0.420,"gpa_lo":3.78,"gpa_hi":4.10,"sat_25":1380,"sat_75":1510,"act_25":31,"act_75":33,"tier":3,"type":"private","state":"New Jersey","size":4000,"tuition":62000,"desc":"Engineering and tech across the Hudson from Manhattan. Strong placement into NYC finance/tech.","majors":["Engineering","Computer Science","Business","Mathematics","Information Systems"]},
    {"name":"Olin College","slug":"olin","accept":0.180,"gpa_lo":3.95,"gpa_hi":4.00,"sat_25":1480,"sat_75":1560,"act_25":33,"act_75":35,"tier":2,"type":"private","state":"Massachusetts","size":400,"tuition":56000,"desc":"Tiny experimental engineering college. Project-based curriculum, no academic departments.","majors":["Engineering","Computer Science","Mechanical Engineering","Electrical Engineering","Bioengineering"]},
    {"name":"Cooper Union","slug":"cooper","accept":0.130,"gpa_lo":3.80,"gpa_hi":4.00,"sat_25":1340,"sat_75":1500,"act_25":30,"act_75":34,"tier":2,"type":"private","state":"New York","size":900,"tuition":48000,"desc":"East Village engineering/architecture/art school. Half-tuition merit scholarships for all admitted students.","majors":["Engineering","Architecture","Fine Arts","Computer Science","Mechanical Engineering"]},
    # --- Specialty / arts schools ---
    {"name":"Berklee College of Music","slug":"berklee","accept":0.530,"gpa_lo":3.20,"gpa_hi":3.85,"sat_25":1100,"sat_75":1350,"act_25":21,"act_75":29,"tier":3,"type":"private","state":"Massachusetts","size":4500,"tuition":52000,"desc":"World's largest contemporary music college. Audition-based admissions. Strong production, songwriting, performance.","majors":["Music Production","Songwriting","Performance","Music Business","Film Scoring"]},
    {"name":"Juilliard","slug":"juilliard","accept":0.080,"gpa_lo":3.60,"gpa_hi":3.95,"sat_25":1100,"sat_75":1400,"act_25":21,"act_75":31,"tier":1,"type":"private","state":"New York","size":600,"tuition":52000,"desc":"World-class performing arts conservatory. Audition is everything. Lincoln Center campus.","majors":["Music Performance","Drama","Dance","Music Composition","Jazz Studies"]},
    {"name":"Pratt Institute","slug":"pratt","accept":0.530,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1180,"sat_75":1380,"act_25":25,"act_75":31,"tier":4,"type":"private","state":"New York","size":3500,"tuition":59000,"desc":"Brooklyn art and design school. Strong industrial design, fashion, architecture, fine arts.","majors":["Industrial Design","Fashion Design","Architecture","Fine Arts","Communications Design"]},
    {"name":"Parsons (The New School)","slug":"parsons","accept":0.610,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1130,"sat_75":1330,"act_25":23,"act_75":29,"tier":4,"type":"private","state":"New York","size":7300,"tuition":56000,"desc":"NYC fashion-and-design school. Top fashion design program in the US.","majors":["Fashion Design","Strategic Design","Fine Arts","Photography","Graphic Design"]},
    {"name":"Rhode Island School of Design","slug":"risd","accept":0.220,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1290,"sat_75":1470,"act_25":28,"act_75":33,"tier":3,"type":"private","state":"Rhode Island","size":2000,"tuition":58000,"desc":"Top art and design school. Brown cross-registration. Famously rigorous foundational year.","majors":["Industrial Design","Illustration","Graphic Design","Architecture","Fine Arts"]},
    {"name":"School of the Art Institute of Chicago","slug":"saic","accept":0.760,"gpa_lo":3.30,"gpa_hi":3.85,"sat_25":1100,"sat_75":1310,"act_25":21,"act_75":28,"tier":4,"type":"private","state":"Illinois","size":3500,"tuition":54000,"desc":"Art school in downtown Chicago, attached to The Art Institute museum. Open-curriculum across visual disciplines.","majors":["Fine Arts","Illustration","Photography","Architecture","Visual Communication Design"]},
    # --- More public flagships ---
    {"name":"University of Kentucky","slug":"uky","accept":0.940,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1080,"sat_75":1290,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Kentucky","size":22000,"tuition":13000,"desc":"Lexington flagship. Strong Gatton business and engineering. Generous merit aid for in-state.","majors":["Business","Biology","Engineering","Communication","Psychology"]},
    {"name":"Auburn","slug":"auburn","accept":0.460,"gpa_lo":3.70,"gpa_hi":4.10,"sat_25":1170,"sat_75":1340,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"Alabama","size":24000,"tuition":13000,"desc":"Strong engineering, agriculture, and architecture programs. SEC sports culture.","majors":["Engineering","Business","Biology","Pre-Vet","Education"]},
    {"name":"LSU","slug":"lsu","accept":0.770,"gpa_lo":3.45,"gpa_hi":3.94,"sat_25":1080,"sat_75":1280,"act_25":22,"act_75":28,"tier":5,"type":"public","state":"Louisiana","size":29000,"tuition":12000,"desc":"Baton Rouge flagship. Strong petroleum engineering, agriculture, mass communication.","majors":["Engineering","Biology","Business","Communication","Mass Communication"]},
    {"name":"University of South Carolina","slug":"sc","accept":0.640,"gpa_lo":3.65,"gpa_hi":4.05,"sat_25":1180,"sat_75":1360,"act_25":25,"act_75":31,"tier":4,"type":"public","state":"South Carolina","size":27000,"tuition":13000,"desc":"Columbia flagship. Top international business undergrad in the US. Strong Honors college.","majors":["International Business","Marketing","Public Health","Biology","Engineering"]},
    {"name":"University of Missouri","slug":"missou","accept":0.810,"gpa_lo":3.55,"gpa_hi":3.92,"sat_25":1130,"sat_75":1310,"act_25":23,"act_75":29,"tier":5,"type":"public","state":"Missouri","size":24000,"tuition":12000,"desc":"Columbia flagship. Renowned journalism school. Strong veterinary medicine.","majors":["Journalism","Business","Biology","Engineering","Psychology"]},
    {"name":"University of Kansas","slug":"ku","accept":0.890,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1090,"sat_75":1310,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Kansas","size":20000,"tuition":12000,"desc":"Lawrence flagship. Strong pharmacy, business, and journalism programs.","majors":["Business","Biology","Engineering","Communication","Psychology"]},
    {"name":"University of Nebraska","slug":"unl","accept":0.800,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1100,"sat_75":1310,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Nebraska","size":21000,"tuition":10000,"desc":"Lincoln flagship. Affordable, strong agriculture and architecture, Big Ten sports.","majors":["Business","Engineering","Biology","Psychology","Education"]},
    {"name":"University of Vermont","slug":"uvm","accept":0.700,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1230,"sat_75":1390,"act_25":27,"act_75":32,"tier":4,"type":"public","state":"Vermont","size":11000,"tuition":21000,"desc":"Burlington flagship. Strong environmental studies, nursing, business. Outdoor-focused culture.","majors":["Business","Biology","Environmental Studies","Psychology","Nursing"]},
    {"name":"University of Delaware","slug":"udel","accept":0.660,"gpa_lo":3.65,"gpa_hi":3.95,"sat_25":1180,"sat_75":1370,"act_25":26,"act_75":31,"tier":4,"type":"public","state":"Delaware","size":19000,"tuition":15000,"desc":"Newark flagship. Strong engineering, business (Lerner), agriculture, fashion.","majors":["Business","Engineering","Biology","Education","Communication"]},
    {"name":"Binghamton University","slug":"binghamton","accept":0.420,"gpa_lo":3.71,"gpa_hi":3.96,"sat_25":1310,"sat_75":1450,"act_25":29,"act_75":32,"tier":3,"type":"public","state":"New York","size":14000,"tuition":11000,"desc":"SUNY public ivy. Strong business (School of Management), engineering, accounting.","majors":["Business","Biology","Psychology","Engineering","Economics"]},
    {"name":"Cal Poly San Luis Obispo","slug":"calpoly-slo","accept":0.300,"gpa_lo":3.80,"gpa_hi":4.20,"sat_25":1280,"sat_75":1450,"act_25":27,"act_75":32,"tier":3,"type":"public","state":"California","size":22000,"tuition":10000,"desc":"Hands-on engineering and architecture. Quarter system. 'Learn by doing' is the official motto and not an exaggeration.","majors":["Engineering","Business","Architecture","Computer Science","Biology"]},
    {"name":"San Jose State","slug":"sjsu","accept":0.700,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1100,"sat_75":1330,"act_25":21,"act_75":29,"tier":5,"type":"public","state":"California","size":29000,"tuition":8000,"desc":"Silicon Valley CSU. Massive direct pipeline into Bay Area tech (more SV CS hires than Stanford or Berkeley).","majors":["Business","Computer Science","Engineering","Psychology","Communication"]},
    {"name":"Cal State Long Beach","slug":"csulb","accept":0.420,"gpa_lo":3.55,"gpa_hi":3.93,"sat_25":1090,"sat_75":1290,"act_25":21,"act_75":28,"tier":4,"type":"public","state":"California","size":33000,"tuition":7000,"desc":"Largest CSU. Strong engineering, business, film, and education programs.","majors":["Business","Engineering","Psychology","Biology","Film & Electronic Arts"]},
    {"name":"Iowa State","slug":"iowa-state","accept":0.880,"gpa_lo":3.40,"gpa_hi":3.85,"sat_25":1100,"sat_75":1310,"act_25":21,"act_75":29,"tier":5,"type":"public","state":"Iowa","size":29000,"tuition":11000,"desc":"Ames flagship. Strong agriculture, engineering, design, and statistics.","majors":["Engineering","Business","Agriculture","Biology","Psychology"]},
    {"name":"Temple","slug":"temple","accept":0.690,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1170,"sat_75":1340,"act_25":24,"act_75":30,"tier":4,"type":"public","state":"Pennsylvania","size":24000,"tuition":18000,"desc":"Philadelphia public. Strong business (Fox), education, and media programs.","majors":["Business","Communication","Engineering","Psychology","Education"]},
    {"name":"George Mason","slug":"gmu","accept":0.890,"gpa_lo":3.50,"gpa_hi":3.95,"sat_25":1130,"sat_75":1330,"act_25":24,"act_75":30,"tier":5,"type":"public","state":"Virginia","size":27000,"tuition":13000,"desc":"DC-area public. Strong economics (Mercatus Center), public policy, and CS. Affordable for OOS.","majors":["Business","Computer Science","Psychology","Economics","Engineering"]},
    {"name":"University of Houston","slug":"uh","accept":0.700,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1100,"sat_75":1290,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Texas","size":37000,"tuition":11000,"desc":"Bauer business school, strong hotel & restaurant management, urban research university.","majors":["Business","Biology","Psychology","Engineering","Hotel Management"]},
    {"name":"University of Utah","slug":"utah","accept":0.870,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1100,"sat_75":1330,"act_25":22,"act_75":29,"tier":5,"type":"public","state":"Utah","size":24000,"tuition":10000,"desc":"Salt Lake City flagship. Strong CS (Entertainment Arts & Engineering games program is top-tier), business, biomedical.","majors":["Business","Biology","Engineering","Computer Science","Psychology"]},
    {"name":"University of Hawaii Manoa","slug":"hawaii","accept":0.840,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1080,"sat_75":1280,"act_25":22,"act_75":27,"tier":5,"type":"public","state":"Hawaii","size":13000,"tuition":12000,"desc":"Honolulu flagship. Strong marine biology, oceanography, second-language studies, hospitality.","majors":["Business","Biology","Psychology","Education","Marine Biology"]},
    # --- More accessible / community-pipeline ---
    {"name":"University of New Hampshire","slug":"unh","accept":0.870,"gpa_lo":3.45,"gpa_hi":3.85,"sat_25":1110,"sat_75":1300,"act_25":24,"act_75":29,"tier":5,"type":"public","state":"New Hampshire","size":12000,"tuition":19000,"desc":"Durham flagship. Strong engineering, business, marine sciences. New England state-school feel.","majors":["Business","Engineering","Biology","Psychology","Communication"]},
    {"name":"University of Maine","slug":"umaine","accept":0.940,"gpa_lo":3.30,"gpa_hi":3.80,"sat_25":1050,"sat_75":1240,"act_25":21,"act_75":27,"tier":5,"type":"public","state":"Maine","size":9000,"tuition":12000,"desc":"Orono flagship. Strong forestry, marine science, engineering. Affordable for New Englanders via NEBHE.","majors":["Engineering","Business","Biology","Education","Forestry"]},
    {"name":"Howard University","slug":"howard","accept":0.300,"gpa_lo":3.55,"gpa_hi":3.90,"sat_25":1130,"sat_75":1340,"act_25":22,"act_75":29,"tier":4,"type":"private","state":"District of Columbia","size":7700,"tuition":31000,"desc":"Premier HBCU. Top medical and law schools, strong communications, business. Vice President alma mater.","majors":["Biology","Business","Political Science","Psychology","Communication"]},
    {"name":"Spelman","slug":"spelman","accept":0.330,"gpa_lo":3.65,"gpa_hi":3.92,"sat_25":1130,"sat_75":1280,"act_25":22,"act_75":27,"tier":4,"type":"private","state":"Georgia","size":2200,"tuition":31000,"desc":"Top women's HBCU. Atlanta. Strong sciences and pre-med pipeline.","majors":["Biology","Psychology","English","Political Science","Sociology"]},
    {"name":"Morehouse","slug":"morehouse","accept":0.580,"gpa_lo":3.55,"gpa_hi":3.85,"sat_25":1010,"sat_75":1230,"act_25":19,"act_75":24,"tier":4,"type":"private","state":"Georgia","size":2200,"tuition":31000,"desc":"Top men's HBCU. MLK alma mater. Strong business and political science.","majors":["Business","Biology","Political Science","Psychology","Computer Science"]},
    {"name":"Florida A&M","slug":"famu","accept":0.350,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":1010,"sat_75":1180,"act_25":19,"act_75":23,"tier":4,"type":"public","state":"Florida","size":8500,"tuition":6000,"desc":"Top public HBCU. Strong business, pharmacy, engineering. Affordable in-state.","majors":["Business","Pharmacy","Engineering","Biology","Psychology"]},
    {"name":"University of Puerto Rico Mayagüez","slug":"uprm","accept":0.560,"gpa_lo":3.50,"gpa_hi":3.85,"sat_25":0,"sat_75":0,"act_25":0,"act_75":0,"tier":4,"type":"public","state":"Puerto Rico","size":11000,"tuition":2000,"desc":"Top engineering school in PR. Heavy NASA/defense recruiting pipeline. Bilingual instruction.","majors":["Engineering","Computer Science","Biology","Business","Agriculture"]},
    {"name":"Tuskegee","slug":"tuskegee","accept":0.290,"gpa_lo":3.30,"gpa_hi":3.75,"sat_25":960,"sat_75":1130,"act_25":18,"act_75":22,"tier":4,"type":"private","state":"Alabama","size":3000,"tuition":21000,"desc":"Historic HBCU. Strong veterinary medicine and engineering. Booker T. Washington founded.","majors":["Engineering","Business","Biology","Veterinary Medicine","Psychology"]},
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
    "harvard":      {"rounds": ["REA","RD"],            "rates": {"REA":0.087,"RD":0.026}},
    "yale":         {"rounds": ["REA","RD"],            "rates": {"REA":0.099,"RD":0.039}},
    "princeton":    {"rounds": ["REA","RD"],            "rates": {"REA":0.092,"RD":0.039}},
    "stanford":     {"rounds": ["REA","RD"],            "rates": {"REA":0.082,"RD":0.034}},
    "mit":          {"rounds": ["EA","RD"],             "rates": {"EA":0.054,"RD":0.040}},
    "caltech":      {"rounds": ["EA","RD"],             "rates": {"EA":0.060,"RD":0.040}},
    "columbia":     {"rounds": ["ED","RD"],             "rates": {"ED":0.119,"RD":0.038}},
    "upenn":        {"rounds": ["ED","RD"],             "rates": {"ED":0.150,"RD":0.046}},
    "duke":         {"rounds": ["ED","RD"],             "rates": {"ED":0.139,"RD":0.039}},
    "dartmouth":    {"rounds": ["ED","RD"],             "rates": {"ED":0.171,"RD":0.054}},
    "brown":        {"rounds": ["ED","RD"],             "rates": {"ED":0.130,"RD":0.046}},
    "cornell":      {"rounds": ["ED","RD"],             "rates": {"ED":0.165,"RD":0.071}},
    "northwestern": {"rounds": ["ED","RD"],             "rates": {"ED":0.250,"RD":0.053}},
    "uchicago":     {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.220,"ED2":0.180,"EA":0.080,"RD":0.040}},
    # Top privates with ED/EA splits
    "vanderbilt":   {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.176,"ED2":0.150,"RD":0.050}},
    "rice":         {"rounds": ["ED","RD"],             "rates": {"ED":0.150,"RD":0.071}},
    "jhu":          {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.319,"ED2":0.175,"RD":0.058}},
    "washu":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.300,"ED2":0.160,"RD":0.110}},
    "notre-dame":   {"rounds": ["REA","RD"],            "rates": {"REA":0.165,"RD":0.080}},
    "georgetown":   {"rounds": ["EA","RD"],             "rates": {"EA":0.110,"RD":0.110}},
    "nyu":          {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.250,"ED2":0.220,"RD":0.080}},
    "emory":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.300,"ED2":0.220,"RD":0.110}},
    "tufts":        {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.310,"ED2":0.200,"RD":0.080}},
    "cmu":          {"rounds": ["ED","RD"],             "rates": {"ED":0.190,"RD":0.110}},
    "usc":          {"rounds": ["EA","RD"],             "rates": {"EA":0.130,"RD":0.090}},
    "wake-forest":  {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.450,"ED2":0.300,"RD":0.190}},
    "bc":           {"rounds": ["ED","EA","RD"],        "rates": {"ED":0.330,"EA":0.200,"RD":0.160}},
    "tulane":       {"rounds": ["ED","EA","RD"],        "rates": {"ED":0.430,"EA":0.110,"RD":0.090}},
    "villanova":    {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.380,"ED2":0.200,"EA":0.250,"RD":0.220}},
    "lehigh":       {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.480,"ED2":0.350,"RD":0.300}},
    "case":         {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.350,"ED2":0.300,"EA":0.290,"RD":0.200}},
    "northeastern": {"rounds": ["ED","ED2","EA","RD"],  "rates": {"ED":0.300,"ED2":0.180,"EA":0.060,"RD":0.060}},
    "bu":           {"rounds": ["ED","ED2","RD"],       "rates": {"ED":0.290,"ED2":0.140,"RD":0.100}},
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
    "umich":        {"rounds": ["EA","RD"],   "rates": {"EA":0.230,"RD":0.180}, "in_state_rate":0.42, "out_of_state_rate":0.20},
    "uva":          {"rounds": ["ED","EA","RD"], "rates": {"ED":0.300,"EA":0.180,"RD":0.150}, "in_state_rate":0.30, "out_of_state_rate":0.16},
    "unc":          {"rounds": ["EA","RD"],   "rates": {"EA":0.190,"RD":0.150}, "in_state_rate":0.42, "out_of_state_rate":0.09},
    "ucla":         {"rounds": ["RD"],         "rates": {"RD":0.085}, "in_state_rate":0.13, "out_of_state_rate":0.07},
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


def merged_school(c):
    """Return a copy of the college dict with Scorecard overrides applied
    where they exist. Renderers use this so the displayed values always
    match the most-recently-verified source."""
    over = _get_overrides(c["slug"])
    if not over:
        return c
    out = dict(c)
    for k in ("accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"):
        v = over.get(k)
        if v is not None:
            out[k] = v
    return out


# Map our slug → Scorecard "name" search term. For most schools the school
# name works directly; the trickier ones get an explicit override.
SCORECARD_NAME_OVERRIDES = {
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
    rated = sum(1 for k in ("weather","setting","size","class_size","greek","sports","major_strength","prestige","cost") if k in out)
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


def _normalize_score(sat, act):
    act_to_sat = {36:1590,35:1540,34:1500,33:1460,32:1430,31:1400,30:1370,29:1340,28:1310,27:1280,26:1240,25:1210,24:1180,23:1140,22:1110,21:1080,20:1040,19:1010,18:970}
    if sat: return int(sat)
    if act: return act_to_sat.get(int(act), 1000)
    return None


def compute_fit(profile, school):
    score = 50.0
    components = {}
    gpa = profile.get("uw_gpa")
    if gpa is not None:
        midpoint = (school["gpa_lo"] + school["gpa_hi"]) / 2
        delta = max(-18, min(18, (gpa - midpoint) * 50))
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


def estimate_odds(school, fit, profile):
    """Harsher version. Markets and admissions are noisy; previous curve was
    over-generous in the middle of the fit range. Tighter slope + lower caps
    on elite schools so the headline numbers don't promise a Stanford that
    isn't there."""
    a = school["accept"]
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
    center = a * fit_mult * hook_mult
    # Caps tightened — even strong applicants almost never crack 18% at sub-10%
    # accept schools.
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
    test_str = ('SAT ' + str(profile['sat'])) if profile.get('sat') else (('ACT ' + str(profile['act'])) if profile.get('act') else 'none submitted')
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

CRITICAL: Use ONLY the numbers given above. Do not invent percentile rankings or stats not provided. If the student submitted ACT, compare to the ACT range; if SAT, compare to the SAT range. Don't compare an ACT score to an SAT range.

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
             legacy, first_gen, athlete, legacy_schools,
             pref_weather, pref_setting, pref_size, pref_greek, pref_sports, pref_major_strength,
             pref_class_size, pref_prestige, pref_cost, pref_weights, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                uw_gpa=excluded.uw_gpa, weighted_gpa=excluded.weighted_gpa, sat=excluded.sat, act=excluded.act,
                major=excluded.major, state=excluded.state, school_type=excluded.school_type,
                ecs=excluded.ecs, leadership=excluded.leadership, awards=excluded.awards,
                legacy=excluded.legacy, first_gen=excluded.first_gen, athlete=excluded.athlete,
                legacy_schools=excluded.legacy_schools,
                pref_weather=excluded.pref_weather, pref_setting=excluded.pref_setting,
                pref_size=excluded.pref_size, pref_greek=excluded.pref_greek,
                pref_sports=excluded.pref_sports, pref_major_strength=excluded.pref_major_strength,
                pref_class_size=excluded.pref_class_size, pref_prestige=excluded.pref_prestige,
                pref_cost=excluded.pref_cost,
                pref_weights=excluded.pref_weights,
                updated_at=CURRENT_TIMESTAMP""",
            (user_id, p.get("uw_gpa"), p.get("weighted_gpa"), p.get("sat"), p.get("act"),
             p.get("major"), p.get("state"), p.get("school_type"), p.get("ecs"),
             p.get("leadership"), p.get("awards"),
             legacy_flag, 1 if p.get("first_gen") else 0, 1 if p.get("athlete") else 0,
             legacy_schools,
             p.get("pref_weather") or "any", p.get("pref_setting") or "any",
             p.get("pref_size") or "any", p.get("pref_greek") or "any",
             p.get("pref_sports") or "any", p.get("pref_major_strength") or "any",
             p.get("pref_class_size") or "any", p.get("pref_prestige") or "any",
             p.get("pref_cost") or "any",
             pref_weights))
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
    for k in ("weather","setting","size","class_size","greek","sports","major_strength","prestige","cost"):
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
def fetch_articles(college_slug):
    """Return cached articles if fresh; else fetch from NewsAPI and cache."""
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
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fafafa;color:#1a1a1a;line-height:1.55}
a{color:#2b6cff;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1100px;margin:0 auto;padding:0 20px 60px}
.nav{display:flex;align-items:center;gap:20px;padding:14px 24px;background:#fff;border-bottom:1px solid #e6e6e6;margin-bottom:24px}
.nav .brand{font-weight:800;font-size:1.05em;color:#1a1a1a}
.nav a{color:#444;font-size:.92em;font-weight:500}
.nav .sp{flex:1}
h1{font-size:1.85em;font-weight:800;letter-spacing:-.5px;margin:6px 0 6px}
h2{font-size:1.25em;font-weight:700;margin:24px 0 10px}
h3{font-size:1em;font-weight:700;margin:14px 0 6px}
.muted{color:#666;font-size:.92em}
.btn{display:inline-block;background:#1a1a1a;color:#fff;font-weight:600;padding:11px 22px;border-radius:8px;border:0;cursor:pointer;font-size:.93em;text-decoration:none;font-family:inherit}
.btn:hover{background:#000;text-decoration:none}
.btn-primary{background:#2b6cff}.btn-primary:hover{background:#1f4fd1}
.btn-light{background:#fff;color:#1a1a1a;border:1px solid #ddd}.btn-light:hover{background:#f4f4f4}
.btn-sm{font-size:.82em;padding:6px 12px}
.card{background:#fff;border:1px solid #e6e6e6;border-radius:12px;padding:18px;margin-bottom:12px}
label{display:block;font-weight:600;font-size:.86em;margin:12px 0 4px}
input,select,textarea{width:100%;padding:9px 11px;border:1px solid #d4d4d4;border-radius:7px;font-size:.93em;font-family:inherit;background:#fff}
textarea{min-height:80px;resize:vertical}
input:focus,select:focus,textarea:focus{outline:0;border-color:#2b6cff}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:560px){.row{grid-template-columns:1fr}}
.checks label{display:flex;align-items:center;gap:8px;font-weight:500;margin:6px 0}
.checks input{width:auto}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.7em;font-weight:700;letter-spacing:.4px;text-transform:uppercase}
.pill-dream{background:#fde2e2;color:#9a1d1d}
.pill-reach{background:#fde9c8;color:#8a4a00}
.pill-target{background:#daedff;color:#1547a3}
.pill-safety{background:#dff6e0;color:#1d6c2a}
.pill-tier-1{background:#1a1a1a;color:#fff}
.pill-tier-2{background:#5b4dff;color:#fff}
.pill-tier-3{background:#daedff;color:#1547a3}
.pill-tier-4{background:#f1f1f1;color:#444}
.pill-tier-5{background:#f4f4f4;color:#666}
.pill-public{background:#dff6e0;color:#1d6c2a}
.pill-private{background:#e8e6ff;color:#3d2dc9}
.pill-conf-low{background:#f2f2f2;color:#666}
.pill-conf-medium{background:#e6efff;color:#1547a3}
.pill-conf-high{background:#dff6e0;color:#1d6c2a}
.odds{font-size:1.6em;font-weight:800;letter-spacing:-.5px;margin:8px 0 0}
.flash{padding:10px 14px;background:#fff8e1;border:1px solid #ffeaa7;border-radius:7px;margin-bottom:14px;font-size:.9em}
.flash.error{background:#fdecec;border-color:#f5b3b3;color:#9a1d1d}
.flash.success{background:#e6f7ec;border-color:#a3dcb8;color:#1d6c2a}
.search{display:flex;gap:8px;margin:8px 0 18px}
.search input{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.school-card{background:#fff;border:1px solid #e6e6e6;border-radius:10px;padding:14px;transition:all .15s}
.school-card:hover{border-color:#2b6cff;text-decoration:none;transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.06)}
.school-card a{color:inherit;display:block}
.school-card a:hover{text-decoration:none}
.stat-row{display:flex;justify-content:space-between;font-size:.78em;color:#666;margin-top:8px}
.rank-row{display:flex;align-items:center;gap:14px;padding:14px;background:#fff;border:1px solid #e6e6e6;border-radius:10px;margin-bottom:10px}
.rank-row .num{font-size:1.4em;font-weight:800;color:#999;min-width:30px;text-align:center}
.rank-row .body{flex:1}
.rank-row .body .nm{font-weight:700;color:#1a1a1a}
.rank-row .body .meta{font-size:.78em;color:#666}
.rank-row a{color:inherit}.rank-row a:hover{text-decoration:none}
.bar{display:flex;justify-content:space-between;margin:8px 0 18px;align-items:center;flex-wrap:wrap;gap:8px}
.bar a{font-size:.92em}
.tag-list{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.tag{display:inline-block;padding:3px 9px;border-radius:5px;background:#f1f1f1;color:#444;font-size:.78em}
"""

NAV = """<div class="nav"><a class="brand" href="/colleges">Candor</a>
<a href="/colleges">Browse</a>
<a href="/rankings">Rankings</a>
<a href="/profiles">Real Profiles</a>
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
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{BASE_CSS}</style></head>
<body>{_nav()}<div class="wrap">{_flash()}{body_html}</div></body></html>"""


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
            <div class="stat-row"><span>SAT mid-50%</span><span>{c['sat_25']}-{c['sat_75']}</span></div>
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
<div class="grid">{cards or '<p class="muted">No matches.</p>'}</div>
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
                   "major_strength":"Major strength","prestige":"Prestige","cost":"Cost"}
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
    over = _get_overrides(slug)
    verified_badge = ""
    if over and over.get("source"):
        verified_badge = f'<span class="muted" style="font-size:.78em;background:#dff6e0;color:#1d6c2a;padding:2px 8px;border-radius:5px;margin-left:8px">✓ {over["source"]}</span>'
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
    <div class="odds">{c['sat_25']}–{c['sat_75']}</div>
    <div class="muted" style="font-size:.82em">middle 50% admitted SAT score</div>
  </div>
  <div class="card">
    <h3 style="margin-top:0">ACT mid-50%</h3>
    <div class="odds">{c['act_25']}–{c['act_75']}</div>
    <div class="muted" style="font-size:.82em">middle 50% admitted ACT score</div>
  </div>
</div>
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
.rank-table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e6e6e6;border-radius:12px;overflow:hidden;font-size:.92em}
.rank-table th{background:#f7f7f7;text-align:left;padding:11px 14px;color:#444;font-weight:600;font-size:.78em;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid #e6e6e6}
.rank-table td{padding:11px 14px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
.rank-table tr:hover td{background:#fafafa}
.rank-table tr:last-child td{border-bottom:0}
.rank-table .rank-num{font-weight:800;color:#999;font-variant-numeric:tabular-nums}
.rank-table .name a{color:#1a1a1a;font-weight:700}
.rank-table .num-col{font-variant-numeric:tabular-nums;color:#444}
.rank-table .stars{color:#f0c040;letter-spacing:1px}
.rank-table .stars-empty{color:#ddd}
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
          <td class="hide-sm num-col">{c['sat_25']}-{c['sat_75']}</td>
          <td class="hide-sm num-col">{c['act_25']}-{c['act_75']}</td>
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
        "pref_weather": profile.get("pref_weather"), "pref_setting": profile.get("pref_setting"),
        "pref_size": profile.get("pref_size"), "pref_greek": profile.get("pref_greek"),
        "pref_sports": profile.get("pref_sports"), "pref_major_strength": profile.get("pref_major_strength"),
        "pref_class_size": profile.get("pref_class_size"), "pref_prestige": profile.get("pref_prestige"),
        "pref_cost": profile.get("pref_cost"),
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


def signup_html():
    return _page("""
<div class="bar"><a href="/">&larr; back</a></div>
<h1>Create your account</h1>
<p class="muted">Saves your profile so you can come back without re-entering everything.</p>
<form method="post" action="/signup" class="card" style="max-width:440px">
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
    return _page("""
<div class="bar"><a href="/">&larr; back</a></div>
<h1>Log in</h1>
<form method="post" action="/login" class="card" style="max-width:440px">
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
        "pref_weather":         "Weather",
        "pref_setting":         "Campus setting",
        "pref_size":            "School size",
        "pref_class_size":      "Class size",
        "pref_prestige":        "Prestige",
        "pref_cost":            "Cost",
        "pref_greek":           "Greek life",
        "pref_sports":          "Sports culture",
        "pref_major_strength":  "Major strength",
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
                f'<label style="display:inline-flex;align-items:center;gap:6px;background:#fff;'
                f'border:1px solid #ddd;border-radius:6px;padding:6px 10px;font-weight:500;'
                f'cursor:pointer;font-size:.85em;margin:0">'
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


def profile_html():
    p = get_profile(current_user()["id"]) or {}
    def v(k): return (p.get(k) if p.get(k) is not None else "")
    checked = lambda k: 'checked' if p.get(k) else ''
    return _page(f"""
<h1>Your profile</h1>
<p class="muted">Used by the chances calculator. Be specific — generic answers produce generic odds.</p>
<form method="post" action="/profile" class="card">
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
  </div>

  <h3>Preferences</h3>
  <p class="muted" style="font-size:.85em;margin:-2px 0 8px">Pick everything you'd be happy with for each preference (multi-select), and dial in how much each one matters (1-10, where 5 is neutral). <b>Set importance to 10 to make a preference a deal-breaker</b> — schools that mismatch will be removed from your My Fit ranking.</p>
  {_pref_form_fields(p)}

  <p style="margin-top:18px"><button class="btn btn-primary" type="submit">Save profile</button></p>
</form>
""", title="Profile — Candor")


def chances_html(slug):
    p = get_profile(current_user()["id"])
    if not p:
        flash("Create your profile first so we can calculate your chances.", "error")
        session["next_url"] = f"/chances/{slug}"
        return redirect(url_for("profile_page"))
    profile = {
        "uw_gpa": p.get("uw_gpa"), "weighted_gpa": p.get("weighted_gpa"),
        "sat": p.get("sat"), "act": p.get("act"), "major": p.get("major"),
        "state": p.get("state"), "school_type": p.get("school_type"),
        "ecs": p.get("ecs"), "leadership": p.get("leadership"), "awards": p.get("awards"),
        "legacy": bool(p.get("legacy")), "first_gen": bool(p.get("first_gen")),
        "athlete": bool(p.get("athlete")),
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
    return _page(f"""
<div class="bar"><a href="/college/{r['slug']}">&larr; back to {r['school']}</a></div>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
    <div>
      <h1 style="margin:0">{r['school']}</h1>
      <div class="muted" style="font-size:.85em">Acceptance {r['accept_rate_pct']}% · profile fit {r['fit']}/100</div>
    </div>
    <div><span class="pill {tier_class}">{r['tier']}</span> <span class="pill {conf_class}" style="margin-left:4px">{r['confidence']} confidence</span></div>
  </div>
  <div class="odds" style="color:#2b6cff">{r['odds_low']}–{r['odds_high']}%</div>
  <div class="muted" style="font-size:.82em">your estimated chances</div>
  {render_admissions_breakdown(COLLEGES_BY_SLUG.get(r['slug']), admissions_detail(COLLEGES_BY_SLUG.get(r['slug'])))}
  <ul style="padding-left:18px;margin:18px 0 0">
    <li><b>Strength —</b> {r['strength']}</li>
    <li><b>Weakness —</b> {r['weakness']}</li>
    <li><b>Differentiator —</b> {r['differentiator']}</li>
  </ul>
</div>
<p style="margin-top:18px"><a class="btn btn-light" href="/profile">Edit profile</a> <a class="btn btn-light" href="/college/{r['slug']}/improve">Get tailored advice for {r['school']} &rarr;</a></p>
""", title=f"Your chances at {r['school']} — Candor")


def _resource_block(category, items):
    rows = ""
    for it in items:
        link = f'<a href="{it["url"]}" target="_blank" rel="noopener">{it["title"]}</a>' if it.get("url") else f'<b>{it["title"]}</b>'
        rows += f'<div style="padding:8px 0;border-top:1px solid #eee"><div>{link}</div><div class="muted" style="font-size:.84em;margin-top:2px">{it["note"]}</div></div>'
    return f'<div class="card"><h3 style="margin-top:0">{category}</h3>{rows}</div>'


def improve_html():
    user = current_user()
    profile = get_profile(user["id"]) if user else None
    personalized = ""
    if profile:
        gaps = []
        gpa = profile.get("uw_gpa") or 0
        if gpa and gpa < 3.7:
            gaps.append("GPA below 3.7 — see the Academics section. Upward grade trend can change a competitive read.")
        sat, act = profile.get("sat"), profile.get("act")
        if not sat and not act:
            gaps.append("No test score. See Test Prep — for top schools, a strong score is one of the cleanest ways to stand out.")
        elif sat and sat < 1450:
            gaps.append(f"SAT {sat} is below the typical admitted range at most reach schools. Test prep is high-leverage.")
        elif act and act < 32:
            gaps.append(f"ACT {act} is below the typical admitted range at most reach schools.")
        ec_text = profile.get("ecs") or ""
        if len(ec_text) < 80 or _keyword_strength(ec_text, EC_STRONG_SIGNALS) < 1:
            gaps.append("ECs read thin. See the Extracurriculars + Competitions sections — depth in 1-2 areas beats breadth across many.")
        if not (profile.get("leadership") or "").strip():
            gaps.append("No leadership listed. See the Leadership section for concrete moves.")
        if not (profile.get("awards") or "").strip():
            gaps.append("No awards listed. The Competitions section lists routes to externally-validated recognition.")
        if gaps:
            items = "".join(f"<li>{g}</li>" for g in gaps)
            personalized = f"""<div class="card" style="background:#fff8e1;border-color:#ffeaa7">
                <h3 style="margin-top:0">For your specific profile</h3>
                <p class="muted" style="margin:0 0 8px">Based on what you have saved. Update <a href="/profile">your profile</a> to refine.</p>
                <ul style="padding-left:18px;margin:0">{items}</ul>
            </div>"""
        else:
            personalized = '<div class="card" style="background:#dff6e0;border-color:#a3dcb8"><h3 style="margin-top:0">Your profile is in solid shape</h3><p style="margin:0">No obvious gaps showing. Focus on essays + school-specific advice from each college\'s detail page.</p></div>'
    blocks = ""
    blocks += _resource_block("Academics & rigor", RESOURCES["academics"])
    blocks += _resource_block("Test prep (SAT / ACT)", RESOURCES["test_prep"])
    blocks += _resource_block("Extracurriculars — competitions to enter", RESOURCES["competitions"])
    blocks += _resource_block("Summer programs (selective)", RESOURCES["summer_programs"])
    blocks += _resource_block("Leadership", RESOURCES["leadership"])
    blocks += _resource_block("Essays", RESOURCES["essays"])
    blocks += _resource_block("Recommendation letters", RESOURCES["recommendations"])
    blocks += _resource_block("Demonstrated interest", RESOURCES["interest"])
    return _page(f"""
<h1>How to improve your application</h1>
<p class="muted">Curated, free or near-free resources organized by category. None of this is theoretical — these are the things that actually move admissions outcomes.</p>
{personalized}
{blocks}
<div class="card" style="background:#f4f4f4;border-color:#ddd">
  <h3 style="margin-top:0">Looking for school-specific advice?</h3>
  <p class="muted" style="margin:0 0 8px">Every college's detail page has a "Tailored advice for [school]" link with what that school weights, supplemental essay strategy, and specific programs to apply to.</p>
  <a class="btn btn-light btn-sm" href="/colleges">Browse colleges &rarr;</a>
</div>
""", title="Improve your application — Candor")


def school_improve_html(slug):
    school = COLLEGES_BY_SLUG.get(slug)
    if not school: abort(404)
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
<div class="card">
  <h3 style="margin-top:0">What {school['name']} weights most</h3>
  <p style="margin:0">{note['values']}</p>
</div>
<div class="card">
  <h3 style="margin-top:0">Supplemental essay strategy</h3>
  <p style="margin:0">{note['supplemental_strategy']}</p>
</div>
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
                f"Write a rich 2-3 paragraph overview of {c['name']} — what it's known for, who thrives there, the campus culture, the specific academic strengths, the social scene, and what the location adds (or detracts). "
                f"Be concrete: name specific programs, traditions, or quirks if known. "
                f"Avoid marketing copy — write like a knowledgeable friend describing the school to someone considering applying. "
                f"~250-300 words total. No headers, no bullets — flowing prose."
            )
            response = _claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=f"You write substantive, honest college overviews. Specific, knowledgeable, opinionated where useful. No marketing fluff.\n\n{_date_context()}",
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
ESSAYS_THAT_WORKED = {
    "tufts":   "https://admissions.tufts.edu/apply/essays-that-worked/",
    "jhu":     "https://apply.jhu.edu/application-process/essays-that-worked/",
    "hamilton":"https://www.hamilton.edu/admission/apply/essays-that-worked",
    "conn-college":"https://www.conncoll.edu/admission/apply/essays-that-worked/",
    "ucb":     "https://www.berkeleyside.org/2014/05/05/uc-berkeley-admissions-essays",  # archive
    "uchicago":"https://college.uchicago.edu/admissions/uchicago-supplemental-essay-questions",
    "georgetown":"https://www.georgetown.edu/admissions",
    "stanford":"https://admission.stanford.edu/apply/freshman/essays.html",
    "mit":     "https://mitadmissions.org/blogs/topic/process-application/",
    "duke":    "https://admissions.duke.edu/voices/",
    "harvard": "https://college.harvard.edu/admissions/apply/application-tips",
    "yale":    "https://admissions.yale.edu/sample-essays",
    "ucla":    "https://admission.ucla.edu/apply/personal-insight",
    "northwestern":"https://admissions.northwestern.edu/blogs/",
    "columbia":"https://undergrad.admissions.columbia.edu/apply/instructions",
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

TASK: Write 6-8 SPECIFIC, ACTIONABLE bullets advising this exact student on applying to {school['name']}. Each bullet must:
1. Reference a specific number, program, deadline, course, professor, or activity from the data above (no generic advice).
2. Acknowledge the student's actual profile (their GPA/test/ECs) — what they already have, what's missing for THIS school.
3. Speak directly to fit/mismatch when relevant (e.g. "you preferred warm but X is cold — here's how to evaluate that tradeoff").
4. Be concrete: name the program, the threshold, the action, the deadline.

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
    force = request.args.get("refresh") == "1"
    profiles_md = get_school_profiles(c, force=force)
    essays_md = get_school_essays(c, force=force)
    profiles_html = _render_tailored_advice(profiles_md)
    essays_html = _render_tailored_advice(essays_md)
    # Real essay archive link if we have one
    archive_link = ESSAYS_THAT_WORKED.get(slug)
    archive_html = ""
    if archive_link:
        archive_html = f'<div class="card" style="background:#f0f7ff;border-color:#cfe0ff"><h3 style="margin-top:0">Real published essays for {c["name"]}</h3><p>{c["name"]} publishes admitted-student essays:</p><a class="btn btn-light btn-sm" href="{archive_link}" target="_blank" rel="noopener">Open official archive &rarr;</a></div>'
    generic_links_html = "".join(f'<div style="padding:6px 0;border-top:1px solid #f0f0f0"><a href="{url}" target="_blank" rel="noopener">{label} &rarr;</a></div>' for label, url in GENERIC_PROFILE_LINKS)
    # Reddit search URL specific to this school
    reddit_q = c["name"].replace(" ", "+")
    reddit_url = f"https://www.reddit.com/r/ApplyingToCollege/search/?q={reddit_q}+results&restrict_sr=1&sort=new"
    return _page(f"""
<div class="bar"><a href="/college/{slug}">&larr; back to {c['name']}</a></div>
<h1>Real profiles & essays — {c['name']}</h1>
<p class="muted">{city_state(c)} · {round(c['accept']*100,1)}% acceptance · tier {c['tier']}</p>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 18px">
  <a class="btn btn-light btn-sm" href="/college/{slug}">Overview</a>
  <a class="btn btn-light btn-sm" href="/college/{slug}/plan">My plan</a>
  <a class="btn btn-light btn-sm" href="/college/{slug}/improve">Improve</a>
  <a class="btn btn-light btn-sm" href="/college/{slug}/chat">AI advisor</a>
  <a class="btn btn-light btn-sm" href="?refresh=1">Regenerate</a>
</div>

<div class="card">
  <h3 style="margin-top:0">Composite student profiles</h3>
  <p class="muted" style="font-size:.82em;margin:0 0 8px">Six representative applicants — three admitted, one waitlisted, two rejected — built from real admit patterns at {c['name']}. Names are fictional. Stats reflect the actual admit pool's range.</p>
  {profiles_html}
</div>

<div class="card">
  <h3 style="margin-top:0">Sample essay openings</h3>
  <p class="muted" style="font-size:.82em;margin:0 0 8px">Two illustrative model openings tailored to {c['name']}'s preferred essay style. Use as inspiration, not a template — admissions readers spot copied voice instantly.</p>
  {essays_html}
</div>

{archive_html}

<div class="card">
  <h3 style="margin-top:0">Real-world sources</h3>
  <p class="muted" style="font-size:.82em;margin:0 0 6px">For unfiltered, public profiles + outcomes:</p>
  <div style="padding:6px 0;border-top:1px solid #f0f0f0"><a href="{reddit_url}" target="_blank" rel="noopener">r/ApplyingToCollege results threads for {c['name']} &rarr;</a></div>
  {generic_links_html}
</div>
""", title=f"Real profiles — {c['name']} — Candor")


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
        "pref_weather": profile.get("pref_weather"), "pref_setting": profile.get("pref_setting"),
        "pref_size": profile.get("pref_size"), "pref_greek": profile.get("pref_greek"),
        "pref_sports": profile.get("pref_sports"), "pref_major_strength": profile.get("pref_major_strength"),
        "pref_class_size": profile.get("pref_class_size"), "pref_prestige": profile.get("pref_prestige"),
        "pref_cost": profile.get("pref_cost"),
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
                   "major_strength":"Major strength","prestige":"Prestige","cost":"Cost"}
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
    <div><span class="pill {tier_class}">{r['tier']}</span> <span class="pill {conf_class}" style="margin-left:4px">{r['confidence']} confidence</span></div>
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
    if not rows:
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
    cards = ""
    for row in rows:
        c = COLLEGES_BY_SLUG.get(row["college_slug"])
        if not c: continue
        tier_class = {"Dream":"pill-dream","Reach":"pill-reach","Target":"pill-target","Safety":"pill-safety"}.get(row["tier"], "pill-target")
        # Compute the SAME composite My Fit score the ranking uses, so all
        # surfaces agree on the number for a given school.
        match_score = ""
        if profile:
            prof_dict = {k: profile.get(k) for k in profile.keys()}
            overall, _ = compute_my_fit(prof_dict, c)
            col = "#1d6c2a" if overall >= 80 else ("#8a4a00" if overall >= 60 else "#9a1d1d")
            match_score = f'<div class="muted" style="font-size:.85em;margin-top:4px">My Fit: <span style="color:{col};font-weight:700">{overall}/100</span></div>'
        cards += f"""<a href="/college/{c['slug']}/plan" class="school-card" style="display:block;color:inherit">
          <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
            <div>
              <div style="font-weight:700;font-size:1.05em">{c['name']}</div>
              <div class="muted" style="font-size:.82em">{city_state(c)} · {round(c['accept']*100,1)}% accept · fit {row['fit']}/100</div>
            </div>
            <div><span class="pill {tier_class}">{row['tier']}</span></div>
          </div>
          <div style="font-size:1.4em;font-weight:800;color:#2b6cff;margin-top:8px">{row['odds_low']}–{row['odds_high']}%</div>
          {match_score}
        </a>"""
    return _page(f"""
<h1>My Plans</h1>
<p class="muted">Schools you've computed chances for. Click any card for the full personalized plan — chances, match, gaps, school-specific advice.</p>
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
.chat-msgs{background:#fff;border:1px solid #e6e6e6;border-radius:12px;padding:14px;min-height:300px;max-height:520px;overflow-y:auto;margin-bottom:12px}
.msg{margin:10px 0;display:flex;gap:8px}
.msg-user{justify-content:flex-end}
.msg-bubble{max-width:78%;padding:10px 14px;border-radius:14px;font-size:.95em;line-height:1.5}
.msg-user .msg-bubble{background:#2b6cff;color:#fff}
.msg-assistant .msg-bubble{background:#f1f1f1;color:#1a1a1a}
.msg-bubble ul{margin:6px 0;padding-left:18px}
.msg-bubble li{margin:2px 0}
.chat-input{display:flex;gap:8px;align-items:flex-end}
.chat-input textarea{flex:1;min-height:46px;max-height:160px;padding:10px 12px;border:1px solid #d4d4d4;border-radius:10px;font-family:inherit;resize:vertical;font-size:.95em}
.chat-input button{padding:11px 22px;border-radius:10px;border:0;background:#2b6cff;color:#fff;font-weight:700;cursor:pointer;font-family:inherit}
.chat-input button:disabled{background:#9bb6f0;cursor:wait}
.suggestions{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 14px}
.suggestion{background:#fff;border:1px solid #ddd;border-radius:6px;padding:6px 10px;font-size:.83em;cursor:pointer;color:#444}
.suggestion:hover{border-color:#2b6cff;color:#2b6cff}
.typing{display:flex;gap:4px;padding:10px 14px;background:#f1f1f1;border-radius:14px;width:fit-content}
.typing span{width:8px;height:8px;background:#888;border-radius:50%;animation:typing 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes typing{0%,60%,100%{opacity:.3}30%{opacity:1}}
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
  fetch(SEND_URL, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: msg})})
    .then(function(r){return r.json();})
    .then(function(d){
      clearTyping(); btn.disabled=false; input.focus();
      if(d.error){renderAssistantMsg('<i>'+escapeHTML(d.error)+'</i>');return;}
      renderAssistantMsg(d.html || escapeHTML(d.reply || ''));
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
    header = f'<h1>AI advisor</h1><p class="muted">Personalized to your profile. Conversation history saved between visits.</p>'
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
    header = f'<div class="bar"><a href="/college/{slug}/improve">&larr; back to {school["name"]} advice</a></div><h1>AI advisor — {school["name"]}</h1><p class="muted">Specific to {school["name"]} ({round(school["accept"]*100,1)}% acceptance) and your profile.</p>'
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


def _read_profile_form(form):
    def f(k, cast=str, default=None):
        v = form.get(k)
        if v is None or v == "": return default
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
        "legacy_schools": (f("legacy_schools") or "").strip(),
        "first_gen": form.get("first_gen") in ("yes","on","true","1"),
        "athlete": form.get("athlete") in ("yes","on","true","1"),
    }
    # Legacy boolean is derived from whether they listed any legacy schools.
    result["legacy"] = bool(result["legacy_schools"])
    # Multi-select prefs: getlist returns all checked values. Stored as
    # comma-separated string. Empty string = no preference.
    for key in ("pref_weather","pref_setting","pref_size","pref_greek","pref_sports",
                "pref_major_strength","pref_class_size","pref_prestige","pref_cost"):
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
    return redirect("/colleges")


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


@app.route("/signup", methods=["GET", "POST"])
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
        with db() as conn:
            conn.execute("DELETE FROM tailored_advice WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM saved_chances WHERE user_id=?", (uid,))
            conn.commit()
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


@app.route("/plans")
@login_required
def plans_index_page():
    return plans_index_html()


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
    return jsonify({
        "reply": reply,
        "html": _render_message({"role": "assistant", "content": reply or ""}),
    })


@app.route("/admin/refresh-scorecard")
def admin_refresh_scorecard():
    """Bulk-refresh all 155 schools' stats from College Scorecard.
    Gated by ADMIN_KEY so only the operator can run it (it's slow + makes
    155 API calls). Hit with ?key=YOUR_ADMIN_KEY."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return jsonify({"error": "unauthorized"}), 401
    if not SCORECARD_KEY:
        return jsonify({"error": "SCORECARD_KEY env var not set"}), 500
    only_slug = request.args.get("slug")
    target = [c for c in COLLEGES if c["slug"] == only_slug] if only_slug else COLLEGES
    updated, failed = [], []
    for c in target:
        if update_scorecard_overrides(c):
            updated.append(c["slug"])
        else:
            failed.append(c["slug"])
    return jsonify({"updated": len(updated), "failed": len(failed),
                    "updated_slugs": updated[:30], "failed_slugs": failed[:30]})


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
