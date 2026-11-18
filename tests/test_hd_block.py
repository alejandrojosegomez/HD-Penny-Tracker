"""Block detection: the bot-check interstitial must stop the scrape, real pages must not."""
import pytest

from tracker.hd_client import Blocked, HDClient, is_challenge_page

CHALLENGE = ('<!DOCTYPE html> <html> <body><script type="text/javascript" '
             'src="/R99k76gGH9/VmiVczkY/Uj/Y1ZEA6A3UHAQ?v=b90e&amp;t=97"></script> <div id="sec-if"></div>'
             '</body></html>')
PRODUCT = ('<html><head><title>DURASACK 20 Gal. Bag - The Home Depot</title>'
           '<script type="application/ld+json">{"@type":"Product","name":"x"}</script></head>'
           '<body>' + "x" * 20000 + '</body></html>')


def test_challenge_page_detected():
    assert is_challenge_page(CHALLENGE)
    assert not is_challenge_page(PRODUCT)


def _client():
    c = HDClient.__new__(HDClient)        # no browser / network
    c.blocks_in_a_row = 0
    return c


def test_check_block_raises_and_counts():
    c = _client()
    with pytest.raises(Blocked):
        c._check_block(200, CHALLENGE)
    assert c.blocks_in_a_row == 1
    c._check_block(200, PRODUCT)
    assert c.blocks_in_a_row == 0
