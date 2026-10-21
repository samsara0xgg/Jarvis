# ADR 0345 — Map search stays inside the area asked about, puts the nearest first, and says how far on foot

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0283, `query` as a search only biased to a circle round him, and `area` as "<query> in <area>" with distances still measured from him

## Context

- 2026-10-18: a search within 1.5 km returned 4 places of 5 about 7.2 km away, and a search
  within 5 km returned only places 78 to 90 km away, in Richmond. A `query` search sends his circle
  as a bias only. Nothing drops a place outside the circle or orders the rest by distance, and the
  summary says only "N places for …".
- `area=` adds "in <area>" to the words and ignores the radius. The distances are still measured
  from him: 7,248 m in one answer about a place in the area he named.
- Google's Text Search takes only a rectangle as a hard restriction; a circle is only a bias. Its
  results can be ranked by distance. Nearby Search, which `kind=` uses, takes a circle as a
  restriction and is ranked by distance already.
- A result does not say whether a place has closed for good, or when it next closes or opens.
  Every distance is a straight line.
- At his volume, Google's free monthly usage covers a walking-time request (ADR 0347).
- Here is fresh since ADR 0322. When it cannot confirm his position, the result carries a warning
  field, and the summary leaves it out.

## Decision

Search only inside the area asked about, put the places near him in order of distance, and say for
each whether it is open, when that changes, and how long the walk is.

- **Near him.** The search is restricted to the rectangle round his circle. Places outside the
  circle are dropped, and the rest are ordered by distance. When nothing is inside, one more
  request without the restriction finds the nearest match, and the answer says nothing is within
  the radius and how far the nearest one is.
- **In an area.** The area is looked up once, as a point and its bounds, by the place reader (ADR
  0346). The search is restricted to those bounds, and distances are measured from the area's
  point and said as from there.
- **Open or closed.** Each place says whether it is open, closed for now, or closed for good, and
  when it next closes or opens.
- **On foot.** The places within walking range get walking times from one route matrix request for
  the whole list.
- **Stops.** A question about bus stops goes to `bus` (ADR 0344).
- **Summary.** It says what was searched and where, then the nearest places with distance, walking
  time and whether each is open. When Here could not confirm his position, it says that first.

## Alternatives rejected

- **Bias the search and trust Google's ranking (today).** On 10-18 that returned only places 78 to
  90 km away for a 5 km search.
- **Keep the bias, and filter the results by distance.** The same 5 km search returned nothing
  inside its circle, so a filter would answer "nothing nearby" while a restricted search returns the
  nearer matches.
- **One Routes request per place for the walking times.** That is up to eight paid requests and
  waits for each search. A route matrix is one request, billed per place.
- **Turn the straight-line distance into minutes on foot.** Streets, water and hills are not
  straight lines: a café 400 m away across a harbour can be a 25-minute walk.

## Consequences

- A search with nothing inside its area pays a second request.
- The rectangle covers the corners outside the circle. Places Google returns there are dropped, so
  a limit of 5 can come back with fewer.
- Most searches near him add the matrix request and its wait.
- Distance order can put a weaker match before a better one a little farther away.
