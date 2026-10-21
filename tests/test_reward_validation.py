import pytest

from xynclaim import ocr as reward_reader


@pytest.mark.parametrize("title", ["Received Zeni", "Received +10 of Item", "Received 2X EXP for"])
def test_reward_title_without_its_value_is_not_verified(title):
    assert reward_reader.parse_rewards([title]) == []


def test_missing_value_does_not_consume_the_following_lock_title():
    assert reward_reader.parse_rewards(["Received Zeni", "Global reward locked", "+10 Mastery Points"]) == [
        "+10 Mastery Points (global reward locked)",
    ]


@pytest.mark.parametrize("duration", ["6OO", "6.00", "600 seconds", "6,00", "-600"])
def test_malformed_ocr_duration_is_rejected_without_changing_its_number(duration):
    assert reward_reader.parse_rewards(["Received 2X EXP for", duration]) == []


@pytest.mark.parametrize("quantity", ["6OO", "6,00", "6.00", "Global reward locked"])
def test_zeni_requires_a_complete_integer_amount(quantity):
    rewards = reward_reader.parse_rewards(["Received Zeni", quantity])
    assert all("Zeni" not in reward for reward in rewards)
