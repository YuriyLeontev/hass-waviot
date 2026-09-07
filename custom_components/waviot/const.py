"""Constants for the WAVIoT integration."""

DOMAIN = "waviot"

CONF_API_KEY = "api_key"
CONF_MODEM_ID = "modem_id"
CONF_BASE_URL = "base_url"
CONF_CHANNELS = "channels"
CONF_IMPORT_STATISTICS = "import_statistics"
CONF_PRICES = "prices"

DEFAULT_BASE_URL = "https://lk.waviot.ru"
DEFAULT_UPDATE_INTERVAL_MIN = 30

# How far back to look for readings on every refresh (the meter uplinks
# in batches, sometimes hours apart, so we re-read a generous window).
FETCH_WINDOW_DAYS = 7
# On the very first refresh we backfill history into HA statistics. How far
# back the API goes differs per meter (it serves the meter's own load
# profile, which can predate the installation), so rather than guessing a
# depth the backfill walks backwards until the readings run out.
#
# It is fetched in windows of this size: a single multi-year request is slow
# and trips server-side rate limits.
BACKFILL_CHUNK_DAYS = 30
# End of history: this many empty windows in a row. Meters do go quiet for
# a while, so a single empty window is not the end.
BACKFILL_EMPTY_CHUNKS = 3
# Give up instead when this many windows in a row fail - the API is down,
# and walking the whole range would only hammer it.
BACKFILL_ERROR_CHUNKS = 3
# Hard floor on the walk, whatever the API keeps answering.
BACKFILL_MAX_DAYS = 5 * 365

# Candidate energy channels probed automatically (WAVIoT FOBOS electricity
# meters). Channels that return no data are silently skipped.
DEFAULT_ENERGY_CHANNELS = [
    "electro_ac_p_lsum",
    "electro_ac_p_lsum_t1",
    "electro_ac_p_lsum_t2",
    "electro_ac_p_lsum_t3",
    "electro_ac_p_lsum_t4",
    "electro_ac_p_lsum_tsum",
]

# Only channels with this prefix are considered for auto-discovery
# (active imported energy). FOBOS meters expose ~50 channels (voltages,
# cos-fi, phase angles, ...) and probing them all is slow and trips
# server-side rate limits.
ENERGY_CHANNEL_PREFIX = "electro_ac_p_lsum"

# Shortcuts accepted in the "prices" option instead of full channel names.
PRICE_KEY_ALIASES = {
    "total": ENERGY_CHANNEL_PREFIX,
    "t1": f"{ENERGY_CHANNEL_PREFIX}_t1",
    "t2": f"{ENERGY_CHANNEL_PREFIX}_t2",
    "t3": f"{ENERGY_CHANNEL_PREFIX}_t3",
    "t4": f"{ENERGY_CHANNEL_PREFIX}_t4",
    "tsum": f"{ENERGY_CHANNEL_PREFIX}_tsum",
}

# Date formats accepted after "@" in a price entry (t1@2026-10-01=7.86).
PRICE_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d")

CHANNEL_NAMES = {
    "electro_ac_p_lsum": "Energy total",
    "electro_ac_p_lsum_t1": "Energy T1",
    "electro_ac_p_lsum_t2": "Energy T2",
    "electro_ac_p_lsum_t3": "Energy T3",
    "electro_ac_p_lsum_t4": "Energy T4",
    "electro_ac_p_lsum_tsum": "Energy tariffs sum",
}

STATISTICS_SOURCE = DOMAIN
