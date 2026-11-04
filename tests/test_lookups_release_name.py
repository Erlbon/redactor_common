"""Tests for core/release_name_parser.py -- pure string/regex logic, fully runnable."""

import unittest

from redactor_common.core.lookups.release_name import parse_release_name


class TestTvNames(unittest.TestCase):
    def test_standard_sxxexx(self):
        g = parse_release_name("Breaking.Bad.S01E05.720p.WEB-DL.x264-GROUP")
        self.assertEqual(g.kind, "tv")
        self.assertEqual(g.title, "Breaking Bad")
        self.assertEqual(g.season, 1)
        self.assertEqual(g.episode, 5)
        self.assertEqual(g.extra_episodes, [])

    def test_underscores_and_mixed_case(self):
        g = parse_release_name("the_mandalorian_s02e08_1080p_hevc")
        self.assertEqual(g.kind, "tv")
        self.assertEqual(g.title, "the mandalorian")
        self.assertEqual(g.season, 2)
        self.assertEqual(g.episode, 8)

    def test_multi_episode_file(self):
        g = parse_release_name("Some.Show.S03E01E02.1080p.WEBRip")
        self.assertEqual(g.kind, "tv")
        self.assertEqual(g.season, 3)
        self.assertEqual(g.episode, 1)
        self.assertEqual(g.extra_episodes, [2])

    def test_nxnn_style(self):
        g = parse_release_name("Frasier - 4x11 - Odd Man Out")
        self.assertEqual(g.kind, "tv")
        self.assertEqual(g.title, "Frasier")
        self.assertEqual(g.season, 4)
        self.assertEqual(g.episode, 11)

    def test_spelled_out_season_episode(self):
        g = parse_release_name("The Office Season 4 Episode 2")
        self.assertEqual(g.kind, "tv")
        self.assertEqual(g.title, "The Office")
        self.assertEqual(g.season, 4)
        self.assertEqual(g.episode, 2)

    def test_trailing_release_group_stripped_from_title(self):
        g = parse_release_name("Show.Name.S01E01.1080p.BluRay.x264-RARBG")
        self.assertEqual(g.title, "Show Name")


class TestMovieNames(unittest.TestCase):
    def test_standard_movie_with_year(self):
        g = parse_release_name("The.Matrix.1999.1080p.BluRay.x264-GROUP")
        self.assertEqual(g.kind, "movie")
        self.assertEqual(g.title, "The Matrix")
        self.assertEqual(g.year, "1999")

    def test_movie_with_underscores(self):
        g = parse_release_name("Inception_2010_720p_BRRip_XviD")
        self.assertEqual(g.kind, "movie")
        self.assertEqual(g.title, "Inception")
        self.assertEqual(g.year, "2010")

    def test_movie_year_in_parens(self):
        g = parse_release_name("Parasite (2019) [1080p]")
        self.assertEqual(g.kind, "movie")
        self.assertEqual(g.title, "Parasite")
        self.assertEqual(g.year, "2019")

    def test_no_junk_tags_still_finds_year(self):
        g = parse_release_name("Arrival 2016")
        self.assertEqual(g.kind, "movie")
        self.assertEqual(g.title, "Arrival")
        self.assertEqual(g.year, "2016")


class TestFallback(unittest.TestCase):
    def test_no_year_no_episode_marker(self):
        g = parse_release_name("My.Home.Movie.Clip.mp4".rsplit(".", 1)[0])
        self.assertEqual(g.kind, "unknown")
        self.assertTrue(g.title)

    def test_never_raises_on_empty_input(self):
        g = parse_release_name("")
        self.assertEqual(g.kind, "unknown")

    def test_junk_tags_stripped_in_fallback(self):
        g = parse_release_name("Some.Clip.1080p.x264")
        self.assertNotIn("1080p", g.title)
        self.assertNotIn("x264", g.title)


if __name__ == "__main__":
    unittest.main()
