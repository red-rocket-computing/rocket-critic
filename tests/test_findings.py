import json

import pytest

from rocket_critic.findings import (Finding, FindingsError, build_review, commentable_lines, extract_json,
                                    normalize_severity, parse_findings)

DIFF = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,5 @@
 import os
-x = 1
+x = 2
+y = 3

 def main():
@@ -20,2 +21,3 @@ def other():
     pass
+    return 1
\\ No newline at end of file
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-print('bye')
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
"""


def doc(*findings, summary="Looks fine."):
    return json.dumps({"summary": summary, "findings": list(findings)})


def item(**kw):
    base = {"path": "app.py", "line": 2, "severity": "important", "title": "Wrong value", "body": "Use 3."}
    base.update(kw)
    return base


def test_normalize_severity():
    assert normalize_severity("Important") == "important"
    assert normalize_severity(" CRITICAL ") == "important"
    assert normalize_severity("nitpick") == "nit"
    assert normalize_severity("Pre-existing") == "pre-existing"
    assert normalize_severity("pre_existing") == "pre-existing"
    assert normalize_severity("pre existing") == "pre-existing"
    for bad in ("medium", "warning", "", None, 3):
        assert normalize_severity(bad) is None


def test_parse_findings_cleans_up():
    summary, findings = parse_findings(doc(
        item(title="\U0001F534 **Important**: Wrong value", line="2"),
        item(path="./new.py", line=2, start_line=1, severity="minor", title="Nit - naming", body="b"),
        item(start_line=5, line=3),
    ))
    assert summary == "Looks fine."
    assert findings[0] == Finding("app.py", 2, "important", "Wrong value", "Use 3.")
    assert findings[1] == Finding("new.py", 2, "nit", "naming", "b", start_line=1)
    assert findings[2].start_line is None  # a start after the end is dropped, not an error
    assert parse_findings(json.dumps([item()]))[1][0].severity == "important"  # bare list accepted


def test_parse_findings_rejects_the_whole_document():
    with pytest.raises(FindingsError) as exc:
        parse_findings(doc(item(), item(severity="medium"), item(path="", line=0), item(title="", body="")))
    msg = str(exc.value)
    assert "finding 2 (app.py): severity 'medium' is not one of important, nit, pre-existing" in msg
    assert "finding 3 (?): no path; line 0 is not a positive number" in msg
    assert "finding 4 (app.py): no title or body" in msg
    assert "finding 1" not in msg
    for bad in ("not json", "{}", '{"findings": {}}', '{"findings": [], "summary": 3}'):
        with pytest.raises(FindingsError):
            parse_findings(bad)


def test_extract_json():
    text = 'Done.\n```json\n{"summary": "s", "findings": []}\n```\n'
    assert json.loads(extract_json(text)) == {"summary": "s", "findings": []}
    assert extract_json("```\nnot it\n```") is None


def test_commentable_lines():
    lines = commentable_lines(DIFF)
    assert lines["app.py"] == {1, 2, 3, 4, 5, 21, 22}
    assert lines["new.py"] == {1, 2}
    assert "gone.py" not in lines


def test_build_review_places_findings():
    findings = [
        Finding("app.py", 3, "important", "Wrong value", "Use 3."),
        Finding("app.py", 3, "nit", "Name", "", start_line=2),
        Finding("app.py", 22, "nit", "Range across hunks", "", start_line=5),
        Finding("old.py", 9, "pre-existing", "Leak", "Close it.\nAlways."),
    ]
    review = build_review("Summary.", findings, commentable_lines(DIFF), "abc")
    p = review.payload
    assert p["commit_id"] == "abc" and p["event"] == "COMMENT"
    assert [c["body"] for c in p["comments"]] == [
        "\U0001F534 **Important**: Wrong value\n\nUse 3.",
        "\U0001F7E1 **Nit**: Name",
        "\U0001F7E1 **Nit**: Range across hunks",
    ]
    assert p["comments"][0] == {"path": "app.py", "line": 3, "side": "RIGHT", "body": p["comments"][0]["body"]}
    assert p["comments"][1]["start_line"] == 2 and p["comments"][1]["start_side"] == "RIGHT"
    assert "start_line" not in p["comments"][2]  # 6..21 aren't all in the diff
    assert "1 \U0001F534 Important · 2 \U0001F7E1 Nit · 1 \U0001F7E3 Pre-existing" in p["body"]
    assert "Summary." in p["body"]
    assert "### Findings outside the diff" in p["body"]
    assert "- \U0001F7E3 **Pre-existing** `old.py:9`: Leak\n\n  Close it.\n  Always." in p["body"]
    assert len(review.inline) == 3 and len(review.outside) == 1


def test_build_review_holds_nits_and_falls_back_to_body():
    findings = [Finding("app.py", 3, "important", "Bug", ""), Finding("app.py", 2, "nit", "Name", "")]
    review = build_review("", findings, commentable_lines(DIFF), "abc", post_nits=False)
    assert len(review.payload["comments"]) == 1 and review.nits_held == 1
    assert "1 \U0001F7E1 Nit finding(s) not posted." in review.payload["body"]
    assert "0 \U0001F7E1 Nit" in review.payload["body"]

    flat = build_review("", findings, commentable_lines(DIFF), "abc", inline=False)
    assert flat.payload["comments"] == [] and "### Findings\n" in flat.payload["body"]
    assert "`app.py:3`: Bug" in flat.payload["body"]

    empty = build_review("Nothing to report.", [], {}, "abc")
    assert "No issues found." in empty.payload["body"] and empty.payload["comments"] == []
