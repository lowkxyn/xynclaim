from pathlib import Path

from xynclaim import engine as claimer


def fixture(name):
    frame = claimer.cv2.imread(
        str(Path(__file__).parent / "fixtures" / name)
    )
    assert frame is not None
    return frame


def test_travel_tab_is_recognised_in_the_open_menu():
    frame = fixture("menu_2026_travel.png")
    detector = claimer.VisualDetector()
    assert detector.matches(frame, "game_menu")[0]
    assert detector.matches(frame, "travel_menu")[0]


def test_other_menu_tabs_are_not_mistaken_for_travel():
    frame = fixture("menu_2026_profile.png")
    detector = claimer.VisualDetector()
    assert detector.matches(frame, "game_menu")[0]
    assert not detector.matches(frame, "travel_menu")[0]


def test_daily_panel_is_not_mistaken_for_an_open_menu():
    frame = fixture("daily_panel_2026_claim_day1.png")
    detector = claimer.VisualDetector()
    assert detector.matches(frame, "daily_rewards")[0]
    assert not detector.matches(frame, "game_menu")[0]
    assert not detector.matches(frame, "travel_menu")[0]


def test_unobscured_reference_still_matches():
    frame = claimer.cv2.imread(str(claimer.REFERENCE_DIR / "travel_menu.png"))
    assert frame is not None
    assert claimer.VisualDetector().matches(frame, "travel_menu")[0]
