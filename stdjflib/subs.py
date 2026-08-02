"""Subtitle sample text and the SRT / ASS / VTT writers.

The point of the multi-script samples is font fallback and bidirectional
layout, so each script gets real sentences in that script rather than
transliterated Latin. A client that renders the Hebrew and Arabic cues
left-to-right, or draws tofu boxes for the CJK ones, fails visibly.
"""

from __future__ import annotations

# Four cues per script. Kept short so they fit one line at 720p, and written so
# that a wrong-direction render of the RTL ones is obvious rather than subtle
# (the punctuation lands on the wrong end).
SCRIPTS = {
    "latin": [
        "This is a subtitle cue.",
        "The quick brown fox jumps over the lazy dog.",
        "Line one of two.\nLine two of two.",
        "Punctuation: commas, colons; and — dashes.",
    ],
    "cyrillic": [
        "Это строка субтитров.",
        "Съешь же ещё этих мягких французских булок.",
        "Первая строка.\nВторая строка.",
        "Пунктуация: запятые, двоеточия; и — тире.",
    ],
    "greek": [
        "Αυτός είναι ένας υπότιτλος.",
        "Ξεσκεπάζω την ψυχοφθόρα βδελυγμία.",
        "Πρώτη γραμμή.\nΔεύτερη γραμμή.",
        "Στίξη: κόμματα, άνω τελείες· και — παύλες.",
    ],
    "cjk": [
        "これは字幕の行です。",
        "色は匂へど散りぬるを。",
        "一行目。\n二行目。",
        "句読点：読点、句点。そしてダッシュ —",
    ],
    "rtl": [
        "זוהי שורת כתוביות.",
        "הנה משפט נוסף בעברית, עם פסיק.",
        "שורה ראשונה.\nשורה שנייה.",
        "‏מספרים בתוך טקסט: 1234 ואחריהם עוד מילים.",
    ],
    "arabic": [
        "هذا سطر من الترجمة.",
        "جملة أخرى بالعربية، مع فاصلة.",
        "السطر الأول.\nالسطر الثاني.",
        "‏أرقام داخل النص: 1234 ثم كلمات أخرى.",
    ],
}

# The Arabic entries live under their own key but "rtl" is what recipes ask for;
# pick per language so heb and ara do not get identical text.
_BY_LANG = {"heb": "rtl", "ara": "arabic", "ar": "arabic", "he": "rtl"}


def sample_lines(script: str, lang: str = "eng") -> list[str]:
    key = _BY_LANG.get(lang, script)
    return SCRIPTS.get(key, SCRIPTS["latin"])


def _ts(seconds: float, sep: str = ",") -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def cues(duration: int, script: str, lang: str, label: str,
         *, forced: bool = False) -> list[tuple[float, float, str]]:
    """Evenly spaced cues across the clip.

    A forced track gets only two cues, near the start and near the end — that
    asymmetry is what makes "did the client honour the forced flag" answerable
    by looking at the screen.
    """
    lines = sample_lines(script, lang)
    if forced:
        return [
            (2.0, 6.0, f"[{label} FORCED] {lines[0]}"),
            (max(8.0, duration - 6.0), max(11.0, duration - 2.0),
             f"[{label} FORCED] {lines[1]}"),
        ]
    out = []
    # Leave a second of lead-in, and one cue per slot with a short gap between.
    n = min(len(lines) * 2, max(2, duration // 4))
    slot = (duration - 2.0) / n
    for i in range(n):
        start = 1.0 + i * slot
        end = start + slot * 0.75
        text = lines[i % len(lines)]
        out.append((start, end, f"[{label} {i + 1}/{n}] {text}"))
    return out


def srt(duration: int, script: str, lang: str, label: str,
        *, forced: bool = False) -> str:
    parts = []
    for i, (start, end, text) in enumerate(cues(duration, script, lang, label,
                                                forced=forced), 1):
        parts.append(f"{i}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
    return "\n".join(parts)


def vtt(duration: int, script: str, lang: str, label: str,
        *, forced: bool = False) -> str:
    parts = ["WEBVTT", ""]
    for i, (start, end, text) in enumerate(cues(duration, script, lang, label,
                                                forced=forced), 1):
        parts.append(f"{i}")
        parts.append(f"{_ts(start, '.')} --> {_ts(end, '.')}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


# The styled ASS uses three named styles and one positioned + one karaoke cue,
# because those are the four things a "renders ASS as plain text" client loses.
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,30,1
Style: Sign,{font},40,&H0000D7FF,&H000000FF,&H00202020,&H00000000,1,0,0,0,100,100,0,0,1,3,0,8,20,20,20,1
Style: Alt,{font},44,&H00A0FF80,&H000000FF,&H00000000,&H80000000,0,1,0,0,100,100,0,0,1,2,1,2,20,20,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass(duration: int, script: str, lang: str, label: str,
        *, forced: bool = False, font: str = "DejaVu Sans") -> str:
    body = [_ASS_HEADER.format(font=font)]
    items = cues(duration, script, lang, label, forced=forced)
    for i, (start, end, text) in enumerate(items):
        text = text.replace("\n", "\\N")
        style = "Default"
        prefix = ""
        if i == 1 and not forced:
            # A positioned sign in the top-right, which plain-text rendering
            # drops to the bottom centre with everything else.
            style = "Sign"
            prefix = "{\\pos(1140,60)\\fad(200,200)}"
        elif i == 2 and not forced:
            style = "Alt"
            # Karaoke timing: each word highlights in turn.
            words = text.split(" ")
            per = max(10, int((end - start) * 100 / max(1, len(words))))
            text = "".join(f"{{\\k{per}}}{w} " for w in words).strip()
        body.append(
            f"Dialogue: 0,{_ts(start, '.')[:-1]},{_ts(end, '.')[:-1]},"
            f"{style},,0,0,0,,{prefix}{text}"
        )
    return "\n".join(body) + "\n"


WRITERS = {"subrip": srt, "srt": srt, "webvtt": vtt, "vtt": vtt, "ass": ass}


def render(codec: str, duration: int, script: str, lang: str, label: str,
           *, forced: bool = False) -> tuple[str, str]:
    """Return (text, extension) for a subtitle track.

    Bitmap codecs (dvdsub, dvbsub, xsub) have no text form — they are produced
    by transcoding an SRT through ffmpeg, so this still hands back SubRip and
    `generate.py` does the conversion.
    """
    if codec in ("dvdsub", "dvbsub", "xsub", "mov_text"):
        return srt(duration, script, lang, label, forced=forced), "srt"
    writer = WRITERS.get(codec, srt)
    ext = {"srt": "srt", "subrip": "srt", "webvtt": "vtt", "vtt": "vtt",
           "ass": "ass"}.get(codec, "srt")
    return writer(duration, script, lang, label, forced=forced), ext
