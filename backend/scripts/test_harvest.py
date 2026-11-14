import urllib.request
import json
import time

repos = [
    "psf/requests",
    "pallets/click",
    "marshmallow-code/marshmallow",
    "python-attrs/attrs",
    "pytest-dev/pluggy",
    "pytest-dev/pytest",
    "pallets/flask",
    "urllib3/urllib3",
]

total_found = 0
all_prs = []

for r in repos:
    url = f"https://api.github.com/search/issues?q=repo:{r}+is:pr+is:merged+revert&per_page=20"
    req = urllib.request.Request(url, headers={"User-Agent": "TestPilot-Harvester"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            print(f"[{r}] Total in repo: {data.get('total_count')}, fetched {len(items)}")
            for it in items:
                all_prs.append({
                    "repo": r,
                    "number": it["number"],
                    "title": it["title"],
                    "html_url": it["html_url"],
                    "pull_request_url": it.get("pull_request", {}).get("url")
                })
                total_found += 1
    except Exception as e:
        print(f"[{r}] Error: {e}")
    time.sleep(1.5)

print(f"\nTotal real merged Revert PRs fetched: {len(all_prs)}")
with open(r"F:\TestPilot-AI\backend\scripts\harvested_revert_prs.json", "w", encoding="utf-8") as f:
    json.dump(all_prs, f, indent=2, ensure_ascii=False)
print("Saved to harvested_revert_prs.json")
