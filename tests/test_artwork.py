import json
from io import BytesIO

import pytest
from PIL import Image

from srtube.artwork import CoverSettings, compose_artwork, make_artwork, open_artwork, prepare_artwork_edit
from srtube.errors import UserError
from srtube.metadata_editor import ArtworkBuffer


def image_bytes(image, format="PNG"):
    with BytesIO() as buffer:
        image.save(buffer, format)
        return buffer.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_supported_upload_formats_are_normalized(format):
    image = open_artwork(image_bytes(Image.new("RGB", (120, 80), "red"), format))
    assert image.mode == "RGB" and image.size == (120, 80)
    assert image.getpixel((60, 40))[0] > 200


def test_transparency_and_exif_orientation_are_applied():
    transparent = open_artwork(image_bytes(Image.new("RGBA", (80, 40), (255, 255, 255, 0))))
    assert transparent.getpixel((20, 20)) == (18, 18, 28)
    exif = Image.Exif()
    exif[274] = 6
    with BytesIO() as buffer:
        Image.new("RGB", (80, 40), "red").save(buffer, "JPEG", exif=exif)
        assert open_artwork(buffer.getvalue()).size == (40, 80)


def test_crop_position_zoom_rotation_and_brightness():
    image = Image.new("RGB", (300, 100), "red")
    image.paste("blue", (200, 0, 300, 100))
    left = compose_artwork(image, CoverSettings(crop=True, x=0))
    right = compose_artwork(image, CoverSettings(crop=True, x=100))
    assert left.getpixel((400, 400)) == (255, 0, 0)
    assert right.getpixel((400, 400)) == (0, 0, 255)
    vertical = compose_artwork(image, CoverSettings(rotation=90))
    assert vertical.getpixel((400, 100)) == (255, 0, 0)
    assert vertical.getpixel((400, 700)) == (0, 0, 255)
    gray = Image.new("RGB", (100, 100), (100, 100, 100))
    assert compose_artwork(gray, CoverSettings(brightness=150)).getpixel((400, 400)) == (150, 150, 150)
    image.paste("green", (140, 40, 160, 60))
    zoom = compose_artwork(image, CoverSettings(zoom=300))
    red, green, blue = zoom.getpixel((200, 400))
    assert red < 10 and green > 120 and blue == 0
    assert compose_artwork(image, CoverSettings()).getpixel((200, 400)) == (255, 0, 0)


def test_cover_settings_persist_and_reset_uses_source_without_accumulated_edits(tmp_path):
    directory = tmp_path / "track"
    directory.mkdir()
    make_artwork(None, directory)
    staged = tmp_path / "staged"
    staged.mkdir()
    raw = image_bytes(Image.new("RGB", (120, 80), "#904020"))

    def apply(raw=None, action=None):
        prepare_artwork_edit(directory, staged, raw, action)
        for file in staged.iterdir():
            file.replace(directory / file.name)

    apply(raw=raw)
    apply(action="reset")
    source = (directory / "cover.source.jpg").read_bytes()
    baseline = (directory / "cover.jpg").read_bytes()
    for action in ("rotate", "zoom_in", "brighter", "right"):
        apply(action=action)
    assert json.loads((directory / "cover.settings.json").read_text()) == {
        "crop": False,
        "rotation": 90,
        "zoom": 125,
        "x": 60,
        "y": 50,
        "brightness": 110,
    }
    assert (directory / "cover.jpg").read_bytes() != baseline
    assert (directory / "cover.source.jpg").read_bytes() == source
    apply(action="reset")
    assert (directory / "cover.jpg").read_bytes() == baseline


def test_old_cached_cover_can_be_edited_without_source_file(tmp_path):
    directory = tmp_path / "track"
    directory.mkdir()
    Image.new("RGB", (800, 800), "blue").save(directory / "cover.jpg")
    staged = tmp_path / "staged"
    staged.mkdir()
    prepare_artwork_edit(directory, staged, None, "rotate")
    assert (staged / "cover.source.jpg").is_file()
    assert (staged / "cover.jpg").is_file()


def test_reject_unsupported_invalid_oversized_images_and_bounded_download(monkeypatch):
    with pytest.raises(UserError):
        open_artwork(b"broken image")
    with pytest.raises(UserError):
        open_artwork(image_bytes(Image.new("RGB", (10, 10)), "GIF"))
    monkeypatch.setattr("srtube.artwork.MAX_ARTWORK_BYTES", 10)
    with pytest.raises(UserError, match="10 МБ"):
        open_artwork(b"a" * 11)
    monkeypatch.setattr("srtube.metadata_editor.MAX_ARTWORK_BYTES", 10)
    with ArtworkBuffer() as buffer:
        buffer.write(b"a" * 10)
        with pytest.raises(UserError, match="10 МБ"):
            buffer.write(b"a")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 50)
    monkeypatch.setattr("srtube.artwork.MAX_ARTWORK_BYTES", 10_000)
    with pytest.raises(UserError, match="мегапикселей"):
        open_artwork(image_bytes(Image.new("RGB", (10, 10))))
