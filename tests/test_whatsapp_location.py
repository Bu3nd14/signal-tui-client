"""Shared positions must render as readable text, never as base64 thumbnails."""

from protocols.whatsapp_events import _event_from_message

THUMB = "/9j/4AAQSkZJRgABAQAAAQABAAD/4gHYSUNDX1BST0ZJTEUAAQEAAAHIAAAAAAQwAA"


def _base(**extra):
    raw = {
        "id": "false_391234@c.us_ABC",
        "from": "391234@c.us",
        "fromMe": False,
        "timestamp": 1792000000,
        "type": "location",
        "body": THUMB,
    }
    raw.update(extra)
    return raw


def _text(raw):
    events = _event_from_message(raw)
    assert len(events) == 1
    return events[0].payload["text"]


def test_flat_location_webjs():
    text = _text(
        _base(
            location={
                "latitude": "45.4642",
                "longitude": "9.19",
                "description": "Duomo\nMilano",
            }
        )
    )
    assert "/9j/" not in text
    assert text.startswith("📍 Posizione: Duomo")
    assert "https://maps.google.com/?q=45.464200,9.190000" in text


def test_nested_location_message_outgoing():
    raw = _base(
        fromMe=True,
        to="391234@c.us",
        _data={
            "message": {
                "locationMessage": {
                    "degreesLatitude": 41.9,
                    "degreesLongitude": 12.5,
                    "name": "Colosseo",
                    "address": "Piazza del Colosseo, Roma",
                    "jpegThumbnail": THUMB,
                }
            }
        },
    )
    text = _text(raw)
    assert "/9j/" not in text
    assert "Colosseo" in text and "Piazza del Colosseo, Roma" in text
    assert "q=41.900000,12.500000" in text


def test_live_location():
    raw = _base(
        _data={
            "message": {
                "liveLocationMessage": {"degreesLatitude": 1, "degreesLongitude": 2}
            }
        }
    )
    assert _text(raw).startswith("📍 Posizione in tempo reale")


def test_unknown_base64_body_is_not_shown_raw():
    raw = _base(type="chat")
    assert "/9j/" not in _text(raw)


def test_plain_text_untouched():
    assert _text(_base(type="chat", body="Ciao!")) == "Ciao!"


def _jpeg_b64():
    import base64

    return base64.b64encode(b"\xff\xd8\xff\xe0" + b"map" * 20).decode()


def test_location_thumbnail_saved_from_nested_jpeg():
    from protocols.location_thumbs import load_thumbnail

    raw = _base(
        body="",
        _data={
            "message": {
                "locationMessage": {
                    "degreesLatitude": 45.4642,
                    "degreesLongitude": 9.19,
                    "jpegThumbnail": _jpeg_b64(),
                }
            }
        },
    )
    _text(raw)
    assert load_thumbnail(45.4642, 9.19).startswith(b"\xff\xd8\xff")


def test_location_thumbnail_saved_from_webjs_body():
    from protocols.location_thumbs import load_thumbnail

    raw = _base(body=_jpeg_b64(), location={"latitude": "1.5", "longitude": "2.5"})
    _text(raw)
    assert load_thumbnail(1.5, 2.5) is not None


def test_non_jpeg_thumbnail_is_ignored():
    from protocols.location_thumbs import load_thumbnail

    raw = _base(body="aGVsbG8=", location={"latitude": "3", "longitude": "4"})
    _text(raw)
    assert load_thumbnail(3, 4) is None


def test_maps_url_round_trip():
    from protocols.location_thumbs import maps_url, parse_maps_url

    assert parse_maps_url(maps_url(-33.86, 151.2)) == (-33.86, 151.2)
    assert parse_maps_url("https://maps.google.com/?q=1,2") is None
    assert parse_maps_url("https://maps.google.com/?q=95.000000,2.000000") is None
