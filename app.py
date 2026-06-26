#!/usr/bin/env python3
"""
Ad Intelligence Scraper — Railway hosted version
Supports: Meta, Google, TikTok, LinkedIn
"""

import json, time, threading, uuid, urllib.request, os
from urllib.parse import urlparse, quote as urlquote
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, render_template_string

app = Flask(__name__)

TOKEN = os.environ.get("APIFY_TOKEN", "apify_api_EOWdJTlP8HioILrGOSXC2TRA1bQV7E1vwt0z")
BASE  = "https://api.apify.com/v2"

jobs = {}

COUNTRIES = ["","GB","DE","FR","SE","NO","DK","FI","IT","ES","NL","BE","AT","CH","US","AU","CA"]

PLATFORMS = {
    "meta":   {"label": "📘 Meta (Facebook/Instagram)", "color": "#1877f2",
               "actor": "automation-lab~facebook-ads-library"},
    "google": {"label": "🔵 Google Ads Transparency",   "color": "#4285F4",
               "actor": "automation-lab~google-ads-scraper"},
    "tiktok": {"label": "⬛ TikTok Ad Library",         "color": "#010101",
               "actor": "rFFzT2mRuOd1K4iTM"},
}

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

def wait_for_run(run_id, log):
    status = "UNKNOWN"
    for _ in range(80):
        time.sleep(5)
        s      = api_get(f"actor-runs/{run_id}")
        status = s["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    if status != "SUCCEEDED":
        log(f"   ✗ {status}")
        return []
    dsid  = s["data"]["defaultDatasetId"]
    items = api_get(f"datasets/{dsid}/items?limit=200&clean=true")
    return items if isinstance(items, list) else items.get("data", {}).get("items", [])

# ── URL extraction per platform ───────────────────────────────────────────────
def extract_urls(ad, platform):
    imgs, vids = [], []

    if platform == "meta":
        snap = ad.get("snapshot") or {}
        for u in (ad.get("imageUrls") or []):
            if u: imgs.append(u)
        for u in (ad.get("videoUrls") or []):
            if u: vids.append(u)
        for obj in list(snap.get("images") or []) + list(snap.get("cards") or []):
            u = obj.get("resizedImageUrl") or obj.get("originalImageUrl")
            if u and u not in imgs: imgs.append(u)
        for obj in (snap.get("videos") or []):
            u = obj.get("videoUrl") or obj.get("videoHdUrl")
            if u and u not in vids: vids.append(u)

    elif platform == "google":
        for v in (ad.get("variations") or []):
            img = v.get("imageUrl")
            vid = v.get("videoUrl")
            if img and img not in imgs: imgs.append(img)
            if vid and vid not in vids: vids.append(vid)
        preview = ad.get("previewUrl")
        if preview and preview not in imgs: imgs.append(preview)

    elif platform == "tiktok":
        # data_xplorer: "Ad Media" = ["Video 1: url", "Cover 1: url", "Image 1: url"]
        for item in (ad.get("Ad Media") or []):
            if not isinstance(item, str): continue
            colon = item.find(": ")
            if colon == -1: continue
            label = item[:colon].lower()
            url   = item[colon+2:].strip()
            if not url: continue
            if "video" in label and url not in vids: vids.append(url)
            elif url not in imgs: imgs.append(url)
        # fallback: older field names from silva95gustavo
        for v in (ad.get("videos") or []):
            u = v.get("url") or v.get("videoUrl")
            c = v.get("coverImageUrl") or v.get("cover")
            if u and u not in vids: vids.append(u)
            if c and c not in imgs: imgs.append(c)
        for u in (ad.get("imageUrls") or []):
            if u and u not in imgs: imgs.append(u)
        preview = ad.get("AD Preview")
        if preview and preview not in imgs: imgs.append(preview)

    return imgs, vids

# ── Normalize ad fields per platform ─────────────────────────────────────────
def normalize_ad(ad, platform):
    n = {}
    if platform == "meta":
        n["name"]   = ad.get("pageName", "Unknown")
        n["status"] = "ACTIVE" if ad.get("isActive") else "INACTIVE"
        n["date"]   = ad.get("startDate", "")
        n["body"]   = ad.get("bodyText", "")
        n["title"]  = ad.get("title", "")
        n["cta"]    = ad.get("ctaText", "")
        n["landing"]= ad.get("linkUrl", "#")
        n["lib_url"]= ad.get("adLibraryUrl", "#")
        n["plats"]  = " · ".join(p.replace("AUDIENCE_NETWORK","AN")
            .replace("FACEBOOK","FB").replace("INSTAGRAM","IG").replace("MESSENGER","Msg")
            for p in (ad.get("platforms") or []))
        n["variants"] = ad.get("collationCount", 0)
        n["impressions"] = ""

    elif platform == "google":
        n["name"]   = ad.get("advertiserName", "Unknown")
        first = ad.get("firstShown", "")
        last  = ad.get("lastShown", "")
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d")
            n["status"] = "ACTIVE" if (datetime.now() - last_dt).days <= 30 else "INACTIVE"
        except:
            n["status"] = "UNKNOWN"
        n["date"]   = f"{first} – {last}" if first and last else first or last
        variations  = ad.get("variations") or []
        fv          = variations[0] if variations else {}
        n["body"]   = fv.get("description", "")
        n["title"]  = fv.get("headline", "") or fv.get("title", "")
        n["cta"]    = fv.get("cta", "")
        n["landing"]= fv.get("clickUrl", "#")
        # Build Google Ads Transparency URL from IDs if adLibraryUrl not present
        adv_id  = ad.get("advertiserId", "")
        cre_id  = ad.get("creativeId", "")
        n["lib_url"] = (
            ad.get("adLibraryUrl") or
            (f"https://adstransparency.google.com/advertiser/{adv_id}/creative/{cre_id}"
             if adv_id and cre_id else None) or
            (f"https://adstransparency.google.com/?query={urlquote(n['name'])}"
             if n.get("name") else "#")
        )
        n["plats"]  = "Google"
        n["variants"] = len(variations)
        n["impressions"] = ""

    elif platform == "tiktok":
        # data_xplorer output: "AD ID", "Advertiser Name", "Ad Dates", "Ad Detail URL", etc.
        n["name"]   = (ad.get("Advertiser Name") or ad.get("advertiserName") or
                       ad.get("adv_name") or "Unknown")
        # Ad Dates: [{"FirstShown": "2025-01-01", ...}, {"LastShown": "2025-03-08", ...}]
        dates = ad.get("Ad Dates") or []
        first, last = "", ""
        for d in dates:
            if isinstance(d, dict):
                first = first or d.get("FirstShown", "")
                last  = last  or d.get("LastShown",  "")
        # fallback for older actor formats
        first = first or ad.get("firstShownDate") or ad.get("first_shown_date") or ""
        last  = last  or ad.get("lastShownDate")  or ad.get("last_shown_date")  or ""
        try:
            last_dt = datetime.strptime(last[:10], "%Y-%m-%d")
            n["status"] = "ACTIVE" if (datetime.now() - last_dt).days <= 30 else "INACTIVE"
        except:
            n["status"] = "ACTIVE"
        n["date"]   = f"{first[:10]} – {last[:10]}" if first and last else first[:10] if first else ""
        n["body"]   = ad.get("adText") or ad.get("ad_text") or ad.get("description") or ""
        n["title"]  = ad.get("adTitle") or ad.get("ad_title") or n["name"]
        n["cta"]    = ad.get("callToAction") or ad.get("call_to_action") or ""
        n["landing"]= ad.get("landingPageUrl") or ad.get("landing_page_url") or ad.get("clickUrl") or "#"
        ad_id   = ad.get("AD ID") or ad.get("adId") or ad.get("ad_id") or ""
        adv_id  = ad.get("advertiserId") or ad.get("adv_id") or ""
        lib_url = ad.get("Ad Detail URL") or ad.get("adLibraryUrl") or ad.get("ad_library_url") or ""
        n["lib_url"] = (
            lib_url or
            (f"https://library.tiktok.com/ads/detail/?ad_id={ad_id}" if ad_id else None) or
            (f"https://library.tiktok.com/ads?adv_biz_ids={adv_id}&query_type=2" if adv_id else None) or
            "#"
        )
        n["plats"]  = "TikTok"
        n["variants"] = 0
        # Audience: "100K-200K" string or impressions dict
        audience = ad.get("Ad Audience") or ""
        imp = ad.get("impressions") or {}
        if audience:
            n["impressions"] = audience
        elif isinstance(imp, dict):
            lo = imp.get("lowerBound", "") or imp.get("lower_bound", "")
            hi = imp.get("upperBound", "") or imp.get("upper_bound", "")
            n["impressions"] = f"{lo}–{hi}" if (lo and hi) else str(lo) if lo else ""
        else:
            n["impressions"] = ""
        # Targeting regions
        targeting = ad.get("Ad Targeting") or {}
        regions = targeting.get("regions") or ad.get("regionStats") or []
        if regions:
            n["plats"] = "TikTok · " + ", ".join(
                (r.get("region") or r.get("regionCode") or "") for r in regions[:4]
            )

    return n

# ── Scrape worker ─────────────────────────────────────────────────────────────
def run_job(job_id, platform, brand, country, searches, domain):
    job = jobs[job_id]
    def log(msg): job["log"].append(msg)

    all_ads = []

    # ── Meta ──────────────────────────────────────────────────────────────────
    if platform == "meta":
        actor = PLATFORMS["meta"]["actor"]
        for i, queries in enumerate(searches):
            log(f"🔍 Search {i+1}/{len(searches)}: {queries}")
            try:
                run    = api_post(f"acts/{actor}/runs",
                                  {"searchQueries": queries, "country": country, "maxAds": 60})
                run_id = run["data"]["id"]
                log(f"   Run started...")
                ads = wait_for_run(run_id, log)
                log(f"   ✓ {len(ads)} ads found")
                all_ads.extend(ads)
            except Exception as e:
                log(f"   ✗ Error: {e}")

    # ── Google ────────────────────────────────────────────────────────────────
    elif platform == "google":
        actor      = PLATFORMS["google"]["actor"]
        all_terms  = [q for queries in searches for q in queries]
        payload    = {"maxAds": 100}
        if all_terms:
            payload["searchTerms"] = all_terms
        if domain:
            payload["domains"] = [domain]
        if country:
            payload["region"] = country
        log(f"🔍 Google Ads search: terms={all_terms} domain={domain} region={country}")
        try:
            run    = api_post(f"acts/{actor}/runs", payload)
            run_id = run["data"]["id"]
            log(f"   Run started...")
            ads = wait_for_run(run_id, log)
            log(f"   ✓ {len(ads)} ads found")
            all_ads.extend(ads)
        except Exception as e:
            log(f"   ✗ Error: {e}")

    # ── TikTok ────────────────────────────────────────────────────────────────
    elif platform == "tiktok":
        actor = PLATFORMS["tiktok"]["actor"]
        # data_xplorer actor supports "all" or any country code globally
        tiktok_region = country if country else "all"
        keywords = [brand] + [q for queries in searches for q in queries]
        keywords = list(dict.fromkeys(kw for kw in keywords if kw))[:3]
        log(f"🔍 TikTok Ad Library: query={keywords} region={tiktok_region}")
        for kw in keywords:
            payload = {
                "query":        kw,
                "queryType":    "1",        # 1=keyword, 2=advertiser name/ID
                "region":       tiktok_region,
                "maxAds":       50,
                "fetchDetails": True,
                "proxyConfiguration": {"useApifyProxy": True},
            }
            try:
                run    = api_post(f"acts/{actor}/runs", payload)
                run_id = run["data"]["id"]
                log(f"   Run started for '{kw}'...")
                ads = wait_for_run(run_id, log)
                log(f"   ✓ {len(ads)} ads for '{kw}'")
                all_ads.extend(ads)
            except Exception as e:
                log(f"   ✗ Error for '{kw}': {e}")

    # ── Deduplicate ───────────────────────────────────────────────────────────
    seen, unique = set(), []
    for ad in all_ads:
        if platform == "meta":
            aid = ad.get("adArchiveId") or str(id(ad))
        elif platform == "google":
            aid = ad.get("creativeId") or ad.get("adLibraryUrl") or str(id(ad))
        elif platform == "tiktok":
            aid = ad.get("AD ID") or ad.get("adId") or str(id(ad))
        else:
            aid = str(id(ad))
        if aid not in seen:
            seen.add(aid)
            unique.append(ad)

    log(f"📊 {len(unique)} unique ads total")
    log("🖼️  Building viewer...")

    for ad in unique:
        imgs, vids = extract_urls(ad, platform)
        ad["_imgs_cdn"] = imgs
        ad["_vids_cdn"] = vids

    job["html"]   = build_viewer(job_id, brand, platform, country, unique)
    job["status"] = "done"
    log("✅ Done!")


# ── Viewer builder ────────────────────────────────────────────────────────────
def build_viewer(job_id, brand, platform, country, ads):
    COLOR  = PLATFORMS.get(platform, {}).get("color", "#1A3A5C")
    plabel = PLATFORMS.get(platform, {}).get("label", platform)

    # Count active/inactive
    active_count = 0
    for ad in ads:
        n = normalize_ad(ad, platform)
        if n.get("status") == "ACTIVE":
            active_count += 1

    def card(ad):
        n    = normalize_ad(ad, platform)
        imgs = ad.get("_imgs_cdn") or []
        vids = ad.get("_vids_cdn") or []

        # format badge
        if vids:
            fmt = "VIDEO"
        elif imgs:
            fmt = "IMAGE"
        elif platform == "google":
            fmt = ad.get("format", "TEXT")
        elif platform == "linkedin":
            fmt = ad.get("format", "").replace("SINGLE_IMAGE","IMAGE").replace("_"," ")
            if not fmt: fmt = "UNKNOWN"
        else:
            fmt = "UNKNOWN"

        status  = n["status"]
        lib_url = n["lib_url"] or "#"

        if vids:
            media = (f'<div class="media-wrap">'
                     f'<video controls preload="metadata" src="{vids[0]}" crossorigin="anonymous"></video>'
                     f'</div>')
        elif imgs:
            img_html = "".join(
                f'<img src="{img}" onclick="openFull(this.src)" crossorigin="anonymous">'
                for img in imgs[:4])
            media = (f'<div class="media-wrap img-grid img-count-{min(len(imgs),4)}">'
                     f'{img_html}</div>')
        else:
            icon = "🎬" if fmt == "VIDEO" else ("📝" if fmt in ("TEXT","") else "🖼️")
            media = (f'<div class="media-placeholder">'
                     f'<span>{icon}</span>'
                     f'<a href="{lib_url}" target="_blank">View in Ad Library →</a>'
                     f'</div>')

        body  = (n["body"] or "").replace('"', '&quot;').replace('\n', '<br>')
        title = (n["title"] or "").replace('"', '&quot;')
        cta   = n["cta"] or ""
        lp    = n["landing"] or "#"
        try:
            lp_host = urlparse(lp).netloc or lp
        except:
            lp_host = lp

        imp_badge = (f'<span class="badge imp">👁 {n["impressions"]}</span>'
                     if n.get("impressions") else "")
        var_badge = (f'<span class="badge hot">🔥 {n["variants"]} variants</span>'
                     if n.get("variants", 0) > 2 else "")

        return f'''<div class="card" data-status="{status}" data-fmt="{fmt}">
  <div class="card-header">
    <div class="card-name">{n["name"]}</div>
    <div class="card-meta">{n["date"]} · {n["plats"]}</div>
    <div class="badge-row">
      <span class="badge {'active' if status=='ACTIVE' else ('inactive' if status=='INACTIVE' else 'unknown')}">{status}</span>
      <span class="badge fmt">{fmt}</span>
      {imp_badge}{var_badge}
    </div>
  </div>
  {media}
  <div class="card-body">
    {f'<div class="ad-title">{title}</div>' if title else ""}
    <div class="ad-copy">{body or "<em style='color:#aaa'>No copy text</em>"}</div>
  </div>
  <div class="card-footer">
    <span class="cta-pill">{cta}</span>
    <a href="{lp}" target="_blank" class="lp-link">{lp_host}</a>
    <a href="{lib_url}" target="_blank" class="lib-link">Ad Library ↗</a>
  </div>
</div>'''

    cards = "\n".join(card(a) for a in ads)

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>{brand} — {plabel}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:Arial,sans-serif;background:#f0f2f5}}
header{{background:{COLOR};color:white;padding:16px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}}
header h1{{font-size:18px}}header a{{color:rgba(255,255,255,.7);font-size:12px;text-decoration:none}}
header a:hover{{color:white}}
.plat-tag{{background:rgba(255,255,255,.2);padding:3px 10px;border-radius:10px;font-size:11px;font-weight:bold}}
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
.badge.unknown{{background:#e2e3e5;color:#383d41}}
.badge.fmt{{background:#e2e3e5;color:#383d41}}.badge.hot{{background:#fff3cd;color:#856404}}
.badge.imp{{background:#cce5ff;color:#004085}}
.media-wrap{{background:#000;max-height:300px;overflow:hidden}}
.media-wrap video{{width:100%;max-height:300px;object-fit:contain;display:block}}
.img-grid{{display:grid;background:#f7f8fa}}
.img-count-1{{grid-template-columns:1fr}}.img-count-2,.img-count-3,.img-count-4{{grid-template-columns:1fr 1fr}}
.img-grid img{{width:100%;height:150px;object-fit:cover;cursor:zoom-in;border:1px solid #eee}}
.media-placeholder{{background:#f7f8fa;min-height:140px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:#999}}
.media-placeholder span{{font-size:32px}}.media-placeholder a{{color:{COLOR};font-size:13px;font-weight:bold;text-decoration:none}}
.card-body{{padding:10px 12px;flex:1}}.ad-title{{font-weight:bold;font-size:13px;margin-bottom:4px}}
.ad-copy{{font-size:12px;color:#555;line-height:1.5;max-height:72px;overflow:hidden;transition:max-height .3s}}
.ad-copy.open{{max-height:500px}}.toggle-copy{{color:{COLOR};font-size:11px;font-weight:bold;cursor:pointer;margin-top:4px;display:inline-block}}
.card-footer{{padding:8px 12px;border-top:1px solid #f0f0f0;display:flex;gap:6px;align-items:center}}
.cta-pill{{background:{COLOR};color:white;font-size:10px;font-weight:bold;padding:2px 8px;border-radius:10px;white-space:nowrap}}
.lp-link{{font-size:11px;color:{COLOR};text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}
.lib-link{{font-size:11px;color:#888;text-decoration:none;white-space:nowrap}}
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:999;align-items:center;justify-content:center;cursor:zoom-out}}
#lb.open{{display:flex}}#lb img{{max-width:92vw;max-height:92vh;border-radius:8px}}
</style></head><body>
<header>
  <div>
    <h1>{brand} — Ad Intelligence</h1>
    <span class="plat-tag">{plabel}</span>
    <a href="/" style="margin-top:6px;display:block">← New scrape</a>
  </div>
  <div class="stats">
    <div class="stat"><strong>{len(ads)}</strong>total</div>
    <div class="stat"><strong>{active_count}</strong>active</div>
  </div>
</header>
<div class="filters">
  <button class="fbtn on" onclick="filter('all',this)">All</button>
  <button class="fbtn" onclick="filter('ACTIVE',this)">Active Only</button>
  <button class="fbtn" onclick="filter('INACTIVE',this)">Inactive</button>
  <button class="fbtn" onclick="filter('VIDEO',this)">📹 Video</button>
  <button class="fbtn" onclick="filter('IMAGE',this)">🖼 Image</button>
  <span class="fcount" id="fc">{len(ads)} ads</span>
</div>
<div class="grid" id="grid">{cards}</div>
<div id="lb" onclick="this.classList.remove('open')"><img id="lbi" src=""></div>
<script>
function filter(f,btn){{document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');let n=0;document.querySelectorAll('.card').forEach(c=>{{let s=f==='all'||(f==='ACTIVE'&&c.dataset.status==='ACTIVE')||(f==='INACTIVE'&&c.dataset.status==='INACTIVE')||(f==='VIDEO'&&c.dataset.fmt==='VIDEO')||(f==='IMAGE'&&c.dataset.fmt==='IMAGE');c.style.display=s?'':'none';if(s)n++}});document.getElementById('fc').textContent=n+' ads'}}
function openFull(s){{document.getElementById('lbi').src=s;document.getElementById('lb').classList.add('open')}}
document.querySelectorAll('.ad-copy').forEach(el=>{{if(el.scrollHeight>el.clientHeight+5){{const t=document.createElement('span');t.className='toggle-copy';t.textContent='Read more ▼';t.onclick=()=>{{el.classList.toggle('open');t.textContent=el.classList.contains('open')?'Show less ▲':'Read more ▼'}};el.after(t)}}}})</script>
</body></html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

PLATFORM_OPTIONS = "\n".join(
    f'<option value="{k}">{v["label"]}</option>'
    for k, v in PLATFORMS.items()
)
COUNTRY_OPTIONS = "\n".join(
    f'<option value="{c}">{"🌍 All Regions" if c == "" else c}</option>'
    for c in COUNTRIES
)

HOME = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Ad Intel Scraper</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:white;border-radius:14px;padding:36px 40px;width:540px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
h1{font-size:22px;color:#1a1a1a;margin-bottom:4px}p.sub{font-size:13px;color:#888;margin-bottom:24px}
label{display:block;font-size:12px;font-weight:bold;color:#555;margin-bottom:5px;margin-top:16px}
input,select{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:7px;font-size:14px;outline:none}
input:focus,select:focus{border-color:#1877f2}
.platform-select{border:2px solid #1877f2;font-weight:bold}
.search-row{display:flex;gap:6px;margin-bottom:6px}
.search-row input{flex:1}
.remove-btn{background:#fee;border:1px solid #fcc;color:#c00;border-radius:6px;padding:0 10px;cursor:pointer;font-size:16px;flex-shrink:0}
.add-btn{background:none;border:1px dashed #bbb;color:#888;border-radius:7px;padding:7px;width:100%;cursor:pointer;font-size:13px;margin-top:4px}
.add-btn:hover{border-color:#1877f2;color:#1877f2}
.submit-btn{background:#1877f2;color:white;border:none;border-radius:8px;padding:12px;width:100%;font-size:15px;font-weight:bold;cursor:pointer;margin-top:24px}
.submit-btn:hover{background:#166fe5}
.hint{font-size:11px;color:#aaa;margin-top:4px}
.platform-hint{font-size:11px;color:#888;margin-top:6px;padding:8px 10px;background:#f8f9ff;border-radius:6px;border-left:3px solid #1877f2;display:none}
.platform-hint.show{display:block}
</style></head><body>
<div class="card">
  <h1>Ad Intelligence Scraper</h1>
  <p class="sub">Scrape ad libraries across Meta, Google, TikTok and LinkedIn</p>
  <form method="POST" action="/start">
    <label>Platform</label>
    <select name="platform" class="platform-select" onchange="updatePlatform(this.value)">
      PLATFORM_OPTIONS
    </select>
    <div id="platform-hint" class="platform-hint"></div>
    <label>Brand Name</label>
    <input name="brand" placeholder="e.g. NovaBurn" required>
    <label>Brand Website <span style="font-weight:normal;color:#aaa">(optional)</span></label>
    <input name="domain" placeholder="e.g. https://get-novaburn.com">
    <label>Country</label>
    <select name="country">
      COUNTRY_OPTIONS
    </select>
    <label>Search Terms <span style="font-weight:normal;color:#aaa">(one group per row, comma-separated)</span></label>
    <div id="searches">
      <div class="search-row"><input name="search[]" placeholder="e.g. brand name, slogan"><button type="button" class="remove-btn" onclick="removeRow(this)">×</button></div>
    </div>
    <button type="button" class="add-btn" onclick="addRow()">+ Add search group</button>
    <p class="hint">Tip: brand name + domain give the best coverage</p>
    <button type="submit" class="submit-btn">Start Scrape →</button>
  </form>
</div>
<script>
const HINTS = {
  meta: 'Searches Meta Ad Library (Facebook & Instagram). Uses keyword search — works best with brand name and domain.',
  google: 'Searches Google Ads Transparency Center by brand name and domain. Country filters by region shown.',
  tiktok: 'Searches TikTok Ad Library by advertiser name. Country filters which region\'s ads to show.'
};
function updatePlatform(v){
  const h=document.getElementById('platform-hint');
  h.textContent=HINTS[v]||'';
  h.className='platform-hint'+(HINTS[v]?' show':'');
}
function addRow(){const d=document.getElementById('searches');const r=document.createElement('div');r.className='search-row';r.innerHTML='<input name="search[]" placeholder="Search terms..."><button type="button" class="remove-btn" onclick="removeRow(this)">×</button>';d.appendChild(r)}
function removeRow(b){if(document.querySelectorAll('.search-row').length>1)b.parentElement.remove()}
updatePlatform(document.querySelector('[name=platform]').value);
</script></body></html>"""


@app.route("/")
def home():
    return (HOME
            .replace("PLATFORM_OPTIONS", PLATFORM_OPTIONS)
            .replace("COUNTRY_OPTIONS", COUNTRY_OPTIONS))


@app.route("/start", methods=["POST"])
def start():
    platform     = request.form.get("platform", "meta")
    brand        = request.form.get("brand", "Brand").strip()
    country      = request.form.get("country", "GB")
    domain_input = request.form.get("domain", "").strip()
    searches_raw = request.form.getlist("search[]")
    searches     = [[q.strip() for q in s.split(",") if q.strip()] for s in searches_raw if s.strip()]

    # Extract clean domain
    domain = ""
    if domain_input:
        parsed = urlparse(domain_input if "://" in domain_input else "https://" + domain_input)
        domain = parsed.netloc

    # For Meta only: prepend domain as first search group
    if platform == "meta" and domain and [domain] not in searches:
        searches.insert(0, [domain])

    # If no searches at all, use brand name
    if not searches:
        searches = [[brand]]

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "log":    [],
        "html":   None,
        "config": {"brand": brand, "platform": platform}
    }

    threading.Thread(
        target=run_job,
        args=(job_id, platform, brand, country, searches, domain),
        daemon=True
    ).start()

    plabel = PLATFORMS.get(platform, {}).get("label", platform)
    return render_template_string(PROGRESS_HTML, job_id=job_id, brand=brand, plabel=plabel)


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


PROGRESS_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Scraping {{ brand }}…</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:white;border-radius:14px;padding:36px 40px;width:520px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
h1{font-size:20px;color:#1a1a1a;margin-bottom:4px}
.plat{font-size:12px;color:#888;margin-bottom:18px}
#log{font-family:monospace;font-size:13px;line-height:1.8;color:#333;min-height:200px;max-height:340px;overflow-y:auto;background:#f8f8f8;border-radius:8px;padding:14px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #ddd;border-top-color:#1877f2;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
#open-btn{display:none;background:#1877f2;color:white;border:none;border-radius:8px;padding:12px;width:100%;font-size:15px;font-weight:bold;cursor:pointer;margin-top:20px}
#open-btn:hover{background:#166fe5}
#new-btn{display:none;background:#f0f2f5;color:#555;border:none;border-radius:8px;padding:10px;width:100%;font-size:13px;cursor:pointer;margin-top:8px}
</style></head><body>
<div class="card">
  <h1><span class="spinner" id="spin"></span>Scraping {{ brand }}…</h1>
  <div class="plat">{{ plabel }}</div>
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
