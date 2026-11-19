#!/usr/bin/env python3
"""Daily AI-job finder for freshers. Pulls from company ATS APIs (+ Adzuna),
filters by role/location/seniority, shows only NEW jobs since last run.

Usage:  python job_finder.py            (normal run)
        python job_finder.py --all      (ignore 'seen' history, show everything matching)
Optional env: ADZUNA_APP_ID, ADZUNA_APP_KEY (free at developer.adzuna.com)
              TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (phone alerts)
"""
import csv, json, os, re, sys, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(HERE, "seen.json")
UA = {"User-Agent": "Mozilla/5.0 (job-finder personal script)"}

ROLE_KW = ["ai engineer", "ai/ml", "ml engineer", "machine learning", "genai", "gen ai",
           "generative", "llm", "nlp", "computer vision", "data scientist", "data science",
           "applied scientist", "applied ai", "forward deployed", "deployment engineer",
           "ai developer", "ai software", "software engineer - ai", "software engineer, ai",
           "ai research", "research engineer", "prompt", "agent", "mlops", "deep learning",
           "associate engineer", "software engineer"]
# generic "software engineer" is kept only if description/title also hints at AI (see is_relevant)
AI_HINT = ["ai", "ml", "machine learning", "llm", "genai", "nlp", "vision", "data", "agent"]
GENERIC = ["associate engineer", "software engineer"]
EXCLUDE = ["senior", "sr.", "sr ", "staff", "principal", "lead", "manager", "director", "head",
           "vp ", "architect", "iii", "iv", "ii ", " 2", "sde 2", "sde-2", "sde ii", "consultant"]
LOCS = ["hyderabad", "bangalore", "bengaluru", "mumbai", "gurgaon", "gurugram", "india", "remote"]
FRESHER_BOOST = ["intern", "graduate", "fresher", "entry", "junior", "associate", "trainee",
                 "new grad", "early career", "campus", "0-1", "0-2", "apprentice"]

def is_relevant(title, loc):
    t, l = title.lower(), (loc or "").lower()
    if not any(x in l for x in LOCS) and l != "":
        return False
    if any(x in t for x in EXCLUDE):
        return False
    if any(k in t for k in ROLE_KW if k not in GENERIC):
        return True
    if any(k in t for k in GENERIC):
        return any(re.search(r"\b" + h + r"\b", t) for h in AI_HINT)
    return False

def score(title):
    t = title.lower()
    return 2 if any(b in t for b in FRESHER_BOOST) else 0

def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=20, **kw)
    r.raise_for_status()
    return r.json()

def fetch_greenhouse(slug):
    for j in get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")["jobs"]:
        yield dict(company=slug, title=j["title"], location=(j.get("location") or {}).get("name", ""),
                   url=j["absolute_url"], posted=(j.get("updated_at") or "")[:10])

def fetch_lever(slug):
    for j in get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}):
        ts = j.get("createdAt")
        d = dt.datetime.fromtimestamp(ts/1000).strftime("%Y-%m-%d") if ts else ""
        yield dict(company=slug, title=j["text"], location=(j.get("categories") or {}).get("location", "") or "",
                   url=j["hostedUrl"], posted=d)

def fetch_ashby(slug):
    for j in get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")["jobs"]:
        yield dict(company=slug, title=j["title"], location=j.get("location", "") or "",
                   url=j.get("jobUrl", ""), posted=(j.get("publishedAt") or "")[:10])

FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

def fetch_company(line):
    ats, slug = line.split(":", 1)
    try:
        return line, list(FETCHERS[ats](slug)), None
    except Exception as e:
        return line, [], str(e)[:60]

def fetch_adzuna():
    aid, key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (aid and key):
        return []
    out = []
    for what in ["ai engineer", "machine learning engineer", "data scientist", "generative ai",
                 "llm engineer", "python developer ai", "forward deployed engineer"]:
        for city in ["hyderabad", "bangalore", "mumbai", "gurgaon"]:
            try:
                d = get("https://api.adzuna.com/v1/api/jobs/in/search/1",
                        params=dict(app_id=aid, app_key=key, what=what, where=city,
                                    results_per_page=50, max_days_old=7, sort_by="date"))
                for j in d.get("results", []):
                    out.append(dict(company=(j.get("company") or {}).get("display_name", ""),
                                    title=j["title"], location=(j.get("location") or {}).get("display_name", city),
                                    url=j["redirect_url"], posted=(j.get("created") or "")[:10]))
            except Exception as e:
                print(f"  adzuna {what}/{city}: {e}")
    return out

def notify(rows):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat) or not rows:
        return
    msg = f"{len(rows)} new AI jobs\n\n" + "\n\n".join(f"{r['title']} - {r['company']} ({r['location']})\n{r['url']}" for r in rows[:15])
    requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": msg[:4000]}, timeout=20)

def main():
    show_all = "--all" in sys.argv
    lines = [l.strip() for l in open(os.path.join(HERE, "companies.txt")) if l.strip() and not l.startswith("#")]
    seen = set(json.load(open(SEEN_FILE))) if os.path.exists(SEEN_FILE) else set()
    jobs, dead = [], []
    with ThreadPoolExecutor(8) as ex:
        for line, res, err in ex.map(fetch_company, lines):
            if err: dead.append(f"{line} ({err})")
            jobs += res
    jobs += fetch_adzuna()

    matched = {}
    for j in jobs:
        if j["url"] and is_relevant(j["title"], j["location"]):
            j["score"] = score(j["title"])
            matched[j["url"]] = j
    new = [j for u, j in matched.items() if show_all or u not in seen]
    new.sort(key=lambda j: (-j["score"], j["posted"]), reverse=False)
    new.sort(key=lambda j: j["score"], reverse=True)

    print(f"Scanned {len(lines)} boards, {len(jobs)} postings, {len(matched)} relevant, {len(new)} new.")
    if dead: print("Skipped (bad slug/blocked):", "; ".join(dead))
    if new:
        fn = os.path.join(HERE, f"new_jobs_{dt.date.today()}.csv")
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["score", "title", "company", "location", "posted", "url"])
            w.writeheader(); [w.writerow({k: j.get(k, "") for k in w.fieldnames}) for j in new]
        print(f"Saved {fn}\n")
        for j in new[:40]:
            print(f"{'*' if j['score'] else ' '} {j['title']} | {j['company']} | {j['location']} | {j['posted']}\n    {j['url']}")
        notify(new)
    json.dump(sorted(seen | set(matched)), open(SEEN_FILE, "w"))

if __name__ == "__main__":
    main()
