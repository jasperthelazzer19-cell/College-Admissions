import json, os, urllib.request, ssl

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 CandorContentTool"}

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30, context=ctx).read()

# slug -> ESPN displayName (athletic team). D3 schools (MIT/Caltech/Chicago/NYU/JHU)
# aren't in ESPN D1 -> they'll fall back to the wordmark.
ALIAS = {
    "ucb":"california golden bears","uc-berkeley":"california golden bears",
    "ucla":"ucla bruins","harvard":"harvard crimson","yale":"yale bulldogs",
    "stanford":"stanford cardinal","uva":"virginia cavaliers","unc":"north carolina tar heels",
    "umich":"michigan wolverines","usc":"usc trojans","northwestern":"northwestern wildcats",
    "princeton":"princeton tigers","columbia":"columbia lions","cornell":"cornell big red",
    "upenn":"penn quakers","brown":"brown bears","dartmouth":"dartmouth big green",
    "duke":"duke blue devils","gatech":"georgia tech yellow jackets","rice":"rice owls",
    "vanderbilt":"vanderbilt commodores","notre-dame":"notre dame fighting irish",
    "georgetown":"georgetown hoyas","ucsd":"uc san diego tritons","ucsb":"uc santa barbara gauchos",
    "uci":"uc irvine anteaters","ucd":"uc davis aggies","uc-davis":"uc davis aggies",
    "ut-austin":"texas longhorns","uw":"washington huskies","wisconsin":"wisconsin badgers",
    "uiuc":"illinois fighting illini","purdue":"purdue boilermakers","jhu":"johns hopkins blue jays",
    "johns-hopkins":"johns hopkins blue jays",
    # extra top-50 mainstream
    "florida":"florida gators","uga":"georgia bulldogs","osu":"ohio state buckeyes",
    "ohio-state":"ohio state buckeyes","penn-state":"penn state nittany lions",
    "michigan-state":"michigan state spartans","ucsc":"uc santa cruz","unc-chapel-hill":"north carolina tar heels",
    "tamu":"texas a&m aggies","texas-am":"texas a&m aggies","ufl":"florida gators",
    "wustl":"washington university bears","bc":"boston college eagles","bu":"boston university terriers",
    "tufts":"tufts jumbos","emory":"emory eagles","wake-forest":"wake forest demon deacons",
    "boston-college":"boston college eagles","virginia-tech":"virginia tech hokies","vt":"virginia tech hokies",
}

# pull both football (more logos) and basketball (covers Ivies/UC/Georgetown)
teams = {}
for sport in ("football/college-football", "basketball/mens-college-basketball"):
    try:
        d = json.loads(fetch(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/teams?limit=900"))
        for t in d["sports"][0]["leagues"][0]["teams"]:
            tm = t["team"]; dn = tm.get("displayName","").lower()
            logos = tm.get("logos") or []
            href = logos[0]["href"] if logos else None
            if dn and href and dn not in teams:
                teams[dn] = href
    except Exception as e:
        print("sport fetch failed", sport, e)
print("ESPN teams indexed:", len(teams))

os.makedirs("static/logos", exist_ok=True)
got = {}
missed = []
for slug, name in ALIAS.items():
    href = teams.get(name)
    if not href:
        # loose contains match
        href = next((h for dn,h in teams.items() if name in dn or dn in name), None)
    if not href:
        missed.append(slug); continue
    try:
        data = fetch(href)
        with open(f"static/logos/{slug}.png","wb") as f: f.write(data)
        got[slug] = f"/static/logos/{slug}.png"
    except Exception as e:
        missed.append(slug); print("dl fail", slug, e)

print("\nDOWNLOADED", len(got), "logos")
print("MISSED:", missed)
# emit the python dict literal for SCHOOL_LOGOS
print("\n--- SCHOOL_LOGOS ---")
print("{" + ", ".join(f'"{k}": "{v}"' for k,v in sorted(got.items())) + "}")
json.dump(got, open("logos_map.json","w"), indent=0)
