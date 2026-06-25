#!/usr/bin/env python3
"""
Ad Intelligence Scraper — Railway hosted version
"""

import json, time, threading, uuid, os, urllib.request
from flask import Flask, request, jsonify, Response, render_template_string

app = Flask(__name__)

TOKEN  = os.environ.get("APIFY_TOKEN", "apify_api_EOWdJTlP8HioILrGOSXC2TRA1bQV7E1vwt0z")
ACTOR  = "automation-lab~facebook-ads-library"
BASE   = "https://api.apify.com/v2"

# in-memory job store: {job_id: {status, log, media, html}}
jobs = {}

COUNTRIES = ["GB","DE","FR","SE","NO","DK","FI","IT","ES","NL","BE","AT","CH","US","AU","CA"]

# ── Apify helpers ─────────────────────────────────────────────────────────────
def api_post(path, payload):
    url  = f"{BASE}/{path}?token={TOKEN}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
           headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def api_get(path):
    sep = "&" if "?" in path else "?"
    url = f"{BASE}/{path}{sep}token={TOKEN}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())

def fetch_url(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer":    "https://www.facebook.com/"
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.read(), r.headers.get("Content-Type", "application/octet-stream")
    except:
        return None, None

def safe(s):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))

# ── scrape worker ─────────────────────────────────────────────────────────────
def run_job(job_id, brand, country, searches):
    job = jobs[job_id]

    def log(msg):
        job["log"].append(msg)

    all_ads = []

    for i, queries in enumerate(searches):
        log(f"🔍 Search {i+1}/{len(searches)}: {queries}")
        try:
            run    = api_post(f"acts/{ACTOR}/runs", {"searchQueries": queries, "country": country, "maxAds": 30})
            run_id = run["data"]["id"]
            log(f"   Run started...")
            for _ in range(80):
                time.sleep(5)
                s      = api_get(f"actor-runs/{run_id}")
                status = s["data"]["status"]
                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    break
            if status != "SUCCEEDED":
                log(f"   ✗ {status}")
                continue
            dsid  = s["data"]["defaultDatasetId"]
            items = api_get(f"datasets/{dsid}/items?limit=100&clean=true")
            ads   = items if isinstance(items, list) else items.get("data", {}).get("items", [])
            log(f"   ✓ {len(ads)} ads found")
            all_ads.extend(ads)
        except Exception as e:
            log(f"   ✗ Error: {e}")

    # deduplicate
    seen, unique = set(), []
    for ad in all_ads:
        aid = ad.get("adArchiveId") or str(ad)
        if aid not in seen:
            seen.add(aid)
            unique.append(ad)

    log(f"📊 {len(unique)} unique ads total")

    # download creatives into memory
    log("📥 Downloading creatives...")
    downloaded = 0
    for ad in unique:
        aid  = ad.get("adArchiveId", "unknown")
        page = safe(ad.get("pageName", brand))
        for i, url in enumerate(ad.get("imageUrls") or []):
            fname = f"{page}_{aid}_img{i+1}.jpg"
            data, ct = fetch_url(url)
            if data:
                job["media"][fname] = (data, ct or "image/jpeg")
                downloaded += 1
            ad.setdefault("_imgs", []).append(fname if data else None)
            time.sleep(0.1)
        for i, url in enumerate(ad.get("videoUrls") or []):
            fname = f"{page}_{aid}_vid{i+1}.mp4"
            data, ct = fetch_url(url)
            if data:
                job["media"][fname] = (data, ct or "video/mp4")
                downloaded += 1
            ad.setdefault("_vids", []).append(fname if data else None)
            time.sleep(0.1)

    log(f"   ✓ {downloaded} creatives downloaded")

    # build viewer HTML
    job["html"]   = build_viewer(job_id, brand, country, unique)
    job["status"] = "done"
    log("✅ Done!")


def build_viewer(job_id, brand, country, ads):
    active = sum(1 for a in ads if a.get("isActive"))

    def card(ad):
        aid     = ad.get("adArchiveId", "")
        page    = ad.get("pageName", "Unknown")
        status  = "ACTIVE" if ad.get("isActive") else "INACTIVE"
        start   = ad.get("startDate", "")
        fmt     = ad.get("displayFormat", "")
        body    = (ad.get("bodyText") or "").replace('"','&quot;').replace('\n','<br>')
        title   = (ad.get("title") or "").replace('"','&quot;')
        cta     = ad.get("ctaText") or ""
        lp      = ad.get("linkUrl") or "#"
        lib_url = ad.get("adLibraryUrl", f"https://www.facebook.com/ads/library/?id={aid}")
        plats   = " · ".join(p.replace("AUDIENCE_NETWORK","AN").replace("FACEBOOK","FB")
                              .replace("INSTAGRAM","IG").replace("MESSENGER","Msg")
                              for p in (ad.get("platforms") or []))
        imgs    = [f for f in (ad.get("_imgs") or []) if f]
        vids    = [f for f in (ad.get("_vids") or []) if f]
        collations = ad.get("collationCount", 0)

        if vids:
            media = f'<div class="media-wrap"><video controls preload="metadata" src="/media/{job_id}/{vids[0]}"></video></div>'
        elif imgs:
            img_html = "".join(f'<img src="/media/{job_id}/{img}" onclick="openFull(this.src)">' for img in imgs[:4])
            media = f'<div class="media-wrap img-grid img-count-{min(len(imgs),4)}">{img_html}</div>'
        else:
            media = f'<div class="media-placeholder"><span>{"🎬" if fmt=="VIDEO" else "🖼️"}</span><a href="{lib_url}" target="_blank">View in Ad Library →</a></div>'

        try:
            from urllib.parse import urlparse
            lp_host = urlparse(lp).netloc
        except:
            lp_host = lp

        return f'''<div class="card" data-status="{status}" data-fmt="{fmt}">
  <div class="card-header">
    <div class="card-name">{page}</div>
    <div class="card-meta">{start} · {plats}</div>
    <div class="badge-row">
      <span class="badge {'active' if status=='ACTIVE' else 'inactive'}">{status}</span>
      <span class="badge fmt">{fmt}</span>
      {"<span class='badge hot'>🔥 "+str(collations)+" variants</span>" if collations>2 else ""}
    </div>
  </div>
  {media}
  <div class="card-body">
    {f'<div class="ad-title">{title}</div>' if title else ""}
    <div class="ad-copy">{body or "<em style='color:#aaa'>No copy</em>"}</div>
  </div>
  <div class="card-footer">
    <span class="cta-pill">{cta}</span>
    <a href="{lp}" target="_blank" class="lp-link">{lp_host}</a>
    <a href="{lib_url}" target="_blank" class="lib-link">Ad Library ↗</a>
  </div>
</div>'''

    cards = "\n".join(card(a) for a in ads)
    COLOR = "#1A3A5C"

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>{brand} — Ad Intel</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:Arial,sans-serif;background:#f0f2f5}}
header{{background:{COLOR};color:white;padding:16px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}}
header h1{{font-size:18px}}header a{{color:rgba(255,255,255,.7);font-size:12px;text-decoration:none}}
header a:hover{{color:white}}
.stats{{display:flex;gap:12px;font-size:12px}}
.stat{{background:rgba(255,255,255,.15);padding:5px 12px;border-radius:20px;text-align:center}}
.stat strong{{display:block;font-size:18px}}
.filters{{background:rgba(0,0,0,.06);padding:8px 24px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.fbtn{{background:rgba(0,0,0,.1);color:#333;border:1px solid #ccc;padding:4px 12px;border-radius:14px;cursor:pointer;font-size:12px}}
.fbtn.on,.fbtn:hover{{background:{COLOR};color:white;border-color:{COLOR}}}
.fcount{{margin-left:auto;font-size:12px;color:#666}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:20px 24px}}
.card{{background:white;border-radius:10px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,.1);display:flex;flex-direction:column}}
.card-header{{padding:10px 12px 8px;border-bottom:1px solid #f0f0f0}}
.card-name{{font-weight:bold;font-size:13px}}.card-meta{{font-size:11px;color:#888;margin-top:2px}}
.badge-row{{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}}
.badge{{font-size:10px;font-weight:bold;padding:2px 7px;border-radius:9px}}
.badge.active{{background:#d4edda;color:#155724}}.badge.inactive{{background:#f8d7da;color:#721c24}}
.badge.fmt{{background:#e2e3e5;color:#383d41}}.badge.hot{{background:#fff3cd;color:#856404}}
.media-wrap{{background:#000;max-height:300px;overflow:hidden}}
.media-wrap video{{width:100%;max-height:300px;object-fit:contain;display:block}}
.img-grid{{display:grid;background:#f7f8fa}}
.img-count-1{{grid-template-columns:1fr}}.img-count-2,.img-count-3,.img-count-4{{grid-template-columns:1fr 1fr}}
.img-grid img{{width:100%;height:150px;object-fit:cover;cursor:zoom-in;border:1px solid #eee}}
.media-placeholder{{background:#f7f8fa;min-height:140px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:#999}}
.media-placeholder span{{font-size:32px}}.media-placeholder a{{color:#1877f2;font-size:13px;font-weight:bold;text-decoration:none}}
.card-body{{padding:10px 12px;flex:1}}.ad-title{{font-weight:bold;font-size:13px;margin-bottom:4px}}
.ad-copy{{font-size:12px;color:#555;line-height:1.5;max-height:72px;overflow:hidden;transition:max-height .3s}}
.ad-copy.open{{max-height:500px}}.toggle-copy{{color:{COLOR};font-size:11px;font-weight:bold;cursor:pointer;margin-top:4px;display:inline-block}}
.card-footer{{padding:8px 12px;border-top:1px solid #f0f0f0;display:flex;gap:6px;align-items:center}}
.cta-pill{{background:{COLOR};color:white;font-size:10px;font-weight:bold;padding:2px 8px;border-radius:10px;white-space:nowrap}}
.lp-link{{font-size:11px;color:#1877f2;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}
.lib-link{{font-size:11px;color:#888;text-decoration:none;white-space:nowrap}}
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:999;align-items:center;justify-content:center;cursor:zoom-out}}
#lb.open{{display:flex}}#lb img{{max-width:92vw;max-height:92vh;border-radius:8px}}
</style></head><body>
<header>
  <div>
    <h1>{brand} — Ad Intelligence</h1>
    <a href="/">← New scrape</a>
  </div>
  <div class="stats">
    <div class="stat"><strong>{len(ads)}</strong>total</div>
    <div class="stat"><strong>{active}</strong>active</div>
  </div>
</header>
<div class="filters">
  <button class="fbtn on" onclick="filter('all',this)">All</button>
  <button class="fbtn" onclick="filter('ACTIVE',this)">Active Only</button>
  <button class="fbtn" onclick="filter('VIDEO',this)">📹 Video</button>
  <button class="fbtn" onclick="filter('IMAGE',this)">🖼 Image</button>
  <span class="fcount" id="fc">{len(ads)} ads</span>
</div>
<div class="grid" id="grid">{cards}</div>
<div id="lb" onclick="this.classList.remove('open')"><img id="lbi" src=""></div>
<script>
function filter(f,btn){{document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');let n=0;document.querySelectorAll('.card').forEach(c=>{{let s=f==='all'||(f==='ACTIVE'&&c.dataset.status==='ACTIVE')||(f==='VIDEO'&&c.dataset.fmt==='VIDEO')||(f==='IMAGE'&&['IMAGE','DCO','DPA'].includes(c.dataset.fmt));c.style.display=s?'':'none';if(s)n++}});document.getElementById('fc').textContent=n+' ads'}}
function openFull(s){{document.getElementById('lbi').src=s;document.getElementById('lb').classList.add('open')}}
document.querySelectorAll('.ad-copy').forEach(el=>{{if(el.scrollHeight>el.clientHeight+5){{const t=document.createElement('span');t.className='toggle-copy';t.textContent='Read more ▼';t.onclick=()=>{{el.classList.toggle('open');t.textContent=el.classList.contains('open')?'Show less ▲':'Read more ▼'}};el.after(t)}}}})</script>
</body></html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

HOME = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Ad Intel Scraper</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:white;border-radius:14px;padding:36px 40px;width:520px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
h1{font-size:22px;color:#1a1a1a;margin-bottom:4px}p.sub{font-size:13px;color:#888;margin-bottom:28px}
label{display:block;font-size:12px;font-weight:bold;color:#555;margin-bottom:5px;margin-top:16px}
input,select{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:7px;font-size:14px;outline:none}
input:focus,select:focus{border-color:#1877f2}
.search-row{display:flex;gap:6px;margin-bottom:6px}
.search-row input{flex:1}
.remove-btn{background:#fee;border:1px solid #fcc;color:#c00;border-radius:6px;padding:0 10px;cursor:pointer;font-size:16px;flex-shrink:0}
.add-btn{background:none;border:1px dashed #bbb;color:#888;border-radius:7px;padding:7px;width:100%;cursor:pointer;font-size:13px;margin-top:4px}
.add-btn:hover{border-color:#1877f2;color:#1877f2}
.submit-btn{background:#1877f2;color:white;border:none;border-radius:8px;padding:12px;width:100%;font-size:15px;font-weight:bold;cursor:pointer;margin-top:24px}
.submit-btn:hover{background:#166fe5}
.hint{font-size:11px;color:#aaa;margin-top:4px}
</style></head><body>
<div class="card">
  <h1>Ad Intelligence Scraper</h1>
  <p class="sub">Scrape Meta Ad Library for any brand</p>
  <form method="POST" action="/start">
    <label>Brand Name</label>
    <input name="brand" placeholder="e.g. GLPure" required>
    <label>Brand Website <span style="font-weight:normal;color:#aaa">(optional — domain auto-added to search)</span></label>
    <input name="domain" placeholder="e.g. https://get-glpure.com/en-GB">
    <label>Country</label>
    <select name="country">
      COUNTRY_OPTIONS
    </select>
    <label>Search Terms <span style="font-weight:normal;color:#aaa">(one group per row, comma-separated)</span></label>
    <div id="searches">
      <div class="search-row"><input name="search[]" placeholder="e.g. glpure, gl pure" required><button type="button" class="remove-btn" onclick="removeRow(this)">×</button></div>
      <div class="search-row"><input name="search[]" placeholder="e.g. get-glpure.com"><button type="button" class="remove-btn" onclick="removeRow(this)">×</button></div>
    </div>
    <button type="button" class="add-btn" onclick="addRow()">+ Add search group</button>
    <p class="hint">Tip: brand name, domain, and category terms give the best coverage</p>
    <button type="submit" class="submit-btn">Start Scrape →</button>
  </form>
</div>
<script>
function addRow(){const d=document.getElementById('searches');const r=document.createElement('div');r.className='search-row';r.innerHTML='<input name="search[]" placeholder="Search terms..."><button type="button" class="remove-btn" onclick="removeRow(this)">×</button>';d.appendChild(r)}
function removeRow(b){if(document.querySelectorAll('.search-row').length>1)b.parentElement.remove()}
</script></body></html>"""

COUNTRY_OPTIONS = "\n".join(f'<option value="{c}">{c}</option>' for c in COUNTRIES)

@app.route("/")
def home():
    return HOME.replace("COUNTRY_OPTIONS", COUNTRY_OPTIONS)

@app.route("/start", methods=["POST"])
def start():
    brand        = request.form.get("brand", "Brand").strip()
    country      = request.form.get("country", "GB")
    domain_input = request.form.get("domain", "").strip()
    searches_raw = request.form.getlist("search[]")
    searches     = [[q.strip() for q in s.split(",") if q.strip()] for s in searches_raw if s.strip()]

    # extract domain from URL and prepend as a search group
    if domain_input:
        from urllib.parse import urlparse
        parsed  = urlparse(domain_input if "://" in domain_input else "https://" + domain_input)
        netloc  = parsed.netloc
        if netloc:
            searches.insert(0, [netloc])

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "log": [], "media": {}, "html": None}

    threading.Thread(target=run_job, args=(job_id, brand, country, searches), daemon=True).start()

    return render_template_string(PROGRESS_HTML, job_id=job_id, brand=brand)

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id, {"status": "not_found", "log": []})
    return jsonify({"status": job["status"], "log": job["log"]})

@app.route("/viewer/<job_id>")
def viewer(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("html"):
        return "Job not found or still running.", 404
    return job["html"]

@app.route("/media/<job_id>/<filename>")
def media(job_id, filename):
    job = jobs.get(job_id)
    if not job:
        return "Not found", 404
    item = job["media"].get(filename)
    if not item:
        return "Not found", 404
    data, ct = item
    return Response(data, content_type=ct)

PROGRESS_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Scraping {{ brand }}…</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:white;border-radius:14px;padding:36px 40px;width:520px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
h1{font-size:20px;color:#1a1a1a;margin-bottom:20px}
#log{font-family:monospace;font-size:13px;line-height:1.8;color:#333;min-height:200px;max-height:340px;overflow-y:auto;background:#f8f8f8;border-radius:8px;padding:14px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #ddd;border-top-color:#1877f2;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
#open-btn{display:none;background:#1877f2;color:white;border:none;border-radius:8px;padding:12px;width:100%;font-size:15px;font-weight:bold;cursor:pointer;margin-top:20px}
#open-btn:hover{background:#166fe5}
#new-btn{display:none;background:#f0f2f5;color:#555;border:none;border-radius:8px;padding:10px;width:100%;font-size:13px;cursor:pointer;margin-top:8px}
</style></head><body>
<div class="card">
  <h1><span class="spinner" id="spin"></span>Scraping {{ brand }}…</h1>
  <div id="log">Starting…</div>
  <button id="open-btn" onclick="window.location='/viewer/{{ job_id }}'">Open Viewer →</button>
  <button id="new-btn" onclick="window.location='/'">← Scrape another brand</button>
</div>
<script>
function poll(){
  fetch('/status/{{ job_id }}').then(r=>r.json()).then(d=>{
    document.getElementById('log').innerHTML=d.log.map(l=>
      l.replace(/✅/,'<span style="color:green">✅</span>')
       .replace(/✗/,'<span style="color:#c00">✗</span>')
       .replace(/✓/,'<span style="color:green">✓</span>')
    ).join('<br>');
    const el=document.getElementById('log');
    el.scrollTop=el.scrollHeight;
    if(d.status==='done'){
      document.getElementById('spin').style.display='none';
      document.getElementById('open-btn').style.display='block';
      document.getElementById('new-btn').style.display='block';
    } else { setTimeout(poll,2500); }
  });
}
poll();
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
