import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import job_alerts as ja  # noqa: E402

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:job_listing="https://example.org">
<channel><title>Jobs</title>
<item>
  <title>Data Visualization Consultant &#8211; Farm Action</title>
  <link>https://example.org/job/farm-action/</link>
  <pubDate>Tue, 06 Oct 2026 21:20:19 +0000</pubDate>
  <guid isPermaLink="false">https://example.org/?p=1</guid>
  <description><![CDATA[Short excerpt [...]]]></description>
  <content:encoded><![CDATA[<p>Remote engagement, beginning as soon as possible.</p>]]></content:encoded>
  <job_listing:location><![CDATA[Anywhere]]></job_listing:location>
  <job_listing:job_type><![CDATA[Contract]]></job_listing:job_type>
  <job_listing:company><![CDATA[Farm Action]]></job_listing:company>
</item>
<item>
  <title>IT Specialist</title>
  <link>https://example.org/job/treasury/</link>
  <guid isPermaLink="false">https://example.org/?p=2</guid>
  <content:encoded><![CDATA[<p>This is not a remote or hybrid role.</p>]]></content:encoded>
</item>
<item>
  <title>Bad link</title>
  <link>javascript:alert(1)</link>
</item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Graphics Developer</title>
    <id>tag:example.org,2026:7</id>
    <link rel="alternate" href="https://example.org/jobs/7"/>
    <updated>2026-10-20T12:00:00Z</updated>
    <summary>Fully remote position.</summary>
  </entry>
</feed>"""


def remote(job):
    return ja.is_remote(job.text, ja.DEFAULT_INCLUDE, ja.DEFAULT_EXCLUDE)


class ParseTests(unittest.TestCase):
    def test_rss_fields_and_unsafe_link_dropped(self):
        jobs = ja.parse_feed(RSS, "Test")
        self.assertEqual([j.url for j in jobs],
                         ["https://example.org/job/farm-action/", "https://example.org/job/treasury/"])
        first = jobs[0]
        self.assertEqual(first.company, "Farm Action")
        self.assertEqual(first.location, "Anywhere")
        self.assertEqual(first.job_type, "Contract")
        self.assertEqual(first.uid, "https://example.org/?p=1")
        self.assertEqual(first.published.year, 2026)

    def test_atom_entry(self):
        (job,) = ja.parse_feed(ATOM, "Atom")
        self.assertEqual(job.url, "https://example.org/jobs/7")
        self.assertEqual(job.uid, "tag:example.org,2026:7")


class FilterTests(unittest.TestCase):
    def test_negated_remote_is_excluded_and_real_remote_kept(self):
        farm, treasury = ja.parse_feed(RSS, "Test")
        self.assertTrue(remote(farm))
        self.assertFalse(remote(treasury))

    def test_hybrid_and_in_person_excluded(self):
        for text in ("Remote or hybrid", "Remote but in-person onboarding", "#LI-Hybrid remote"):
            self.assertFalse(ja.is_remote(text, ja.DEFAULT_INCLUDE, ja.DEFAULT_EXCLUDE), text)

    def test_no_signal_is_not_remote(self):
        self.assertFalse(ja.is_remote("Analyst in Washington, DC", ja.DEFAULT_INCLUDE, ja.DEFAULT_EXCLUDE))

    def test_remote_synonyms(self):
        for text in ("This is a WFH role", "Telecommuting available", "Work from anywhere",
                     "Home-based position", "Location independent team", "Fully distributed team",
                     "Open to candidates anywhere in the US", "Virtual position"):
            self.assertTrue(ja.is_remote(text, ja.DEFAULT_INCLUDE, ja.DEFAULT_EXCLUDE), text)

    def test_negated_synonym_is_excluded(self):
        self.assertFalse(ja.is_remote("Telework is not available; onsite only", ja.DEFAULT_INCLUDE, ja.DEFAULT_EXCLUDE))


class KeywordTests(unittest.TestCase):
    WWR = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Acme: Data Visualization Engineer</title><link>https://example.org/a</link>
<region>Anywhere in the World</region><category>Programming</category>
<description>Build charts.</description></item>
<item><title>Acme: Sales Rep</title><link>https://example.org/b</link>
<description>Sell things. Familiar with data visualization tools a plus.</description></item>
<item><title>Acme: Support Agent</title><link>https://example.org/c</link>
<description>Answer tickets.</description></item>
</channel></rss>"""

    def setUp(self):
        self.jobs = ja.parse_feed(self.WWR, "WWR")

    def test_no_keywords_keeps_everything(self):
        self.assertTrue(all(ja.matches_keywords(j, []) for j in self.jobs))

    def test_keywords_match_title_or_summary_case_insensitively(self):
        kept = [j.url for j in self.jobs if ja.matches_keywords(j, ["data[- ]?visuali[sz]ation"])]
        self.assertEqual(kept, ["https://example.org/a", "https://example.org/b"])

    def test_non_matching_keywords_drop_everything(self):
        self.assertFalse(any(ja.matches_keywords(j, ["cartograph"]) for j in self.jobs))


class GoogleAlertTests(unittest.TestCase):
    FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>tag:google.com,2013:googlealerts/feed:1</id>
<title type="html">&lt;b&gt;Remote&lt;/b&gt; Data Visualization Engineer</title>
<link href="https://www.google.com/url?rct=j&amp;sa=t&amp;url=https://boards.greenhouse.io/acme/jobs/1&amp;ct=ga"/>
<published>2026-10-31T07:53:58Z</published>
<content type="html">&lt;b&gt;Remote&lt;/b&gt; role building charts.</content>
</entry></feed>"""

    def test_redirect_unwrapped_and_title_cleaned(self):
        (job,) = ja.parse_feed(self.FEED, "G")
        self.assertEqual(job.url, "https://boards.greenhouse.io/acme/jobs/1")
        self.assertEqual(job.title, "Remote Data Visualization Engineer")
        self.assertTrue(remote(job))

    def test_non_redirect_google_url_left_alone(self):
        self.assertEqual(ja.unwrap_redirect("https://example.org/x"), "https://example.org/x")

    def test_blocked_domains_and_suffixes(self):
        self.assertTrue(ja.is_blocked("https://jobhaven.shop/a", [".shop"]))
        self.assertTrue(ja.is_blocked("https://www.spam.com/a", ["spam.com"]))
        self.assertFalse(ja.is_blocked("https://notspam.com/a", ["spam.com"]))
        self.assertFalse(ja.is_blocked("https://example.org/a", []))


class DuplicateTests(unittest.TestCase):
    def job(self, url, feed="F"):
        return ja.Job(uid=url, title="T", url=url, company="", location="", job_type="",
                      published=None, summary="", text="T", feed_name=feed)

    def test_same_destination_variants_share_a_key(self):
        base = ja.canonical_url("https://boards.greenhouse.io/acme/jobs/1?gh_jid=1")
        for variant in ("http://www.boards.greenhouse.io/acme/jobs/1/?gh_jid=1&utm_source=x#apply",
                        "https://BOARDS.greenhouse.io/acme/jobs/1?utm_medium=a&gh_jid=1"):
            self.assertEqual(ja.canonical_url(variant), base)

    def test_different_ids_stay_distinct(self):
        self.assertNotEqual(ja.canonical_url("https://x.org/jobs?gh_jid=1"), ja.canonical_url("https://x.org/jobs?gh_jid=2"))

    def test_first_feed_wins_and_sent_urls_are_skipped(self):
        a = self.job("https://x.org/job/1", "Direct")
        b = self.job("https://www.x.org/job/1/?utm_source=g", "Google")
        c = self.job("https://x.org/job/2")
        d = self.job("https://x.org/job/3")
        kept = ja.drop_duplicates([a, b, c, d], [ja.canonical_url(d.url)])
        self.assertEqual([j.feed_name for j in kept], ["Direct", "F"])
        self.assertEqual([j.url for j in kept], [a.url, c.url])


class SelectNewTests(unittest.TestCase):
    def setUp(self):
        self.jobs = ja.parse_feed(RSS, "Test")
        self.matches = [j for j in self.jobs if remote(j)]

    def test_first_run_seeds_without_sending(self):
        new, seen, seeded = ja.select_new(self.jobs, None, False, self.matches)
        self.assertEqual(new, [])
        self.assertTrue(seeded)
        self.assertEqual(len(seen), len(self.jobs))

    def test_backlog_flag_sends_current_matches(self):
        new, _, seeded = ja.select_new(self.jobs, None, True, self.matches)
        self.assertEqual(len(new), 1)
        self.assertFalse(seeded)

    def test_only_unseen_matches_are_new(self):
        new, _, _ = ja.select_new(self.jobs, [], False, self.matches)
        self.assertEqual(len(new), 1)
        new, _, _ = ja.select_new(self.jobs, [self.matches[0].uid], False, self.matches)
        self.assertEqual(new, [])

    def test_seen_list_is_capped_and_keeps_current_first(self):
        old = [f"old-{i}" for i in range(ja.MAX_SEEN_PER_FEED + 50)]
        _, seen, _ = ja.select_new(self.jobs, old, False, self.matches)
        self.assertEqual(len(seen), ja.MAX_SEEN_PER_FEED)
        self.assertEqual(seen[: len(self.jobs)], [j.uid for j in self.jobs])


class EmailTests(unittest.TestCase):
    def test_message_has_listing_link_and_escapes_markup(self):
        job = ja.parse_feed(RSS, "Test")[0]
        job.title = "<script>alert(1)</script> Role"
        msg = ja.build_message([job], "from@example.org", "to@example.org")
        html_part = msg.get_body(("html",)).get_content()
        plain_part = msg.get_body(("plain",)).get_content()
        self.assertNotIn("<script>", html_part)
        self.assertIn("&lt;script&gt;", html_part)
        self.assertIn('href="https://example.org/job/farm-action/"', html_part)
        self.assertIn("https://example.org/job/farm-action/", plain_part)
        self.assertEqual(msg["To"], "to@example.org")

    def test_subject_counts_multiple_jobs(self):
        jobs = ja.parse_feed(RSS, "Test")
        msg = ja.build_message(jobs, "a@example.org", "b@example.org")
        self.assertEqual(msg["Subject"], "2 new remote data viz jobs")


if __name__ == "__main__":
    unittest.main()
