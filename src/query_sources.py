"""Search queries from public feeds instead of a language model.

The LLM in this project has one job: produce short strings to type into Bing.
That is worth an Ollama account and a model download if you want it, but it is
not the only way to get a search query, and it is the piece that stops someone
running the bot in five minutes.

Three keyless sources, all stdlib, no new dependencies:

  Google Trends RSS   real queries people are typing right now
  Wikipedia most-read topic seeds, useful when trends is unavailable
  Bing autosuggest    expands a seed into related queries

Autosuggest is what makes the chaining work. Asking Bing what follows a term
gives queries Bing itself expects, which is closer to what the LLM prompt was
reaching for than a model guessing in the dark.

Every source degrades rather than raises. A search that does not happen costs
points; a run that dies costs the rest of the day's points too.
"""

import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import paths

TRENDS_URL = "https://trends.google.com/trending/rss?geo={geo}"
WIKIPEDIA_URL = "https://en.wikipedia.org/api/rest_v1/feed/featured/{y}/{m:02d}/{d:02d}"
AUTOSUGGEST_URL = "https://api.bing.com/osjson.aspx?query={query}"

# A browser agent: trends and the Wikipedia REST feed both answer differently
# to an unfamiliar client, and one of them refuses outright.
USER_AGENT = (
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
	"(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 15

# Words that make a query read as an instruction rather than a search. The
# task descriptions are phrased at the user, "Search on Bing to compare
# checking accounts", and typing that verbatim searches for the sentence.
INSTRUCTION_WORDS = {
	"search", "searching", "bing", "on", "to", "the", "a", "an", "for", "your",
	"you", "use", "using", "find", "get", "with", "and", "or", "of", "in",
	"at", "by", "now", "today", "this", "that", "these", "those", "learn",
	"discover", "explore", "check", "see", "our", "more", "about", "how",
}


def _fetch(url: str) -> str | None:
	"""Body of a GET, or None. Never raises: callers fall through to the next source."""
	request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

	try:
		with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
			return response.read().decode("utf-8", "replace")
	except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
		return None


def trending_queries(geo: str = "US") -> list[str]:
	"""Queries currently trending on Google, most popular first.

	These are real searches rather than descriptions of searches, which is
	exactly the shape wanted here.
	"""
	body = _fetch(TRENDS_URL.format(geo=urllib.parse.quote(geo)))

	if not body:
		return []

	# The channel carries a <title> of its own before any item, so the first
	# match is the feed name rather than a query.
	titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", body, re.S)

	return [_clean(t) for t in titles[1:] if _clean(t)]


def wikipedia_topics(days_ago: int = 1) -> list[str]:
	"""Most-read Wikipedia articles, as topic seeds.

	Yesterday by default: today's feed is not published until the day is over.
	"""
	day = date.today() - timedelta(days=days_ago)
	body = _fetch(WIKIPEDIA_URL.format(y=day.year, m=day.month, d=day.day))

	if not body:
		return []

	try:
		payload = json.loads(body)
	except json.JSONDecodeError:
		return []

	articles = payload.get("mostread", {}).get("articles", [])
	titles = [a.get("titles", {}).get("normalized", "") for a in articles]

	# Wikipedia's own chrome outranks real topics most days.
	skipped = ("Main Page", "Special:", "Wikipedia:", "Portal:")

	return [
		_clean(t) for t in titles
		if t and not t.startswith(skipped) and _clean(t)
	]


def suggestions(seed: str) -> list[str]:
	"""What Bing suggests for a term, which is what Bing expects to be asked."""
	if not seed.strip():
		return []

	body = _fetch(AUTOSUGGEST_URL.format(query=urllib.parse.quote(seed)))

	if not body:
		return []

	try:
		payload = json.loads(body)
	except json.JSONDecodeError:
		return []

	# Opensearch shape: [term, [suggestions], ...]
	if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
		return []

	return [_clean(s) for s in payload[1] if _clean(s)]


def wordlist_queries(count: int) -> list[str]:
	"""Seeds from nouns.txt, the last resort when nothing is reachable.

	Read here rather than borrowed from llm_utils so that a trends-only install
	never has to import the model client.
	"""
	try:
		with open(paths.nouns_file(), encoding="utf-8") as handle:
			nouns = [line.strip().lower() for line in handle if len(line.strip()) >= 3]
	except OSError:
		return []

	if not nouns:
		return []

	return random.sample(nouns, min(count, len(nouns)))


def _clean(text: str) -> str:
	"""Strip markup, collapse whitespace and drop punctuation Bing does not need."""
	text = re.sub(r"<[^>]+>", " ", text or "")
	text = re.sub(r"[\"'?!,;:]", " ", text)

	return " ".join(text.split()).strip().lower()


def query_from_task_description(description: str) -> str | None:
	"""A search query for a task phrased as an instruction.

	"Search on Bing to compare checking and savings account options" becomes
	the content words, then whatever Bing suggests for them, so the query is
	one Bing already recognises rather than the sentence itself.
	"""
	words = [w for w in _clean(description).split() if w not in INSTRUCTION_WORDS]

	if not words:
		return None

	seed = " ".join(words[:6])
	options = suggestions(seed)

	# Prefer a suggestion, since it is a query Bing has seen. The trimmed
	# sentence is a reasonable fallback and still beats typing the imperative.
	return options[0] if options else seed


def related_queries(count: int, seed: str | None = None) -> list[str]:
	"""`count` distinct queries, branching out the way the LLM prompt asks for.

	Trending queries first, since they need no expansion at all, then Bing's
	suggestions for each to reach the requested number.
	"""
	collected: list[str] = []
	seen: set[str] = set()

	def take(candidates):
		for candidate in candidates:
			if candidate and candidate not in seen and len(candidate) > 2:
				seen.add(candidate)
				collected.append(candidate)

				if len(collected) >= count:
					return True
		return False

	if seed:
		take(suggestions(seed))

	if len(collected) < count and take(trending_queries()):
		return collected[:count]

	if len(collected) < count:
		take(wikipedia_topics())

	# Expand what we have until the count is met. Iterating over a snapshot
	# because take() appends to the same list.
	for term in list(collected):
		if len(collected) >= count:
			break

		take(suggestions(term))

	# Nothing reachable: let the caller decide, rather than typing junk.
	return collected[:count]
