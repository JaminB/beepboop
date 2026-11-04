"""English text normalisation before synthesis (VOICE_PIPELINE §11.2).

The LLM is told to write for the ear; this is the backstop. Numbers, times,
temperatures, percentages, money, ordinals and common abbreviations become
words: ``"10:30"`` → ``"ten thirty"``, ``"72°F"`` → ``"seventy-two degrees"``,
``"Dr."`` → ``"doctor"``. Pure string functions, no I/O.
"""

from __future__ import annotations

import re

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))
_ORDINAL_EXCEPTIONS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_ABBREVIATIONS = {
    "Dr.": "doctor",
    "Mr.": "mister",
    "Mrs.": "missus",
    "Ms.": "miz",
    "St.": "street",
    "Ave.": "avenue",
    "approx.": "approximately",
    "vs.": "versus",
    "etc.": "et cetera",
    "e.g.": "for example",
    "i.e.": "that is",
}
_UNITS = {
    "km/h": "kilometres per hour",
    "mph": "miles per hour",
    "km": "kilometres",
    "kg": "kilograms",
    "cm": "centimetres",
    "mm": "millimetres",
    "ml": "millilitres",
    "kWh": "kilowatt hours",
    "kW": "kilowatts",
    "W": "watts",
    "lb": "pounds",
    "lbs": "pounds",
    "oz": "ounces",
    "ft": "feet",
    "min": "minutes",
    "mins": "minutes",
    "hrs": "hours",
    "hr": "hours",
}


def number_words(n: int) -> str:
    """Cardinal words for an integer (``-5`` → ``"minus five"``)."""
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("-" + _ONES[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ONES[hundreds] + " hundred" + (" and " + number_words(rest) if rest else "")
    for value, name in _SCALES:
        if n >= value:
            high, rest = divmod(n, value)
            tail = ""
            if rest:
                tail = (" and " if rest < 100 else " ") + number_words(rest)
            return number_words(high) + " " + name + tail
    return str(n)  # pragma: no cover - unreachable


def ordinal_words(n: int) -> str:
    words = number_words(n)
    head, sep, last = words.rpartition(" ") if " " in words else ("", "", words)
    prefix, dash, unit = last.rpartition("-")
    if unit in _ORDINAL_EXCEPTIONS:
        unit = _ORDINAL_EXCEPTIONS[unit]
    elif unit.endswith("y"):
        unit = unit[:-1] + "ieth"
    else:
        unit += "th"
    return head + sep + prefix + dash + unit


def time_words(hour: int, minute: int) -> str:
    if minute == 0:
        return number_words(hour) + " o'clock"
    if minute < 10:
        return number_words(hour) + " oh " + number_words(minute)
    return number_words(hour) + " " + number_words(minute)


def _decimal(match: re.Match[str]) -> str:
    whole, frac = match.group(1), match.group(2)
    return number_words(int(whole)) + " point " + " ".join(_ONES[int(d)] for d in frac)


_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(am|pm|AM|PM|a\.m\.|p\.m\.)?")
_TEMP = re.compile(r"(-?\d+(?:\.\d+)?)\s*°\s*([FC])?")
_PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_MONEY = re.compile(r"([$£€])(\d+)(?:\.(\d{2}))?")
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b")
_DECIMAL = re.compile(r"\b(\d+)\.(\d+)\b")
_INTEGER = re.compile(r"(?<![\w.])(-?\d{1,3}(?:,\d{3})+|-?\d+)(?![\w])")
_UNIT = re.compile(r"\b(\d+)\s?(" + "|".join(re.escape(u) for u in _UNITS) + r")\b")
_CURRENCY = {"$": ("dollar", "cent"), "£": ("pound", "penny"), "€": ("euro", "cent")}


def _money(match: re.Match[str]) -> str:
    major_name, minor_name = _CURRENCY[match.group(1)]
    major = int(match.group(2))
    out = number_words(major) + " " + major_name + ("" if major == 1 else "s")
    if match.group(3) and int(match.group(3)):
        minor = int(match.group(3))
        plural = "pence" if minor_name == "penny" else minor_name + "s"
        out += " and " + number_words(minor) + " " + (minor_name if minor == 1 else plural)
    return out


def _time(match: re.Match[str]) -> str:
    words = time_words(int(match.group(1)), int(match.group(2)))
    suffix = (match.group(3) or "").replace(".", "").lower()
    return words + (" " + " ".join(suffix.upper()) if suffix else "")


def normalize(text: str) -> str:
    """Rewrite ``text`` for the ear."""
    out = text
    for abbr, words in _ABBREVIATIONS.items():
        out = out.replace(abbr, words)
    out = _TIME.sub(_time, out)
    out = _TEMP.sub(lambda m: _number(m.group(1)) + " degrees", out)
    out = _PERCENT.sub(lambda m: _number(m.group(1)) + " percent", out)
    out = _MONEY.sub(_money, out)
    out = _DECIMAL.sub(_decimal, out)
    out = _UNIT.sub(lambda m: number_words(int(m.group(1))) + " " + _UNITS[m.group(2)], out)
    out = _ORDINAL.sub(lambda m: ordinal_words(int(m.group(1))), out)
    out = _INTEGER.sub(lambda m: number_words(int(m.group(1).replace(",", ""))), out)
    return re.sub(r"\s+", " ", out).strip()


def _number(raw: str) -> str:
    if raw.startswith("-"):
        return "minus " + _number(raw[1:])
    if "." in raw:
        whole, frac = raw.split(".", 1)
        return number_words(int(whole)) + " point " + " ".join(_ONES[int(d)] for d in frac)
    return number_words(int(raw))


__all__ = ["normalize", "number_words", "ordinal_words", "time_words"]
