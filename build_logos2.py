import json, os, re, ssl, urllib.request

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 Candor"}), timeout=40, context=ctx).read()

colleges = json.loads(open("/tmp/colleges.txt").read().strip().splitlines()[-1])

# index ESPN teams by normalized location / shortDisplayName / abbreviation
def norm(s):
    s = (s or "").lower()
    s = s.replace("&","and").replace("’","'").replace("'","")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(university|college|the|of|at|state)\b" if False else r"\b(university|college|the|of|at)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

by_loc, by_sdn, by_abbr, by_dn = {}, {}, {}, {}
for sport in ("football/college-football", "basketball/mens-college-basketball"):
    try:
        d = json.loads(fetch(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/teams?limit=900"))
        for t in d["sports"][0]["leagues"][0]["teams"]:
            tm = t["team"]; logos = tm.get("logos") or []
            if not logos: continue
            href = logos[0]["href"]; dn = tm.get("displayName","")
            for idx, key in ((by_loc, norm(tm.get("location",""))),
                             (by_sdn, norm(tm.get("shortDisplayName",""))),
                             (by_abbr, (tm.get("abbreviation","") or "").lower()),
                             (by_dn, dn.lower().strip())):
                if key and key not in idx: idx[key] = (href, dn)
    except Exception as e:
        print("fetch fail", sport, e)
print("ESPN indexed: loc", len(by_loc))

# direct exact-displayName overrides (fix collisions where a tiny "X College"
# stole the location key) and a blocklist (wrong matches with no clean fix).
DIRECT = {"uiuc": "illinois fighting illini", "ut-austin": "texas longhorns",
          "bu": "boston university terriers"}
BLOCK = {"colorado-college"}

# Curated aliases: slug -> exact ESPN location key (normalized) for cases where
# the college's name won't directly equal the ESPN location.
ALIAS = {
 "ucb":"california","ucla":"ucla","ucsd":"uc san diego","uci":"uc irvine","ucsb":"uc santa barbara",
 "ucdavis":"uc davis","ucr":"uc riverside","usc":"usc","unc":"north carolina","uva":"virginia",
 "umich":"michigan","uf":"florida","uw":"washington","wisc":"wisconsin","ut-austin":"texas",
 "umd":"maryland","gwu":"george washington","osu":"ohio state","iu":"indiana","uiuc":"illinois",
 "uiowa":"iowa","umn":"minnesota","cu-boulder":"colorado","uoregon":"oregon","uconn":"uconn",
 "pitt":"pittsburgh","utk":"tennessee","uga":"georgia","alabama":"alabama","arizona":"arizona",
 "uky":"kentucky","sc":"south carolina","missou":"missouri","ku":"kansas","unl":"nebraska",
 "uvm":"vermont","udel":"delaware","uh":"houston","utah":"utah","hawaii":"hawaii","unh":"new hampshire",
 "umaine":"maine","umass":"massachusetts","ncsu":"nc state","wvu":"west virginia","louisville":"louisville",
 "oklahoma":"oklahoma","utsa":"utsa","wsu":"washington state","unc-charlotte":"charlotte","ecu":"east carolina",
 "fau":"florida atlantic","jmu":"james madison","app-state":"appalachian state","unlv":"unlv","odu":"old dominion",
 "miss-state":"mississippi state","uri":"rhode island","usf-tampa":"south florida","ucf":"ucf","fiu":"fiu",
 "vcu":"vcu","unm":"new mexico","gsu":"georgia state","oklahoma-state":"oklahoma state","oregon-state":"oregon state",
 "colorado-state":"colorado state","cincinnati":"cincinnati","gmu":"george mason","iowa-state":"iowa state",
 "sjsu":"san jose state","csulb":"long beach state","calpoly-slo":"cal poly","binghamton":"binghamton",
 "stony-brook":"stony brook","suny-buffalo":"buffalo","temple":"temple","rutgers":"rutgers","syracuse":"syracuse",
 "marquette":"marquette","depaul":"depaul","loyola-chicago":"loyola chicago","slu":"saint louis","dayton":"dayton",
 "butler":"butler","drake":"drake","tulsa":"tulsa","hofstra":"hofstra","towson":"towson","umbc":"umbc",
 "howard":"howard","famu":"florida aandm","hampton":"hampton","liberty":"liberty","baylor":"baylor","tcu":"tcu",
 "smu":"smu","auburn":"auburn","lsu":"lsu","clemson":"clemson","fsu":"florida state","vt":"virginia tech",
 "tamu":"texas aandm","penn-state":"penn state","msu":"michigan state","asu":"arizona state","purdue":"purdue",
 "nau":"northern arizona","wmu":"western michigan","emich":"eastern michigan","niu":"northern illinois",
 "ilstu":"illinois state","bgsu":"bowling green","ohio-u":"ohio","kent-state":"kent state","akron":"akron",
 "boise-state":"boise state","unf":"north florida","stetson":"stetson","seattle-u":"seattle u","usd":"san diego",
 "usf-sf":"san francisco","drexel":"drexel","northeastern":"northeastern","villanova":"villanova","bucknell":"bucknell",
 "lehigh":"lehigh","lafayette":"lafayette","holy-cross":"holy cross","colgate":"colgate","fordham":"fordham",
 "davidson":"davidson","pepperdine":"pepperdine","scu":"santa clara","lmu":"loyola marymount","col-charleston":"charleston",
 "gatech":"georgia tech","byu":"byu","bu":"boston university","bc":"boston college","wake-forest":"wake forest",
 "wm":"william and mary","penn-state":"penn state","stevens":"stevens","richmond":"richmond","duquesne":"duquesne",
}

matches, review, skipped = {}, [], []
for c in colleges:
    slug, nm = c["slug"], c["name"]
    if slug in BLOCK:
        skipped.append((slug, nm)); continue
    hit = None
    if slug in DIRECT:
        hit = by_dn.get(DIRECT[slug])
    if not hit and slug in ALIAS:
        hit = by_loc.get(ALIAS[slug]) or by_sdn.get(ALIAS[slug])
    if not hit:
        n = norm(nm)
        hit = by_loc.get(n) or by_sdn.get(n)
    if not hit:
        hit = by_abbr.get(slug.lower())
    if hit:
        href, dn = hit
        matches[slug] = href; review.append((slug, nm, dn))
    else:
        skipped.append((slug, nm))

print(f"\nMATCHED {len(matches)} / {len(colleges)}  (downloading...)")
os.makedirs("static/logos", exist_ok=True)
final, failed = {}, []
for slug, href in matches.items():
    try:
        data = fetch(href)
        if len(data) < 500:
            failed.append(slug); continue
        with open(f"static/logos/{slug}.png", "wb") as f:
            f.write(data)
        final[slug] = f"/static/logos/{slug}.png"
    except Exception as e:
        failed.append(slug); print("dl fail", slug, e)
print(f"DOWNLOADED {len(final)} logos; failed: {failed}")
print(f"SKIPPED {len(skipped)} (no ESPN / D3 / art)")
# emit the SCHOOL_LOGOS dict literal
with open("logos_map.json", "w") as f:
    json.dump(final, f)
print("\n--- SCHOOL_LOGOS ---")
print("{" + ", ".join(f'"{k}": "{v}"' for k, v in sorted(final.items())) + "}")
