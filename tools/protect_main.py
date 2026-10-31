"""Reviewable main protection plan; --apply requires actual repository admin rights."""
import argparse
import json
import subprocess

REPO = 'BAITC-Hacks/hack-a4a36997-oguzki'
POLICY = {
    'required_status_checks': {'strict': True, 'contexts': ['security-gate']},
    'enforce_admins': True,
    'required_pull_request_reviews': {'dismiss_stale_reviews': True, 'require_code_owner_reviews': False,
                                    'required_approving_review_count': 1, 'require_last_push_approval': True},
    'restrictions': None,
    'required_linear_history': True, 'allow_force_pushes': False, 'allow_deletions': False,
    'required_conversation_resolution': True,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({'repository': REPO, 'branch': 'main', 'policy': POLICY}, indent=2))
        return 0
    result = subprocess.run(['gh', 'api', 'repos/' + REPO], capture_output=True, text=True, check=True)
    if not json.loads(result.stdout).get('permissions', {}).get('admin'):
        print('Not applied: authenticated account is not a repository administrator.')
        return 2
    # Do not install an impossible status gate before this job exists and has passed.
    runs = subprocess.run(['gh', 'api', f'repos/{REPO}/actions/workflows/security.yml/runs?status=success&per_page=1'],
                          capture_output=True, text=True, check=True)
    if not json.loads(runs.stdout).get('workflow_runs'):
        print('Not applied: merge/enable the security workflow and obtain a successful run first.')
        return 2
    subprocess.run(['gh', 'api', '--method', 'PUT', f'repos/{REPO}/branches/main/protection', '--input', '-'],
                   input=json.dumps(POLICY), text=True, check=True, stdout=subprocess.DEVNULL)
    verified = subprocess.run(['gh', 'api', f'repos/{REPO}/branches/main/protection'],
                              capture_output=True, text=True, check=True)
    current = json.loads(verified.stdout)
    assert current['enforce_admins']['enabled']
    assert current['required_pull_request_reviews']['required_approving_review_count'] >= 1
    assert 'security-gate' in current['required_status_checks']['contexts']
    assert not current['allow_force_pushes']['enabled'] and not current['allow_deletions']['enabled']
    print('Verified: main requires review and security-gate; admin enforcement on; force push/deletion off.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
