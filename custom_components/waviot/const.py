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
# On the very first refresh we backfill this much history into HA statistics.
BACKFILL_DAYS = 365
# The backfill is fetched in windows of this size: a single year-long request
# is slow and trips server-side rate limits.
BACKFILL_CHUNK_DAYS = 30

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

CHANNEL_NAMES = {
    "electro_ac_p_lsum": "Energy total",
    "electro_ac_p_lsum_t1": "Energy T1",
    "electro_ac_p_lsum_t2": "Energy T2",
    "electro_ac_p_lsum_t3": "Energy T3",
    "electro_ac_p_lsum_t4": "Energy T4",
    "electro_ac_p_lsum_tsum": "Energy tariffs sum",
}

STATISTICS_SOURCE = DOMAIN
