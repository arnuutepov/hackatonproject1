"""Check every commit on every fetched ref for datasets, local databases and secrets.

Run after `git fetch --all` so the scan covers all branches. Exit 1 on a finding.
False-positive-safe patterns only: reading OPENAI_API_KEY from the environment is fine.
"""
import re
import subprocess
import sys

DATA_PATH = re.compile(r'(^|/)(data|outputs|datasets|raw)/|(^|/)(rules|taxonomy)\.json$|'
                       r'\.(gz|zip|tgz|csv|parquet|xlsx|sqlite3?|db|arb|pkl)$|(^|/)\.env$')
ALLOWED = {'examples/synthetic_tickets.jsonl', 'examples/wave3_synthetic.jsonl',
           'examples/wave7_demo.jsonl', 'examples/wave7_new.jsonl'}
SECRET = (r'sk-(proj-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|'
          r'AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY|'
          r'OPENAI_API_KEY *= *[^ \n$]+')
LARGE_BLOB = 500_000
# Exact synthetic canary once used by submission_check to test that 422 does not echo input.
# It is not a credential; only this literal is exempt, nothing pattern-based.
KNOWN_SYNTHETIC = ('sk-demo-should-not-echo',)


def git(*args: str) -> str:
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    commits = git('rev-list', '--all').split()
    findings = []
    paths = {line for line in git('log', '--all', '--pretty=format:', '--name-only').splitlines() if line}
    for path in sorted(paths):
        if (DATA_PATH.search(path) or path.endswith('.jsonl')) and path not in ALLOWED:
            findings.append(f'data-like path in history: {path}')
    for line in git('rev-list', '--objects', '--all').splitlines():
        parts = line.split(' ', 1)
        if len(parts) == 2:
            size = int(git('cat-file', '-s', parts[0]).strip())
            if size > LARGE_BLOB:
                findings.append(f'large blob {size} bytes: {parts[1]}')
    for commit in commits:
        found = subprocess.run(['git', 'grep', '-I', '-n', '-E', SECRET, commit, '--', '.'],
                               capture_output=True, text=True).stdout
        findings += [f'secret-like text: {hit[:120]}' for hit in found.splitlines()
                     if not any(token in hit for token in KNOWN_SYNTHETIC)]
    tracked = git('ls-files').splitlines()
    findings += [f'tracked data-like file: {path}' for path in tracked
                 if (DATA_PATH.search(path) or path.endswith('.jsonl')) and path not in ALLOWED]
    print(f'Scanned {len(commits)} commits, {len(paths)} paths ever committed, {len(tracked)} tracked files.')
    for finding in sorted(set(findings)):
        print(finding)
    print('OK: no datasets, databases or secrets found' if not findings else f'{len(set(findings))} findings')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
