import os
import sys
import re

def identify_issue():
    # Read variables from environment
    title = os.getenv("PR_TITLE", "").strip()
    body = os.getenv("PR_BODY", "").strip()
    commit_msg = os.getenv("COMMIT_MSG", "").strip()
    branch = os.getenv("BRANCH_NAME", "").strip()

    # Regex patterns (restricted to range [4, 10])
    # Closing patterns: fixes #N, closes #N, resolves #N
    close_pattern = re.compile(
        r'\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*[:\-]?\s*#\s*([4-9]|10)\b',
        re.IGNORECASE
    )
    # Reference patterns: #N, issue N, issue #N
    ref_pattern = re.compile(
        r'\b(?:issue\s*#?|#)\s*([4-9]|10)\b',
        re.IGNORECASE
    )
    # Branch patterns: issue-4, feature/4, feature/issue-4, feature/issue4
    branch_pattern = re.compile(
        r'\b(?:issue|feature)?[-/_]?([4-9]|10)\b',
        re.IGNORECASE
    )

    # Source 1: PR body closing keywords
    if body:
        closing_matches = set(int(m) for m in close_pattern.findall(body))
        if len(closing_matches) == 1:
            return list(closing_matches)[0]
        elif len(closing_matches) > 1:
            print(f"Error: Multiple targeted issues in PR body closing keywords: {closing_matches}", file=sys.stderr)
            sys.exit(1)

    # Source 2: PR title or body explicit reference
    ref_matches = set()
    if title:
        ref_matches.update(int(m) for m in ref_pattern.findall(title))
    if body:
        ref_matches.update(int(m) for m in ref_pattern.findall(body))

    if len(ref_matches) == 1:
        return list(ref_matches)[0]
    elif len(ref_matches) > 1:
        print(f"Error: Multiple targeted issues in PR title/body references: {ref_matches}", file=sys.stderr)
        sys.exit(1)

    # Source 3: Commit message closing or reference
    if commit_msg:
        commit_matches = set(int(m) for m in close_pattern.findall(commit_msg))
        if not commit_matches:
            commit_matches = set(int(m) for m in ref_pattern.findall(commit_msg))
        if len(commit_matches) == 1:
            return list(commit_matches)[0]
        elif len(commit_matches) > 1:
            print(f"Error: Multiple targeted issues in commit message: {commit_matches}", file=sys.stderr)
            sys.exit(1)

    # Source 4: Branch name
    if branch:
        branch_matches = set(int(m) for m in branch_pattern.findall(branch))
        if len(branch_matches) == 1:
            return list(branch_matches)[0]
        elif len(branch_matches) > 1:
            print(f"Error: Multiple targeted issues in branch name: {branch_matches}", file=sys.stderr)
            sys.exit(1)

    print("Error: No Issue #4–10 could be determined.", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    issue_num = identify_issue()
    print(issue_num)
