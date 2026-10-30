import unittest

from notifyd.sanitize import body_runs, safe_href


def plain(runs):
    return "".join(r["text"] for r in runs)


class Basics(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(body_runs(""), [])

    def test_plain_text_has_no_flags(self):
        self.assertEqual(body_runs("hello"), [{"text": "hello"}])

    def test_flags_only_when_set(self):
        runs = body_runs("a<b>b</b><i>c</i><u>d</u>")
        self.assertEqual(
            runs,
            [
                {"text": "a"},
                {"text": "b", "b": True},
                {"text": "c", "i": True},
                {"text": "d", "u": True},
            ],
        )

    def test_br_becomes_newline(self):
        self.assertEqual(plain(body_runs("a<br>b<br/>c")), "a\nb\nc")


class Nesting(unittest.TestCase):
    def test_nested_flags_combine(self):
        runs = body_runs("<b>x<i>y</i>z</b>")
        self.assertEqual(
            runs,
            [
                {"text": "x", "b": True},
                {"text": "y", "b": True, "i": True},
                {"text": "z", "b": True},
            ],
        )

    def test_misnested_closes(self):
        runs = body_runs("<b>bold <i>both</b> italic</i> end")
        self.assertEqual(plain(runs), "bold both italic end")
        self.assertEqual(runs[2], {"text": " italic", "i": True})
        self.assertEqual(runs[3], {"text": " end"})

    def test_stray_close_ignored(self):
        self.assertEqual(body_runs("a</b></a>b"), [{"text": "ab"}])

    def test_nested_same_tag(self):
        runs = body_runs("<b><b>x</b>y</b>z")
        self.assertEqual(runs, [{"text": "xy", "b": True}, {"text": "z"}])

    def test_adjacent_equal_runs_merge(self):
        self.assertEqual(body_runs("a<span>b</span>c"), [{"text": "abc"}])


class Unclosed(unittest.TestCase):
    def test_unclosed_flag_runs_to_end(self):
        self.assertEqual(body_runs("<b>x"), [{"text": "x", "b": True}])

    def test_unclosed_link(self):
        self.assertEqual(
            body_runs('<a href="https://e.com">go'), [{"text": "go", "href": "https://e.com"}]
        )

    def test_dangling_open_bracket_keeps_text(self):
        for s in ["x<y", "1 <", "<a href", "<b", "a <b c", "<!-- never closed"]:
            self.assertEqual(plain(body_runs(s)), s, s)

    def test_unterminated_comment_with_later_gt(self):
        self.assertEqual(plain(body_runs("<!-- a > b")), "<!-- a > b")

    def test_comment_dropped_when_closed(self):
        self.assertEqual(body_runs("a<!-- hidden -->b"), [{"text": "ab"}])

    def test_never_raises(self):
        for s in [
            "<",
            ">",
            "<<<>>>",
            "<a href=",
            "&",
            "&#xZZ;",
            "<\x00>",
            "<!DOCTYPE",
            "<![CDATA[x",
        ]:
            body_runs(s)


class Links(unittest.TestCase):
    def test_allowed_schemes(self):
        for url in ["http://a.b/c", "https://a.b/?q=1&r=2", "mailto:me@x.org", "HTTPS://A.B"]:
            runs = body_runs(f'<a href="{url}">t</a>')
            self.assertEqual(runs, [{"text": "t", "href": url.replace("&amp;", "&")}], url)

    def test_javascript_becomes_plain_text(self):
        self.assertEqual(body_runs('<a href="javascript:alert(1)">click</a>'), [{"text": "click"}])

    def test_obfuscated_javascript(self):
        for href in [
            "JaVaScRiPt:x",
            " javascript:x",
            "java\tscript:x",
            "java\nscript:x",
            "\x01javascript:x",
        ]:
            self.assertIsNone(safe_href(href), repr(href))

    def test_other_schemes_and_relative(self):
        for href in ["data:text/html,x", "file:///etc/passwd", "ftp://x", "//evil.com", "/p", "x"]:
            self.assertEqual(body_runs(f'<a href="{href}">t</a>'), [{"text": "t"}], href)

    def test_missing_href(self):
        self.assertEqual(body_runs("<a>t</a>"), [{"text": "t"}])

    def test_link_with_formatting(self):
        runs = body_runs('<a href="https://e.com"><b>x</b>y</a>')
        self.assertEqual(
            runs,
            [
                {"text": "x", "b": True, "href": "https://e.com"},
                {"text": "y", "href": "https://e.com"},
            ],
        )

    def test_text_after_link_has_no_href(self):
        self.assertEqual(body_runs('<a href="https://e.com">a</a>b')[1], {"text": "b"})

    def test_inner_bad_link_does_not_inherit_outer(self):
        runs = body_runs('<a href="https://e.com"><a href="javascript:x">in</a>out</a>')
        self.assertEqual(runs[0], {"text": "in"})
        self.assertEqual(runs[1], {"text": "out", "href": "https://e.com"})

    def test_attribute_injection_is_inert(self):
        runs = body_runs('<a href="https://e.com" onclick="evil()">t</a>')
        self.assertEqual(runs, [{"text": "t", "href": "https://e.com"}])


class Entities(unittest.TestCase):
    def test_named_and_numeric(self):
        self.assertEqual(
            plain(body_runs("&amp; &lt; &gt; &quot; &#65; &#x42; &copy;")), '& < > " A B ©'
        )

    def test_escaped_tags_stay_text(self):
        self.assertEqual(body_runs("&lt;b&gt;not bold&lt;/b&gt;"), [{"text": "<b>not bold</b>"}])

    def test_entity_in_href(self):
        runs = body_runs('<a href="https://e.com/?a=1&amp;b=2">t</a>')
        self.assertEqual(runs[0]["href"], "https://e.com/?a=1&b=2")


class Dropped(unittest.TestCase):
    def test_img_dropped(self):
        self.assertEqual(body_runs('a<img src="file:///x.png" alt="pic">b'), [{"text": "ab"}])

    def test_unknown_tags_stripped_text_kept(self):
        self.assertEqual(body_runs("<span style='x'>a</span><div>b</div><p>c"), [{"text": "abc"}])

    def test_script_and_style_content_is_text_only(self):
        runs = body_runs("<script>alert(1)</script>x<style>p{color:red}</style>")
        self.assertEqual(runs, [{"text": "alert(1)xp{color:red}"}])
        self.assertTrue(all(set(r) == {"text"} for r in runs))

    def test_unclosed_script_keeps_text(self):
        self.assertEqual(plain(body_runs("a<script>b")), "ab")

    def test_event_handlers_never_surface(self):
        runs = body_runs('<b onmouseover="x()">t</b>')
        self.assertEqual(runs, [{"text": "t", "b": True}])


if __name__ == "__main__":
    unittest.main()
