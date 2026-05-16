"""Unit tests for local_dictation.transcribe.clean_text."""

from __future__ import annotations

import unittest

from local_dictation.transcribe import clean_text


class CleanTextTests(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(clean_text(""), "")

    def test_only_whitespace(self) -> None:
        self.assertEqual(clean_text("   \n\t \n  "), "")

    def test_preserves_danish_chars_and_punctuation(self) -> None:
        s = "Æbler, øl og åre! Hvad så? Det er fint."
        self.assertEqual(clean_text(s), s)

    def test_collapses_four_newlines(self) -> None:
        self.assertEqual(clean_text("a\n\n\n\nb"), "a\n\nb")

    def test_collapses_five_newlines(self) -> None:
        self.assertEqual(clean_text("a\n\n\n\n\nb"), "a\n\nb")

    def test_leaves_single_newline_alone(self) -> None:
        self.assertEqual(clean_text("a\nb"), "a\nb")

    def test_leaves_one_blank_line_alone(self) -> None:
        self.assertEqual(clean_text("a\n\nb"), "a\n\nb")

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        self.assertEqual(clean_text("   hello world   "), "hello world")
        self.assertEqual(clean_text("\n\n  hi  \n\n"), "hi")
        self.assertEqual(clean_text("\t\n hej \n\t"), "hej")

    def test_preserves_inline_whitespace(self) -> None:
        self.assertEqual(clean_text("a   b\tc"), "a   b\tc")

    def test_collapses_runs_inside_text(self) -> None:
        # Multiple runs of >=3 newlines each become a single blank line.
        self.assertEqual(
            clean_text("para1\n\n\npara2\n\n\n\npara3"),
            "para1\n\npara2\n\npara3",
        )


if __name__ == "__main__":
    unittest.main()
