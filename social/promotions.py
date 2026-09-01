"""The Promote wizard's copy: five templates, rendered from course facts.

Steps 1 and 2 of the wizard (PRD §3.3). A template is a **function of the
course**, not a stored row — the five cards never vary per trainer, so keeping
them in code makes adding a sixth a patch rather than a data migration some
deployment is missing.

Two things this module deliberately is not:

* **Not an LLM.** "Rewrite for LinkedIn" reshapes a caption by rule — hashtag
  placement, length caps, and what happens to the link. There is no model
  behind it. Saying so in the docs beats implying copy that thinks.
* **Not the publisher.** Rendering produces text; :mod:`social.publishing`
  decides what actually reaches a network. The split matters because the same
  caption is composed differently per destination, and that should be decided
  once, late, right before the call.

Every builder degrades rather than fails. A course with no reviews still
renders the testimonial template — it falls back to the rating, or to the
course's own promise — and reports which fallback it used in ``hint`` so the
wizard can tell the trainer why the copy reads generic.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

#: Caption ceilings per network, in characters. LinkedIn and Instagram both
#: hard-reject past these; Facebook's real limit is 63k, which no caption from
#: this module will ever approach, so it is capped at something readable.
CAPTION_LIMIT = {
    "linkedin": 3000,
    "facebook": 5000,
    "instagram": 2200,
}
DEFAULT_LIMIT = 2200

#: Instagram renders URLs as plain text — they are not tappable anywhere except
#: the profile bio. Pasting one anyway trains people to ignore the CTA, so the
#: link is swapped for this instead of shipped as dead text.
IG_LINK_TEXT = "Link in bio"


# --------------------------------------------------------------------------- #
# Course facts
# --------------------------------------------------------------------------- #


@dataclass
class CourseFacts:
    """Everything the five builders are allowed to know about a course.

    Gathered once, up front, so a template body is a pure string function and
    the query count does not depend on which card the trainer clicked.
    """

    title: str = ""
    subtitle: str = ""
    trainer_name: str = ""
    link_url: str = ""
    image_url: str = ""
    is_free: bool = False
    price_label: str = ""
    rating_avg: Decimal = Decimal("0")
    rating_count: int = 0
    enrolled_count: int = 0
    seats_left: int = 0
    batch_starts: str = ""
    testimonial: str = ""
    testimonial_author: str = ""
    outcomes: list = field(default_factory=list)


def course_facts(course) -> CourseFacts:
    """Read a ``Course`` into the flat shape the builders want."""
    from courses.models import Batch, CourseReview
    from courses.serializers import public_media_url

    facts = CourseFacts(
        title=course.title,
        subtitle=(course.subtitle or "").strip(),
        trainer_name=(getattr(course.trainer, "full_name", "") or "").strip(),
        link_url=course_url(course),
        image_url=public_media_url(course.thumbnail, course.thumbnail_key) or "",
        is_free=course.is_free,
        price_label=_price_label(course),
        rating_avg=course.rating_avg or Decimal("0"),
        rating_count=course.rating_count or 0,
        enrolled_count=course.enrolled_count or 0,
        outcomes=[str(o) for o in (course.outcomes or []) if str(o).strip()][:3],
    )

    batch = (
        Batch.objects.filter(course=course, capacity__gt=0)
        .order_by("start_date")
        .first()
    )
    if batch:
        facts.seats_left = max(batch.capacity - batch.enrolled_count, 0)
        facts.batch_starts = (
            batch.start_date.strftime("%d %b") if batch.start_date else ""
        )

    review = (
        CourseReview.objects.filter(course=course, rating__gte=4)
        .exclude(comment="")
        .select_related("student")
        .order_by("-rating", "-created_at")
        .first()
    )
    if review:
        facts.testimonial = _clip(review.comment.strip().replace("\n", " "), 180)
        facts.testimonial_author = (
            getattr(review.student, "full_name", "") or "A recent learner"
        )
    return facts


def course_url(course) -> str:
    """The public landing page a promo post points at.

    Built from the frontend *origin* and a configurable path, for the reason
    documented in ``social.oauth.frontend_origin``: ``FRONTEND_URL`` carries the
    student app's own path segment, and concatenating onto it would send every
    click on every promoted post to a 404.
    """
    from social.oauth import frontend_origin

    template = getattr(settings, "SOCIAL_COURSE_URL_PATH", "") or "/courses/{slug}"
    path = template.format(slug=course.slug)
    if not path.startswith("/"):
        path = "/" + path
    return frontend_origin() + path


def _price_label(course) -> str:
    """A human price for the CTA line, or "" when there is nothing to say."""
    if course.is_free:
        return ""
    price = (
        course.prices.filter(pricing_type="one_time").order_by("amount").first()
        or course.prices.order_by("amount").first()
    )
    if price is None or not price.amount:
        return ""
    amount = price.amount
    if amount == amount.to_integral():
        amount = amount.quantize(Decimal("1"))
    symbol = "₹" if (price.currency or "").upper() == "INR" else price.currency + " "
    return "{}{:,}".format(symbol, amount)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# The five templates
# --------------------------------------------------------------------------- #

#: Used whenever a course has no subtitle. Generic on purpose — a trainer who
#: hasn't written a subtitle still gets something postable, and ``hint`` tells
#: them where better copy would come from.
DEFAULT_HOOK = (
    "A practical, project-led course you can start today. "
    "Learn by building, finish with proof."
)

ROCKET = "\U0001f680"
TARGET = "\U0001f3af"
HOURGLASS = "⏳"
SPEECH = "\U0001f4ac"
STAR = "⭐"
PARTY = "\U0001f389"


def _hook(facts) -> str:
    return facts.subtitle or DEFAULT_HOOK


def _cta(facts) -> str:
    if facts.is_free:
        return "Enrol now for free."
    if facts.price_label:
        return "Enrol now for {}.".format(facts.price_label)
    return "Enrol now."


def _launch(facts):
    body = "{} Just launched: {}!\n\n{}\n\n{}".format(
        ROCKET, facts.title, _hook(facts), _cta(facts)
    )
    hint = (
        ""
        if facts.subtitle
        else "Using default copy — add a course subtitle for a sharper hook."
    )
    return body, hint


def _discount(facts):
    if facts.price_label:
        offer = "Now {} for a limited time.".format(facts.price_label)
    elif facts.is_free:
        offer = "Free while this cohort fills."
    else:
        offer = "Special pricing for a limited time."
    body = "{} {} — limited-time offer.\n\n{}\n\n{} Offer closes soon.".format(
        TARGET, facts.title, _hook(facts), offer
    )
    hint = (
        ""
        if facts.price_label or facts.is_free
        else "No price set on this course, so the offer line stays vague. "
        "Set a price to name the number."
    )
    return body, hint


def _last_seats(facts):
    if facts.seats_left:
        seats = "Only {} seat{} left in this cohort.".format(
            facts.seats_left, "s" if facts.seats_left != 1 else ""
        )
        hint = ""
    else:
        seats = "The current cohort is nearly full."
        hint = "No batch with a capacity found, so the seat count stays vague."
    starts = " We start {}.".format(facts.batch_starts) if facts.batch_starts else ""
    body = "{} Final seats: {}\n\n{}{}\n\n{}".format(
        HOURGLASS, facts.title, seats, starts, _cta(facts)
    )
    return body, hint


def _testimonial(facts):
    if facts.testimonial:
        quote = '{} "{}"\n— {}'.format(
            SPEECH, facts.testimonial, facts.testimonial_author
        )
        hint = ""
    elif facts.rating_count:
        quote = "{} Rated {} by {} learner{}.".format(
            STAR,
            facts.rating_avg,
            facts.rating_count,
            "s" if facts.rating_count != 1 else "",
        )
        hint = "No written review yet — using the star rating instead."
    else:
        quote = "{} {}".format(SPEECH, _hook(facts))
        hint = "No reviews yet, so this reads as a promise rather than proof."
    body = "{}\n\n{}\n\n{}".format(quote, facts.title, _cta(facts))
    return body, hint


def _milestone(facts):
    if facts.enrolled_count:
        head = "{} {:,} learner{} now joined {}.".format(
            PARTY,
            facts.enrolled_count,
            "s have" if facts.enrolled_count != 1 else " has",
            facts.title,
        )
        hint = ""
    else:
        head = "{} {} is live and growing.".format(PARTY, facts.title)
        hint = "No enrolments yet, so there is no number to celebrate."
    rating = (
        "Rated {}/5 by {} of them.".format(facts.rating_avg, facts.rating_count)
        if facts.rating_count
        else _hook(facts)
    )
    body = "{}\n\n{}\n\n{}".format(head, rating, _cta(facts))
    return body, hint


@dataclass(frozen=True)
class Template:
    """One card in step 1 of the wizard."""

    key: str
    badge: str
    title: str
    blurb: str
    hashtags: tuple
    build: object


TEMPLATES = (
    Template(
        "launch", "Launch", "Just launched", "Announce a brand-new course.",
        ("#learning", "#newcourse", "#upskill"), _launch,
    ),
    Template(
        "discount", "Discount", "Limited discount",
        "Drive urgency with a time-boxed offer.",
        ("#offer", "#learning", "#upskill"), _discount,
    ),
    Template(
        "last_seats", "Last seats", "Last seats",
        "Fill the final spots in a cohort.",
        ("#lastseats", "#cohort", "#learning"), _last_seats,
    ),
    Template(
        "testimonial", "Testimonial", "Learner testimonial",
        "Lead with social proof.",
        ("#studentsuccess", "#learning", "#review"), _testimonial,
    ),
    Template(
        "milestone", "Milestone", "Milestone",
        "Celebrate traction and growth.",
        ("#milestone", "#community", "#learning"), _milestone,
    ),
)

TEMPLATES_BY_KEY = {t.key: t for t in TEMPLATES}

#: ``Campaign.template`` choices, kept here so the catalog is the single source
#: of truth and the model cannot drift from what the API offers.
TEMPLATE_CHOICES = [(t.key, t.title) for t in TEMPLATES]


def render(template_key: str, facts: CourseFacts) -> dict:
    """Render one template into the fields step 2 pre-fills."""
    template = TEMPLATES_BY_KEY[template_key]
    caption, hint = template.build(facts)
    return {
        "key": template.key,
        "badge": template.badge,
        "title": template.title,
        "blurb": template.blurb,
        "caption": caption,
        "hashtags": " ".join(template.hashtags),
        "hint": hint,
    }


def render_all(facts: CourseFacts) -> list:
    return [render(t.key, facts) for t in TEMPLATES]


# --------------------------------------------------------------------------- #
# Per-network shaping
# --------------------------------------------------------------------------- #


def rewrite(caption: str, provider: str) -> str:
    """Reshape a caption for one network. Rule-based, not generated.

    What actually differs between the three is smaller than it looks, and all
    of it is mechanical: Instagram wants the link gone, Facebook rewards a lead
    that survives the "See more" fold, LinkedIn is happy with the long form as
    written. Nothing here invents a sentence the trainer did not write.
    """
    text = "\n\n".join(p.strip() for p in caption.split("\n\n") if p.strip())

    if provider == "instagram":
        # Any URL in an IG caption is dead text — see IG_LINK_TEXT.
        text = _strip_urls(text)
    elif provider == "facebook":
        # The feed truncates after roughly three lines behind a "See more", so
        # the ask has to survive the fold: keep the lead and the CTA.
        paragraphs = text.split("\n\n")
        if len(paragraphs) > 3:
            text = "\n\n".join(paragraphs[:2] + [paragraphs[-1]])

    return _clip_caption(text, provider)


def compose(caption: str, hashtags: str, link_url: str, provider: str) -> str:
    """The final string sent to a network: caption + link + hashtags.

    Assembled at publish time rather than stored, because one campaign fans out
    to destinations that treat a link differently. Storing the composed text
    would freeze one network's rules onto all of them.
    """
    body = rewrite(caption, provider)
    tail = []

    if link_url:
        if provider == "instagram":
            tail.append(IG_LINK_TEXT)
        elif link_url not in body:
            # A trainer who already pasted the link into their caption should
            # not get it twice; appending regardless reads like a bug they made.
            tail.append(link_url)
    if hashtags:
        tail.append(hashtags.strip())

    text = "\n\n".join([body] + tail) if tail else body
    return _clip_caption(text, provider)


#: Matches a bare URL anywhere in a caption, including one sitting alone on its
#: own line — which is exactly where a trainer puts it, so splitting on spaces
#: would have missed the common case.
_URL_RE = re.compile(r"https?://\S+")


def _strip_urls(text: str) -> str:
    """Remove URLs, then tidy the whitespace they leave behind.

    Paragraph breaks survive; a line that held nothing but a link does not.
    """
    paragraphs = []
    for block in _URL_RE.sub("", text).split("\n\n"):
        lines = [" ".join(line.split()) for line in block.split("\n")]
        block = "\n".join(line for line in lines if line)
        if block:
            paragraphs.append(block)
    return "\n\n".join(paragraphs)


def _clip_caption(text: str, provider: str) -> str:
    limit = CAPTION_LIMIT.get(provider, DEFAULT_LIMIT)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Image choice
# --------------------------------------------------------------------------- #

#: The "Auto promo card" toggle in step 2 needs a server-side image pipeline —
#: rasterising the title over the course's brand colour, then uploading the
#: result somewhere Meta can fetch it. That does not exist, and there is no
#: image library in the requirements to build it with. Declared unavailable
#: rather than quietly aliased to the thumbnail: the same idiom the connect
#: page uses for X and YouTube, so the frontend greys one button instead of
#: learning a second rule.
PROMO_CARD_AVAILABLE = False


def image_options(facts) -> list:
    """The Image row in step 2, in the order the mock draws it."""
    return [
        {
            "value": "thumbnail",
            "label": "Course thumbnail",
            "available": bool(facts.image_url),
            "url": facts.image_url,
            "note": "" if facts.image_url else "This course has no cover image yet.",
        },
        {
            "value": "promo_card",
            "label": "Auto promo card",
            "available": PROMO_CARD_AVAILABLE,
            "url": "",
            "note": "" if PROMO_CARD_AVAILABLE else "Not available on this server yet.",
        },
        {
            "value": "none",
            "label": "No image",
            "available": True,
            "url": "",
            "note": "Instagram can't be a destination without an image.",
        },
    ]


def resolve_image(facts, source: str) -> str:
    """The URL a campaign will actually post, for a chosen source."""
    if source == "thumbnail":
        return facts.image_url
    return ""
