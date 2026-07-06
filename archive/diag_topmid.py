import os; os.environ.setdefault("NO_NET","1")
import app
EC_TOP="Founder of a 200+ member nonprofit with national award, USAMO/ISEF-level recognition, published research, 4 years sustained, 15+ hrs/wk in field"
EC_STRONG="Founder of a real student org, varsity captain, summer research/internship, 3 years sustained, deep 10hr/wk in field"
EC_SOLID="Club officer, varsity team member, 2 years volunteering, part-time job, newspaper contributor"
EC_WEAK="Member of two clubs, some volunteering, no leadership or sustained commitment"
def P(g,s,ec,aw,exc=False):
    return dict(uw_gpa=g,sat=s,act=0,ecs=ec,leadership="Club president",awards=aw,
               aps="Calc BC, Bio, Chem, Physics, Lang, US History",major="",state="",school_type="private",is_exceptional=exc)
profs=[("TOP   4.0/1570",P(4.0,1570,EC_TOP,"National award, USAMO finalist")),
       ("STRONG 3.95/1520",P(3.95,1520,EC_STRONG,"National Merit, state award")),
       ("MID   3.7/1380",P(3.7,1380,EC_SOLID,"Honor roll, AP Scholar")),
       ("WEAK  3.5/1250",P(3.5,1250,EC_WEAK,"Honor roll"))]
schools=["stanford","mit","cornell","ucla","umich","ucsd","tulane","asu"]
def odds(prof,slug):
    sc=app.merged_school(app.COLLEGES_BY_SLUG[slug])
    fit,_=app.compute_fit(prof,sc); lo,hi=app.estimate_odds(sc,fit,prof)
    return f"{lo}-{hi}%"
hdr="profile".ljust(17)+"".join(f"{s[:9]:>11}" for s in schools)
print(hdr); print("-"*len(hdr))
for name,pr in profs:
    print(name.ljust(17)+"".join(f"{odds(pr,s):>11}" for s in schools))
print("\naccept rates:", {s: f"{app.merged_school(app.COLLEGES_BY_SLUG[s])['accept']*100:.0f}%" for s in schools})
