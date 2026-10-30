"""Recipient local time: US ZIP -> state -> timezone, and UTC -> local conversion.

Every timestamp in the database is UTC. Convert only when analysing behaviour ("opens at 9am local time").
States that span two timezones use the zone most of their people live in, with ZIP overrides for the
largest exceptions (El Paso, the Florida panhandle, East Tennessee, West Kentucky, North Idaho).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# First three ZIP digits -> state (inclusive ranges)
ZIP3_STATE = [
    (5, 5, "NY"), (6, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"), (50, 54, "VT"),
    (55, 55, "MA"), (56, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"), (150, 196, "PA"),
    (197, 199, "DE"), (200, 200, "DC"), (201, 201, "VA"), (202, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"),
    (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"), (320, 349, "FL"), (350, 369, "AL"),
    (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"), (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"),
    (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"), (569, 569, "DC"), (570, 577, "SD"),
    (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"), (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"),
    (700, 714, "LA"), (716, 729, "AR"), (730, 731, "OK"), (733, 733, "TX"), (734, 749, "OK"), (750, 799, "TX"),
    (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"), (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"),
    (885, 885, "TX"), (889, 898, "NV"), (900, 961, "CA"), (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"),
    (995, 999, "AK"),
]

STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver", "PR": "America/Puerto_Rico",
}

# Parts of a state on a different clock than the rest of it
ZIP3_TZ = {
    **{z: "America/Denver" for z in (798, 799, 885)},                    # El Paso, TX
    **{z: "America/Chicago" for z in (324, 325)},                        # Florida panhandle
    **{z: "America/New_York" for z in (373, 374, 376, 377, 378, 379)},   # East Tennessee
    **{z: "America/Chicago" for z in (420, 421, 422, 423, 424)},         # West Kentucky
    **{z: "America/Los_Angeles" for z in (835, 838)},                    # North Idaho
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def clean_zip(zip_code: str | None) -> str | None:
    """'78701-1234' / '8901' (Excel dropped the leading zero) -> '78701' / '08901'."""
    digits = "".join(c for c in (zip_code or "") if c.isdigit())
    if len(digits) in (9, 5):
        return digits[:5]
    if len(digits) in (3, 4):  # lost leading zeros in a spreadsheet
        return digits.zfill(5)
    return None


def clean_state(state: str | None) -> str | None:
    s = (state or "").strip()
    code = s.upper() if len(s) == 2 else STATE_NAMES.get(s.lower())
    return code if code in STATE_TZ else None


def state_for_zip(zip_code: str | None) -> str | None:
    z = clean_zip(zip_code)
    if not z:
        return None
    prefix = int(z[:3])
    return next((st for lo, hi, st in ZIP3_STATE if lo <= prefix <= hi), None)


def is_valid_timezone(tz: str | None) -> bool:
    if not tz:
        return False
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def timezone_for(zip_code: str | None = None, state: str | None = None) -> str | None:
    """Best IANA timezone for a US recipient; the ZIP wins over the state when both are given."""
    z = clean_zip(zip_code)
    if z and int(z[:3]) in ZIP3_TZ:
        return ZIP3_TZ[int(z[:3])]
    st = state_for_zip(z) or clean_state(state)
    return STATE_TZ.get(st) if st else None


def to_local(when: datetime, tz: str | None) -> datetime:
    """UTC (or naive, treated as UTC) -> the recipient's local time. Unknown timezones fall back to UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(ZoneInfo(tz) if is_valid_timezone(tz) else timezone.utc)
