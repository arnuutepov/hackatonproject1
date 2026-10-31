from pathlib import Path
import yaml
from tools.deploy_preflight import settings, validate
from tools.protect_main import POLICY

ROOT = Path(__file__).resolve().parents[1]


def test_template_cannot_accidentally_deploy_or_claim_public_tls():
    errors = validate(settings(ROOT / 'deploy/settings.env.example'))
    assert errors and any('PUBLIC_DOMAIN' in error for error in errors)


def test_only_tls_edge_publishes_ports_and_password_bypass_is_absent():
    config = yaml.safe_load((ROOT/'deploy/compose.yml').read_text())
    assert 'ports' not in config['services']['app'] and 'ports' not in config['services']['sso']
    assert config['services']['edge']['ports'] == ['80:80', '443:443']
    app = config['services']['app']['environment']
    assert app['COPILOT_AUTH_MODE'] == 'oidc'
    assert 'COPILOT_INSECURE_LOCAL_HTTP' not in app
    assert 'COPILOT_TRIAGE_TOKEN' not in app
    flags = config['services']['sso']['command']
    assert '--code-challenge-method=S256' in flags
    assert '--insecure-oidc-skip-nonce=false' in flags
    assert '--cookie-secure=true' in flags
    assert '--cookie-samesite=lax' in flags  # IdP top-level redirect callback must carry its CSRF cookie.


def test_branch_policy_requires_review_checks_and_no_force_push():
    assert POLICY['enforce_admins']
    assert POLICY['required_pull_request_reviews']['required_approving_review_count'] >= 1
    assert POLICY['required_pull_request_reviews']['require_last_push_approval']
    assert POLICY['required_status_checks']['contexts'] == ['security-gate']
    assert not POLICY['allow_force_pushes'] and not POLICY['allow_deletions']


def test_workflow_is_read_only_and_never_uses_pull_request_target():
    text = (ROOT/'.github/workflows/security.yml').read_text()
    assert 'pull_request_target' not in text
    assert 'contents: read' in text and 'persist-credentials: false' in text
    assert 'security-gate:' in text
