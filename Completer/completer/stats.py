"""The arithmetic behind a verdict.

Only a sample of the articles is asked about, so every share comes with a
margin. A download is failed only when even the friendliest reading of the
sample says it cannot be repaired; in every other case it is let through.
"""

import math

Z = 2.576  # 99 percent confidence, two-sided


def wilson(hits, sample, population=None, z=Z):
    """Range (low, high) in which the true share of hits lies, given ``hits`` among ``sample`` picked.

    ``population`` is how many there are in total; once all of them were looked
    at, the range collapses to the exact share.
    """
    if sample <= 0:
        return 0.0, 1.0
    share = float(hits) / sample
    if population is not None:
        if sample >= population:
            return share, share
        if population > 1:
            z = z * math.sqrt(float(population - sample) / (population - 1))
    z2 = z * z
    centre = (share + z2 / (2.0 * sample)) / (1.0 + z2 / sample)
    half = z * math.sqrt(share * (1.0 - share) / sample + z2 / (4.0 * sample * sample)) / (1.0 + z2 / sample)
    return max(0.0, centre - half), min(1.0, centre + half)


def capacity(allowed, par_lost):
    """Share of the content that can be missing and still be repaired.

    ``allowed`` is the share that can be repaired when all repair data arrives
    (repair bytes divided by content bytes); ``par_lost`` is the share of the
    repair data that is itself missing. This is NZBGet's health rule
    (health below critical health) solved for the missing share.
    """
    par_lost = min(1.0, max(0.0, par_lost))
    return allowed * (1.0 - par_lost) / (1.0 + allowed * par_lost)


class Tally(object):
    """Counts for one kind of article (content or repair)."""

    def __init__(self, listed, gaps=0):
        self.listed = listed   # articles the nzb names, each of which can be asked about
        self.gaps = gaps       # articles the nzb leaves out; lost for certain
        self.checked = 0
        self.missing = 0       # every server that would be used says it does not have it
        self.unknown = 0       # not found, but at least one server could not be asked
        self.fill = 0          # found only on a fill server (a level above the first)

    @property
    def total(self):
        return self.listed + self.gaps

    def lost(self, pessimistic=False):
        """(low, high) share of all articles that is gone; ``pessimistic`` counts the unknown as gone."""
        if self.total <= 0:
            return 0.0, 0.0
        hits = self.missing + (self.unknown if pessimistic else 0)
        low, high = wilson(hits, self.checked, self.listed)
        return ((self.gaps + low * self.listed) / self.total,
                (self.gaps + high * self.listed) / self.total)

    def estimate(self, pessimistic=False):
        """Best single guess of the share that is gone."""
        if self.total <= 0:
            return 0.0
        hits = self.missing + (self.unknown if pessimistic else 0)
        share = float(hits) / self.checked if self.checked else 0.0
        return (self.gaps + share * self.listed) / self.total

    def fill_share(self):
        """(low, high) share of all articles that only a fill server has."""
        if self.total <= 0:
            return 0.0, 0.0
        low, high = wilson(self.fill, self.checked, self.listed)
        return low * self.listed / self.total, high * self.listed / self.total


CLEAN_SAMPLE = 400  # this many content articles without a single miss is proof enough


def judge(data, par, allowed, final=False):
    """What the sample says so far: 'pass', 'fail', 'suspect' or 'more' (keep sampling).

    fail     even counting only articles that are missing for certain, and
             assuming the best about the repair data, too much is gone
    pass     even counting every doubtful article as gone, it can be repaired;
             or nothing at all is missing; or, with the sample exhausted
             (``final``), failure could not be shown
    suspect  it looks unrepairable, but only when the articles that some
             server could not be asked about are counted as gone
    """
    sure_low, _ = data.lost()
    suspect_low, worst_high = data.lost(pessimistic=True)
    par_low, _ = par.lost()
    _, par_high = par.lost(pessimistic=True)
    if par.listed > 0 and par.checked == 0:
        par_high = 1.0  # nothing is known about the repair data yet
    most = capacity(allowed, par_low)
    least = capacity(allowed, par_high)

    if sure_low > most:
        return "fail"
    if worst_high < least:
        return "pass"
    clean = data.missing == 0 and data.unknown == 0 and data.gaps == 0
    if clean and (worst_high < allowed or data.checked >= CLEAN_SAMPLE or data.checked >= data.listed):
        return "pass"
    if data.unknown and suspect_low > most:
        return "suspect"  # asking about more articles cannot make an unreachable server answer
    everything = data.checked >= data.listed and par.checked >= par.listed
    if final or everything:
        return "suspect" if suspect_low > most else "pass"
    return "more"
