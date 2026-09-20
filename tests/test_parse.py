"""End-to-end tests for the /parse endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit


def parse(data):
    return client.post("/parse", json={"data": data})


# ---------------------------------------------------------------- happy path

def test_full_string_all_ais_in_order():
    resp = parse(f"01{GTIN_OK}1725013110ABC123{GS}21SN0001")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["01", "17", "10", "21"]
    assert [e["value"] for e in elements] == [GTIN_OK, "250131", "ABC123", "SN0001"]
    assert [e["offset"] for e in elements] == [0, 16, 24, 33]
    assert [e["name"] for e in elements] == ["GTIN", "EXPIRY", "BATCH/LOT", "SERIAL"]


def test_fixed_length_fields_need_no_separator():
    resp = parse(f"01{GTIN_OK}17250131")
    assert resp.status_code == 200
    assert [e["ai"] for e in resp.json()["elements"]] == ["01", "17"]


def test_variable_field_at_end_needs_no_separator():
    resp = parse("1725013110ABC123")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert elements[-1]["ai"] == "10"
    assert elements[-1]["value"] == "ABC123"


def test_variable_field_max_length_20():
    resp = parse("10" + "A" * 20)
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["value"] == "A" * 20


def test_serial_full_charset():
    resp = parse("21SN-001/A:OK_+x")
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["value"] == "SN-001/A:OK_+x"


def test_expiry_day_00_means_last_day_of_month():
    assert parse("17250200").status_code == 200


def test_leap_day_valid():
    assert parse("17240229").status_code == 200


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- errors

def test_empty_input():
    resp = parse("")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "EMPTY_INPUT"
    assert detail["offset"] == 0


def test_unknown_ai():
    resp = parse("99ABC")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNSUPPORTED_AI"
    assert detail["offset"] == 0
    assert detail["found"] == "99"
    assert "01" in detail["expected"]


def test_unexpected_separator_at_start():
    resp = parse(f"{GS}01{GTIN_OK}")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNEXPECTED_SEPARATOR"
    assert detail["offset"] == 0


def test_truncated_fixed_field():
    resp = parse("0109501101")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_SHORT"
    assert "14" in detail["expected"]


def test_separator_inside_fixed_field():
    resp = parse(f"0109501{GS}17250131")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "FIELD_TOO_SHORT"


def test_gtin_bad_check_digit():
    resp = parse("0109501101530004")  # last digit should be 3
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHECK_DIGIT"
    assert detail["offset"] == 15  # byte offset of the check digit
    assert detail["found"] == "4"
    assert "3" in detail["expected"]


def test_gtin_non_digit():
    resp = parse("0109501101530A03")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 13
    assert detail["found"] == "A"


def test_invalid_month():
    resp = parse("17251331")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_DATE"
    assert detail["offset"] == 4


def test_invalid_day():
    resp = parse("17250230")  # Feb 30 does not exist
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_DATE"
    assert detail["offset"] == 6


def test_invalid_leap_day():
    assert parse("17250229").status_code == 422  # 2025 is not a leap year


# ----------------------------------------------- weight family 310n / 320n


def test_weight_kg_implied_three_decimals():
    resp = parse("3103002513")  # 002513 with 3 decimals -> 2.513 kg
    assert resp.status_code == 200
    element = resp.json()["elements"][0]
    assert element == {
        "ai": "3103",
        "name": "NET WEIGHT (KG)",
        "value": "002513",  # original 6 digits preserved
        "decimal_value": 2.513,
        "unit": "kg",
        "offset": 0,
    }


def test_weight_lb_implied_two_decimals():
    resp = parse("3202002513")  # 002513 with 2 decimals -> 25.13 lb
    assert resp.status_code == 200
    element = resp.json()["elements"][0]
    assert element["ai"] == "3202"
    assert element["name"] == "NET WEIGHT (LB)"
    assert element["value"] == "002513"
    assert element["decimal_value"] == 25.13
    assert element["unit"] == "lb"


def test_weight_n_zero_is_plain_integer():
    resp = parse("3100012345")
    assert resp.status_code == 200
    element = resp.json()["elements"][0]
    assert element["value"] == "012345"
    assert element["decimal_value"] == 12345.0
    assert element["unit"] == "kg"


def test_weight_n_six_places():
    resp = parse("3106123456")
    assert resp.json()["elements"][0]["decimal_value"] == 0.123456


def test_weight_n_above_six_implies_leading_fractional_zeros():
    # int("000001") / 10**9 = 0.000000001 (n-6 leading zeros are implied)
    resp = parse("3109000001")
    assert resp.json()["elements"][0]["decimal_value"] == 0.000000001
    resp = parse("3207000001")
    assert resp.json()["elements"][0]["decimal_value"] == 0.0000001


def test_weight_zero_value():
    for ai in ("3100", "3105", "3209"):
        resp = parse(f"{ai}000000")
        assert resp.status_code == 200
        element = resp.json()["elements"][0]
        assert element["value"] == "000000"
        assert element["decimal_value"] == 0.0
        assert element["unit"] == ("kg" if ai.startswith("310") else "lb")


def test_weight_fixed_field_needs_no_separator_after_fixed_fields():
    resp = parse(f"01{GTIN_OK}172501313101012345")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["01", "17", "3101"]
    weight = elements[-1]
    assert weight["offset"] == 24
    assert weight["value"] == "012345"
    assert weight["decimal_value"] == 1234.5
    assert weight["unit"] == "kg"


def test_weight_followed_by_variable_field_needs_no_separator():
    # Weight fields are self-delimiting by their fixed length, just like
    # AI 01/17, so a following variable-length field needs no separator.
    resp = parse("310212345610ABC")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["3102", "10"]
    assert elements[0]["decimal_value"] == 1234.56
    assert elements[0]["unit"] == "kg"
    assert elements[1]["value"] == "ABC"


def test_weight_separator_directly_after_fixed_field_rejected_like_01_17():
    # Same rule as the other fixed-length AIs: an FNC1 separator placed
    # right after the 6 digits is unexpected.
    resp = parse(f"3102123456{GS}10ABC")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNEXPECTED_SEPARATOR"
    assert detail["offset"] == 10


def test_weight_at_end_after_variable_field():
    resp = parse(f"21SN0001{GS}3203002513")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["21", "3203"]
    assert elements[1]["decimal_value"] == 2.513
    assert elements[1]["unit"] == "lb"
    assert elements[1]["offset"] == 9


def test_non_weight_elements_have_no_decimal_value_or_unit():
    resp = parse(f"01{GTIN_OK}17250131")
    for element in resp.json()["elements"]:
        assert "decimal_value" not in element
        assert "unit" not in element


def test_weight_four_char_ai_wins_longest_match():
    # "3110..." must not be read as weight (only prefixes 310/320 are);
    # it falls back to the 2-character AI "31", which is unsupported.
    resp = parse("3110000000")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNSUPPORTED_AI"
    assert detail["found"] == "31"
    assert detail["offset"] == 0


def test_weight_missing_precision_digit_element_string():
    resp = parse("310")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["offset"] == 3  # end of the raw input
    assert "310n" in detail["expected"]


def test_weight_missing_precision_before_separator():
    resp = parse(f"320{GS}17250131")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["offset"] == 3
    assert detail["found"] == "<GS>"


def test_weight_non_digit_precision_element_string():
    resp = parse("310A002513")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["offset"] == 3  # byte offset of the precision digit
    assert detail["found"] == "A"
    assert "precision" in detail["message"]


def test_weight_value_too_short_element_string():
    resp = parse("310300251")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_SHORT"
    assert detail["offset"] == 9  # end of raw input
    assert "6" in detail["expected"]


def test_weight_value_too_short_with_separator():
    resp = parse(f"31030025{GS}17250131")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_SHORT"
    assert detail["offset"] == 8  # byte offset of the separator
    assert detail["found"] == "<GS>"


def test_weight_non_digit_value_element_string():
    resp = parse("31030025A3")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 8  # byte offset of 'A'
    assert detail["found"] == "A"
    assert "3103" in detail["message"]


def test_weight_ai_offset_after_fixed_field():
    resp = parse("172501313203002513")
    assert resp.status_code == 200
    element = resp.json()["elements"][1]
    assert element["ai"] == "3203"
    assert element["offset"] == 8
    assert element["decimal_value"] == 2.513


def test_batch_too_long():
    resp = parse("10" + "A" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22  # offset of the 21st character


def test_batch_empty():
    resp = parse(f"10{GS}21ABC")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "FIELD_EMPTY"


def test_batch_invalid_character():
    resp = parse("10ABC#DEF")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 5
    assert detail["found"] == "#"


def test_missing_data_field():
    resp = client.post("/parse", json={})
    assert resp.status_code == 422


# ------------------------------------------------------- Digital Link URIs

DL_BASE = "https://id.gs1.org"


def test_digital_link_path_only():
    resp = parse(f"{DL_BASE}/01/{GTIN_OK}/17/250131/10/ABC123/21/SN0001")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["01", "17", "10", "21"]
    assert [e["value"] for e in elements] == [GTIN_OK, "250131", "ABC123", "SN0001"]
    assert [e["name"] for e in elements] == ["GTIN", "EXPIRY", "BATCH/LOT", "SERIAL"]


def test_digital_link_path_then_query_order():
    resp = parse(f"{DL_BASE}/01/{GTIN_OK}/17/250131?10=ABC123&21=SN0001")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["01", "17", "10", "21"]
    # offsets point at the AI characters inside the *original URI*
    assert [e["offset"] for e in elements] == [19, 37, 47, 57]


def test_digital_link_query_only():
    resp = parse(f"{DL_BASE}?01={GTIN_OK}&17=250131")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["01", "17"]


def test_digital_link_http_scheme_and_port():
    resp = parse(f"http://example.local:8080/01/{GTIN_OK}")
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["value"] == GTIN_OK


def test_digital_link_trailing_slash_accepted():
    resp = parse(f"{DL_BASE}/21/SN0001/")
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["ai"] == "21"


def test_digital_link_percent_decoded_value():
    resp = parse(f"{DL_BASE}/10/A%2FB%3CC%3E/21/x")
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["value"] == "A/B<C>"


def test_digital_link_plus_is_literal_in_query():
    # '+' is charset-82 legal; only application/x-www-form-urlencoded forms
    # treat it as space, which Digital Link does not.
    resp = parse(f"{DL_BASE}?10=A+B")
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["value"] == "A+B"


def test_digital_link_validation_reuses_element_rules():
    resp = parse(f"{DL_BASE}/01/09501101530004")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHECK_DIGIT"
    assert detail["offset"] == 35  # check digit byte offset in the URI

    resp = parse(f"{DL_BASE}/17/250230")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_DATE"
    assert detail["offset"] == 26  # first byte of the day component

    resp = parse(f"{DL_BASE}/01/{GTIN_OK}/17/240229")
    assert resp.status_code == 200


def test_digital_link_unknown_ai_in_path():
    resp = parse(f"{DL_BASE}/99/abc")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNSUPPORTED_AI"
    assert detail["offset"] == 19
    assert detail["found"] == "99"


def test_digital_link_unknown_longer_numeric_ai():
    resp = parse(f"{DL_BASE}/999/abc")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNSUPPORTED_AI"
    assert detail["found"] == "999"


def test_digital_link_unknown_ai_in_query():
    resp = parse(f"{DL_BASE}?99=abc")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNSUPPORTED_AI"
    assert "query" in detail["message"]


def test_digital_link_non_numeric_path_segment_as_ai():
    resp = parse(f"{DL_BASE}/gtin/{GTIN_OK}")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_AI"
    assert detail["offset"] == 19


def test_digital_link_duplicate_ai_in_path():
    resp = parse(f"{DL_BASE}/10/A/10/B")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "DUPLICATE_AI"
    assert detail["offset"] == 24  # second '10'


def test_digital_link_duplicate_ai_in_query():
    resp = parse(f"{DL_BASE}?10=A&10=B")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "DUPLICATE_AI"


def test_digital_link_ai_in_both_path_and_query():
    resp = parse(f"{DL_BASE}/10/A?10=B")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "DUPLICATE_AI"
    assert detail["offset"] == 24  # second '10' (after '?' at byte 23)
    assert "path" in detail["message"] and "query" in detail["message"]


def test_digital_link_unpaired_path_segment():
    resp = parse(f"{DL_BASE}/01/{GTIN_OK}/17")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "UNPAIRED_PATH"
    assert detail["offset"] == 37
    assert detail["found"] == "17"


def test_digital_link_no_pairs():
    resp = parse(f"{DL_BASE}/")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "EMPTY_INPUT"


def test_digital_link_empty_query_value():
    resp = parse(f"{DL_BASE}?10=")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "FIELD_EMPTY"


def test_digital_link_path_value_too_long():
    resp = parse(f"{DL_BASE}/10/{'A' * 21}")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "FIELD_TOO_LONG"


def test_digital_link_fixed_field_too_short():
    resp = parse(f"{DL_BASE}/01/12345")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_SHORT"
    assert detail["offset"] == 27  # end of the value segment


def test_digital_link_fixed_field_too_long():
    resp = parse(f"{DL_BASE}/01/{GTIN_OK}9")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 36  # 15th character of the value segment


def test_digital_link_query_separator_is_ampersand_only():
    # ';' is not a pair separator in GS1 Digital Link; only '&' splits
    # pairs, so "10%20;21" is read as one non-numeric key.
    resp = parse(f"{DL_BASE}?10%20;21=B")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_AI"
    assert detail["found"] == "10%20;21"


def test_digital_link_fragment_is_ignored():
    resp = parse(f"{DL_BASE}/21/SN0001#frag/10/x")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["21"]


def test_digital_link_encoded_slash_does_not_split_path():
    resp = parse(f"{DL_BASE}/21/a%2Fb")
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["value"] == "a/b"


def test_digital_link_percent_encoded_ai_key():
    resp = parse(f"{DL_BASE}/%32%31/SN0001")  # %32%31 == "21"
    assert resp.status_code == 200
    assert resp.json()["elements"][0]["ai"] == "21"


def test_digital_link_charset_error_offset_is_decoded_byte():
    resp = parse(f"{DL_BASE}/10/AB%23C")  # '#' not in character set 82
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["found"] == "#"
    assert detail["offset"] == 24  # byte offset of '%' in the raw URI


def test_digital_link_bad_percent_encoding():
    resp = parse(f"{DL_BASE}/10/AB%ZZ")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == 24


def test_digital_link_truncated_percent_encoding():
    resp = parse(f"{DL_BASE}/10/AB%2")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "INVALID_PERCENT_ENCODING"


def test_digital_link_invalid_utf8_percent_sequence():
    resp = parse(f"{DL_BASE}/10/%FF")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == 22


def test_digital_link_utf8_multibyte_char_offsets():
    # "café" percent-encoded: caf%C3%A9 ; 'é' is outside charset 82 and its
    # raw location is the '%' of %C3.
    resp = parse(f"{DL_BASE}/10/caf%C3%A9")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 25


def test_digital_link_non_uri_still_parsed_as_element_string():
    # must not start with http:// or https:// to be treated as a URI
    assert parse("21httpsniffed").status_code == 200
    resp = parse(f"01{GTIN_OK}17250131")
    assert resp.status_code == 200


# --------------------------------- Digital Link: weight family 310n / 320n


def test_digital_link_weight_in_path():
    resp = parse(f"{DL_BASE}/01/{GTIN_OK}/3103/002513")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["01", "3103"]
    weight = elements[1]
    assert weight["value"] == "002513"
    assert weight["decimal_value"] == 2.513
    assert weight["unit"] == "kg"
    assert weight["offset"] == 37  # first byte of "3103" in the raw URI


def test_digital_link_weight_in_query():
    resp = parse(f"{DL_BASE}?3202=002513")
    assert resp.status_code == 200
    element = resp.json()["elements"][0]
    assert element == {
        "ai": "3202",
        "name": "NET WEIGHT (LB)",
        "value": "002513",
        "decimal_value": 25.13,
        "unit": "lb",
        "offset": 19,  # byte offset of the query key "3202"
    }


def test_digital_link_weight_path_and_query_same_validation():
    resp = parse(f"{DL_BASE}/3101/012345?3204=002513")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["ai"] for e in elements] == ["3101", "3204"]
    assert elements[0]["decimal_value"] == 1234.5
    assert elements[0]["unit"] == "kg"
    assert elements[1]["decimal_value"] == 0.2513
    assert elements[1]["unit"] == "lb"


def test_digital_link_weight_missing_precision_path():
    resp = parse(f"{DL_BASE}/310/002513")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["offset"] == 22  # byte just after "310" in the raw URI
    assert detail["found"] == "310"
    assert "precision" in detail["message"]


def test_digital_link_weight_missing_precision_query():
    resp = parse(f"{DL_BASE}?320=002513")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["found"] == "320"


def test_digital_link_weight_non_digit_precision():
    resp = parse(f"{DL_BASE}/310x/002513")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["offset"] == 22  # the "x" inside the raw URI
    assert detail["found"] == "x"


def test_digital_link_weight_value_too_short():
    resp = parse(f"{DL_BASE}/3103/00251")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_SHORT"
    assert detail["offset"] == 29  # end of the raw value segment


def test_digital_link_weight_value_too_long():
    resp = parse(f"{DL_BASE}/3103/0025136")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 30  # 7th digit of the value segment


def test_digital_link_weight_non_digit_value():
    resp = parse(f"{DL_BASE}/3103/0025A3")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 28  # raw byte offset of 'A'
    assert detail["found"] == "A"


def test_digital_link_weight_duplicate_same_ai_rejected():
    resp = parse(f"{DL_BASE}/3103/002513?3103=000001")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "DUPLICATE_AI"


def test_digital_link_weight_kg_and_lb_are_distinct_ais():
    # 3103 and 3203 differ by family, not precision, so both are allowed.
    resp = parse(f"{DL_BASE}/3103/002513/3203/002513")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert [e["unit"] for e in elements] == ["kg", "lb"]


def test_digital_link_weight_percent_decoded_precision_digit():
    # "%33" == "3", so "310%33" decodes to AI "3103" (precision digit encoded)
    resp = parse(f"{DL_BASE}/310%33/002513")
    assert resp.status_code == 200
    element = resp.json()["elements"][0]
    assert element["ai"] == "3103"
    assert element["decimal_value"] == 2.513


def test_digital_link_weight_percent_encoded_bad_precision_offset():
    # "%41" == "A"; the error offset points at the '%' of its encoding.
    resp = parse(f"{DL_BASE}/310%41/002513")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PRECISION"
    assert detail["found"] == "A"
    assert detail["offset"] == 22
