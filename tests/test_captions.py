from pathlib import Path

from captions import Word, _chunks, _clean, _ts, ass_filter, build_ass


def words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(t, s, e) for t, s, e in spec]


def test_timestamp_format():
    assert _ts(0) == "0:00:00.00"
    assert _ts(61.234) == "0:01:01.23"
    assert _ts(3725.5) == "1:02:05.50"


def test_clean_uppercases_and_strips_ass_control_chars():
    assert _clean("hi {there}\\n") == "HI (THERE)N"


def test_chunks_cap_at_three_words():
    ws = words(*[(f"w{i}", i * 0.2, i * 0.2 + 0.15) for i in range(7)])
    assert [len(c) for c in _chunks(ws)] == [3, 3, 1]


def test_chunks_break_on_pause_and_punctuation():
    ws = words(("no", 0.0, 0.2), ("way!", 0.25, 0.5), ("what", 0.55, 0.7), ("now", 2.0, 2.2))
    assert [[w.text for w in c] for c in _chunks(ws)] == [["no", "way!"], ["what"], ["now"]]


def test_build_ass_highlights_each_word(tmp_path: Path):
    ws = words(("let's", 0.0, 0.3), ("go", 0.3, 0.6))
    out = build_ass(ws, tmp_path / "c.ass", y=700)
    text = out.read_text()
    assert "[Events]" in text
    dialogue = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue) == 2  # one event per spoken word
    assert all("\\pos(540,700)" in d for d in dialogue)


def test_build_ass_without_words_writes_nothing(tmp_path: Path):
    assert build_ass([], tmp_path / "none.ass", y=0) is None
    assert not (tmp_path / "none.ass").exists()


def test_ass_filter_points_at_file():
    assert ass_filter(Path("clips/x.ass")).startswith("ass=clips/x.ass:")
