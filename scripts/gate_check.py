import os
import sys
import time
import subprocess
import json

# Append current directory to path so identify_issue can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from identify_issue import identify_issue

def get_check_runs(repo, sha):
    cmd = ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error calling GitHub API: {res.stderr}", file=sys.stderr)
        return None
    try:
        return json.loads(res.stdout)
    except Exception as e:
        print(f"Error parsing JSON from GitHub API: {e}", file=sys.stderr)
        return None

def main():
    print("Starting Issue Gating Check...")
    
    # 1. Identify target issue
    try:
        target_issue = identify_issue()
    except SystemExit:
        print("Gating failed: Issue identification failed.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Detected Target Issue: #{target_issue}")
    
    # 2. Get HEAD_SHA and Repository
    head_sha = os.getenv("HEAD_SHA")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not head_sha or not repo:
        print("Error: HEAD_SHA and GITHUB_REPOSITORY environment variables must be set.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Target Commit SHA: {head_sha}")
    print(f"Repository: {repo}")
    
    # 3. Define Required Checks
    required_checks = [
        "Node Application Build & Test",
        "Backend Tests",
        "Database Integrity",
        "Full Application Integration",
        "Dependency Security",
        f"Issue {target_issue} Evaluator"
    ]
    
    print("Waiting for the following required checks to complete successfully:")
    for check in required_checks:
        print(f"  - {check}")
        
    # 4. Polling loop
    poll_interval = 20
    max_duration = 900  # 15 minutes
    elapsed = 0
    
    while elapsed < max_duration:
        check_runs_data = get_check_runs(repo, head_sha)
        if not check_runs_data:
            print("Failed to fetch check runs. Retrying in next cycle...")
            time.sleep(poll_interval)
            elapsed += poll_interval
            continue
            
        runs = check_runs_data.get("check_runs", [])
        
        # Build a mapping of check run name to its status/conclusion
        runs_by_name = {}
        for r in runs:
            name = r.get("name")
            status = r.get("status")
            conclusion = r.get("conclusion")
            # Skip the gate check itself
            if name == "Issue Evaluator Gate":
                continue
            if name not in runs_by_name:
                runs_by_name[name] = []
            runs_by_name[name].append({"status": status, "conclusion": conclusion})
            
        all_passed = True
        pending_checks = []
        failed_checks = []
        
        for check in required_checks:
            if check not in runs_by_name:
                pending_checks.append(check)
                all_passed = False
                continue
                
            runs_for_check = runs_by_name[check]
            
            # Check if any run for this check succeeded
            succeeded = any(r["status"] == "completed" and r["conclusion"] == "success" for r in runs_for_check)
            if succeeded:
                continue
                
            # If not succeeded, check if all runs for this check are completed with failures
            all_completed = all(r["status"] == "completed" for r in runs_for_check)
            any_failed = any(r["status"] == "completed" and r["conclusion"] in ["failure", "cancelled", "timed_out", "action_required"] for r in runs_for_check)
            
            if any_failed and all_completed:
                failed_checks.append((check, runs_for_check[0]["conclusion"]))
                all_passed = False
            else:
                pending_checks.append(check)
                all_passed = False
                
        if failed_checks:
            print("\nGating failed! The following required checks failed:")
            for check, conclusion in failed_checks:
                print(f"  - {check}: {conclusion}")
            sys.exit(1)
            
        if all_passed:
            print("\nAll required checks passed successfully!")
            sys.exit(0)
            
        print(f"\n[{elapsed}s / {max_duration}s elapsed] Pending checks: {', '.join(pending_checks)}")
        time.sleep(poll_interval)
        elapsed += poll_interval
        
    print("\nTimeout waiting for required checks to complete.", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
