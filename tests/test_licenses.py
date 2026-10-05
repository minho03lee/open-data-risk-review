import pytest

from app.licenses import flags, to_spdx


@pytest.mark.parametrize(
    "raw,spdx",
    [
        ("cc-by-4.0", "CC-BY-4.0"),
        ("CC BY-NC-SA 4.0", "CC-BY-NC-SA-4.0"),
        ("license:cc-by-nc-4.0", "CC-BY-NC-4.0"),
        ("CC0: Public Domain", "CC0-1.0"),
        ("odc-by", "ODC-By-1.0"),
        ("apache-2.0", "Apache-2.0"),
        ("other", None),
        ("AI허브 이용약관", None),
    ],
)
def test_to_spdx(raw, spdx):
    assert to_spdx(raw) == spdx


def test_flags():
    assert flags("CC-BY-NC-4.0")["commercial"] is False
    assert flags("CC-BY-ND-4.0")["derivatives"] is False
    assert flags("CC-BY-SA-4.0")["share_alike"] is True
    assert flags(None)["commercial"] is None
