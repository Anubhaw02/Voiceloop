from brand_agent.linkedin_format import to_linkedin_commentary, unicode_bold


def test_reserved_characters_are_escaped():
    body = r"Latency (p99) fell 37% [vs vLLM] for user_a ~ approx <fast> | a*b @team {x} \ end"
    expected = r"Latency \(p99\) fell 37% \[vs vLLM\] for user\_a \~ approx \<fast\> \| a\*b \@team \{x\} \\ end"
    assert to_linkedin_commentary(body) == expected


def test_bold_markers_become_unicode_bold_and_are_not_escaped():
    out = to_linkedin_commentary("**What's new** in v2")
    assert out == unicode_bold("What's new") + " in v2"
    assert unicode_bold("Ab9") == "\U0001D5D4\U0001D5EF\U0001D7F5"
    assert "*" not in out


def test_hashtags_become_clickable_templates():
    out = to_linkedin_commentary("Read more #CloudNative #SRE, not a#tag or C# or https://x.y/#anchor")
    assert out == (
        r"Read more {hashtag|\#|CloudNative} {hashtag|\#|SRE}, not a\#tag or C\# or https://x.y/\#anchor"
    )


def test_plain_text_is_unchanged():
    text = "Agents fail at repair. 41.3/100 is the best score.\n\n→ Diagnosis works\n→ Repair does not"
    assert to_linkedin_commentary(text) == text
