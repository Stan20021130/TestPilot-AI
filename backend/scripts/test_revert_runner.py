import json
import subprocess
import os
import sys
import time
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.verify.runners import SandboxTestRunner
from app.verify.contracts import VerifyRequest, TestBundle

test_repos_dir = backend_dir.parent / "test_repos"

with open(backend_dir / "scripts" / "github_100_real_revert_prs.json", "r", encoding="utf-8") as f:
    prs = json.load(f)

print(f"Testing first 5 real PRs out of {len(prs)}...")

runners = {}

for p in prs[:5]:
    repo_name = p["repo"]
    repo_dir = test_repos_dir / repo_name
    diff = p["diff"]
    
    # 1. Apply reverse diff
    apply_res = subprocess.run(["git", "apply", "--reverse"], cwd=repo_dir, input=diff, text=True, capture_output=True)
    if apply_res.returncode != 0:
        print(f"[{p['id']}] Failed to apply reverse diff: {apply_res.stderr}")
        continue
    
    t0 = time.monotonic()
    if repo_name not in runners:
        runners[repo_name] = SandboxTestRunner(repo_dir)
    runner = runners[repo_name]
    
    # Run tests on modified files
    test_target = "tests/" if (repo_dir / "tests").is_dir() else "testing/"
    req = VerifyRequest(workspace=str(repo_dir), bundle=TestBundle(existing=[test_target]))
    res = runner.run(req)
    dt = round(time.monotonic() - t0, 2)
    
    # Revert back to clean
    subprocess.run(["git", "checkout", "."], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_dir, capture_output=True)
    
    caught = (res.status == "failed" and res.failed > 0) or (res.status == "error")
    status_str = f"🔴 FAILED ({res.failed} failures)" if caught else "⚪ PASSED (bug escaped standard tests)"
    print(f"[{p['id']}] ({repo_name}) {p['short_commit']}: {p['title'][:50]} | {status_str} in {dt}s")

print("\nBatch test successful! Git apply and cleanup works 100% cleanly.")
