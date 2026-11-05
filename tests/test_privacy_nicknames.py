"""Outbound masking covers Latin and Cyrillic Name_Surname nicknames."""
import pytest

from support_copilot.privacy import redact, redact_tree


@pytest.mark.parametrize('text,expected', [
    ('Alex_North тут', '[NICKNAME] тут'),
    ('Игрок Вася_Пупкин мешает', 'Игрок [NICKNAME] мешает'),
    ('Админ Иван_Петров забанил', 'Админ [NICKNAME] забанил'),
    ('Night_Rider и Вася_Пупкин', '[NICKNAME] и [NICKNAME]'),
    ('Обычный текст без ника', 'Обычный текст без ника'),
])
def test_nicknames_are_masked(text, expected):
    assert redact(text) == expected


def test_control_fields_are_not_masked():
    value = redact_tree({'category': 'complaint.cheat', 'text': 'Вася_Пупкин читерит'})
    assert value['category'] == 'complaint.cheat'
    assert value['text'] == '[NICKNAME] читерит'
