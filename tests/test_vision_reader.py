"""How the vision reader treats the model's answer: parsing, cut-off answers, retries. No network.

Background (KI-060): in the 2026-10-27 eval 10 of 15 chittis failed with "did not return valid JSON".
A live call showed why: the model thinks first, the thinking used 3906 of the 4000 allowed tokens
(finish_reason "length"), and the JSON was cut off or never started.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from order_desk.config import load_settings
from order_desk.domain.errors import ExternalServiceError
from order_desk.vision.reader import DEFAULT_MAX_TOKENS, TokenFactoryReader, _json_object

LINE = {"raw_text": "6A switch (Anchor) - 20 Nos", "mark": "none",
        "item": {"category": "switch", "spec": {"rating_amp": 6}, "brand": "Anchor", "brand_raw": "Anchor"},
        "quantity": 20, "unit": "nos", "legibility": "clear", "ambiguity": None}
ANSWER = {"header": {"shop_name_raw": "Sri Sai Electricals", "date_raw": "25/10/26"}, "lines": [LINE, LINE]}
ANSWER_TEXT = json.dumps(ANSWER, indent=2)

# The start of the answer the model really gave in the live call: cut off after `"category":`.
CUT_OFF = ('{\n  "header": {\n    "shop_name_raw": "Sri Sai Electricals",\n    "date_raw": "25/10/26"\n  },\n'
           '  "lines": [\n    {\n      "line_no_written": "1)",\n      "raw_text": "1.5 sq mm wire (Red) - 2 coil",\n'
           '      "mark": "none",\n      "item": {\n        "category":')


# ------------------------------------------------------------------ _json_object

@pytest.mark.parametrize("text", [
    ANSWER_TEXT,                                                       # bare
    "```json\n" + ANSWER_TEXT + "\n```",                               # fenced, with language
    "```\n" + ANSWER_TEXT + "\n```",                                   # fenced, without language
    "Here is the reading:\n\n" + ANSWER_TEXT + "\n\nLine 2 was hard to read.",   # text around it
    "  \n" + ANSWER_TEXT + "  \n\n",                                   # whitespace
    "```json\n" + ANSWER_TEXT + "\n```\nHope this helps {smile}",      # braces in the text after it
])
def test_the_object_is_found_in_the_usual_wrappings(text):
    assert _json_object(text) == ANSWER


def test_thinking_in_the_text_is_dropped_even_if_it_has_braces():
    thinking = '<think>The shape is {"header": {...}, "lines": [{...}]}. Line 1 first {</think>\n'
    assert _json_object(thinking + ANSWER_TEXT) == ANSWER
    assert _json_object("<THINK>x {</THINK>" + ANSWER_TEXT) == ANSWER


def test_only_a_closing_think_tag_means_everything_before_it_is_thinking():
    assert _json_object('I will list {the lines} first.\n</think>\n' + ANSWER_TEXT) == ANSWER


def test_thinking_that_never_ends_is_not_an_answer():
    with pytest.raises(ValueError, match="only thinking"):
        _json_object('<think>Line 1 is {"raw_text": "6A switch"}. Now line 2')


def test_braces_and_quotes_inside_strings_do_not_confuse_the_search():
    tricky = {"lines": [dict(LINE, raw_text='6A sw } - 20 {nos} "x" \\ end', ambiguity="brace } in text")]}
    assert _json_object("answer: " + json.dumps(tricky)) == tricky


def test_with_several_objects_the_last_valid_one_wins():
    draft = json.dumps({"lines": []})
    assert _json_object(draft + "\nWait, correction:\n" + ANSWER_TEXT) == ANSWER
    assert _json_object(ANSWER_TEXT + "\n" + "{not json}") == ANSWER


def test_a_cut_off_answer_is_an_error_not_a_partial_reading():
    # The old code sliced to the LAST `}` (the end of "header") and failed with a confusing
    # "Expecting ',' delimiter: line 5 column 4 (char 90)".
    with pytest.raises(ValueError, match="cut off"):
        _json_object(CUT_OFF)


def test_a_cut_off_answer_never_gives_back_a_smaller_object_from_inside_it():
    cut_after_two_lines = ANSWER_TEXT[: ANSWER_TEXT.rindex("}")].rstrip().rstrip("]").rstrip()
    with pytest.raises(ValueError, match="cut off"):
        _json_object(cut_after_two_lines)           # the header and both lines are complete objects inside it


def test_an_invalid_object_is_an_error_and_its_parts_are_not_returned():
    missing_comma = '{"header": {"shop_name_raw": null}, "lines": [{"raw_text": "a"} {"raw_text": "b"}]}'
    with pytest.raises(ValueError, match="invalid JSON"):
        _json_object(missing_comma)


@pytest.mark.parametrize("text, message", [
    ("", "empty"), ("   \n", "empty"), (None, "empty"),
    ("I cannot read this photo.", "no JSON object"),
    ('["a", "b"]', "no JSON object"),                # a list is not an object
    ("<think>only thinking</think>", "only thinking"),
])
def test_no_object_gives_a_clear_reason(text, message):
    with pytest.raises(ValueError, match=message):
        _json_object(text)


# ------------------------------------------------------------------ the call loop

class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def reply(content, finish="stop", tokens=None, thinking=None):
    usage = SimpleNamespace(completion_tokens=tokens,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=thinking))
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage)


def make_reader(*replies, **kwargs):
    reader = TokenFactoryReader("test-key", "http://localhost:9/v1/", "test-model", **kwargs)
    reader._client = FakeClient(*replies)
    return reader


def test_a_good_answer_is_read_with_the_default_budget_and_no_unconfirmed_provider_options():
    reader = make_reader(reply(ANSWER_TEXT))
    reading = reader.read_chit_text("6A switch 20")
    assert [l.quantity for l in reading.lines] == [20, 20]
    (call,) = reader._client.calls
    assert call["max_tokens"] == DEFAULT_MAX_TOKENS == 16000 and call["temperature"] == 0
    # Nebius' docs do not confirm JSON mode or a thinking switch for this model (KI-060): send neither.
    assert not {"response_format", "extra_body", "reasoning_effort", "chat_template_kwargs"} & set(call)


def test_the_budget_can_be_set():
    reader = make_reader(reply(ANSWER_TEXT), max_tokens=9000)
    reader.read_chit_text("x")
    assert reader._client.calls[0]["max_tokens"] == 9000


def test_an_answer_cut_off_by_the_limit_ends_at_once_with_a_clear_error():
    cut = reply(CUT_OFF, finish="length", tokens=4000, thinking=3906)
    reader = make_reader(cut, reply(ANSWER_TEXT))            # the 2nd reply must never be asked for
    with pytest.raises(ExternalServiceError, match=r"ran out of output tokens.*3906 of them thinking"):
        reader.read_chit_text("x")
    assert len(reader._client.calls) == 1                    # no repair turn: it cannot finish the answer


def test_all_the_budget_spent_on_thinking_leaves_no_content_and_is_the_same_clear_error():
    reader = make_reader(reply(None, finish="length", tokens=16000, thinking=16000))
    with pytest.raises(ExternalServiceError, match="ran out of output tokens"):
        reader.read_chit_text("x")
    assert len(reader._client.calls) == 1


def test_an_answer_that_is_not_json_gets_one_repair_turn():
    reader = make_reader(reply("Sorry, I could not read it."), reply(ANSWER_TEXT))
    assert len(reader.read_chit_text("x").lines) == 2
    first, second = reader._client.calls
    assert len(second["messages"]) == len(first["messages"]) + 2
    assert second["messages"][-2] == {"role": "assistant", "content": "Sorry, I could not read it."}
    assert "not one valid JSON object" in second["messages"][-1]["content"]


def test_an_empty_answer_that_was_not_cut_off_is_asked_again_without_an_empty_assistant_turn():
    reader = make_reader(reply(None, finish="stop"), reply(ANSWER_TEXT))
    assert len(reader.read_chit_text("x").lines) == 2
    first, second = reader._client.calls
    assert second["messages"] == first["messages"]


def test_two_bad_answers_end_in_an_external_service_error():
    reader = make_reader(reply("no"), reply("still no"))
    with pytest.raises(ExternalServiceError, match="did not return valid JSON: no JSON object found"):
        reader.read_chit_text("x")
    assert len(reader._client.calls) == 2


def test_the_image_path_uses_the_same_budget():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (30, 20), (255, 255, 255)).save(buf, format="PNG")
    reader = make_reader(reply(ANSWER_TEXT), max_tokens=7777)
    reader.read_chit_image(buf.getvalue(), "image/png")
    assert reader._client.calls[0]["max_tokens"] == 7777


# ------------------------------------------------------------------ the setting

def test_the_vision_budget_is_a_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("VISION_MAX_TOKENS", raising=False)
    assert load_settings(tmp_path / "none.env").vision_max_tokens == 16000
    monkeypatch.setenv("VISION_MAX_TOKENS", "9000")
    assert load_settings(tmp_path / "none.env").vision_max_tokens == 9000


def test_the_budget_reaches_the_reader_that_the_server_builds(tmp_path):
    from order_desk.config import Settings
    from order_desk.services.deps import Deps

    settings = Settings(db_path=tmp_path / "x.sqlite3", upload_dir=tmp_path / "u", generated_dir=tmp_path / "g",
                        nebius_api_key="test-key", vision_max_tokens=1234)
    assert Deps.build(settings).reader.max_tokens == 1234
