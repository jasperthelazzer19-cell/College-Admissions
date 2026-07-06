# ══ Deterministic round-split — calibrated 2350 real stored round-odds (384 users). ══
# ECs/stats are already priced into odds_low/high UPSTREAM; this only SPLITS that
# overall number across ED/EA/RD using each school's own published pool ratio,
# calibrated to real Candor behavior. Set ROUND_SPLIT_MODE=llm to revert to the API.
_RS_ELITE = ['brown', 'caltech', 'columbia', 'cornell', 'dartmouth', 'duke', 'harvard', 'johns-hopkins', 'mit', 'northwestern', 'princeton', 'rice', 'stanford', 'uchicago', 'upenn', 'vanderbilt', 'yale']
_RS_YIELD = ['american', 'boston-college', 'boston-university', 'brandeis', 'case-western', 'fordham', 'george-washington', 'lehigh', 'miami', 'northeastern', 'pepperdine', 'rochester', 'smu', 'syracuse', 'tufts', 'tulane', 'villanova', 'wake-forest']
_RS_SCHOOL_MULT = {'babson|ED2|hi': 1.235, 'babson|ED|hi': 1.391, 'bc|ED2|hi': 1.309, 'bc|ED|hi': 1.536, 'brown|ED|hi': 1.448, 'brown|ED|lo': 1.829, 'brown|ED|mid': 1.67, 'bu|ED2|hi': 1.424, 'bu|ED|hi': 1.68, 'caltech|REA|lo': 1.892, 'clemson|EA|hi': 1.084, 'cmu|ED|hi': 1.274, 'cmu|ED|mid': 1.474, 'columbia|ED|lo': 2.733, 'columbia|ED|mid': 1.8, 'cornell|ED|hi': 1.688, 'cornell|ED|mid': 2.333, 'dartmouth|ED|lo': 2.31, 'duke|ED|lo': 2.353, 'duke|ED|mid': 1.938, 'emory|ED2|hi': 1.337, 'emory|ED|hi': 1.618, 'gatech|EA|hi': 1.169, 'gatech|EA|mid': 1.202, 'georgetown|EA|hi': 1.2, 'georgetown|EA|mid': 1.231, 'harvard|REA|hi': 1.259, 'harvard|REA|lo': 1.28, 'harvard|REA|mid': 1.235, 'iu|EA|hi': 1.065, 'jhu|ED2|mid': 1.573, 'jhu|ED|mid': 2.077, 'lehigh|ED2|hi': 1.296, 'lehigh|ED|hi': 1.5, 'miami|EA|hi': 1.21, 'miami|ED2|hi': 1.393, 'miami|ED|hi': 1.679, 'mit|EA|hi': 1.391, 'mit|EA|lo': 1.4, 'mit|EA|mid': 1.341, 'northeastern|EA|hi': 1.568, 'northeastern|EA|mid': 1.727, 'northeastern|ED2|hi': 1.824, 'northeastern|ED2|mid': 1.8, 'northeastern|ED|hi': 2.122, 'northeastern|ED|mid': 2.4, 'northwestern|ED|hi': 1.583, 'northwestern|ED|mid': 2.222, 'notre-dame|REA|hi': 1.385, 'nyu|ED2|hi': 1.342, 'nyu|ED2|mid': 1.549, 'nyu|ED|hi': 1.564, 'nyu|ED|mid': 1.85, 'penn-state|EA|hi': 1.074, 'princeton|REA|hi': 1.231, 'princeton|REA|lo': 1.411, 'princeton|REA|mid': 1.273, 'purdue|EA|hi': 1.078, 'rice|ED2|mid': 1.286, 'rice|ED|mid': 1.586, 'stanford|REA|hi': 1.273, 'stanford|REA|lo': 1.371, 'stanford|REA|mid': 1.294, 'stevens|ED2|hi': 1.205, 'stevens|ED|hi': 1.341, 'tufts|ED2|mid': 1.703, 'tufts|ED|mid': 2.168, 'tulane|ED|mid': 4.404, 'uchicago|EA|lo': 1.486, 'uchicago|EA|mid': 1.2, 'uchicago|ED2|hi': 1.333, 'uchicago|ED2|lo': 1.857, 'uchicago|ED2|mid': 1.536, 'uchicago|ED|hi': 1.583, 'uchicago|ED|lo': 2.343, 'uchicago|ED|mid': 1.862, 'uf|EA|hi': 1.095, 'uga|EA|hi': 1.114, 'uiuc|EA|hi': 1.22, 'umd|EA|hi': 1.127, 'umich|EA|hi': 1.16, 'umich|EA|mid': 1.333, 'umich|ED|hi': 1.333, 'unc|EA|hi': 1.153, 'unc|EA|mid': 1.371, 'upenn|ED|hi': 1.5, 'upenn|ED|lo': 1.873, 'upenn|ED|mid': 1.696, 'usc|EA|hi': 1.235, 'usc|EA|mid': 1.308, 'ut-austin|EA|hi': 1.138, 'ut-austin|EA|mid': 1.31, 'uva|EA|hi': 1.338, 'uva|EA|mid': 1.5, 'uva|ED|hi': 1.8, 'uva|ED|mid': 2.168, 'vanderbilt|ED2|lo': 1.833, 'vanderbilt|ED2|mid': 1.584, 'vanderbilt|ED|lo': 2.278, 'vanderbilt|ED|mid': 1.942, 'villanova|EA|hi': 1.105, 'villanova|ED1|hi': 1.793, 'villanova|ED2|hi': 1.514, 'wake-forest|ED2|hi': 1.302, 'wake-forest|ED|hi': 1.584, 'washu|ED2|hi': 1.267, 'washu|ED2|mid': 1.571, 'washu|ED|hi': 1.556, 'washu|ED|mid': 2.0, 'wisc|EA|hi': 1.118, 'wm|ED2|hi': 1.179, 'wm|ED|hi': 1.357, 'yale|REA|lo': 1.327, 'yale|REA|mid': 1.25}
_RS_CAPTURE = {'elite|hi|EA': 1.082, 'elite|hi|ED': 0.227, 'elite|hi|ED2': 0.14, 'elite|hi|REA': 0.174, 'elite|lo|EA': 0.544, 'elite|lo|ED': 0.445, 'elite|lo|ED2': 0.316, 'elite|lo|REA': 0.216, 'elite|mid|EA': 0.796, 'elite|mid|ED': 0.359, 'elite|mid|ED2': 0.222, 'elite|mid|REA': 0.164, 'public|hi|EA': 0.721, 'public|hi|ED': 0.417, 'public|hi|ED2': 0.615, 'public|mid|EA': 0.987, 'public|mid|ED': 0.605, 'standard|hi|EA': 0.702, 'standard|hi|ED': 0.302, 'standard|hi|ED2': 0.247, 'standard|hi|REA': 0.269, 'standard|lo|ED': 0.389, 'standard|lo|ED2': 0.363, 'standard|mid|EA': 0.821, 'standard|mid|ED': 0.392, 'standard|mid|ED2': 0.335, 'standard|mid|REA': 0.4, 'yield|hi|EA': 0.337, 'yield|hi|ED': 0.38, 'yield|hi|ED2': 0.329, 'yield|lo|EA': 0.788, 'yield|lo|ED': 0.252, 'yield|lo|ED2': 0.206, 'yield|mid|EA': 0.613, 'yield|mid|ED': 0.365, 'yield|mid|ED2': 0.333}
_RS_CAP_DEF = {'elite': 0.45, 'yield': 0.8, 'standard': 0.62, 'public': 0.45}
_RS_FALLBACK = {"ED":1.8,"ED1":1.8,"ED2":1.5,"REA":1.4,"EA":1.3}

def _rs_regime(slug, school_type=""):
    s=(slug or "").lower()
    if any(k in s for k in _RS_YIELD): return "yield"
    if s in _RS_ELITE or any(k in s for k in _RS_ELITE): return "elite"
    if "public" in (school_type or "").lower(): return "public"
    return "standard"
def _rs_bucket(rd): return "lo" if rd<0.05 else ("mid" if rd<0.15 else "hi")
def _rs_rtype(rc): return "ED" if rc in ("ED","ED1") else ("ED2" if rc=="ED2" else ("REA" if rc=="REA" else "EA"))
def _rs_capture(reg,b,rt):
    return (_RS_CAPTURE.get(f"{reg}|{b}|{rt}") or _RS_CAPTURE.get(f"{reg}|mid|{rt}") or _RS_CAP_DEF.get(reg,0.6))
def _rs_pool_ratio(pub, rc):
    rdp=pub.get("RD")
    if not rdp or rdp<=0: return None
    v=pub.get(rc) or (pub.get("ED") if rc in ("ED1","ED2") else None)
    return (v/rdp) if v else None
def _rs_school_mult(slug, pub, rc, b, reg):
    k=f"{slug}|{rc}|{b}"
    if k in _RS_SCHOOL_MULT: return _RS_SCHOOL_MULT[k]
    pr=_rs_pool_ratio(pub, rc)
    if pr and pr>1.0:
        cf=_rs_capture(reg,b,_rs_rtype(rc))
        if rc=="ED2": cf*=0.8
        return 1+(pr-1)*cf
    return _RS_FALLBACK.get(rc,1.3)

def deterministic_round_odds(school, detail, rd_frac):
    """Split an overall chance (rd_frac = personalized midpoint, EC-inclusive)
    across a school's rounds using calibrated per-school multipliers. No API call."""
    rounds=detail.get("rounds") or []
    pub=detail.get("rates") or {}
    if not rounds or rd_frac is None or rd_frac<=0: return None
    rd=max(0.005, min(0.95, rd_frac))
    reg=_rs_regime(school.get("slug",""), school.get("type",""))
    b=_rs_bucket(rd)
    out={"RD":round(rd,4)}
    for rc in rounds:
        if rc=="RD": continue
        m=_rs_school_mult(school.get("slug",""), pub, rc, b, reg)
        m_eff=1.0+(m-1.0)*(1.0-rd)                 # headroom damping
        out[rc]=min(0.95, rd*m_eff)
    if "ED2" in out:
        top=max([out.get("ED",0), out.get("ED1",0)])
        if top: out["ED2"]=min(out["ED2"], top)
    for rc in rounds:
        if rc!="RD":
            floor = rd*1.03 if _rs_rtype(rc)!="EA" else rd
            out[rc]=round(max(out[rc], floor),4)
    return out
