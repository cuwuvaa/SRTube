import pytest

from srtube.errors import UserError
from srtube.keyboards import effects_keyboard
from srtube.models import Effects, metadata, parse_effects, youtube_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=BaW_jenozKc&list=ignored&t=3",
        "https://youtu.be/BaW_jenozKc?si=ignored",
        "https://music.youtube.com/watch?v=BaW_jenozKc",
        "youtube.com/shorts/BaW_jenozKc",
        "https://m.youtube.com/watch?v=BaW_jenozKc",
        "https://youtube.com/embed/BaW_jenozKc",
    ],
)
def test_canonical_video_only(url):
    assert youtube_url(url) == "https://www.youtube.com/watch?v=BaW_jenozKc"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/watch?v=BaW_jenozKc",
        "https://youtube.com.evil.com/watch?v=BaW_jenozKc",
        "https://youtube.com@127.0.0.1/watch?v=BaW_jenozKc",
        "https://youtube.com:8000/watch?v=BaW_jenozKc",
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "https://youtube.com/playlist?list=123",
        "https://youtu.be/not-valid",
        "https://youtu.be/BaW_jenozKc/another",
    ],
)
def test_reject_non_video_and_local_urls(url):
    with pytest.raises(UserError):
        youtube_url(url)


def test_structured_metadata_wins():
    assert metadata(
        {"track": "Actual song", "artist": "Artist", "title": "Clickbait", "uploader": "Channel"}
    ) == ("Actual song", "Artist")


def test_title_fallback_and_topic_channel():
    assert metadata({"title": "Artist — Song (Official Music Video)"}) == ("Song", "Artist")
    assert metadata({"title": "Song", "uploader": "Artist - Topic"}) == ("Song", "Artist")


@pytest.mark.parametrize("text", ["17 35", "17,35", "17% 35%", "17 / 35"])
def test_custom_percentages(text):
    assert parse_effects(text) == Effects(17, 35)


@pytest.mark.parametrize("text", ["-1 30", "51 30", "10 101", "slowed", "10.5 20", "10 20 30"])
def test_reject_bad_percentages(text):
    with pytest.raises(UserError):
        parse_effects(text)


def test_all_keyboard_payloads_fit_telegram_limit():
    for effects in (Effects(0, 0), Effects(50, 100), Effects(17, 35)):
        markup = effects_keyboard("123456789abc", effects)
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
