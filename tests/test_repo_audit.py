"""A historical fixture must never exempt other credentials on its line."""
from support_copilot.repo_audit import KNOWN_SYNTHETIC, has_secret


def test_exact_synthetic_marker_is_allowed():
    assert not has_secret('fixture = ' + repr(KNOWN_SYNTHETIC[0]))


def test_other_secret_on_canary_line_is_reported():
    credential = 'ghp_' + 'A' * 36
    for text in (credential, KNOWN_SYNTHETIC[0] + ' ' + credential,
                 credential + ' ' + KNOWN_SYNTHETIC[0]):
        assert has_secret(text)


def test_canary_prefix_does_not_allow_longer_secret():
    assert has_secret(KNOWN_SYNTHETIC[0] + '-other-credential')
