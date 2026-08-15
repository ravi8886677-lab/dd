"""Map a spoken-language name back to the ISO-639-1 code the app uses.

Local Whisper reports ``"en"``. Groq's ``verbose_json`` reports
``"English"`` for the same audio, because it serialises Whisper's own
display name rather than the key. Everything downstream of the listener —
tool locale selection, the TTS voice picker — was written against the
two-letter code, so the hosted path has to be translated or it silently
degrades those choices.

The table is Whisper's own ``LANGUAGES`` mapping inverted, plus the
alias spellings Whisper accepts, so a name it can emit always round-trips.
An unrecognised name yields ``None`` rather than a guess: no language at
all is recoverable downstream, a confidently wrong one is not.
"""

from __future__ import annotations

from typing import Optional

# Whisper's LANGUAGES dict, inverted to name -> code.
_NAME_TO_CODE = {
    "english": "en", "chinese": "zh", "german": "de", "spanish": "es",
    "russian": "ru", "korean": "ko", "french": "fr", "japanese": "ja",
    "portuguese": "pt", "turkish": "tr", "polish": "pl", "catalan": "ca",
    "dutch": "nl", "arabic": "ar", "swedish": "sv", "italian": "it",
    "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi",
    "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms",
    "czech": "cs", "romanian": "ro", "danish": "da", "hungarian": "hu",
    "tamil": "ta", "norwegian": "no", "thai": "th", "urdu": "ur",
    "croatian": "hr", "bulgarian": "bg", "lithuanian": "lt", "latin": "la",
    "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk",
    "telugu": "te", "persian": "fa", "latvian": "lv", "bengali": "bn",
    "serbian": "sr", "azerbaijani": "az", "slovenian": "sl", "kannada": "kn",
    "estonian": "et", "macedonian": "mk", "breton": "br", "basque": "eu",
    "icelandic": "is", "armenian": "hy", "nepali": "ne", "mongolian": "mn",
    "bosnian": "bs", "kazakh": "kk", "albanian": "sq", "swahili": "sw",
    "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si",
    "khmer": "km", "shona": "sn", "yoruba": "yo", "somali": "so",
    "afrikaans": "af", "occitan": "oc", "georgian": "ka", "belarusian": "be",
    "tajik": "tg", "sindhi": "sd", "gujarati": "gu", "amharic": "am",
    "yiddish": "yi", "lao": "lo", "uzbek": "uz", "faroese": "fo",
    "haitian creole": "ht", "pashto": "ps", "turkmen": "tk", "nynorsk": "nn",
    "maltese": "mt", "sanskrit": "sa", "luxembourgish": "lb", "myanmar": "my",
    "tibetan": "bo", "tagalog": "tl", "malagasy": "mg", "assamese": "as",
    "tatar": "tt", "hawaiian": "haw", "lingala": "ln", "hausa": "ha",
    "bashkir": "ba", "javanese": "jw", "sundanese": "su", "cantonese": "yue",
    # Whisper's accepted alias spellings for the same languages.
    "burmese": "my", "valencian": "ca", "flemish": "nl", "haitian": "ht",
    "letzeburgesch": "lb", "pushto": "ps", "panjabi": "pa",
    "moldavian": "ro", "moldovan": "ro", "sinhalese": "si",
    "castilian": "es", "mandarin": "zh",
}

# The codes above, as a set, so an input that is already a code is
# recognised as one instead of being looked up as a name and lost.
_CODES = frozenset(_NAME_TO_CODE.values())


def normalise_language(raw: object) -> Optional[str]:
    """Return an ISO-639-1 code for ``raw``, or ``None`` if unrecognised.

    Accepts either form, since the two backends disagree: a code passes
    through, a display name is translated. Case and surrounding space are
    ignored because these values come off the wire.
    """
    name = str(raw or "").strip().lower()
    if not name:
        return None
    if name in _CODES:
        return name
    return _NAME_TO_CODE.get(name)
