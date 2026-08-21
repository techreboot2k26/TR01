import os
import sys
import subprocess

# Append current directory to path so identify_issue can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from identify_issue import identify_issue

def trigger_workflow(issue_num, ref):
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        repo = "techreboot2k26/TR01"
        
    workflow_file = f"issue{issue_num}-evaluator.yml"
    
    # Trigger workflow via gh API
    cmd = [
        "gh", "api",
        "-X", "POST",
        f"repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        "-F", f"ref={ref}"
    ]
    print(f"Triggering workflow: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error triggering workflow: {res.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Successfully triggered Issue {issue_num} Evaluator workflow.")

def main():
    try:
        target_issue = identify_issue()
    except SystemExit:
        print("Trigger failed: Issue identification failed.", file=sys.stderr)
        sys.exit(1)
        
    ref = os.getenv("PR_HEAD_REF")
    if not ref:
        ref = os.getenv("BRANCH_NAME")
    if not ref:
        ref = "main"
        
    print(f"Triggering evaluator for Issue #{target_issue} on ref {ref}")
    trigger_workflow(target_issue, ref)

if __name__ == "__main__":
    main()
